"""#431 alert entry on a real PostgreSQL (implementer test, zero model).

Opt-in with ``OPSPILOT_431_PG=1``: each test starts its own throwaway
instance through the driver's ``LocalPgLauncher`` (own directory, random
port), never the shared 55431 lab database. The deliveries go through the
product's own ``/intake/alertmanager`` route in-process; no Run is started.
Checks the counts the seams fix (section 0, re-fired: incidents=1, runs=1,
episodes=2) and that an alert Run's scope policy is the alert template.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from uuid import UUID

import pytest

from opspilot.evaluation.live import scope_policy_hash
from opspilot.investigation.context import InvestigationInput
from opspilot.persistence import DurableStore
from opspilot.tools import otel_demo
from scripts import eval_live, eval_live_inject
from scripts.eval_live_lab import LocalPgLauncher, SystemClock
from tests.unit.test_eval_c_driver_impl import Clock

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_431_PG") != "1",
    reason="opt-in throwaway PostgreSQL: OPSPILOT_431_PG=1",
)


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def pg(tmp_path: Path):
    launcher = LocalPgLauncher()
    instance = launcher.start(tmp_path / "attempt" / "pg", _port())
    try:
        yield instance
    finally:
        launcher.stop(instance)


def _deliver(pg, params):
    web = DurableStore(pg.web_dsn)
    web.install()
    clock = Clock()
    clock.t = SystemClock().now()  # the DB clock judges startsAt vs now
    run = eval_live_inject.alert_entry(
        params,
        web=web,
        face=otel_demo.otel_demo_face(clock),
        versions=otel_demo.otel_demo_versions(),
        clock=clock,
        round_id="t431",
        first_starts_at=clock.now(),
        token="synthetic-431-token",
    )
    return run, eval_live_inject.intake_counts(pg.owner_dsn)


@pytest.mark.parametrize(
    ("params", "counts"),
    [
        ({"pattern": "single"}, {"incidents": 1, "runs": 1, "episodes": 1}),
        (
            {"pattern": "duplicate", "deliveries": 3, "interval_seconds": 2},
            {"incidents": 1, "runs": 1, "episodes": 1},
        ),
        (
            {"pattern": "refire", "interval_seconds": 2},
            {"incidents": 1, "runs": 1, "episodes": 2},
        ),
        (
            {"pattern": "unresolved_labels", "namespace": "otel-demo-x"},
            {"incidents": 1, "runs": 0, "episodes": 1},
        ),
    ],
)
def test_delivery_patterns_commit_the_expected_counts(pg, params, counts):
    run, committed = _deliver(pg, params)
    assert [d.http_status for d in run.deliveries] == [200] * len(run.deliveries)
    assert [d.outcome for d in run.deliveries] == list(
        eval_live_inject.EXPECTED_OUTCOMES[params["pattern"]]
    )
    assert committed == counts
    if params["pattern"] == "unresolved_labels":
        assert run.first.reason == "TARGET_UNRESOLVED" and run.first.run_id is None
    else:
        assert run.first.run_id is not None
        assert len({d.incident_id for d in run.deliveries}) == 1


def test_an_alert_run_has_the_alert_template_scope_policy(pg):
    run, _ = _deliver(pg, {"pattern": "single"})
    store = DurableStore(pg.owner_dsn)
    snapshot = store.rebuild(UUID(run.first.incident_id))
    run_input = InvestigationInput.from_json(snapshot["run"]["input"])
    window = otel_demo.scope_window(run_input)
    default = otel_demo.scope_default_query_window(run_input)
    assert default is not None
    assert (window.end - window.start).total_seconds() == otel_demo.OBSERVATION_SECONDS
    template = eval_live.template_scope(alert=True)
    from dataclasses import replace

    actual = replace(template, window=window, default_query_window=default)
    assert scope_policy_hash(actual) == scope_policy_hash(template)
    assert scope_policy_hash(template) != scope_policy_hash(eval_live.template_scope())
