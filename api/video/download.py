"""Vercel function -> POST /api/video/download (TikTok only).

The video is streamed back with StreamingResponse. Vercel's 4.5 MB body cap
does not apply to streamed responses, so a normal TikTok MP4 goes through.
Only /tmp is writable (500 MB), hence the 250 MB ceiling in vercel_core.
"""

import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

import yt_dlp  # noqa: E402

from vercel_core import (  # noqa: E402
    FFMPEG,
    MAX_BYTES,
    cached_file,
    cffi_requests,
    client_ip,
    error_category,
    fetch_media,
    file_cache_key,
    install_security_headers,
    locate_download,
    log_failure,
    parse_mode,
    parse_quality,
    rate_allowed,
    safe_extension,
    safe_filename,
    StoryError,
    story_media,
    store_file,
    validate_story_id,
    validate_tiktok_url,
    with_hint,
    ydl_variants,
)

app = FastAPI()
install_security_headers(app)

CHUNK_SIZE = 64 * 1024
RATE_MESSAGE = "Too many requests. Please try again in a few minutes."


def is_h264_video(meta):
    """True when the picked format already uses universally supported H.264."""
    downloads = meta.get("requested_downloads") or []
    sources = [entry.get("format") or {} for entry in downloads if isinstance(entry, dict)]
    if not sources and isinstance(meta.get("format"), dict):
        sources = [meta["format"]]
    codecs = [str(source.get("vcodec") or "") for source in sources]
    codecs = [codec for codec in codecs if codec and codec != "none"]
    if not codecs:
        return True
    return all(codec.startswith(("avc1", "h264")) for codec in codecs)


def make_compatible_mp4(path, workdir):
    """Re-encode HEVC/VP9 to H.264 + AAC so the file plays without extra codecs."""
    if not FFMPEG:
        return path
    output = os.path.join(workdir, "compatible.mp4")
    command = [
        FFMPEG, "-y", "-loglevel", "error", "-i", path,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", output,
    ]
    subprocess.run(command, check=True, timeout=240, capture_output=True)
    os.remove(path)
    return output


def iter_file(path):
    """Read in small chunks so the response is streamed, not buffered."""
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            yield chunk


def stream_file(path, meta, cleanup_dir=None):
    """Share response headers between a fresh download and a cache hit."""
    size = os.path.getsize(path)
    if size > MAX_BYTES:
        return JSONResponse(
            {"error": "The file is over 250 MB — it cannot be saved on Vercel."},
            status_code=400,
        )

    extension = safe_extension(path)
    filename = safe_filename(meta.get("title") or "tiktok", extension)
    raw_title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", meta.get("title") or "").strip()
    ascii_base = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_title).strip("._-")
    if not ascii_base:
        ascii_base = f"tiktok-{meta.get('id') or 'video'}"
    fallback = ascii_base[:80]
    if not fallback.lower().endswith(extension.lower()):
        fallback += extension
    disposition = (
        f"attachment; filename=\"{fallback}\"; "
        f"filename*=UTF-8''{quote(filename)}"
    )
    headers = {
        "Content-Disposition": disposition,
        "Content-Length": str(size),
    }
    if extension.lower() == ".mp3":
        media_type = "audio/mpeg"
    elif extension.lower() in (".jpg", ".jpeg", ".png"):
        media_type = "image/jpeg"
    else:
        media_type = "video/mp4"

    options = {}
    if cleanup_dir:
        options["background"] = BackgroundTask(shutil.rmtree, cleanup_dir, ignore_errors=True)
    return StreamingResponse(iter_file(path), media_type=media_type, headers=headers, **options)


@app.post("/")
@app.post("/video/download")
@app.post("/api/video/download")
def video_download(payload: dict, request: Request):
    if not rate_allowed(client_ip(request), "download"):
        return JSONResponse({"error": RATE_MESSAGE}, status_code=429)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "Invalid request."}, status_code=400)
    try:
        url = validate_tiktok_url(payload.get("url", ""))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    mode = parse_mode(payload.get("mode"))
    if mode == "mp3" and not FFMPEG:
        return JSONResponse(
            {"error": "FFmpeg is not available for MP3 conversion. Configure the server and try again."},
            status_code=400,
        )

    try:
        _, video_format = parse_quality(payload.get("quality"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    story_id = payload.get("storyId")
    if story_id not in (None, ""):
        try:
            story_id = validate_story_id(story_id)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    cache_key = file_cache_key(url, mode, payload.get("quality"), story_id)
    cached = cached_file(cache_key)
    if cached:
        return stream_file(*cached)

    workdir = tempfile.mkdtemp(prefix="tiktok-video-")
    last_error = None
    try:
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
                preferred=payload.get("variant"),
            )
            for attempt, options in enumerate(variants, start=1):
                # Remove partial files left by a failed strategy before retrying.
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
                        if mode == "video" and not is_h264_video(meta):
                            try:
                                path = make_compatible_mp4(path, workdir)
                            except Exception as exc:
                                # Keep the original file if re-encoding is not
                                # possible — a playable source beats no file.
                                log_failure("h264 transcode", exc)
                        break
                    last_error = RuntimeError("Downloaded file was not found.")
                except Exception as exc:
                    last_error = exc
                    log_failure(f"video/download attempt {attempt}", exc)
            else:
                shutil.rmtree(workdir, ignore_errors=True)
                return JSONResponse(
                    {"error": with_hint("Download failed.", error_category(last_error))},
                    status_code=400,
                )

        store_file(cache_key, path, meta, os.path.getsize(path))
        return stream_file(path, meta, workdir)
    except StoryError as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        log_failure("video/download", exc)
        return JSONResponse(
            {"error": with_hint("Download failed.", error_category(exc))},
            status_code=400,
        )
