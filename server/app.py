"""
Tiny helper server for TikTok Video Downloader.

It does two things:
  1. Serves the built React app (dist/) so everything runs on one origin.
  2. Exposes /api/video/info and /api/video/download using yt-dlp, so links
     from YouTube / TikTok / Facebook etc. can actually be saved.

Run:  python server/app.py
Then: http://localhost:8000
"""

import io
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from urllib.parse import quote, urlparse

import yt_dlp
from flask import Flask, jsonify, request, send_file, send_from_directory

# Story helpers live in the shared core module (same directory as this file).
from vercel_core import (  # noqa: E402
    account_name,
    cached_file,
    cffi_requests,
    client_ip,
    download_filename,
    fetch_media,
    fetch_media_raced,
    fetch_stories,
    file_cache_key,
    media_from_meta,
    rate_allowed,
    SECURITY_HEADERS,
    safe_extension,
    SITE_NAME,
    StoryError,
    store_file,
    story_media,
    validate_media_url,
    validate_story_id,
)

# TikWM resolves TikTok CDN links from its own network. yt-dlp alone has to
# fight TikTok's app API from this machine, which makes every download slow.
from tikwm import tikwm_data, tikwm_media_candidates  # noqa: E402

try:
    # Newer yt-dlp needs an ImpersonateTarget object, not a plain string.
    from yt_dlp.networking.impersonate import ImpersonateTarget

    IMPERSONATE = ImpersonateTarget.from_str("chrome")
    SAFARI = ImpersonateTarget.from_str("safari")
except Exception:
    IMPERSONATE = "chrome"
    SAFARI = "safari"

try:
    # imageio-ffmpeg ships an ffmpeg binary, so no manual ffmpeg install is needed.
    import imageio_ffmpeg

    # Pass the exact .exe path — imageio names it ffmpeg-win-x86_64-*.exe,
    # which yt-dlp would not find if only the directory were given.
    FFMPEG_DIR = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:  # yt-dlp will still work for sites offering progressive formats
    FFMPEG_DIR = None

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST_DIR = os.path.join(BASE_DIR, "dist")
COOKIE_FILE = os.path.join(BASE_DIR, "server", "cookies.txt")
# Hosted platforms cannot ship cookies.txt through Git (it is gitignored).
# Set VIDEO_COOKIES to the Netscape file contents; it is written to a temp file.
ENV_COOKIE_FILE = os.path.join(tempfile.gettempdir(), "tiktok-video-downloader-cookies.txt")

# yt-dlp TikTok app-API config. Different apps/hosts behave differently, so when one
# path fails, another is tried.
AWEME_APP_INFO = "/aweme/28.2.3/2022823030/1128"
API_HOST_21 = "api21-normal-c-useast2a.tiktokv.com"
API_HOST_22 = "api22-normal-c-useast2a.tiktokv.com"


def prepare_cookie_file():
    raw = os.environ.get("VIDEO_COOKIES", "").strip()
    if not raw:
        return
    # Session secrets — create owner-readable-only (0600).
    fd = os.open(ENV_COOKIE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(raw.replace("\\n", "\n"))
    try:
        os.chmod(ENV_COOKIE_FILE, 0o600)
    except OSError:
        pass


prepare_cookie_file()

# Only known video platforms are accepted. This keeps the server from being
# used as a generic URL fetcher (SSRF) while covering what the UI promises.
ALLOWED_HOSTS = [
    "youtube.com", "youtu.be", "youtube-nocookie.com",
    "tiktok.com", "vtiktok.com",
    "facebook.com", "facebook.net", "fb.com", "fb.watch",
    "instagram.com", "cdninstagram.com",
    "vimeo.com", "vimeocdn.com",
    "x.com", "twitter.com", "t.co",
    "reddit.com", "redd.it",
    "dailymotion.com", "bilibili.com", "threads.net",
    "linkedin.com", "pinterest.com", "pinimg.com",
    "snapchat.com", "twitch.tv",
]
TIKTOK_HOSTS = ("tiktok.com", "vtiktok.com")

# Quality choices exposed by the UI. Video + audio are merged into MP4 with
# ffmpeg (bundled via imageio-ffmpeg). Higher quality = bigger file = slower.
VIDEO_FORMATS = {
    "480": "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
    "720": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
}
DEFAULT_QUALITY = "720"
MAX_BYTES = 300 * 1024 * 1024


def video_selector(height, tiktok=False, exact=False):
    """Pick one already-muxed TikTok stream instead of muxing two again.

    TikTok's app API returns progressive MP4s (picture + AAC in the same
    file) and a separate music-only track. `bestvideo+bestaudio` pairs the
    picture with that second track, which shifts the dialogue a few seconds
    out of sync in the saved file. H.264 wins over HEVC even at a lower height:
    HEVC needs a full re-encode that adds seconds after the ad.
    """
    bound = f"[height={height}]" if exact else f"[height<={height}]"
    if tiktok:
        return (
            f"best{bound}[vcodec^=h264][acodec!=none]/"
            f"best{bound}[vcodec^=avc1][acodec!=none]/"
            f"best{bound}[vcodec!=none][acodec!=none]/"
            f"best{bound}[vcodec!=none]/best"
        )
    return f"bestvideo{bound}+bestaudio/best{bound}/best"

# yt-dlp metadata extraction costs a few seconds per link. Cache short-lived
# results so re-opening the same link is instant — keep them on disk too so a
# server restart does not make the demo feel slow again.
INFO_CACHE_TTL = 300  # seconds
INFO_CACHE_MAX = 50
INFO_CACHE_FILE = os.path.join(tempfile.gettempdir(), "tiktok-video-downloader-info-cache.json")


def load_info_cache():
    try:
        with open(INFO_CACHE_FILE, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        now = time.time()
        return {
            key: (stamp, value)
            for key, (stamp, value) in raw.items()
            if now - stamp < INFO_CACHE_TTL
        }
    except Exception:
        return {}


def persist_info_cache():
    try:
        with open(INFO_CACHE_FILE, "w", encoding="utf-8") as handle:
            json.dump(INFO_CACHE, handle)
    except Exception:
        pass


INFO_CACHE = load_info_cache()

app = Flask(__name__, static_folder=None)


@app.before_request
def limit_api_requests():
    path = request.path
    if not path.startswith("/api/video/"):
        return None
    bucket = "download" if path.endswith("/download") else "info"
    if not rate_allowed(client_ip(request), bucket):
        return jsonify({"error": "Too many requests. Please try again in a few minutes."}), 429
    return None


@app.after_request
def apply_security_headers(response):
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


def json_body():
    """Only a JSON object is a valid request body — never a list or scalar."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def validate_link(raw):
    """Return a safe URL or raise ValueError. Blocks local/private targets."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Please paste a video link.")

    value = raw.strip()
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value

    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise ValueError("Only http/https links are allowed.")

    if not any(host == domain or host.endswith("." + domain) for domain in ALLOWED_HOSTS):
        raise ValueError("That site is not supported. Paste a link from a supported platform.")

    try:
        addresses = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ValueError("That site could not be resolved.") from None

    for entry in addresses:
        ip = ipaddress.ip_address(entry[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError("Local/private addresses are blocked.")

    return value


def parse_mode(value):
    mode = str(value or "video").strip().lower()
    return mode if mode in ("video", "mp3", "story") else "video"


def is_tiktok_link(url):
    host = (urlparse(url).hostname or "").lower()
    return any(host == name or host.endswith("." + name) for name in TIKTOK_HOSTS)


def ffprobe_text(path):
    """Lowercase ffmpeg stream description, used only for compatibility checks."""
    if not FFMPEG_DIR:
        return ""
    try:
        result = subprocess.run(
            [FFMPEG_DIR, "-hide_banner", "-i", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
        return (result.stderr or "").lower()
    except Exception:
        return ""


def ensure_real_mp3(path, workdir):
    """Convert mislabeled AAC music to a real MP3; keep genuine MP3 files."""
    if not FFMPEG_DIR:
        return path
    if "audio: mp3" in ffprobe_text(path):
        return path
    output = os.path.join(workdir, "audio.mp3")
    subprocess.run(
        [FFMPEG_DIR, "-y", "-loglevel", "error", "-i", path, "-vn",
         "-codec:a", "libmp3lame", "-b:a", "128k", output],
        check=True, timeout=120, capture_output=True,
    )
    return output


def ensure_compatible_video(path, workdir):
    """Re-encode only HEVC/10-bit files; H.264/yuv420p is saved untouched."""
    if not FFMPEG_DIR:
        return path
    info = ffprobe_text(path)
    video = next((line for line in info.splitlines() if "video:" in line), "")
    if "h264" in video or "avc" in video:
        if "yuv420p10" not in video and "yuv444" not in video and "yuv420p" in video:
            return path
    output = os.path.join(workdir, "compatible.mp4")
    subprocess.run(
        [FFMPEG_DIR, "-y", "-loglevel", "error", "-fflags", "+genpts", "-i", path,
         "-c:v", "libx264", "-profile:v", "high", "-preset", "veryfast",
         "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "copy",
         "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", output],
        check=True, timeout=240, capture_output=True,
    )
    return output


def tikwm_payload(data):
    """Shape a TikWM response like the info endpoint's normal payload."""
    author = data.get("author") or {}
    size = data.get("hd_size") or data.get("size") or 0
    video_urls = tikwm_media_candidates(data, "video")
    audio_urls = tikwm_media_candidates(data, "mp3")
    return {
        "title": data.get("title") or "video",
        "duration": data.get("duration"),
        "thumbnail": data.get("cover") or data.get("origin_cover"),
        "uploader": f"@{author.get('unique_id')}" if author.get("unique_id") else None,
        "platform": "TikTok",
        "formats": [{"height": 720, "label": "HD", "ext": "mp4", "filesize": int(size)}],
        "variant": "tikwm",
        # Hand the resolved CDN mirrors to /download so it never has to resolve
        # the same link twice.
        "media": {
            "video": video_urls[0] if video_urls else None,
            "audio": audio_urls[0] if audio_urls else None,
            "video_alternates": video_urls,
            "audio_alternates": audio_urls,
            "title": data.get("title") or "tiktok",
            "id": data.get("id") or "video",
            "uploader": author.get("unique_id") or author.get("nickname"),
            "duration": data.get("duration"),
            "size": int(size),
        },
    }


def ydl_options(outtmpl=None, variant="app22", mode="video", fast=False):
    """Build yt-dlp options for one TikTok access strategy."""
    options = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 8 if fast else 20,
        "retries": 0 if fast else 1,
        "noplaylist": True,
        "concurrent_fragment_downloads": 8,
        # curl-cffi impersonation must actually be enabled — otherwise the
        # "install curl-cffi" hint was pure misinformation.
        "impersonate": IMPERSONATE,
    }
    if mode == "mp3":
        options["format"] = "bestaudio/best"
        options["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    if variant == "aweme1128":
        # This path was the most reliable in local testing.
        options["extractor_args"] = {"tiktok": {"app_info": [AWEME_APP_INFO]}}
    elif variant == "app22":
        options["extractor_args"] = {
            "tiktok": {"app_info": [""], "api_hostname": [API_HOST_22]}
        }
    elif variant == "app21":
        options["extractor_args"] = {
            "tiktok": {"app_info": [""], "api_hostname": [API_HOST_21]}
        }
    elif variant == "app16":
        options["extractor_args"] = {"tiktok": {"app_info": [""]}}
    elif variant == "safari":
        options["impersonate"] = SAFARI
    # variant "web" keeps the plain Chrome webpage path as the last resort.
    if FFMPEG_DIR:
        options["ffmpeg_location"] = FFMPEG_DIR
    cookie_path = ENV_COOKIE_FILE if os.environ.get("VIDEO_COOKIES", "").strip() else COOKIE_FILE
    if os.path.exists(cookie_path):
        options["cookiefile"] = cookie_path
    if outtmpl:
        options["outtmpl"] = outtmpl
    return options


STRATEGIES = ("app22", "app16", "aweme1128", "app21", "safari", "web")


def ydl_variants(outtmpl=None, mode="video", fast=False, preferred=None, tiktok=True):
    """Ordered fallback strategies — first success wins.

    `preferred` is the path that worked during the metadata lookup. Trying it
    first for the download avoids burning time on paths TikTok just blocked.
    Non-TikTok extractors ignore the TikTok app arguments, so repeating six
    variants for them was pure latency — one attempt is enough.
    """
    if not tiktok:
        return [ydl_options(outtmpl, "web", mode, fast=fast)]
    order = list(STRATEGIES)
    if preferred in order:
        order.remove(preferred)
        order.insert(0, preferred)
    # Metadata only needs the three fastest paths. Downloads race every path:
    # TikTok blocks them at random, so dropping the last two could discard the
    # one route that actually works. Since they run in parallel, extra paths
    # add resilience without making the visitor wait longer.
    if fast:
        order = order[:3]
    return [ydl_options(outtmpl, name, mode, fast=fast) for name in order]


def ydl_info_payload(url, options, variant, tiktok=True):
    """Metadata from one yt-dlp strategy, labelled with the path that worked."""
    with yt_dlp.YoutubeDL(options) as ydl:
        meta = ydl.extract_info(url, download=False)
        try:
            formats = quality_formats(ydl, meta, tiktok=tiktok)
        except Exception:
            formats = []
    return {
        "title": meta.get("title") or "video",
        "duration": meta.get("duration"),
        "thumbnail": meta.get("thumbnail"),
        "uploader": meta.get("uploader"),
        "platform": meta.get("extractor_key") or meta.get("extractor") or "video",
        "formats": formats,
        "variant": variant,
        "media": media_from_meta(meta) if tiktok else None,
    }


def resolve_tiktok_info(url):
    """First working resolver wins — no more waiting on TikWM before yt-dlp.

    TikTok blocks most app API hosts intermittently and TikWM is frequently
    unreachable. Trying them one after another made a single paste take 20+
    seconds. All paths are now started together and the losing requests are
    cancelled as soon as the first answer arrives.
    """
    def try_tikwm():
        data = tikwm_data(url)
        return tikwm_payload(data) if data else None

    def wave(names, timeout):
        tasks = [("tikwm", try_tikwm)] if "tikwm" in names else []
        for name in names:
            if name == "tikwm":
                continue
            options = ydl_options(variant=name, fast=True)
            tasks.append((name, lambda o=options, v=name: ydl_info_payload(url, o, v)))
        executor = ThreadPoolExecutor(max_workers=len(tasks))
        try:
            futures = [executor.submit(fn) for _, fn in tasks]
            try:
                for future in as_completed(futures, timeout=timeout):
                    try:
                        result = future.result()
                    except Exception as exc:
                        app.logger.debug("resolver failed [%s]: %s", type(exc).__name__, str(exc)[:200])
                        continue
                    if result:
                        return result
            except TimeoutError:
                pass
            return None
        finally:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)

    # Every resolver starts together and losers are cancelled on first success.
    # TikTok blocks app hosts at random; when a whole wave is rejected we give
    # it one quiet second chance instead of showing a 400 for a video that the
    # download race can still fetch. Cold fast answers are completely unchanged.
    result = wave(("tikwm",) + STRATEGIES, 10)
    if result:
        return result
    return wave(("tikwm",) + STRATEGIES, 6)


def race_ydl_download(url, workdir, variants, video_format, mode):
    """Download with several TikTok strategies at once; first file wins.

    TikTok blocks most app hosts at random. Trying them one by one meant four
    slow 403s in a row while the visitor waited. Each strategy writes to its
    own temporary folder outside the request workdir: the winner is copied
    into `workdir`, while losing workers keep writing safely until a helper
    thread cleans them up. This avoids deleting a folder under a live writer.
    """
    def attempt(race_root, index, options):
        localdir = os.path.join(race_root, f"try-{index}")
        os.makedirs(localdir, exist_ok=True)
        opts = dict(options)
        opts["outtmpl"] = os.path.join(localdir, "%(id)s.%(ext)s")
        if mode != "mp3":
            opts["format"] = video_format
        opts["merge_output_format"] = "mp4"
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                meta = ydl.extract_info(url, download=True)
                path = locate_download(localdir, ydl, meta, mode)
            if path and os.path.getsize(path) > 0:
                extension = os.path.splitext(path)[1] or (
                    ".mp3" if mode == "mp3" else ".mp4"
                )
                stable = os.path.join(workdir, f"winner-{index}{extension}")
                shutil.copy2(path, stable)
                return stable, meta
        except Exception as exc:
            return exc
        return RuntimeError("Downloaded file was not found.")

    def race_root_cleanup(futures, race_root):
        # Losing strategies can still be mid-write when a winner is returned.
        # Wait for them outside the response path, then remove their scratch.
        wait(futures)
        shutil.rmtree(race_root, ignore_errors=True)

    def run():
        race_root = tempfile.mkdtemp(prefix="tiktok-race-")
        executor = ThreadPoolExecutor(max_workers=len(variants))
        futures = [
            executor.submit(attempt, race_root, index, opts)
            for index, opts in enumerate(variants)
        ]
        result = None
        last_error = None
        try:
            for future in as_completed(futures, timeout=100):
                value = future.result()
                if isinstance(value, tuple):
                    result = value
                    break
                last_error = value
                app.logger.debug("download strategy failed [%s]: %s", type(value).__name__, str(value)[:200])
        except TimeoutError:
            last_error = last_error or RuntimeError("Download timed out.")
        finally:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            threading.Thread(
                target=race_root_cleanup, args=(futures, race_root), daemon=True
            ).start()
        return result, last_error

    result, last_error = run()
    if result:
        return result, last_error
    # A whole first wave can be 403'd during a short TikTok block. One quiet
    # retry succeeds often enough to be worth it, and costs nothing on the
    # normal path where a route wins immediately.
    time.sleep(0.8)
    return run()


def locate_download(workdir, ydl, meta, mode="video"):
    """Find the written file — FFmpegExtractAudio renames it to .mp3."""
    expected = ".mp3" if mode == "mp3" else None
    try:
        path = ydl.prepare_filename(meta)
    except Exception:
        path = None
    if expected and path:
        mp3 = os.path.splitext(path)[0] + expected
        if os.path.exists(mp3):
            return mp3
    if path and os.path.exists(path):
        return path
    files = [
        os.path.join(workdir, name)
        for name in os.listdir(workdir)
        if os.path.isfile(os.path.join(workdir, name))
    ]
    if expected:
        mp3_files = [item for item in files if item.lower().endswith(expected)]
        if mp3_files:
            return mp3_files[0]
    return files[0] if files else None


def error_response(message, status=400):
    return jsonify({"error": message}), status


def error_category(exc):
    text = str(exc).lower()
    if any(k in text for k in (
        "403", "forbidden", "blocked", "unable to download webpage", "sign in to confirm",
        "verify you are human", "captcha", "slardar", "challenge", "bot",
    )):
        return "blocked"
    if any(k in text for k in ("429", "too many requests", "rate limit")):
        return "rate"
    if any(k in text for k in (
        "private", "age", "login", "log in", "sign in", "cookie", "authenticat", "member",
    )):
        return "login"
    if any(k in text for k in (
        "unavailable", "removed", "deleted", "not found", "404", "does not exist",
        "expired", "invalid",
    )):
        return "unavailable"
    return "generic"


def with_hint(message, category="generic"):
    """Give users an actionable next step without leaking error internals."""
    if category == "blocked":
        hint = (
            " Tip: TikTok is blocking this network right now. Try again later, switch networks, "
            "or paste a direct video link."
        )
    elif category == "login":
        hint = " Tip: This clip is private, age-restricted, or login-only. Please use a public TikTok link."
    elif category == "unavailable":
        hint = " Tip: This clip is unavailable or may have been removed. Try another link."
    elif category == "rate":
        hint = " Tip: Too many requests right now. Wait a moment and try again."
    else:
        hint = " Tip: please try again with a fresh public TikTok link."
    return f"{message}{hint}"


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "tiktok-video-downloader"})


def quality_formats(ydl, meta, tiktok=False):
    """Estimated final MP4 size for each height the video actually offers.

    yt-dlp's own format selector is reused on the metadata we already have, so
    the number matches the streams /download will pick — no extra network call.
    """
    all_formats = meta.get("formats") or []
    heights = sorted({
        fmt.get("height")
        for fmt in all_formats
        if fmt.get("height") and fmt.get("vcodec") not in (None, "none")
    })

    result = []
    for height in heights:
        def size_of(fmt):
            return int(fmt.get("filesize") or fmt.get("filesize_approx") or 0)

        at_height = [
            fmt for fmt in all_formats
            if fmt.get("height") == height and fmt.get("vcodec") not in (None, "none")
        ]
        # A muxed MP4 already carries its audio. Adding the separate music
        # track here would overstate the size of the file we actually save.
        muxed = [size_of(fmt) for fmt in at_height if fmt.get("acodec") not in (None, "none")]
        size = max(muxed) if muxed else 0
        if not size:
            audio_sizes = [
                size_of(fmt) for fmt in all_formats
                if fmt.get("vcodec") in (None, "none") and fmt.get("acodec") not in (None, "none")
            ]
            size = max((size_of(fmt) for fmt in at_height), default=0) + max(audio_sizes, default=0)

        if size:
            result.append({"height": height, "ext": "mp4", "filesize": size})
    return result


@app.post("/api/video/info")
def video_info():
    data = json_body()
    if data is None:
        return error_response("Invalid request.")
    try:
        url = validate_link(data.get("url", ""))
    except ValueError as exc:
        return error_response(str(exc))

    mode = parse_mode(data.get("mode"))
    cache_key = url + "#story" if mode == "story" else url
    cached = INFO_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < INFO_CACHE_TTL:
        return jsonify(cached[1])

    if mode == "story":
        try:
            stories, username = fetch_stories(url)
            payload = {
                "kind": "story",
                "title": f"@{username} stories",
                "uploader": f"@{username}",
                "platform": "TikTok",
                "stories": stories,
                "formats": [],
            }
            INFO_CACHE[cache_key] = (time.time(), payload)
            persist_info_cache()
            return jsonify(payload)
        except StoryError as exc:
            return error_response(str(exc))
        except Exception as exc:
            app.logger.error("story info failed [%s]: %s", type(exc).__name__, str(exc)[:500])
            return error_response(with_hint("Could not load stories.", error_category(exc)))

    tiktok = is_tiktok_link(url)
    payload = None
    if tiktok:
        # TikWM and yt-dlp are tried at the same time; the first answer is the
        # one the visitor sees. A dead resolver never blocks a healthy one.
        payload = resolve_tiktok_info(url)
    else:
        try:
            payload = ydl_info_payload(
                url, ydl_variants(fast=True, tiktok=False)[0], "web", tiktok=False
            )
        except Exception as exc:
            app.logger.error("video info failed [%s]: %s", type(exc).__name__, str(exc)[:500])
            payload = None

    if payload:
        if len(INFO_CACHE) >= INFO_CACHE_MAX:
            INFO_CACHE.pop(next(iter(INFO_CACHE)))
        INFO_CACHE[cache_key] = (time.time(), payload)
        persist_info_cache()
        return jsonify(payload)

    return error_response(with_hint("Could not read video information.", "generic"))


def make_file_response(path, meta):
    """Share the response headers between a fresh download and a cache hit."""
    size = os.path.getsize(path)
    if size > MAX_BYTES:
        return error_response("The file is over 300 MB — it cannot be saved.")

    with open(path, "rb") as handle:
        payload = io.BytesIO(handle.read())

    extension = safe_extension(path)
    preferred = download_filename(meta, extension)

    # Browsers that ignore filename*= still get the same account + ID + site
    # shape, without depending on a Unicode title.
    video_id = re.sub(r"[^A-Za-z0-9_-]+", "", str(meta.get("id") or ""))[:24]
    fallback = "_".join(
        part for part in (account_name(meta), video_id, SITE_NAME) if part
    )[:110]
    if not fallback.lower().endswith(extension.lower()):
        fallback += extension
    disposition = (
        f"attachment; filename=\"{fallback}\"; "
        f"filename*=UTF-8''{quote(preferred, safe='')}"
    )

    if extension.lower() == ".mp3":
        mimetype = "audio/mpeg"
    elif extension.lower() in (".jpg", ".jpeg", ".png"):
        mimetype = "image/jpeg"
    else:
        mimetype = "video/mp4"
    response = send_file(payload, mimetype=mimetype)
    response.headers["Content-Disposition"] = disposition
    return response


@app.post("/api/video/download")
def video_download():
    data = json_body()
    if data is None:
        return error_response("Invalid request.")
    try:
        url = validate_link(data.get("url", ""))
    except ValueError as exc:
        return error_response(str(exc))

    mode = parse_mode(data.get("mode"))
    tiktok = is_tiktok_link(url)
    if mode == "mp3" and not FFMPEG_DIR:
        return error_response("FFmpeg is not available for MP3 conversion. Configure the server and try again.")

    workdir = tempfile.mkdtemp(prefix="tiktok-video-downloader-video-")
    try:
        quality = str(data.get("quality") or DEFAULT_QUALITY)
        if re.fullmatch(r"[0-9]{1,4}", quality):
            height = int(quality)
            if height < 144 or height > 4320:
                return error_response("Invalid video quality.")
            video_format = video_selector(height, tiktok)
        elif quality in VIDEO_FORMATS:  # backwards compatibility
            video_format = video_selector(int(quality), tiktok)
        else:
            return error_response("Invalid video quality.")

        story_id = data.get("storyId")
        if story_id not in (None, ""):
            try:
                story_id = validate_story_id(story_id)
            except ValueError as exc:
                return error_response(str(exc))

        cache_key = file_cache_key(url, mode, quality, story_id)
        cached = cached_file(cache_key)
        if cached:
            # Already prepared — return it now instead of making the ad wait on
            # another TikTok extraction and CDN transfer.
            return make_file_response(*cached)

        last_error = None
        if mode == "story":
            media = story_media(story_id, url)
            if cffi_requests is None:
                raise RuntimeError("curl-cffi is required for Story downloads.")
            session = cffi_requests.Session(impersonate="chrome")
            content, content_type = fetch_media(media, session)
            extension = (
                ".jpg" if "image" in content_type
                else ".mp3" if "audio" in content_type
                else ".mp4"
            )
            path = os.path.join(workdir, "story" + extension)
            with open(path, "wb") as handle:
                handle.write(content)
            meta = {"title": f"tiktok-story-{story_id}", "id": story_id}
        else:
            def ydl_attempt():
                variants = ydl_variants(
                    os.path.join(workdir, "%(id)s.%(ext)s"),
                    mode,
                    preferred=data.get("variant"),
                    tiktok=tiktok,
                )
                # TikTok blocks hosts at random. Racing every route lets one
                # working path finish immediately instead of queueing 403s.
                return race_ydl_download(url, workdir, variants, video_format, mode)

            def mirror_attempt():
                if not tiktok or cffi_requests is None:
                    return None, None, None
                hint = data.get("media") if isinstance(data.get("media"), dict) else {}
                mirror_key = "audio_alternates" if mode == "mp3" else "video_alternates"
                candidates = [hint.get("audio" if mode == "mp3" else "video")]
                if mode != "mp3":
                    # Honour the chosen quality when the resolver supplied
                    # per-resolution mirrors; the generic list is fallback.
                    candidates += list((hint.get("by_height") or {}).get(str(height)) or [])
                candidates += list(hint.get(mirror_key) or [])
                mirrors = []
                for candidate in candidates:
                    try:
                        checked = validate_media_url(candidate)
                    except (ValueError, TypeError):
                        continue
                    if checked not in mirrors:
                        mirrors.append(checked)
                source = hint
                if not mirrors and data.get("variant") not in STRATEGIES:
                    # /info already found a yt-dlp route, so TikWM is known to
                    # be down — asking it again would only add dead time.
                    resolved = tikwm_data(url)
                    if resolved:
                        mirrors = tikwm_media_candidates(resolved, mode)
                        source = resolved
                if not mirrors:
                    return None, None, None
                try:
                    content, _content_type = fetch_media_raced(mirrors)
                    extension = ".mp3" if mode == "mp3" else ".mp4"
                    candidate = os.path.join(workdir, "media" + extension)
                    with open(candidate, "wb") as handle:
                        handle.write(content)
                    meta = {
                        "title": source.get("title") or "tiktok",
                        "id": source.get("id") or "video",
                        "uploader": source.get("uploader"),
                        "duration": source.get("duration"),
                    }
                    return candidate, meta, None
                except Exception as exc:
                    app.logger.error("media download failed [%s]: %s", type(exc).__name__, str(exc)[:300])
                    return None, None, exc

            if mode == "video":
                # A CDN copy is often HEVC and needs a slow full re-encode. The
                # yt-dlp race picks TikTok's muxed H.264 stream instead, which is
                # both quicker and already in sync. Mirrors stay as fallback.
                result, failure = ydl_attempt()
                if result:
                    path, meta = result
                else:
                    app.logger.error(
                        "yt-dlp race failed [%s]: %s",
                        type(failure).__name__, str(failure)[:300],
                    )
                    last_error = failure
                    path, meta, failure = mirror_attempt()
                    if not path:
                        last_error = failure or last_error
                        return error_response(
                            with_hint("Download failed.", error_category(last_error))
                        )
            else:
                path, meta, failure = mirror_attempt()
                if not path:
                    result, last_error = ydl_attempt()
                    if not result:
                        last_error = last_error or failure
                        return error_response(
                            with_hint("Download failed.", error_category(last_error))
                        )
                    path, meta = result

        if mode != "story":
            path = (
                ensure_real_mp3(path, workdir) if mode == "mp3"
                else ensure_compatible_video(path, workdir)
            )
        store_file(cache_key, path, meta, os.path.getsize(path))
        return make_file_response(path, meta)
    except StoryError as exc:
        return error_response(str(exc))
    except Exception as exc:
        app.logger.error("video download failed [%s]: %s", type(exc).__name__, str(exc)[:500])
        return error_response(with_hint("Download failed.", error_category(exc)))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def frontend(path):
    if path:
        target = os.path.join(DIST_DIR, path)
        if os.path.isfile(target):
            return send_from_directory(DIST_DIR, path)
    index = os.path.join(DIST_DIR, "index.html")
    if os.path.isfile(index):
        return send_from_directory(DIST_DIR, "index.html")
    return "Frontend build missing. Run 'npm run build' first.", 404


if __name__ == "__main__":
    # Local dev keeps Flask's reloader. Hosts like Render/Railway/Fly set PORT —
    # there we serve with a real WSGI server and never expose the debugger.
    port = int(os.environ.get("PORT", "8000"))
    production = bool(os.environ.get("PORT")) or os.environ.get("PRODUCTION") == "1"

    if production:
        try:
            from waitress import serve
        except ImportError as exc:
            raise SystemExit(
                "waitress is required in production — run "
                "'python -m pip install -r server/requirements.txt'"
            ) from exc
        serve(app, host="0.0.0.0", port=port, threads=8)
    else:
        # Werkzeug's debug console allows remote code execution if reachable.
        # Bind local dev to loopback; opt in to LAN only with HOST explicitly.
        app.run(host=os.environ.get("HOST", "127.0.0.1"), port=port, debug=True)
