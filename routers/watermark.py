"""Watermark tool router — /wm/*
Supports 1–N watermarks per export/preview, each with optional animation.
"""
import asyncio, tempfile, subprocess
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List, Optional
from .utils import get_video_info, run_ffmpeg, make_job, fmt_time
import uuid

router = APIRouter(prefix="/wm", tags=["watermark"])
_jobs: dict[str, dict] = {}

POSITIONS = {
    "top-left":     ("10",       "10"),
    "top-center":   ("(W-w)/2",  "10"),
    "top-right":    ("W-w-10",   "10"),
    "center-left":  ("10",       "(H-h)/2"),
    "center":       ("(W-w)/2",  "(H-h)/2"),
    "center-right": ("W-w-10",   "(H-h)/2"),
    "bottom-left":  ("10",       "H-h-10"),
    "bottom-center":("(W-w)/2",  "H-h-10"),
    "bottom-right": ("W-w-10",   "H-h-10"),
}

def _pad(expr: str, padding: int) -> str:
    return expr.replace("10", str(padding))

class WMLayer(BaseModel):
    wm_path:        str
    position:       str   = "bottom-right"
    scale_pct:      int   = 15
    opacity:        float = 1.0
    padding:        int   = 20
    animation:      str   = "none"   # none | fade | pendulum | float | bounce
    anim_speed:     float = 0.2      # slow default
    anim_amplitude: int   = 30

class WMExportRequest(BaseModel):
    video_path:  str
    output_path: str
    layers:      List[WMLayer]
    crf:         int  = 18
    preset:      str  = "fast"
    codec:       str  = "libx264"

class WMPreviewRequest(BaseModel):
    video_path: str
    layers:     List[WMLayer]
    t:          float = 0.0
    w:          int   = 960


def _build_layer_filters(layer: WMLayer, idx: int, prev_label: str) -> tuple[list[str], str]:
    """
    Build filter fragments for one layer.
    Returns (list_of_filter_fragments, output_label).
    Uses eval=frame on overlay so t/T/W/H/w/h work correctly.
    """
    wm_in  = f"[{idx+1}:v]"
    scaled = f"[wms{idx}]"
    parts  = []

    # 1. Scale
    parts.append(f"{wm_in}scale=iw*{layer.scale_pct}/100:-1{scaled}")

    # 2. Alpha manipulation (for fade animation or static opacity)
    s   = layer.anim_speed
    A   = layer.anim_amplitude
    wm_ready = scaled

    if layer.animation == "fade":
        # geq uses T = time in seconds (capital T), alpha channel 0-255
        faded = f"[wmf{idx}]"
        # escape commas inside geq with backslash for filter_complex
        parts.append(
            f"{scaled}format=rgba,"
            f"geq=r=r(X\\,Y):g=g(X\\,Y):b=b(X\\,Y):"
            f"a='255*{layer.opacity:.3f}*abs(sin(PI*{s:.4f}*T))'{faded}"
        )
        wm_ready = faded
    elif layer.opacity < 1.0:
        dimmed = f"[wmd{idx}]"
        parts.append(
            f"{scaled}format=rgba,"
            f"colorchannelmixer=aa={layer.opacity:.3f}{dimmed}"
        )
        wm_ready = dimmed

    # 3. Position base (with padding)
    pos    = POSITIONS.get(layer.position, ("W-w-10", "H-h-10"))
    base_x = _pad(pos[0], layer.padding)
    base_y = _pad(pos[1], layer.padding)

    # 4. Animation x/y expressions
    if layer.animation == "none" or layer.animation == "fade":
        x_expr = base_x
        y_expr = base_y

    elif layer.animation == "pendulum":
        # Lissajous-style: X oscillates, Y follows with phase offset → "U" shape
        x_expr = f"{base_x}+{A}*sin(2*PI*{s:.4f}*t)"
        y_expr = f"{base_y}+{A//2}*sin(2*PI*{s:.4f}*t+PI/2)"

    elif layer.animation == "float":
        x_expr = base_x
        y_expr = f"{base_y}+{A}*sin(2*PI*{s:.4f}*t)"

    elif layer.animation == "bounce":
        # Uses W/H/w/h which are available in overlay eval=frame context
        x_expr = f"abs(mod(t*{s*200:.2f},(W-w)*2)-(W-w))"
        y_expr = f"abs(mod(t*{s*130:.2f},(H-h)*2)-(H-h))"

    else:
        x_expr = base_x
        y_expr = base_y

    out_label = f"[vl{idx}]"
    needs_eval = layer.animation not in ("none",)
    eval_opt   = ":eval=frame" if needs_eval else ""
    parts.append(f"{prev_label}{wm_ready}overlay={x_expr}:{y_expr}{eval_opt}{out_label}")

    return parts, out_label


def build_filter_complex(layers: List[WMLayer], preview_w: int = 0) -> tuple[str, str]:
    all_parts = []
    prev = "[0:v]"

    for i, layer in enumerate(layers):
        frags, out_label = _build_layer_filters(layer, i, prev)
        all_parts.extend(frags)
        prev = out_label

    # Rename last label to [vout]
    if all_parts:
        last = all_parts[-1]
        final_label = prev
        if final_label != "[vout]":
            all_parts[-1] = last[:-len(final_label)] + "[vout]"
            final_label = "[vout]"
    else:
        final_label = "[vout]"

    fc = ";".join(all_parts)

    if preview_w and fc:
        fc += f";[vout]scale={preview_w}:-1:force_original_aspect_ratio=decrease[vfinal]"
        return fc, "[vfinal]"

    return fc, final_label


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


@router.post("/preview")
async def preview_composite(req: WMPreviewRequest):
    vp = Path(req.video_path)
    if not vp.exists(): raise HTTPException(404, "Video not found")
    active = [l for l in req.layers if l.wm_path and Path(l.wm_path).exists()]
    if not active: raise HTTPException(400, "No valid watermark layers")

    fc, out_label = build_filter_complex(active, preview_w=req.w)

    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp.close()
    cmd = ["ffmpeg", "-y", "-ss", fmt_time(req.t), "-i", str(vp)]
    for layer in active: cmd += ["-i", layer.wm_path]
    cmd += ["-filter_complex", fc, "-map", out_label,
            "-vframes", "1", "-q:v", "3", tmp.name]

    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise HTTPException(500, f"Preview failed: {r.stderr.decode()[-400:]}")
    return FileResponse(tmp.name, media_type="image/jpeg",
                        headers={"Cache-Control": "no-cache"})


@router.post("/export")
async def export(req: WMExportRequest):
    vp = Path(req.video_path)
    if not vp.exists(): raise HTTPException(400, "Video not found")
    active = [l for l in req.layers if l.wm_path and Path(l.wm_path).exists()]
    if not active: raise HTTPException(400, "No valid watermark layers")

    info = get_video_info(req.video_path)
    Path(req.output_path).parent.mkdir(parents=True, exist_ok=True)

    fc, out_label = build_filter_complex(active)

    job_id = str(uuid.uuid4())[:8]
    job    = make_job(info["frames"])
    job["output"] = req.output_path
    _jobs[job_id] = job

    cmd = ["ffmpeg", "-y", "-i", req.video_path]
    for layer in active: cmd += ["-i", layer.wm_path]
    cmd += [
        "-filter_complex", fc,
        "-map", out_label,
        "-map", "0:a?",
        "-codec:v", req.codec,
        "-crf",     str(req.crf),
        "-preset",  req.preset,
        "-codec:a", "copy",
        "-progress", "pipe:2", "-loglevel", "error",
        req.output_path,
    ]
    asyncio.create_task(run_ffmpeg(cmd, job))
    return {"job_id": job_id}


@router.get("/job/{job_id}")
async def job_status(job_id: str):
    job = _jobs.get(job_id)
    if not job: raise HTTPException(404, "Job not found")
    return job

def get_jobs(): return _jobs
