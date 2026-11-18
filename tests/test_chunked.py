import hashlib
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from fractions import Fraction
from pathlib import Path

import pytest

from av1ator import chunked
from av1ator.batch import chunked_temp
from av1ator.chunked import encode_chunked, segment_cmd
from av1ator.encode import build_mux_cmd, build_segment_cmd
from av1ator.probe import probe

INFO = {
    "streams": [
        {
            "codec_type": "video", "color_primaries": "bt709",
            "r_frame_rate": "24000/1001", "start_time": "0.000000",
        },
        {"codec_type": "audio", "codec_name": "aac", "channels": 2},
    ],
    "format": {"start_time": "0.000000"},
}

# Emulates the segment muxer: writes segments and appends a line to the
# segment list as each one closes. A segment's content is its frame count,
# for FAKE_FFPROBE to report. One 6.006s segment is 144 frames, and
# FAKE_TORN makes the last segment one short.
FAKE_FFMPEG = """#!/bin/sh
for last; do :; done
case " $* " in
    *" -f concat "*)
        echo mux >> "$FAKE_LOG"
        echo muxed > "$last"
        [ "$FAKE_MUX" = fail ] && exit 4
        exit 0 ;;
esac
echo encode >> "$FAKE_LOG"
echo "$@" > "$FAKE_LOG.encode"
while [ $# -gt 0 ]; do
    case "$1" in
        -segment_list) list="$2"; shift ;;
        -segment_start_number) n="$2"; shift ;;
    esac
    shift
done
dir=$(dirname "$last")
: > "$list"
seg() {
    name=$(printf 'seg%04d.mkv' "$n")
    echo "$3" > "$dir/$name"
    echo "$name,$1,$2" >> "$list"
    n=$((n + 1))
}
case "$FAKE_ENCODE" in
    fail) seg 0.000000 6.006000 144; exit 3 ;;
    signalled) seg 0.000000 6.006000 $((144 - ${FAKE_TORN:-0})); exit 255 ;;
    sleep) touch "$FAKE_LOG.started"; sleep 30 ;;
    killed)
        seg 0.000000 6.006000 144
        seg 6.006000 10.010000 96
        printf 'seg%04d.mkv,10.01' "$n" >> "$list"
        echo half > "$dir/$(printf 'seg%04d.mkv' "$n")"
        kill -KILL $$ ;;
esac
seg 0.000000 6.006000 144
"""

FAKE_FFPROBE = """#!/bin/sh
for last; do :; done
echo ffprobe >> "$FAKE_LOG"
case "$last" in
    *.txt) echo "${FAKE_OUT_FRAMES:-240}" ;;
    */seg*.mkv) cat "$last" ;;
    *) echo "${FAKE_SRC_FRAMES:-240}" ;;
esac
"""


@pytest.fixture(autouse=True)
def reset_state():
    chunked._State.stop = None
    yield
    chunked._State.stop = None


@pytest.fixture
def job(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tools = {}
    for name, body in (("ffmpeg", FAKE_FFMPEG), ("ffprobe", FAKE_FFPROBE)):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(0o755)
        tools[name] = str(path)
    log = tmp_path / "log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    src = tmp_path / "src.mkv"
    src.write_bytes(b"source")

    class Job:
        temp = tmp_path / "temp"
        segments = tmp_path / "temp" / "segments"
        dst = tmp_path / "out.mkv"

        def run(self, params=("--crf", "30"), info=INFO):
            return encode_chunked(
                tools["ffmpeg"], tools["ffprobe"], src, self.dst, info,
                list(params), self.temp,
            )

        def calls(self):
            return log.read_text().split() if log.exists() else []

        def encode_args(self):
            return Path(f"{log}.encode").read_text().split()

    job = Job()
    job.src, job.log, job.tools = src, log, tools
    return job


def test_build_segment_cmd():
    cmd = build_segment_cmd(
        "ffmpeg", Path("/in/a.mkv"), INFO, ["--crf", "30", "--tune", "2"],
        Path("/t/segments"), 1, 300,
    )
    assert cmd == [
        "ffmpeg", "-hide_banner", "-y", "-i", "/in/a.mkv",
        "-vf", "setpts=PTS-STARTPTS",
        "-map", "0:v:0", "-c:v", "libsvtav1", "-pix_fmt", "yuv420p10le",
        "-svtav1-params", "crf=30:tune=2",
        "-color_primaries", "bt709",
        "-fps_mode", "vfr",
        "-f", "segment", "-segment_time", "300", "-reset_timestamps", "1",
        "-segment_start_number", "0",
        "-segment_list", "/t/segments/run1.csv", "-segment_list_type", "csv",
        "/t/segments/seg%04d.mkv",
    ]


def test_build_segment_cmd_resume():
    info = {**INFO, "format": {"start_time": "1.400000"}}
    cmd = build_segment_cmd(
        "ffmpeg", Path("a"), info, [], Path("s"), 3, 300,
        resume_at=42.5, start_number=7,
    )
    assert cmd[3:11] == [
        "-copyts", "-noaccurate_seek", "-ss", "11.100000", "-i", "a",
        "-vf", "trim=start=42.500000,setpts=PTS-STARTPTS",
    ]
    assert cmd[cmd.index("-segment_start_number") + 1] == "7"
    assert cmd[cmd.index("-segment_list") + 1] == "s/run3.csv"


def test_build_mux_cmd():
    cmd = build_mux_cmd(
        "ffmpeg", Path("/t/concat.txt"), Path("/in/a.mkv"), Path("/out/a.mkv"),
        INFO,
    )
    assert cmd == [
        "ffmpeg", "-hide_banner", "-y",
        "-f", "concat", "-safe", "0", "-i", "/t/concat.txt",
        "-i", "/in/a.mkv",
        "-map", "0:v:0", "-c:v", "copy", "-disposition:v:0", "default",
        "-map", "1:a:0",
        "-map", "1:s?", "-c:s", "copy",
        "-c:a", "libopus", "-b:a", "96k",
        "-color_primaries", "bt709",
        "/out/a.mkv",
    ]


def test_build_mux_cmd_restores_video_start_offset():
    info = {
        **INFO,
        "streams": [{**INFO["streams"][0], "start_time": "1.485000"}],
        "format": {"start_time": "1.400000"},
    }
    cmd = build_mux_cmd("ffmpeg", Path("v"), Path("s"), Path("d"), info)
    assert cmd[3:7] == ["-itsoffset", "0.085000", "-f", "concat"]


def test_build_mux_cmd_copies_video_disposition():
    video = {**INFO["streams"][0], "disposition": {"default": 0, "forced": 1}}
    info = {**INFO, "streams": [video]}
    cmd = build_mux_cmd("ffmpeg", Path("v"), Path("s"), Path("d"), info)
    assert cmd[cmd.index("-disposition:v:0") + 1] == "forced"

    video["disposition"] = {"default": 0}
    cmd = build_mux_cmd("ffmpeg", Path("v"), Path("s"), Path("d"), info)
    assert cmd[cmd.index("-disposition:v:0") + 1] == "0"


def _audio_maps(cmd):
    return [
        cmd[i + 1] for i, a in enumerate(cmd)
        if a == "-map" and "a:" in cmd[i + 1]
    ]


def test_build_mux_cmd_without_audio():
    info = {"streams": [{"codec_type": "video"}]}
    cmd = build_mux_cmd("ffmpeg", Path("v"), Path("s"), Path("d"), info)
    assert _audio_maps(cmd) == []
    assert "-an" in cmd


def test_build_mux_cmd_two_audio_streams():
    info = {
        "streams": [
            {"codec_type": "audio", "codec_name": "opus", "channels": 2},
            {"codec_type": "video"},
            {"codec_type": "audio", "codec_name": "opus", "channels": 6},
        ],
    }
    cmd = build_mux_cmd("ffmpeg", Path("v"), Path("s"), Path("d"), info)
    assert _audio_maps(cmd) == ["1:a:0", "1:a:1"]
    assert cmd[cmd.index("-c:a") + 1] == "copy"


def test_build_mux_cmd_raises_without_video():
    with pytest.raises(ValueError, match="no video stream"):
        build_mux_cmd(
            "ffmpeg", Path("v"), Path("s"), Path("d"),
            {"streams": [{"codec_type": "audio"}]},
        )


def test_read_progress(tmp_path):
    (tmp_path / "run2.csv").write_text(
        "seg0002.mkv,0.000000,4.004000\n"
        "seg0003.mkv,4.004000,4.004000\n",
    )
    (tmp_path / "run10.csv").write_text(
        "seg0004.mkv,0.000000,6.006000\nseg0005.mkv,6.0",
    )
    (tmp_path / "run1.csv").write_text(
        "seg0000.mkv,0.000000,6.006000\nseg0001.mkv,6.006000,10.010000\n",
    )
    progress = chunked._read_progress(tmp_path, Fraction(24000, 1001))
    assert [s.name for s in progress.segments] == [
        "seg0000.mkv", "seg0001.mkv", "seg0002.mkv", "seg0004.mkv",
    ]
    assert [s.frames for s in progress.segments] == [144, 96, 96, 144]
    assert progress.runs == 10
    assert progress.next_index == 5
    assert not progress.complete


def test_success_deletes_temp(job):
    assert job.run() == (0, None)
    assert job.dst.read_text() == "muxed\n"
    assert not job.temp.exists()
    assert job.calls() == ["encode", "mux"]
    assert "-ss" not in job.encode_args()


def test_encoder_failure_deletes_temp(job, monkeypatch):
    monkeypatch.setenv("FAKE_ENCODE", "fail")
    assert job.run() == (3, "ffmpeg")
    assert not job.temp.exists()
    assert job.calls() == ["encode"]


def test_signalled_encoder_keeps_temp(job, monkeypatch):
    monkeypatch.setenv("FAKE_ENCODE", "signalled")
    assert job.run() == (255, "ffmpeg")
    assert (job.segments / "seg0000.mkv").exists()
    assert job.calls() == ["encode"]


def test_forwarded_signal_keeps_temp(job, monkeypatch):
    monkeypatch.setenv("FAKE_ENCODE", "sleep")
    result = []
    thread = threading.Thread(target=lambda: result.append(job.run()))
    thread.start()
    started = Path(f"{job.log}.started")
    deadline = time.monotonic() + 5
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    chunked.forward_signal(signal.SIGTERM)
    thread.join(5)
    assert result == [(128 + signal.SIGTERM, None)]
    assert (job.temp / "job.json").exists()
    assert job.calls() == ["encode"]


def test_stop_before_start_runs_nothing(job):
    chunked.forward_signal(signal.SIGTERM)
    assert job.run() == (128 + signal.SIGTERM, None)
    assert job.calls() == []
    assert not job.dst.exists()


def test_hard_kill_resumes_after_last_listed_segment(job, monkeypatch):
    monkeypatch.setenv("FAKE_ENCODE", "killed")
    assert job.run() == (128 + signal.SIGKILL, "ffmpeg")
    assert (job.segments / "seg0002.mkv").exists()

    monkeypatch.delenv("FAKE_ENCODE")
    assert job.run() == (0, None)
    args = job.encode_args()
    assert args[args.index("-ss") + 1] == "0.000000"
    assert args[args.index("-vf") + 1].startswith(
        f"trim=start={239.5 * 1001 / 24000:.6f},",
    )
    assert args[args.index("-segment_start_number") + 1] == "2"
    assert args[args.index("-segment_list") + 1].endswith("run2.csv")
    assert job.calls() == [
        "encode", "ffprobe", "encode", "ffprobe", "ffprobe", "mux",
    ]


def test_resume_deletes_unlisted_segments(job, monkeypatch):
    monkeypatch.setenv("FAKE_ENCODE", "killed")
    job.run()
    monkeypatch.setenv("FAKE_ENCODE", "sleep")
    thread = threading.Thread(target=job.run)
    thread.start()
    started = Path(f"{job.log}.started")
    deadline = time.monotonic() + 5
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sorted(p.name for p in job.segments.glob("seg*.mkv")) == [
        "seg0000.mkv", "seg0001.mkv",
    ]
    chunked.forward_signal(signal.SIGTERM)
    thread.join(5)


def test_resume_cuts_on_source_timestamps(job, monkeypatch):
    info = {
        **INFO,
        "streams": [{**INFO["streams"][0], "start_time": "1.500000"}],
        "format": {"start_time": "1.400000"},
    }
    monkeypatch.setenv("FAKE_ENCODE", "signalled")
    job.run(info=info)
    monkeypatch.delenv("FAKE_ENCODE")
    cmd = segment_cmd(
        job.tools["ffmpeg"], job.tools["ffprobe"], job.src, info, ["--crf", "30"], job.temp,
    )
    assert cmd[cmd.index("-vf") + 1].startswith(
        f"trim=start={1.5 + 143.5 * 1001 / 24000:.6f},",
    )


def test_torn_segment_is_reencoded(job, monkeypatch):
    monkeypatch.setenv("FAKE_ENCODE", "signalled")
    monkeypatch.setenv("FAKE_TORN", "1")
    job.run()
    monkeypatch.delenv("FAKE_ENCODE")
    monkeypatch.delenv("FAKE_TORN")
    monkeypatch.setenv("FAKE_MUX", "fail")
    job.run()
    args = job.encode_args()
    assert "-ss" not in args
    assert args[args.index("-segment_start_number") + 1] == "0"
    assert (job.segments / "run1.csv").read_text() == ""
    assert (job.segments / "concat.txt").read_text() == "file 'seg0000.mkv'\n"


def test_intact_segment_is_kept(job, monkeypatch):
    monkeypatch.setenv("FAKE_ENCODE", "signalled")
    job.run()
    monkeypatch.delenv("FAKE_ENCODE")
    monkeypatch.setenv("FAKE_MUX", "fail")
    job.run()
    assert "-ss" in job.encode_args()
    assert (job.segments / "run1.csv").read_text().startswith("seg0000.mkv,")


def test_frame_count_mismatch_fails_and_deletes_temp(job, monkeypatch):
    monkeypatch.setenv("FAKE_ENCODE", "signalled")
    job.run()
    monkeypatch.delenv("FAKE_ENCODE")
    monkeypatch.setenv("FAKE_OUT_FRAMES", "239")
    assert job.run() == (1, "verify")
    assert not job.temp.exists()
    assert not job.dst.exists()


def test_single_run_skips_frame_count(job):
    job.run()
    assert "ffprobe" not in job.calls()


def test_mux_failure_keeps_temp_and_skips_encode_on_rerun(job, monkeypatch):
    monkeypatch.setenv("FAKE_MUX", "fail")
    assert job.run() == (4, "ffmpeg")
    assert (job.segments / "complete").exists()
    assert not job.dst.exists()
    assert segment_cmd(
        job.tools["ffmpeg"], job.tools["ffprobe"], job.src, INFO, ["--crf", "30"], job.temp,
    ) is None

    monkeypatch.delenv("FAKE_MUX")
    assert job.run() == (0, None)
    assert job.calls() == ["encode", "mux", "mux"]
    assert not job.temp.exists()


def test_concat_list_names_finished_segments(job, monkeypatch):
    monkeypatch.setenv("FAKE_ENCODE", "signalled")
    job.run()
    monkeypatch.delenv("FAKE_ENCODE")
    monkeypatch.setenv("FAKE_MUX", "fail")
    job.run()
    assert (job.segments / "concat.txt").read_text() == (
        "file 'seg0000.mkv'\nfile 'seg0001.mkv'\n"
    )


def test_changed_settings_wipe_temp(job, monkeypatch):
    monkeypatch.setenv("FAKE_ENCODE", "signalled")
    job.run()
    (job.temp / "stale").write_text("x")
    monkeypatch.delenv("FAKE_ENCODE")
    assert "-ss" not in segment_cmd(
        job.tools["ffmpeg"], job.tools["ffprobe"], job.src, INFO, ["--crf", "20"], job.temp,
    )

    job.run(params=("--crf", "20"))
    assert "-ss" not in job.encode_args()
    fingerprint = json.loads((job.temp / "job.json").read_text()) if (
        job.temp.exists()
    ) else None
    assert fingerprint is None
    assert job.calls() == ["encode", "encode", "mux"]


def test_replaced_source_wipes_temp(job, monkeypatch):
    monkeypatch.setenv("FAKE_ENCODE", "signalled")
    job.run()
    st = job.src.stat()
    os.utime(job.src, ns=(st.st_atime_ns, st.st_mtime_ns + 1))
    monkeypatch.delenv("FAKE_ENCODE")
    job.run()
    assert "-ss" not in job.encode_args()


def test_unknown_frame_rate_raises(job):
    info = {**INFO, "streams": [{"codec_type": "video", "r_frame_rate": "0/0"}]}
    with pytest.raises(ValueError, match="frame rate"):
        job.run(info=info)


def test_chunked_temp_is_per_source():
    root, temp = Path("/in"), Path("/temp")
    a = chunked_temp(Path("/in/Movies/A.mkv"), root, temp)
    assert a.parent == temp
    assert a != chunked_temp(Path("/in/Movies/B.mkv"), root, temp)
    assert a.name == hashlib.sha256(b"Movies/A.mkv").hexdigest()[:16]


def _has_libsvtav1() -> bool:
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        return False
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        capture_output=True, text=True,
    ).stdout
    return "libsvtav1" in out


def _frames(path: Path) -> int:
    return int(subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-count_packets", "-show_entries", "stream=nb_read_packets",
            "-of", "csv=p=0", str(path),
        ],
        check=True, capture_output=True, text=True,
    ).stdout.split()[0])


def _misaligned_frames(a: Path, b: Path, stats: Path) -> int:
    """Frames whose PSNR between a and b shows they aren't the same picture."""
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(a), "-i", str(b), "-lavfi",
            "[0:v]settb=1/24,setpts=N[a];"
            "[1:v]settb=1/24,setpts=N,format=yuv420p10le[b];"
            f"[a][b]psnr=stats_file={stats}",
            "-f", "null", "-",
        ],
        check=True,
    )
    return sum(
        float(line.split("psnr_avg:")[1].split()[0]) < 30
        for line in stats.read_text().splitlines()
    )


def _video_pts(path: Path) -> list[float]:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path),
        ],
        check=True, capture_output=True, text=True,
    ).stdout
    return sorted(float(line.rstrip(",")) for line in out.split())


def _start(path: Path, stream: str) -> float:
    return float(subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", stream,
            "-show_entries", "stream=start_time", "-of", "csv=p=0", str(path),
        ],
        check=True, capture_output=True, text=True,
    ).stdout.split()[0])


def _assert_same_timing(dst: Path, src: Path) -> None:
    """dst's video has no timestamp gaps and starts as far after its audio
    as src's does."""
    pts = _video_pts(dst)
    steps = [b - a for a, b in zip(pts, pts[1:])]
    assert max(steps) - min(steps) < 0.002
    delay = _start(dst, "v:0") - _start(dst, "a:0")
    assert abs(delay - (_start(src, "v:0") - _start(src, "a:0"))) < 0.002


def _delayed_video_source(src: Path, seconds: int, delay: float) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-itsoffset", str(delay),
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24000/1001",
            "-f", "lavfi", "-i", "sine", "-t", str(seconds),
            "-c:v", "libx264", "-g", "24", "-c:a", "aac", str(src),
        ],
        check=True,
    )


@pytest.mark.skipif(not _has_libsvtav1(), reason="needs ffmpeg with libsvtav1")
def test_delayed_video_keeps_timing_across_segments(tmp_path):
    src = tmp_path / "src.mkv"
    _delayed_video_source(src, 10, 0.1)
    info = probe("ffprobe", src)
    dst = tmp_path / "out.mkv"
    result = encode_chunked(
        "ffmpeg", "ffprobe", src, dst, info, ["--preset", "12", "--keyint", "1s"],
        tmp_path / "temp", segment_time=2,
    )
    assert result == (0, None)
    assert _frames(dst) == _frames(src)
    _assert_same_timing(dst, src)


@pytest.mark.skipif(not _has_libsvtav1(), reason="needs ffmpeg with libsvtav1")
@pytest.mark.parametrize(
    "container, offset, delay", [("mkv", 0, 0), ("ts", 10, 0), ("mkv", 0, 0.1)],
)
def test_interrupted_encode_keeps_every_frame(tmp_path, container, offset, delay):
    src = tmp_path / f"src.{container}"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-itsoffset", str(delay),
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24000/1001",
            "-f", "lavfi", "-i", "sine", "-t", "45",
            "-c:v", "libx264", "-g", "24", "-bf", "2", "-c:a", "aac",
            "-output_ts_offset", str(offset), str(src),
        ],
        check=True,
    )
    info = probe("ffprobe", src)
    params = ["--preset", "12", "--keyint", "1s"]
    temp = tmp_path / "temp"
    segments = temp / "segments"

    # A first run cut off mid-segment leaves the same state as a SIGTERM.
    # Stopping past RESUME_PREROLL makes the resume seek the demuxer.
    segments.mkdir(parents=True)
    (temp / "job.json").write_text(
        json.dumps(chunked._fingerprint(src, params, [])),
    )
    first = segment_cmd(
        "ffmpeg", "ffprobe", src, info, params, temp, segment_time=2,
    )
    first[-1:-1] = ["-t", "40.3"]
    subprocess.run(first, check=True, capture_output=True)

    dst = tmp_path / "out.mkv"
    result = encode_chunked(
        "ffmpeg", "ffprobe", src, dst, info, params, temp, segment_time=2,
    )
    assert result == (0, None)
    assert _frames(dst) == _frames(src)
    assert _misaligned_frames(dst, src, tmp_path / "psnr.log") == 0
    _assert_same_timing(dst, src)
    assert not temp.exists()


@pytest.mark.skipif(not _has_libsvtav1(), reason="needs ffmpeg with libsvtav1")
def test_stopped_encode_keeps_every_frame(tmp_path):
    src = tmp_path / "src.mkv"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=24000/1001",
            "-t", "60", "-c:v", "libx264", "-g", "48", "-bf", "3", str(src),
        ],
        check=True,
    )
    info = probe("ffprobe", src)
    params = ["--preset", "8", "--keyint", "1s"]
    temp = tmp_path / "temp"
    dst = tmp_path / "out.mkv"

    def run():
        return encode_chunked(
            "ffmpeg", "ffprobe", src, dst, info, params, temp, segment_time=2,
        )

    result = []
    thread = threading.Thread(target=lambda: result.append(run()))
    thread.start()
    run_list = temp / "segments" / "run1.csv"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if run_list.exists() and run_list.read_text().count("\n") >= 2:
            break
        time.sleep(0.01)
    chunked.forward_signal(signal.SIGTERM)
    thread.join(30)
    assert result == [(128 + signal.SIGTERM, None)]

    chunked._State.stop = None
    assert run() == (0, None)
    assert _frames(dst) == _frames(src)
    assert _misaligned_frames(dst, src, tmp_path / "psnr.log") == 0
