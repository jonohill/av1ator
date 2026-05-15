from av1ator.audio import audio_args, audio_bitrate_for


def test_audio_bitrate_tiers():
    assert audio_bitrate_for(8) == "256k"
    assert audio_bitrate_for(7) == "192k"
    assert audio_bitrate_for(6) == "192k"
    assert audio_bitrate_for(2) == "96k"
    assert audio_bitrate_for(1) == "48k"


def test_no_audio_streams_uses_an():
    assert audio_args([]) == ["-an"]


def test_all_opus_passthrough():
    streams = [
        {"codec_name": "opus", "channels": 2},
        {"codec_name": "opus", "channels": 6},
    ]
    assert audio_args(streams) == ["-c:a", "copy"]


def test_mixed_codecs_reencode_with_max_channel_bitrate():
    streams = [
        {"codec_name": "opus", "channels": 2},
        {"codec_name": "ac3", "channels": 6},
    ]
    result = audio_args(streams)
    assert result[:4] == ["-c:a", "libopus", "-b:a", "192k"]
    assert "-mapping_family" in result
    assert "-af" in result


def test_stereo_reencode_no_mapping_family():
    streams = [{"codec_name": "aac", "channels": 2}]
    assert audio_args(streams) == ["-c:a", "libopus", "-b:a", "96k"]


def test_missing_channels_defaults_to_two():
    streams = [{"codec_name": "aac"}]
    assert audio_args(streams) == ["-c:a", "libopus", "-b:a", "96k"]
