"""HDR10 bitstream metadata for SVT-AV1."""

from __future__ import annotations

from av1ator.sidedata import _to_float


def hdr10_svt_params(side_data: list[dict]) -> list[str]:
    """svt-av1 CLI args for HDR10 mastering display and content light metadata.
    Iterates all candidate side-data entries and picks the first complete one
    for each type, so a partial entry doesn't shadow a complete one."""
    args: list[str] = []

    md_keys = (
        "green_x", "green_y", "blue_x", "blue_y",
        "red_x", "red_y", "white_point_x", "white_point_y",
        "max_luminance", "min_luminance",
    )
    for sd in side_data:
        if sd.get("side_data_type") != "Mastering display metadata":
            continue
        coords = {k: _to_float(sd.get(k)) for k in md_keys}
        if all(v is not None for v in coords.values()):
            args += [
                "--mastering-display",
                f"G({coords['green_x']:.4f},{coords['green_y']:.4f})"
                f"B({coords['blue_x']:.4f},{coords['blue_y']:.4f})"
                f"R({coords['red_x']:.4f},{coords['red_y']:.4f})"
                f"WP({coords['white_point_x']:.4f},{coords['white_point_y']:.4f})"
                f"L({coords['max_luminance']:.4f},{coords['min_luminance']:.4f})",
            ]
            break

    for sd in side_data:
        if sd.get("side_data_type") != "Content light level metadata":
            continue
        mc = _to_float(sd.get("max_content"))
        ma = _to_float(sd.get("max_average"))
        if mc is not None and ma is not None:
            args += ["--content-light", f"{int(mc)},{int(ma)}"]
            break

    return args
