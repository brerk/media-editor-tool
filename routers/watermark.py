"""Watermark tool router — /wm/*"""
import asyncio, tempfile, subprocess, os, base64
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from typing import Optional
from .utils import get_video_info, run_ffmpeg, make_job, fmt_time
import uuid

router = APIRouter(prefix="/wm", tags=["watermark"])
_jobs: dict[str, dict] = {}

POSITIONS = {
    "top-left":     ("10", "10"),
    "top-center":   ("(W-w)/2", "10"),
    "top-right":    ("W-w-10", "10"),
    "center-left":  ("10", "(H-h)/2"),
    "center":       ("(W-w)/2", "(H-h)/2"),
    "center-right": ("W-w-10", "(H-h)/2"),
    "bottom-left":  ("10", "H-h-10"),
    "bottom-center":("(W-w)/2", "H-h-10"),
    "bottom-right": ("W-w-10", "H-h-10"),
}

class WMExportRequest(BaseModel):
    video_path: str
    wm_path: str
    output_path: str
    position: str = "bottom-right"
    scale_pct: int = 15       # WM size as % of video width
    opacity: float = 1.0
    padding: int = 20
    crf: int = 18
    preset: str = "fast"
    codec: str = "libx264"
    container: str = "mp4"

@router.get("/frame")
async def get_frame(path: str = Query(...), t: float = Query(0.0),
                    w: int = Query(960)):
    p = Path(path)
    if not p.exists(): raise HTTPException(404, "File not found")
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp.close()
    r = subprocess.run([
        "ffmpeg", "-y", "-ss", fmt_time(t), "-i", str(p),
        "-vframes", "1", "-q:v", "3",
        "-vf", f"scale={w}:-1:force_original_aspect_ratio=decrease",
        tmp.name], capture_output=True)
    if r.returncode != 0: raise HTTPException(500, "Frame extraction failed")
    return FileResponse(tmp.name, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=60"})

@router.get("/preview")
async def preview_composite(
    video_path: str = Query(...),
    wm_path: str = Query(...),
    t: float = Query(0.0),
    position: str = Query("bottom-right"),
    scale_pct: int = Query(15),
    opacity: float = Query(1.0),
    padding: int = Query(20),
    w: int = Query(960),
):
    """Return a composited preview frame with watermark."""
    vp = Path(video_path); wp = Path(wm_path)
    if not vp.exists(): raise HTTPException(404, "Video not found")
    if not wp.exists(): raise HTTPException(404, "WM not found")

    pos = POSITIONS.get(position, ("W-w-10", "H-h-10"))
    px = pos[0].replace("10", str(padding))
    py = pos[1].replace("10", str(padding))

    scale_f = f"[1:v]scale=iw*{scale_pct}/100:-1[wm];"
    alpha_f = f"[wm]format=rgba,colorchannelmixer=aa={opacity:.2f}[wmo];" if opacity < 1.0 else "[wm]copy[wmo];"
    overlay  = f"[0:v][wmo]overlay={px}:{py}"
    vf = f"{scale_f}{alpha_f}{overlay},scale={w}:-1:force_original_aspect_ratio=decrease"

    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp.close()
    r = subprocess.run([
        "ffmpeg", "-y", "-ss", fmt_time(t),
        "-i", str(vp), "-i", str(wp),
        "-filter_complex", vf,
        "-vframes", "1", "-q:v", "3", tmp.name
    ], capture_output=True)
    if r.returncode != 0:
        raise HTTPException(500, f"Preview failed: {r.stderr.decode()[-300:]}")
    return FileResponse(tmp.name, media_type="image/jpeg",
                        headers={"Cache-Control": "no-cache"})

@router.post("/export")
async def export(req: WMExportRequest):
    vp = Path(req.video_path); wp = Path(req.wm_path)
    if not vp.exists(): raise HTTPException(400, "Video not found")
    if not wp.exists(): raise HTTPException(400, "WM not found")
    info = get_video_info(req.video_path)
    Path(req.output_path).parent.mkdir(parents=True, exist_ok=True)

    pos = POSITIONS.get(req.position, ("W-w-10", "H-h-10"))
    px  = pos[0].replace("10", str(req.padding))
    py  = pos[1].replace("10", str(req.padding))
    scale_f = f"[1:v]scale=iw*{req.scale_pct}/100:-1[wm];"
    alpha_f = (f"[wm]format=rgba,colorchannelmixer=aa={req.opacity:.2f}[wmo];"
               if req.opacity < 1.0 else "[wm]copy[wmo];")
    overlay = f"[0:v][wmo]overlay={px}:{py}"
    vf = f"{scale_f}{alpha_f}{overlay}"

    job_id = str(uuid.uuid4())[:8]
    job = make_job(info["frames"])
    job["output"] = req.output_path
    _jobs[job_id] = job

    cmd = ["ffmpeg", "-y",
           "-i", req.video_path,
           "-i", req.wm_path,
           "-filter_complex", vf,
           "-codec:v", req.codec,
           "-crf", str(req.crf),
           "-preset", req.preset,
           "-codec:a", "copy",
           "-progress", "pipe:2", "-loglevel", "error",
           req.output_path]
    asyncio.create_task(run_ffmpeg(cmd, job))
    return {"job_id": job_id}

@router.get("/job/{job_id}")
async def job_status(job_id: str):
    job = _jobs.get(job_id)
    if not job: raise HTTPException(404, "Job not found")
    return job

def get_jobs(): return _jobs
