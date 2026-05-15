"""Polling batch mode: encode an input tree into a mirrored output tree.

Each poll scans the input dir, processes files whose size was unchanged since
the previous scan (so partial copies are skipped), and writes the result to
the matching relative path under the output dir with a .mkv extension. A
file is considered already done if its destination exists, so the manifest
is implicit and survives restarts without a state file."""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from av1ator.cli import require
from av1ator.encode import (
    DEFAULT_CRF,
    DEFAULT_PRESET,
    build_ffmpeg_cmd,
    svt_params,
)
from av1ator.hdr import hdr10_svt_params
from av1ator.probe import first_frame_side_data, probe
from av1ator.sidedata import merge_side_data

DEFAULT_INTERVAL = 30

VIDEO_EXTENSIONS = frozenset({
    ".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm",
    ".ts", ".m2ts", ".mts", ".wmv", ".flv",
    ".mpg", ".mpeg", ".vob", ".ogv", ".ogm", ".3gp", ".divx",
})

_SIZE_UNITS = {
    "": 1, "B": 1,
    "K": 10**3, "KB": 10**3,
    "M": 10**6, "MB": 10**6,
    "G": 10**9, "GB": 10**9,
    "T": 10**12, "TB": 10**12,
}


def _parse_size(s: str) -> int:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([A-Za-z]*)\s*", s)
    unit = m.group(2).upper() if m else ""
    if not m or unit not in _SIZE_UNITS:
        raise argparse.ArgumentTypeError(f"invalid size: {s!r}")
    return int(float(m.group(1)) * _SIZE_UNITS[unit])


def _filter_reason(args: argparse.Namespace, info: dict) -> str | None:
    """Return a reason string if the input should be skipped per user
    filters, else None. Size is checked separately by the caller."""
    video = next(
        (s for s in info.get("streams", []) if s.get("codec_type") == "video"),
        None,
    )
    if video is None:
        return "no video stream"
    codec = (video.get("codec_name") or "").lower()
    if codec in args.skip_codec:
        return f"codec is {codec}"
    w, h = video.get("width") or 0, video.get("height") or 0
    if args.max_width is not None and w > args.max_width:
        return f"width {w} > {args.max_width}"
    if args.max_height is not None and h > args.max_height:
        return f"height {h} > {args.max_height}"
    return None


def scan(root: Path) -> dict[Path, int]:
    sizes: dict[Path, int] = {}
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            try:
                sizes[p] = p.stat().st_size
            except OSError:
                continue
    return sizes


def output_path(src: Path, in_root: Path, out_root: Path) -> Path:
    return out_root / src.relative_to(in_root).with_suffix(".mkv")


def partial_path(dst: Path) -> Path:
    return dst.with_name(f"{dst.stem}.part{dst.suffix}")


class _State:
    stop = False
    proc: subprocess.Popen | None = None


def _handle_signal(signum, frame):
    _State.stop = True
    if _State.proc is not None:
        _State.proc.terminate()


def _encode(
    ffmpeg: str, ffprobe: str, src: Path, dst: Path, info: dict,
    crf: int, preset: int,
) -> int | None:
    """Return ffmpeg's rc, or None if the command cannot be built."""
    video = next(
        s for s in info.get("streams", []) if s.get("codec_type") == "video"
    )
    side_data = merge_side_data(video, first_frame_side_data(ffprobe, src))
    video_params = svt_params(video, side_data, preset, crf)
    if hdr10_svt_params(side_data):
        print(f"hdr10: {src}", file=sys.stderr)
    try:
        cmd = build_ffmpeg_cmd(ffmpeg, src, dst, info, video_params)
    except ValueError as e:
        print(f"error: {src}: {e}", file=sys.stderr)
        return None

    _State.proc = subprocess.Popen(cmd)
    try:
        return _State.proc.wait()
    finally:
        _State.proc = None


def main() -> int:
    p = argparse.ArgumentParser(
        description="Watch a directory and batch-encode video to AV1.",
    )
    p.add_argument("input_dir", type=Path)
    p.add_argument("output_dir", type=Path)
    p.add_argument("--crf", type=int, default=DEFAULT_CRF)
    p.add_argument("--preset", type=int, default=DEFAULT_PRESET)
    p.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL,
        help=f"poll interval in seconds (default {DEFAULT_INTERVAL})",
    )
    p.add_argument(
        "--delete-input", action="store_true",
        help="delete source file after a successful encode",
    )

    filters = p.add_argument_group(
        "input filters", "skip source files that match these criteria",
    )
    filters.add_argument(
        "--min-size", type=_parse_size, default=None, metavar="SIZE",
        help="skip files smaller than SIZE (e.g. 500MB, 2G)",
    )
    filters.add_argument(
        "--max-width", type=int, default=None, metavar="N",
        help="skip files wider than N pixels",
    )
    filters.add_argument(
        "--max-height", type=int, default=None, metavar="N",
        help="skip files taller than N pixels",
    )
    filters.add_argument(
        "--skip-codec", action="append", default=None, metavar="CODEC",
        help="skip files with this video codec (e.g. av1); repeatable",
    )

    args = p.parse_args()
    args.skip_codec = {c.lower() for c in (args.skip_codec or [])}

    if not args.input_dir.is_dir():
        sys.exit(f"error: input dir not found: {args.input_dir}")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg = require("ffmpeg")
    ffprobe = require("ffprobe")

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    skipped: set[Path] = set()
    prev: dict[Path, int] = {}

    while not _State.stop:
        current = scan(args.input_dir)
        stable = sorted(p for p, s in current.items() if prev.get(p) == s)
        prev = current

        for src in stable:
            if _State.stop:
                break
            if src in skipped:
                continue
            dst = output_path(src, args.input_dir, args.output_dir)
            if dst.exists():
                continue

            if args.min_size is not None and current[src] < args.min_size:
                print(
                    f"skip {src}: size {current[src]} < {args.min_size}",
                    file=sys.stderr,
                )
                skipped.add(src)
                continue

            try:
                info = probe(ffprobe, src)
            except subprocess.CalledProcessError:
                skipped.add(src)
                continue

            reason = _filter_reason(args, info)
            if reason is not None:
                print(f"skip {src}: {reason}", file=sys.stderr)
                skipped.add(src)
                continue

            dst.parent.mkdir(parents=True, exist_ok=True)
            partial = partial_path(dst)
            print(f"encoding {src} -> {dst}", file=sys.stderr)
            rc = _encode(
                ffmpeg, ffprobe, src, partial, info, args.crf, args.preset,
            )

            if rc is None:
                skipped.add(src)
                if partial.exists():
                    partial.unlink()
                continue
            if rc == 0:
                partial.rename(dst)
                if args.delete_input:
                    try:
                        src.unlink()
                    except OSError as e:
                        print(
                            f"warning: could not delete {src}: {e}",
                            file=sys.stderr,
                        )
            else:
                if partial.exists():
                    partial.unlink()
                if _State.stop:
                    break
                print(f"failed rc={rc}: {src}", file=sys.stderr)
                skipped.add(src)

        if _State.stop:
            break
        for _ in range(args.interval):
            if _State.stop:
                break
            time.sleep(1)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
