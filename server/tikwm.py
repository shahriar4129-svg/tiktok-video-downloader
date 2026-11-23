"""TikTok metadata through the public TikWM API.

yt-dlp is blocked from cloud/datacenter IP ranges (403 / status code 10231),
which is exactly where Vercel functions run. TikWM resolves the same video
from its own network and returns the real CDN media URLs, so this is the
primary path for TikTok links. yt-dlp stays as a fallback.
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from vercel_core import cffi_requests, log_failure, validate_media_url

# TikWM has two public hostnames. They do not fail together, so querying both
# at once gives a free second chance without waiting on a slow/dead endpoint.
API_HOSTS = (
    "https://www.tikwm.com/api/",
    "https://api.tikwm.com/api/",
    "https://tikwm.com/api/",
)
# A slow/dead resolver must never hold the whole page hostage. Both hosts are
# raced at this short timeout and the caller also races yt-dlp, so the visitor
# gets a result from whichever service answers first.
REQUEST_TIMEOUT = 3
CACHE_TTL = 600  # CDN URLs carry signed tokens; refresh every 10 minutes.
CACHE_MAX = 50
_CACHE = {}


def _fetch_once(endpoint, url, timeout=REQUEST_TIMEOUT):
    if cffi_requests is None:
        log_failure("tikwm unavailable", RuntimeError("curl-cffi is not installed"))
        return None
    try:
        session = cffi_requests.Session(impersonate="chrome")
        response = session.get(
            endpoint,
            params={"url": url, "hd": "1"},
            headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"},
            timeout=timeout,
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


def tikwm_data(url, timeout=REQUEST_TIMEOUT):
    """Return TikWM data from the fastest host, plus a short cache.

    Every caller is also racing a different resolver, so this function must
    fail fast: two 8 s timeouts plus a serial retry used to add 16+ seconds to
    a single paste before yt-dlp was even tried. Both hosts now share one short
    deadline and there is no second slow round — a failure simply lets the
    other resolver win.
    """
    hit = _CACHE.get(url)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]

    data = None
    if cffi_requests is not None:
        executor = ThreadPoolExecutor(max_workers=len(API_HOSTS))
        try:
            futures = [executor.submit(_fetch_once, host, url, timeout) for host in API_HOSTS]
            for future in as_completed(futures, timeout=timeout + 1):
                result = future.result()
                if result:
                    data = result
                    break
            for future in futures:
                future.cancel()
        except TimeoutError:
            # The executor deadline is only a safety net; each request already
            # has its own timeout. A stale edge must not delay the fallback.
            pass
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    if data:
        if len(_CACHE) >= CACHE_MAX:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[url] = (time.time(), data)
    return data


def tikwm_media(data, mode="video"):
    """Pick a validated media URL from a TikWM response."""
    candidates = tikwm_media_candidates(data, mode)
    return candidates[0] if candidates else None


def tikwm_media_candidates(data, mode="video"):
    """Ordered, de-duplicated CDN mirrors for the same video.

    TikWM returns a clean HD stream plus plain/watermarked alternates. They can
    live on different CDN edges; one edge may hang while another is healthy.
    The downloader races these mirrors so a single slow host cannot decide how
    long the visitor waits for the file.
    """
    if not isinstance(data, dict):
        return []
    if mode == "mp3":
        raw = (data.get("music"), data.get("play"), data.get("wmplay"))
    else:
        # TikWM's HD mirror is often HEVC, which forces a full H.264 re-encode
        # (a 2-minute video took over a minute). Its standard `play` mirror is
        # usually already H.264 and clean — try it first so every video saves
        # at the same speed without a long transcode. HD stays as fallback.
        raw = (data.get("play"), data.get("hdplay"), data.get("wmplay"))
    result = []
    for candidate in raw:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        try:
            url = validate_media_url(candidate)
        except (ValueError, TypeError):
            continue
        if url not in result:
            result.append(url)
    return result
