"""Independent acceptance tests for #432: the noise-positive pre-observation gate (seams §6).

The round runs through the public driver (``eval_live.run_round``) over the
offline lab doubles used by the PR-C driver tests: an in-memory flagd
ConfigMap, a fake clock, and a telemetry double.  The double here adds
``ad`` error spans on demand, so the tests control whether the noise is
observed and when.  No kind, kubectl, PG or model is touched.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from opspilot.evaluation import load_scenarios_v2
from scripts import eval_live, eval_live_lab
from scripts.eval_live_lab import ObservedSpan, SpanQuery
from tests.unit import test_eval_c_driver_impl as pr_c

ROOT = Path(__file__).resolve().parents[2]
DEV_V2 = ROOT / "evals" / "datasets" / "dev-v2"
_FLAGS = json.loads(pr_c.ORIGINAL)
_FLAGS["flags"]["adFailure"] = {
    "state": "ENABLED",
    "variants": {"on": True, "off": False},
    "defaultVariant": "off",
}
# F1 S6 (prereview U2): the whitelist's holdout flags, as the lab ConfigMap
# defines them (all off; the flood flag's variants are numbers).
for _name in ("paymentUnreachable", "adManualGc"):
    _FLAGS["flags"][_name] = {
        "state": "ENABLED",
        "variants": {"on": True, "off": False},
        "defaultVariant": "off",
    }
_FLAGS["flags"]["loadGeneratorFloodHomepage"] = {
    "state": "ENABLED",
    "variants": {"on": 100, "off": 0},
    "defaultVariant": "off",
}
ORIGINAL_V2 = (json.dumps(_FLAGS, indent=4) + "\n").encode()
PAYMENT = {"kind": "flagd", "params": {"flag": "paymentFailure", "variant": "100%"}}
AD = {"kind": "flagd", "params": {"flag": "adFailure", "variant": "on"}}
SIGNATURE = json.loads((DEV_V2 / "regression/dv2-2b214b/answer.json").read_text())[
    "signature"
]
AD_SERVICE = "ad"
AD_OPERATION = "oteldemo.AdService/GetAds"
NOISE_POSITIVE = "dv2-a0a001"
NOISE_NEGATIVE = "dv2-a0a002"
FRESHNESS = timedelta(seconds=300)
# Payment error spans start this long after paymentFailure is effective, so
# an ad span right at the start of the noise observation is strictly older.
PAYMENT_LAG = timedelta(seconds=60)


def _api(name: str) -> Any:
    for module in (eval_live, eval_live_lab):
        value = getattr(module, name, None)
        if value is not None:
            return value
    pytest.fail(f"#432 public seam is missing: {name}")


class Telemetry:
    """pr_c's lab telemetry, plus ad error spans in one of three modes:
    ``none`` (no ad error span ever), ``early`` (one span 1 s after the
    first ad query that starts once adFailure is effective), ``late`` (a
    span at the end of every ad query while adFailure is effective)."""

    def __init__(self, lab: pr_c.Lab, mode: str) -> None:
        self.lab = lab
        self.mode = mode
        self.early_at: datetime | None = None
        self.last_ad: datetime | None = None  # newest ad error span returned

    def _effective_since(self, flag: str) -> datetime | None:
        for when, variant in self.lab.history.get(flag, []):
            if variant != "off":
                return when
        return None

    def spans(
        self, service: str, start: datetime, end: datetime, timeout: float
    ) -> SpanQuery:
        base = self.lab.spans(service, start, end, timeout)
        rows = list(base.spans)
        paid = self._effective_since("paymentFailure")
        if paid is not None:
            rows = [
                row
                for row in rows
                if row.tags.get("otel.status_code") != "ERROR"
                or row.start >= paid + PAYMENT_LAG
            ]
        if service == AD_SERVICE:
            rows.extend(self._ad(start, end))
        return SpanQuery(tuple(rows), base.complete)

    def _ad(self, start: datetime, end: datetime) -> list[ObservedSpan]:
        on = self._effective_since("adFailure")
        if self.mode == "none" or on is None:
            return []
        if self.mode == "early":
            if self.early_at is None and start >= on:
                self.early_at = start + timedelta(seconds=1)
            at = self.early_at
            if at is None or not start <= at <= end:
                return []
        else:
            at = end
        if self.lab.effective("adFailure", at) == "off":
            return []
        if self.last_ad is None or at > self.last_ad:
            self.last_ad = at
        return [
            ObservedSpan(
                "ad-trace",
                f"ad-{at.timestamp():.0f}",
                AD_SERVICE,
                AD_OPERATION,
                at,
                {"otel.status_code": "ERROR"},
            )
        ]

    def checkout_calls(self, start: datetime, end: datetime, timeout: float):
        return self.lab.checkout_calls(start, end, timeout)


def _scenario(root: Path, sid: str, injectors, signature, polarity: str) -> None:
    directory = root / "capability" / sid
    directory.mkdir(parents=True)
    scenario = json.loads((DEV_V2 / "regression/dv2-2b214b/scenario.json").read_text())
    scenario.update(scenario_id=sid, entry="question")
    answer = json.loads((DEV_V2 / "regression/dv2-2b214b/answer.json").read_text())
    answer.update(
        scenario_id=sid,
        category="noise",
        polarity=polarity,
        injectors=injectors,
        signature=signature,
    )
    (directory / "scenario.json").write_text(json.dumps(scenario))
    (directory / "answer.json").write_text(json.dumps(answer))


def make(tmp_path: Path, mode: str) -> dict[str, Any]:
    root = tmp_path / "dev-v2"
    shutil.copytree(DEV_V2 / "regression", root / "regression")
    (root / "capability").mkdir()
    _scenario(root, NOISE_POSITIVE, [PAYMENT, AD], SIGNATURE, "positive")
    _scenario(root, NOISE_NEGATIVE, [AD], None, "negative")
    load_scenarios_v2(root)
    env = pr_c.make_env(tmp_path)
    env["lab"].raw = ORIGINAL_V2
    telemetry = Telemetry(env["lab"], mode)
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


def test_noise_rule_constant():
    rules = _api("NOISE_RULES")
    rule = rules["adFailure"]
    assert rule.rule_id == "ad-getads-error-span-v1"
    assert [(m.service, m.operation) for m in rule.any_of] == [
        (AD_SERVICE, AD_OPERATION)
    ]
    assert rule.min_matches == 1


def test_no_ad_error_span_is_setup_failed_noise_not_observed_and_restored(tmp_path):
    env = make(tmp_path, "none")
    deps = env["deps"]
    result = run(env, NOISE_POSITIVE)
    (slot,) = result.slots
    assert slot.status == "setup_failed"
    assert slot.reason == "NOISE_NOT_OBSERVED"
    assert env["seen"] == []  # never investigated
    # Restored and washed: the intent is gone and every flag is back.
    assert eval_live_lab.read_state(deps.ledger_root) is None
    assert env["lab"].raw == ORIGINAL_V2
    assert env["lab"].effective("adFailure", env["clock"].now()) == "off"
    assert env["lab"].effective("paymentFailure", env["clock"].now()) == "off"
    run_dir = deps.evidence_root / "r1" / "runs" / slot.attempt_id
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["capture"]["attempt"]["status"] == "setup_failed"
    private = eval_live.attempt_ledger_dir(deps, "r1", slot.attempt_id)
    # §10 A4: the noise gate's raw observation is its own private file.
    noise = json.loads((private / "pre-observation-noise-adFailure.json").read_text())
    assert noise


def test_an_observed_ad_error_span_proceeds_to_the_investigation(tmp_path):
    env = make(tmp_path, "late")
    result = run(env, NOISE_POSITIVE)
    (slot,) = result.slots
    assert slot.reason != "NOISE_NOT_OBSERVED"
    assert len(env["seen"]) == 1
    assert eval_live_lab.read_state(env["deps"].ledger_root) is None
    assert env["lab"].raw == ORIGINAL_V2


def _payment_deadline(request: Any) -> datetime:
    matched = [s for s in request.pre_observation.matched if s.service != AD_SERVICE]
    assert matched
    return max(span.start for span in matched) + FRESHNESS


def test_submit_deadline_follows_an_older_ad_span(tmp_path):
    env = make(tmp_path, "early")
    run(env, NOISE_POSITIVE)
    (request,) = env["seen"]
    early = env["telemetry"].early_at
    assert early is not None
    # The ad span is older than the newest payment span: its +300 s wins.
    assert early + FRESHNESS < _payment_deadline(request)
    assert request.submit_not_after == early + FRESHNESS


def test_submit_deadline_keeps_the_signature_gate_when_ad_is_newer(tmp_path):
    env = make(tmp_path, "late")
    run(env, NOISE_POSITIVE)
    (request,) = env["seen"]
    assert request.submit_not_after == _payment_deadline(request)


# -- noise negative (lead ruling A16, 2026-11-02) ----------------------------------
#
# A16: the noise negative (adFailure only, signature null) must observe its noise
# before submit.  After the flag is effective it waits (600 s budget) for >= 1
# ad GetAds error span, else setup_failed / NOISE_NOT_OBSERVED (a Q20 failure
# under D1); then the trailing 300 s normal window must be clean on the fault
# signatures (ad errors do not make it unclean); and
# submit_not_after = min(window end + 300 s, newest ad error + 300 s).
# This replaces test_noise_negative_does_not_wait_for_ad_errors.


def test_noise_negative_without_ad_errors_is_noise_not_observed(tmp_path):
    # A16: no ad error span -> setup failed, never investigated, lab restored.
    env = make(tmp_path, "none")
    deps = env["deps"]
    result = run(env, NOISE_NEGATIVE)
    (slot,) = result.slots
    assert slot.status == "setup_failed"
    assert slot.reason == "NOISE_NOT_OBSERVED"
    assert env["seen"] == []
    assert eval_live_lab.read_state(deps.ledger_root) is None
    assert env["lab"].raw == ORIGINAL_V2
    assert env["lab"].effective("adFailure", env["clock"].now()) == "off"
    run_dir = deps.evidence_root / "r1" / "runs" / slot.attempt_id
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["capture"]["attempt"]["status"] == "setup_failed"


@pytest.mark.parametrize("mode", ["early", "late"])
def test_noise_negative_with_ad_errors_submits_with_the_earlier_deadline(
    tmp_path, mode
):
    # A16: ad error seen -> clean trailing 300 s normal window -> submit with
    # submit_not_after = min(window end + 300 s, newest ad error + 300 s).
    env = make(tmp_path, mode)
    result = run(env, NOISE_NEGATIVE)
    (slot,) = result.slots
    assert slot.reason != "NOISE_NOT_OBSERVED"
    (request,) = env["seen"]
    newest_ad = env["telemetry"].last_ad
    assert newest_ad is not None
    gate = request.pre_observation
    assert gate.passed
    assert gate.window.end - gate.window.start == FRESHNESS
    assert gate.window.start >= gate.flag_confirmed_at
    assert request.submit_not_after == min(
        gate.window.end + FRESHNESS, newest_ad + FRESHNESS
    )
    assert eval_live_lab.read_state(env["deps"].ledger_root) is None
    assert env["lab"].raw == ORIGINAL_V2


def test_noise_negative_with_a_dirty_signature_window_is_still_refused(tmp_path):
    # A16 keeps the normal-window rule: ad errors are observed, but fault
    # signature errors in the trailing window still refuse the attempt.
    env = make(tmp_path, "late")
    lab = env["lab"]
    real = lab.spans

    def dirty_while_ad_is_on(service, start, end, timeout):
        # A payment-signature error span while adFailure is on; clean after
        # restore, so the post-restore wash can still confirm.
        query = real(service, start, end, timeout)
        if lab.effective("adFailure", end) == "off":
            return query
        extra = tuple(
            ObservedSpan(
                "dirty-trace",
                f"dirty-{end.timestamp():.0f}",
                m["service"],
                m["operation"],
                end,
                {"otel.status_code": "ERROR"},
            )
            for m in SIGNATURE["any_of"]
            if m["service"] == service
        )
        return SpanQuery((*query.spans, *extra), query.complete)

    lab.spans = dirty_while_ad_is_on  # type: ignore[method-assign]
    result = run(env, NOISE_NEGATIVE)
    (slot,) = result.slots
    assert (slot.status, slot.reason) == ("setup_failed", "NORMAL_WINDOW_UNCONFIRMED")
    assert env["seen"] == []
    assert env["telemetry"].last_ad is not None  # the noise itself was seen
    assert eval_live_lab.read_state(env["deps"].ledger_root) is None
    assert env["lab"].raw == ORIGINAL_V2
