"""ffprobe subprocess wrappers."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path


def nice_preexec(nice: int | None) -> Callable[[], None] | None:
    """Return a preexec_fn that bumps the child's nice level, or None."""
    if nice is None:
        return None
    return lambda: os.nice(nice)


def probe(ffprobe: str, src: Path, nice: int | None = None) -> dict:
    cmd = [
        ffprobe, "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(src),
    ]
    result = subprocess.run(
        cmd, check=True, capture_output=True, text=True,
        preexec_fn=nice_preexec(nice),
    )
    return json.loads(result.stdout)


def first_frame_side_data(
    ffprobe: str, src: Path, nice: int | None = None,
) -> list[dict]:
    """Probe the first video frame for HDR side-data.

    The only path that catches HEVC SEI, AV1 OBU metadata, and MKV
    BlockAdditional — none of which appear in `-show_streams`."""
    cmd = [
        ffprobe, "-v", "error",
        "-select_streams", "v:0",
        "-read_intervals", "%+#1",
        "-show_frames",
        "-show_entries", "frame=side_data_list",
        "-of", "json",
        str(src),
    ]
    try:
        result = subprocess.run(
            cmd, check=True, capture_output=True, text=True,
            preexec_fn=nice_preexec(nice),
        )
        frames = json.loads(result.stdout).get("frames") or []
        if frames:
            return frames[0].get("side_data_list") or []
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        pass
    return []
