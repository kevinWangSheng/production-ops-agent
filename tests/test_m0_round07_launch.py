import pytest

from scripts.m0_lab.round07.launch import validate_max_http


@pytest.mark.parametrize("value", [0, -1, True, 1.0])
def test_round07_launch_rejects_non_positive_max_http(value):
    with pytest.raises(ValueError, match="positive integer"):
        validate_max_http(value)


def test_round07_launch_accepts_positive_max_http():
    assert validate_max_http(1) == 1
    assert validate_max_http(3) == 3
