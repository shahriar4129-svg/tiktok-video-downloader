"""Vercel function -> POST /api/video/prepare

The browser calls this as a best-effort warm-up hint before an ad is shown.
Real resolution and streaming happen in /api/video/info and /api/video/download,
which serve every format from the same prepared-file cache. Keeping this
endpoint cheap and fast avoids a noisy 404 in the console without duplicating
download work.
"""

from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI()


@app.post("/")
@app.post("/api/video/prepare")
@app.post("/video/prepare")
def prepare():
    return JSONResponse({"ok": True, "warmed": False})
