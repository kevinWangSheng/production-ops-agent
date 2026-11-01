"""Implementer tests for #431 (v2 injectors, alert entry, zero-model probe).

Offline: the lab collaborators are the PR-C fakes, the model is a scripted
opener, nothing touches kubectl, the network, PostgreSQL or a model.
Contract: ~/.agent/handoffs/m3-batch4-2026-10-31/seams-431.md.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from opspilot.evaluation import Injector, load_attempt
from opspilot.evaluation.model import load_scenarios_v2
from opspilot.investigation.client import DeepSeekClient
from opspilot.investigation.loop import ModelError
from scripts import eval_live, eval_live_inject, eval_live_lab
from scripts.eval_live_lab import LabError, validate_injectors
from tests.unit import test_eval_b2_impl as b2
from tests.unit import test_eval_c_driver_impl as pr_c

ROOT = Path(__file__).resolve().parents[2]
DEV_V2 = ROOT / "evals/datasets/dev-v2"
FLAGS = json.loads(pr_c.ORIGINAL)
FLAGS["flags"]["adFailure"] = {
    "state": "ENABLED",
    "variants": {"on": True, "off": False},
    "defaultVariant": "off",
}
ORIGINAL_V2 = (json.dumps(FLAGS, indent=4) + "\n").encode()
PAYMENT = {"kind": "flagd", "params": {"flag": "paymentFailure", "variant": "100%"}}
AD = {"kind": "flagd", "params": {"flag": "adFailure", "variant": "on"}}
SIGNATURE = json.loads((DEV_V2 / "regression/dv2-2b214b/answer.json").read_text())[
    "signature"
]


def inj(*items: dict[str, Any]) -> tuple[Injector, ...]:
    return tuple(Injector(i["kind"], i["params"]) for i in items)


# -- whitelist -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "items"),
    [
        ("question", [PAYMENT, AD]),
        ("question", [AD]),
        (
            "question",
            [PAYMENT, {"kind": "source_unreachable", "params": {"source": "jaeger"}}],
        ),
        ("question", [{"kind": "model_transport", "params": {"mode": "first_503"}}]),
        (
            "question",
            [
                PAYMENT,
                {
                    "kind": "otlp_span",
                    "params": {
                        "service": "checkout",
                        "operation": "op",
                        "message": "m",
                    },
                },
            ],
        ),
        (
            "alertmanager",
            [PAYMENT, {"kind": "alert_delivery", "params": {"pattern": "single"}}],
        ),
        (
            "alertmanager",
            [
                PAYMENT,
                {
                    "kind": "alert_delivery",
                    "params": {
                        "pattern": "duplicate",
                        "deliveries": 3,
                        "interval_seconds": 59,
                    },
                },
            ],
        ),
        (
            "alertmanager",
            [
                {
                    "kind": "alert_delivery",
                    "params": {"pattern": "refire", "interval_seconds": 1},
                }
            ],
        ),
        (
            "alertmanager",
            [
                {
                    "kind": "alert_delivery",
                    "params": {
                        "pattern": "unresolved_labels",
                        "namespace": "otel-demo-x",
                    },
                }
            ],
        ),
    ],
)
def test_whitelisted_injectors_are_accepted(entry, items):
    plan = validate_injectors(entry, inj(*items))
    assert plan.entry == entry and len(plan.injectors) == len(items)


@pytest.mark.parametrize(
    ("entry", "items", "code"),
    [
        (
            "question",
            [{"kind": "flagd", "params": {"flag": "adFailure", "variant": "100%"}}],
            "FAULT_NOT_WHITELISTED",
        ),
        (
            "question",
            [
                {
                    "kind": "flagd",
                    "params": {"flag": "recommendationCache", "variant": "on"},
                }
            ],
            "FAULT_NOT_WHITELISTED",
        ),
        ("question", [{"kind": "kubectl", "params": {}}], "INJECTOR_NOT_WHITELISTED"),
        (
            "question",
            [
                {
                    "kind": "flagd",
                    "params": {"flag": "adFailure", "variant": "on", "x": 1},
                }
            ],
            "INJECTOR_NOT_WHITELISTED",
        ),
        (
            "question",
            [{"kind": "source_unreachable", "params": {"source": "loki"}}],
            "INJECTOR_NOT_WHITELISTED",
        ),
        (
            "question",
            [{"kind": "model_transport", "params": {"mode": "always_500"}}],
            "INJECTOR_NOT_WHITELISTED",
        ),
        (
            "question",
            [
                {
                    "kind": "otlp_span",
                    "params": {"service": "frontend", "operation": "o", "message": "m"},
                }
            ],
            "INJECTOR_NOT_WHITELISTED",
        ),
        (
            "question",
            [
                {
                    "kind": "otlp_span",
                    "params": {
                        "service": "cart",
                        "operation": "o",
                        "message": "x" * 601,
                    },
                }
            ],
            "INJECTOR_NOT_WHITELISTED",
        ),
        (
            "alertmanager",
            [
                {
                    "kind": "alert_delivery",
                    "params": {
                        "pattern": "duplicate",
                        "deliveries": 4,
                        "interval_seconds": 5,
                    },
                }
            ],
            "INJECTOR_NOT_WHITELISTED",
        ),
        (
            "alertmanager",
            [
                {
                    "kind": "alert_delivery",
                    "params": {"pattern": "refire", "interval_seconds": 60},
                }
            ],
            "INJECTOR_NOT_WHITELISTED",
        ),
        (
            "alertmanager",
            [
                {
                    "kind": "alert_delivery",
                    "params": {
                        "pattern": "unresolved_labels",
                        "namespace": "otel-demo",
                    },
                }
            ],
            "INJECTOR_NOT_WHITELISTED",
        ),
        (
            "alertmanager",
            [
                {
                    "kind": "alert_delivery",
                    "params": {"pattern": "single", "deliveries": 2},
                }
            ],
            "INJECTOR_NOT_WHITELISTED",
        ),
        ("question", [PAYMENT, PAYMENT], "INJECTOR_COMBINATION_INVALID"),
        (
            "question",
            [
                {"kind": "source_unreachable", "params": {"source": "jaeger"}},
                {"kind": "source_unreachable", "params": {"source": "prometheus"}},
            ],
            "INJECTOR_COMBINATION_INVALID",
        ),
        (
            "question",
            [{"kind": "alert_delivery", "params": {"pattern": "single"}}],
            "INJECTOR_COMBINATION_INVALID",
        ),
        ("alertmanager", [PAYMENT], "INJECTOR_COMBINATION_INVALID"),
    ],
)
def test_injectors_outside_the_whitelist_are_refused(entry, items, code):
    with pytest.raises(LabError, match=code):
        validate_injectors(entry, inj(*items))


def test_v1_whitelist_is_unchanged_and_v2_adds_only_adfailure():
    v1 = {f.flag: f.fault_variant for f in eval_live_lab.FAULT_FLAGS.values()}
    v2 = {f.flag: f.fault_variant for f in eval_live_lab.V2_FAULT_FLAGS.values()}
    assert v2 == {**v1, "adFailure": "on"} and "adFailure" not in v1


# -- model transport -----------------------------------------------------------

REPLY = json.dumps(
    {
        "id": "r",
        "model": "deepseek-flash",
        "choices": [
            {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {},
    }
).encode()


class CountingOpener(b2._FixedOpener):
    def __init__(self) -> None:
        super().__init__(REPLY)
        self.calls = 0

    def open(self, request, timeout):
        self.calls += 1
        return super().open(request, timeout)


def test_always_503_sends_no_request_to_the_provider():
    inner = CountingOpener()
    seen: list[Any] = []
    build = eval_live_inject.model_transport_factory(
        "always_503", inner=inner, seen=seen
    )
    client = build("synthetic-key", pr_c.Clock())
    for _ in range(3):
        with pytest.raises(ModelError, match="MODEL_UNAVAILABLE"):
            client.complete(b2.call())
    (fault,) = seen
    assert inner.calls == 0 and fault.intercepted == 3 and fault.forwarded == 0


def test_first_503_hits_only_the_first_request():
    inner = CountingOpener()
    seen: list[Any] = []
    build = eval_live_inject.model_transport_factory(
        "first_503", inner=inner, seen=seen
    )
    client = build("synthetic-key", pr_c.Clock())
    with pytest.raises(ModelError, match="MODEL_UNAVAILABLE"):
        client.complete(b2.call())
    client.complete(b2.call())
    client.complete(b2.call())
    (fault,) = seen
    assert (fault.intercepted, fault.forwarded, inner.calls) == (1, 2, 2)
    # A new attempt's client starts over.
    build("synthetic-key", pr_c.Clock())
    assert seen[1].intercepted == 0


def test_an_injected_503_passes_recording_validation(tmp_path):
    inner = CountingOpener()

    def drive(hooks):
        client = DeepSeekClient("synthetic-key", capture_http=hooks.http)
        eval_live_inject.front_client(client, "first_503", inner=inner)
        captured = hooks.model(client)
        with pytest.raises(ModelError):
            captured.complete(b2.call())
        captured.complete(b2.call())
        return replace(b2.outcome(), evidence_ids=())

    # ``source`` seals the package and reads it back through load_recording.
    recording = b2.source(tmp_path, driver=drive)
    kinds = [(e.kind, e.error, e.hashes.get("status")) for e in recording.events]
    assert kinds == [
        ("model", "MODEL_UNAVAILABLE", None),
        ("http", None, "503"),
        ("model", None, None),
        ("http", None, "200"),
    ]
    assert inner.calls == 1


# -- otlp span / alert payload -------------------------------------------------


def test_span_payload_is_one_internal_error_span_with_the_message():
    now = pr_c.T0
    span = eval_live_inject.span_payload(
        {"service": "payment", "operation": "charge.validate", "message": "ops-7k3m9x"},
        now=now,
        trace_id="a" * 32,
        span_id="b" * 16,
    )
    body = json.loads(span.payload)
    (resource,) = body["resourceSpans"]
    attrs = {
        a["key"]: a["value"]["stringValue"] for a in resource["resource"]["attributes"]
    }
    assert attrs == {
        "service.name": "payment",
        "service.namespace": "opentelemetry-demo",
        "opspilot.integration.id": "m0-otel-20260909",
    }
    (item,) = resource["scopeSpans"][0]["spans"]
    assert item["kind"] == 1 and item["status"] == {"code": 2}
    (event,) = item["events"]
    assert {a["key"]: a["value"]["stringValue"] for a in event["attributes"]} == {
        "exception.type": "Error",
        "exception.message": "ops-7k3m9x",
    }
    assert span.end - span.start == timedelta(milliseconds=50) and span.start < now


@pytest.mark.parametrize(
    ("params", "statuses", "episodes"),
    [
        ({"pattern": "single"}, ["firing"], [1]),
        (
            {"pattern": "duplicate", "deliveries": 3, "interval_seconds": 10},
            ["firing"] * 3,
            [1, 1, 1],
        ),
        (
            {"pattern": "refire", "interval_seconds": 10},
            ["firing", "resolved", "firing"],
            [1, 1, 2],
        ),
        ({"pattern": "unresolved_labels", "namespace": "otel-demo-x"}, ["firing"], [1]),
    ],
)
def test_alert_sequences_and_request_shape(params, statuses, episodes):
    sent: list[tuple[str, dict[str, str], bytes]] = []

    class Transport:
        def post(self, path, headers, body):
            sent.append((path, dict(headers), body))
            outcome = eval_live_inject.EXPECTED_OUTCOMES[params["pattern"]][
                len(sent) - 1
            ]
            return 200, json.dumps({"results": [{"outcome": outcome}]}).encode()

    clock = pr_c.Clock()
    run = eval_live_inject.deliver_alerts(
        params,
        transport=Transport(),
        token="t0k",
        clock=clock,
        first_starts_at=pr_c.T0,
        fingerprint="0123456789abcdef",
    )
    assert [d.status for d in run.deliveries] == statuses
    assert [d.outcome for d in run.deliveries] == list(
        eval_live_inject.EXPECTED_OUTCOMES[params["pattern"]]
    )
    starts = {}
    for (path, headers, raw), episode in zip(sent, episodes, strict=True):
        assert path == "/intake/alertmanager"
        assert set(headers) == {"Authorization", "Content-Type"}
        body = json.loads(raw)
        assert body["version"] == "4" and len(body["alerts"]) == 1
        alert = body["alerts"][0]
        assert set(alert) == {
            "status",
            "labels",
            "annotations",
            "startsAt",
            "endsAt",
            "generatorURL",
            "fingerprint",
        }
        assert alert["fingerprint"] == "0123456789abcdef"
        assert alert["labels"]["alertname"] == "CheckoutPlaceOrderErrorRatioHigh"
        expected_ns = params.get("namespace", "otel-demo")
        assert alert["labels"]["namespace"] == expected_ns
        starts.setdefault(episode, alert["startsAt"])
        assert alert["startsAt"] == starts[episode]
        assert (alert["endsAt"] == "0001-01-01T00:00:00Z") == (
            alert["status"] == "firing"
        )
    assert len(set(starts.values())) == len(starts)


def test_a_non_200_delivery_fails_the_setup():
    class Transport:
        def post(self, path, headers, body):
            return 401, b"{}"

    with pytest.raises(LabError, match="ALERT_DELIVERY_FAILED"):
        eval_live_inject.deliver_alerts(
            {"pattern": "single"},
            transport=Transport(),
            token="t",
            clock=pr_c.Clock(),
            first_starts_at=pr_c.T0,
        )


# -- driver: v2 slots, binding class, intent and restore -----------------------


def _scenario(root: Path, sid: str, entry: str, injectors, signature, category="noise"):
    directory = root / "capability" / sid
    directory.mkdir(parents=True)
    scenario = json.loads((DEV_V2 / "regression/dv2-2b214b/scenario.json").read_text())
    scenario.update(scenario_id=sid, entry=entry)
    answer = json.loads((DEV_V2 / "regression/dv2-2b214b/answer.json").read_text())
    answer.update(
        scenario_id=sid, category=category, injectors=injectors, signature=signature
    )
    (directory / "scenario.json").write_text(json.dumps(scenario))
    (directory / "answer.json").write_text(json.dumps(answer))


@pytest.fixture
def v2(tmp_path):
    root = tmp_path / "dev-v2"
    shutil.copytree(DEV_V2 / "regression", root / "regression")
    (root / "capability").mkdir()
    _scenario(root, "dv2-a00001", "question", [PAYMENT, AD], SIGNATURE)
    _scenario(root, "dv2-a00002", "question", [AD], None)
    _scenario(
        root,
        "dv2-a00003",
        "question",
        [PAYMENT, {"kind": "source_unreachable", "params": {"source": "jaeger"}}],
        SIGNATURE,
        "missing_telemetry",
    )
    _scenario(
        root,
        "dv2-a00004",
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
    _scenario(
        root,
        "dv2-a00005",
        "question",
        [
            PAYMENT,
            {
                "kind": "otlp_span",
                "params": {"service": "checkout", "operation": "op", "message": "m"},
            },
        ],
        SIGNATURE,
        "prompt_injection",
    )
    load_scenarios_v2(root)
    env = pr_c.make_env(tmp_path)
    env["lab"].raw = ORIGINAL_V2
    env["deps"] = replace(env["deps"], scenarios_root=root)
    env["root"] = root
    return env


def _config(env, ids, round_id="r1"):
    slots = eval_live.plan_slots(env["root"], ids, 1)
    manifest = eval_live.build_manifest(
        round_id, slots, 1, ["t"], scenarios_root=env["root"]
    )
    return eval_live.RoundConfig(round_id, slots, "lead", "b1", 55999, manifest, ("t",))


def test_v2_root_plans_through_the_v2_loader_and_refuses_mixed_bindings(v2):
    slots = eval_live.plan_slots(v2["root"], ["dv2-a00001", "dv2-2b214b"], 2)
    assert [s.spec.scenario_id for s in slots] == ["dv2-2b214b"] * 2 + [
        "dv2-a00001"
    ] * 2
    assert all(s.plan is not None for s in slots)
    with pytest.raises(eval_live._Usage, match="ROUND_BINDING_MIXED"):
        eval_live.plan_slots(v2["root"], ["dv2-a00001", "dv2-a00003"], 1)
    with pytest.raises(eval_live._Usage, match="ROUND_BINDING_MIXED"):
        eval_live.plan_slots(v2["root"], ["dv2-a00001", "dv2-a00004"], 1)
    rules = eval_live.fault_signatures(v2["deps"])
    assert [r.rule_id for r in rules] == sorted({r.rule_id for r in rules})


def test_manifest_follows_the_binding_class(v2):
    base = _config(v2, ["dv2-a00001"]).manifest.dimensions
    jaeger = _config(v2, ["dv2-a00003"]).manifest.dimensions
    alert = _config(v2, ["dv2-a00004"]).manifest.dimensions
    # The unreachable target is another registry entry, and the scope policy
    # carries the registry revision: both move with the binding class.
    assert base["registry"] != jaeger["registry"]
    assert base["permissions"] != jaeger["permissions"]
    assert base["permissions"] != alert["permissions"]
    assert alert["clock_policy"].value == eval_live.ALERT_CLOCK_POLICY
    assert base["dataset"].value.startswith("dev-v2:")


def test_multi_flag_attempt_writes_one_intent_and_restores_every_flag(v2):
    deps, lab = v2["deps"], v2["lab"]
    seen: dict[str, Any] = {}

    def look(request):
        seen["state"] = eval_live_lab.read_state(deps.ledger_root)
        seen["plan"] = request.plan

    v2["investigator"].action = look
    result = eval_live.run_round(_config(v2, ["dv2-a00001"]), deps)
    state = seen["state"]
    assert [f["flag"] for f in state["flags"]] == ["paymentFailure", "adFailure"]
    assert (
        state["flag"] == "paymentFailure" and state["original"] == ORIGINAL_V2.decode()
    )
    assert {i["kind"] for i in state["injectors"]} == {"flagd"}
    assert all(i["phase"] == "effective" for i in state["injectors"])
    assert len(lab.writes) == 2 and lab.raw == ORIGINAL_V2  # one inject, one restore
    assert lab.effective("adFailure", v2["clock"].now()) == "off"
    assert eval_live_lab.read_state(deps.ledger_root) is None
    assert seen["plan"].flags[1].flag == "adFailure"
    assert result.slots[0].attempt_id is not None
    journal = load_attempt(
        deps.evidence_root / "r1/runs" / result.slots[0].attempt_id / "attempt.json"
    )
    assert journal.spec.scenario_id == "dv2-a00001"


def test_v2_round_requires_adfailure_off_before_it_starts(v2):
    data = json.loads(ORIGINAL_V2)
    data["flags"]["adFailure"]["defaultVariant"] = "on"
    v2["lab"].raw = (json.dumps(data) + "\n").encode()
    result = eval_live.run_round(_config(v2, ["dv2-a00001"]), v2["deps"])
    assert result.exit_code == 3 and result.reason == "FLAGS_NOT_NORMAL"


def test_a_flag_without_signature_gates_on_the_normal_window(v2):
    eval_live.run_round(_config(v2, ["dv2-a00002"]), v2["deps"])
    (request,) = v2["investigator"].requests
    assert request.pre_observation.kind == "normal" and request.pre_observation.passed


def test_an_interrupt_during_a_v2_attempt_still_restores_all_flags(v2):
    deps, lab = v2["deps"], v2["lab"]

    def interrupt(request):
        raise eval_live_lab.Interrupted()

    v2["investigator"].action = interrupt
    result = eval_live.run_round(_config(v2, ["dv2-a00001"]), deps)
    assert result.exit_code == 130
    assert eval_live_lab.read_state(deps.ledger_root) is None and lab.raw == ORIGINAL_V2


def test_recover_restores_a_retained_multi_flag_intent(v2):
    deps, lab = v2["deps"], v2["lab"]

    def crash_restore(*args, **kwargs):
        raise LabError("FLAG_WRITE_FAILED")

    original = eval_live.restore_fault
    eval_live.restore_fault = crash_restore
    try:
        result = eval_live.run_round(_config(v2, ["dv2-a00001"]), deps)
    finally:
        eval_live.restore_fault = original
    assert result.exit_code == 4
    assert eval_live_lab.read_state(deps.ledger_root)["round"] == "r1"
    recovered = eval_live.recover_round("r1", "lead", "b1", deps)
    assert recovered.exit_code == 0
    assert eval_live_lab.read_state(deps.ledger_root) is None and lab.raw == ORIGINAL_V2


def test_unknown_injector_in_a_retained_intent_is_refused(v2):
    deps = v2["deps"]
    deps.ledger_root.mkdir(parents=True, exist_ok=True)
    state = {
        "owner": eval_live_lab.OWNER,
        "round": "r1",
        "attempt_id": "a",
        "original": None,
        "injectors": [{"kind": "kubectl", "params": {}, "phase": "intent"}],
    }
    (deps.ledger_root / eval_live_lab.STATE_FILE).write_text(json.dumps(state))
    ledger = eval_live_lab.AttemptLedger(deps.ledger_root / "l", deps.clock)
    with pytest.raises(LabError, match="FAULT_STATE_INVALID"):
        eval_live_lab.restore_fault("r1", "a", (), deps=deps, ledger=ledger)
    assert (deps.ledger_root / eval_live_lab.STATE_FILE).exists()


class SpanWriter:
    def __init__(self, clock):
        self.clock = clock
        self.payloads: list[bytes] = []

    def write(self, payload, timeout):
        self.payloads.append(payload)
        return 200

    def visible(self, trace_id, timeout):
        return bool(self.payloads)


def test_otlp_span_is_recorded_before_the_write_and_aged_out_before_the_wash(v2):
    deps, clock = v2["deps"], v2["clock"]
    writer = SpanWriter(clock)
    deps = replace(deps, span_writer=writer)
    seen: dict[str, Any] = {}

    def look(request):
        seen["state"] = eval_live_lab.read_state(deps.ledger_root)
        seen["at"] = clock.now()

    v2["investigator"].action = look
    eval_live.run_round(_config(v2, ["dv2-a00005"]), deps)
    (span,) = [i for i in seen["state"]["injectors"] if i["kind"] == "otlp_span"]
    assert span["phase"] == "effective" and len(span["trace_id"]) == 32
    end = eval_live_lab.datetime.fromisoformat(span["span_end"])
    assert len(writer.payloads) == 1
    assert eval_live_lab.read_state(deps.ledger_root) is None
    lifecycle = (deps.ledger_root / "r1").rglob("lifecycle.jsonl")
    events = [
        json.loads(line) for path in lifecycle for line in path.read_text().splitlines()
    ]
    aged = [e for e in events if e.get("event") == "span_aged_out"]
    assert aged and eval_live_lab.datetime.fromisoformat(
        aged[0]["at"]
    ) >= end + timedelta(seconds=300)


def test_wrong_target_handoff_without_run_is_ran_with_intake_record(v2):
    deps = v2["deps"]
    intake = {
        "entry": "alertmanager",
        "deliveries": [
            {
                "seq": 1,
                "status": "firing",
                "starts_at": "x",
                "http_status": 200,
                "outcome": "handoff_created",
                "reason": "TARGET_UNRESOLVED",
                "incident_id": "i",
                "run_id": None,
            }
        ],
        "counts": {"incidents": 1, "runs": 0, "episodes": 1},
    }

    class Handoff(pr_c.Investigator):
        def investigate(self, request):
            self.requests.append(request)
            return eval_live.InvestigationResult(
                None,
                None,
                None,
                None,
                None,
                (),
                self.clock.now(),
                None,
                self.clock.now(),
                {"epoch": None},
                None,
                intake=intake,
            )

    deps = replace(deps, investigator=Handoff(v2["lab"], v2["clock"]))
    result = eval_live.run_round(_config(v2, ["dv2-a00004"]), deps)
    (slot,) = result.slots
    assert slot.status == "ran" and slot.product_run_id is None
    run_dir = deps.evidence_root / "r1/runs" / slot.attempt_id
    assert json.loads((run_dir / "intake.json").read_text()) == intake
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["capture"]["recording_ref"] is None
    assert summary["capture"]["attempt"]["status"] == "ran"


def test_v1_root_is_unchanged():
    slots = eval_live.plan_slots(pr_c.SCENARIOS, ["all"], 1)
    assert all(s.plan is None for s in slots)
    assert eval_live.round_binding(slots) == ("question", "none")


def test_unreachable_config_and_connection_check():
    from opspilot.tools.otel_demo import OtelDemoConfig

    config = eval_live_inject.unreachable_config(OtelDemoConfig(), "jaeger")
    assert config.jaeger_url == "http://127.0.0.1:9/jaeger/ui"
    assert config.prometheus_url == OtelDemoConfig().prometheus_url
    assert eval_live_inject.connection_refused("http://127.0.0.1:9") is True


def test_v2_probe_injects_checks_and_washes_without_a_run(v2):
    from scripts.eval_live_probe import run_probe

    root = v2["root"]
    _scenario(
        root,
        "dv2-a00006",
        "question",
        [
            PAYMENT,
            AD,
            {"kind": "model_transport", "params": {"mode": "always_503"}},
        ],
        SIGNATURE,
        "model_failure",
    )
    deps, lab = v2["deps"], v2["lab"]
    slots = eval_live.plan_slots(root, ["dv2-a00006"], 1)
    config = eval_live.RoundConfig("p1", slots, "lead", "b1", 0, None, ("t",), 1)
    result = run_probe(config, deps)
    assert result.exit_code == 0, result
    probe = json.loads((deps.evidence_root / "p1/probe.json").read_text())
    (entry,) = probe["scenarios"]
    assert entry["reason"] is None and entry["model_requests"] == 0
    assert entry["fault"]["flags"] == ["paymentFailure", "adFailure"]
    assert entry["fault"]["restored"] and entry["fault"]["washed"]
    model = entry["effects"]["model_transport"]
    assert (model["intercepted"], model["forwarded"], model["inner_calls"]) == (3, 0, 0)
    assert v2["investigator"].requests == [] and lab.raw == ORIGINAL_V2
    assert eval_live_lab.read_state(deps.ledger_root) is None


# -- seams revision 1/2 public names --------------------------------------------


def test_interrupted_adfailure_is_restored_from_the_intent(tmp_path):
    """Revision 1 condition: recovery after an interrupt restores adFailure."""
    harness = eval_live.InjectorHarness(tmp_path / "state")
    harness.interrupt_after_effect = True
    with pytest.raises(InterruptedError):
        harness.apply([PAYMENT, AD])
    state = json.loads((tmp_path / "state/fault-active.json").read_text())
    assert [f["flag"] for f in state["flags"]] == ["paymentFailure", "adFailure"]
    assert {i["phase"] for i in state["injectors"]} == {"effective"}
    assert not harness.flags_are_original()
    assert harness.lab.resolve("adFailure", "", 0) == "on"
    harness.recover()
    assert harness.flags_are_original()
    assert not (tmp_path / "state/fault-active.json").exists()


def test_adfailure_alone_is_restored_and_a_span_intent_is_waited_out(tmp_path):
    harness = eval_live.InjectorHarness(tmp_path / "state")
    span = {
        "kind": "otlp_span",
        "params": {"service": "cart", "operation": "op", "message": "m"},
    }
    plan = harness.apply([AD, span])
    assert plan.restore_intent_count == 1 and harness.restore_intent_count == 1
    harness.recover()
    assert harness.flags_are_original()
    assert not (tmp_path / "state/fault-active.json").exists()


def test_v1_paths_still_refuse_adfailure():
    with pytest.raises(LabError, match="FAULT_NOT_WHITELISTED"):
        eval_live_lab.fault_for("adFailure", "on")
    with pytest.raises(SystemExit):
        eval_live_lab.kind_lab.mutate_flags(ORIGINAL_V2, "on", "off", flag="adFailure")


def test_revision_two_wrappers():
    eval_live.validate_recorded_model_error(
        {"status": "503", "response": b"{}", "error": "MODEL_UNAVAILABLE"}
    )
    for bad in (
        {"status": "503", "response": None, "error": "MODEL_UNAVAILABLE"},
        {"status": "200", "response": b"{}", "error": "MODEL_UNAVAILABLE"},
        {"status": "503", "response": b"{}", "error": None},
    ):
        with pytest.raises(eval_live.StorageError):
            eval_live.validate_recorded_model_error(bad)
    request = eval_live.build_alertmanager_request(
        pattern="duplicate", fingerprint="f" * 16, starts_at=pr_c.T0, token="sekrit"
    )
    assert request["path"] == "/intake/alertmanager"
    assert set(request["headers"]) == {"Authorization", "Content-Type"}
    assert "sekrit" not in json.dumps(request["body"])
    assert request["body"]["alerts"][0]["status"] == "firing"
    with pytest.raises(LabError) as caught:
        eval_live.validate_round_bindings(
            [
                eval_live.Slot(None, None, 1, validate_injectors("question", ())),  # type: ignore[arg-type]
                eval_live.Slot(
                    None,  # type: ignore[arg-type]
                    None,  # type: ignore[arg-type]
                    1,
                    validate_injectors(
                        "alertmanager",
                        inj(
                            {"kind": "alert_delivery", "params": {"pattern": "single"}}
                        ),
                    ),
                ),
            ]
        )
    assert caught.value.code == "ROUND_BINDING_MIXED"


def test_write_intake_creates_its_parent(tmp_path):
    path = tmp_path / "a/b/intake.json"
    payload = {
        "entry": "question",
        "deliveries": [],
        "counts": {"incidents": 1, "runs": 1, "episodes": None},
    }
    eval_live.write_intake(path, payload)
    assert json.loads(path.read_text()) == payload


def test_v2_slots_expose_scenario_id_and_answer_injectors():
    slots = eval_live.plan_slots(DEV_V2, ["all"], 1)
    loaded = dict((s.scenario_id, a) for s, a in load_scenarios_v2(DEV_V2))
    for slot in slots:
        assert slot.scenario_id in loaded
        assert slot.answer.injectors == loaded[slot.scenario_id].injectors
