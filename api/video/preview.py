"""Vercel function -> GET/POST /api/video/preview.

The preview player cannot use TikTok's signed CDN URLs directly: those URLs are
hotlink-protected and the browser blocks the HTML 403 response. Reuse the
download app so both flows prepare the same file once and stream it inline.
"""

import sys
from pathlib import Path

VIDEO_DIR = Path(__file__).resolve().parent
if str(VIDEO_DIR) not in sys.path:
    sys.path.insert(0, str(VIDEO_DIR))

from download import app  # noqa: E402,F401
