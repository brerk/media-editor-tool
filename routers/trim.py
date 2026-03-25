"""Trim tool router — /trim/*"""
import asyncio, tempfile, os, subprocess
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from .utils import get_video_info, run_ffmpeg, make_job, fmt_time
import uuid

router = APIRouter(prefix="/trim", tags=["trim"])
_jobs: dict[str, dict] = {}

class TrimExportRequest(BaseModel):
    video_path: str
    output_path: str
    mark_in: float = 0.0
    mark_out: float = 0.0
    mode: str = "copy"       # "copy" (stream copy) or "encode" (re-encode)
    crf: int = 18
    preset: str = "fast"

class ThumbnailRequest(BaseModel):
    video_path: str
    output_path: str
    time: float = 0.0
    quality: int = 90         # JPEG quality

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

@router.post("/export")
async def export(req: TrimExportRequest):
    p = Path(req.video_path)
    if not p.exists(): raise HTTPException(400, "Video not found")
    dur = req.mark_out - req.mark_in
    if dur <= 0: raise HTTPException(400, "Invalid trim range")
    info = get_video_info(req.video_path)
    Path(req.output_path).parent.mkdir(parents=True, exist_ok=True)
    job_id = str(uuid.uuid4())[:8]
    job = make_job(int(info["fps"] * dur))
    job["output"] = req.output_path
    _jobs[job_id] = job
    if req.mode == "copy":
        cmd = ["ffmpeg", "-y",
               "-ss", fmt_time(req.mark_in),
               "-i", req.video_path,
               "-t", str(dur),
               "-c", "copy",
               "-progress", "pipe:2", "-loglevel", "error",
               req.output_path]
    else:
        cmd = ["ffmpeg", "-y",
               "-ss", fmt_time(req.mark_in),
               "-i", req.video_path,
               "-t", str(dur),
               "-codec:v", "libx264", "-crf", str(req.crf),
               "-preset", req.preset,
               "-codec:a", "aac",
               "-progress", "pipe:2", "-loglevel", "error",
               req.output_path]
    asyncio.create_task(run_ffmpeg(cmd, job))
    return {"job_id": job_id}

@router.post("/thumbnail")
async def save_thumbnail(req: ThumbnailRequest):
    p = Path(req.video_path)
    if not p.exists(): raise HTTPException(400, "Video not found")
    Path(req.output_path).parent.mkdir(parents=True, exist_ok=True)
    ext = Path(req.output_path).suffix.lower()
    if ext in (".jpg", ".jpeg"):
        vf = f"scale=1920:-1"
        q  = ["-q:v", str(max(1, min(31, int(31 - req.quality * 0.3))))]
    else:
        vf = "scale=1920:-1"
        q  = ["-compression_level", "6"]
    r = subprocess.run([
        "ffmpeg", "-y", "-ss", fmt_time(req.time),
        "-i", str(p), "-vframes", "1",
        "-vf", vf, *q, req.output_path
    ], capture_output=True)
    if r.returncode != 0:
        raise HTTPException(500, f"Thumbnail failed: {r.stderr.decode()[-200:]}")
    return {"output": req.output_path, "ok": True}

@router.get("/job/{job_id}")
async def job_status(job_id: str):
    job = _jobs.get(job_id)
    if not job: raise HTTPException(404, "Job not found")
    return job

def get_jobs(): return _jobs
