"""#432 seams §0/§2 on a real PostgreSQL (implementer test, zero model).

Opt-in ``OPSPILOT_432_PG=1``: a throwaway PostgreSQL per test through the
driver's ``LocalPgLauncher``; the model is a scripted opener (or the #431
always-503 transport fault, which never calls it), the trace read-back is
skipped. Settles what the offline seams left open: the always-503 terminal
projection (§1 row 7) and where the Run's input frame lives
(``opspilot_runs.input``), and checks ``attempt_projection`` against
``check_expectations`` for a no-report Run, an alert Run and a
``TARGET_UNRESOLVED`` handoff.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from opspilot.evaluation import AttemptRecord, manifest_hash
from opspilot.evaluation.expectations import check_expectations
from opspilot.investigation.client import DeepSeekClient
from opspilot.tools.otel_demo import OtelDemoConfig
from scripts import eval_live, eval_live_investigator
from scripts.eval_live_lab import LocalPgLauncher, SystemClock
from tests.integration.test_431_investigator_binding_impl_postgres import Opener
from tests.unit.test_431_injectors_impl import DEV_V2, PAYMENT, SIGNATURE, _scenario

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_432_PG") != "1",
    reason="opt-in throwaway PostgreSQL: OPSPILOT_432_PG=1",
)

CART = {"kind": "flagd", "params": {"flag": "cartFailure", "variant": "on"}}
CANARY = "ops-zz9q41"


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _answer(root: Path, sid: str, **outcome: Any) -> None:
    path = root / "capability" / sid / "answer.json"
    answer = json.loads(path.read_text())
    answer["expected_outcome"].update(outcome)
    path.write_text(json.dumps(answer))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "dev-v2"
    shutil.copytree(DEV_V2 / "regression", root / "regression")
    (root / "capability").mkdir()
    _scenario(
        root,
        "dv2-d00001",
        "question",
        [CART, {"kind": "model_transport", "params": {"mode": "always_503"}}],
        SIGNATURE,
        "model_failure",
    )
    _answer(
        root,
        "dv2-d00001",
        final_states=["failed"],
        decision="handoff",
        handoff_reason="MODEL_UNAVAILABLE",
        canary=CANARY,
    )
    _scenario(
        root,
        "dv2-d00002",
        "alertmanager",
        [PAYMENT, {"kind": "alert_delivery", "params": {"pattern": "single"}}],
        SIGNATURE,
        "wrong_target",
    )
    _answer(
        root,
        "dv2-d00002",
        final_states=["failed"],
        decision="handoff",
        counts={"incidents": 1, "runs": 1, "episodes": 1},
    )
    _scenario(
        root,
        "dv2-d00003",
        "alertmanager",
        [
            {
                "kind": "alert_delivery",
                "params": {"pattern": "unresolved_labels", "namespace": "otel-demo-x"},
            }
        ],
        None,
        "wrong_target",
    )
    _answer(
        root,
        "dv2-d00003",
        final_states=["no_run"],
        decision="handoff",
        handoff_reason="TARGET_UNRESOLVED",
        counts={"incidents": 1, "runs": 0, "episodes": 1},
    )
    return root


def _investigate(tmp_path: Path, root: Path, scenario_id: str, monkeypatch):
    monkeypatch.setattr(eval_live_investigator, "_traces", lambda *a, **k: ())
    monkeypatch.setattr(
        eval_live_investigator.kind_lab, "webhook_token", lambda *a: "synthetic-432"
    )
    config = OtelDemoConfig()
    (slot,) = eval_live.plan_slots(root, [scenario_id], 1)
    manifest = eval_live.build_manifest(
        "t432", (slot,), 1, ["t"], scenarios_root=root, config=config
    )
    opener = Opener()
    session = eval_live_investigator.RoundSession("synthetic-key", None, config)
    clock = SystemClock()
    investigator = eval_live_investigator.OtelDemoInvestigator(
        session,
        clock,
        client_factory=lambda key, c: DeepSeekClient(key, clock=c, opener=opener),
    )
    attempt_id = f"att-{uuid4().hex[:12]}"
    recording_dir = eval_live.ROOT / "tmp/eval" / f"eval-live-t432-{attempt_id}"
    attempt = AttemptRecord(
        attempt_id,
        "t432",
        manifest_hash(manifest),
        scenario_id,
        "2",
        "live",
        1,
        True,
        "unknown",
        False,
        None,
    )
    launcher = LocalPgLauncher()
    pg = launcher.start(tmp_path / "attempt" / "pg", _port())
    try:
        (tmp_path / "ledger").mkdir()
        request = eval_live.InvestigationRequest(
            attempt=attempt,
            spec=slot.spec,
            pg=pg,
            recording_dir=recording_dir,
            ledger_dir=tmp_path / "ledger",
            round_id="t432",
            pre_observation=None,  # type: ignore[arg-type]
            manifest=manifest,
            plan=slot.plan,
            alert_starts_at=clock.now(),
            canary=slot.answer.expected_outcome.canary,  # type: ignore[union-attr]
        )
        result = investigator.investigate(request)
    finally:
        launcher.stop(pg)
        shutil.rmtree(recording_dir, ignore_errors=True)
    return slot, result, opener


def test_always_503_is_a_failed_handoff_with_two_physical_requests(
    tmp_path, root, monkeypatch
):
    slot, result, opener = _investigate(tmp_path, root, "dv2-d00001", monkeypatch)
    projection = result.projection
    assert projection is not None and opener.calls == 0
    assert projection["outcome"] == {
        "final_state": "failed",
        "decision": "handoff",
        "handoff_reasons": ["MODEL_UNAVAILABLE"],
        "report_available": False,
        "evidence_ids": [],
    }
    (run,) = projection["runs"]
    assert run["row_state"] == "waiting_human"
    assert run["model_requests_used"] == 2
    # The input frame lives in opspilot_runs.input: 24 h, bound to the target.
    assert run["bound_target_id"] == "m0-otel-20260909"
    assert run["scope_target_ids"] == ["m0-otel-20260909"]
    assert run["time_policies"][0]["id"] == "policy-window-1"
    assert projection["tool_results"] == []
    assert projection["canary_hits"] == {"report": 0, "step_responses": 0}
    assert projection["writes"] == {"controls": 0, "self_heal_authorizations": 0}
    assert result.intake["model_transport"] == {
        "mode": "always_503",
        "intercepted": 2,
        "forwarded": 0,
    }
    checked = check_expectations(
        slot.answer,
        attempt_status="ran",
        attempt_reason=None,
        projection=projection,
        intake=result.intake,
    )
    assert checked.passed, checked
    assert checked.checks["no_report"] is True
    assert checked.checks["canary_absent"] is True


def test_alert_run_projection_carries_the_alert_frame(tmp_path, root, monkeypatch):
    slot, result, _opener = _investigate(tmp_path, root, "dv2-d00002", monkeypatch)
    projection = result.projection
    assert projection is not None
    (run,) = projection["runs"]
    assert run["time_policies"][0]["anchor_rule"] == "alert_starts_at"
    assert projection["alert"]["handoff"] is False
    assert projection["counts"] == {"incidents": 1, "runs": 1, "episodes": 1}
    # The scripted "{}" reply is not a valid report: a failed handoff.
    assert projection["outcome"]["final_state"] == "failed"
    assert projection["report"] == {
        "parsed": False,
        "assessment_status": None,
        "conclusion": None,
        "gaps_count": None,
        "fact_claims": None,
    }
    checked = check_expectations(
        slot.answer,
        attempt_status="ran",
        attempt_reason=None,
        projection=projection,
        intake=result.intake,
    )
    assert checked.passed, checked


def test_unresolved_target_projection_has_no_run(tmp_path, root, monkeypatch):
    slot, result, _opener = _investigate(tmp_path, root, "dv2-d00003", monkeypatch)
    projection = result.projection
    assert projection is not None and result.product_run_id is None
    assert projection["runs"] == [] and projection["outcome"] is None
    assert projection["alert"]["handoff"] is True
    assert projection["alert"]["handoff_reason"] == "TARGET_UNRESOLVED"
    checked = check_expectations(
        slot.answer,
        attempt_status="ran",
        attempt_reason=None,
        projection=projection,
        intake=result.intake,
    )
    assert checked.passed, checked
    assert checked.checks["scope_unchanged"] is None
