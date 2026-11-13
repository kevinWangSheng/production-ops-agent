"""#604 alert entry through a real web process on a throwaway PostgreSQL.

Opt-in with ``OPSPILOT_LITE_PG=1``: starts its own instance through
``LocalPgLauncher`` (own directory, random port) and the real
``python -m opspilot.web serve`` with the driver's environment and target
entry, then delivers the driver's webhook for a synthetic scenario. No
worker, no model call. Checks that the entry binds one Run to the extended
profile's target with the alert question, and that ``run_freeze.collect``
reads that Run from the real schema.
"""

from __future__ import annotations

import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import run_freeze
from scripts.eval_live_lab import LocalPgLauncher
from scripts.itbench_lite import driver, services
from scripts.itbench_lite.alerts import INTAKE_PATH, EntryTarget, entry_webhook
from scripts.itbench_lite.shift import compute_offset_seconds
from scripts.itbench_lite.snapshot import load_snapshot
from tests.test_itbench_lite_driver import make_scenario

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_LITE_PG") != "1",
    reason="opt-in throwaway PostgreSQL: OPSPILOT_LITE_PG=1",
)


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.parametrize(
    "labels", [{"service_name": "checkout"}, {"namespace": "otel-demo"}]
)
def test_driver_webhook_binds_one_run_on_the_real_intake(tmp_path: Path, labels):
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    make_scenario(dataset, labels=labels, answer_mode=0o600)
    scenario = driver.prepare_scenario(dataset, "Scenario-16", tmp_path / "round")
    cfg = driver.Config(
        round="t604",
        dataset=dataset,
        ground_truth=dataset,
        ports=driver.Ports(pg=_port(), web=_port()),
    )
    launcher = LocalPgLauncher()
    pg = launcher.start(tmp_path / "attempt" / "pg", cfg.ports.pg)
    web = None
    try:
        identities = tmp_path / "identities.json"
        driver.write_identities(identities, scenario)
        token = "synthetic-604-token"
        web = services.start_web(
            driver.web_env(cfg, pg, identities, token),
            cfg.ports.web,
            tmp_path / "web.log",
        )
        delivery = datetime.now(UTC) + timedelta(seconds=1)
        offset = compute_offset_seconds(scenario.ends, delivery)
        body = entry_webhook(
            load_snapshot(scenario.copy),
            offset_seconds=offset,
            target=EntryTarget(match_labels=scenario.plan.match_labels),
        )
        status, reply = driver.deliver(
            f"http://127.0.0.1:{cfg.ports.web}{INTAKE_PATH}", token, body
        )
        assert status == 200, reply
        result = reply["results"][0]
        assert result["outcome"] == "created", result
        block, ledger = run_freeze.collect(pg.owner_dsn, result["run_id"])
        assert block["run"]["bound_target_id"] == driver.TARGET_ID
        assert block["run"]["state"] == "queued"
        assert block["report_present"] is False
        question = block["run"]["question"]
        assert "RequestErrorRate" in question
        workload = labels.get("service_name") or labels["namespace"]
        assert f"otel-demo/{workload}" in question
        assert block["versions"]["tool_schema_revision"]
        assert [d["outcome"] for d in block["alert_deliveries"]] == ["created"]
        # F3 collector fields (review #606 item 7).
        assert block["alert_delivery_outcomes"] == ["created"]
        assert [r["run_id"] for r in block["incident_runs"]] == [result["run_id"]]
        assert block["investigation_evidence"]["incident_id"] == block["incident_id"]
        assert block["audit"]["entries"], "audit timeline is frozen"
        assert block["cited_evidence"] == []
        # A queued Run has no final durable outcome yet: recorded as such.
        assert block["outcome"] == {
            "available": False,
            "reason": "UNKNOWN_DURABLE_STATE",
        }
        _check_outcome_follows_the_frozen_run(pg.owner_dsn, result["run_id"])
    finally:
        if web is not None:
            web.stop()
        launcher.stop(pg)
    assert not services.port_in_use(cfg.ports.web)
    assert not services.port_in_use(cfg.ports.pg)


def _check_outcome_follows_the_frozen_run(owner_dsn: str, run_a: str) -> None:
    """Review #606 r2: the incident's current Run moves from A to B (a copy of
    A's row, test-only rows on the throwaway database); freezing A must not
    report B's outcome, and freezing B reports B's."""
    import uuid

    import psycopg

    run_b = str(uuid.uuid4())
    with psycopg.connect(owner_dsn, autocommit=True) as conn:
        conn.execute(
            "CREATE TEMP TABLE r AS SELECT * FROM opspilot_runs WHERE run_id=%s",
            (run_a,),
        )
        conn.execute(
            "UPDATE r SET run_id=%s, state='cancelled',"
            " control_generation=control_generation+1",
            (run_b,),
        )
        conn.execute("INSERT INTO opspilot_runs SELECT * FROM r")
        conn.execute(
            "UPDATE opspilot_incidents SET current_run_id=%s,"
            " control_generation=control_generation+1"
            " WHERE incident_id=(SELECT incident_id FROM r)",
            (run_b,),
        )
    a, _ = run_freeze.collect(owner_dsn, run_a)
    assert a["run_id"] == run_a
    assert a["outcome"]["available"] is False
    assert a["outcome"]["reason"] == "RUN_NOT_CURRENT"
    assert a["outcome"]["current_run_id"] == run_b
    b, _ = run_freeze.collect(owner_dsn, run_b)
    assert b["outcome"]["available"] is True, b["outcome"]
    assert b["outcome"]["run_id"] == run_b
    assert b["outcome"]["final_state"] == "cancelled"
    assert b["run"]["state"] == "cancelled"
