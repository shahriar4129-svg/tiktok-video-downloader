"""Vercel function -> GET /api/health"""

from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI()


@app.get("/")
@app.get("/api/health")
@app.get("/health")
def health():
    return JSONResponse({"ok": True, "service": "tiktok-video-downloader", "platform": "tiktok"})
