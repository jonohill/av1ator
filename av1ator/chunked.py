"""Resumable segmented encoding: ffmpeg encodes the video into fixed-length
segments in a temp dir, and a rerun continues after the last finished segment.
A second ffmpeg joins the segments and muxes in the source's audio and
subtitles."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import NamedTuple

from av1ator.dovi import dovi_args
from av1ator.encode import build_mux_cmd, build_segment_cmd
from av1ator.probe import nice_preexec

DEFAULT_SEGMENT_TIME = 300

# ffmpeg exits 255 when a signal stops it, after closing the open segment.
_FFMPEG_SIGNALLED = 255


class Result(NamedTuple):
    """rc is non-zero on failure or stop. tool names the step that failed,
    "ffmpeg" or "verify", and is None on success or stop."""

    rc: int
    tool: str | None


class _State:
    stop: int | None = None
    proc: subprocess.Popen | None = None


def forward_signal(signum: int) -> None:
    """Record a stop and send signum to the running step's process group.
    Safe to call from a signal handler."""
    _State.stop = signum
    if _State.proc is not None:
        _killpg(_State.proc, signum)


def _killpg(proc: subprocess.Popen, signum: int) -> None:
    try:
        os.killpg(proc.pid, signum)
    except OSError:
        pass


def _fingerprint(
    src: Path, video_params: list[str], side_data: list[dict],
) -> dict:
    st = src.stat()
    return {
        "dovi": dovi_args(side_data),
        "src": str(src),
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "video_params": video_params,
    }


def _same_job(temp: Path, fingerprint: dict) -> bool:
    try:
        return json.loads((temp / "job.json").read_text()) == fingerprint
    except (OSError, ValueError):
        return False


def _video(info: dict) -> dict:
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            return s
    raise ValueError("no video stream found in input")


def _frame_rate(video: dict) -> Fraction:
    try:
        fps = Fraction(video.get("r_frame_rate", "0/0"))
    except (ValueError, ZeroDivisionError):
        fps = Fraction(0)
    if fps <= 0:
        raise ValueError("--chunked needs a known frame rate")
    return fps


def _video_start(info: dict, video: dict) -> float:
    start = video.get("start_time", info.get("format", {}).get("start_time"))
    return float(start or 0)


class _Segment(NamedTuple):
    run: int
    name: str
    frames: int


class _Progress(NamedTuple):
    runs: int
    segments: list[_Segment]
    complete: bool

    @property
    def frames(self) -> int:
        return sum(s.frames for s in self.segments)

    @property
    def next_index(self) -> int:
        indices = (int(Path(s.name).stem[3:]) for s in self.segments)
        return max(indices, default=-1) + 1


def _run_list(segments: Path, run: int) -> Path:
    return segments / f"run{run}.csv"


def _list_lines(path: Path) -> list[str]:
    # A hard kill can leave a partial last line, so only lines that end in a
    # newline count.
    return path.read_text().split("\n")[:-1]


def _read_progress(segments: Path, fps: Fraction) -> _Progress:
    """Finished segments, in order, from every run's segment list."""
    runs = sorted(int(p.stem[3:]) for p in segments.glob("run*.csv"))
    finished = []
    for run in runs:
        for line in _list_lines(_run_list(segments, run)):
            name, start, end = line.split(",")
            # Each run's timeline starts at its first frame, so a segment's
            # duration gives its frame count. Rounding absorbs Matroska's 1ms
            # timestamps.
            frames = round((float(end) - float(start)) * fps)
            if frames > 0:
                finished.append(_Segment(run, name, frames))
    return _Progress(
        max(runs, default=0), finished, (segments / "complete").exists(),
    )


def _without_torn_segment(
    ffprobe: str, segments: Path, progress: _Progress, nice: int | None,
) -> _Progress:
    """progress without the latest run's last segment if that segment has
    fewer frames than its duration spans.

    A stop closes the open segment, but the decoder can already have output
    frames that follow B-frames it hasn't read yet. The segment then has a
    gap before its last frame, and resuming after it would skip the missing
    frames."""
    if not progress.segments:
        return progress
    last = progress.segments[-1]
    if last.run != progress.runs:
        return progress
    frames = _count_frames(ffprobe, segments / last.name, nice)
    if frames == last.frames or _State.stop is not None:
        return progress
    return progress._replace(segments=progress.segments[:-1])


def _segment_cmd(
    ffmpeg: str, src: Path, info: dict, video_params: list[str],
    side_data: list[dict], segments: Path, segment_time: int,
    progress: _Progress,
) -> list[str]:
    video = _video(info)
    resume_at = None
    if progress.segments:
        # Cut midway between the last finished frame and the next one, so
        # timestamp rounding can't drop or repeat a frame.
        fps = _frame_rate(video)
        resume_at = (
            _video_start(info, video)
            + float((progress.frames - Fraction(1, 2)) / fps)
        )
    return build_segment_cmd(
        ffmpeg, src, info, video_params, segments, progress.runs + 1,
        segment_time, resume_at, progress.next_index, side_data,
    )


def segment_cmd(
    ffmpeg: str, ffprobe: str, src: Path, info: dict, video_params: list[str],
    temp: Path, segment_time: int = DEFAULT_SEGMENT_TIME,
    nice: int | None = None, side_data: list[dict] = (),
) -> list[str] | None:
    """The encode command encode_chunked would run given temp's current
    state, or None if the encode is already complete."""
    segments = temp / "segments"
    if _same_job(temp, _fingerprint(src, video_params, side_data)):
        progress = _read_progress(segments, _frame_rate(_video(info)))
    else:
        progress = _Progress(0, [], False)
    if progress.complete:
        return None
    progress = _without_torn_segment(ffprobe, segments, progress, nice)
    return _segment_cmd(
        ffmpeg, src, info, video_params, side_data, segments, segment_time,
        progress,
    )


def mux_cmd(
    ffmpeg: str, src: Path, dst: Path, info: dict, temp: Path,
) -> list[str]:
    return build_mux_cmd(
        ffmpeg, temp / "segments" / "concat.txt", src, dst, info,
    )


def _run(
    cmd: list[str], nice: int | None, capture: bool = False,
) -> tuple[int | None, str]:
    """Run cmd as its own process group and return its rc and, if capture is
    set, its stdout. rc is None if a stop was requested before it started."""
    if _State.stop is not None:
        return None, ""
    _State.proc = subprocess.Popen(
        cmd, start_new_session=True, preexec_fn=nice_preexec(nice),
        stdout=subprocess.PIPE if capture else None, text=True,
    )
    try:
        # A signal that landed before proc was assigned was not forwarded.
        if _State.stop is not None:
            _killpg(_State.proc, _State.stop)
        out, _ = _State.proc.communicate()
        return _State.proc.returncode, out or ""
    finally:
        _State.proc = None


def _count_frames(
    ffprobe: str, path: Path, nice: int | None, concat: bool = False,
) -> int | None:
    cmd = [ffprobe, "-v", "error"]
    if concat:
        cmd += ["-f", "concat", "-safe", "0"]
    cmd += [
        "-select_streams", "v:0", "-count_packets",
        "-show_entries", "stream=nb_read_packets",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    rc, out = _run(cmd, nice, capture=True)
    try:
        # Streams in an mpegts program are printed twice.
        return int(out.split()[0]) if rc == 0 else None
    except (ValueError, IndexError):
        return None


def _rc(rc: int) -> int:
    return rc if rc > 0 else 128 - rc


def encode_chunked(
    ffmpeg: str, ffprobe: str, src: Path, dst: Path, info: dict,
    video_params: list[str], temp: Path,
    segment_time: int = DEFAULT_SEGMENT_TIME, nice: int | None = None,
    side_data: list[dict] = (),
) -> Result:
    """Encode src to dst, resuming from temp when it holds segments from the
    same job. temp is kept when stopped, when a signal kills ffmpeg, or when
    the mux fails, and deleted otherwise. Raises ValueError if src has no
    video stream, no known frame rate, or Dolby Vision that can't be
    carried. side_data is as for build_ffmpeg_cmd."""
    fps = _frame_rate(_video(info))
    segments = temp / "segments"
    concat = segments / "concat.txt"
    mux = mux_cmd(ffmpeg, src, dst, info, temp)

    fingerprint = _fingerprint(src, video_params, side_data)
    if temp.exists() and not _same_job(temp, fingerprint):
        shutil.rmtree(temp)
    segments.mkdir(parents=True, exist_ok=True)
    (temp / "job.json").write_text(json.dumps(fingerprint))

    progress = _read_progress(segments, fps)
    if not progress.complete:
        trusted = _without_torn_segment(ffprobe, segments, progress, nice)
        if trusted != progress:
            torn = progress.segments[-1]
            run_list = _run_list(segments, torn.run)
            run_list.write_text("".join(
                f"{line}\n" for line in _list_lines(run_list)
                if line.split(",")[0] != torn.name
            ))
            progress = trusted
        listed = {s.name for s in progress.segments}
        for seg in segments.glob("seg*.mkv"):
            if seg.name not in listed:
                seg.unlink()
        rc, _ = _run(_segment_cmd(
            ffmpeg, src, info, video_params, side_data, segments,
            segment_time, progress,
        ), nice)
        if _State.stop is not None:
            return Result(128 + _State.stop, None)
        if rc == _FFMPEG_SIGNALLED or rc < 0:
            return Result(_rc(rc), "ffmpeg")
        if rc > 0:
            shutil.rmtree(temp, ignore_errors=True)
            return Result(rc, "ffmpeg")
        (segments / "complete").touch()
        progress = _read_progress(segments, fps)

    concat.write_text("".join(f"file '{s.name}'\n" for s in progress.segments))

    resumed = sum(1 for _ in segments.glob("run*.csv")) > 1
    if resumed:
        expected = _count_frames(ffprobe, src, nice)
        actual = _count_frames(ffprobe, concat, nice, concat=True)
        if _State.stop is not None:
            return Result(128 + _State.stop, None)
        if expected is None or actual != expected:
            print(
                f"error: {src}: segments have {actual} frames, "
                f"source has {expected}",
                file=sys.stderr,
            )
            shutil.rmtree(temp, ignore_errors=True)
            return Result(1, "verify")

    rc, _ = _run(mux, nice)
    if rc == 0 and _State.stop is None:
        shutil.rmtree(temp, ignore_errors=True)
        return Result(0, None)
    dst.unlink(missing_ok=True)
    if _State.stop is not None:
        return Result(128 + _State.stop, None)
    return Result(_rc(rc), "ffmpeg")
