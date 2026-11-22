"""Vercel function -> POST /api/video/info (TikTok only)"""

import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# Vercel keeps shared code outside /api; import it from server/.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

import yt_dlp  # noqa: E402

from tikwm import tikwm_data  # noqa: E402

from vercel_core import (  # noqa: E402
    cached_info,
    cache_info,
    client_ip,
    error_category,
    fetch_stories,
    install_security_headers,
    log_failure,
    parse_mode,
    quality_formats,
    rate_allowed,
    StoryError,
    STRATEGIES,
    validate_tiktok_url,
    with_hint,
    ydl_variants,
)

app = FastAPI()
install_security_headers(app)

RATE_MESSAGE = "Too many requests. Please try again in a few minutes."


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

    # Cloud IPs are blocked by TikTok, so resolve metadata through TikWM
    # first. yt-dlp remains the fallback if that service is unavailable.
    tikwm = tikwm_data(url)
    if tikwm:
        author = tikwm.get("author") or {}
        size = tikwm.get("hd_size") or tikwm.get("size") or 0
        result = {
            "title": tikwm.get("title") or "video",
            "duration": tikwm.get("duration"),
            "thumbnail": tikwm.get("cover") or tikwm.get("origin_cover"),
            "uploader": f"@{author.get('unique_id')}" if author.get("unique_id") else None,
            "platform": "TikTok",
            "formats": [{"height": 720, "ext": "mp4", "filesize": int(size)}],
            "variant": "tikwm",
        }
        cache_info(url, result)
        return JSONResponse(result)

    last_error = None
    for attempt, options in enumerate(ydl_variants(fast=True), start=1):
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                meta = ydl.extract_info(url, download=False)
                formats = quality_formats(ydl, meta)

            result = {
                "title": meta.get("title") or "video",
                "duration": meta.get("duration"),
                "thumbnail": meta.get("thumbnail"),
                "uploader": meta.get("uploader"),
                "platform": "TikTok",
                "formats": formats,
                # The path that worked now — the download reuses it first.
                "variant": STRATEGIES[(attempt - 1) % len(STRATEGIES)],
            }
            cache_info(url, result)
            return JSONResponse(result)
        except Exception as exc:
            last_error = exc
            log_failure(f"video/info attempt {attempt}", exc)

    return JSONResponse(
        {"error": with_hint("Could not read video information.", error_category(last_error))},
        status_code=400,
    )
