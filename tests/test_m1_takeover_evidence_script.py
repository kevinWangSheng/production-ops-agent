"""``scripts/m1_takeover_evidence.py`` (#127): the "request sent" signal comes
from the HTTP emit boundary, and a takeover that did not hold is never frozen.

No network, database or model: the socket write is a stub and the freeze step
a recording fake.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPSConnection
from pathlib import Path
from types import SimpleNamespace

import pytest

from opspilot.investigation.client import DeepSeekClient
from opspilot.investigation.loop import ModelError
from scripts import m1_takeover_evidence as script

_RUNS = Path(__file__).resolve().parents[1] / "docs/evidence/m1-02-takeover/live-runs"
#: The frozen run whose request preceded the takeover (the evidence #127 cites)
#: and the earlier one in which no request had started when the takeover landed.
HELD = _RUNS / "7f1d7572-da37-4967-afcc-21a566acafb8/summary.json"
NO_REQUEST_YET = _RUNS / "770e781b-d547-4953-a343-a8e61152efb3/summary.json"
FROZEN = [HELD] if HELD.exists() else []


class _Clock:
    def now(self) -> datetime:
        return datetime(2026, 10, 8, tzinfo=timezone.utc)


def _post(connection: HTTPConnection, body: bytes) -> None:
    connection.putrequest("POST", "/v1/chat/completions")
    connection.putheader("Content-Length", str(len(body)))
    connection.endheaders(body)


def test_the_signal_fires_only_after_the_bytes_are_written(monkeypatch):
    log: list[str] = []

    def write(self, data):  # the socket write
        log.append("write")

    monkeypatch.setattr(HTTPConnection, "send", write)
    connection = script.signalling_connection(
        HTTPConnection, lambda: log.append("signal")
    )("example.invalid")
    _post(connection, b"{}")
    # headers and body are two writes; the signal follows the first, once
    assert log == ["write", "signal", "write"]
    again = script.signalling_connection(HTTPConnection, lambda: log.append("signal"))(
        "example.invalid"
    )
    _post(again, b"{}")
    assert log.count("signal") == 2  # one per request


def test_a_failed_write_signals_nothing(monkeypatch):
    log: list[str] = []

    def write(self, data):
        raise OSError("socket closed")

    monkeypatch.setattr(HTTPConnection, "send", write)
    connection = script.signalling_connection(
        HTTPConnection, lambda: log.append("signal")
    )("example.invalid")
    with pytest.raises(OSError):
        _post(connection, b"{}")
    assert log == []


def test_the_recorder_counts_requests_at_the_connection_not_at_complete(monkeypatch):
    client = DeepSeekClient("test-key")
    recorder = script.TimestampedRecordingClient(client, _Clock())  # type: ignore[arg-type]
    seen_before_write: list[bool] = []

    def write(self, data):
        seen_before_write.append(recorder.first_request_sent.is_set())

    monkeypatch.setattr(HTTPSConnection, "send", write)
    assert not recorder.first_request_sent.is_set() and recorder.request_starts == []
    connection = client._https._connect("example.invalid")  # type: ignore[union-attr]
    assert client._https.current is connection  # type: ignore[union-attr]
    _post(connection, b"{}")
    assert seen_before_write[0] is False  # not set until the write returned
    assert recorder.first_request_sent.is_set()
    assert len(recorder.request_starts) == 1


def test_entering_complete_does_not_count_a_request_that_was_not_sent(monkeypatch):
    """The regression of #127: the signal was set on entry to ``complete()``,
    before anything reached the socket."""
    client = DeepSeekClient("test-key")
    recorder = script.TimestampedRecordingClient(client, _Clock())  # type: ignore[arg-type]
    seen: dict[str, object] = {}

    def write(self, data):
        seen.setdefault(
            "during_write",
            (recorder.first_request_sent.is_set(), len(recorder.request_starts)),
        )

    monkeypatch.setattr(HTTPSConnection, "send", write)

    def complete(call):
        seen["on_entry"] = (
            recorder.first_request_sent.is_set(),
            len(recorder.request_starts),
        )
        _post(client._https._connect("example.invalid"), b"{}")  # type: ignore[union-attr]
        raise ModelError("MODEL_UNAVAILABLE")

    client.complete = complete  # type: ignore[method-assign]
    with pytest.raises(ModelError):
        recorder.complete(SimpleNamespace(json_mode=False))
    assert seen["on_entry"] == (False, 0)
    assert seen["during_write"] == (False, 0)  # not yet: the write is in progress
    assert recorder.first_request_sent.is_set() and len(recorder.request_starts) == 1


def test_a_client_without_a_connection_hook_is_refused():
    client = DeepSeekClient("test-key", opener=object())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="no HTTPS connection hook"):
        script.TimestampedRecordingClient(client, _Clock())  # type: ignore[arg-type]


def _held() -> tuple[dict, dict]:
    """Verdicts of a takeover that held (shape of the frozen evidence)."""
    summary = json.loads(HELD.read_text())
    return summary["verdicts"], summary["takeover"]


@pytest.mark.skipif(not FROZEN, reason="no frozen takeover evidence")
def test_the_frozen_evidence_has_the_shape_the_required_verdicts_expect():
    """Shape check only: ``7f1d7572…`` was recorded before the wire hook (its
    request time is taken on entry to ``complete()``), so this does not prove
    the socket-write invariant; the script change applies to later runs (#127)
    and the frozen evidence is deliberately left as it is."""
    summary = json.loads(HELD.read_text())
    assert script.failed_verdicts(summary["verdicts"], summary["takeover"]) == []


@pytest.mark.skipif(not NO_REQUEST_YET.exists(), reason="evidence not present")
def test_a_takeover_before_any_request_was_sent_is_not_accepted():
    summary = json.loads(NO_REQUEST_YET.read_text())
    assert script.failed_verdicts(summary["verdicts"], summary["takeover"]) == [
        "model_request_sent_before_takeover"
    ]


@pytest.mark.skipif(not FROZEN, reason="no frozen takeover evidence")
@pytest.mark.parametrize(
    ("name", "mutate"),
    [
        ("takeover_applied", lambda v, t: v.update(takeover_applied=False)),
        (
            "model_request_sent_before_takeover",
            lambda v, t: v.update(model_requests_before_takeover=0),
        ),
        ("run_parked_waiting_human", lambda v, t: v.update(run_state_after="running")),
        ("run_owner_released", lambda v, t: v.update(run_owner_after="set")),
        ("incident_human_owned", lambda v, t: v.update(incident_mode_after="auto")),
        (
            "control_generation_stepped",
            lambda v, t: v.update(control_generation_after=0),
        ),
        (
            "no_model_request_after_takeover_returned",
            lambda v, t: v.update(no_model_request_after_takeover_returned=False),
        ),
        (
            "no_request_sent_after_takeover",
            lambda v, t: v.update(
                request_started_at=v["request_started_at"]
                + ["2026-10-08T00:00:00+00:00"]
            ),
        ),
        (
            "not_claimable_after_takeover",
            lambda v, t: v.update(claimable_after_takeover=True),
        ),
        (
            "second_resume_handed_off",
            lambda v, t: v.update(second_resume={"status": "claimed"}),
        ),
    ],
)
def test_each_required_verdict_failing_is_reported(name, mutate):
    verdicts, takeover = _held()
    mutate(verdicts, takeover)
    assert script.failed_verdicts(verdicts, takeover) == [name]


@pytest.mark.skipif(not FROZEN, reason="no frozen takeover evidence")
def test_a_refused_or_timed_out_takeover_fails_the_run_and_freezes_nothing(capsys):
    frozen: list[str] = []
    refused = {"applied": False, "error": "control refused"}
    verdicts, _ = _held()
    verdicts.update(takeover_applied=False, model_requests_before_takeover=None)
    code = script.settle(verdicts, refused, "10", "9", lambda: frozen.append("x"))  # type: ignore[arg-type, return-value]
    assert code == 1
    assert frozen == []
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    assert "takeover_applied" in report["failed_verdicts"]


@pytest.mark.skipif(not FROZEN, reason="no frozen takeover evidence")
def test_a_held_takeover_is_frozen_and_exits_zero(capsys):
    verdicts, takeover = _held()
    summary_path = script.ROOT / "docs/evidence/x/summary.json"
    code = script.settle(
        verdicts, takeover, "10", "9", lambda: ({"trace": None}, summary_path)
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ok"
