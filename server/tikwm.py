"""TikTok metadata through the public TikWM API.

yt-dlp is blocked from cloud/datacenter IP ranges (403 / status code 10231),
which is exactly where Vercel functions run. TikWM resolves the same video
from its own network and returns the real CDN media URLs, so this is the
primary path for TikTok links. yt-dlp stays as a fallback.
"""

from vercel_core import cffi_requests, validate_media_url

API_URL = "https://www.tikwm.com/api/"


def tikwm_data(url):
    """Return the raw TikWM data object, or None when the service is unusable."""
    if cffi_requests is None:
        return None
    try:
        session = cffi_requests.Session(impersonate="chrome")
        response = session.get(
            API_URL,
            params={"url": url, "hd": "1"},
            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
            timeout=25,
        )
        payload = response.json()
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("code") != 0:
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def tikwm_media(data, mode="video"):
    """Pick a validated media URL from a TikWM response."""
    if mode == "mp3":
        candidates = (data.get("music"), data.get("play"))
    else:
        candidates = (data.get("hdplay"), data.get("play"))
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        try:
            return validate_media_url(candidate)
        except (ValueError, TypeError):
            continue
    return None
