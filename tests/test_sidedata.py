from av1ator.sidedata import _to_float, merge_side_data


def test_to_float_plain_number():
    assert _to_float("1.5") == 1.5
    assert _to_float(2.5) == 2.5
    assert _to_float("42") == 42.0


def test_to_float_fraction():
    assert _to_float("1/2") == 0.5
    assert _to_float("10000000/10000") == 1000.0


def test_to_float_zero_denominator():
    assert _to_float("3/0") is None


def test_to_float_garbage():
    assert _to_float(None) is None
    assert _to_float("") is None
    assert _to_float("N/A") is None
    assert _to_float("abc") is None
    assert _to_float("1/abc") is None


def test_merge_side_data_concatenates_all_three_sources():
    stream = {
        "side_data_list": [{"a": 1}],
        "coded_side_data": [{"b": 2}],
    }
    assert merge_side_data(stream, [{"c": 3}]) == [
        {"a": 1}, {"b": 2}, {"c": 3},
    ]


def test_merge_side_data_handles_missing_or_none():
    assert merge_side_data({}, []) == []
    assert merge_side_data({"side_data_list": None}, None) == []
