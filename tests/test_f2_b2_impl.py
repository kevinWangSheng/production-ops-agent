"""F2 PR-B2 implementer unit tests: where the worker runs the backlog pass.

R-B2-1 / U-B2-1: once per timed poll, after the deadline sweep and before
the claim listing; a refused pass is logged and the poll goes on; no
``backlog`` means no pass. The PostgreSQL side is in
``tests/integration/test_f2_b2_impl_postgres.py``.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from opspilot.investigation.progress import BACKLOG_FULL, announce_backlog_full
from opspilot.persistence import PersistenceError
from opspilot.worker_main import WorkerLoop


class Recorder:
    def __init__(self, *, refuse_backlog: bool = False) -> None:
        self.calls: list[str] = []
        self.parked = (uuid4(), uuid4(), 0)
        self.refuse_backlog = refuse_backlog
        self.appended: list[tuple] = []

    def sweep_expired_runs_with_generations(self, *, incident_id=None, limit=100):
        self.calls.append("sweep")
        return ()

    def sweep_expired_runs(self, *, incident_id=None, limit=100):  # pragma: no cover
        return ()

    def park_backlog(self, *, max_queued: int, limit: int = 100):
        self.calls.append(f"backlog:{max_queued}")
        if self.refuse_backlog:
            raise PersistenceError("TIMEOUT")
        # A real store parks each Run once; the next call finds none.
        return () if self.calls.count(f"backlog:{max_queued}") > 1 else (self.parked,)

    def claimable_incidents(self, *, limit: int = 20) -> tuple[UUID, ...]:
        self.calls.append("list")
        return ()

    def append(self, subject_id, kind, payload):  # pragma: no cover
        raise AssertionError("a park is announced with append_once")

    def append_once(self, subject_id, kind, payload, *, key=None):
        self.appended.append((subject_id, kind, dict(payload), dict(key or {})))
        return len(self.appended)


class Idle:
    def resume(self, incident_id):  # pragma: no cover - nothing is listed
        raise AssertionError


def _loop(recorder: Recorder, **kwargs) -> WorkerLoop:
    return WorkerLoop(
        queue=recorder,
        runner=Idle(),
        sweeper=recorder,
        events=recorder,
        **kwargs,
    )


def test_the_pass_runs_after_the_sweep_and_before_the_listing():
    recorder = Recorder()
    _loop(recorder, backlog=recorder, backlog_max=3).poll_once()
    # One pass repeats the store call until it parks nothing.
    assert recorder.calls == ["sweep", "backlog:3", "backlog:3", "list"]
    [(subject, kind, payload, key)] = recorder.appended
    assert subject == recorder.parked[0] and kind == "run_handoff"
    assert payload["reasons"] == [BACKLOG_FULL]
    assert key == {
        "run_id": str(recorder.parked[1]),
        "parked": True,
        "reconciled": False,
    }


def test_no_backlog_means_no_pass_and_a_refusal_does_not_stop_the_poll():
    recorder = Recorder()
    _loop(recorder).poll_once()
    assert recorder.calls == ["sweep", "list"]
    refusing = Recorder(refuse_backlog=True)
    _loop(refusing, backlog=refusing).poll_once()
    assert refusing.calls == ["sweep", "backlog:8", "list"]
    assert refusing.appended == []


@pytest.mark.parametrize("bad", [0, -1, True, 2.0])
def test_backlog_max_must_be_a_positive_integer(bad):
    recorder = Recorder()
    with pytest.raises(ValueError):
        _loop(recorder, backlog=recorder, backlog_max=bad)


def test_a_renewed_generation_is_part_of_the_announcement_key():
    recorder = Recorder()
    subject, run = uuid4(), uuid4()
    announce_backlog_full(recorder, subject, run, 3)
    [(_, _, payload, key)] = recorder.appended
    assert payload["control_generation"] == 3 and payload["execution"] == "not_started"
    assert key == {
        "run_id": str(run),
        "parked": True,
        "control_generation": 3,
        "reconciled": False,
    }
