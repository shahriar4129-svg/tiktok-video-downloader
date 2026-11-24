"""Vercel function -> POST /api/video/info (TikTok only)"""

import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# Vercel keeps shared code outside /api; import it from server/.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))


from tikwm import tikwm_data, tikwm_media, tikwm_media_candidates  # noqa: E402

from vercel_core import (  # noqa: E402
    cached_info,
    cache_info,
    client_ip,
    error_category,
    fetch_stories,
    install_security_headers,
    log_failure,
    media_from_meta,
    parse_mode,
    quality_formats,
    rate_allowed,
    StoryError,
    STRATEGIES,
    validate_tiktok_url,
    with_hint,
    ydl_options,
)

app = FastAPI()
install_security_headers(app)

RATE_MESSAGE = "Too many requests. Please try again in a few minutes."


def tikwm_result(tikwm):
    """Shape the TikWM response exactly like the yt-dlp metadata response."""
    author = tikwm.get("author") or {}
    size = tikwm.get("hd_size") or tikwm.get("size") or 0
    try:
        height = int(tikwm.get("height") or tikwm.get("video_height") or 0)
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
    video_urls = tikwm_media_candidates(tikwm, "video")
    return {
        "title": tikwm.get("title") or "video",
        "duration": tikwm.get("duration"),
        "thumbnail": tikwm.get("cover") or tikwm.get("origin_cover"),
        "uploader": f"@{author.get('unique_id')}" if author.get("unique_id") else None,
        "platform": "TikTok",
        "formats": formats,
        "variant": "tikwm",
        # Resolved CDN URLs are handed to /download so it never resolves twice.
        "media": {
            "video": tikwm_media(tikwm, "video"),
            "audio": tikwm_media(tikwm, "mp3"),
            "video_alternates": video_urls,
            "audio_alternates": tikwm_media_candidates(tikwm, "mp3"),
            "by_height": {str(height): video_urls} if height > 0 else {},
            "title": tikwm.get("title") or "tiktok",
            "id": tikwm.get("id") or "video",
            "uploader": author.get("unique_id") or author.get("nickname"),
            "duration": tikwm.get("duration"),
            "size": int(size),
        },
    }


def ydl_result(url, options, variant):
    """Metadata from one yt-dlp strategy, including the path that worked."""
    import yt_dlp  # lazy — the TikWM path never needs it

    with yt_dlp.YoutubeDL(options) as ydl:
        meta = ydl.extract_info(url, download=False)
        try:
            formats = quality_formats(ydl, meta, tiktok=True)
        except Exception:
            formats = []
    return {
        "title": meta.get("title") or "video",
        "duration": meta.get("duration"),
        "thumbnail": meta.get("thumbnail"),
        "uploader": meta.get("uploader"),
        "platform": meta.get("extractor_key") or meta.get("extractor") or "TikTok",
        "formats": formats,
        "variant": variant,
        "media": media_from_meta(meta),
    }


def resolve_fast(url):
    """Race resolvers instead of waiting for a dead service before trying the next."""
    def wave(names, timeout):
        tasks = []
        if "tikwm" in names:
            def try_tikwm():
                data = tikwm_data(url)
                return tikwm_result(data) if data else None
            tasks.append(try_tikwm)
        for name in names:
            if name == "tikwm":
                continue
            options = ydl_options(variant=name, fast=True)
            tasks.append(lambda o=options, v=name: ydl_result(url, o, v))
        executor = ThreadPoolExecutor(max_workers=len(tasks))
        try:
            futures = [executor.submit(fn) for fn in tasks]
            try:
                for future in as_completed(futures, timeout=timeout):
                    try:
                        result = future.result()
                    except Exception as exc:
                        log_failure(f"resolver {type(exc).__name__}", exc)
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

    # One wave keeps a cold paste fast; losers are cancelled on first success.
    # If every route is blocked, retry once quietly — the download path already
    # does this and often succeeds on the second attempt.
    result = wave(("tikwm",) + STRATEGIES, 10)
    if result:
        return result
    return wave(("tikwm",) + STRATEGIES, 6)


@app.post("/")
@app.post("/video/info")
@app.post("/api/video/info")
def video_info(payload: dict, request: Request):
    if not rate_allowed(client_ip(request), "info"):
        return JSONResponse({"error": RATE_MESSAGE}, status_code=429)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "Invalid request."}, status_code=400)
    try:
        url = validate_tiktok_url(payload.get("url", ""))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    mode = parse_mode(payload.get("mode"))
    cache_key = url + "#story" if mode == "story" else url
    hit = cached_info(cache_key)
    if hit is not None:
        return JSONResponse(hit)

    if mode == "story":
        try:
            stories, username = fetch_stories(url)
            result = {
                "kind": "story",
                "title": f"@{username} stories",
                "uploader": f"@{username}",
                "platform": "TikTok",
                "stories": stories,
                "formats": [],
            }
            cache_info(cache_key, result)
            return JSONResponse(result)
        except StoryError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            log_failure("video/info story", exc)
            return JSONResponse(
                {"error": with_hint("Could not load stories.", error_category(exc))},
                status_code=400,
            )

    # TikWM and yt-dlp start together; whichever answers first is used. A dead
    # or rate-limited resolver can no longer add 16+ seconds to a page load.
    result = resolve_fast(url)
    if result:
        cache_info(url, result)
        return JSONResponse(result)

    return JSONResponse(
        {"error": with_hint("Could not read video information.", "generic")},
        status_code=400,
    )
