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
import unicodedata
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


def needs_transcode(path):
    """Only re-encode when the *video* track cannot play everywhere.

    Re-encoding just the audio sounds cheap, but on a long video it still costs
    over ten seconds — the visitor sits in the ad well past the four-second
    beat while a short video closes on time. TikWM's H.264 mirror already has
    a broadly playable video track; its HE-AAC audio plays in all current
    browsers and phones, so the file is saved untouched. Full re-encoding is
    kept only for HEVC/10-bit sources that genuinely need it.
    """
    if not FFMPEG:
        return False
    info = probe_streams(path)
    if not info:
        return True
    return not video_stream_ok(info)


def uniform_video_audio(path, donor_urls, workdir, primary_url=None):
    """Give every saved MP4 the same HE-AACv2 audio without re-encoding.

    Most TikTok mirrors already carry HE-AACv2. A few (usually the clean
    `play` mirror of an older post) carry AAC-LC instead. Rather than spend
    20+ seconds re-encoding the audio of a ten-minute video, borrow the audio
    track from another mirror of the *same post* and stream-copy it beside the
    playable H.264 video. The mux itself takes about 200 ms.
    """
    if not FFMPEG:
        return path
    if "he-aacv2" in probe_streams(path):
        return path

    session = cffi_requests.Session(impersonate="chrome") if cffi_requests else None
    for index, raw in enumerate(donor_urls or []):
        if primary_url and raw == primary_url:
            continue
        try:
            url = validate_media_url(raw)
            content, _ = fetch_media(url, session, timeout=8)
            donor = os.path.join(workdir, f"audio-donor-{index}.mp4")
            with open(donor, "wb") as handle:
                handle.write(content)
            if "he-aacv2" not in probe_streams(donor):
                continue
            output = os.path.join(workdir, "uniform-audio.mp4")
            command = [
                FFMPEG, "-y", "-loglevel", "error", "-i", path, "-i", donor,
                "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
                "-movflags", "+faststart", output,
            ]
            subprocess.run(command, check=True, timeout=90, capture_output=True)
            return output
        except Exception as exc:
            log_failure("uniform video audio", exc)
    return path


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
    """Re-encode only incompatible video; always keep the source audio.

    Re-encoding a long clip's audio alone costs 20+ seconds, which delays the
    ad close. TikTok's mirrors all carry the same HE-AAC audio, so copying it
    keeps every saved file identical in audio quality with no extra wait.
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
        # Keep the source audio untouched. Every TikTok mirror carries the same
        # HE-AAC stream, so copying it makes short and long videos identical in
        # audio quality — and avoids a ~20 s re-encode on a 10-minute clip.
        "-c:a", "copy",
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
    filename = download_filename(meta, extension)
    # ASCII fallback for browsers that ignore filename*=; keep the title and
    # the unique post ID when they use Latin characters.
    raw_title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", str(meta.get("title") or "")).strip()
    ascii_title = unicodedata.normalize("NFKD", raw_title).encode("ascii", "ignore").decode()
    ascii_title = re.sub(r"[^A-Za-z0-9._ -]+", "_", ascii_title).strip(" ._")[:60]
    video_id = re.sub(r"[^A-Za-z0-9_-]+", "", str(meta.get("id") or ""))[:24]
    fallback = " - ".join(
        part for part in (ascii_title or "tiktok-video", video_id, "TikTok Video Downloader") if part
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
            # Prefer the CDN mirrors resolved by /api/video/info. That keeps
            # one paste -> download flow down to a single TikWM lookup, and
            # racing the mirrors stops one hanging edge from stretching the
            # wait (and the ad-close timing) for the whole video.
            media_hint = payload.get("media")
            resolved = media_hint if isinstance(media_hint, dict) else {}
            mirror_key = "audio_alternates" if mode == "mp3" else "video_alternates"
            mirrors = list(resolved.get(mirror_key) or [])
            raw_media = resolved.get("audio") if mode == "mp3" else resolved.get("video")
            primary_url = None
            if isinstance(raw_media, str) and raw_media.strip():
                try:
                    primary_url = validate_media_url(raw_media)
                    mirrors = [primary_url] + [item for item in mirrors if item != primary_url]
                except (ValueError, TypeError) as exc:
                    log_failure("media hint", exc)

            data = resolved or None
            if not mirrors:
                data = tikwm_data(url)
                if data:
                    mirrors = tikwm_media_candidates(data, mode)

            extension = ".mp3" if mode == "mp3" else ".mp4"
            path = os.path.join(workdir, "media" + extension)
            tikwm_ok = False

            def save_media(content, source, donor_urls=None, primary=None):
                nonlocal path
                with open(path, "wb") as handle:
                    handle.write(content)
                meta = {
                    "title": (source or {}).get("title") or "tiktok",
                    "id": (source or {}).get("id") or "video",
                    "duration": (source or {}).get("duration"),
                }
                if mode == "video":
                    if needs_transcode(path):
                        try:
                            path = make_compatible_mp4(path, workdir)
                        except Exception as exc:
                            log_failure("h264 transcode", exc)
                    path = uniform_video_audio(path, donor_urls, workdir, primary)
                else:
                    path = make_real_mp3(path, workdir)
                return meta

            if mirrors and cffi_requests is not None:
                try:
                    content, _content_type = fetch_media_raced(mirrors)
                    meta = save_media(content, data, mirrors, primary_url)
                    tikwm_ok = True
                except Exception as exc:
                    log_failure("tikwm download", exc)
                    # A signed CDN URL can expire between the info lookup and
                    # the click. Resolve once more, then race the fresh mirrors.
                    fresh = tikwm_data(url)
                    retry_urls = tikwm_media_candidates(fresh, mode) if fresh else []
                    if retry_urls:
                        try:
                            content, _content_type = fetch_media_raced(retry_urls)
                            meta = save_media(content, fresh, retry_urls)
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
                            if mode == "video":
                                if needs_transcode(path):
                                    try:
                                        path = make_compatible_mp4(path, workdir)
                                    except Exception as exc:
                                        # Keep the original file if re-encoding is
                                        # not possible — a file beats no file.
                                        log_failure("h264 transcode", exc)
                                path = uniform_video_audio(path, [], workdir)
                            else:
                                path = make_real_mp3(path, workdir)
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
