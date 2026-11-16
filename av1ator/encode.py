"""SVT-AV1 parameter assembly and ffmpeg command construction."""

from __future__ import annotations

from pathlib import Path

from av1ator.audio import audio_args
from av1ator.colour import colour_mux_args, colour_svt_params
from av1ator.dovi import dovi_args
from av1ator.hdr import hdr10_svt_params

DEFAULT_CRF = 26
DEFAULT_PRESET = 7
DEFAULT_TUNE = 2
DEFAULT_KEYINT = "10s"
DEFAULT_VARIANCE_BOOST_STRENGTH = 2
DEFAULT_FILM_GRAIN = 8

# Seconds to seek before a resume point. Some demuxers, mpegts among them,
# land after the keyframe before the seek target, and decoding from the next
# keyframe would drop the frames in between.
RESUME_PREROLL = 30


def svt_params(
    video: dict,
    side_data: list[dict],
    preset: int,
    crf: int,
    film_grain: int,
) -> list[str]:
    params = [
        "--tune",
        str(DEFAULT_TUNE),
        "--keyint",
        DEFAULT_KEYINT,
        "--preset",
        str(preset),
        "--input-depth",
        "10",
        "--crf",
        str(crf),
        "--enable-variance-boost",
        "1",
        "--variance-boost-strength",
        str(DEFAULT_VARIANCE_BOOST_STRENGTH),
        "--film-grain",
        str(film_grain),
        "--film-grain-denoise",
        "0",
    ]
    params += colour_svt_params(video)
    params += hdr10_svt_params(side_data)
    return params


def _svt_cli_to_ffmpeg_params(args: list[str]) -> list[str]:
    """Convert svt-av1 CLI args (['--key', 'value', ...]) to ffmpeg's
    -svtav1-params form (['key=value', ...])."""
    if len(args) % 2:
        raise ValueError("expected paired --key value args")
    return [f"{k.lstrip('-')}={v}" for k, v in zip(args[0::2], args[1::2])]


def _video_args(video_params: list[str], side_data: list[dict]) -> list[str]:
    return [
        "-map", "0:v:0",
        "-c:v", "libsvtav1",
        "-pix_fmt", "yuv420p10le",
        "-svtav1-params", ":".join(_svt_cli_to_ffmpeg_params(video_params)),
        *dovi_args(side_data),
    ]


def _video_and_audio(info: dict) -> tuple[dict, list[dict]]:
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError("no video stream found in input")
    return video, [s for s in streams if s.get("codec_type") == "audio"]


def build_ffmpeg_cmd(
    ffmpeg: str,
    src: Path,
    dst: Path,
    info: dict,
    video_params: list[str],
    side_data: list[dict] = (),
) -> list[str]:
    """side_data is the source's merged stream and first frame side data.
    Raises ValueError if src has no video stream or has Dolby Vision that
    can't be carried."""
    video, audio = _video_and_audio(info)

    cmd = [
        ffmpeg, "-hide_banner", "-y", "-i", str(src),
        *_video_args(video_params, side_data),
    ]
    for i in range(len(audio)):
        cmd += ["-map", f"0:a:{i}"]
    cmd += ["-map", "0:s?", "-c:s", "copy"]
    cmd += audio_args(audio)
    cmd += colour_mux_args(video)
    cmd += [str(dst)]
    return cmd


def build_segment_cmd(
    ffmpeg: str,
    src: Path,
    info: dict,
    video_params: list[str],
    segments: Path,
    run: int,
    segment_time: int,
    resume_at: float | None = None,
    start_number: int = 0,
    side_data: list[dict] = (),
) -> list[str]:
    """ffmpeg command that encodes src's video into segments/seg%04d.mkv,
    split at the first keyframe after every segment_time seconds, and appends
    each segment to segments/run<run>.csv once it is closed. resume_at is a
    source video timestamp in seconds, and the encode starts at the first
    frame after it. side_data is as for build_ffmpeg_cmd. Raises ValueError
    if src has no video stream or has Dolby Vision that can't be carried."""
    video, _ = _video_and_audio(info)
    cmd = [ffmpeg, "-hide_banner", "-y"]
    filters = []
    if resume_at is not None:
        # Cutting with trim on the source's own timestamps avoids depending
        # on which start time ffmpeg's seek and -ss count from. setpts
        # restarts the run's timeline at the first frame, so segment list
        # durations count frames.
        file_start = float(info.get("format", {}).get("start_time", 0))
        seek = max(0.0, resume_at - file_start - RESUME_PREROLL)
        cmd += ["-copyts", "-noaccurate_seek", "-ss", f"{seek:.6f}"]
        filters = [
            "-vf", f"trim=start={resume_at:.6f},setpts=PTS-STARTPTS",
        ]
    cmd += [
        "-i", str(src),
        *filters,
        *_video_args(video_params, side_data),
        *colour_mux_args(video),
        # Direct Matroska output defaults to vfr. The segment muxer defaults
        # to cfr, which duplicates frames to fill timestamp gaps.
        "-fps_mode", "vfr",
        "-f", "segment",
        "-segment_time", str(segment_time),
        "-reset_timestamps", "1",
        "-segment_start_number", str(start_number),
        "-segment_list", str(segments / f"run{run}.csv"),
        "-segment_list_type", "csv",
        str(segments / "seg%04d.mkv"),
    ]
    return cmd


def build_mux_cmd(
    ffmpeg: str,
    concat_list: Path,
    src: Path,
    dst: Path,
    info: dict,
) -> list[str]:
    """ffmpeg command that joins the segments named in concat_list and muxes
    them with src's audio and subtitles. Raises ValueError if src has no
    video stream."""
    src_video, audio = _video_and_audio(info)
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list),
        "-i", str(src),
        "-map", "0:v:0",
        "-c:v", "copy",
    ]
    for i in range(len(audio)):
        cmd += ["-map", f"1:a:{i}"]
    cmd += ["-map", "1:s?", "-c:s", "copy"]
    cmd += audio_args(audio)
    cmd += colour_mux_args(src_video)
    cmd += [str(dst)]
    return cmd
