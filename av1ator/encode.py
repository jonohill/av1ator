"""SVT-AV1 parameter assembly and full ffmpeg command construction."""

from __future__ import annotations

from pathlib import Path

from av1ator.audio import audio_args
from av1ator.colour import colour_mux_args, colour_svt_params
from av1ator.hdr import hdr10_svt_params

DEFAULT_CRF = 26
DEFAULT_PRESET = 7
DEFAULT_TUNE = 2
DEFAULT_KEYINT = "10s"
DEFAULT_VARIANCE_BOOST_STRENGTH = 2


def svt_params(
    video: dict,
    side_data: list[dict],
    preset: int,
    crf: int,
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


def build_ffmpeg_cmd(
    ffmpeg: str,
    src: Path,
    dst: Path,
    info: dict,
    video_params: list[str],
) -> list[str]:
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ValueError("no video stream found in input")
    audio = [s for s in streams if s.get("codec_type") == "audio"]

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-y",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-c:v",
        "libsvtav1",
        "-pix_fmt",
        "yuv420p10le",
        "-svtav1-params",
        ":".join(_svt_cli_to_ffmpeg_params(video_params)),
    ]
    for i in range(len(audio)):
        cmd += ["-map", f"0:a:{i}"]
    cmd += ["-map", "0:s?", "-c:s", "copy"]
    cmd += audio_args(audio)
    cmd += colour_mux_args(video)
    cmd += [str(dst)]
    return cmd
