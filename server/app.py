"""
Tiny helper server for TikTok Video Downloader.

It does two things:
  1. Serves the built React app (dist/) so everything runs on one origin.
  2. Exposes /api/video/info and /api/video/download using yt-dlp, so links
     from YouTube / TikTok / Facebook etc. can actually be saved.

Run:  python server/app.py
Then: http://localhost:8000
"""

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
# Keep the local helper in step with the shared cache/Vercel ceilings. A file
# that cannot be cached cannot be prepared ahead of the ad beat.
MAX_BYTES = 250 * 1024 * 1024


def video_selector(height, tiktok=False, exact=False):
    """Pick the requested resolution, not merely the highest bitrate below it.

    TikTok's app API returns progressive MP4s (picture + AAC in the same
    file) and a separate music-only track. `bestvideo+bestaudio` pairs the
    picture with that second track, which shifts the dialogue a few seconds
    out of sync in the saved file. H.264 wins over HEVC even at a lower height:
    HEVC needs a full re-encode that adds seconds after the ad.

    When the visitor picked a resolution we know exists, try that exact height
    first — otherwise `best[height<=1280]` silently saves a smaller 576x1024
    file even though a 720x1280 stream was offered.
    """
    bounds = [f"[height={height}]", f"[height<={height}]"] if exact else [f"[height<={height}]"]
    if tiktok:
        chains = []
        for bound in bounds:
            chains.extend((
                f"best{bound}[vcodec^=h264][acodec!=none]",
                f"best{bound}[vcodec^=avc1][acodec!=none]",
                f"best{bound}[vcodec!=none][acodec!=none]",
                f"best{bound}[vcodec!=none]",
            ))
        chains.append("best")
        return "/".join(chains)
    return "/".join(
        f"bestvideo{bound}+bestaudio/best{bound}/best" for bound in bounds
    )

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
    bucket = "download" if path.endswith(("/download", "/prepare")) else "info"
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
    # HEVC usually arrives inside an MP4 already. Copying the streams keeps the
    # original picture and takes well under a second, while a full libx264 pass
    # costs 20-40 seconds on a long clip — the visitor would feel that after the
    # ad. Only re-encode when a clean remux is not possible.
    if "mov,mp4" in info or ".mp4" in path.lower():
        remux = os.path.join(workdir, "remux.mp4")
        try:
            subprocess.run(
                [FFMPEG_DIR, "-y", "-loglevel", "error", "-i", path,
                 "-c", "copy", "-tag:v", "hvc1", "-movflags", "+faststart", remux],
                check=True, timeout=60, capture_output=True,
            )
            return remux
        except Exception:
            pass
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
    try:
        height = int(data.get("height") or data.get("video_height") or 0)
    except (TypeError, ValueError):
        height = 0
    formats = []
    if height > 0:
        formats.append({
            "height": height,
            "label": f"{height}p",
            "ext": "mp4",
            "filesize": int(size or 0),
        })
    return {
        "title": data.get("title") or "video",
        "duration": data.get("duration"),
        "thumbnail": data.get("cover") or data.get("origin_cover"),
        "uploader": f"@{author.get('unique_id')}" if author.get("unique_id") else None,
        "platform": "TikTok",
        "formats": formats,
        "variant": "tikwm",
        # Hand the resolved CDN mirrors to /download so it never has to resolve
        # the same link twice.
        "media": {
            "video": video_urls[0] if video_urls else None,
            "audio": audio_urls[0] if audio_urls else None,
            "video_alternates": video_urls,
            "audio_alternates": audio_urls,
            "by_height": {str(height): video_urls} if height > 0 else {},
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


def ydl_variants(outtmpl=None, mode="video", fast=False, preferred=None, tiktok=True, limit=None):
    """Ordered fallback strategies — first success wins.

    `preferred` is the path that worked during the metadata lookup. Trying it
    first for the download avoids burning time on paths TikTok just blocked.
    Non-TikTok extractors ignore the TikTok app arguments, so repeating six
    variants for them was pure latency — one attempt is enough.

    `limit` caps the race when the winner is already known. Racing all six
    routes for a file whose CDN mirror just failed saturates the connection and
    makes even a working route finish late — after the four-second ad.
    """
    if not tiktok:
        return [ydl_options(outtmpl, "web", mode, fast=fast)]
    order = list(STRATEGIES)
    if preferred in order:
        order.remove(preferred)
        order.insert(0, preferred)
    if fast:
        limit = 3 if limit is None else min(limit, 3)
    if limit:
        order = order[:limit]
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


def race_ydl_download(url, workdir, variants, video_format, mode, timeout=100, retry=True):
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
            for future in as_completed(futures, timeout=timeout):
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
    if result or not retry:
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


class StalePreparation(Exception):
    """A better-hinted prepare job replaced this one before it finished."""


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


def make_file_response(path, meta, inline=False):
    """Stream a prepared file from disk — never buffer a 100 MB file in RAM."""
    size = os.path.getsize(path)
    if size > MAX_BYTES:
        return error_response("The file is over 250 MB — it cannot be saved.")

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
    response = send_file(path, mimetype=mimetype, conditional=True)
    if inline:
        # Same-origin preview: no attachment, no filename header. The browser
        # gets real bytes with Range support and can play/seek the clip.
        response.headers["Content-Disposition"] = "inline"
        response.headers["Cache-Control"] = "private, max-age=300"
    else:
        response.headers["Content-Disposition"] = disposition
    return response


# A preparation job is shared by every request for the same file. The browser
# starts one when a link is pasted and again when a format is tapped; both reuse
# the same background work, and ad close only has to stream the cached result.
DOWNLOAD_JOBS = {}
DOWNLOAD_JOB_LOCK = threading.Lock()
PREPARE_WAIT_SECONDS = 120


def parse_download_request(data):
    """Validate one download request. Returns (params, None) or (None, response)."""
    if not isinstance(data, dict):
        return None, error_response("Invalid request.")
    try:
        url = validate_link(data.get("url", ""))
    except ValueError as exc:
        return None, error_response(str(exc))

    mode = parse_mode(data.get("mode"))
    tiktok = is_tiktok_link(url)
    if mode == "mp3" and not FFMPEG_DIR:
        return None, error_response(
            "FFmpeg is not available for MP3 conversion. Configure the server and try again."
        )

    # A plain browser download GET cannot carry the resolved CDN hint in its
    # query string. Reuse the metadata lookup already cached for this link so
    # the download never has to resolve the same video a second time.
    info_entry = INFO_CACHE.get(url + "#story" if mode == "story" else url)
    info_payload = {}
    if info_entry and time.time() - info_entry[0] < INFO_CACHE_TTL:
        candidate = info_entry[1]
        if isinstance(candidate, dict):
            info_payload = candidate

    quality = str(data.get("quality") or DEFAULT_QUALITY)
    if re.fullmatch(r"[0-9]{1,4}", quality):
        height = int(quality)
        if height < 144 or height > 4320:
            return None, error_response("Invalid video quality.")
    elif quality in VIDEO_FORMATS:  # backwards compatibility
        height = int(quality)
    else:
        return None, error_response("Invalid video quality.")

    story_id = data.get("storyId")
    if story_id not in (None, ""):
        try:
            story_id = validate_story_id(story_id)
        except ValueError as exc:
            return None, error_response(str(exc))

    media = data.get("media") if isinstance(data.get("media"), dict) else None
    if not media and isinstance(info_payload.get("media"), dict):
        media = info_payload["media"]
    variant = data.get("variant") or info_payload.get("variant")

    # An exact-height stream was offered. Without this, `best[height<=1280]`
    # happily saves the larger-bitrate 1024p file when the visitor chose 1280p.
    offered_heights = {
        str(item.get("height"))
        for item in (info_payload.get("formats") or [])
        if isinstance(item, dict) and item.get("height")
    }
    if isinstance(media, dict):
        offered_heights.update((media.get("by_height") or {}).keys())
    exact = str(height) in offered_heights
    video_format = video_selector(height, tiktok, exact=exact)

    params = {
        "url": url,
        "mode": mode,
        "tiktok": tiktok,
        "height": height,
        "quality": quality,
        "video_format": video_format,
        "exact": exact,
        "story_id": story_id,
        "variant": variant,
        "media": media,
        "cache_key": file_cache_key(url, mode, quality, story_id),
    }
    return params, None


def build_prepared_file(params, stale=None):
    """Resolve and save the file into the shared cache. Raises on failure."""
    if stale and stale():
        raise StalePreparation()
    key = params["cache_key"]
    workdir = tempfile.mkdtemp(prefix="tiktok-video-downloader-video-")
    try:
        url = params["url"]
        mode = params["mode"]
        tiktok = params["tiktok"]
        height = params["height"]
        video_format = params["video_format"]
        story_id = params["story_id"]
        hint = params["media"] or {}
        variant = params["variant"]

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
            def ydl_attempt(target_dir, fast=False, limit=None, retry=True):
                variants = ydl_variants(
                    os.path.join(target_dir, "%(id)s.%(ext)s"),
                    mode,
                    preferred=variant,
                    tiktok=tiktok,
                    limit=limit,
                )
                # TikTok blocks hosts at random. Race a shortlist when /info
                # already told us which route works; otherwise keep the full
                # wave so one blocked host cannot hold up the whole download.
                return race_ydl_download(
                    url, target_dir, variants, video_format, mode,
                    timeout=12 if fast else 100,
                    retry=retry and not fast,
                )

            def mirror_attempt(target_dir):
                if not tiktok or cffi_requests is None:
                    return None, None, None
                mirror_key = "audio_alternates" if mode == "mp3" else "video_alternates"
                candidates = []
                if mode != "mp3":
                    # Honour the chosen quality when the resolver supplied
                    # per-resolution mirrors; the generic stream is only a
                    # fallback after those exact-height URLs.
                    candidates += list((hint.get("by_height") or {}).get(str(height)) or [])
                candidates += [hint.get("audio" if mode == "mp3" else "video")]
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
                if not mirrors and variant not in STRATEGIES:
                    # /info already found a yt-dlp route, so TikWM is known to
                    # be down — asking it again would only add dead time.
                    resolved = tikwm_data(url)
                    if resolved:
                        mirrors = tikwm_media_candidates(resolved, mode)
                        source = resolved
                if not mirrors:
                    return None, None, None
                if stale and stale():
                    return None, None, StalePreparation()
                try:
                    content, _content_type = fetch_media_raced(mirrors)
                    extension = ".mp3" if mode == "mp3" else ".mp4"
                    candidate = os.path.join(target_dir, "media" + extension)
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
                    app.logger.error(
                        "media download failed [%s]: %s", type(exc).__name__, str(exc)[:300]
                    )
                    return None, None, exc

            has_mirror = any(
                isinstance(hint.get(key), str) and hint[key]
                for key in ("video", "audio")
            ) or any(
                isinstance(hint.get(key), list) and hint[key]
                for key in ("video_alternates", "audio_alternates", "by_height")
            )

            if mode == "video" and has_mirror:
                # /info already resolved the real CDN URLs. Use them directly.
                # Racing six yt-dlp routes here as well hammered TikTok from the
                # same IP and caused 403s plus TikWM rate limits; the plain CDN
                # copy is both faster and far more reliable. yt-dlp stays as a
                # quiet fallback only when every mirror is genuinely dead.
                mirror_dir = os.path.join(workdir, "mirror")
                os.makedirs(mirror_dir, exist_ok=True)
                path, meta, mirror_failure = mirror_attempt(mirror_dir)
                if not path:
                    ydl_dir = os.path.join(workdir, "ydl")
                    os.makedirs(ydl_dir, exist_ok=True)
                    # The CDN mirror failed, but /info still knows the route that
                    # resolved this video. Use only that route plus two fallbacks;
                    # a six-way retry storm here is what made the file appear
                    # many seconds after the ad closed.
                    result, failure = ydl_attempt(
                        ydl_dir,
                        limit=3 if variant in STRATEGIES else None,
                        retry=False,
                    )
                    if result:
                        path, meta = result
                    else:
                        raise (failure or mirror_failure or RuntimeError("Download failed."))
                if stale and stale():
                    raise StalePreparation()
            elif mode == "video":
                # No CDN hint yet. yt-dlp must resolve the link itself; mirrors
                # stay as a fallback if every app route is blocked.
                result, failure = ydl_attempt(workdir)
                if result:
                    path, meta = result
                else:
                    app.logger.error(
                        "yt-dlp race failed [%s]: %s",
                        type(failure).__name__, str(failure)[:300],
                    )
                    path, meta, mirror_failure = mirror_attempt(workdir)
                    if not path:
                        raise (mirror_failure or failure or RuntimeError("Download failed."))
            else:
                path, meta, mirror_failure = mirror_attempt(workdir)
                if not path:
                    result, failure = ydl_attempt(workdir)
                    if not result:
                        raise (failure or mirror_failure or RuntimeError("Download failed."))
                    path, meta = result

        if stale and stale():
            raise StalePreparation()
        if mode != "story":
            path = (
                ensure_real_mp3(path, workdir) if mode == "mp3"
                else ensure_compatible_video(path, workdir)
            )
        if stale and stale():
            raise StalePreparation()
        store_file(key, path, meta, os.path.getsize(path))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    hit = cached_file(key)
    if not hit:
        raise RuntimeError("The prepared file is no longer available.")
    return hit


def _forget_job(key, job):
    with DOWNLOAD_JOB_LOCK:
        if DOWNLOAD_JOBS.get(key) is job:
            DOWNLOAD_JOBS.pop(key, None)


def _prepare_score(params):
    """Prefer jobs that already know the real CDN mirrors and exact quality."""
    media = params.get("media") or {}
    score = 0
    if any(isinstance(media.get(key), str) and media[key] for key in ("video", "audio")):
        score += 2
    if any(isinstance(media.get(key), (list, dict)) and media[key] for key in (
        "video_alternates", "audio_alternates", "by_height",
    )):
        score += 1
    if params.get("variant"):
        score += 1
    if params.get("exact"):
        score += 1
    return score


def start_prepare(params):
    """Start, reuse, or upgrade background preparation.

    The first paste can arrive before /info has resolved. That job must not
    monopolise the file for two minutes while a better-hinted request waits.
    A later request with CDN mirrors replaces it; the old worker is marked
    stale and never writes its result into the cache.
    """
    key = params["cache_key"]
    score = _prepare_score(params)
    with DOWNLOAD_JOB_LOCK:
        if cached_file(key):
            return True
        job = DOWNLOAD_JOBS.get(key)
        if job and not job["event"].is_set():
            if score <= job["score"]:
                return False
            job["stale"] = True
        job = {
            "event": threading.Event(),
            "error": None,
            "stale": False,
            "score": score,
        }
        DOWNLOAD_JOBS[key] = job

    def work():
        try:
            build_prepared_file(params, stale=lambda: job["stale"])
        except StalePreparation:
            pass
        except Exception as exc:
            if not job["stale"]:
                job["error"] = exc
                app.logger.error(
                    "background prepare failed [%s]: %s", type(exc).__name__, str(exc)[:300]
                )
        finally:
            job["event"].set()
            # Keep the failure briefly so a download GET can show a safe error.
            timer = threading.Timer(30, _forget_job, args=(key, job))
            timer.daemon = True
            timer.start()

    threading.Thread(target=work, daemon=True).start()
    return False


def wait_for_prepare(key):
    with DOWNLOAD_JOB_LOCK:
        job = DOWNLOAD_JOBS.get(key)
    if not job:
        return None
    job["event"].wait(PREPARE_WAIT_SECONDS)
    return job["error"]


@app.post("/api/video/prepare")
def video_prepare():
    params, error = parse_download_request(json_body() or {})
    if error:
        return error
    key = params["cache_key"]
    if cached_file(key):
        return jsonify({"ready": True, "token": key})
    return jsonify({"ready": not start_prepare(params), "token": key})


@app.get("/api/video/download")
@app.post("/api/video/download")
def video_download():
    # GET lets ad close trigger a plain browser download: the browser streams the
    # file straight to disk with zero JavaScript waiting and no in-memory Blob.
    data = dict(request.args) if request.method == "GET" else (json_body() or {})
    params, error = parse_download_request(data)
    if error:
        return error
    key = params["cache_key"]
    hit = cached_file(key)
    if not hit:
        failure = wait_for_prepare(key)
        if failure:
            return error_response(with_hint("Download failed.", error_category(failure)))
        hit = cached_file(key)
    if hit:
        return make_file_response(*hit)
    try:
        return make_file_response(*build_prepared_file(params))
    except StoryError as exc:
        return error_response(str(exc))
    except Exception as exc:
        app.logger.error("video download failed [%s]: %s", type(exc).__name__, str(exc)[:500])
        return error_response(with_hint("Download failed.", error_category(exc)))


def _prepared_file_response(data, inline):
    params, error = parse_download_request(data)
    if error:
        return error
    key = params["cache_key"]
    hit = cached_file(key)
    if not hit:
        failure = wait_for_prepare(key)
        if failure:
            return error_response(with_hint("Preview failed.", error_category(failure)))
        hit = cached_file(key)
    if hit:
        return make_file_response(*hit, inline=inline)
    try:
        return make_file_response(*build_prepared_file(params), inline=inline)
    except StoryError as exc:
        return error_response(str(exc))
    except Exception as exc:
        app.logger.error("video preview failed [%s]: %s", type(exc).__name__, str(exc)[:500])
        return error_response(with_hint("Preview failed.", error_category(exc)))


@app.get("/api/video/preview")
@app.post("/api/video/preview")
def video_preview():
    # The popup plays this same-origin endpoint instead of TikTok's signed CDN
    # URL, which is hotlink-protected and would be blocked by the browser.
    data = dict(request.args) if request.method == "GET" else (json_body() or {})
    return _prepared_file_response(data, inline=True)


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
    # Hosts like Render/Railway/Fly set PORT; there we serve with a real WSGI
    # server and never expose the debugger.
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
        # Keep local preview predictable: one process, no reloader child, no
        # debugger console, and a clear "ready" line so it is obvious when the
        # helper has finished importing yt-dlp/ffmpeg.
        host = os.environ.get("HOST", "127.0.0.1")
        print(f"Preview API ready at http://{host}:{port}", flush=True)
        app.run(host=host, port=port, debug=False, use_reloader=False)
