"""ffprobe subprocess wrappers."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def probe(ffprobe: str, src: Path) -> dict:
    cmd = [
        ffprobe, "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(src),
    ]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def first_frame_side_data(ffprobe: str, src: Path) -> list[dict]:
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
        )
        frames = json.loads(result.stdout).get("frames") or []
        if frames:
            return frames[0].get("side_data_list") or []
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        pass
    return []
