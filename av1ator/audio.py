"""Audio stream encoding decisions."""

from __future__ import annotations

AUDIO_PASSTHROUGH = {"opus"}


def audio_bitrate_for(channels: int) -> str:
    if channels >= 8:
        return "256k"
    if channels >= 6:
        return "192k"
    if channels >= 2:
        return "96k"
    return "48k"


def audio_args(audio_streams: list[dict]) -> list[str]:
    if not audio_streams:
        return ["-an"]
    codecs = {s.get("codec_name") for s in audio_streams}
    if codecs.issubset(AUDIO_PASSTHROUGH):
        return ["-c:a", "copy"]
    max_channels = max((int(s.get("channels") or 2)) for s in audio_streams)
    args = ["-c:a", "libopus", "-b:a", audio_bitrate_for(max_channels)]
    if max_channels > 2:
        # libopus needs mapping_family=1 for >2 channels, and only accepts a
        # fixed set of canonical layouts — variants like "5.1(side)" are
        # rejected, so we normalise via aformat.
        args += [
            "-mapping_family", "1",
            "-af",
            "aformat=channel_layouts=mono|stereo|3.0|4.0|5.0|5.1|6.1|7.1",
        ]
    return args
