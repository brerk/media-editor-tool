"""routers/utils.py — shared helpers for Media Editor Suite."""

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

# ── Constants ─────────────────────────────────────────────────────────────────

VIDEO_EXTS = {
    ".mp4", ".mkv", ".mov", ".webm", ".avi", ".ts",
    ".m4v", ".flv", ".wmv", ".mts", ".m2ts", ".vob",
}

# Common aspect ratios  name → (width_part, height_part)
RATIOS: dict[str, tuple[int, int]] = {
    "9:16":  (9,  16),
    "16:9":  (16,  9),
    "1:1":   (1,   1),
    "4:3":   (4,   3),
    "3:4":   (3,   4),
    "4:5":   (4,   5),
    "5:4":   (5,   4),
    "21:9":  (21,  9),
    "2:1":   (2,   1),
}

# ── Time helpers ──────────────────────────────────────────────────────────────

def fmt_time(seconds: float) -> str:
    """Convert a float number of seconds to HH:MM:SS.mmm string for ffmpeg."""
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


# ── Video info ────────────────────────────────────────────────────────────────

def get_video_info(path: str) -> dict[str, Any]:
    """
    Return a dict with video metadata via ffprobe.

    Keys guaranteed:
        width, height  – int, pixels
        duration       – float, seconds
        fps            – float, frames per second
        frames         – int, total frame count (estimated)
        codec          – str, video codec name
        has_audio      – bool
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Video not found: {path}")

    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        str(p),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {result.stderr[:300]}")

    data = json.loads(result.stdout)
    streams = data.get("streams", [])
    fmt    = data.get("format", {})

    video_stream = next(
        (s for s in streams if s.get("codec_type") == "video"), None
    )
    if video_stream is None:
        raise ValueError("No video stream found")

    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    width  = int(video_stream.get("width",  0))
    height = int(video_stream.get("height", 0))
    codec  = video_stream.get("codec_name", "unknown")

    # Duration: prefer stream, fall back to format
    duration = float(
        video_stream.get("duration")
        or fmt.get("duration")
        or 0
    )

    # FPS: r_frame_rate is usually the display rate, e.g. "30000/1001"
    fps_raw = video_stream.get("r_frame_rate", "0/1")
    try:
        num, den = fps_raw.split("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0

    # nb_frames is most accurate when present
    frames_raw = video_stream.get("nb_frames")
    if frames_raw and frames_raw.isdigit():
        frames = int(frames_raw)
    else:
        frames = int(duration * fps) if fps > 0 else 0

    return {
        "width":     width,
        "height":    height,
        "duration":  duration,
        "fps":       fps,
        "frames":    frames,
        "codec":     codec,
        "has_audio": has_audio,
        "path":      str(p),
        "filename":  p.name,
        "size":      p.stat().st_size,
    }


# ── Job management ────────────────────────────────────────────────────────────

def make_job(total_frames: int) -> dict[str, Any]:
    """
    Create a fresh job dict.

    Schema consumed by ws_progress in server.py and watchJob in shared.js:
        progress  – 0–100 int
        status    – "running" | "done" | "error"
        log       – list[str]  (last N lines from ffmpeg)
        output    – str | None (set by caller after make_job)
        error     – str | None
    """
    return {
        "progress":     0,
        "status":       "running",
        "log":          [],
        "output":       None,
        "error":        None,
        "_total_frames": max(1, total_frames),
    }


# ── FFmpeg runner ─────────────────────────────────────────────────────────────

async def run_ffmpeg(cmd: list[str], job: dict[str, Any]) -> None:
    """
    Run an ffmpeg command asynchronously, updating *job* in place.

    ffmpeg is invoked with ``-progress pipe:2 -loglevel error`` (set by
    callers), so stderr carries progress key=value pairs mixed with error
    lines.

    Progress lines look like:
        frame=120
        fps=24.5
        out_time_ms=5000000
        progress=continue   (or "end")
    """
    total: int = job["_total_frames"]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        assert proc.stderr is not None
        current_frame = 0

        while True:
            line_bytes = await proc.stderr.readline()
            if not line_bytes:
                break
            line = line_bytes.decode(errors="replace").rstrip()

            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()

                if key == "frame":
                    try:
                        current_frame = int(value)
                        job["progress"] = min(99, int(current_frame * 100 / total))
                    except ValueError:
                        pass

                elif key == "progress" and value == "end":
                    job["progress"] = 100

                # Keep last 20 log lines for the WS status endpoint
                if line:
                    job["log"].append(line)
                    if len(job["log"]) > 20:
                        job["log"].pop(0)
            else:
                # Plain error/warning text from ffmpeg
                if line:
                    job["log"].append(line)
                    if len(job["log"]) > 20:
                        job["log"].pop(0)

        await proc.wait()

        if proc.returncode == 0:
            job["progress"] = 100
            job["status"]   = "done"
        else:
            job["status"] = "error"
            job["error"]  = " | ".join(job["log"][-3:]) or "ffmpeg exited with error"

    except Exception as exc:
        job["status"] = "error"
        job["error"]  = str(exc)
        job["log"].append(f"[utils] exception: {exc}")
