"""Colour metadata mapping for SVT-AV1 and the ffmpeg muxer.

ffprobe reports colour metadata as strings (e.g. "bt709"); svt-av1's CLI
takes H.273 numeric codepoints; the ffmpeg muxer takes the original strings.
Both paths are wired up so the metadata lands in both the AV1 bitstream and
the container."""

from __future__ import annotations

COLOUR_PRIMARIES = {
    "bt709": 1, "bt470m": 4, "bt470bg": 5, "smpte170m": 6, "smpte240m": 7,
    "film": 8, "bt2020": 9, "smpte428": 10, "smpte431": 11, "smpte432": 12,
}
TRANSFER = {
    "bt709": 1, "gamma22": 4, "gamma28": 5, "smpte170m": 6, "smpte240m": 7,
    "linear": 8, "log100": 9, "log316": 10, "iec61966-2-4": 11, "bt1361e": 12,
    "iec61966-2-1": 13, "bt2020-10": 14, "bt2020-12": 15, "smpte2084": 16,
    "smpte428": 17, "arib-std-b67": 18,
}
MATRIX = {
    "bt709": 1, "fcc": 4, "bt470bg": 5, "smpte170m": 6, "smpte240m": 7,
    "ycgco": 8, "bt2020nc": 9, "bt2020c": 10, "smpte2085": 11,
    "chroma-derived-nc": 12, "chroma-derived-c": 13, "ictcp": 14,
}
COLOUR_RANGE = {"tv": 0, "limited": 0, "pc": 1, "full": 1}
CHROMA_SAMPLE_POSITION = {"left": 1, "topleft": 2}


def colour_mux_args(video_stream: dict) -> list[str]:
    """ffmpeg flags that stamp colour metadata at the container level.
    Belt-and-braces — svt-av1 also writes this into the bitstream."""
    args: list[str] = []
    mapping = {
        "color_primaries": "-color_primaries",
        "color_transfer": "-color_trc",
        "color_space": "-colorspace",
        "color_range": "-color_range",
    }
    for key, flag in mapping.items():
        val = video_stream.get(key)
        if val and val != "unknown":
            args += [flag, val]
    return args


def colour_svt_params(video_stream: dict) -> list[str]:
    """svt-av1 CLI args for colour signalling embedded in the AV1 bitstream."""
    args: list[str] = []
    cp = COLOUR_PRIMARIES.get(video_stream.get("color_primaries") or "")
    tc = TRANSFER.get(video_stream.get("color_transfer") or "")
    mc = MATRIX.get(video_stream.get("color_space") or "")
    cr = COLOUR_RANGE.get(video_stream.get("color_range") or "")
    csp = CHROMA_SAMPLE_POSITION.get(video_stream.get("chroma_location") or "")
    if cp is not None:
        args += ["--color-primaries", str(cp)]
    if tc is not None:
        args += ["--transfer-characteristics", str(tc)]
    if mc is not None:
        args += ["--matrix-coefficients", str(mc)]
    if cr is not None:
        args += ["--color-range", str(cr)]
    if csp is not None:
        args += ["--chroma-sample-position", str(csp)]
    return args
