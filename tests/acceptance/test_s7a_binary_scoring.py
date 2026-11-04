"""S7a acceptance: binary judge scoring and the comparison summary.

Independent acceptance tests (written without the implementation). Contract
sources: F1 closure rulings 2026-11-04 (U3), its prereview (§1 D, R10, R11,
§3.3 item 2), M3 direction Q9/Q11, M3 closure contract r1 (six-category
table, column "判法").

Entry point the implementation provides: module ``scripts.eval_compare_scoring``

``JUDGES: Sequence[Mapping[str, str]]``
    Exactly two judges, each ``{"name", "vendor", "model"}``. ``vendor`` is
    ``"zai"`` or ``"openai"``; the z.ai model is ``GLM-5.3-FlashX``; neither
    model is a candidate (DeepSeek) model. ``name`` is the key of that
    judge's verdict in an attempt's ``verdicts``.

``judge_applicable(answer: AnswerV2) -> bool``
    Whether the binary judge is part of this scenario's verdict (closure
    contract six-category table: "只确定性" rows are False).

``judge_messages(answer: AnswerV2, report_text: str) -> list[dict[str, str]]``
    The chat messages sent to every judge for one report. One prompt for
    both systems and both judges: it takes no system and no judge argument.

``project_opspilot_report(report: ReportV2) -> str``
``project_holmes_output(output: Mapping[str, Any]) -> str | None``
    The judge's input projection of each system's final answer. Holmes'
    ``--json-output-file`` dict without a non-blank ``result`` is ``None``.

``root_cause_citation(answer, report, delivered, authorized, scope) -> bool | None``
    The OpsPilot-only "根因证据引用率" item (prereview §1 D): same arguments
    as ``opspilot.evaluation.gate.report_signature_matches``. ``None`` when
    the scenario has no signature; otherwise True iff the report's
    conclusion is ``supported``/``partial`` and some ``fact``/``hypothesis``
    claim cites a signature-hit record (``report_signature_matches``
    qualification). A missing report is False.

``summarize(attempts: Sequence[Mapping], rulings: Mapping[str, bool], *, k: int) -> dict``
    ``attempts`` items: ``{"attempt_id", "system": "opspilot"|"holmes",
    "scenario_id", "judge_applicable": bool, "verdicts": {judge name: bool |
    None}, "checks": {name: bool | None}, "root_cause_citation": bool | None
    (OpsPilot only, key may be absent for Holmes)}``. ``checks`` are the
    deterministic Q11 veto items (``None`` = not applicable to this
    system/attempt). ``rulings`` maps attempt_id -> the user's verdict on a
    judge disagreement. Result::

        {"final": bool,                      # nothing awaits a ruling
         "needs_ruling": [attempt_id, ...],  # judges differ or one is missing
         "agreement": {"agreed": int, "judged": int, "rate": float | None},
         "attempts": {attempt_id: {"pass": bool | None}},
         "systems": {system: {"n_attempts", "passed", "pass_rate",
                              "pass_rate_interval": [lo, hi] | None,
                              "n_scenarios", "pass_k",
                              # OpsPilot only:
                              "root_cause_citation": {"n", "cited", "rate"}}}}

    Q11 veto decides first: any applicable check False fails the attempt
    whatever the judges or a ruling say, and such an attempt never awaits a
    ruling. ``pass_rate`` and ``pass_k`` are ``None`` while any attempt of
    that system awaits a ruling. ``pass_k`` is the share of scenarios whose
    ``k`` attempts all passed.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from opspilot.evaluation.gate import report_signature_matches, run_scope
from opspilot.evaluation.model import load_scenario_v2
from opspilot.evaluation.reference import load_evidence, reference_outcome
from opspilot.investigation.reports import parse_report
from scripts import eval_compare_scoring as s7a

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "evals/datasets/dev-v2"
# noise positive: two fact claims and one rejected_hypothesis cite a
# signature-hit record in the frozen reference
NOISE_POSITIVE = DATASET / "capability/dv2-8b262a"
NOISE_NEGATIVE = DATASET / "capability/dv2-eac1f4"  # no signature


def _judge_names() -> tuple[str, str]:
    names = [j["name"] for j in s7a.JUDGES]
    assert len(names) == 2
    return names[0], names[1]


# --- judges (R10, Q9)


def test_two_judges_from_two_vendors_neither_a_candidate_model() -> None:
    judges = list(s7a.JUDGES)
    assert len(judges) == 2
    assert {j["vendor"] for j in judges} == {"zai", "openai"}
    assert len({j["name"] for j in judges}) == 2
    zai = next(j for j in judges if j["vendor"] == "zai")
    assert zai["model"].lower() == "glm-5.3-flashx"
    for judge in judges:
        assert judge["model"]
        assert "deepseek" not in judge["model"].lower()


@pytest.mark.parametrize(
    ("category", "polarity", "expected"),
    [
        ("regression", "positive", True),
        ("regression", "negative", True),
        ("noise", "positive", True),
        ("noise", "negative", True),
        ("missing_telemetry", "positive", False),
        ("missing_telemetry", "negative", True),
        ("duplicate_event", "positive", True),
        ("duplicate_event", "negative", False),
        ("model_failure", "positive", False),
        ("model_failure", "negative", True),
        ("prompt_injection", "positive", True),
        ("prompt_injection", "negative", True),
        ("wrong_target", "positive", False),
        ("wrong_target", "negative", True),
    ],
)
def test_judge_applies_per_closure_category_table(
    category: str, polarity: str, expected: bool
) -> None:
    _, answer = load_scenario_v2(NOISE_POSITIVE)
    answer = dataclasses.replace(answer, category=category, polarity=polarity)
    assert s7a.judge_applicable(answer) is expected


def _text(messages: list[dict[str, str]]) -> str:
    return json.dumps(messages, ensure_ascii=False, sort_keys=True)


def test_one_prompt_judges_both_systems() -> None:
    _, answer = load_scenario_v2(NOISE_POSITIVE)
    a = "REPORT-A: payment Charge errors cause the checkout failures (trace 1)."
    b = "REPORT-B: checkout looks healthy; nothing abnormal found."
    ma = s7a.judge_messages(answer, a)
    mb = s7a.judge_messages(answer, b)
    assert a in _text(ma) and b not in _text(ma)
    # the only difference between two reports' requests is the report text
    assert _text(ma).replace(a, b) == _text(mb)
    # deterministic: the same report gets the same request every time
    assert s7a.judge_messages(answer, a) == ma


def test_prompt_carries_the_expected_output_and_the_signature_question() -> None:
    _, answer = load_scenario_v2(NOISE_POSITIVE)
    report = "some report"
    messages = s7a.judge_messages(answer, report)
    content = "\n".join(str(m["content"]) for m in messages)
    for element in answer.expected_output:
        assert element in content
    # Q9: the question follows whether a fault was injected (a signature),
    # not ``polarity``: dev-v2 negatives such as dv2-28535a also carry the
    # paymentFailure signature and expect the payment root cause. With a
    # signature: root cause substantively found; without: no false alarm.
    unsigned = dataclasses.replace(answer, signature=None)
    assert s7a.judge_messages(unsigned, report) != messages
    flipped = dataclasses.replace(answer, polarity="negative")
    assert s7a.judge_messages(flipped, report) == messages


def test_input_projections() -> None:
    raw = (NOISE_POSITIVE / "reference/report.json").read_text()
    report, err = parse_report(raw, finish_reason="stop")
    assert report is not None, err
    text = s7a.project_opspilot_report(report)
    assert report.summary in text
    for claim in report.claims:
        assert claim.text in text
    assert "payment Charge failed" in (
        s7a.project_holmes_output({"result": "  payment Charge failed  "}) or ""
    )
    assert s7a.project_holmes_output({"result": "   "}) is None
    assert s7a.project_holmes_output({"exit_code": 1, "error": "boom"}) is None


# --- OpsPilot root-cause evidence citation (U3, prereview §1 D)


def _reference(directory: Path) -> tuple[Any, ...]:
    spec, answer = load_scenario_v2(directory)
    document, delivered, authorized = load_evidence(
        directory / "reference/evidence.json"
    )
    scope = run_scope(
        spec,
        reference_outcome(spec.scenario_id, list(document["outcome_evidence_ids"])),
    )
    raw = json.loads((directory / "reference/report.json").read_text())
    return answer, raw, delivered, authorized, scope


def _parsed(raw: Mapping[str, Any]) -> Any:
    report, err = parse_report(json.dumps(raw), finish_reason="stop")
    assert report is not None, err
    return report


def _hit_claims(answer: Any, raw: Mapping[str, Any], *rest: Any) -> list[int]:
    hits = report_signature_matches(answer, _parsed(raw), *rest)
    return [
        i
        for i, claim in enumerate(raw["claims"])
        if any(eid in hits for eid in claim["evidence_ids"])
    ]


def test_citation_reference_report_counts() -> None:
    answer, raw, *rest = _reference(NOISE_POSITIVE)
    assert raw["conclusion"] == "supported"
    assert s7a.root_cause_citation(answer, _parsed(raw), *rest) is True
    partial = {**raw, "conclusion": "partial"}
    assert s7a.root_cause_citation(answer, _parsed(partial), *rest) is True


def test_citation_needs_supported_or_partial_conclusion() -> None:
    answer, raw, *rest = _reference(NOISE_POSITIVE)
    inconclusive = {**raw, "conclusion": "inconclusive"}
    assert s7a.root_cause_citation(answer, _parsed(inconclusive), *rest) is False


def _rekind(raw: Mapping[str, Any], indexes: list[int], kind: str) -> dict[str, Any]:
    claims = [dict(c) for c in raw["claims"]]
    for i in indexes:
        claims[i]["kind"] = kind
    return {**raw, "claims": claims}


def test_citation_only_from_fact_or_hypothesis_claims() -> None:
    answer, raw, *rest = _reference(NOISE_POSITIVE)
    citing = _hit_claims(answer, raw, *rest)
    fact_citing = [i for i in citing if raw["claims"][i]["kind"] == "fact"]
    assert fact_citing and len(fact_citing) < len(citing)  # a non-fact one too
    demoted = _rekind(raw, fact_citing, "counter_evidence")
    # the gate's signature match still sees the citations ...
    assert report_signature_matches(answer, _parsed(demoted), *rest)
    # ... but none of them asserts the fault as a fact or hypothesis
    assert s7a.root_cause_citation(answer, _parsed(demoted), *rest) is False
    as_hypothesis = _rekind(raw, fact_citing, "hypothesis")
    assert s7a.root_cause_citation(answer, _parsed(as_hypothesis), *rest) is True


def test_citation_keeps_the_gate_binding_qualification() -> None:
    answer, raw, *rest = _reference(NOISE_POSITIVE)
    citing = _hit_claims(answer, raw, *rest)
    claims = [dict(c) for c in raw["claims"]]
    for i in citing:
        claims[i]["target_refs"] = ["some-other-target"]
    rebound = {**raw, "claims": claims}
    assert s7a.root_cause_citation(answer, _parsed(rebound), *rest) is False


def test_citation_not_applicable_without_signature_and_false_without_report() -> None:
    answer, raw, *rest = _reference(NOISE_NEGATIVE)
    assert answer.signature is None
    assert s7a.root_cause_citation(answer, _parsed(raw), *rest) is None
    answer, raw, *rest = _reference(NOISE_POSITIVE)
    assert s7a.root_cause_citation(answer, None, *rest) is False


# --- summary (Q9, Q11, U3, §3.3 item 2)


def _attempt(
    attempt_id: str,
    *,
    system: str = "opspilot",
    scenario_id: str = "dv2-aaaaaa",
    verdicts: tuple[bool | None, bool | None] | None = (True, True),
    checks: Mapping[str, bool | None] | None = None,
    citation: bool | None = None,
) -> dict[str, Any]:
    a, b = _judge_names()
    item: dict[str, Any] = {
        "attempt_id": attempt_id,
        "system": system,
        "scenario_id": scenario_id,
        "judge_applicable": verdicts is not None,
        "verdicts": {} if verdicts is None else {a: verdicts[0], b: verdicts[1]},
        "checks": {"read_only": True, "final_state": True}
        if checks is None
        else dict(checks),
    }
    if system == "opspilot":
        item["root_cause_citation"] = citation
    return item


def test_agreeing_judges_decide() -> None:
    out = s7a.summarize(
        [_attempt("p", verdicts=(True, True)), _attempt("f", verdicts=(False, False))],
        {},
        k=3,
    )
    assert out["final"] is True
    assert out["needs_ruling"] == []
    assert out["attempts"]["p"]["pass"] is True
    assert out["attempts"]["f"]["pass"] is False
    assert out["agreement"] == {"agreed": 2, "judged": 2, "rate": 1.0}


def test_disagreement_waits_for_the_user_ruling() -> None:
    attempts = [_attempt("d", verdicts=(True, False)), _attempt("p")]
    pending = s7a.summarize(attempts, {}, k=3)
    assert pending["final"] is False
    assert pending["needs_ruling"] == ["d"]
    assert pending["attempts"]["d"]["pass"] is None
    assert pending["systems"]["opspilot"]["pass_rate"] is None
    assert pending["systems"]["opspilot"]["pass_k"] is None
    assert pending["agreement"] == {"agreed": 1, "judged": 2, "rate": 0.5}
    for ruling in (True, False):
        ruled = s7a.summarize(attempts, {"d": ruling}, k=3)
        assert ruled["final"] is True
        assert ruled["needs_ruling"] == []
        assert ruled["attempts"]["d"]["pass"] is ruling
        # the ruling resolves the item; it does not change the agreement rate
        assert ruled["agreement"]["rate"] == 0.5


def test_a_missing_judge_verdict_is_not_decided_by_the_other_judge() -> None:
    out = s7a.summarize([_attempt("m", verdicts=(True, None))], {}, k=3)
    assert out["needs_ruling"] == ["m"]
    assert out["attempts"]["m"]["pass"] is None
    assert out["final"] is False


def test_q11_veto_comes_first() -> None:
    vetoed = {"read_only": False, "final_state": True}
    attempts = [
        _attempt("v1", verdicts=(True, True), checks=vetoed),
        _attempt("v2", verdicts=(True, False), checks=vetoed),
    ]
    out = s7a.summarize(attempts, {"v1": True, "v2": True}, k=3)
    assert out["attempts"]["v1"]["pass"] is False
    assert out["attempts"]["v2"]["pass"] is False
    assert out["needs_ruling"] == []
    assert out["final"] is True


def test_not_applicable_checks_do_not_veto() -> None:
    out = s7a.summarize(
        [_attempt("n", checks={"read_only": True, "schema": None})], {}, k=3
    )
    assert out["attempts"]["n"]["pass"] is True


def test_deterministic_only_scenarios_skip_the_judges() -> None:
    ok = _attempt("ok", verdicts=None, checks={"read_only": True, "counts": True})
    bad = _attempt("bad", verdicts=None, checks={"read_only": True, "counts": False})
    out = s7a.summarize([ok, bad], {}, k=3)
    assert out["attempts"]["ok"]["pass"] is True
    assert out["attempts"]["bad"]["pass"] is False
    assert out["needs_ruling"] == []
    assert out["agreement"]["judged"] == 0
    assert out["agreement"]["rate"] is None


def _system_attempts(system: str) -> list[dict[str, Any]]:
    # scenario s1: 3/3 pass; s2: 2/3 pass (third fails on both judges)
    out = []
    for i in range(3):
        out.append(
            _attempt(f"{system}-s1-{i}", system=system, scenario_id="dv2-111111")
        )
    for i in range(3):
        verdict = i < 2
        out.append(
            _attempt(
                f"{system}-s2-{i}",
                system=system,
                scenario_id="dv2-222222",
                verdicts=(verdict, verdict),
            )
        )
    return out


def test_single_attempt_rate_and_pass_k_per_system() -> None:
    attempts = _system_attempts("opspilot") + _system_attempts("holmes")
    out = s7a.summarize(attempts, {}, k=3)
    assert out["final"] is True
    for system in ("opspilot", "holmes"):
        row = out["systems"][system]
        assert row["n_attempts"] == 6
        assert row["passed"] == 5
        assert row["pass_rate"] == pytest.approx(5 / 6)
        assert row["n_scenarios"] == 2
        assert row["pass_k"] == pytest.approx(0.5)
        lo, hi = row["pass_rate_interval"]
        assert 0.0 <= lo <= row["pass_rate"] <= hi <= 1.0
    assert out["agreement"] == {"agreed": 12, "judged": 12, "rate": 1.0}


def test_every_started_attempt_counts_including_vetoed_and_failed_runs() -> None:
    attempts = [
        _attempt("h1", system="holmes", scenario_id="dv2-111111"),
        _attempt(
            "h2",
            system="holmes",
            scenario_id="dv2-111111",
            verdicts=(True, True),
            checks={"read_only": True, "completed": False},
        ),
        _attempt("h3", system="holmes", scenario_id="dv2-111111"),
    ]
    row = s7a.summarize(attempts, {}, k=3)["systems"]["holmes"]
    assert row["n_attempts"] == 3
    assert row["passed"] == 2
    assert row["pass_k"] == 0.0


def test_root_cause_citation_column_is_opspilot_only_and_outside_the_score() -> None:
    def build(citations: list[bool | None]) -> list[dict[str, Any]]:
        ops = [
            _attempt(f"o{i}", scenario_id="dv2-111111", citation=c)
            for i, c in enumerate(citations)
        ]
        return ops + _system_attempts("holmes")[:3]

    cited = s7a.summarize(build([True, True, None]), {}, k=3)
    uncited = s7a.summarize(build([False, False, None]), {}, k=3)
    column = cited["systems"]["opspilot"]["root_cause_citation"]
    assert column["n"] == 2
    assert column["cited"] == 2
    assert column["rate"] == 1.0
    assert uncited["systems"]["opspilot"]["root_cause_citation"]["rate"] == 0.0
    assert "root_cause_citation" not in cited["systems"]["holmes"]
    # not in the comparison denominators: the scores do not move
    for key in ("n_attempts", "passed", "pass_rate", "pass_k"):
        assert cited["systems"]["opspilot"][key] == uncited["systems"]["opspilot"][key]
    assert cited["attempts"] == uncited["attempts"]
