from av1ator.colour import colour_mux_args, colour_svt_params


def test_colour_mux_args_full_metadata():
    stream = {
        "color_primaries": "bt2020",
        "color_transfer": "smpte2084",
        "color_space": "bt2020nc",
        "color_range": "tv",
    }
    assert colour_mux_args(stream) == [
        "-color_primaries", "bt2020",
        "-color_trc", "smpte2084",
        "-colorspace", "bt2020nc",
        "-color_range", "tv",
    ]


def test_colour_mux_args_skips_unknown_missing_and_empty():
    stream = {
        "color_primaries": "bt709",
        "color_transfer": "unknown",
        "color_range": "",
    }
    assert colour_mux_args(stream) == ["-color_primaries", "bt709"]


def test_colour_mux_args_empty_stream():
    assert colour_mux_args({}) == []


def test_colour_svt_params_full():
    stream = {
        "color_primaries": "bt2020",
        "color_transfer": "smpte2084",
        "color_space": "bt2020nc",
        "color_range": "tv",
    }
    assert colour_svt_params(stream) == [
        "--color-primaries", "9",
        "--transfer-characteristics", "16",
        "--matrix-coefficients", "9",
        "--color-range", "0",
    ]


def test_colour_svt_params_pc_maps_to_full_range():
    assert colour_svt_params({"color_range": "pc"}) == ["--color-range", "1"]


def test_colour_svt_params_unknown_or_missing_skipped():
    stream = {
        "color_primaries": "weird",
        "color_transfer": "",
        "color_space": None,
        "chroma_location": "center",
    }
    assert colour_svt_params(stream) == []


def test_colour_svt_params_chroma_left():
    assert colour_svt_params({"chroma_location": "left"}) == [
        "--chroma-sample-position", "1",
    ]


def test_colour_svt_params_chroma_topleft():
    assert colour_svt_params({"chroma_location": "topleft"}) == [
        "--chroma-sample-position", "2",
    ]
