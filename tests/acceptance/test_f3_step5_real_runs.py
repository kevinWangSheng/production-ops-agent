"""F3 acceptance over the frozen real-Run evidence of PR-3b (steps 1, 2, 3, 5, 6).

The two kind Runs of step 5 (U4-a) are also the Runs steps 1, 2 and 6 are
judged on (pre-review section 1 table); step 3's D9 rerun of scenario A and
step 6's R10 authorization probe are frozen next to them. This module applies
the judge in ``tests/acceptance/test_f3_step5_rubric_gates.py`` to those
frozen files. It reads only files under ``docs/evidence/`` and ``evals/``.

Red until PR-3b freezes the evidence: a missing file fails (never skips),
because a step without its evidence has not passed. A change to the
model-visible surface normally voids step-5 evidence; the only exception is
an already registered change with an attached equivalence proof (user ruling
2026-10-26, #319). Paths and the record shape are ASSUMED as listed in the
judge module's docstring.

Not decided here (reviewer judgements, U3-a; recorded in each Run's
``run.md``): rubric items 5-8 and the step-2 summary-consistency check. Per
U-A (ruled 2026-10-24) only Runs that produced no valid report may be
replaced; a valid Run the rubric fails stays as ``k/n`` and ``passes`` is the
user's call -- so the two summaries here must be the two counted Runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from opspilot.investigation.loop import investigation_versions
from opspilot.redaction import redact_credentials, redact_json_bytes
from opspilot.tools.otel_demo import otel_demo_versions
from opspilot.tools.outcomes import PROJECTION_REVISION
from tests.acceptance.test_f3_step5_rubric_gates import (
    ANSWER_SHA256,
    MISSING_SOURCE_ANSWER_SHA256,
    ROOT,
    RUBRIC_SHA256,
    SCENARIO_ID,
    STIMULUS_SHA256,
    gate4_problems,
    probe_problems,
    registered_verbs,
    rubric_gate_problems,
    scenario,
    scenario_shape_problems,
    step1_problems,
    step2_problems,
)

STEP5 = ROOT / "docs/evidence/f3-step5-competing-cause"
LIVE = ROOT / "docs/evidence/f3-acceptance-live"
PROBE = ROOT / "docs/evidence/f3-step6-authz-probe/probe.json"
STEP3 = ROOT / "docs/evidence/f3-step3-coverage"
RUNS = ("run-1", "run-2")


def _summary(run: str) -> dict[str, Any]:
    path = STEP5 / run / "summary.json"
    assert path.is_file(), f"step-5 evidence not frozen yet: {path.relative_to(ROOT)}"
    return json.loads(path.read_text())


def _record(run: str) -> dict[str, Any]:
    summary = _summary(run)
    assert "f3_acceptance" in summary, "summary has no f3_acceptance record"
    record = summary["f3_acceptance"]
    assert record["scenario_id"] == SCENARIO_ID
    return record


def _equivalence() -> dict[str, Any]:
    path = STEP5 / "v11-equivalence.json"
    assert path.is_file(), (
        f"surface equivalence proof missing: {path.relative_to(ROOT)}"
    )
    proof = json.loads(path.read_text())
    assert proof.get("schema") == "f3-surface-equivalence-v1"
    return proof


def _check_version_surface(run: str, record: dict[str, Any]) -> None:
    recorded = record["run_versions"]
    today = otel_demo_versions()
    proof: dict[str, Any] | None = None
    for key, value in today.items():
        if recorded.get(key) == value:
            continue
        if proof is None:
            proof = _equivalence()
        change = proof.get("change")
        assert isinstance(change, dict), "surface equivalence proof has no change"
        assert change.get("key") == key
        assert recorded.get(key) == change.get("from")
        assert value == change.get("to")

        computed = proof.get("computed_with")
        assert isinstance(computed, dict), (
            "surface equivalence proof has no computed_with"
        )
        assert computed.get("tool_schema_revision") == today["tool_schema_revision"]
        assert computed.get("projection_revision") == PROJECTION_REVISION

        runs = proof.get("runs")
        assert isinstance(runs, dict) and run in runs
        run_proof = runs[run]
        assert isinstance(run_proof, dict)
        assert run_proof.get("run_id") == record["run_id"]
        assert run_proof.get("tool_messages", 0) > 0
        assert run_proof.get("llm_calls", 0) > 0
        assert run_proof.get("changed_by_current_redaction") == 0
        assert run_proof.get("escaped_authority_matches") == 0


def _assert_live_views_are_currently_redacted(byte_views: dict[str, Any]) -> None:
    assert byte_views, "live summary has no cited_evidence_views"
    for evidence_id, evidence in byte_views.items():
        assert isinstance(evidence, dict), evidence_id
        view = evidence.get("view")
        assert isinstance(view, dict), evidence_id
        canonical = json.dumps(
            view, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        assert redact_credentials(canonical) == canonical, evidence_id
        encoded = canonical.encode("utf-8")
        assert redact_json_bytes(encoded) == encoded, evidence_id


@pytest.mark.parametrize("run", RUNS)
def test_step5_real_run_used_the_frozen_files_and_current_model_surface(
    run: str,
) -> None:
    sc = scenario(5, f"real-{run}")
    summary = _summary(run)
    registered = {
        "rubric_sha256": RUBRIC_SHA256,
        "stimulus_sha256": STIMULUS_SHA256,
        "answer_sha256": ANSWER_SHA256,
    }
    for key, value in registered.items():
        assert summary["frozen"][key] == value, (sc.scenario_id, key)
    _check_version_surface(run, _record(run))


@pytest.mark.parametrize("run", RUNS)
def test_step5_live_cited_views_are_stable_under_current_redaction(run: str) -> None:
    path = LIVE / run / "summary.json"
    assert path.is_file(), f"live acceptance summary missing: {path.relative_to(ROOT)}"
    summary = json.loads(path.read_text())
    _assert_live_views_are_currently_redacted(summary.get("cited_evidence_views", {}))


@pytest.mark.parametrize("run", RUNS)
def test_step5_real_run_meets_rubric_gates_1_to_4(run: str) -> None:
    record = _record(run)
    sc = scenario(5, f"real-{run}", record["incident_id"])
    problems = rubric_gate_problems(record)
    assert problems == {"gate1": [], "gate2": [], "gate3": [], "gate4": []}, sc
    assert scenario_shape_problems(record) == [], sc


def test_step5_two_independent_real_runs() -> None:
    records = [_record(run) for run in RUNS]
    assert len({r["incident_id"] for r in records}) == 2
    assert len({r["run_id"] for r in records}) == 2


@pytest.mark.parametrize("run", RUNS)
def test_step1_every_observation_of_the_real_run_is_attributed_and_resolvable(
    run: str,
) -> None:
    record = _record(run)
    sc = scenario(1, f"real-{run}", record["incident_id"])
    assert step1_problems(record) == [], sc


@pytest.mark.parametrize("run", RUNS)
def test_step2_real_run_keeps_the_five_claim_kinds_apart(run: str) -> None:
    record = _record(run)
    sc = scenario(2, f"real-{run}", record["incident_id"])
    assert step2_problems(record) == [], sc


@pytest.mark.parametrize("run", RUNS)
def test_step6_real_run_tool_requests_are_read_only_on_the_bound_target(
    run: str,
) -> None:
    record = _record(run)
    sc = scenario(6, f"real-{run}", record["incident_id"])
    assert gate4_problems(record, registered_verbs()) == [], sc


def test_step6_authorization_probe_in_the_f3_window_denies_writes() -> None:
    sc = scenario(6, "authz-probe")
    assert PROBE.is_file(), f"R10 probe not frozen yet: {PROBE.relative_to(ROOT)}"
    assert probe_problems(json.loads(PROBE.read_text())) == [], sc


def _rerun_summaries() -> list[Path]:
    suffix = investigation_versions()["prompt_revision"].rsplit("-", 1)[-1]
    dirs = [p for p in STEP3.glob("missing-source-rerun-*") if suffix in p.name]
    return sorted(path for d in dirs for path in d.rglob("summary.json"))


def test_step3_d9_rerun_of_scenario_a_on_the_current_prompt_revision() -> None:
    # D9-a: the prompt revision moved since PR-2's evidence, so scenario A is
    # rerun once on today's revision (R-3) and judged by the frozen U6 rule.
    sc = scenario(3, "missing-source-rerun")
    found = _rerun_summaries()
    assert len(found) == 1, (sc.scenario_id, [str(p) for p in found])
    summary = json.loads(found[0].read_text())
    u6 = summary["f3_coverage"]["u6"]
    assert u6["answer_sha256"] == MISSING_SOURCE_ANSWER_SHA256
    assert u6["required_source"] == "jaeger"
    rows = {s["name"]: s for s in summary["f3_coverage"]["coverage"]["sources"]}
    gap = rows["jaeger"]["gap"] is True
    supported = summary["report"]["conclusion"] == "supported"
    # U6-a recomputed from the frozen fields, not read from ``passed``.
    verdict = (not supported or not u6["fact_evidence_matching_signature"]) and gap
    assert verdict is True, sc
    assert u6["passed"] is True
