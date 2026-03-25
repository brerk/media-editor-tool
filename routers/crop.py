"""Crop tool router — /crop/*"""
import asyncio
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from .utils import get_video_info, run_ffmpeg, make_job, fmt_time, RATIOS
import tempfile, os

router = APIRouter(prefix="/crop", tags=["crop"])
_jobs: dict[str, dict] = {}

class Keyframe(BaseModel):
    time: float
    crop_x: float

class CropExportRequest(BaseModel):
    video_path: str
    output_path: str
    ratio: str = "9:16"
    crf: int = 18
    preset: str = "fast"
    keyframes: list[Keyframe] = []
    static_crop_x: float = 0.5

def build_crop_filter(keyframes, static_x, crop_w, crop_h, vw, vh):
    y = (vh - crop_h) // 2
    if not keyframes or len(keyframes) < 2:
        x = int((vw - crop_w) * (keyframes[0].crop_x if keyframes else static_x))
        x = max(0, min(x, vw - crop_w))
        return f"crop={crop_w}:{crop_h}:{x}:{y}"
    kfs = sorted(keyframes, key=lambda k: k.time)
    max_x = vw - crop_w
    def xa(kf): return int(max_x * kf.crop_x)
    expr = str(xa(kfs[-1]))
    for i in range(len(kfs) - 2, -1, -1):
        a, b = kfs[i], kfs[i+1]
        dt = b.time - a.time
        if dt <= 0:
            expr = f"if(lt(t,{b.time:.3f}),{xa(a)},{expr})"
        else:
            interp = f"{xa(a)}+({xa(b)}-{xa(a)})*(t-{a.time:.3f})/{dt:.3f}"
            expr = f"if(lt(t,{b.time:.3f}),{interp},{expr})"
    return f"crop={crop_w}:{crop_h}:'max(0,min({max_x},{expr}))':{y}"

@router.get("/frame")
async def get_frame(path: str = Query(...), t: float = Query(0.0),
                    w: int = Query(960)):
    p = Path(path)
    if not p.exists(): raise HTTPException(404, "File not found")
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp.close()
    ts = fmt_time(t)
    import subprocess
    r = subprocess.run([
        "ffmpeg", "-y", "-ss", ts, "-i", str(p),
        "-vframes", "1", "-q:v", "3",
        "-vf", f"scale={w}:-1:force_original_aspect_ratio=decrease",
        tmp.name], capture_output=True)
    if r.returncode != 0:
        raise HTTPException(500, "Frame extraction failed")
    return FileResponse(tmp.name, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=300"})

@router.post("/export")
async def export(req: CropExportRequest):
    p = Path(req.video_path)
    if not p.exists(): raise HTTPException(400, "Video not found")
    info = get_video_info(req.video_path)
    vw, vh = info["width"], info["height"]
    rw, rh = RATIOS.get(req.ratio, (9, 16))
    crop_h = vh
    crop_w = int(vh * rw / rh)
    if crop_w > vw: crop_w = vw; crop_h = int(vw * rh / rw)
    vf = build_crop_filter(req.keyframes, req.static_crop_x,
                           crop_w, crop_h, vw, vh)
    Path(req.output_path).parent.mkdir(parents=True, exist_ok=True)
    import uuid
    job_id = str(uuid.uuid4())[:8]
    job = make_job(info["frames"])
    job["output"] = req.output_path
    _jobs[job_id] = job
    cmd = ["ffmpeg", "-y", "-i", req.video_path, "-vf", vf,
           "-codec:v", "libx264", "-crf", str(req.crf),
           "-preset", req.preset, "-codec:a", "copy",
           "-progress", "pipe:2", "-loglevel", "error", req.output_path]
    asyncio.create_task(run_ffmpeg(cmd, job))
    return {"job_id": job_id}

@router.get("/job/{job_id}")
async def job_status(job_id: str):
    job = _jobs.get(job_id)
    if not job: raise HTTPException(404, "Job not found")
    return job

def get_jobs(): return _jobs
