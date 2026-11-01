"""#431 S2 on a real Run: a v2 attempt's recording binds to its round
manifest (implementer test). Opt-in ``OPSPILOT_431_PG=1``; throwaway
PostgreSQL per test; the model is a scripted opener that never calls a
tool (no lab request), the trace read-back is skipped.

Covers the two binding classes that change the manifest -- the alert entry
(scope policy with a default query window) and an unreachable Jaeger (target
registry) -- plus the first-503 transport fault inside the recorded Run.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
from pathlib import Path
from uuid import uuid4

import pytest

from opspilot.evaluation import AttemptRecord, load_recording, manifest_hash
from opspilot.evaluation.live import check_recording_binding
from opspilot.investigation.client import DeepSeekClient
from opspilot.tools.otel_demo import OtelDemoConfig
from scripts import eval_live, eval_live_investigator
from scripts.eval_live_lab import LocalPgLauncher, SystemClock
from tests.unit.test_431_injectors_impl import DEV_V2, PAYMENT, SIGNATURE, _scenario

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_431_PG") != "1",
    reason="opt-in throwaway PostgreSQL: OPSPILOT_431_PG=1",
)

REPLY = json.dumps(
    {
        "id": "scripted",
        "model": "deepseek-flash",
        "choices": [
            {
                "message": {"role": "assistant", "content": "{}"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
).encode()


class Response:
    status = 200

    def __init__(self) -> None:
        self._body = REPLY
        self.headers = {"Content-Length": str(len(REPLY))}

    def read(self, n: int = -1) -> bytes:
        body, self._body = self._body, b""
        return body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Opener:
    def __init__(self) -> None:
        self.calls = 0

    def open(self, request, timeout):
        self.calls += 1
        return Response()


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "dev-v2"
    shutil.copytree(DEV_V2 / "regression", root / "regression")
    (root / "capability").mkdir()
    _scenario(
        root,
        "dv2-b00001",
        "alertmanager",
        [PAYMENT, {"kind": "alert_delivery", "params": {"pattern": "single"}}],
        SIGNATURE,
        "wrong_target",
    )
    _scenario(
        root,
        "dv2-b00002",
        "question",
        [
            PAYMENT,
            {"kind": "source_unreachable", "params": {"source": "jaeger"}},
            {"kind": "model_transport", "params": {"mode": "first_503"}},
        ],
        SIGNATURE,
        "missing_telemetry",
    )
    return root


@pytest.mark.parametrize("scenario_id", ["dv2-b00001", "dv2-b00002"])
def test_v2_attempt_recording_binds_to_its_round_manifest(
    tmp_path, root, scenario_id, monkeypatch
):
    monkeypatch.setattr(eval_live_investigator, "_traces", lambda *a, **k: ())
    monkeypatch.setattr(
        eval_live_investigator.kind_lab, "webhook_token", lambda *a: "synthetic-431"
    )
    config = OtelDemoConfig()
    (slot,) = eval_live.plan_slots(root, [scenario_id], 1)
    manifest = eval_live.build_manifest(
        "t431", (slot,), 1, ["t"], scenarios_root=root, config=config
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
    # Live recordings must live at <repo>/tmp/eval/<recording_id> (B2 rule).
    recording_dir = eval_live.ROOT / "tmp/eval" / f"eval-live-t431-{attempt_id}"
    attempt = AttemptRecord(
        attempt_id,
        "t431",
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
        request = eval_live.InvestigationRequest(
            attempt=attempt,
            spec=slot.spec,
            pg=pg,
            recording_dir=recording_dir,
            ledger_dir=tmp_path / "ledger",
            round_id="t431",
            pre_observation=None,
            manifest=manifest,
            plan=slot.plan,
            alert_starts_at=clock.now(),
        )
        (tmp_path / "ledger").mkdir()
        result = investigator.investigate(request)
    finally:
        launcher.stop(pg)
    try:
        assert result.recording_ref is not None, (result.error, result.recording_error)
        recording = load_recording(
            request.recording_dir / result.recording_ref.path,
            expected_hash=result.recording_ref.sha256,
        )
    finally:
        shutil.rmtree(recording_dir, ignore_errors=True)
    bound = AttemptRecord(
        **{**attempt.__dict__, "product_run_id": result.product_run_id}
    )
    check_recording_binding(
        recording,
        bound,
        round_id="t431",
        epoch=result.usage["epoch"],
        manifest=manifest,
    )
    intake = result.intake
    assert intake is not None and intake["counts"]["runs"] == 1
    if scenario_id == "dv2-b00001":
        assert [d["outcome"] for d in intake["deliveries"]] == ["created"]
        assert intake["counts"]["episodes"] == 1
    else:
        assert intake["deliveries"] == [] and intake["counts"]["episodes"] is None
        assert intake["model_transport"]["intercepted"] == 1
        statuses = [
            e.hashes.get("status") for e in recording.events if e.kind == "http"
        ]
        assert statuses[0] == "503" and opener.calls == len(statuses) - 1
