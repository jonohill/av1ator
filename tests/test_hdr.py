from av1ator.hdr import hdr10_svt_params

# BT.2020 / DCI-P3 canonical mastering display values used by most HDR10 sources.
MASTERING_DISPLAY = {
    "side_data_type": "Mastering display metadata",
    "green_x": "13250/50000",
    "green_y": "34500/50000",
    "blue_x": "7500/50000",
    "blue_y": "3000/50000",
    "red_x": "34000/50000",
    "red_y": "16000/50000",
    "white_point_x": "15635/50000",
    "white_point_y": "16450/50000",
    "max_luminance": "10000000/10000",
    "min_luminance": "1/10000",
}

CONTENT_LIGHT = {
    "side_data_type": "Content light level metadata",
    "max_content": 1000,
    "max_average": 400,
}


def test_full_metadata():
    result = hdr10_svt_params([MASTERING_DISPLAY, CONTENT_LIGHT])
    assert result[0] == "--mastering-display"
    md = result[1]
    assert "G(0.2650,0.6900)" in md
    assert "B(0.1500,0.0600)" in md
    assert "R(0.6800,0.3200)" in md
    assert "WP(0.3127,0.3290)" in md
    assert "L(1000.0000,0.0001)" in md
    assert result[2:] == ["--content-light", "1000,400"]


def test_skips_incomplete_mastering_display():
    incomplete = dict(MASTERING_DISPLAY)
    incomplete["max_luminance"] = "N/A"
    assert hdr10_svt_params([incomplete]) == []


def test_picks_first_complete_over_partial():
    partial = dict(MASTERING_DISPLAY, min_luminance="N/A")
    result = hdr10_svt_params([partial, MASTERING_DISPLAY])
    assert result[0] == "--mastering-display"
    assert "L(1000.0000,0.0001)" in result[1]


def test_skips_incomplete_content_light():
    cl_partial = {
        "side_data_type": "Content light level metadata",
        "max_content": 1000,
    }
    assert hdr10_svt_params([cl_partial]) == []


def test_empty_input():
    assert hdr10_svt_params([]) == []


def test_ignores_unrelated_side_data():
    other = {"side_data_type": "Display Matrix", "rotation": 90}
    assert hdr10_svt_params([other]) == []
