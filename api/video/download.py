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

from tikwm import tikwm_data, tikwm_media  # noqa: E402

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
    validate_media_url,
    validate_story_id,
    validate_tiktok_url,
    with_hint,
    ydl_variants,
)

app = FastAPI()
install_security_headers(app)

CHUNK_SIZE = 64 * 1024
RATE_MESSAGE = "Too many requests. Please try again in a few minutes."


def probe_streams(path):
    """Return ffmpeg's lowercased stream description for a media file."""
    if not FFMPEG:
        return ""
    try:
        result = subprocess.run(
            [FFMPEG, "-hide_banner", "-i", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
        )
    except Exception:
        return ""
    return (result.stderr or "").lower()


def video_stream_ok(info):
    """True when the video track is already broadly playable H.264/yuv420p."""
    lines = [line for line in info.splitlines() if "video:" in line]
    if not lines:
        return False
    video = lines[0]
    if "h264" not in video and "avc" not in video:
        return False
    # 10-bit and 4:4:4 H.264 are not playable by Windows Media Foundation.
    if "yuv420p10" in video or "yuv444" in video:
        return False
    return "yuv420p" in video or "yuvj420p" in video


def audio_stream_ok(info):
    """True when every audio track is AAC-LC (HE-AAC is not universal)."""
    for line in (line for line in info.splitlines() if "audio:" in line):
        if "aac" not in line or "he-aac" in line:
            return False
    return True


def needs_transcode(path):
    """Inspect the real file, not yt-dlp metadata.

    yt-dlp can report `h264` for a file that Windows still rejects (10-bit,
    4:4:4, an unusual profile, or a mismatched container tag). Reading the
    stream info with ffmpeg is the only reliable check before saving.
    """
    if not FFMPEG:
        return False
    info = probe_streams(path)
    if not info:
        return True
    if not video_stream_ok(info):
        return True
    return not audio_stream_ok(info)


def make_compatible_mp4(path, workdir):
    """Normalise to H.264/AAC, copying the video track whenever it is fine.

    TikTok's HD stream is usually already H.264/yuv420p with only the audio in
    HE-AAC. Re-encoding the whole file took ~14s and blew past the ad beat;
    stream-copying video and re-encoding only audio does the same job in well
    under a second, with no loss of video quality.
    """
    if not FFMPEG:
        return path
    output = os.path.join(workdir, "compatible.mp4")
    info = probe_streams(path)
    if video_stream_ok(info):
        video_args = ["-c:v", "copy", "-tag:v", "avc1"]
    else:
        video_args = [
            "-c:v", "libx264", "-profile:v", "high", "-preset", "veryfast",
            "-crf", "23", "-pix_fmt", "yuv420p", "-tag:v", "avc1",
        ]
    command = [
        FFMPEG, "-y", "-loglevel", "error", "-i", path,
        *video_args,
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
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
            # Prefer the CDN URL resolved by /api/video/info. That keeps one
            # paste -> download flow down to a single TikWM lookup; a second
            # independent call from this function was the flaky part.
            media_hint = payload.get("media")
            resolved = media_hint if isinstance(media_hint, dict) else {}
            raw_media = resolved.get("audio") if mode == "mp3" else resolved.get("video")
            media_url = None
            if isinstance(raw_media, str) and raw_media.strip():
                try:
                    media_url = validate_media_url(raw_media)
                except (ValueError, TypeError) as exc:
                    log_failure("media hint", exc)

            data = resolved or None
            if not media_url:
                data = tikwm_data(url)
                if data:
                    media_url = tikwm_media(data, mode)

            extension = ".mp3" if mode == "mp3" else ".mp4"
            path = os.path.join(workdir, "media" + extension)
            tikwm_ok = False
            if media_url and cffi_requests is not None:
                try:
                    session = cffi_requests.Session(impersonate="chrome")
                    content, content_type = fetch_media(media_url, session)
                    with open(path, "wb") as handle:
                        handle.write(content)
                    meta = {
                        "title": (data or {}).get("title") or "tiktok",
                        "id": (data or {}).get("id") or "video",
                        "duration": (data or {}).get("duration"),
                    }
                    if mode == "video" and needs_transcode(path):
                        try:
                            path = make_compatible_mp4(path, workdir)
                        except Exception as exc:
                            log_failure("h264 transcode", exc)
                    tikwm_ok = True
                except Exception as exc:
                    log_failure("tikwm download", exc)
                    # A signed CDN URL can expire between the info lookup and
                    # the click. Resolve once more before using the fallback.
                    if media_hint:
                        fresh = tikwm_data(url)
                        retry_url = tikwm_media(fresh, mode) if fresh else None
                        if retry_url:
                            try:
                                session = cffi_requests.Session(impersonate="chrome")
                                content, content_type = fetch_media(retry_url, session)
                                with open(path, "wb") as handle:
                                    handle.write(content)
                                meta = {
                                    "title": fresh.get("title") or "tiktok",
                                    "id": fresh.get("id") or "video",
                                    "duration": fresh.get("duration"),
                                }
                                if mode == "video" and needs_transcode(path):
                                    try:
                                        path = make_compatible_mp4(path, workdir)
                                    except Exception as exc:
                                        log_failure("h264 transcode retry", exc)
                                tikwm_ok = True
                            except Exception as exc:
                                log_failure("tikwm download retry", exc)

            if not tikwm_ok:
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
                        import yt_dlp  # lazy — the TikWM path never needs it

                        with yt_dlp.YoutubeDL(options) as ydl:
                            meta = ydl.extract_info(url, download=True)
                            path = locate_download(workdir, ydl, meta, mode)
                        if path:
                            if mode == "video" and needs_transcode(path):
                                try:
                                    path = make_compatible_mp4(path, workdir)
                                except Exception as exc:
                                    # Keep the original file if re-encoding is
                                    # not possible — a file beats no file.
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
