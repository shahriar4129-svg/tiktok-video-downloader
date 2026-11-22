"""
Tiny helper server for TikTok Video Downloader.

It does two things:
  1. Serves the built React app (dist/) so everything runs on one origin.
  2. Exposes /api/video/info and /api/video/download using yt-dlp, so links
     from YouTube / TikTok / Facebook etc. can actually be saved.

Run:  python server/app.py
Then: http://localhost:8000
"""

import glob
import io
import ipaddress
import json
import os
import re
import shutil
import socket
import tempfile
import time
import unicodedata
from urllib.parse import quote, urlparse

import yt_dlp
from flask import Flask, jsonify, request, send_file, send_from_directory

# Story helpers live in the shared core module (same directory as this file).
from vercel_core import (  # noqa: E402
    cached_file,
    cffi_requests,
    client_ip,
    download_filename,
    fetch_media,
    fetch_stories,
    file_cache_key,
    rate_allowed,
    SECURITY_HEADERS,
    safe_extension,
    StoryError,
    store_file,
    story_media,
    validate_story_id,
)

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

# Quality choices exposed by the UI. Video + audio are merged into MP4 with
# ffmpeg (bundled via imageio-ffmpeg). Higher quality = bigger file = slower.
VIDEO_FORMATS = {
    "480": "bestvideo[height<=480]+bestaudio/best[height<=480]/best",
    "720": "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
    "1080": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
}
DEFAULT_QUALITY = "720"
MAX_BYTES = 300 * 1024 * 1024

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


def ydl_variants(outtmpl=None, mode="video", fast=False, preferred=None):
    """Ordered fallback strategies — first success wins.

    `preferred` is the path that worked during the metadata lookup. Trying it
    first for the download avoids burning time on paths TikTok just blocked.
    """
    order = list(STRATEGIES)
    if preferred in order:
        order.remove(preferred)
        order.insert(0, preferred)
    if fast:
        order = order + ["app22", "app16"]
    else:
        order = order + list(STRATEGIES)
    return [ydl_options(outtmpl, name, mode, fast=fast) for name in order]


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


def quality_formats(ydl, meta):
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
        size = 0
        try:
            selector = ydl.build_format_selector(
                f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
            )
            picked = list(selector({"formats": all_formats}))
            if picked:
                size = picked[0].get("filesize") or picked[0].get("filesize_approx") or 0
        except Exception:
            size = 0

        if not size:
            # Fallback for extractors without a build_format_selector helper.
            video_sizes = [
                fmt.get("filesize") or fmt.get("filesize_approx") or 0
                for fmt in all_formats
                if fmt.get("height") == height and fmt.get("vcodec") not in (None, "none")
            ]
            audio_sizes = [
                fmt.get("filesize") or fmt.get("filesize_approx") or 0
                for fmt in all_formats
                if fmt.get("vcodec") in (None, "none") and fmt.get("acodec") not in (None, "none")
            ]
            size = (max(video_sizes) if video_sizes else 0) + (max(audio_sizes) if audio_sizes else 0)

        if size:
            result.append({"height": height, "ext": "mp4", "filesize": int(size)})
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

    last_error = None
    for attempt, options in enumerate(ydl_variants(fast=True), start=1):
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                meta = ydl.extract_info(url, download=False)
                formats = quality_formats(ydl, meta)

            payload = {
                "title": meta.get("title") or "video",
                "duration": meta.get("duration"),
                "thumbnail": meta.get("thumbnail"),
                "uploader": meta.get("uploader"),
                "platform": meta.get("extractor_key") or meta.get("extractor") or "video",
                "formats": formats,
                # The path that worked now — the download reuses it first.
                "variant": STRATEGIES[(attempt - 1) % len(STRATEGIES)],
            }
            if len(INFO_CACHE) >= INFO_CACHE_MAX:
                INFO_CACHE.pop(next(iter(INFO_CACHE)))
            INFO_CACHE[cache_key] = (time.time(), payload)
            persist_info_cache()
            return jsonify(payload)
        except Exception as exc:  # yt-dlp raises many different exception types
            last_error = exc
            # Full detail stays in the server log — never leak signed URLs/paths.
            app.logger.error(
                "video info attempt %d failed [%s]: %s",
                attempt, type(exc).__name__, str(exc)[:500],
            )

    return error_response(
        with_hint("Could not read video information.", error_category(last_error))
    )


def make_file_response(path, meta):
    """Share the response headers between a fresh download and a cache hit."""
    size = os.path.getsize(path)
    if size > MAX_BYTES:
        return error_response("The file is over 300 MB — it cannot be saved.")

    with open(path, "rb") as handle:
        payload = io.BytesIO(handle.read())

    extension = safe_extension(path)
    preferred = download_filename(meta, extension)

    # Browsers that ignore filename*= still need a readable ASCII name.
    raw_title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", str(meta.get("title") or "")).strip()
    ascii_title = unicodedata.normalize("NFKD", raw_title).encode("ascii", "ignore").decode()
    ascii_title = re.sub(r"[^A-Za-z0-9._ -]+", "_", ascii_title).strip(" ._")[:60]
    video_id = re.sub(r"[^A-Za-z0-9_-]+", "", str(meta.get("id") or ""))[:24]
    fallback = " - ".join(
        part for part in (ascii_title or "tiktok-video", video_id, "TikTok Video Downloader") if part
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
    if mode == "mp3" and not FFMPEG_DIR:
        return error_response("FFmpeg is not available for MP3 conversion. Configure the server and try again.")

    workdir = tempfile.mkdtemp(prefix="tiktok-video-downloader-video-")
    try:
        quality = str(data.get("quality") or DEFAULT_QUALITY)
        if re.fullmatch(r"[0-9]{1,4}", quality):
            height = int(quality)
            if height < 144 or height > 4320:
                return error_response("Invalid video quality.")
            video_format = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
        elif quality in VIDEO_FORMATS:  # backwards compatibility
            video_format = VIDEO_FORMATS[quality]
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
            variants = ydl_variants(
                os.path.join(workdir, "%(id)s.%(ext)s"),
                mode,
                preferred=data.get("variant"),
            )
            for attempt, options in enumerate(variants, start=1):
                for leftover in glob.glob(os.path.join(workdir, "*")):
                    if os.path.isfile(leftover):
                        os.remove(leftover)
                if mode != "mp3":
                    options["format"] = video_format
                options["merge_output_format"] = "mp4"
                try:
                    with yt_dlp.YoutubeDL(options) as ydl:
                        meta = ydl.extract_info(url, download=True)
                        path = locate_download(workdir, ydl, meta, mode)
                    if path:
                        break
                    last_error = RuntimeError("Downloaded file was not found.")
                except Exception as exc:
                    last_error = exc
                    app.logger.error(
                        "video download attempt %d failed [%s]: %s",
                        attempt, type(exc).__name__, str(exc)[:500],
                    )
            else:
                return error_response(
                    with_hint("Download failed.", error_category(last_error))
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
