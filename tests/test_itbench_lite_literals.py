"""#624: list/dict cells of the ITBench-Lite trace export, including the numpy
reprs the real ``Events.Timestamp`` column uses. Synthetic values only."""

from __future__ import annotations

import pytest

from scripts.itbench_lite.literals import parse_cell


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("[]", []),
        ("{}", {}),
        ("['a', \"b\"]", ["a", "b"]),
        ("[{'k': 'v', 'n': -1}, {}]", [{"k": "v", "n": -1}, {}]),
        ("{'a.b': 1.5, 'c': True, 'd': None}", {"a.b": 1.5, "c": True, "d": None}),
        (
            "[np.datetime64('2025-12-15T17:30:00.200000000')]",
            ["2025-12-15T17:30:00.200000000"],
        ),
        (
            "[np.datetime64('2025-12-15T17:30:00.200000000'), "
            "np.datetime64('2025-12-15T17:30:01.000000001')]",
            ["2025-12-15T17:30:00.200000000", "2025-12-15T17:30:01.000000001"],
        ),
    ],
)
def test_accepted_forms(text, expected):
    assert parse_cell(text, "Col") == expected


@pytest.mark.parametrize(
    "text",
    [
        "[__import__('os').system('true')]",
        "[np.datetime64('2025-12-15T17:30:00', 'ns')]",
        "[np.datetime64(x)]",
        "[np.int64(3)]",
        "[numpy.datetime64('2025-12-15T17:30:00')]",
        "[open('f')]",
        "{**x}",
        "[1 + 2]",
        "[a]",
        "{broken",
    ],
)
def test_other_forms_fail_naming_the_column(text):
    with pytest.raises(ValueError, match="column Events.Timestamp"):
        parse_cell(text, "Events.Timestamp")
