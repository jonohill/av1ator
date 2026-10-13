"""Polling batch mode: encode an input tree into a mirrored output tree.

Each poll scans the input dir, processes files whose size was unchanged since
the previous scan (so partial copies are skipped), and writes the result to
the matching relative path under the output dir with a .mkv extension. A
file is considered already done if its destination exists, so the manifest
is implicit and survives restarts without a state file."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

from av1ator.cli import require
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


def passthrough_path(src: Path, in_root: Path, out_root: Path) -> Path:
    return out_root / src.relative_to(in_root)


def partial_path(dst: Path) -> Path:
    return dst.with_name(f"{dst.stem}.part{dst.suffix}")


def progress_blocks(lines: Iterable[str]) -> Iterator[dict[str, str]]:
    """Yield each key=value block of ffmpeg's -progress output."""
    block: dict[str, str] = {}
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if not sep:
            continue
        block[key] = value.strip()
        if key == "progress":
            yield block
            block = {}


def _number(value: str | None, convert: Callable[[str], float]):
    if value is None:
        return None
    try:
        return convert(value)
    except ValueError:
        return None


def progress_fields(block: dict[str, str]) -> dict:
    """Map an ffmpeg progress block to status fields, omitting any that
    ffmpeg reported as N/A."""
    out_time_us = _number(block.get("out_time_us"), int)
    speed = block.get("speed")
    fields = {
        "position_seconds": None if out_time_us is None else out_time_us / 1e6,
        "fps": _number(block.get("fps"), float),
        "speed": _number(speed.removesuffix("x") if speed else None, float),
        "output_bytes": _number(block.get("total_size"), int),
    }
    return {k: v for k, v in fields.items() if v is not None}


def _duration(info: dict | None) -> float | None:
    if info is None:
        return None
    return _number((info.get("format") or {}).get("duration"), float)


class StatusFile:
    """Maintains status_dir/status.json for an external monitor. Every
    method is a no-op when status_dir is None.

    skipped is shared with the caller, which adds to it as files are given
    up on."""

    def __init__(
        self, status_dir: Path | None, in_root: Path, out_root: Path,
        skipped: dict[Path, tuple[str, float]],
    ):
        self.path = None if status_dir is None else status_dir / "status.json"
        self.in_root = in_root
        self.out_root = out_root
        self.skipped = skipped
        self.current: dict | None = None

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def start(
        self, action: str, src: Path, dst: Path, input_bytes: int,
        info: dict | None = None,
    ) -> None:
        current = {
            "action": action,
            "src": src.relative_to(self.in_root).as_posix(),
            "dst": dst.relative_to(self.out_root).as_posix(),
            "input_bytes": input_bytes,
        }
        duration = _duration(info)
        if duration is not None:
            current["duration_seconds"] = duration
        current["started_at"] = time.time()
        self.current = current
        self.write()

    def progress(self, fields: dict) -> None:
        if self.current is not None:
            self.current.update(fields)
        self.write()

    def idle(self) -> None:
        self.current = None
        self.write()

    def write(self) -> None:
        if self.path is None:
            return
        doc = {
            "updated_at": time.time(),
            "current": self.current,
            "skipped": [
                {
                    "src": src.relative_to(self.in_root).as_posix(),
                    "reason": reason,
                    "at": at,
                }
                for src, (reason, at) in self.skipped.items()
            ],
        }
        tmp = self.path.with_name(f"{self.path.name}.tmp")
        try:
            tmp.write_text(json.dumps(doc))
            os.replace(tmp, self.path)
        except OSError as e:
            print(
                f"warning: could not write {self.path}: {e}", file=sys.stderr,
            )


class _State:
    stop = False
    proc: subprocess.Popen | None = None


def _handle_signal(signum, frame):
    _State.stop = True
    if _State.proc is not None:
        _State.proc.terminate()


def _encode(
    ffmpeg: str, ffprobe: str, src: Path, dst: Path, info: dict,
    crf: int, preset: int, film_grain: int, nice: int | None = None,
    on_progress: Callable[[dict], None] | None = None,
) -> int:
    """Return ffmpeg's rc. Raises ValueError if the command cannot be built.

    on_progress, if given, receives the progress_fields of each progress
    report ffmpeg emits."""
    video = next(
        s for s in info.get("streams", []) if s.get("codec_type") == "video"
    )
    side_data = merge_side_data(
        video, first_frame_side_data(ffprobe, src, nice=nice),
    )
    video_params = svt_params(video, side_data, preset, crf, film_grain)
    if hdr10_svt_params(side_data):
        print(f"hdr10: {src}", file=sys.stderr)
    cmd = build_ffmpeg_cmd(ffmpeg, src, dst, info, video_params)
    if on_progress is not None:
        cmd[1:1] = ["-progress", "pipe:1", "-stats_period", "5"]

    _State.proc = subprocess.Popen(
        cmd,
        stdout=None if on_progress is None else subprocess.PIPE,
        text=True,
        preexec_fn=nice_preexec(nice),
    )
    try:
        if on_progress is not None:
            for block in progress_blocks(_State.proc.stdout):
                on_progress(progress_fields(block))
        return _State.proc.wait()
    finally:
        _State.proc = None


def _passthrough(src: Path, dst: Path, delete_input: bool) -> None:
    """Copy (or move, when delete_input) src to dst without re-encoding,
    using a .part staging file so an interrupted transfer is retried on
    the next scan. Raises OSError if the copy fails."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    partial = partial_path(dst)
    action = "moving" if delete_input else "copying"
    print(f"{action} {src} -> {dst}", file=sys.stderr)
    try:
        shutil.copy2(src, partial)
    except OSError:
        if partial.exists():
            try:
                partial.unlink()
            except OSError:
                pass
        raise
    partial.rename(dst)
    if delete_input:
        try:
            src.unlink()
        except OSError as e:
            print(f"warning: could not delete {src}: {e}", file=sys.stderr)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Watch a directory and batch-encode video to AV1.",
    )
    p.add_argument("input_dir", type=Path)
    p.add_argument("output_dir", type=Path)
    p.add_argument("--crf", type=int, default=DEFAULT_CRF)
    p.add_argument("--preset", type=int, default=DEFAULT_PRESET)
    p.add_argument(
        "--film-grain", type=int, default=DEFAULT_FILM_GRAIN, metavar="N",
        help=(
            f"synthesised film grain, 0-50 (0 = off; dithers away banding in "
            f"dark/smooth scenes at ~no bitrate cost, default {DEFAULT_FILM_GRAIN})"
        ),
    )
    p.add_argument(
        "--interval", type=int, default=DEFAULT_INTERVAL,
        help=f"poll interval in seconds (default {DEFAULT_INTERVAL})",
    )
    p.add_argument(
        "--delete-input", action="store_true",
        help="delete source file after a successful encode",
    )
    p.add_argument(
        "--nice", type=int, default=None, metavar="N",
        help="run ffprobe/ffmpeg with nice increment N (lower priority)",
    )
    p.add_argument(
        "--status-dir", type=Path, default=None, metavar="DIR",
        help="maintain DIR/status.json with the current job and skipped files",
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
    filters.add_argument(
        "--passthrough-filtered", action="store_true",
        help=(
            "copy filtered-out files to the output dir unchanged (preserving "
            "their original extension) instead of ignoring them; combine with "
            "--delete-input to move rather than copy"
        ),
    )

    args = p.parse_args()
    args.skip_codec = {c.lower() for c in (args.skip_codec or [])}

    if not args.input_dir.is_dir():
        sys.exit(f"error: input dir not found: {args.input_dir}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.status_dir is not None:
        args.status_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg = require("ffmpeg")
    ffprobe = require("ffprobe")

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    skipped: dict[Path, tuple[str, float]] = {}
    status = StatusFile(
        args.status_dir, args.input_dir, args.output_dir, skipped,
    )
    prev: dict[Path, int] = {}

    def skip(src: Path, reason: str) -> None:
        skipped[src] = (reason, time.time())

    while not _State.stop:
        status.write()
        current = scan(args.input_dir)
        stable = sorted(p for p, s in current.items() if prev.get(p) == s)
        prev = current

        for src in stable:
            if _State.stop:
                break
            if src in skipped:
                continue
            dst = output_path(src, args.input_dir, args.output_dir)
            pass_dst = passthrough_path(src, args.input_dir, args.output_dir)
            if dst.exists() or pass_dst.exists():
                continue

            def _handle_filtered(
                reason: str, info: dict | None = None,
            ) -> None:
                if not args.passthrough_filtered:
                    print(f"skip {src}: {reason}", file=sys.stderr)
                    skip(src, reason)
                    return
                print(f"passthrough {src}: {reason}", file=sys.stderr)
                status.start("passthrough", src, pass_dst, current[src], info)
                try:
                    _passthrough(src, pass_dst, args.delete_input)
                except OSError as e:
                    print(
                        f"warning: could not copy {src}: {e}", file=sys.stderr,
                    )
                    skip(src, f"could not copy: {e}")
                finally:
                    status.idle()

            if args.min_size is not None and current[src] < args.min_size:
                _handle_filtered(f"size {current[src]} < {args.min_size}")
                continue

            try:
                info = probe(ffprobe, src, nice=args.nice)
            except subprocess.CalledProcessError as e:
                reason = f"ffprobe rc={e.returncode}"
                print(f"skip {src}: {reason}", file=sys.stderr)
                skip(src, reason)
                continue

            reason = _filter_reason(args, info)
            if reason is not None:
                _handle_filtered(reason, info)
                continue

            dst.parent.mkdir(parents=True, exist_ok=True)
            partial = partial_path(dst)
            print(f"encoding {src} -> {dst}", file=sys.stderr)
            status.start("encode", src, dst, current[src], info)
            try:
                rc = _encode(
                    ffmpeg, ffprobe, src, partial, info, args.crf, args.preset,
                    args.film_grain, nice=args.nice,
                    on_progress=status.progress if status.enabled else None,
                )
            except ValueError as e:
                print(f"error: {src}: {e}", file=sys.stderr)
                skip(src, str(e))
                if partial.exists():
                    partial.unlink()
                continue
            finally:
                status.idle()

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
                skip(src, f"ffmpeg rc={rc}")

        if _State.stop:
            break
        for _ in range(args.interval):
            if _State.stop:
                break
            time.sleep(1)

    status.idle()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
