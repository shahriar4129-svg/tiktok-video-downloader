"""Vercel function -> POST /api/video/download (TikTok only).

The video is streamed back with StreamingResponse. Vercel's 4.5 MB body cap
does not apply to streamed responses, so a normal TikTok MP4 goes through.
Only /tmp is writable (500 MB), hence the 250 MB ceiling in vercel_core.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))

from tikwm import tikwm_data, tikwm_media_candidates  # noqa: E402

from vercel_core import (  # noqa: E402
    FFMPEG,
    MAX_BYTES,
    account_name,
    cached_file,
    cffi_requests,
    client_ip,
    download_filename,
    error_category,
    fetch_media,
    fetch_media_raced,
    file_cache_key,
    install_security_headers,
    locate_download,
    log_failure,
    parse_mode,
    parse_quality,
    rate_allowed,
    safe_extension,
    SITE_NAME,
    StoryError,
    story_media,
    STRATEGIES,
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


def make_real_mp3(path, workdir):
    """Ensure the saved .mp3 really is MP3, not a mislabeled AAC stream.

    TikWM's music URL is usually MP3, but for some posts it returns a 64 kb/s
    AAC file with an audio/mp4 content type. Encoding that short track to MP3
    is cheap; already-MP3 sources are copied untouched.
    """
    if not FFMPEG:
        return path
    if "audio: mp3" in probe_streams(path):
        return path
    output = os.path.join(workdir, "audio.mp3")
    try:
        command = [
            FFMPEG, "-y", "-loglevel", "error", "-i", path, "-vn",
            "-codec:a", "libmp3lame", "-b:a", "128k", output,
        ]
        subprocess.run(command, check=True, timeout=120, capture_output=True)
        return output
    except Exception as exc:
        log_failure("mp3 normalize", exc)
        return path


def make_compatible_mp4(path, workdir):
    """Re-encode only when the video track cannot play in normal players.

    A remux alone costs time proportional to the file size and gains nothing
    for a TikTok mirror that is already H.264/yuv420p in an MP4 container, so
    compatible downloads are returned untouched. HE-AAC audio stays as-is: it
    plays in current browsers and phones, and re-encoding it would add seconds
    to every long clip without improving compatibility.
    """
    if not FFMPEG:
        return path
    info = probe_streams(path)
    if video_stream_ok(info):
        return path
    output = os.path.join(workdir, "compatible.mp4")
    command = [
        FFMPEG, "-y", "-loglevel", "error", "-fflags", "+genpts", "-i", path,
        "-c:v", "libx264", "-profile:v", "high", "-preset", "veryfast",
        "-crf", "23", "-pix_fmt", "yuv420p", "-tag:v", "avc1",
        "-c:a", "copy", "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart", output,
    ]
    subprocess.run(command, check=True, timeout=240, capture_output=True)
    os.remove(path)
    return output


def race_ydl_download(url, workdir, variants, video_format, mode):
    """Try every TikTok route at once and use the first complete file.

    TikTok 403s most app hosts at random. Serial attempts made visitors wait
    through several failures; racing them keeps one working route instant.
    Each strategy writes into its own scratch folder outside `workdir`; the
    winner is copied into the request folder while a helper thread waits for
    losing writers and removes their scratch safely.
    """
    def attempt(race_root, index, options):
        localdir = os.path.join(race_root, f"try-{index}")
        os.makedirs(localdir, exist_ok=True)
        opts = dict(options)
        opts["outtmpl"] = os.path.join(localdir, "%(id)s.%(ext)s")
        if mode != "mp3":
            opts["format"] = video_format
        opts["merge_output_format"] = "mp4"
        try:
            import yt_dlp  # lazy — direct CDN downloads never need it

            with yt_dlp.YoutubeDL(opts) as ydl:
                meta = ydl.extract_info(url, download=True)
                path = locate_download(localdir, ydl, meta, mode)
            if path and os.path.getsize(path) > 0:
                extension = os.path.splitext(path)[1] or (
                    ".mp3" if mode == "mp3" else ".mp4"
                )
                stable = os.path.join(workdir, f"winner-{index}{extension}")
                shutil.copy2(path, stable)
                return stable, meta
        except Exception as exc:
            return exc
        return RuntimeError("Downloaded file was not found.")

    def race_root_cleanup(futures, race_root):
        wait(futures)
        shutil.rmtree(race_root, ignore_errors=True)

    def run():
        race_root = tempfile.mkdtemp(prefix="tiktok-race-")
        executor = ThreadPoolExecutor(max_workers=len(variants))
        futures = [
            executor.submit(attempt, race_root, index, opts)
            for index, opts in enumerate(variants)
        ]
        result = None
        last_error = None
        try:
            for future in as_completed(futures, timeout=100):
                value = future.result()
                if isinstance(value, tuple):
                    result = value
                    break
                last_error = value
                log_failure(f"download strategy {type(value).__name__}", value)
        except TimeoutError:
            last_error = last_error or RuntimeError("Download timed out.")
        finally:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)
            threading.Thread(
                target=race_root_cleanup, args=(futures, race_root), daemon=True
            ).start()
        return result, last_error

    result, last_error = run()
    if result:
        return result, last_error
    # A full wave can be 403'd during a short block. One quiet retry recovers
    # those cases without adding any delay to the normal first-try-success path.
    time.sleep(0.8)
    return run()


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
    filename = download_filename(meta, extension)
    # ASCII fallback uses the same account + ID + site shape as filename*.
    video_id = re.sub(r"[^A-Za-z0-9_-]+", "", str(meta.get("id") or ""))[:24]
    fallback = "_".join(
        part for part in (account_name(meta), video_id, SITE_NAME) if part
    )[:110]
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
        height, video_format = parse_quality(payload.get("quality"), tiktok=True)
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
            def ydl_attempt():
                variants = ydl_variants(
                    os.path.join(workdir, "%(id)s.%(ext)s"),
                    mode,
                    preferred=payload.get("variant"),
                )
                return race_ydl_download(url, workdir, variants, video_format, mode)

            def mirror_attempt():
                media_hint = payload.get("media")
                resolved = media_hint if isinstance(media_hint, dict) else {}
                mirror_key = "audio_alternates" if mode == "mp3" else "video_alternates"
                mirrors = list(resolved.get(mirror_key) or [])
                if mode != "mp3":
                    by_height = (resolved.get("by_height") or {}).get(str(height)) or []
                    for candidate in reversed(by_height):
                        if candidate not in mirrors:
                            mirrors.insert(0, candidate)
                raw_media = resolved.get("audio") if mode == "mp3" else resolved.get("video")
                if isinstance(raw_media, str) and raw_media.strip():
                    try:
                        primary_url = validate_media_url(raw_media)
                        mirrors = [primary_url] + [item for item in mirrors if item != primary_url]
                    except (ValueError, TypeError) as exc:
                        log_failure("media hint", exc)

                data = resolved or None
                if not mirrors and payload.get("variant") not in STRATEGIES:
                    # /info already found a yt-dlp route, so TikWM is known down.
                    data = tikwm_data(url)
                    if data:
                        mirrors = tikwm_media_candidates(data, mode)
                if not mirrors or cffi_requests is None:
                    return None, None, None

                def save_media(content, source):
                    extension = ".mp3" if mode == "mp3" else ".mp4"
                    candidate = os.path.join(workdir, "media" + extension)
                    with open(candidate, "wb") as handle:
                        handle.write(content)
                    meta = {
                        "title": (source or {}).get("title") or "tiktok",
                        "id": (source or {}).get("id") or "video",
                        "uploader": (source or {}).get("uploader"),
                        "duration": (source or {}).get("duration"),
                    }
                    return candidate, meta

                failure = None
                try:
                    content, _content_type = fetch_media_raced(mirrors)
                    path, meta = save_media(content, data)
                    return path, meta, None
                except Exception as exc:
                    log_failure("tikwm download", exc)
                    failure = exc
                    # A signed CDN URL can expire between the info lookup and
                    # the click. Resolve once more, then race the fresh mirrors.
                    fresh = tikwm_data(url)
                    retry_urls = tikwm_media_candidates(fresh, mode) if fresh else []
                    if retry_urls:
                        try:
                            content, _content_type = fetch_media_raced(retry_urls)
                            path, meta = save_media(content, fresh)
                            return path, meta, None
                        except Exception as exc:
                            log_failure("tikwm download retry", exc)
                            failure = exc
                return None, None, failure

            if mode == "video":
                # A CDN copy is often HEVC and needs a slow full re-encode. The
                # yt-dlp race picks TikTok's muxed H.264 stream instead, which
                # is faster and already in sync. Mirrors stay as fallback.
                result, failure = ydl_attempt()
                if result:
                    path, meta = result
                else:
                    last_error = failure
                    path, meta, failure = mirror_attempt()
                    if not path:
                        last_error = failure or last_error
                        shutil.rmtree(workdir, ignore_errors=True)
                        return JSONResponse(
                            {"error": with_hint("Download failed.", error_category(last_error))},
                            status_code=400,
                        )
            else:
                path, meta, failure = mirror_attempt()
                if not path:
                    result, last_error = ydl_attempt()
                    if not result:
                        last_error = last_error or failure
                        shutil.rmtree(workdir, ignore_errors=True)
                        return JSONResponse(
                            {"error": with_hint("Download failed.", error_category(last_error))},
                            status_code=400,
                        )
                    path, meta = result

        if mode != "story":
            if mode == "video":
                try:
                    path = make_compatible_mp4(path, workdir)
                except Exception as exc:
                    # A playable original is better than no file at all.
                    log_failure("h264 transcode", exc)
            else:
                path = make_real_mp3(path, workdir)

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
