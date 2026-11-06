"""``scripts.soak.lab_io.Database.rows`` against psql output shapes seen on a
real PostgreSQL (da-server, 2026-11-06 rehearsal #497).

``json_agg`` output for two rows was, byte for byte (``od -c``):
``[{"n":1,"s":"a"}, \\n {"n":2,"s":"b"}]`` -- the runner read only the last
line and failed with "Extra data" from the second incident on.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from scripts.soak import lab_io

JSON_AGG_TWO_ROWS = '[{"n":1,"s":"a"}, \n {"n":2,"s":"b"}]\n'
JSONB_AGG_TWO_ROWS = '[{"n": 1, "s": "a"}, {"n": 2, "s": "b"}]\n'


def _db(monkeypatch: pytest.MonkeyPatch, stdout: str, code: int = 0) -> tuple:
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        seen.append(list(cmd))
        return subprocess.CompletedProcess(cmd, code, stdout, "boom" if code else "")

    monkeypatch.setattr(lab_io, "run", fake_run)
    return lab_io.Database(["compose"]), seen


@pytest.mark.parametrize("stdout", [JSON_AGG_TWO_ROWS, JSONB_AGG_TWO_ROWS])
def test_two_rows_parse_whatever_the_line_breaks(
    monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    db, _ = _db(monkeypatch, stdout)
    assert db.rows("SELECT 1") == [{"n": 1, "s": "a"}, {"n": 2, "s": "b"}]


def test_one_and_zero_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    db, _ = _db(monkeypatch, '[{"n": 1}]\n')
    assert db.rows("SELECT 1") == [{"n": 1}]
    db, _ = _db(monkeypatch, "[]\n")
    assert db.rows("SELECT 1") == []


def test_query_is_wrapped_in_single_line_jsonb_and_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, seen = _db(monkeypatch, "[]\n")
    db.rows("SELECT run_id FROM opspilot_runs")
    (cmd,) = seen
    assert "SET default_transaction_read_only = on" in cmd
    assert cmd[-1] == (
        "SELECT coalesce(jsonb_agg(t), '[]'::jsonb) FROM "
        "(SELECT run_id FROM opspilot_runs) t"
    )


@pytest.mark.parametrize(
    "stdout,code",
    [("", 0), ("SET\n", 0), ("[{broken\n", 0), ('{"a": 1}\n', 0), ("[]", 2)],
)
def test_bad_output_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch, stdout: str, code: int
) -> None:
    # callers (record_open_incidents, sizes) catch RuntimeError and log it
    db, _ = _db(monkeypatch, stdout, code)
    with pytest.raises(RuntimeError):
        db.rows("SELECT 1")
