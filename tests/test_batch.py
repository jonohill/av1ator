import json
from pathlib import Path

from av1ator.batch import (
    VIDEO_EXTENSIONS,
    StatusFile,
    progress_blocks,
    progress_fields,
    scan,
    sidecar_owner,
)

PROGRESS_OUTPUT = """\
frame=0
fps=0.00
stream_0_0_q=0.0
bitrate=N/A
total_size=N/A
out_time_us=N/A
out_time_ms=N/A
out_time=N/A
dup_frames=0
drop_frames=0
speed=N/A
progress=continue
frame=48
fps=2.70
stream_0_0_q=35.0
bitrate= 773.8kbits/s
total_size=189417
out_time_us=1958333
out_time_ms=1958333
out_time=00:00:01.958333
dup_frames=0
drop_frames=0
speed=  51x
progress=end
"""


def test_progress_blocks_splits_on_progress_key():
    blocks = list(progress_blocks(PROGRESS_OUTPUT.splitlines(keepends=True)))
    assert [b["progress"] for b in blocks] == ["continue", "end"]
    assert blocks[1]["frame"] == "48"
    assert blocks[1]["bitrate"] == "773.8kbits/s"


def test_progress_blocks_drops_incomplete_trailing_block():
    assert list(progress_blocks(["frame=1\n", "fps=2.0\n"])) == []


def test_progress_fields_parses_values():
    blocks = list(progress_blocks(PROGRESS_OUTPUT.splitlines()))
    assert progress_fields(blocks[1]) == {
        "position_seconds": 1.958333,
        "fps": 2.7,
        "speed": 51.0,
        "output_bytes": 189417,
    }


def test_progress_fields_omits_na_values():
    blocks = list(progress_blocks(PROGRESS_OUTPUT.splitlines()))
    assert progress_fields(blocks[0]) == {"fps": 0.0}


def test_progress_fields_fractional_speed():
    assert progress_fields({"speed": "0.111x"}) == {"speed": 0.111}


def _status(tmp_path: Path, skipped=None) -> tuple[StatusFile, Path, Path]:
    in_root, out_root = tmp_path / "in", tmp_path / "out"
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    status = StatusFile(
        status_dir, in_root, out_root, {} if skipped is None else skipped,
    )
    return status, in_root, out_root


def _read(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "status" / "status.json").read_text())


def test_status_encode(tmp_path):
    status, in_root, out_root = _status(tmp_path)
    rel = Path("TV Shows/Reacher/Season 02/Reacher - S02E06.mkv")
    info = {"format": {"duration": "3012.500000"}}
    status.start("encode", in_root / rel, out_root / rel, 2731185389, info)

    current = _read(tmp_path)["current"]
    assert current["action"] == "encode"
    assert current["src"] == rel.as_posix()
    assert current["dst"] == rel.as_posix()
    assert current["input_bytes"] == 2731185389
    assert current["duration_seconds"] == 3012.5
    assert isinstance(current["started_at"], float)
    assert "position_seconds" not in current

    status.progress({
        "position_seconds": 1333.2, "fps": 2.7, "speed": 0.111,
        "output_bytes": 170917888,
    })
    doc = _read(tmp_path)
    assert doc["current"]["position_seconds"] == 1333.2
    assert doc["current"]["fps"] == 2.7
    assert doc["current"]["speed"] == 0.111
    assert doc["current"]["output_bytes"] == 170917888
    assert isinstance(doc["updated_at"], float)
    assert doc["skipped"] == []


def test_status_encode_without_probe_duration(tmp_path):
    status, in_root, out_root = _status(tmp_path)
    status.start(
        "encode", in_root / "a.mkv", out_root / "a.mkv", 1, {"format": {}},
    )
    assert "duration_seconds" not in _read(tmp_path)["current"]


def test_status_passthrough(tmp_path):
    status, in_root, out_root = _status(tmp_path)
    status.start("passthrough", in_root / "a.mp4", out_root / "a.mp4", 42)
    assert _read(tmp_path)["current"] == {
        "action": "passthrough",
        "src": "a.mp4",
        "dst": "a.mp4",
        "input_bytes": 42,
        "started_at": _read(tmp_path)["current"]["started_at"],
    }


def test_status_idle(tmp_path):
    status, in_root, out_root = _status(tmp_path)
    status.start("encode", in_root / "a.mkv", out_root / "a.mkv", 1)
    status.idle()
    doc = _read(tmp_path)
    assert doc["current"] is None
    assert not (tmp_path / "status" / "status.json.tmp").exists()


def test_status_skipped(tmp_path):
    skipped = {}
    status, in_root, _ = _status(tmp_path, skipped)
    skipped[in_root / "Movies/Foo (2020)/Foo (2020).mkv"] = (
        "ffmpeg rc=1", 1791700000.0,
    )
    status.write()
    assert _read(tmp_path)["skipped"] == [
        {
            "src": "Movies/Foo (2020)/Foo (2020).mkv",
            "reason": "ffmpeg rc=1",
            "at": 1791700000.0,
        },
    ]


def test_status_disabled_writes_nothing(tmp_path):
    status = StatusFile(None, tmp_path / "in", tmp_path / "out", {})
    assert not status.enabled
    status.start("encode", tmp_path / "in/a.mkv", tmp_path / "out/a.mkv", 1)
    status.progress({"fps": 1.0})
    status.idle()
    assert list(tmp_path.iterdir()) == []


D = Path("/in/Movies/Name (2020)")


def test_sidecar_owner_matches_stem():
    video = D / "Name (2020).mkv"
    assert sidecar_owner(D / "Name (2020).srt", [video]) == video
    assert sidecar_owner(D / "Name (2020).eng.srt", [video]) == video


def test_sidecar_owner_matches_extra_dots():
    video = D / "Name (2020).mkv"
    assert sidecar_owner(D / "Name (2020).en.forced.srt", [video]) == video
    dotted = D / "Show.S01E01.720p.mkv"
    assert sidecar_owner(D / "Show.S01E01.720p.eng.srt", [dotted]) == dotted


def test_sidecar_owner_requires_dot_after_stem():
    video = D / "Name.mkv"
    assert sidecar_owner(D / "Name2.eng.srt", [video]) is None
    assert sidecar_owner(D / "Name - extra.srt", [video]) is None


def test_sidecar_owner_prefers_longest_stem():
    short, long = D / "Name.mkv", D / "Name.Part2.mkv"
    assert sidecar_owner(D / "Name.Part2.eng.srt", [short, long]) == long
    assert sidecar_owner(D / "Name.eng.srt", [short, long]) == short


def test_sidecar_owner_orphan_when_no_video_in_directory():
    assert sidecar_owner(D / "Name.eng.srt", []) is None
    elsewhere = Path("/in/Movies/Other/Name.mkv")
    assert sidecar_owner(D / "Name.eng.srt", [elsewhere]) is None


def test_scan_includes_extra_extensions(tmp_path):
    (tmp_path / "a.mkv").write_bytes(b"x")
    (tmp_path / "a.eng.SRT").write_bytes(b"xy")
    (tmp_path / "a.nfo").write_bytes(b"xyz")
    assert scan(tmp_path) == {tmp_path / "a.mkv": 1}
    assert scan(tmp_path, VIDEO_EXTENSIONS | {".srt"}) == {
        tmp_path / "a.mkv": 1,
        tmp_path / "a.eng.SRT": 2,
    }
