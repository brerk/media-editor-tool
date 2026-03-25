#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.9"
# dependencies = ["fastapi", "uvicorn[standard]", "python-multipart"]
# ///
"""
server.py - Media Editor Suite backend
Usage: uv run server.py
       uv run server.py --port 7070 --host 0.0.0.0 --open
"""

from pathlib import Path
from fastapi import FastAPI, HTTPException, Query, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import asyncio, shutil, tempfile, uuid

from routers import crop_router, trim_router, watermark_router
from routers.utils import get_video_info, VIDEO_EXTS

# ── Directories ──────────────────────────────────────────────────────────────
import os
_base = Path(__file__).parent
UPLOAD_DIR = Path(os.environ.get("MEDIA_UPLOAD_DIR", _base / "uploads"))
OUTPUT_DIR = Path(os.environ.get("MEDIA_OUTPUT_DIR", _base / "outputs"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Public API URL (injected into the frontend) ───────────────────────────────
API_URL = os.environ.get("MEDIA_API_URL", "")

app = FastAPI(title="Media Editor Suite")

app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# Mount routers
app.include_router(crop_router)
app.include_router(trim_router)
app.include_router(watermark_router)

# Serve static files
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# ── Shared endpoints ──────────────────────────────────────────────────────────

@app.get("/")
async def index():
    html_path = Path(__file__).parent / "index.html"
    if not html_path.exists():
        return HTMLResponse("<h1>index.html not found</h1>")
    content = html_path.read_text()
    injection = f'<script>window.__API_URL__ = {repr(API_URL)};</script>'
    content = content.replace("</head>", f"{injection}\n</head>", 1)
    return HTMLResponse(content)

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """
    Receive a video or image upload, save it to UPLOAD_DIR with a unique name,
    and return the server-side path so tools can reference it.
    """
    ext = Path(file.filename).suffix.lower() if file.filename else ""
    allowed = VIDEO_EXTS | {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    if ext not in allowed:
        raise HTTPException(400, f"Unsupported file type: {ext or '(none)'}")

    dest = UPLOAD_DIR / f"{uuid.uuid4().hex}{ext}"
    try:
        with dest.open("wb") as f:
            shutil.copyfileobj(file.file, f)
    finally:
        await file.close()

    is_video = ext in VIDEO_EXTS
    info = {}
    if is_video:
        try:
            info = get_video_info(str(dest))
        except Exception:
            pass

    return {
        "path":     str(dest),
        "filename": file.filename,
        "size":     dest.stat().st_size,
        "is_video": is_video,
        "is_image": not is_video,
        **info,
    }

@app.get("/config")
async def get_config():
    """Expose server-side paths to the frontend."""
    return {
        "upload_dir": str(UPLOAD_DIR),
        "output_dir": str(OUTPUT_DIR),
        "api_url":    API_URL,
    }

@app.get("/video")
async def serve_video(path: str = Query(...)):
    """Serve local video with range support for browser seeking."""
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "File not found")
    ext = p.suffix.lower()
    mimes = {".mp4": "video/mp4", ".mkv": "video/webm",
             ".mov": "video/quicktime", ".webm": "video/webm",
             ".avi": "video/x-msvideo", ".ts": "video/mp2t"}
    return FileResponse(str(p), media_type=mimes.get(ext, "video/mp4"),
                        headers={"Accept-Ranges": "bytes"})

@app.get("/image")
async def serve_image(path: str = Query(...)):
    """Serve a local image file."""
    p = Path(path)
    if not p.exists(): raise HTTPException(404, "File not found")
    ext = p.suffix.lower()
    mimes = {".png": "image/png", ".jpg": "image/jpeg",
             ".jpeg": "image/jpeg", ".webp": "image/webp",
             ".gif": "image/gif"}
    return FileResponse(str(p), media_type=mimes.get(ext, "image/png"))

@app.get("/download")
async def download_file(path: str = Query(...)):
    """Serve an output file as a download (Content-Disposition: attachment)."""
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(str(p), filename=p.name,
                        headers={"Content-Disposition": f'attachment; filename="{p.name}"'})

@app.get("/info")
async def video_info(path: str = Query(...)):
    try:
        return get_video_info(path)
    except Exception as e:
        raise HTTPException(400, str(e))

@app.websocket("/ws/progress/{tool}/{job_id}")
async def ws_progress(websocket: WebSocket, tool: str, job_id: str):
    """Stream job progress. tool = crop | trim | wm"""
    from routers.crop import get_jobs as crop_jobs
    from routers.trim import get_jobs as trim_jobs
    from routers.watermark import get_jobs as wm_jobs
    jobs_map = {"crop": crop_jobs, "trim": trim_jobs, "wm": wm_jobs}
    get_jobs = jobs_map.get(tool)
    await websocket.accept()
    try:
        while True:
            job = get_jobs().get(job_id) if get_jobs else None
            if not job:
                await websocket.send_json({"error": "job not found"}); break
            await websocket.send_json({
                "progress": job["progress"],
                "status":   job["status"],
                "log":      job["log"][-5:],
            })
            if job["status"] in ("done", "error"): break
            await asyncio.sleep(0.4)
    except WebSocketDisconnect:
        pass

# ── Entry point ───────────────────────────────────────────────────────────────

def _main():
    import uvicorn, argparse
    global API_URL
    parser = argparse.ArgumentParser(description="Media Editor Suite")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=7070)
    parser.add_argument("--open", action="store_true")
    parser.add_argument("--api-url", default="",
        help="Public URL the browser uses to reach this server "
             "(e.g. http://192.168.1.50:7070). "
             "Defaults to same-origin. Also via MEDIA_API_URL env var.")
    args = parser.parse_args()
    if args.api_url:
        API_URL = args.api_url
    elif not API_URL and args.host not in ("0.0.0.0", ""):
        API_URL = f"http://{args.host}:{args.port}"
    if args.open:
        import webbrowser, threading
        threading.Timer(1.2, lambda: webbrowser.open(
            f"http://{args.host}:{args.port}")).start()
    print(f"\n  Media Editor  →  http://{args.host}:{args.port}\n")
    uvicorn.run(app, host=args.host, port=args.port, reload=False)

if __name__ == "__main__":
    _main()
