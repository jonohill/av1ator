"""HDR side-data parsing and merging from ffprobe output.

ffprobe surfaces side-data in three different places depending on the
container and the metadata's origin. Merging from all three ensures a
partial entry in one bucket can't shadow a complete one in another."""

from __future__ import annotations


def _to_float(value) -> float | None:
    if value is None:
        return None
    s = str(value)
    if s in ("", "N/A"):
        return None
    if "/" in s:
        num, _, den = s.partition("/")
        try:
            d = int(den)
            return int(num) / d if d else None
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def merge_side_data(
    video_stream: dict, first_frame_side_data: list[dict] | None,
) -> list[dict]:
    """Combine side-data from the three places ffprobe may surface it.

    Stream `side_data_list` covers container boxes (MP4 mdcv/clli, some MKV).
    Stream `coded_side_data` covers codec-level metadata in newer ffmpeg.
    First-frame `side_data_list` is the only path that catches HEVC SEI, AV1
    OBU metadata, and MKV BlockAdditional."""
    merged: list[dict] = []
    merged.extend(video_stream.get("side_data_list") or [])
    merged.extend(video_stream.get("coded_side_data") or [])
    merged.extend(first_frame_side_data or [])
    return merged
