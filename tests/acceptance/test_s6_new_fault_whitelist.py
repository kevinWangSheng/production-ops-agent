"""Independent acceptance tests for F1 closure S6: three new flagd switches in
the v2 injector whitelist, plus a metric-signal noise confirmation gate.

Contract (F1 closure pre-review §1 A, §2.2 S6, R3; rulings U2):

* ``validate_injectors`` accepts ``paymentUnreachable``, ``adManualGc`` and
  ``loadGeneratorFloodHomepage`` (fault variant ``on``, normal ``off``) and
  still refuses the unselected switches (``kafkaQueueProblems``,
  ``imageSlowLoad``, ``adHighCpu``, ``recommendationCacheFailure``) with
  ``FAULT_NOT_WHITELISTED``.  The v1 table stays the three v1 flags.
* ``paymentUnreachable`` is a positive base fault under the existing payment
  signature; it gets no noise gate.
* ``adManualGc`` and ``loadGeneratorFloodHomepage`` are noise only.  They
  produce no error span, so their "noise observed" gate is metric based: after
  the flag is effective the harness must see the switch's metric signal rise
  over its pre-injection baseline before the Run is submitted, else the
  attempt is ``setup_failed`` / ``NOISE_NOT_OBSERVED`` and every flag is
  restored.  This holds beside a signature fault (noise positive) and alone
  (noise negative, ``signature: null``).
* No new answer signature type: answers keep the V2-R2 schema (R3).

Seam convention this file introduces (the only new public name):

    TelemetryProbe.metric(expr: str, start: datetime, end: datetime,
                          timeout: float) -> CallCount

  One Prometheus value of ``expr`` over the window ``[start, end]`` (range
  ``end - start``, evaluated at ``end``), the same shape as the existing
  ``checkout_calls``; ``CallCount(None, False)`` when the query failed.  The
  metric noise gate reads every one of its signals through this method only.
  The double below does not parse ``expr``: a window whose midpoint falls
  while the noise flag is effective reads ``HIGH`` (1000.0, far over every
  pre-review threshold: 10x / 5x / 3x baseline and the 200 ms floor), any
  other window reads ``BASELINE`` (1.0).  So a gate that compares the fault
  window with a pre-injection baseline window, as the pre-review criterion
  says ("crosses the threshold in F and not in B"), passes when the signal
  rose and fails when it never left baseline.

  The gate's raw observation is the private file
  ``pre-observation-noise-<flag>.json`` in the attempt ledger, as for the
  existing ``adFailure`` span gate (#432 §10 A4).

The round runs through ``eval_live.run_round`` over the offline PR-C lab
doubles (fake clock, in-memory CAS ConfigMap, fake flagd).  No kind, kubectl,
PostgreSQL, Prometheus or model is touched.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opspilot.evaluation import load_scenarios_v2
from opspilot.evaluation.model import ScenarioError
from scripts import eval_live, eval_live_lab
from scripts.eval_live_lab import CallCount, LabError, ObservedSpan, SpanQuery
from tests.unit import test_eval_c_driver_impl as pr_c

ROOT = Path(__file__).resolve().parents[2]
DEV_V2 = ROOT / "evals" / "datasets" / "dev-v2"
PAYMENT_DIR = DEV_V2 / "regression" / "dv2-2b214b"

NEW_FLAGS = ("paymentUnreachable", "adManualGc", "loadGeneratorFloodHomepage")
METRIC_NOISE = ("adManualGc", "loadGeneratorFloodHomepage")
UNSELECTED = (
    "kafkaQueueProblems",
    "imageSlowLoad",
    "adHighCpu",
    "recommendationCacheFailure",
)
V1_FLAGS = {"paymentFailure", "productCatalogFailure", "cartFailure"}

# The lab ConfigMap: v1 three + adFailure + the three new switches, with the
# upstream 2.0.2 variants (booleans; the flood is a number of users).
_FLAGS = json.loads(pr_c.ORIGINAL)
for _name, _variants in {
    "adFailure": {"on": True, "off": False},
    "paymentUnreachable": {"on": True, "off": False},
    "adManualGc": {"on": True, "off": False},
    "loadGeneratorFloodHomepage": {"on": 100, "off": 0},
}.items():
    _FLAGS["flags"][_name] = {
        "state": "ENABLED",
        "variants": _variants,
        "defaultVariant": "off",
    }
ORIGINAL_S6 = (json.dumps(_FLAGS, indent=4) + "\n").encode()
ALL_FLAGS = tuple(_FLAGS["flags"])

PAYMENT = {"kind": "flagd", "params": {"flag": "paymentFailure", "variant": "100%"}}
UNREACHABLE = {
    "kind": "flagd",
    "params": {"flag": "paymentUnreachable", "variant": "on"},
}
SIGNATURE = json.loads((PAYMENT_DIR / "answer.json").read_text())["signature"]
CHECKOUT_CHARGE = ("checkout", "oteldemo.PaymentService/Charge")

NOISE_POSITIVE = "dv2-5e6a01"
NOISE_NEGATIVE = "dv2-5e6a02"
UNREACHABLE_POSITIVE = "dv2-5e6b01"
UNSELECTED_ID = "dv2-5e6c01"

BASELINE = 1.0
HIGH = 1000.0


def _flagd(flag: str, variant: str = "on") -> dict[str, Any]:
    return {"kind": "flagd", "params": {"flag": flag, "variant": variant}}


def _ns(injector: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(kind=injector["kind"], params=dict(injector["params"]))


class Telemetry:
    """pr_c's lab spans and checkout calls, plus:

    * checkout ``oteldemo.PaymentService/Charge`` error spans while
      ``paymentUnreachable`` is effective (payment has no server span then);
    * ``metric``: the S6 seam.  ``mode`` is ``seen`` (HIGH while the noise
      flag is effective), ``none`` (always BASELINE), ``always`` (HIGH in
      every window, baseline included) or ``fail`` (every query fails).
    """

    def __init__(self, lab: pr_c.Lab, noise_flag: str | None, mode: str) -> None:
        self.lab = lab
        self.noise_flag = noise_flag
        self.mode = mode
        self.metric_calls: list[tuple[str, datetime, datetime]] = []

    def spans(
        self, service: str, start: datetime, end: datetime, timeout: float
    ) -> SpanQuery:
        base = self.lab.spans(service, start, end, timeout)
        rows = list(base.spans)
        if service == CHECKOUT_CHARGE[0]:
            t, index = start, 0
            while t <= end:
                if self.lab.effective("paymentUnreachable", t) != "off":
                    rows.append(
                        ObservedSpan(
                            f"pu-trace-{t.timestamp():.0f}",
                            f"pu-{index}",
                            CHECKOUT_CHARGE[0],
                            CHECKOUT_CHARGE[1],
                            t,
                            {"otel.status_code": "ERROR", "rpc.grpc.status_code": 14},
                        )
                    )
                t += timedelta(seconds=10)
                index += 1
        return SpanQuery(tuple(rows), base.complete)

    def checkout_calls(self, start: datetime, end: datetime, timeout: float):
        return self.lab.checkout_calls(start, end, timeout)

    def metric(
        self, expr: str, start: datetime, end: datetime, timeout: float
    ) -> CallCount:
        self.metric_calls.append((expr, start, end))
        if self.mode == "fail":
            return CallCount(None, False)
        if self.mode == "always":
            return CallCount(HIGH, True)
        if self.mode == "none" or self.noise_flag is None:
            return CallCount(BASELINE, True)
        mid = start + (end - start) / 2
        on = self.lab.effective(self.noise_flag, mid) != "off"
        return CallCount(HIGH if on else BASELINE, True)


def _write(root: Path, sid: str, *, category: str, polarity: str, injectors, signature):
    group = "regression" if category == "regression" else "capability"
    directory = root / group / sid
    directory.mkdir(parents=True)
    scenario = json.loads((PAYMENT_DIR / "scenario.json").read_text())
    scenario.update(scenario_id=sid, entry="question")
    answer = json.loads((PAYMENT_DIR / "answer.json").read_text())
    answer.update(
        scenario_id=sid,
        category=category,
        polarity=polarity,
        injectors=injectors,
        signature=signature,
    )
    (directory / "scenario.json").write_text(json.dumps(scenario))
    (directory / "answer.json").write_text(json.dumps(answer))
    return directory


def make(tmp_path: Path, noise_flag: str | None, mode: str) -> dict[str, Any]:
    root = tmp_path / "dev-v2"
    shutil.copytree(DEV_V2 / "regression", root / "regression")
    (root / "capability").mkdir()
    if noise_flag is not None:
        noise = _flagd(noise_flag)
        _write(
            root,
            NOISE_POSITIVE,
            category="noise",
            polarity="positive",
            injectors=[PAYMENT, noise],
            signature=SIGNATURE,
        )
        _write(
            root,
            NOISE_NEGATIVE,
            category="noise",
            polarity="negative",
            injectors=[noise],
            signature=None,
        )
    _write(
        root,
        UNREACHABLE_POSITIVE,
        category="regression",
        polarity="positive",
        injectors=[UNREACHABLE],
        signature=SIGNATURE,
    )
    load_scenarios_v2(root)
    env = pr_c.make_env(tmp_path)
    env["lab"].raw = ORIGINAL_S6
    telemetry = Telemetry(env["lab"], noise_flag, mode)
    env["telemetry"] = telemetry
    env["deps"] = replace(env["deps"], scenarios_root=root, telemetry=telemetry)
    env["root"] = root
    seen: list[Any] = []
    env["investigator"].action = seen.append
    env["seen"] = seen
    return env


def run(env: dict[str, Any], scenario_id: str):
    slots = eval_live.plan_slots(env["root"], [scenario_id], 1)
    manifest = eval_live.build_manifest(
        "r1", slots, 1, ["t"], scenarios_root=env["root"]
    )
    config = eval_live.RoundConfig("r1", slots, "lead", "b1", 55999, manifest, ("t",))
    return eval_live.run_round(config, env["deps"])


def assert_all_off(env: dict[str, Any]) -> None:
    """Restored and washed: no intent, the original bytes, every flag off in
    the ConfigMap and on the (fake) daemon."""
    lab, now = env["lab"], env["clock"].now()
    assert eval_live_lab.read_state(env["deps"].ledger_root) is None
    assert lab.raw == ORIGINAL_S6
    flags = json.loads(lab.raw)["flags"]
    for flag in ALL_FLAGS:
        assert flags[flag]["defaultVariant"] == "off", flag
        assert lab.effective(flag, now) == "off", flag


def _private(env: dict[str, Any], attempt_id: str) -> Path:
    return eval_live.attempt_ledger_dir(env["deps"], "r1", attempt_id)


def _summary(env: dict[str, Any], attempt_id: str) -> dict[str, Any]:
    run_dir = env["deps"].evidence_root / "r1" / "runs" / attempt_id
    return json.loads((run_dir / "summary.json").read_text())


# -- whitelist ---------------------------------------------------------------------


@pytest.mark.parametrize("flag", NEW_FLAGS)
def test_whitelist_accepts_each_new_switch(flag):
    plan = eval_live_lab.validate_injectors("question", [_ns(_flagd(flag))])
    assert [(f.flag, f.fault_variant, f.normal_variant) for f in plan.flags] == [
        (flag, "on", "off")
    ]
    assert plan.injectors == (
        {"kind": "flagd", "params": {"flag": flag, "variant": "on"}},
    )


@pytest.mark.parametrize(
    "injectors",
    [
        [PAYMENT, _flagd("adManualGc")],
        [PAYMENT, _flagd("loadGeneratorFloodHomepage")],
        [UNREACHABLE, _flagd("adManualGc")],
        [UNREACHABLE, _flagd("loadGeneratorFloodHomepage")],
    ],
)
def test_whitelist_accepts_a_metric_noise_switch_beside_a_payment_fault(injectors):
    plan = eval_live_lab.validate_injectors("question", [_ns(i) for i in injectors])
    assert [f.flag for f in plan.flags] == [i["params"]["flag"] for i in injectors]


@pytest.mark.parametrize("flag", UNSELECTED)
def test_whitelist_refuses_unselected_switches(flag):
    with pytest.raises(LabError) as raised:
        eval_live_lab.validate_injectors("question", [_ns(_flagd(flag))])
    assert raised.value.code == "FAULT_NOT_WHITELISTED"


@pytest.mark.parametrize(
    ("flag", "variant"),
    [
        ("paymentUnreachable", "off"),
        ("adManualGc", "off"),
        ("loadGeneratorFloodHomepage", "off"),
        ("loadGeneratorFloodHomepage", "100"),
        ("adManualGc", "true"),
    ],
)
def test_whitelist_refuses_a_new_switch_with_any_other_variant(flag, variant):
    with pytest.raises(LabError) as raised:
        eval_live_lab.validate_injectors("question", [_ns(_flagd(flag, variant))])
    assert raised.value.code == "FAULT_NOT_WHITELISTED"


def test_v1_fault_table_is_still_the_three_v1_flags():
    # A v1 round must not start depending on the new switches.
    assert set(eval_live_lab.FAULT_FLAGS) == V1_FLAGS


def test_a_dataset_with_an_unselected_switch_is_refused_before_any_slot(tmp_path):
    env = make(tmp_path, None, "none")
    _write(
        env["root"],
        UNSELECTED_ID,
        category="noise",
        polarity="negative",
        injectors=[_flagd("kafkaQueueProblems")],
        signature=None,
    )
    with pytest.raises(eval_live._Usage) as raised:
        eval_live.plan_slots(env["root"], [UNSELECTED_ID], 1)
    assert str(raised.value) == "FAULT_NOT_WHITELISTED"
    assert env["lab"].writes == []


@pytest.mark.parametrize("flag", NEW_FLAGS)
def test_a_round_refuses_to_start_while_a_new_switch_is_not_off(tmp_path, flag):
    env = make(tmp_path, None, "none")
    data = json.loads(ORIGINAL_S6)
    data["flags"][flag]["defaultVariant"] = "on"
    before = (json.dumps(data, indent=4) + "\n").encode()
    env["lab"].raw = before
    result = run(env, UNREACHABLE_POSITIVE)
    assert (result.status, result.reason) == ("refused", "FLAGS_NOT_NORMAL")
    assert env["seen"] == []
    assert env["lab"].raw == before  # never touched
    assert env["lab"].writes == []


# -- paymentUnreachable: positive base fault, payment signature, no noise gate -----


def test_payment_unreachable_positive_submits_under_the_payment_signature(tmp_path):
    # mode "none": no metric signal anywhere; paymentUnreachable has no noise
    # gate, so this must not block it.
    env = make(tmp_path, None, "none")
    result = run(env, UNREACHABLE_POSITIVE)
    (slot,) = result.slots
    assert slot.status != "setup_failed", slot.reason
    (request,) = env["seen"]
    gate = request.pre_observation
    assert gate.passed
    assert {(s.service, s.operation) for s in gate.matched} <= {
        (m["service"], m["operation"]) for m in SIGNATURE["any_of"]
    }
    assert gate.matched
    private = _private(env, slot.attempt_id)
    assert not list(private.glob("pre-observation-noise-*.json"))
    assert_all_off(env)


# -- metric noise gate -------------------------------------------------------------


@pytest.mark.parametrize("flag", METRIC_NOISE)
@pytest.mark.parametrize("scenario", [NOISE_POSITIVE, NOISE_NEGATIVE])
@pytest.mark.parametrize("mode", ["none", "always", "fail"])
def test_metric_noise_not_observed_is_setup_failed_and_restored(
    tmp_path, flag, scenario, mode
):
    # none: the signal never leaves baseline; always: it is already high
    # before the injection (not caused by the switch); fail: every metric
    # query fails.  None of these is "noise observed".
    env = make(tmp_path, flag, mode)
    result = run(env, scenario)
    (slot,) = result.slots
    assert (slot.status, slot.reason) == ("setup_failed", "NOISE_NOT_OBSERVED")
    assert env["seen"] == []  # never investigated
    assert env["telemetry"].metric_calls  # the gate read the metric seam
    assert_all_off(env)
    assert _summary(env, slot.attempt_id)["capture"]["attempt"]["status"] == (
        "setup_failed"
    )
    raw = json.loads(
        (
            _private(env, slot.attempt_id) / f"pre-observation-noise-{flag}.json"
        ).read_text()
    )
    assert raw


@pytest.mark.parametrize("flag", METRIC_NOISE)
@pytest.mark.parametrize("scenario", [NOISE_POSITIVE, NOISE_NEGATIVE])
def test_metric_noise_observed_submits_the_run_and_restores(tmp_path, flag, scenario):
    env = make(tmp_path, flag, "seen")
    result = run(env, scenario)
    (slot,) = result.slots
    assert slot.status != "setup_failed", slot.reason
    (request,) = env["seen"]
    assert request.pre_observation.passed
    if scenario == NOISE_POSITIVE:
        assert request.pre_observation.matched  # the payment signature was seen
    calls = env["telemetry"].metric_calls
    assert calls
    # Every metric read of the gate finished before the Run was submitted.
    assert max(end for _, _, end in calls) <= request.submit_not_after
    assert (
        _private(env, slot.attempt_id) / f"pre-observation-noise-{flag}.json"
    ).is_file()
    assert_all_off(env)


# -- answer schema unchanged (R3) --------------------------------------------------


def test_new_switch_answers_load_under_the_unchanged_v2_schema(tmp_path):
    env = make(tmp_path, "adManualGc", "none")
    loaded = {
        spec.scenario_id: answer for spec, answer in load_scenarios_v2(env["root"])
    }
    positive = loaded[UNREACHABLE_POSITIVE]
    assert positive.signature is not None
    assert positive.signature.rule_id == SIGNATURE["rule_id"]
    assert [(m.service, m.operation) for m in positive.signature.any_of] == [
        (m["service"], m["operation"]) for m in SIGNATURE["any_of"]
    ]
    assert loaded[NOISE_NEGATIVE].signature is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a.update(noise_signal={"metric": "jvm_gc_duration_seconds_sum"}),
        lambda a: a["signature"].update(metric="jvm_gc_duration_seconds_sum"),
        lambda a: a["signature"].update(kind="metric"),
    ],
    ids=["answer-noise-field", "signature-metric-field", "signature-kind-field"],
)
def test_no_metric_signature_type_is_accepted_in_answers(tmp_path, mutate):
    root = tmp_path / "dev-v2"
    shutil.copytree(DEV_V2 / "regression", root / "regression")
    (root / "capability").mkdir()
    directory = _write(
        root,
        NOISE_POSITIVE,
        category="noise",
        polarity="positive",
        injectors=[PAYMENT, _flagd("adManualGc")],
        signature=json.loads(json.dumps(SIGNATURE)),
    )
    load_scenarios_v2(root)  # the plain answer loads
    answer = json.loads((directory / "answer.json").read_text())
    mutate(answer)
    (directory / "answer.json").write_text(json.dumps(answer))
    with pytest.raises(ScenarioError) as raised:
        load_scenarios_v2(root)
    assert "UNEXPECTED_FIELDS" in str(raised.value)
