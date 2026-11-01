"""Offline acceptance contract for issue #431 (scenario set v2 B2).

These tests are written from seams-431 and the M3 closure contract.  They do
not start kind, PostgreSQL, a web server, or a model provider.  Missing v2
public seams are intentional red tests while #431 is implemented.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from opspilot.evaluation import (
    AttemptJournal,
    AttemptRecord,
    EvaluationCapture,
    load_attempt,
    load_scenarios_v2,
    manifest_hash,
    write_attempt,
    write_summary,
)
from scripts import eval_live, eval_live_lab
from tests.acceptance import eval_b2_support as b2

ROOT = Path(__file__).resolve().parents[2]
V2_ROOT = ROOT / "evals" / "datasets" / "dev-v2"
V1_ROOT = ROOT / "evals" / "scenarios"


def _api(name: str):
    """Resolve a seam at test time so collection succeeds before #431."""
    for module in (eval_live_lab, eval_live):
        value = getattr(module, name, None)
        if value is not None:
            return value
    pytest.fail(f"#431 public seam is missing: {name}")


VALID = [
    ("flagd", {"flag": "paymentFailure", "variant": "100%"}),
    ("flagd", {"flag": "adFailure", "variant": "on"}),
    ("source_unreachable", {"source": "jaeger"}),
    ("source_unreachable", {"source": "prometheus"}),
    ("model_transport", {"mode": "always_503"}),
    ("model_transport", {"mode": "first_503"}),
    (
        "otlp_span",
        {"service": "checkout", "operation": "PlaceOrder", "message": "synthetic"},
    ),
    ("alert_delivery", {"pattern": "single"}),
    (
        "alert_delivery",
        {"pattern": "duplicate", "deliveries": 2, "interval_seconds": 1},
    ),
    ("alert_delivery", {"pattern": "refire", "interval_seconds": 1}),
    ("alert_delivery", {"pattern": "unresolved_labels", "namespace": "canary"}),
]


@pytest.mark.parametrize(("kind", "params"), VALID)
def test_allowlist_accepts_every_seams_injector(kind, params):
    entry = "alertmanager" if kind == "alert_delivery" else "question"
    plan = _api("validate_injectors")(
        entry, [SimpleNamespace(kind=kind, params=params)]
    )
    assert plan


@pytest.mark.parametrize(
    ("entry", "injectors", "code"),
    [
        (
            "question",
            [SimpleNamespace(kind="alert_delivery", params={"pattern": "single"})],
            "INJECTOR_COMBINATION_INVALID",
        ),
        ("alertmanager", [], "INJECTOR_COMBINATION_INVALID"),
        (
            "question",
            [SimpleNamespace(kind="unknown", params={})],
            "INJECTOR_NOT_WHITELISTED",
        ),
        (
            "question",
            [SimpleNamespace(kind="flagd", params={"flag": "paymentFailure"})],
            "INJECTOR_NOT_WHITELISTED",
        ),
        (
            "question",
            [SimpleNamespace(kind="flagd", params={"flag": "nope", "variant": "on"})],
            "FAULT_NOT_WHITELISTED",
        ),
        (
            "question",
            [
                SimpleNamespace(
                    kind="source_unreachable", params={"source": "jaeger", "extra": 1}
                )
            ],
            "INJECTOR_NOT_WHITELISTED",
        ),
        (
            "question",
            [
                SimpleNamespace(kind="model_transport", params={"mode": "always_503"}),
                SimpleNamespace(kind="model_transport", params={"mode": "first_503"}),
            ],
            "INJECTOR_COMBINATION_INVALID",
        ),
        (
            "question",
            [
                SimpleNamespace(
                    kind="flagd", params={"flag": "paymentFailure", "variant": "100%"}
                ),
                SimpleNamespace(
                    kind="flagd", params={"flag": "paymentFailure", "variant": "100%"}
                ),
            ],
            "INJECTOR_COMBINATION_INVALID",
        ),
        (
            "alertmanager",
            [
                SimpleNamespace(
                    kind="alert_delivery",
                    params={
                        "pattern": "duplicate",
                        "deliveries": 2,
                        "interval_seconds": 60,
                    },
                )
            ],
            "INJECTOR_NOT_WHITELISTED",
        ),
    ],
)
def test_allowlist_rejects_out_of_contract_values(entry, injectors, code):
    with pytest.raises(Exception) as exc:
        _api("validate_injectors")(entry, injectors)
    assert getattr(exc.value, "code", str(exc.value)) == code


def test_multi_flag_injection_has_one_restore_intent(tmp_path):
    plan = _api("validate_injectors")(
        "question",
        [
            SimpleNamespace(
                kind="flagd", params={"flag": "paymentFailure", "variant": "100%"}
            ),
            SimpleNamespace(
                kind="flagd", params={"flag": "adFailure", "variant": "on"}
            ),
        ],
    )
    assert getattr(plan, "restore_intent_count", 1) == 1


def test_interruption_leaves_intent_and_wash_restores_everything(tmp_path):
    """The fake flag store raises after effect; recovery must be idempotent."""
    harness = _api("InjectorHarness")(tmp_path)
    injectors = [
        {"kind": "flagd", "params": {"flag": "paymentFailure", "variant": "100%"}},
        {"kind": "source_unreachable", "params": {"source": "jaeger"}},
    ]
    harness.interrupt_after_effect = True
    with pytest.raises(InterruptedError):
        harness.apply(injectors)
    intent = json.loads((tmp_path / "fault-active.json").read_text())
    assert intent["injectors"]
    assert all(item["phase"] in {"intent", "effective"} for item in intent["injectors"])
    harness.recover()
    assert not (tmp_path / "fault-active.json").exists()
    assert harness.flags_are_original()


@pytest.mark.parametrize("mode", ["always_503", "first_503"])
def test_model_transport_fault_has_bounded_forwarding(mode):
    fault_type = _api("ModelTransportFault")

    class Inner:
        def __init__(self):
            self.calls = 0

        def open(self, request, timeout=None):
            self.calls += 1
            return object()

    inner = Inner()
    fault = fault_type(mode, inner)
    request = Request("https://example.test/model")
    if mode == "always_503":
        for _ in range(3):
            with pytest.raises(HTTPError) as exc:
                fault.open(request, timeout=1)
            assert exc.value.code == 503
        assert inner.calls == 0
        assert fault.intercepted == 3
        assert fault.forwarded == 0
    else:
        with pytest.raises(HTTPError) as exc:
            fault.open(request, timeout=1)
        assert exc.value.code == 503
        fault.open(request, timeout=1)
        fault.open(request, timeout=1)
        assert (inner.calls, fault.intercepted, fault.forwarded) == (2, 1, 2)


def test_recorded_503_body_is_complete_for_recording_validation():
    fault = _api("ModelTransportFault")(
        "always_503", SimpleNamespace(open=lambda *a, **k: None)
    )
    request = Request("https://example.test/model")
    with pytest.raises(HTTPError) as exc:
        fault.open(request, timeout=1)
    body = exc.value.read()
    assert json.loads(body) == {"error": {"message": "Service Unavailable"}}
    assert exc.value.headers["Content-Length"] == str(len(body))
    validator = _api("validate_recorded_model_error")
    assert (
        validator({"status": "503", "response": body, "error": "MODEL_UNAVAILABLE"})
        is None
    )


def test_alert_entry_request_shapes_and_counts_are_public_contract():
    builder = _api("build_alertmanager_request")
    token = "lab-token"
    request = builder(
        pattern="duplicate",
        fingerprint="a" * 16,
        starts_at=datetime(2026, 11, 1, tzinfo=UTC),
        token=token,
    )
    assert request["path"] == "/intake/alertmanager"
    assert request["headers"]["Authorization"] == f"Bearer {token}"
    assert request["body"]["version"] == "4"
    assert len(request["body"]["alerts"]) == 1
    assert request["body"]["alerts"][0]["fingerprint"] == "a" * 16
    assert request["body"]["alerts"][0]["startsAt"] == "2026-11-01T00:00:00Z"
    assert request["body"]["alerts"][0]["labels"]["namespace"] == "otel-demo"
    assert token not in json.dumps(request["body"])
    assert set(request["headers"]) == {"Authorization", "Content-Type"}


@pytest.mark.parametrize(
    ("pattern", "counts"),
    [
        ("duplicate", {"incidents": 1, "runs": 1, "episodes": 1}),
        ("refire", {"incidents": 1, "runs": 1, "episodes": 2}),
        ("unresolved_labels", {"incidents": 1, "runs": 0, "episodes": 1}),
    ],
)
def test_alert_entry_intake_counts_and_wrong_target_outcome(pattern, counts, tmp_path):
    write = _api("write_intake")
    payload = {"entry": "alertmanager", "deliveries": [], "counts": counts}
    if pattern == "unresolved_labels":
        payload["deliveries"] = [
            {"outcome": "handoff_created", "reason": "TARGET_UNRESOLVED"}
        ]
    path = tmp_path / "runs" / "attempt" / "intake.json"
    write(path, payload)
    doc = json.loads(path.read_text())
    assert doc["counts"] == counts
    if pattern == "unresolved_labels":
        assert doc["deliveries"][0]["reason"] == "TARGET_UNRESOLVED"


def test_v2_slot_planning_uses_v2_loader_and_answer_injectors():
    slots = eval_live.plan_slots(V2_ROOT, ["all"], 1)
    loaded = {spec.scenario_id: answer for spec, answer in load_scenarios_v2(V2_ROOT)}
    assert slots
    assert all(slot.scenario_id in loaded for slot in slots)
    assert all(
        slot.answer.injectors == loaded[slot.scenario_id].injectors for slot in slots
    )


def test_v1_slot_planning_remains_byte_compatible():
    before = sorted(
        (p.relative_to(V1_ROOT), p.read_bytes())
        for p in V1_ROOT.rglob("*")
        if p.is_file()
    )
    eval_live.plan_slots(V1_ROOT, ["all"], 1)
    after = sorted(
        (p.relative_to(V1_ROOT), p.read_bytes())
        for p in V1_ROOT.rglob("*")
        if p.is_file()
    )
    assert after == before


def test_mixed_binding_classes_are_rejected(tmp_path):
    single_class_slots = _api("plan_slots")(V2_ROOT, ["all"], 1)
    _api("validate_round_bindings")(single_class_slots)

    mixed_root = tmp_path / "dev-v2-mixed"
    shutil.copytree(V2_ROOT, mixed_root)
    scenario_dir = sorted((mixed_root / "regression").iterdir())[1]
    answer_path = scenario_dir / "answer.json"
    answer = json.loads(answer_path.read_text())
    answer["injectors"] = [
        {"kind": "source_unreachable", "params": {"source": "jaeger"}}
    ]
    answer_path.write_text(json.dumps(answer))
    # Seams S2: a mixed round is a usage error; planning may already refuse it,
    # otherwise the explicit binding check must.
    with pytest.raises(Exception) as exc:
        slots = _api("plan_slots")(mixed_root, ["all"], 1)
        _api("validate_round_bindings")(slots)
    assert getattr(exc.value, "code", str(exc.value)) == "ROUND_BINDING_MIXED"


def test_v2_storage_journal_and_summary_round_trip(tmp_path):
    spec, answer = load_scenarios_v2(V2_ROOT)[0]
    journal = AttemptJournal(
        attempt=AttemptRecord(
            attempt_id="attempt-v2",
            round_id="round-v2",
            manifest_hash="a" * 64,
            scenario_id=spec.scenario_id,
            scenario_version=spec.version,
            mode="live",
            repetition=1,
            started=True,
            status="ran",
            replay_miss=False,
            product_run_id=None,
        ),
        spec=spec,
        answer=answer,
    )
    ref = write_attempt(tmp_path / "attempt.json", journal)
    loaded = load_attempt(tmp_path / ref.path)
    assert loaded.spec.version == "2"
    assert loaded.answer.injectors == answer.injectors


def test_v2_storage_summary_writer_accepts_the_same_slot_capture(tmp_path):
    spec, answer = load_scenarios_v2(V2_ROOT)[0]
    manifest = b2.manifest()
    attempt = AttemptRecord(
        attempt_id="attempt-v2-summary",
        round_id=manifest.round_id,
        manifest_hash=manifest_hash(manifest),
        scenario_id=spec.scenario_id,
        scenario_version=spec.version,
        mode="live",
        repetition=1,
        started=True,
        status="ran",
        replay_miss=False,
        product_run_id=None,
    )
    capture = EvaluationCapture(
        manifest=manifest,
        spec=spec,
        answer=answer,
        attempt=attempt,
        outcome=None,
        report=None,
        delivered={},
        authorized={},
        traces=(),
        judge=None,
        delivery_refs={},
    )
    ref = write_summary(tmp_path / "summary.json", capture)
    assert ref.path == "summary.json"


def test_v1_storage_bytes_are_unchanged(tmp_path):
    source = next(V1_ROOT.glob("*/answer.json"))
    original = source.read_bytes()
    eval_live.plan_slots(V1_ROOT, ["all"], 1)
    assert source.read_bytes() == original
