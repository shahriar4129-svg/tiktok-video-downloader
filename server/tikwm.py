"""TikTok metadata through the public TikWM API.

yt-dlp is blocked from cloud/datacenter IP ranges (403 / status code 10231),
which is exactly where Vercel functions run. TikWM resolves the same video
from its own network and returns the real CDN media URLs, so this is the
primary path for TikTok links. yt-dlp stays as a fallback.
"""

import time

from vercel_core import cffi_requests, log_failure, validate_media_url

# TikWM has two public hostnames. They do not fail together, so alternating
# them gives a free second chance without waiting on the same endpoint.
API_HOSTS = ("https://www.tikwm.com/api/", "https://api.tikwm.com/api/")
CACHE_TTL = 600  # CDN URLs carry signed tokens; refresh every 10 minutes.
CACHE_MAX = 50
_CACHE = {}


def _fetch_once(endpoint, url):
    if cffi_requests is None:
        log_failure("tikwm unavailable", RuntimeError("curl-cffi is not installed"))
        return None
    try:
        session = cffi_requests.Session(impersonate="chrome")
        response = session.get(
            endpoint,
            params={"url": url, "hd": "1"},
            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
            timeout=12,
        )
        payload = response.json()
    except Exception as exc:
        log_failure("tikwm request", exc)
        return None
    if not isinstance(payload, dict):
        log_failure("tikwm response", RuntimeError("non-object JSON"))
        return None
    if payload.get("code") != 0:
        # TikWM returns its own code/message for rate limits and bad links.
        log_failure("tikwm API", RuntimeError(
            f"code={payload.get('code')} msg={payload.get('msg')}"
        ))
        return None
    data = payload.get("data")
    return data if isinstance(data, dict) else None


def tikwm_data(url):
    """Return TikWM data with retries, alternating hosts, and a short cache.

    A single paste can otherwise fire several upstream calls (prefetch, info,
    download warm-up). The cache keeps repeated lookups in the same function
    instance to one call, which also avoids TikWM's rate limit.
    """
    hit = _CACHE.get(url)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]

    data = None
    for attempt in range(3):
        endpoint = API_HOSTS[attempt % len(API_HOSTS)]
        data = _fetch_once(endpoint, url)
        if data:
            break
        if attempt < 2:
            time.sleep(0.5 * (attempt + 1))

    if data:
        if len(_CACHE) >= CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[url] = (time.time(), data)
    return data


def tikwm_media(data, mode="video"):
    """Pick a validated media URL from a TikWM response."""
    if not isinstance(data, dict):
        return None
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
