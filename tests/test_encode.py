from pathlib import Path

import pytest

from av1ator.encode import (
    DEFAULT_FILM_GRAIN,
    DEFAULT_KEYINT,
    DEFAULT_TUNE,
    DEFAULT_VARIANCE_BOOST_STRENGTH,
    _svt_cli_to_ffmpeg_params,
    build_ffmpeg_cmd,
    svt_params,
)


def test_svt_cli_to_ffmpeg_params_joins_pairs():
    assert _svt_cli_to_ffmpeg_params(["--preset", "6", "--crf", "28"]) == [
        "preset=6", "crf=28",
    ]


def test_svt_cli_to_ffmpeg_params_rejects_odd_args():
    with pytest.raises(ValueError):
        _svt_cli_to_ffmpeg_params(["--preset"])


def test_svt_params_includes_defaults_and_user_values():
    result = svt_params({}, [], preset=6, crf=28, film_grain=8)
    assert result == [
        "--tune", str(DEFAULT_TUNE),
        "--keyint", DEFAULT_KEYINT,
        "--preset", "6",
        "--input-depth", "10",
        "--crf", "28",
        "--enable-variance-boost", "1",
        "--variance-boost-strength", str(DEFAULT_VARIANCE_BOOST_STRENGTH),
        "--film-grain", "8",
        "--film-grain-denoise", "0",
    ]


def test_svt_params_film_grain_defaults_to_constant():
    result = svt_params({}, [], preset=6, crf=28, film_grain=DEFAULT_FILM_GRAIN)
    idx = result.index("--film-grain")
    assert result[idx + 1] == str(DEFAULT_FILM_GRAIN)


def test_svt_params_appends_colour_and_hdr():
    video = {"color_primaries": "bt709"}
    result = svt_params(video, [], preset=6, crf=28, film_grain=8)
    assert "--color-primaries" in result
    assert "1" in result


def test_build_ffmpeg_cmd_basic_shape():
    info = {
        "streams": [
            {"codec_type": "video", "codec_name": "h264"},
            {"codec_type": "audio", "codec_name": "aac", "channels": 2},
        ],
    }
    cmd = build_ffmpeg_cmd(
        "/usr/bin/ffmpeg",
        Path("/tmp/in.mkv"),
        Path("/tmp/out.mkv"),
        info,
        ["--preset", "6", "--crf", "28"],
    )
    assert cmd[0] == "/usr/bin/ffmpeg"
    assert cmd[-1] == "/tmp/out.mkv"
    assert "libsvtav1" in cmd
    assert "yuv420p10le" in cmd
    idx = cmd.index("-svtav1-params")
    assert cmd[idx + 1] == "preset=6:crf=28"


def test_build_ffmpeg_cmd_raises_when_no_video_stream():
    info = {"streams": [{"codec_type": "audio"}]}
    with pytest.raises(ValueError, match="no video stream"):
        build_ffmpeg_cmd("ffmpeg", Path("a"), Path("b"), info, [])


def test_build_ffmpeg_cmd_emits_an_when_no_audio():
    info = {"streams": [{"codec_type": "video"}]}
    cmd = build_ffmpeg_cmd(
        "ffmpeg", Path("a"), Path("b"), info, ["--preset", "6"],
    )
    assert "-an" in cmd
    # No -map for audio streams that don't exist
    assert "0:a:0" not in cmd


def test_build_ffmpeg_cmd_maps_multiple_audio_tracks():
    info = {
        "streams": [
            {"codec_type": "video"},
            {"codec_type": "audio", "channels": 2},
            {"codec_type": "audio", "channels": 6},
        ],
    }
    cmd = build_ffmpeg_cmd(
        "ffmpeg", Path("a"), Path("b"), info, ["--preset", "6"],
    )
    assert "0:a:0" in cmd
    assert "0:a:1" in cmd


def test_build_ffmpeg_cmd_includes_colour_mux_args():
    info = {
        "streams": [
            {"codec_type": "video", "color_primaries": "bt2020"},
        ],
    }
    cmd = build_ffmpeg_cmd(
        "ffmpeg", Path("a"), Path("b"), info, ["--preset", "6"],
    )
    assert "-color_primaries" in cmd
    assert "bt2020" in cmd
