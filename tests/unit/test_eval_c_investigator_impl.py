"""Implementer end-to-end test of the real Investigator wiring (A5/A6, Q1).

Opt-in: needs local PostgreSQL binaries and a free port in
``EVAL_C_PG_PORT``. No lab, model or network: the DeepSeek client speaks to a
scripted opener, the OTel transport to recorded Jaeger/Prometheus bytes, and
the lab (flags, flagd, telemetry) is the offline fake of the driver tests.
The runner, recording, throwaway PostgreSQL and summary writing are real.
"""

from __future__ import annotations

import json
import os
import shutil
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest

from opspilot.evaluation import load_recording, load_round
from opspilot.investigation.loop import ACCEPTED_RESPONSE_MODEL
from opspilot.tools import otel_demo
from scripts import eval_live
from scripts.eval_live_investigator import OtelDemoInvestigator, RoundSession
from scripts.eval_live_lab import LocalPgLauncher, SystemClock
from tests.test_m1_investigation_client import _FixedResponse
from tests.test_m1_otel_demo_contract import CHECKOUT_TRACES, FakeOpener
from tests.unit import test_eval_c_driver_impl as driver

PORT = os.environ.get("EVAL_C_PG_PORT")
pytestmark = pytest.mark.skipif(
    not PORT, reason="explicit opt-in: EVAL_C_PG_PORT=<free port>"
)


class ScriptedDeepSeek:
    """One traces_search call, then a partial report citing its evidence."""

    def __init__(self) -> None:
        self.bodies: list[dict] = []

    def open(self, request, timeout):
        body = json.loads(request.data)
        self.bodies.append(body)
        tools = [m for m in body["messages"] if m["role"] == "tool"]
        if not tools:
            message = {
                "role": "assistant",
                "content": "",
                "reasoning_content": "look at checkout traces",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": otel_demo.TRACES_TOOL,
                            "arguments": json.dumps({"service": "checkout"}),
                        },
                    }
                ],
            }
            finish = "tool_calls"
        else:
            evidence_id = json.loads(tools[-1]["content"])["evidence_id"]
            report = {
                "schema_version": "m0-report-v2",
                "assessment_status": "completed",
                "conclusion": "partial",
                "summary": "Checkout traces were read; no cause is established.",
                "claims": [
                    {
                        "kind": "hypothesis",
                        "text": "The traces may show the failing dependency.",
                        "evidence_ids": [evidence_id],
                        "target_refs": [],
                        "time_scope_ref": None,
                    }
                ],
                "gaps": ["Only one trace page was read."],
                "next_steps": ["Read the payment traces."],
            }
            message = {
                "role": "assistant",
                "content": json.dumps(report),
                "reasoning_content": "done",
            }
            finish = "stop"
        payload = {
            "id": f"resp-{len(self.bodies)}",
            "model": ACCEPTED_RESPONSE_MODEL,
            "choices": [{"message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        return _FixedResponse(json.dumps(payload).encode())


def test_a_real_recorded_run_is_ran_and_reloads(tmp_path: Path, monkeypatch, request):
    from opspilot.investigation.client import DeepSeekClient

    otel = FakeOpener(routes={"/api/traces": CHECKOUT_TRACES})
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: otel)
    monkeypatch.delenv("OPSPILOT_TRACE", raising=False)
    deepseek = ScriptedDeepSeek()
    session = RoundSession("synthetic-key", None, otel_demo.OtelDemoConfig())
    investigator = OtelDemoInvestigator(
        session,
        SystemClock(),
        client_factory=lambda key, clock: DeepSeekClient(
            key, clock=clock, opener=deepseek
        ),
    )
    env = driver.make_env(tmp_path)  # the offline fake lab
    deps = env["deps"]
    # B2: a non-synthetic package must live under this worktree's tmp/eval/.
    recordings = Path(eval_live.ROOT) / "tmp/eval"
    deps = replace(
        deps,
        investigator=investigator,
        pg=LocalPgLauncher(),
        recording_root=recordings,
    )
    slots = eval_live.plan_slots(driver.SCENARIOS, ["eval-normal-1"], 1)
    manifest = eval_live.build_manifest(
        "r1", slots, 1, ["test"], scenarios_root=driver.SCENARIOS, config=session.config
    )
    cfg = eval_live.RoundConfig("r1", slots, "lead", "b1", int(PORT), manifest, ())
    result = eval_live.run_round(cfg, deps)
    slot = result.slots[0]
    created = eval_live.recording_dir(deps, "r1", str(slot.attempt_id))
    request.addfinalizer(
        lambda: (
            os.environ.get("EVAL_C_KEEP") or shutil.rmtree(created, ignore_errors=True)
        )
    )
    assert (slot.status, slot.reason, result.exit_code) == ("ran", None, 0), slot
    assert len(deepseek.bodies) == 2
    stored = json.loads(
        (deps.evidence_root / "r1/runs" / slot.attempt_id / "summary.json").read_text()
    )
    capture = stored["capture"]
    assert capture["attempt"]["status"] == "ran"
    assert capture["recording_ref"]["path"] == "recording.json"
    assert capture["usage"]["epoch"] == 1
    delivered = capture["delivered"]
    assert len(delivered) == 1 and next(iter(delivered.values()))["delivered"] is True
    recording = load_recording(
        eval_live.recording_dir(deps, "r1", slot.attempt_id) / "recording.json",
        expected_hash=capture["recording_ref"]["sha256"],
    )
    assert not recording.context.synthetic and recording.complete
    kinds = [event.kind for event in recording.events]
    assert kinds.count("model") == kinds.count("http") == 2
    scope = recording.context.tool_snapshots[0].scope
    assert scope.run_id == slot.product_run_id
    assert scope.scope_id == f"otel-demo:{slot.product_run_id}:1"
    resolve = eval_live._round_resolver(deps, "r1", [slot.attempt_id])
    aggregate = load_round(deps.evidence_root / "r1/aggregate.json", resolve=resolve)
    assert aggregate.rows[0].attempted == 1
    # The PG instance this attempt started is stopped again.
    pid = next((deps.pg_root / "r1").rglob("postmaster.pid"), None)
    assert pid is None
    # r9: the real runner's recording passes the zero-model structural check.
    from scripts.eval_live_verify import verify_replay

    code, document = verify_replay(
        "r1", evidence_root=deps.evidence_root, recording_root=deps.recording_root
    )
    assert code == 0, document
    (item,) = document["results"]
    assert item["binding_ok"] and item["structural_verified"]
    assert item["event_counts"]["model"] == item["event_counts"]["http"] == 2


def test_real_launcher_rejects_passwordless_connections(tmp_path):
    """r11: SCRAM only; the throwaway PG never trusts a local connection."""
    import psycopg

    launcher = LocalPgLauncher()
    instance = launcher.start(tmp_path / "attempt" / "pg", int(PORT))
    try:
        for user in ("eval_owner", "eval_web", "eval_worker"):
            with pytest.raises(psycopg.OperationalError):
                psycopg.connect(
                    f"host=127.0.0.1 port={PORT} dbname=opspilot_eval user={user}",
                    connect_timeout=5,
                ).close()
        for dsn in (instance.owner_dsn, instance.web_dsn, instance.runtime_dsn):
            with psycopg.connect(dsn, connect_timeout=5) as conn:
                assert conn.execute("SELECT 1").fetchone() == (1,)
        assert not list((tmp_path / "attempt").glob(".pwfile-*"))
    finally:
        launcher.stop(instance)
    assert not (instance.directory / "postmaster.pid").exists()
