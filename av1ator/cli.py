"""Command-line entry point: parses args, orchestrates probe and encode.

We probe the source for HDR10/colour metadata and audio layout, then run a
single ffmpeg invocation that encodes video with libsvtav1, re-encodes (or
copies) audio, and copies subtitles."""

from __future__ import annotations

import argparse
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from av1ator.encode import (
    DEFAULT_CRF,
    DEFAULT_FILM_GRAIN,
    DEFAULT_PRESET,
    build_ffmpeg_cmd,
    svt_params,
)
from av1ator.hdr import hdr10_svt_params
from av1ator.probe import first_frame_side_data, nice_preexec, probe
from av1ator.sidedata import merge_side_data


def require(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        sys.exit(f"error: {tool!r} not found on PATH")
    return path


def main() -> int:
    p = argparse.ArgumentParser(
        description="Convert video to AV1 (SVT-AV1) via ffmpeg.",
    )
    p.add_argument("input", type=Path)
    p.add_argument(
        "output", type=Path, nargs="?",
        help="output path (default: <input>.av1.mkv)",
    )
    p.add_argument(
        "--crf", type=int, default=DEFAULT_CRF,
        help=f"SVT-AV1 CRF, 0-63 (lower = higher quality, default {DEFAULT_CRF})",
    )
    p.add_argument(
        "--preset", type=int, default=DEFAULT_PRESET,
        help=f"0 (slowest/best) to 13 (fastest), default {DEFAULT_PRESET}",
    )
    p.add_argument(
        "--film-grain", type=int, default=DEFAULT_FILM_GRAIN, metavar="N",
        help=(
            f"synthesised film grain, 0-50 (0 = off; dithers away banding in "
            f"dark/smooth scenes at ~no bitrate cost, default {DEFAULT_FILM_GRAIN})"
        ),
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="print the ffmpeg command and exit",
    )
    p.add_argument(
        "--nice", type=int, default=None, metavar="N",
        help="run ffprobe/ffmpeg with nice increment N (lower priority)",
    )
    args = p.parse_args()

    if not args.input.is_file():
        sys.exit(f"error: input not found: {args.input}")
    dst = args.output or args.input.with_suffix(".av1.mkv")
    if dst.exists():
        sys.exit(f"error: output already exists: {dst}")

    ffmpeg = require("ffmpeg")
    ffprobe = require("ffprobe")

    info = probe(ffprobe, args.input, nice=args.nice)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        sys.exit("error: no video stream found in input")

    side_data = merge_side_data(
        video, first_frame_side_data(ffprobe, args.input, nice=args.nice),
    )
    video_params = svt_params(
        video, side_data, args.preset, args.crf, args.film_grain,
    )

    if hdr10_svt_params(side_data):
        print(
            "hdr10: passing through mastering/content-light metadata",
            file=sys.stderr,
        )

    try:
        ffmpeg_cmd = build_ffmpeg_cmd(
            ffmpeg, args.input, dst, info, video_params,
        )
    except ValueError as e:
        sys.exit(f"error: {e}")

    if args.dry_run:
        print(" ".join(shlex.quote(a) for a in ffmpeg_cmd))
        return 0

    return subprocess.run(
        ffmpeg_cmd, preexec_fn=nice_preexec(args.nice),
    ).returncode
