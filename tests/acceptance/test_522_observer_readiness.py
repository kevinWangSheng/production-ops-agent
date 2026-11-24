"""Independent contract tests for #522: Observer ``/readyz`` during a sample.

Contract (B3-U1 option a, B3-R6/R7, F8-R4): while a recovery sample is in
progress the Observer stays ready as long as the sample started no more than
the sample lease (``OBSERVER_LEASE_SECONDS``, 120 s) ago; past that bound it
is 503 ``POLL_STALE``. Outside sampling the old rule holds: a last successful
poll older than 3x the poll interval is stale. No real sleeps: the clock is
controlled.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from opspilot import health
from opspilot.observation.store import OBSERVER_LEASE_SECONDS, SampleLease

POLL = 5.0  # Observer default poll interval; stale window = 3 x 5 = 15 s


# -- (1) poll_readiness -----------------------------------------------------------


def _ready(**kwargs):
    base = {
        "last_success_monotonic": 100.0,
        "poll_interval_seconds": POLL,
        "stopping": False,
    }
    base.update(kwargs)
    return health.poll_readiness(**base)


def test_lease_bound_is_120_seconds():
    assert OBSERVER_LEASE_SECONDS == 120


def test_in_progress_sample_within_bound_keeps_readiness_past_stale_window():
    # last poll 40 s ago (> 15 s), sample started 30 s ago (< 120 s): ready
    result = _ready(
        now_monotonic=140.0,
        in_progress_since=110.0,
        in_progress_bound=OBSERVER_LEASE_SECONDS,
    )
    assert (result.ready, result.reason) == (True, None)


def test_in_progress_sample_with_no_prior_success_is_ready_within_bound():
    # the first pass's first sample: nothing recorded yet, but sampling
    result = _ready(
        last_success_monotonic=None,
        now_monotonic=130.0,
        in_progress_since=110.0,
        in_progress_bound=OBSERVER_LEASE_SECONDS,
    )
    assert result.ready


def test_in_progress_sample_past_bound_is_poll_stale():
    result = _ready(
        now_monotonic=110.0 + OBSERVER_LEASE_SECONDS + 1,
        in_progress_since=110.0,
        in_progress_bound=OBSERVER_LEASE_SECONDS,
    )
    assert (result.ready, result.reason) == (False, health.POLL_STALE)


def test_without_in_progress_sample_old_rule_holds():
    assert _ready(now_monotonic=114.0).ready  # 14 s <= 15 s
    stale = _ready(now_monotonic=116.0)  # 16 s > 15 s
    assert (stale.ready, stale.reason) == (False, health.POLL_STALE)
    explicit_none = _ready(
        now_monotonic=116.0, in_progress_since=None, in_progress_bound=None
    )
    assert (explicit_none.ready, explicit_none.reason) == (False, health.POLL_STALE)


def test_stopping_wins_over_in_progress_sample():
    result = _ready(
        now_monotonic=120.0,
        in_progress_since=110.0,
        in_progress_bound=OBSERVER_LEASE_SECONDS,
        stopping=True,
    )
    assert (result.ready, result.reason) == (False, health.STOPPING)


# -- (2) Observer process: a sample longer than the stale window ---------------------


class _Clock:
    def __init__(self) -> None:
        self._base = time.monotonic()
        self.offset = 0.0
        self._real = time.monotonic

    def __call__(self) -> float:
        return self._real() - self._base + 1000.0 + self.offset


class _Conn:
    def execute(self, *_args, **_kwargs):
        class _Rows:
            def fetchone(self):
                return {"due": 1}

        return _Rows()


class _Store:
    """Just what one pass of the loop and the health record touch."""

    def __init__(self, lease: SampleLease) -> None:
        self._leases = [lease]

    def sweep_expired_sessions(self, *, limit):
        return []

    def claim_due_samples(self, owner, *, limit, lease_seconds):
        leases, self._leases = self._leases, []
        return leases

    @contextmanager
    def transaction(self, **_kwargs):
        yield _Conn()


def _lease(owner) -> SampleLease:
    now = datetime.now(UTC)
    return SampleLease(
        session_id=uuid4(),
        incident_id=uuid4(),
        job_id=uuid4(),
        sequence=1,
        owner=owner,
        epoch=1,
        lease_until=now + timedelta(seconds=OBSERVER_LEASE_SECONDS),
        subject_control_generation=1,
        observation_generation=1,
        health_profile_revision="rev",
        deadline_at=now + timedelta(hours=1),
        sample_interval_seconds=60,
        adopted_window_end=None,
    )


def _readyz(port: int) -> tuple[int, dict]:
    request = urllib.request.Request(f"http://127.0.0.1:{port}/readyz")
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_observer_readyz_stays_200_during_a_sample_longer_than_stale_window(
    monkeypatch,
):
    from opspilot.observer import __main__ as observer_main
    from opspilot.observer.loop import ObserverLoop

    clock = _Clock()
    monkeypatch.setattr(time, "monotonic", clock)

    entered = threading.Event()
    release = threading.Event()

    class SlowLoop(ObserverLoop):
        # Stands in for a sample against a slow Prometheus: the sample is
        # in progress until the test releases it.
        def sample(self, lease):
            entered.set()
            assert release.wait(10), "test never released the sample"
            return "receipt"

    owner = uuid4()
    loop = SlowLoop(store=_Store(_lease(owner)), source=object(), owner=owner)
    loop.poll_seconds = POLL
    server = observer_main._start_health(loop, {health.BIND_ENV: "127.0.0.1:0"})
    assert server is not None
    worker = None
    try:
        # one completed pass: ready
        assert loop.on_poll is not None
        loop.on_poll(True)
        assert _readyz(server.port)[0] == 200

        # outside sampling the old 15 s rule still applies
        clock.offset += 16
        status, body = _readyz(server.port)
        assert (status, body.get("reason")) == (503, "POLL_STALE")

        # a fresh pass, then a sample that runs far past 15 s
        loop.on_poll(True)
        worker = threading.Thread(
            target=lambda: loop.poll_once(compare=False), daemon=True
        )
        worker.start()
        assert entered.wait(5), "the loop never started the sample"
        clock.offset += 25  # 25 s into the sample, > 3 x poll interval
        status, body = _readyz(server.port)
        assert status == 200, body

        clock.offset += OBSERVER_LEASE_SECONDS  # sample now > 120 s old
        status, body = _readyz(server.port)
        assert (status, body.get("reason")) == (503, "POLL_STALE")
    finally:
        release.set()
        if worker is not None:
            worker.join(5)
        server.close()
