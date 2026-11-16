"""Dolby Vision passthrough for libsvtav1."""

from __future__ import annotations

# Base layer signal compatibility IDs that ffmpeg can derive from the output
# colour tags: 1 is HDR10, 2 is SDR BT.709, 4 is HLG.
_TAGGED_BASE_LAYERS = {1, 2, 4}


def dovi_args(side_data: list[dict]) -> list[str]:
    """libsvtav1 options that carry the source's Dolby Vision RPUs into the
    AV1 output as profile 10. side_data is the merged stream and first frame
    side data. Raises ValueError if the source has Dolby Vision that can't be
    carried, since encoding it without would leave a picture that displays
    wrongly."""
    types = {sd.get("side_data_type") for sd in side_data}
    # Re-encodes can keep the container's configuration record from a Dolby
    # Vision source while the stream has no RPUs.
    if "Dolby Vision Metadata" not in types:
        return []
    record = next(
        (
            sd for sd in side_data
            if sd.get("side_data_type") == "DOVI configuration record"
        ),
        None,
    )
    if record is None:
        return []
    if record.get("dv_bl_signal_compatibility_id") not in _TAGGED_BASE_LAYERS:
        # ffmpeg picks profile 10.0 (IPT base layer, as in profile 5) only
        # from an RPU on the encoder context at init, which the ffmpeg CLI
        # never provides.
        raise ValueError(
            f"Dolby Vision profile {record.get('dv_profile')} with base layer "
            f"compatibility "
            f"{record.get('dv_bl_signal_compatibility_id')} is unsupported",
        )
    # auto needs the same RPU at init, so it never enables from the CLI.
    return ["-dolbyvision", "1"]
