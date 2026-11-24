"""
Shared logic for the Vercel Python functions (TikTok-only edition).

Kept outside /api so Vercel does not deploy it as its own function. The
file-based functions in api/ import these helpers.

Vercel constraints respected here:
  - read-only filesystem, writable /tmp only (500 MB)
  - 300 s max function duration on Hobby
  - response bodies must be streamed (4.5 MB static payload cap)
"""

import hashlib
import ipaddress
import logging
import os
import re
import shutil
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

try:
    from curl_cffi import requests as cffi_requests
except Exception:  # optional — story lookup degrades to a clear error message
    cffi_requests = None

try:
    # Shared DNS/TLS caches let the parallel range workers skip repeated
    # handshakes without sharing a curl handle across threads (unsafe).
    from curl_cffi import CurlShare
except Exception:
    CurlShare = None


def impersonate_target(name):
    """Resolve yt-dlp's impersonate target only when the fallback is used.

    Importing yt-dlp costs about a second on a cold start, and the normal TikWM
    path never needs it. Keeping it lazy shortens the wait before the file is
    ready and the ad can start.
    """
    try:
        # Newer yt-dlp expects an ImpersonateTarget object, not a plain string.
        from yt_dlp.networking.impersonate import ImpersonateTarget

        return ImpersonateTarget.from_str(name)
    except Exception:
        return name  # older yt-dlp accepts the string directly

try:
    import imageio_ffmpeg

    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = None

# TikTok only — this keeps the function from being used as a generic fetcher.
TIKTOK_HOSTS = ("tiktok.com", "vtiktok.com")
# Story thumbnails/media come from TikTok's own CDNs. Nothing else may be
# fetched by the download endpoint — this is the SSRF boundary.
MEDIA_HOSTS = (
    "tiktok.com", "tiktokcdn.com", "tiktokcdn-us.com", "tiktokcdn-eu.com",
    "tiktokcdn-apac.com", "tiktokv.com", "byteimg.com", "byteimg.net",
    "musical.ly",
)
SAFE_EXTENSIONS = (".mp4", ".mp3", ".webm", ".ogg", ".m4a", ".mkv", ".jpg", ".jpeg", ".png")
ENV_COOKIE_FILE = os.path.join(tempfile.gettempdir(), "tiktok-video-downloader-cookies.txt")

# AdSense injects its own inline scripts/iframes, so this CSP is permissive
# there while still blocking base-tag hijacking, plugins and framing.
CSP = (
    "default-src 'self'; base-uri 'self'; object-src 'none'; "
    "frame-ancestors 'none'; form-action 'self'; "
    "script-src 'self' 'unsafe-inline' https://pagead2.googlesyndication.com "
    "https://www.googletagservices.com https://ep2.adtrafficquality.google "
    "https://googleads.g.doubleclick.net https://www.googleadservices.com "
    "https://adservice.google.com https://www.google.com https://www.gstatic.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "img-src 'self' data: blob: https:; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "connect-src 'self' blob: data: https:; "
    "media-src 'self' blob: https:; "
    "frame-src https: data:"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=(), usb=(), interest-inventory=()",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add the same hardening headers to every Vercel API response."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers[name] = value
        return response


def install_security_headers(app):
    app.add_middleware(SecurityHeadersMiddleware)

    @app.exception_handler(Exception)
    async def unhandled_exception(request, exc):
        # Never leak tracebacks to the browser. A structured JSON error lets
        # the UI show a useful message instead of its generic fallback.
        log_failure("unhandled API exception", exc)
        return JSONResponse(
            {"error": "This link could not be processed. Please try again."},
            status_code=500,
        )


# Generous sliding-window limits — abuse/DoS dampener, not an auth control.
# On serverless each instance has its own bucket; that is fine, this only
# blunts casual hammering of the extraction endpoints.
RATE_LIMITS = {"info": 60, "download": 20}
RATE_WINDOW = 300
RATE_BUCKETS = {}
RATE_LOCK = threading.Lock()


def rate_allowed(client, bucket):
    key = (str(client or "unknown"), bucket)
    now = time.time()
    with RATE_LOCK:
        hits = [stamp for stamp in RATE_BUCKETS.get(key, []) if now - stamp < RATE_WINDOW]
        if len(hits) >= RATE_LIMITS.get(bucket, 60):
            RATE_BUCKETS[key] = hits
            return False
        hits.append(now)
        RATE_BUCKETS[key] = hits
        if len(RATE_BUCKETS) > 5000:
            for stale in [name for name, stamps in RATE_BUCKETS.items() if not stamps]:
                RATE_BUCKETS.pop(stale, None)
        return True


def client_ip(request):
    """Best-effort client IP for rate limiting; never trust X-Forwarded-For."""
    try:
        return request.client.host if request.client else "unknown"
    except AttributeError:
        return getattr(request, "remote_addr", "unknown")

# yt-dlp TikTok app-API config. TikTok behaves differently across apps/hosts,
# so when one path fails, another is tried.
AWEME_APP_INFO = "/aweme/28.2.3/2022823030/1128"
API_HOST_21 = "api21-normal-c-useast2a.tiktokv.com"
API_HOST_22 = "api22-normal-c-useast2a.tiktokv.com"

# /tmp is 500 MB and merging needs roughly 2x the final file size.
MAX_BYTES = 250 * 1024 * 1024

# A metadata lookup is cheap; a short-lived cache avoids repeated extraction.
INFO_CACHE = {}
INFO_CACHE_TTL = 300
INFO_CACHE_MAX = 25


LOG = logging.getLogger("tiktok-video-downloader")


def log_failure(context, exc):
    """Keep full error detail in private server logs, never in the response.

    yt-dlp messages can contain internal paths or signed URLs. Returning them
    to the browser is information disclosure, so endpoints send only a generic
    message plus a user-facing hint.
    """
    LOG.warning("%s [%s]: %s", context, type(exc).__name__, str(exc)[:500])


def prepare_cookie_file():
    raw = os.environ.get("VIDEO_COOKIES", "").strip()
    if not raw:
        return
    # Cookies are session secrets — create owner-readable-only (0600).
    fd = os.open(ENV_COOKIE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(raw.replace("\\n", "\n"))
    try:
        os.chmod(ENV_COOKIE_FILE, 0o600)
    except OSError:
        pass  # Some hosts ignore chmod; the create mode already tried.


prepare_cookie_file()


def validate_tiktok_url(raw):
    """Return a safe TikTok URL or raise ValueError."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Please paste a TikTok link.")

    value = raw.strip()
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value

    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise ValueError("Only http/https links are allowed.")
    if parsed.username or parsed.password:
        raise ValueError("Links with credentials are not allowed.")
    if any(ord(char) > 127 for char in host):
        raise ValueError("Only plain ASCII hostnames are allowed.")
    if parsed.port not in (None, 80, 443):
        raise ValueError("Only standard web ports are allowed.")

    if not any(host == domain or host.endswith("." + domain) for domain in TIKTOK_HOSTS):
        raise ValueError("Only TikTok links are supported. YouTube/Facebook links are not supported.")

    try:
        addresses = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ValueError("That TikTok link could not be resolved.") from None

    for entry in addresses:
        ip = ipaddress.ip_address(entry[4][0])
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            raise ValueError("Local/private addresses are blocked.")

    return value


def validate_media_url(raw):
    """Return a TikTok CDN media URL that is safe for the server to fetch.

    Story media URLs come from TikTok's API, but they must still be checked
    before curl-cffi follows them — otherwise a poisoned cache/API response
    could turn the download endpoint into an internal-network fetcher.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("This story is no longer available.")
    value = raw.strip()
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        raise ValueError("Only HTTPS media links are allowed.")
    if parsed.username or parsed.password or any(ord(char) > 127 for char in host):
        raise ValueError("This story media link is not valid.")
    if parsed.port not in (None, 443):
        raise ValueError("Only standard HTTPS ports are allowed.")
    if not any(host == domain or host.endswith("." + domain) for domain in MEDIA_HOSTS):
        raise ValueError("This story media link is not from TikTok.")

    try:
        addresses = socket.getaddrinfo(host, 443)
    except socket.gaierror:
        raise ValueError("This story media link could not be resolved.") from None
    for entry in addresses:
        ip = ipaddress.ip_address(entry[4][0])
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            raise ValueError("Local/private addresses are blocked.")
    return value


def validate_story_id(value):
    """Story IDs are opaque tokens from TikTok — keep them boring and bounded."""
    story_id = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", story_id):
        raise ValueError("Invalid story link.")
    return story_id


def safe_extension(path, default=".mp4"):
    """Only allow known media extensions in cache names and Content-Type."""
    extension = os.path.splitext(str(path or ""))[1].lower()
    return extension if extension in SAFE_EXTENSIONS else default


def parse_mode(value):
    """Normalize the requested output mode. Unknown values fall back to video."""
    mode = str(value or "video").strip().lower()
    return mode if mode in ("video", "mp3", "story") else "video"


def ydl_options(outtmpl=None, variant="app22", mode="video", fast=False):
    """Build yt-dlp options. `variant` selects one TikTok access strategy.

    `fast=True` is used for metadata-only lookups: short timeouts and no retries
    make link detection feel instant. Downloads keep the longer, safer values.
    """
    options = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 8 if fast else 20,
        "retries": 0 if fast else 1,
        "noplaylist": True,
        "concurrent_fragment_downloads": 8,
        # curl-cffi gives TikTok browser impersonation when available.
        "impersonate": impersonate_target("chrome"),
    }
    # TikTok blocks datacenter IP ranges. A proxy in a permitted region is the
    # only reliable fix; configure TIKTOK_PROXY (http://user:pass@host:port)
    # when available. Without it, extraction depends on TikTok's mood.
    proxy = os.environ.get("TIKTOK_PROXY", "").strip()
    if proxy:
        options["proxy"] = proxy
    if mode == "mp3":
        # Audio-only extraction + ffmpeg transcode. ffmpeg is bundled locally,
        # so nothing has to be installed on the host running this function.
        options["format"] = "bestaudio/best"
        options["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    if variant == "aweme1128":
        # This was the most reliable path in local testing (~50% per attempt).
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
        options["impersonate"] = impersonate_target("safari")
    # variant "web" keeps the plain Chrome webpage path as the last resort.
    if FFMPEG:
        options["ffmpeg_location"] = FFMPEG
    cookie_path = ENV_COOKIE_FILE if os.environ.get("VIDEO_COOKIES", "").strip() else None
    if cookie_path and os.path.exists(cookie_path):
        options["cookiefile"] = cookie_path
    if outtmpl:
        options["outtmpl"] = outtmpl
    return options


# aweme1128/app21 are the app-API paths that still work from most networks;
# try them before the web and legacy app paths (which datacenter IPs often get
# 403 on). Order matters here — each failed attempt costs real time.
STRATEGIES = ("aweme1128", "app21", "web", "app22", "app16", "safari")


def ydl_variants(outtmpl=None, mode="video", fast=False, preferred=None, limit=None):
    """Ordered fallback strategies — first success wins.

    `preferred` is the path that worked during the metadata lookup. Trying it
    first for the download avoids burning time on paths TikTok just blocked.
    `limit` keeps the download race small when the winning route is already
    known; extra routes then compete for the same bandwidth and delay the save.
    """
    order = list(STRATEGIES)
    if preferred in order:
        order.remove(preferred)
        order.insert(0, preferred)
    if fast:
        limit = 3 if limit is None else min(limit, 3)
    if limit:
        order = order[:limit]
    return [ydl_options(outtmpl, name, mode, fast=fast) for name in order]


def locate_download(workdir, ydl, meta, mode="video"):
    """Find the file yt-dlp actually wrote (postprocessors may rename it).

    FFmpegExtractAudio turns `.m4a`/`.mp4` into `.mp3`, so `prepare_filename`
    alone can point at a file that no longer exists.
    """
    expected = ".mp3" if mode == "mp3" else None
    path = None
    try:
        path = ydl.prepare_filename(meta)
    except Exception:
        pass

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


def media_from_meta(meta):
    """Direct CDN mirrors from a yt-dlp lookup, so /download never re-resolves.

    TikTok's app API gives each format its own signed CDN URL. Passing those
    to the download endpoint turns it into a plain file fetch; the second
    extraction was slow and caused most intermittent 403 errors. H.264 mirrors
    come first because they save without a lengthy HEVC re-encode.
    """
    formats = [item for item in (meta.get("formats") or []) if isinstance(item, dict)]
    videos = []
    for item in formats:
        url = item.get("url")
        height = item.get("height")
        if not url or not height or item.get("vcodec") in (None, "none"):
            continue
        try:
            size = int(height)
        except (TypeError, ValueError):
            continue
        codec = str(item.get("vcodec") or "").lower()
        h264 = codec.startswith(("h264", "avc"))
        videos.append(((0 if h264 else 1, -size), size, url))
    videos.sort(key=lambda entry: entry[0])

    alternates = []
    by_height = {}
    for _rank, height, url in videos:
        key = str(height)
        by_height.setdefault(key, [])
        if url not in by_height[key]:
            by_height[key].append(url)
        if url not in alternates:
            alternates.append(url)

    audio = next(
        (
            item.get("url") for item in formats
            if item.get("url") and item.get("vcodec") in (None, "none")
            and item.get("acodec") not in (None, "none")
        ),
        None,
    )
    return {
        "video": alternates[0] if alternates else None,
        "video_alternates": alternates,
        "audio": audio,
        "audio_alternates": [audio] if audio else [],
        "by_height": by_height,
        "title": meta.get("title") or "tiktok",
        "id": meta.get("id") or "video",
        "uploader": meta.get("uploader") or meta.get("uploader_id") or meta.get("channel"),
        "duration": meta.get("duration"),
    }


def video_selector(height, tiktok=False, exact=False):
    """Keep TikTok's muxed A/V together instead of pairing two streams.

    TikTok exposes progressive MP4s plus a separate music track. Merging a
    progressive picture with that second track produces the delayed dialogue
    visitors notice in saved clips, so TikTok downloads pick one whole stream.
    H.264 is preferred over HEVC even at a lower height: the HEVC copy needs a
    full re-encode, which adds seconds after the ad and can shift audio timing.

    When the visitor picked a resolution the video actually offers, try that
    exact height first. A plain `best[height<=1280]` saves the higher-bitrate
    1024p copy instead of the 1280p one the visitor chose.
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


def quality_formats(ydl, meta, tiktok=False):
    """Resolutions the video actually offers, with estimated final sizes."""
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


def error_category(exc):
    """Classify a failure into a safe, non-sensitive category for the UI."""
    text = str(exc).lower()
    if any(k in text for k in (
        "403", "forbidden", "blocked", "unable to download webpage", "sign in to confirm",
        "verify you are human", "captcha", "slardar", "challenge", "bot",
        # TikTok API status codes for region/availability blocks.
        "10231", "10204", "10222", "100004", "not available",
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
    """Add a short, visitor-friendly next step. Full detail stays in server logs."""
    if category == "blocked":
        hint = " Tip: the video service is busy right now. Please try again in a moment."
    elif category == "login":
        hint = " Tip: This clip is private, age-restricted, or login-only. Please use a public TikTok link."
    elif category == "unavailable":
        hint = " Tip: This clip is unavailable or may have been removed. Try another link."
    elif category == "rate":
        hint = " Tip: Too many requests right now. Wait a moment and try again."
    else:
        hint = " Tip: please try again with a fresh public TikTok link."
    return f"{message}{hint}"


def cached_info(url):
    hit = INFO_CACHE.get(url)
    if hit and time.time() - hit[0] < INFO_CACHE_TTL:
        return hit[1]
    return None


def cache_info(url, payload):
    if len(INFO_CACHE) >= INFO_CACHE_MAX:
        INFO_CACHE.pop(next(iter(INFO_CACHE)))
    INFO_CACHE[url] = (time.time(), payload)


# A prepared file is much more expensive than metadata. Keeping it for a few
# minutes makes a repeat click instant — no TikTok re-extraction or slow CDN
# transfer inside the ad's three-second beat.
FILE_CACHE = {}
FILE_CACHE_DIR = os.path.join(tempfile.gettempdir(), "tiktok-video-downloader-file-cache")
FILE_CACHE_TTL = 300
FILE_CACHE_MAX = 8
# A prepared file is streamed straight from this cache at ad close. Keeping the
# 100 MB+ case cached is the only way a large video feels as instant as a small
# one — the browser never has to assemble a giant Blob in JavaScript.
FILE_CACHE_MAX_BYTES = MAX_BYTES
FILE_CACHE_BUDGET = 400 * 1024 * 1024
FILE_CACHE_BYTES = 0
try:
    os.makedirs(FILE_CACHE_DIR, exist_ok=True)
except OSError:
    pass


def file_cache_key(url, mode, quality, story_id=""):
    # v2: files cached before the H.264 compatibility fix must not be reused.
    raw = f"v2|{url}|{mode}|{quality}|{story_id or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def cached_file(key):
    hit = FILE_CACHE.get(key)
    if not hit or time.time() - hit[0] >= FILE_CACHE_TTL:
        return None
    path = hit[1]
    if not os.path.isfile(path):
        return None
    return path, hit[2]


def _drop_cached_file(key):
    global FILE_CACHE_BYTES
    hit = FILE_CACHE.pop(key, None)
    if not hit:
        return
    FILE_CACHE_BYTES = max(0, FILE_CACHE_BYTES - (hit[3] if len(hit) > 3 else 0))
    try:
        os.remove(hit[1])
    except OSError:
        pass


def store_file(key, path, meta, size):
    """Cache a prepared file so ad close is a plain stream, never a re-fetch."""
    global FILE_CACHE_BYTES
    if size > FILE_CACHE_MAX_BYTES or size <= 0:
        return
    extension = safe_extension(path)
    target = os.path.join(FILE_CACHE_DIR, key + extension)
    try:
        shutil.copy2(path, target)
    except OSError:
        return
    if key in FILE_CACHE:
        _drop_cached_file(key)
    while (
        len(FILE_CACHE) >= FILE_CACHE_MAX
        or FILE_CACHE_BYTES + size > FILE_CACHE_BUDGET
    ) and FILE_CACHE:
        _drop_cached_file(next(iter(FILE_CACHE)))
    FILE_CACHE[key] = (time.time(), target, dict(meta), size)
    FILE_CACHE_BYTES += size


def parse_quality(value, tiktok=False, exact=False):
    """Return (height, yt-dlp selector). Strict ASCII digits only.

    str.isdigit() also accepts unicode digits like '²', which then crash int()
    with an unhandled 500. A regex keeps the input boring and safe to embed.
    """
    text = str(value or "720")
    if not re.fullmatch(r"[0-9]{1,4}", text):
        raise ValueError("Invalid video quality.")
    height = int(text)
    if height < 144 or height > 4320:
        raise ValueError("Invalid video quality.")
    if tiktok:
        return height, video_selector(height, True, exact=exact)
    # Other sites need separate video/audio streams merged by ffmpeg. Prefer
    # H.264 + AAC so the saved MP4 plays in Windows Media Player and QuickTime.
    return height, (
        f"bestvideo[vcodec^=avc1][height<={height}]+bestaudio[acodec^=mp4a]/"
        f"bestvideo[vcodec^=h264][height<={height}]+bestaudio/"
        f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
    )


def safe_filename(title, extension=".mp4"):
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", title or "video").strip()
    return (cleaned[:100] or "video") + extension


SITE_NAME = "TikTok Video Downloader"


def account_name(meta):
    """Clean TikTok handle used as the first part of every saved filename."""
    source = meta if isinstance(meta, dict) else {}
    for key in ("uploader", "uploader_id", "channel", "channel_id", "author", "username"):
        value = source.get(key)
        if isinstance(value, dict):
            value = value.get("unique_id") or value.get("id") or value.get("name")
        if not value:
            continue
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip().lstrip("@"))
        cleaned = cleaned.strip("._-")[:40]
        if cleaned:
            return cleaned
    return "tiktok-video"


def download_filename(meta, extension=".mp4"):
    """Name saved files as: account_uniqueNumber_website.ext.

    The video title is intentionally excluded. The account makes the file
    recognisable, the TikTok ID guarantees uniqueness even for reposts, and the
    site name brands the result. Keeping this in one helper gives the local
    server and the Vercel functions byte-for-byte identical filenames.
    """
    source = meta if isinstance(meta, dict) else {}
    account = account_name(source)
    video_id = re.sub(r"[^A-Za-z0-9_-]+", "", str(source.get("id") or ""))[:24]
    if not video_id:
        video_id = re.sub(r"[^A-Za-z0-9_-]+", "", str(source.get("video_id") or ""))[:24]
    parts = [account]
    if video_id:
        parts.append(video_id)
    return "_".join(parts) + "_" + SITE_NAME + extension


# ---------------------------------------------------------------------------
# TikTok Stories (best effort)
#
# yt-dlp has no Story extractor, so the public web endpoints are used directly
# with browser impersonation. TikTok guards these endpoints with a JS WAF and
# request signing; when that blocks us we return a clear, safe error instead of
# leaking internals. Verified story media URLs are cached briefly so the info
# and download calls do not have to repeat the lookup.
# ---------------------------------------------------------------------------
STORY_CACHE = {}
STORY_CACHE_TTL = 600
STORY_CACHE_MAX = 50


class StoryError(RuntimeError):
    """A user-facing Story failure with a safe, specific message."""


def story_username(url):
    """Pull the @handle out of a TikTok profile/story URL."""
    parsed = urlparse(url)
    for part in [p for p in parsed.path.split("/") if p]:
        if part.startswith("@"):
            return part[1:]
    return ""


def _first_url(value):
    if isinstance(value, str) and value.startswith("http"):
        return value
    if isinstance(value, list):
        for item in value:
            found = _first_url(item)
            if found:
                return found
    if isinstance(value, dict):
        for key in ("urlList", "downloadAddr", "playAddr", "PlayAddr", "src", "url"):
            if key in value:
                found = _first_url(value[key])
                if found:
                    return found
    return ""


def fetch_stories(url):
    """Return (stories, owner) for a profile link, or raise RuntimeError.

    Media URLs stay server-side in STORY_CACHE; only thumbnails/ids go to the
    browser so /api/video/download remains TikTok-scoped and cannot be abused
    as a generic URL fetcher.
    """
    if cffi_requests is None:
        raise StoryError("curl-cffi is required on the server for Story support.")

    username = story_username(url)
    if not username:
        raise StoryError(
            "For Story mode, paste a TikTok profile link (@username) or a direct story media link."
        )

    session = cffi_requests.Session(impersonate="chrome")
    headers = {
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://www.tiktok.com/@{username}",
    }
    profile = session.get(headers["Referer"], headers=headers, timeout=20)
    page = profile.text or ""
    if "slardar" in page.lower() or len(page) < 4000:
        raise StoryError(
            "The TikTok story API is blocked on this network. Paste the story's direct video media link instead — that works."
        )

    match = re.search(r'"secUid"\s*:\s*"([^"]+)"', page)
    if not match:
        raise StoryError("No story data was found on this profile. Try a direct story media link.")
    sec_uid = match.group(1)

    listing = session.get(
        "https://www.tiktok.com/api/story/list/",
        params={
            "aid": "1988",
            "secUid": sec_uid,
            "count": "32",
            "type": "0",
            "fp": "",
            "cursor": "",
            "language": "en",
        },
        headers=headers,
        timeout=20,
    )
    try:
        data = listing.json()
    except Exception:
        raise StoryError("Could not read the TikTok story list. Try a direct story media link.") from None

    raw = data.get("stories") or data.get("storyList") or []
    stories = []
    now = time.time()
    for item in raw:
        if not isinstance(item, dict):
            continue
        story_id = str(item.get("id") or item.get("storyId") or "")
        video = item.get("video") or {}
        images = item.get("imageList") or []
        media = _first_url(video.get("downloadAddr")) or _first_url(video.get("bitrateInfo"))
        kind = "video"
        if not media and images:
            media = _first_url(images[0])
            kind = "image"
        if not media or not story_id:
            continue

        try:
            validate_media_url(media)  # never cache an unsafe fetch target
        except ValueError:
            continue
        STORY_CACHE[story_id] = {"media": media, "owner": username, "exp": now + STORY_CACHE_TTL}
        cover = _first_url(video.get("cover")) or _first_url(video.get("originCover"))
        if not cover and images:
            cover = _first_url(images[0])
        stories.append({
            "id": story_id,
            "type": kind,
            "thumbnail": cover,
            "createTime": item.get("createTime"),
        })

    if len(STORY_CACHE) > STORY_CACHE_MAX:
        for key in list(STORY_CACHE)[: len(STORY_CACHE) - STORY_CACHE_MAX]:
            STORY_CACHE.pop(key, None)
    return stories, username


def story_media(story_id, url):
    """Resolve a cached story media URL, refreshing the cache if expired."""
    entry = STORY_CACHE.get(str(story_id))
    if entry and entry.get("exp", 0) > time.time():
        return entry["media"]
    fetch_stories(url)  # repopulates the cache
    entry = STORY_CACHE.get(str(story_id))
    if not entry:
        raise StoryError("This story is no longer available (stories expire after 24 hours).")
    return entry["media"]


def fetch_media(url, session, max_redirects=3, timeout=12, max_bytes=MAX_BYTES):
    """Fetch TikTok CDN media with SSRF checks on every hop.

    Redirects are followed manually so a CDN cannot bounce the request to an
    internal address. Each target is re-validated and the body is size-capped
    before it is ever written to disk.
    """
    current = validate_media_url(url)
    for _ in range(max_redirects + 1):
        response = session.get(
            current,
            headers={"Referer": "https://www.tiktok.com/"},
            timeout=timeout,
            allow_redirects=False,
        )
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            if not location:
                raise StoryError("This story media redirect is incomplete.")
            current = validate_media_url(urljoin(current, location))
            continue
        if response.status_code >= 400:
            raise StoryError("Story media download failed.")
        content = response.content
        if len(content) > max_bytes:
            raise StoryError("This story file is too large to save.")
        return content, (response.headers.get("content-type") or "").lower()
    raise StoryError("This story media redirected too many times.")


def fetch_media_fast(url, timeout=5, chunks=None, max_bytes=MAX_BYTES):
    """Download one CDN file in parallel byte ranges.

    TikTok's CDN supports Range requests. A single connection can be slow or
    stall near the end; short parallel ranges finish in roughly the same time
    for a small file and a 100 MB file. The worker count scales with the
    reported size so large files still get a fair shot at the 3s processing
    window instead of trickling over one connection.
    """
    current = validate_media_url(url)
    headers = {"Referer": "https://www.tiktok.com/"}
    share = CurlShare() if (CurlShare and cffi_requests is not None) else None

    def new_session():
        if share is None:
            return cffi_requests.Session(impersonate="chrome")
        return cffi_requests.Session(impersonate="chrome", curl_share=share)

    redirected = False
    for _ in range(4):
        response = new_session().get(
            current,
            headers={**headers, "Range": "bytes=0-0"},
            timeout=timeout,
            allow_redirects=False,
        )
        if response.status_code in (301, 302, 303, 307, 308):
            redirected = True
            location = response.headers.get("location")
            if not location:
                raise StoryError("This video's media redirect is incomplete.")
            current = validate_media_url(urljoin(current, location))
            continue
        redirected = False
        break

    if redirected:
        raise StoryError("This video's media redirected too many times.")
    if response.status_code >= 400:
        raise StoryError("This video's media download failed.")

    total = None
    content_range = response.headers.get("content-range") or ""
    if response.status_code == 206 and "/" in content_range:
        try:
            total = int(content_range.rsplit("/", 1)[-1])
        except (TypeError, ValueError):
            total = None
    if not total or total > max_bytes:
        # Range not supported — reuse the probe body when it is the whole file.
        if response.status_code == 200 and len(response.content) <= max_bytes:
            return response.content, (response.headers.get("content-type") or "").lower()
        raise StoryError("This video's file is too large to save.")

    # More ranges for bigger files. The probe already told us the exact size,
    # so this costs nothing and keeps large downloads from using one stream.
    if chunks is None:
        chunks = 32 if total > 80 * 1024 * 1024 else 16 if total > 25 * 1024 * 1024 else 8
    chunks = max(1, min(int(chunks), 32))

    def part(index):
        start = (index * total) // chunks
        end = ((index + 1) * total) // chunks - 1
        if end < start:
            return b""
        result = new_session().get(
            current,
            headers={**headers, "Range": f"bytes={start}-{end}"},
            timeout=timeout,
            allow_redirects=False,
        )
        if result.status_code not in (200, 206):
            raise StoryError("This video's media download failed.")
        return result.content

    executor = ThreadPoolExecutor(max_workers=chunks)
    try:
        results = list(executor.map(part, range(chunks)))
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    content = b"".join(results)
    if len(content) != total:
        raise StoryError("This video's media download was incomplete.")
    return content, (response.headers.get("content-type") or "").lower()


def fetch_media_raced(urls, timeout=5, max_bytes=MAX_BYTES):
    """Fetch the first usable CDN mirror for the same video.

    Mirrors are tried in order, not all at once — downloading three full files
    in parallel only splits the bandwidth. A dead mirror fails fast (503), and
    a healthy one is pulled in parallel ranges so the wait stays short and
    even across long and short videos.
    """
    candidates = []
    for candidate in urls or []:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        try:
            candidate = validate_media_url(candidate)
        except (ValueError, TypeError):
            continue
        if candidate not in candidates:
            candidates.append(candidate)
    if not candidates:
        raise StoryError("No usable media URL was returned for this video.")

    if cffi_requests is None:
        raise StoryError("curl-cffi is required to save this video.")

    first_error = None
    for candidate in candidates:
        try:
            return fetch_media_fast(candidate, timeout=timeout, max_bytes=max_bytes)
        except Exception as exc:
            if first_error is None:
                first_error = exc

    # A few CDN edges reject parallel byte ranges. One ordinary request is a
    # slower but much more reliable last resort than failing the whole download.
    try:
        session = cffi_requests.Session(impersonate="chrome")
        return fetch_media(candidates[0], session, timeout=15, max_bytes=max_bytes)
    except Exception as exc:
        log_failure("media plain fallback", exc)
    raise (first_error or StoryError("This video's media is unavailable."))
