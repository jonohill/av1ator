from pathlib import Path

import pytest

from av1ator import chunked
from av1ator.dovi import dovi_args
from av1ator.encode import build_ffmpeg_cmd

RPU = {"side_data_type": "Dolby Vision Metadata"}


def _record(profile, compat):
    return {
        "side_data_type": "DOVI configuration record",
        "dv_profile": profile,
        "dv_bl_signal_compatibility_id": compat,
    }


def test_no_dolby_vision():
    assert dovi_args([]) == []


@pytest.mark.parametrize("compat", [1, 2, 4])
def test_profile_8_is_carried(compat):
    assert dovi_args([_record(8, compat), RPU]) == ["-dolbyvision", "1"]


def test_profile_5_raises():
    with pytest.raises(ValueError, match="profile 5"):
        dovi_args([_record(5, 0), RPU])


def test_record_without_rpus_is_ignored():
    assert dovi_args([_record(7, 6)]) == []


def test_ffmpeg_cmd_enables_dolby_vision():
    cmd = build_ffmpeg_cmd(
        "ffmpeg", Path("in.mkv"), Path("out.mkv"),
        {"streams": [{"codec_type": "video"}]}, [],
        side_data=[_record(8, 1), RPU],
    )
    assert cmd[cmd.index("-dolbyvision") + 1] == "1"


def test_fingerprint_includes_dolby_vision(tmp_path):
    src = tmp_path / "src.mkv"
    src.write_bytes(b"x")
    assert chunked._fingerprint(src, [], []) != chunked._fingerprint(
        src, [], [_record(8, 1), RPU],
    )
