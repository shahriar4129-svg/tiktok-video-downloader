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


def ydl_variants(outtmpl=None, mode="video", fast=False, preferred=None):
    """Ordered fallback strategies — first success wins.

    `preferred` is the path that worked during the metadata lookup. Trying it
    first for the download avoids burning time on paths TikTok just blocked.
    """
    order = list(STRATEGIES)
    if preferred in order:
        order.remove(preferred)
        order.insert(0, preferred)
    # Fast metadata lookups retry only the two strongest paths. Downloads keep
    # the full second cycle for resilience when TikTok blocks intermittently.
    order = order + (["aweme1128", "app21"] if fast else list(STRATEGIES))
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


def quality_formats(ydl, meta):
    """Resolutions the video actually offers, with estimated final sizes."""
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
FILE_CACHE_MAX_BYTES = 50 * 1024 * 1024
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


def store_file(key, path, meta, size):
    if size > FILE_CACHE_MAX_BYTES or size <= 0:
        return
    extension = safe_extension(path)
    target = os.path.join(FILE_CACHE_DIR, key + extension)
    try:
        shutil.copy2(path, target)
    except OSError:
        return
    if len(FILE_CACHE) >= FILE_CACHE_MAX:
        oldest = next(iter(FILE_CACHE))
        old_hit = FILE_CACHE.pop(oldest, None)
        if old_hit and old_hit[1] != target:
            try:
                os.remove(old_hit[1])
            except OSError:
                pass
    FILE_CACHE[key] = (time.time(), target, dict(meta))


def parse_quality(value):
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
    # Prefer H.264 + AAC so the saved MP4 plays in Windows Media Player,
    # QuickTime and browsers without paid HEVC codec extensions. HEVC-only
    # sources are transcoded by the download endpoint when necessary.
    return height, (
        f"bestvideo[vcodec^=avc1][height<={height}]+bestaudio[acodec^=mp4a]/"
        f"bestvideo[vcodec^=h264][height<={height}]+bestaudio/"
        f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
    )


def safe_filename(title, extension=".mp4"):
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", title or "video").strip()
    return (cleaned[:100] or "video") + extension


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


def fetch_media_fast(url, timeout=5, chunks=4, max_bytes=MAX_BYTES):
    """Download one CDN file in parallel byte ranges.

    TikTok's CDN supports Range requests. A single connection can be slow or
    stall near the end; four short, parallel ranges finish in roughly the same
    time for a 3 MB and a 6 MB file. That is what keeps the file ready — and
    therefore the ad-close beat — consistent across videos of any length.
    """
    current = validate_media_url(url)
    headers = {"Referer": "https://www.tiktok.com/"}
    for _ in range(4):
        response = cffi_requests.Session(impersonate="chrome").get(
            current,
            headers={**headers, "Range": "bytes=0-0"},
            timeout=timeout,
            allow_redirects=False,
        )
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location")
            if not location:
                raise StoryError("This video's media redirect is incomplete.")
            current = validate_media_url(urljoin(current, location))
            continue
        break

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

    def part(index):
        start = (index * total) // chunks
        end = ((index + 1) * total) // chunks - 1
        if end < start:
            return b""
        session = cffi_requests.Session(impersonate="chrome")
        result = session.get(
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
    raise (first_error or StoryError("This video's media is unavailable."))
