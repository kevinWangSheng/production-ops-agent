"""F3 independent acceptance judge: the deterministic half of steps 1, 2, 5, 6.

Written by a fresh-context author who did not implement F3 PR-3b and did not
read its branch, from the PR-3b contract pre-review
``~/.agent/handoffs/opspilot-prereview-f3-3b-2026-10-24.md`` (U-A ruled as
recommended, R-1..R-9 as written; section 6 "确定性行为清单"), the F3 contract
``docs/tasks/2026-10-17-f3-contract.md`` (U2-c rubric, U3-a, U4-a, U9-a,
semantic points 1-3), the frozen rubric
``docs/testing/f3-competing-cause-rubric.md`` with the evaluator-side answer
``evals/f3/competing-cause/answer.json``, and ``feature_list.json`` F3.

This module holds the judge and everything that needs no database:

* the judge: pure functions over one **Run record** (shape below), used by
  the PostgreSQL product-path tests (``tests/integration/
  test_f3_acceptance_postgres.py``, records built from the live projection by
  ``run_record`` there) and by the frozen-evidence tests
  (``tests/acceptance/test_f3_step5_real_runs.py``, records read from the two
  kind Runs' ``summary.json``) -- one judge, two sources;
* unit tests that pin the judge on synthetic records (each gate fails on the
  record that breaks it, and only that gate);
* the frozen files' sha256 and the scenario index entry (R11).

What this judge does NOT decide (stated so nobody reads a green run as more):
rubric items 5-8 and the step-2 "summary agrees with the structured groups"
check are reviewer judgements (U3-a, two fresh Codex reviewers per Run, R-6);
no deterministic test replaces them. Gate 2 checks the evaluator signature
on the cited evidence, not that the fact's prose names payment Charge
(item 6); gate 3 checks that a rejection cites this Run's ok evidence, not
that the evidence refutes the deployment (item 5, D5-a).

ASSUMED (the contract fixes semantics, not these names; kept in this module
so they live in one place):

* A **Run record** is a JSON object with these keys (the PR-3b
  ``summary.json`` is expected to carry it under ``"f3_acceptance"``):

  - ``scenario_id`` (``"F3:competing-cause"`` for step 5), ``incident_id``,
    ``run_id`` (strings);
  - ``bound_target_id``: the Run input's ``bound_target_id``;
    ``authorized_window``: ``{"start", "end"}`` ISO instants, the Run input's
    first time policy ``window`` (the authorized frame);
  - ``outcome``: the fields of ``IncidentOutcome`` from
    ``outcome_from_durable`` (tuples as lists);
  - ``report``: the published ``m0-report-v2`` object (``assessment_status``,
    ``conclusion``, ``summary``, ``claims[{kind, text, evidence_ids}]``);
  - ``investigation_evidence``: ``InvestigationEvidence`` from
    ``opspilot.acceptance_investigation.investigation_evidence`` as plain
    JSON (dataclasses -> objects with their field names, mappings -> objects,
    tuples -> lists, UUIDs -> strings);
  - ``cited_evidence``: for every evidence id any report claim cites,
    ``{"run_id", "subject_id", "status", "view"}`` -- the stored view as
    ``GET /incidents/{id}/evidence/{evidence_id}`` returns it (R-6 gives the
    reviewers the same views);
  - ``audit_queries``: every ``audit_timeline`` entry with
    ``category == "query"`` as ``{"run_id": str, **entry.detail}``;
  - ``incident_runs``: ``[{"run_id", "control_generation", "state"}]`` for
    every Run of the incident;
  - ``alert_delivery_outcomes``: the ``opspilot_alert_deliveries.outcome``
    values of the incident in delivery order;
  - ``run_versions``: the Run row's recorded ``versions`` mapping.

  The frozen ``summary.json`` also carries the driver plan's ``frozen``
  object (``rubric_sha256``, ``stimulus_sha256``, ``answer_sha256``) at top
  level.

* The frozen-evidence layout (R-8): ``docs/evidence/
  f3-step5-competing-cause/run-{1,2}/summary.json``; the authorization probe
  output of ``scripts/f7_authz_probe.py`` (unchanged format) at
  ``docs/evidence/f3-step6-authz-probe/probe.json``; the D9 rerun of step-3
  scenario A under ``docs/evidence/f3-step3-coverage/
  missing-source-rerun-<revision>/`` (R-3) with the existing
  ``opspilot-lab-summary-1`` ``f3_coverage.u6`` block.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from opspilot.acceptance import (
    PRODUCT_CONSTRAINT_SCENARIO_INDEX,
    IncidentScenario,
)
from opspilot.tools.otel_demo import otel_demo_registrations
from opspilot.tools.registry import READ_ONLY_VERBS

ROOT = Path(__file__).resolve().parents[2]
SCENARIO_ID = "F3:competing-cause"
RUBRIC = ROOT / "docs/testing/f3-competing-cause-rubric.md"
ANSWER = ROOT / "evals/f3/competing-cause/answer.json"
STIMULUS = ROOT / "evals/f3/competing-cause/stimulus.json"
MISSING_SOURCE_ANSWER = ROOT / "evals/f3/missing-source/answer.json"
# Registered values (PR-3a pre-review; F3 PR-2 README; pre-review section 1).
RUBRIC_SHA256 = "b58974b2c2a5ba309cb0efcf47c5af001d9f9c3a27440fec32d5d31b5cf1d956"
ANSWER_SHA256 = "a35361da83e8a314a2af54bf50aa74441ca16dcd7a533e96c22c85a71b14b37a"
MISSING_SOURCE_ANSWER_SHA256 = (
    "b9a541ab9d13b7fce5433a923263eb133936b95e59822561d136ff6fa3ddaab5"
)
# The stimulus is not registered in the task record; this is the value read
# at 3c18bdb (pre-review: "冻结文件 sha256 与登记值一致" covers rubric+answer).
STIMULUS_SHA256 = "343eacf242a372b12a951c6c745080e21a2714568b9359977e8e83829f43fc0e"

KINDS = (
    "fact",
    "hypothesis",
    "recommendation",
    "counter_evidence",
    "rejected_hypothesis",
)
EVIDENCE_KINDS = ("fact", "counter_evidence", "rejected_hypothesis")
UNCITED_KINDS = ("hypothesis", "recommendation")
# Semantic point 1: keys that must be present AND non-empty / present only.
NON_EMPTY_FIELDS = (
    "source",
    "target_id",
    "evidence_id",
    "query",
    "window",
    "tool",
    "tool_version",
    "registry_revision",
    "tool_registry_revision",
)
PRESENT_FIELDS = ("lookback_seconds", "lookback_start_at")


def scenario(step: int, kind: str, subject_id: str = "") -> IncidentScenario:
    """The F3 acceptance scenario for ``step`` (feature_id ``F3``)."""
    return IncidentScenario(
        scenario_id=f"F3:step{step}:{kind}",
        feature_id="F3",
        acceptance_step=str(step),
        kind="incident",
        subject_id=subject_id,
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def answer() -> dict[str, Any]:
    return json.loads(ANSWER.read_text())


# -- plain JSON of the public projection ------------------------------------------


def plain(value: Any) -> Any:
    """Dataclasses, mappings, tuples, UUIDs and instants as plain JSON values."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, (UUID, Enum)):
        return str(value.value if isinstance(value, Enum) else value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return value


def _instant(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


# -- evaluator signature (answer.json "payment-charge-error-span-v1") -------------


def signature_matches(view: Mapping[str, Any], signature: Mapping[str, Any]) -> int:
    """How many error spans in ``view``'s content match one ``any_of`` pair.

    A span is any mapping with ``service`` / ``operation`` and
    ``error_by_visible_tags`` true (the trace projection's span row).
    """
    pairs = {(item["service"], item["operation"]) for item in signature["any_of"]}
    found = 0
    stack: list[Any] = [view.get("content")]
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping):
            if (
                item.get("error_by_visible_tags") is True
                and (item.get("service"), item.get("operation")) in pairs
            ):
                found += 1
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return found


# -- record accessors ----------------------------------------------------------------


def _report_claims(record: Mapping[str, Any]) -> Mapping[str, list[Mapping[str, Any]]]:
    """The projection's grouped claims of the record's Run (exactly one)."""
    reports = [
        r
        for r in record["investigation_evidence"]["reports"]
        if str(r["run_id"]) == str(record["run_id"])
    ]
    if len(reports) != 1:
        raise AssertionError(f"expected one projected report, got {len(reports)}")
    return reports[0]["claims"]


def _run_observations(record: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        o
        for o in record["investigation_evidence"]["observations"]
        if str(o["run_id"]) == str(record["run_id"])
    ]


def _ok_ref(ref: Mapping[str, Any]) -> bool:
    return ref["resolvable"] is True and ref["ok"] is True


# -- step 1: observations -------------------------------------------------------------


def step1_problems(record: Mapping[str, Any]) -> list[str]:
    """Pre-review section 6 item 1; contract semantic points 1-2 (R1-R3)."""
    problems: list[str] = []
    observations = _run_observations(record)
    if not observations:
        problems.append("no observation for the Run")
    for o in observations:
        label = o.get("evidence_id")
        if o.get("status") not in {"ok", "no_data"}:
            problems.append(
                f"{label}: status {o.get('status')!r} is not an observation"
            )
        for key in NON_EMPTY_FIELDS:
            if key not in o or o[key] in (None, "", {}, []):
                problems.append(f"{label}: {key} missing or empty")
        for key in PRESENT_FIELDS:
            if key not in o:
                problems.append(f"{label}: {key} key missing")
        if tuple(o.get("missing_fields") or ()) != ():
            problems.append(f"{label}: missing_fields {o.get('missing_fields')}")
        freshness = o.get("freshness_seconds")
        if freshness is None:
            # R2: explicit unknown only when there is no source sample time.
            if o.get("data_as_of") is not None:
                problems.append(f"{label}: freshness None with data_as_of")
        elif isinstance(freshness, bool) or not isinstance(freshness, (int, float)):
            problems.append(f"{label}: freshness {freshness!r} is not a number")
        window = o.get("window") or {}
        if not (window.get("start") and window.get("end")):
            problems.append(f"{label}: window without start/end")
        elif _instant(window["start"]) >= _instant(window["end"]):
            problems.append(f"{label}: empty window")
        if o.get("resolvable") is not True:
            problems.append(f"{label}: not resolvable")
    return problems


# -- step 2: five claim kinds -----------------------------------------------------------


def step2_problems(record: Mapping[str, Any]) -> list[str]:
    """Pre-review section 6 item 3; contract semantic point 3 (machine half)."""
    problems: list[str] = []
    claims = _report_claims(record)
    if set(claims) != set(KINDS):
        problems.append(f"claim groups {sorted(claims)} are not the five kinds")
    report = record["report"]
    for kind in KINDS:
        expected = [c["text"] for c in report["claims"] if c["kind"] == kind]
        got = [c["text"] for c in claims.get(kind, [])]
        if got != expected:
            problems.append(f"{kind}: projected {got} != report {expected}")
    fact_texts = {c["text"] for c in claims.get("fact", [])}
    for kind in UNCITED_KINDS:
        for claim in claims.get(kind, []):
            if claim["text"] in fact_texts:
                problems.append(f"{kind} text appears in the fact group")
    for kind in EVIDENCE_KINDS:
        for claim in claims.get(kind, []):
            for ref in claim["evidence"]:
                if not (_ok_ref(ref) and ref["citable_as_fact"] is True):
                    problems.append(
                        f"{kind}: {ref['evidence_id']} not resolvable/ok/citable"
                    )
    return problems


# -- step 5: rubric deterministic gates 1-4 + scenario shape -----------------------------


def gate1_problems(record: Mapping[str, Any]) -> list[str]:
    """Completed, published, not a handoff; evidence/target/time binding."""
    problems: list[str] = []
    outcome = record["outcome"]
    report = record["report"]
    if report.get("assessment_status") != "completed":
        problems.append(f"assessment_status {report.get('assessment_status')!r}")
    if outcome["final_state"] != "completed":
        problems.append(f"final_state {outcome['final_state']!r}")
    if (
        outcome["decision"] != "report_available"
        or outcome["report_available"] is not True
    ):
        problems.append(f"decision {outcome['decision']!r} / report not available")
    if outcome["handoff_reasons"]:
        problems.append(f"handoff reasons {outcome['handoff_reasons']}")
    bound = record["bound_target_id"]
    frame = record["authorized_window"]
    start, end = _instant(frame["start"]), _instant(frame["end"])
    # Only observations (ok / no_data, R1) carry bound evidence; a refused
    # call has no evidence record to bind.
    for o in _run_observations(record):
        if o["target_id"] != bound:
            problems.append(
                f"{o['evidence_id']}: target {o['target_id']!r} != {bound!r}"
            )
        window = o.get("window") or {}
        if window.get("start") and window.get("end"):
            if _instant(window["start"]) < start or _instant(window["end"]) > end:
                problems.append(
                    f"{o['evidence_id']}: window outside the authorized frame"
                )
        if o.get("resolvable") is not True:
            problems.append(f"{o['evidence_id']}: not resolvable")
    return problems


def _cited_ok_this_run(record: Mapping[str, Any], evidence_id: str) -> bool:
    cited = record["cited_evidence"].get(evidence_id)
    return (
        cited is not None
        and str(cited["run_id"]) == str(record["run_id"])
        and str(cited["subject_id"]) == str(record["incident_id"])
        and cited["status"] == "ok"
    )


def gate2_problems(
    record: Mapping[str, Any], signature: Mapping[str, Any]
) -> list[str]:
    """A fact citing this Run's ok evidence, one of which meets the signature."""
    claims = _report_claims(record)
    for claim in claims.get("fact", []):
        refs = [r for r in claim["evidence"] if _ok_ref(r)]
        ok_ids = [
            r["evidence_id"]
            for r in refs
            if _cited_ok_this_run(record, r["evidence_id"])
        ]
        matching = sum(
            signature_matches(record["cited_evidence"][i]["view"], signature)
            for i in ok_ids
        )
        if ok_ids and matching >= signature["min_matches"]:
            return []
    return ["no fact cites this Run's ok evidence meeting " + signature["rule_id"]]


def gate3_problems(record: Mapping[str, Any]) -> list[str]:
    """A rejected_hypothesis or counter_evidence citing this Run's ok evidence."""
    claims = _report_claims(record)
    for kind in ("rejected_hypothesis", "counter_evidence"):
        for claim in claims.get(kind, []):
            if any(
                _ok_ref(r) and _cited_ok_this_run(record, r["evidence_id"])
                for r in claim["evidence"]
            ):
                return []
    return ["no rejected_hypothesis/counter_evidence cites this Run's ok evidence"]


def registered_verbs() -> dict[str, str]:
    return {r.name: r.verb for r in otel_demo_registrations().tools}


def gate4_problems(record: Mapping[str, Any], verbs: Mapping[str, str]) -> list[str]:
    """Every tool request of the Run is a registered read-only verb on the
    bound target (shared with step 6; U9-a: ``permissions`` is not evidence)."""
    problems: list[str] = []
    queries = [
        q for q in record["audit_queries"] if str(q["run_id"]) == str(record["run_id"])
    ]
    if not queries:
        problems.append("no audit query entry for the Run")
    for q in queries:
        verb = verbs.get(q.get("tool"))
        if verb is None or verb not in READ_ONLY_VERBS:
            problems.append(
                f"{q.get('operation_id')}: tool {q.get('tool')!r} verb {verb!r}"
            )
        if q.get("target_id") != record["bound_target_id"]:
            problems.append(f"{q.get('operation_id')}: target {q.get('target_id')!r}")
    for refused in record["investigation_evidence"]["refused_operations"]:
        if str(refused["run_id"]) != str(record["run_id"]):
            continue
        verb = verbs.get(refused.get("tool"))
        if verb is not None and verb not in READ_ONLY_VERBS:
            problems.append(f"refused {refused['operation_id']} has write verb {verb}")
    return problems


def scenario_shape_problems(record: Mapping[str, Any]) -> list[str]:
    """Pre-review item 5 extras: one new alert incident, one Run (the
    pre-claim follow_up re-queued the same Run, generation 1), no attach."""
    problems: list[str] = []
    runs = record["incident_runs"]
    if [str(r["run_id"]) for r in runs] != [str(record["run_id"])]:
        problems.append(f"incident Runs {runs} are not exactly the scenario Run")
    elif runs[0]["control_generation"] != 1:
        problems.append(f"Run generation {runs[0]['control_generation']} != 1")
    outcomes = list(record["alert_delivery_outcomes"])
    if not outcomes or outcomes[0] != "created":
        problems.append(f"alert deliveries {outcomes} do not start with created")
    if "attached" in outcomes:
        problems.append(f"alert deliveries {outcomes} include attached")
    return problems


def rubric_gate_problems(record: Mapping[str, Any]) -> dict[str, list[str]]:
    """Rubric items 1-4 (U2-c deterministic half), keyed by gate."""
    signature = answer()["signature"]
    return {
        "gate1": gate1_problems(record),
        "gate2": gate2_problems(record, signature),
        "gate3": gate3_problems(record),
        "gate4": gate4_problems(record, registered_verbs()),
    }


# -- step 6: authorization probe output (scripts/f7_authz_probe.py) -----------------


DISCOVERY_PATH = "/api/v1/label/__name__/values"


def probe_problems(probe: Mapping[str, Any]) -> list[str]:
    """Pre-review item 6: every write request 403 and every read endpoint
    200 for the investigator account; no new sample written."""
    problems: list[str] = []
    if probe.get("ok") is not True:
        problems.append("probe ok is not true")
    rows = probe.get("requests") or []
    investigator = [r for r in rows if r.get("account") == "investigator"]
    if not investigator:
        problems.append("no investigator rows")
    for r in investigator:
        if r.get("matches") is not True:
            problems.append(
                f"{r['method']} {r['path']}: {r['status']} != {r['expected']}"
            )
        if r["method"] == "POST" and r["status"] != 403 and r["expected"] != "4xx":
            problems.append(f"write {r['path']} answered {r['status']}")
    reads = {(r["method"], r["path"]) for r in investigator if r["status"] == 200}
    if ("GET", "/api/v1/query_range") not in reads or (
        "GET",
        DISCOVERY_PATH,
    ) not in reads:
        problems.append("read endpoints (query_range, label values) not 200")
    if (probe.get("sample_check") or {}).get("matches") is not True:
        problems.append("sample check did not hold")
    return problems


# ======================================================================================
# tests: frozen files and index (no database)
# ======================================================================================


def test_step5_frozen_rubric_answer_stimulus_are_unchanged() -> None:
    assert sha256(RUBRIC) == RUBRIC_SHA256
    assert sha256(ANSWER) == ANSWER_SHA256
    assert sha256(STIMULUS) == STIMULUS_SHA256
    assert sha256(MISSING_SOURCE_ANSWER) == MISSING_SOURCE_ANSWER_SHA256
    signature = answer()["signature"]
    assert signature["rule_id"] == "payment-charge-error-span-v1"
    assert signature["min_matches"] == 1


def test_scenario_index_lists_the_competing_cause_scenario() -> None:
    # R11 / pre-review item 7: the index names the step-5 scenario.
    listed = {
        item for ids in PRODUCT_CONSTRAINT_SCENARIO_INDEX.values() for item in ids
    }
    assert SCENARIO_ID in listed


def test_step6_registered_tools_are_read_only_verbs() -> None:
    verbs = registered_verbs()
    assert verbs, "otel-demo registers no tools"
    assert set(verbs.values()) <= READ_ONLY_VERBS, verbs
    # #307 discovery tools are list verbs; read_evidence is an internal get.
    assert verbs["get_metric_names"] == verbs["get_label_values"] == "list"
    assert verbs["read_evidence"] == "get"


# ======================================================================================
# tests: the judge on synthetic records (pins its semantics, no product code)
# ======================================================================================

RUN = "11111111-1111-1111-1111-111111111111"
INCIDENT = "22222222-2222-2222-2222-222222222222"
TARGET = "m0-otel-20260909"
FRAME = {"start": "2026-10-24T09:00:00+00:00", "end": "2026-10-24T10:00:00+00:00"}
QWIN = {"start": "2026-10-24T09:30:00+00:00", "end": "2026-10-24T09:50:00+00:00"}
TRACE_ID = "op-1-t0:d1"
METRIC_ID = "op-1-t1:d2"


def _obs(evidence_id: str, tool: str, **over: Any) -> dict[str, Any]:
    base = {
        "evidence_id": evidence_id,
        "run_id": RUN,
        "status": "ok",
        "adopted": True,
        "tool": tool,
        "tool_version": "v1",
        "registry_revision": "reg-1",
        "tool_registry_revision": "treg-1",
        "source": "otel-demo",
        "target_id": TARGET,
        "query": {"service": "checkout"},
        "window": dict(QWIN),
        "lookback_seconds": None,
        "lookback_start_at": None,
        "observed_at": "2026-10-24T09:51:00+00:00",
        "data_as_of": "2026-10-24T09:50:00+00:00",
        "freshness_seconds": 60.0,
        "projection_revision": "m1-01-tool-view-v11",
        "resolvable": True,
        "missing_fields": [],
    }
    base.update(over)
    return base


def _ref(evidence_id: str, **over: Any) -> dict[str, Any]:
    return {
        "evidence_id": evidence_id,
        "resolvable": True,
        "ok": True,
        "citable_as_fact": True,
        **over,
    }


def _record(**over: Any) -> dict[str, Any]:
    texts = {kind: f"{kind} text" for kind in KINDS}
    cites = {
        "fact": [TRACE_ID],
        "counter_evidence": [METRIC_ID],
        "rejected_hypothesis": [METRIC_ID],
    }
    report_claims = [
        {"kind": k, "text": texts[k], "evidence_ids": cites.get(k, [])} for k in KINDS
    ]
    grouped = {
        k: [{"text": texts[k], "evidence": [_ref(i) for i in cites.get(k, [])]}]
        for k in KINDS
    }
    span = {
        "service": "payment",
        "operation": "grpc.oteldemo.PaymentService/Charge",
        "error_by_visible_tags": True,
    }
    record = {
        "scenario_id": SCENARIO_ID,
        "incident_id": INCIDENT,
        "run_id": RUN,
        "bound_target_id": TARGET,
        "authorized_window": dict(FRAME),
        "outcome": {
            "scenario_id": SCENARIO_ID,
            "final_state": "completed",
            "evidence_ids": [TRACE_ID, METRIC_ID],
            "decision": "report_available",
            "actions": ["read_only_query"],
            "permissions": ["read_only", "human_control"],
            "human_interaction": None,
            "handoff_reasons": [],
            "report_available": True,
        },
        "report": {
            "schema_version": "m0-report-v2",
            "assessment_status": "completed",
            "conclusion": "supported",
            "summary": "s",
            "claims": report_claims,
        },
        "investigation_evidence": {
            "incident_id": INCIDENT,
            "observations": [
                _obs(TRACE_ID, "traces_search"),
                _obs(METRIC_ID, "metrics_range_query"),
            ],
            "refused_operations": [],
            "reports": [
                {"run_id": RUN, "claims": grouped, "collection_truncated": False}
            ],
            "coverage": [],
        },
        "cited_evidence": {
            TRACE_ID: {
                "run_id": RUN,
                "subject_id": INCIDENT,
                "status": "ok",
                "view": {"content": {"spans": [span]}},
            },
            METRIC_ID: {
                "run_id": RUN,
                "subject_id": INCIDENT,
                "status": "ok",
                "view": {"content": {}},
            },
        },
        "audit_queries": [
            {
                "run_id": RUN,
                "operation_id": "op-1-t0",
                "tool": "traces_search",
                "target_id": TARGET,
            },
            {
                "run_id": RUN,
                "operation_id": "op-1-t1",
                "tool": "metrics_range_query",
                "target_id": TARGET,
            },
            {
                "run_id": RUN,
                "operation_id": "op-1-t2",
                "tool": "get_label_values",
                "target_id": TARGET,
            },
        ],
        "incident_runs": [
            {"run_id": RUN, "control_generation": 1, "state": "completed"}
        ],
        "alert_delivery_outcomes": ["created"],
    }
    record.update(over)
    return record


def _failing(problems: Mapping[str, list[str]]) -> set[str]:
    return {gate for gate, items in problems.items() if items}


def test_judge_passes_a_record_meeting_every_gate() -> None:
    record = _record()
    assert _failing(rubric_gate_problems(record)) == set()
    assert step1_problems(record) == []
    assert step2_problems(record) == []
    assert scenario_shape_problems(record) == []


def test_judge_gate1_fails_a_handoff_or_incomplete_report() -> None:
    record = _record()
    record["report"]["assessment_status"] = "incomplete"
    assert _failing(rubric_gate_problems(record)) == {"gate1"}
    record = _record()
    record["outcome"].update(
        decision="handoff", handoff_reasons=["INCOMPLETE_INVESTIGATION"]
    )
    assert _failing(rubric_gate_problems(record)) == {"gate1"}


def test_judge_gate1_fails_an_observation_outside_the_frame_or_target() -> None:
    record = _record()
    record["investigation_evidence"]["observations"][1]["window"] = {
        "start": "2026-10-24T08:00:00+00:00",
        "end": "2026-10-24T09:10:00+00:00",
    }
    assert _failing(rubric_gate_problems(record)) == {"gate1"}
    record = _record()
    record["investigation_evidence"]["observations"][0]["target_id"] = "payments-prod"
    assert "gate1" in _failing(rubric_gate_problems(record))


def test_judge_gate2_needs_the_signature_on_this_runs_ok_evidence() -> None:
    record = _record()
    record["cited_evidence"][TRACE_ID]["view"] = {
        "content": {
            "spans": [
                {
                    "service": "checkout",
                    "operation": "PlaceOrder",
                    "error_by_visible_tags": True,
                }
            ]
        }
    }
    assert _failing(rubric_gate_problems(record)) == {"gate2"}
    record = _record()
    record["cited_evidence"][TRACE_ID]["run_id"] = (
        "33333333-3333-3333-3333-333333333333"
    )
    assert _failing(rubric_gate_problems(record)) == {"gate2"}
    record = _record()
    record["cited_evidence"][TRACE_ID]["view"]["content"]["spans"][0][
        "error_by_visible_tags"
    ] = False
    assert _failing(rubric_gate_problems(record)) == {"gate2"}
    # The checkout client span on the same RPC also meets the signature.
    record = _record()
    record["cited_evidence"][TRACE_ID]["view"]["content"]["spans"] = [
        {
            "service": "checkout",
            "operation": "oteldemo.PaymentService/Charge",
            "error_by_visible_tags": True,
        }
    ]
    assert _failing(rubric_gate_problems(record)) == set()


def test_judge_gate3_needs_a_rejection_citing_ok_evidence() -> None:
    record = _record()
    for kind in ("rejected_hypothesis", "counter_evidence"):
        record["investigation_evidence"]["reports"][0]["claims"][kind] = [
            {"text": f"{kind} text", "evidence": []}
        ]
    assert _failing(rubric_gate_problems(record)) == {"gate3"}
    record = _record()
    for kind in ("rejected_hypothesis", "counter_evidence"):
        record["investigation_evidence"]["reports"][0]["claims"][kind][0][
            "evidence"
        ] = [_ref(METRIC_ID, ok=False)]
    assert "gate3" in _failing(rubric_gate_problems(record))


def test_judge_gate4_refuses_unregistered_tools_and_other_targets() -> None:
    record = _record()
    record["audit_queries"].append(
        {
            "run_id": RUN,
            "operation_id": "op-2-t0",
            "tool": "kubectl_rollout_restart",
            "target_id": TARGET,
        }
    )
    assert _failing(rubric_gate_problems(record)) == {"gate4"}
    record = _record()
    record["audit_queries"][0]["target_id"] = "payments-prod"
    assert _failing(rubric_gate_problems(record)) == {"gate4"}
    record = _record(audit_queries=[])
    assert _failing(rubric_gate_problems(record)) == {"gate4"}


def test_judge_step1_flags_missing_fields_and_unresolvable_references() -> None:
    record = _record()
    record["investigation_evidence"]["observations"][0]["missing_fields"] = [
        "lookback_seconds"
    ]
    assert step1_problems(record)
    record = _record()
    record["investigation_evidence"]["observations"][0]["resolvable"] = False
    assert step1_problems(record)
    record = _record()
    del record["investigation_evidence"]["observations"][0]["lookback_start_at"]
    assert step1_problems(record)
    record = _record()
    record["investigation_evidence"]["observations"][0]["query"] = {}
    assert step1_problems(record)
    # Explicit unknown freshness on no_data is accepted (R2).
    record = _record()
    record["investigation_evidence"]["observations"][1].update(
        status="no_data", data_as_of=None, freshness_seconds=None
    )
    assert step1_problems(record) == []


def test_judge_step2_flags_mixed_groups_and_uncitable_references() -> None:
    record = _record()
    grouped = record["investigation_evidence"]["reports"][0]["claims"]
    grouped["fact"].append({"text": "hypothesis text", "evidence": []})
    assert step2_problems(record)
    record = _record()
    grouped = record["investigation_evidence"]["reports"][0]["claims"]
    grouped["counter_evidence"][0]["evidence"] = [
        _ref(METRIC_ID, citable_as_fact=False)
    ]
    assert step2_problems(record)


def test_judge_scenario_shape_needs_one_run_and_no_attach() -> None:
    record = _record(alert_delivery_outcomes=["created", "attached"])
    assert scenario_shape_problems(record)
    record = _record()
    record["incident_runs"].append(
        {"run_id": "x", "control_generation": 0, "state": "cancelled"}
    )
    assert scenario_shape_problems(record)
    record = _record()
    record["incident_runs"][0]["control_generation"] = 0
    assert scenario_shape_problems(record)


def test_judge_probe_needs_403_writes_and_200_reads() -> None:
    rows = [
        {
            "method": "GET",
            "path": "/api/v1/query_range",
            "account": "investigator",
            "status": 200,
            "expected": 200,
            "matches": True,
        },
        {
            "method": "GET",
            "path": DISCOVERY_PATH,
            "account": "investigator",
            "status": 200,
            "expected": 200,
            "matches": True,
        },
        {
            "method": "POST",
            "path": "/api/v1/write",
            "account": "investigator",
            "status": 403,
            "expected": 403,
            "matches": True,
        },
    ]
    good = {"requests": rows, "sample_check": {"matches": True}, "ok": True}
    assert probe_problems(good) == []
    bad = json.loads(json.dumps(good))
    bad["requests"][2].update(status=204, matches=False)
    bad["ok"] = False
    assert probe_problems(bad)


@pytest.mark.parametrize("items", [(), ("x",)])
def test_plain_converts_tuples(items: Iterable[str]) -> None:
    assert plain({"a": tuple(items)}) == {"a": list(items)}
