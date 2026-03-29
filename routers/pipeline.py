"""Pipeline router — /pipeline/*
Combines crop + trim + watermark in a single ffmpeg encode.
"""
import asyncio
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from .utils import get_video_info, run_ffmpeg, make_job, fmt_time, RATIOS
from .watermark import WMLayer, build_filter_complex as wm_filter_complex
import uuid

router = APIRouter(prefix="/pipeline", tags=["pipeline"])
_jobs: dict[str, dict] = {}


# ── Sub-configs (all optional) ────────────────────────────────────────────

class PipelineCropConfig(BaseModel):
    enabled:      bool  = False
    ratio:        str   = "9:16"
    static_crop_x: float = 0.5
    keyframes:    list  = []   # [{time, crop_x}]

class PipelineTrimConfig(BaseModel):
    enabled:  bool  = False
    mark_in:  float = 0.0
    mark_out: float = 0.0   # 0 = use full duration

class PipelineExportRequest(BaseModel):
    video_path:  str
    output_path: str
    crop:        PipelineCropConfig  = PipelineCropConfig()
    trim:        PipelineTrimConfig  = PipelineTrimConfig()
    wm_layers:   List[WMLayer]       = []
    crf:         int  = 18
    preset:      str  = "fast"
    codec:       str  = "libx264"


def _build_crop_expr(crop: PipelineCropConfig, vw: int, vh: int) -> str:
    """Return a crop= filter string."""
    rw, rh = RATIOS.get(crop.ratio, (9, 16))
    crop_h = vh
    crop_w = int(vh * rw / rh)
    if crop_w > vw:
        crop_w = vw
        crop_h = int(vw * rh / rw)

    y = (vh - crop_h) // 2

    kfs = crop.keyframes
    if not kfs or len(kfs) < 2:
        x_frac = kfs[0]["crop_x"] if kfs else crop.static_crop_x
        x = int((vw - crop_w) * x_frac)
        x = max(0, min(x, vw - crop_w))
        return f"crop={crop_w}:{crop_h}:{x}:{y}"

    # Keyframe interpolation
    sorted_kfs = sorted(kfs, key=lambda k: k["time"])
    max_x = vw - crop_w

    def xa(kf):
        return int(max_x * kf["crop_x"])

    expr = str(xa(sorted_kfs[-1]))
    for i in range(len(sorted_kfs) - 2, -1, -1):
        a, b = sorted_kfs[i], sorted_kfs[i + 1]
        dt = b["time"] - a["time"]
        if dt <= 0:
            expr = f"if(lt(t,{b['time']:.3f}),{xa(a)},{expr})"
        else:
            interp = f"{xa(a)}+({xa(b)}-{xa(a)})*(t-{a['time']:.3f})/{dt:.3f}"
            expr = f"if(lt(t,{b['time']:.3f}),{interp},{expr})"

    return f"crop={crop_w}:{crop_h}:'max(0,min({max_x},{expr}))':{y}"


@router.post("/export")
async def export(req: PipelineExportRequest):
    vp = Path(req.video_path)
    if not vp.exists():
        raise HTTPException(400, "Video not found")

    info    = get_video_info(req.video_path)
    vw, vh  = info["width"], info["height"]
    Path(req.output_path).parent.mkdir(parents=True, exist_ok=True)

    # ── Build ffmpeg command ──────────────────────────────────────────────
    cmd = ["ffmpeg", "-y"]

    # Trim: seek before input for speed (stream-level seek)
    trim_in  = req.trim.mark_in  if req.trim.enabled else 0.0
    trim_out = req.trim.mark_out if req.trim.enabled else 0.0
    dur      = (trim_out - trim_in) if (req.trim.enabled and trim_out > trim_in) else None

    if trim_in > 0:
        cmd += ["-ss", fmt_time(trim_in)]
    cmd += ["-i", req.video_path]
    if dur:
        cmd += ["-t", str(dur)]

    # ── Filter complex or simple vf ───────────────────────────────────────
    active_wm = [l for l in req.wm_layers if l.wm_path and Path(l.wm_path).exists()]

    # Add WM inputs
    for layer in active_wm:
        cmd += ["-i", layer.wm_path]

    if req.crop.enabled or active_wm:
        # Need filter_complex
        parts = []
        prev  = "[0:v]"

        if req.crop.enabled:
            crop_expr = _build_crop_expr(req.crop, vw, vh)
            parts.append(f"{prev}{crop_expr}[cropped]")
            prev = "[cropped]"

        if active_wm:
            # Re-index WM inputs: they start at input 1 (video is 0)
            # Temporarily shift WMLayer stream indices via a small wrapper
            wm_fc, wm_out = wm_filter_complex(active_wm)
            # wm_filter_complex uses [1:v],[2:v]... which is correct since
            # video is [0:v] and WM files are [1:v],[2:v]...
            # But we need to chain from [prev], not [0:v]
            # Replace the first reference to [0:v] in the wm filter
            wm_fc_patched = wm_fc.replace("[0:v]", prev, 1)
            parts.append(wm_fc_patched)
            final_label = wm_out
        else:
            # Crop only — rename to [vout]
            parts[-1] = parts[-1][:-len(prev)] + "[vout]"  # rename last label
            final_label = "[vout]"

        fc = ";".join(parts)
        cmd += ["-filter_complex", fc, "-map", final_label, "-map", "0:a?"]

    else:
        # No filters needed — passthrough video, just trim
        cmd += ["-map", "0:v", "-map", "0:a?"]

    # ── Encode ────────────────────────────────────────────────────────────
    cmd += [
        "-codec:v", req.codec,
        "-crf",     str(req.crf),
        "-preset",  req.preset,
        "-codec:a", "aac",
        "-progress", "pipe:2", "-loglevel", "error",
        req.output_path,
    ]

    # Estimate frames for progress
    fps    = info["fps"]
    frames = int(dur * fps) if dur else info["frames"]

    job_id = str(uuid.uuid4())[:8]
    job    = make_job(max(1, frames))
    job["output"] = req.output_path
    _jobs[job_id] = job

    asyncio.create_task(run_ffmpeg(cmd, job))
    return {"job_id": job_id}


@router.get("/job/{job_id}")
async def job_status(job_id: str):
    job = _jobs.get(job_id)
    if not job: raise HTTPException(404, "Job not found")
    return job

def get_jobs(): return _jobs
