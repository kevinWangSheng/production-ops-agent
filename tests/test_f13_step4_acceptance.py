"""Independent acceptance tests for F13 step 4 (PR-C): the ``KnowledgeUseOutcome``
projection, without a database.

Written from the task record's public interface and acceptance verdicts
(``docs/tasks/2026-10-15-f13-step4-acceptance.md``), the contract decision
table (``docs/tasks/2026-10-15-f13-step4-contract.md``: R8, R9, U2, U8) and
the PR-B public interface (``docs/tasks/2026-10-15-f13-step4.md``), without
reading the implementation or its own tests.

The records are ``rebuild()``-shaped dicts handed to ``knowledge_use_records``
through a fake that only has ``rebuild``: a v5 Run input carrying
``scope_facts.knowledge`` and, when a report was published, the incident's
``conclusion`` row in the shape the loop commits. The real PostgreSQL path
(web submit, worker, page) is in
``tests/integration/test_f13_step4_acceptance_postgres.py``.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid5

import pytest

from opspilot.acceptance import (
    IncidentScenario,
    KnowledgeUseRecords,
    knowledge_use_outcome,
    knowledge_use_records,
)
from opspilot.investigation.reports import parse_report
from opspilot.knowledge.hints import knowledge_checks, unavailable_fact
from opspilot.tools.otel_demo import otel_demo_face
from opspilot.tools.registry import canonical
from tests.m1_tool_support import FakeClock

POLICY = "knowledge-hint-v1"
MAX_AGE = 2592000
RECEIVED = datetime(2026, 10, 10, 12, 0, 30, tzinfo=timezone.utc)
RETRIEVED_AT = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
_NS = UUID("6b6e6f77-6c65-6467-6575-736500000013")

INCIDENT = uuid5(_NS, "incident")
OTHER_INCIDENT = uuid5(_NS, "other-incident")
RUN = uuid5(_NS, "run")
EV_1 = "ev-run-1"
EV_2 = "ev-run-2"
EV_FOREIGN = "ev-not-in-this-run"
TARGET_REF = "checkout-prod"
TIME_REF = "policy-window-1"

ENTRY_FIELDS = (
    "entry_id",
    "revision",
    "name",
    "content_sha256",
    "source_incident_id",
    "source_postmortem_id",
    "source_version",
    "approved_by",
    "approved_at",
    "observation_ended_at",
    "age_seconds",
    "freshness",
    "truncated",
)


def _id(name: str) -> str:
    return str(uuid5(_NS, name))


def _entry(name: str, *, revision: int = 1, stale: bool = False) -> dict[str, Any]:
    """One retrieved entry, internally consistent (``age_seconds`` is the
    distance from ``RETRIEVED_AT`` to ``observation_ended_at``)."""
    age = MAX_AGE + 7200 if stale else 3600
    observed = RETRIEVED_AT - timedelta(seconds=age)
    return {
        "entry_id": _id(f"entry-{name}"),
        "revision": revision,
        "name": f"knowledge {name}",
        "tags": ["checkout", name],
        "content": canonical(
            {
                "symptoms": [f"{name} 5xx"],
                "checks": ["error rate"],
                "evidence_refs": {"omitted": True, "count": 1},
            }
        ),
        "content_sha256": hashlib.sha256(name.encode()).hexdigest(),
        "source_incident_id": _id(f"incident-{name}"),
        "source_postmortem_id": _id(f"postmortem-{name}"),
        "source_version": 2,
        "approved_by": "alice",
        "approved_at": (observed + timedelta(minutes=30)).isoformat(),
        "observation_ended_at": observed.isoformat(),
        "age_seconds": age,
        "freshness": "stale" if stale else "fresh",
        "truncated": stale,
    }


def _fact(*entries: dict[str, Any], omitted_for_size: int = 0) -> dict[str, Any]:
    return {
        "policy_version": POLICY,
        "status": "ok",
        "retrieved_at": RETRIEVED_AT.isoformat(),
        "max_age_seconds": MAX_AGE,
        "omitted_for_size": omitted_for_size,
        "entries": copy.deepcopy(list(entries)),
    }


A = _entry("a")
B = _entry("b", stale=True)
C = _entry("c", revision=3)


def _tag(entry: dict[str, Any], revision: int | None = None) -> str:
    rev = entry["revision"] if revision is None else revision
    return f"[knowledge:{entry['entry_id']}@r{rev}]"


def _input(knowledge: dict[str, Any] | None) -> dict[str, Any]:
    """A real Run input as the workbench stores it (``as_json``)."""
    base = otel_demo_face(FakeClock(start=RECEIVED)).input_for(
        run_id=str(RUN),
        question="Why is checkout erroring?",
        target_id="checkout-prod",
        deadline=RECEIVED + timedelta(minutes=10),
        model_requests=4,
    )
    if knowledge is not None:
        base = replace(base, scope_facts={**base.scope_facts, "knowledge": knowledge})
    return base.as_json()


def _claim(kind: str, text: str, evidence: list[str] | None = None) -> dict:
    claim: dict[str, Any] = {"kind": kind, "text": text, "evidence_ids": evidence or []}
    if kind in ("fact", "counter_evidence", "rejected_hypothesis"):
        claim["target_refs"] = [TARGET_REF]
        claim["time_scope_ref"] = TIME_REF
    return claim


def _report(*claims: dict, summary: str = "Assessment of checkout errors.") -> str:
    conclusion = (
        "supported"
        if any(c["kind"] == "fact" and c["evidence_ids"] for c in claims)
        else "partial"
    )
    return json.dumps(
        {
            "schema_version": "m0-report-v2",
            "assessment_status": "completed",
            "conclusion": conclusion,
            "summary": summary,
            "claims": list(claims),
            "gaps": [],
            "next_steps": ["Have a human confirm."],
        }
    )


def _conclusion(content: str | None, evidence: list[str]) -> dict[str, Any]:
    """The incident's ``conclusion`` row in the shape the loop publishes."""
    return {
        "kind": "conclusion",
        "assistant": {"role": "assistant", "content": None},
        "conclusion": {
            "execution": "completed",
            "handoff": False,
            "handoff_reasons": [],
            "report_schema_version": None if content is None else "m0-report-v2",
            "report_content": content,
            "report_content_sha256": None
            if content is None
            else hashlib.sha256(content.encode()).hexdigest(),
            "evidence_ids": list(evidence),
            "model_requests_used": 2,
        },
    }


def _rebuilt(
    knowledge: dict[str, Any] | None,
    *,
    report: str | None = None,
    evidence: tuple[str, ...] = (EV_1, EV_2),
    published: bool = True,
    incident: UUID = INCIDENT,
    run_incident: UUID = INCIDENT,
    state: str = "completed",
) -> dict[str, Any]:
    return {
        "incident_id": incident,
        "state": state,
        "control_generation": 0,
        "run": {
            "run_id": RUN,
            "incident_id": run_incident,
            "state": state,
            "input": _input(knowledge),
        },
        "steps": [],
        "inputs": [],
        "input_rounds": [],
        "pending_inputs": [],
        "pending_tools": [],
        "conclusion": _conclusion(report, list(evidence)) if published else None,
    }


class _Incidents:
    """The only read ``knowledge_use_records`` needs: ``rebuild``."""

    def __init__(self, rebuilt: dict[str, Any]) -> None:
        self.rebuilt = rebuilt
        self.calls: list[Any] = []

    def rebuild(self, incident_id):
        self.calls.append(incident_id)
        return self.rebuilt


def _scenario(subject: UUID | str = INCIDENT) -> IncidentScenario:
    return IncidentScenario("f13-step4", "F13", "4", "incident", str(subject))


def _project(rebuilt: dict[str, Any], *, records_for: UUID = INCIDENT, subject=None):
    records = knowledge_use_records(_Incidents(rebuilt), records_for)
    return knowledge_use_outcome(_scenario(subject or INCIDENT), records)


def _by_id(outcome) -> dict[str, Any]:
    return {h.entry_id: h for h in outcome.entries}


def _norm_tag(tag: str) -> str:
    """The record says ``unprovided_tags`` lists ``[knowledge:...]`` tags but
    not whether the brackets/prefix are kept; compare the inner reference."""
    if tag.startswith("[knowledge:") and tag.endswith("]"):
        return tag[len("[knowledge:") : -1]
    return tag


# -- reading (interface item 1) -------------------------------------------------


def test_records_call_rebuild_once_and_keep_the_snapshot_as_is():
    rebuilt = _rebuilt(_fact(A))
    incidents = _Incidents(rebuilt)
    records = knowledge_use_records(incidents, INCIDENT)
    assert incidents.calls == [INCIDENT]
    assert isinstance(records, KnowledgeUseRecords)
    assert records.incident_id == INCIDENT
    assert records.rebuilt == rebuilt


# -- copied fields (items 3, 4) -------------------------------------------------


def test_an_ok_snapshot_is_copied_field_by_field_without_content():
    fact = _fact(A, B, C, omitted_for_size=1)
    outcome = _project(_rebuilt(fact, report=_report(_claim("fact", "x", [EV_1]))))
    assert outcome.scenario_id == "f13-step4"
    assert outcome.subject_id == str(INCIDENT)
    assert outcome.status == "ok"
    assert outcome.unknown_reasons == ()
    assert outcome.run_id == str(RUN)
    assert outcome.run_state == "completed"
    assert outcome.policy_version == POLICY
    assert outcome.retrieved_at == fact["retrieved_at"]
    assert outcome.max_age_seconds == MAX_AGE
    assert outcome.omitted_for_size == 1
    assert outcome.run_evidence_ids == (EV_1, EV_2)
    assert outcome.model_requests == ()
    # Snapshot order, every entry.
    assert [(h.entry_id, h.revision) for h in outcome.entries] == [
        (e["entry_id"], e["revision"]) for e in (A, B, C)
    ]
    for hint, entry in zip(outcome.entries, (A, B, C), strict=True):
        for key in ENTRY_FIELDS:
            assert getattr(hint, key) == entry[key], key
        assert hint.tags == tuple(entry["tags"])
        assert isinstance(hint.tags, tuple)
        assert not hasattr(hint, "content")


def test_the_projection_does_not_mutate_the_records():
    rebuilt = _rebuilt(_fact(A, B), report=_report(_claim("fact", _tag(A), [EV_1])))
    before = copy.deepcopy(rebuilt)
    _project(rebuilt)
    assert rebuilt == before


# -- verdicts (item 4, PR-B item 5) ----------------------------------------------


def test_verdicts_match_knowledge_checks_for_every_kind():
    d = _entry("d")
    e = _entry("e")
    fact = _fact(A, B, C)
    fact_de = _fact(d, e)
    claims = (
        # A: consistent (tagged fact with this Run's evidence)
        _claim("fact", f"{_tag(A)} checkout 5xx confirmed", [EV_1]),
        # B: contradicted (tagged rejected hypothesis with evidence)
        _claim("rejected_hypothesis", f"{_tag(B)} payment failure ruled out", [EV_2]),
        # C: unverified (tagged hypothesis only)
        _claim("hypothesis", f"{_tag(C)} maybe the cache again"),
    )
    report = _report(*claims)
    outcome = _project(_rebuilt(fact, report=report))
    assert outcome.report_available is True
    parsed, reason = parse_report(report, finish_reason="stop")
    assert parsed is not None, reason
    expected = knowledge_checks(fact, parsed)
    assert [(h.verdict, h.claim_kinds) for h in outcome.entries] == [
        (c["verdict"], tuple(c["claim_kinds"])) for c in expected
    ]
    hints = _by_id(outcome)
    assert hints[A["entry_id"]].verdict == "consistent"
    assert hints[A["entry_id"]].claim_kinds == ("fact",)
    assert hints[B["entry_id"]].verdict == "contradicted"
    assert hints[B["entry_id"]].claim_kinds == ("rejected_hypothesis",)
    assert hints[C["entry_id"]].verdict == "unverified"
    assert hints[C["entry_id"]].claim_kinds == ("hypothesis",)

    # D: conflicting (fact and rejected hypothesis); E: untagged.
    report2 = _report(
        _claim("fact", f"{_tag(d)} confirmed", [EV_1]),
        _claim("rejected_hypothesis", f"{_tag(d)} also refuted", [EV_2]),
    )
    outcome2 = _project(_rebuilt(fact_de, report=report2))
    parsed2, _ = parse_report(report2, finish_reason="stop")
    expected2 = knowledge_checks(fact_de, parsed2)
    assert [(h.verdict, h.claim_kinds) for h in outcome2.entries] == [
        (c["verdict"], tuple(c["claim_kinds"])) for c in expected2
    ]
    hints2 = _by_id(outcome2)
    assert hints2[d["entry_id"]].verdict == "conflicting"
    assert hints2[d["entry_id"]].claim_kinds == ("fact", "rejected_hypothesis")
    assert hints2[e["entry_id"]].verdict == "untagged"
    assert hints2[e["entry_id"]].claim_kinds == ()
    assert hints2[e["entry_id"]].tagged_claims == ()


def test_tagged_claims_are_copied_in_report_order():
    claims = (
        _claim("hypothesis", f"first {_tag(A)}"),
        _claim("fact", "untagged fact", [EV_1]),
        _claim("fact", f"{_tag(A)} second", [EV_1, EV_2]),
        _claim("recommendation", f"also {_tag(B)}"),
    )
    outcome = _project(_rebuilt(_fact(A, B), report=_report(*claims)))
    hints = _by_id(outcome)
    assert [dict(c) for c in hints[A["entry_id"]].tagged_claims] == [
        {"kind": "hypothesis", "text": f"first {_tag(A)}", "evidence_ids": ()},
        {"kind": "fact", "text": f"{_tag(A)} second", "evidence_ids": (EV_1, EV_2)},
    ]
    assert [dict(c) for c in hints[B["entry_id"]].tagged_claims] == [
        {"kind": "recommendation", "text": f"also {_tag(B)}", "evidence_ids": ()},
    ]


@pytest.mark.parametrize(
    ("content", "published"),
    [
        (None, False),  # no published conclusion
        ("not json at all", True),  # published but unparseable
        (None, True),  # published without a report body
    ],
    ids=["unpublished", "unparseable", "no-report"],
)
def test_without_a_parseable_report_every_entry_is_untagged(content, published):
    rebuilt = _rebuilt(_fact(A, B), report=content, published=published)
    outcome = _project(rebuilt)
    assert outcome.status == "ok"
    assert outcome.report_available is False
    assert [h.verdict for h in outcome.entries] == ["untagged", "untagged"]
    assert all(h.claim_kinds == () for h in outcome.entries)
    assert all(h.tagged_claims == () for h in outcome.entries)
    assert all(h.handled is False for h in outcome.entries)
    assert outcome.unprovided_tags == ()
    if published:
        # The published conclusion's own evidence list, report or not.
        assert outcome.run_evidence_ids == (EV_1, EV_2)
    else:
        assert outcome.run_evidence_ids == ()


# -- handled (R8 tightening) -------------------------------------------------------


def test_handled_requires_this_runs_evidence_on_every_deciding_claim():
    claims = (
        # A: consistent and every tagged fact cites this Run -> handled.
        _claim("fact", f"{_tag(A)} confirmed", [EV_1]),
        _claim("fact", f"{_tag(A)} again", [EV_2]),
        # B: contradicted with this Run's evidence -> handled.
        _claim("rejected_hypothesis", f"{_tag(B)} refuted", [EV_1, EV_2]),
        # C: consistent per knowledge_checks, but cites evidence outside
        # the published conclusion -> not handled.
        _claim("fact", f"{_tag(C)} confirmed", [EV_FOREIGN]),
    )
    outcome = _project(_rebuilt(_fact(A, B, C), report=_report(*claims)))
    hints = _by_id(outcome)
    assert hints[A["entry_id"]].verdict == "consistent"
    assert hints[A["entry_id"]].handled is True
    assert hints[B["entry_id"]].verdict == "contradicted"
    assert hints[B["entry_id"]].handled is True
    assert hints[C["entry_id"]].verdict == "consistent"
    assert hints[C["entry_id"]].handled is False


def test_one_deciding_claim_outside_the_run_makes_the_entry_unhandled():
    claims = (
        # One tagged fact in the Run, one partly outside -> not handled.
        _claim("fact", f"{_tag(A)} confirmed", [EV_1]),
        _claim("fact", f"{_tag(A)} again", [EV_1, EV_FOREIGN]),
    )
    outcome = _project(_rebuilt(_fact(A), report=_report(*claims)))
    [hint] = outcome.entries
    assert hint.verdict == "consistent"
    assert hint.handled is False


def test_a_contradiction_citing_foreign_evidence_is_not_handled():
    claims = (
        _claim("fact", "plain fact", [EV_1]),
        _claim("rejected_hypothesis", f"{_tag(B)} refuted", [EV_FOREIGN]),
    )
    outcome = _project(_rebuilt(_fact(A, B), report=_report(*claims)))
    hint = _by_id(outcome)[B["entry_id"]]
    assert hint.verdict == "contradicted"
    assert hint.handled is False


def test_unverified_untagged_and_conflicting_are_never_handled():
    claims = (
        _claim("hypothesis", f"{_tag(A)} maybe"),
        _claim("fact", f"{_tag(C)} confirmed", [EV_1]),
        _claim("rejected_hypothesis", f"{_tag(C)} refuted", [EV_2]),
    )
    outcome = _project(_rebuilt(_fact(A, B, C), report=_report(*claims)))
    hints = _by_id(outcome)
    assert (hints[A["entry_id"]].verdict, hints[A["entry_id"]].handled) == (
        "unverified",
        False,
    )
    assert (hints[B["entry_id"]].verdict, hints[B["entry_id"]].handled) == (
        "untagged",
        False,
    )
    assert (hints[C["entry_id"]].verdict, hints[C["entry_id"]].handled) == (
        "conflicting",
        False,
    )


# -- unprovided tags (item 3, R8) --------------------------------------------------


def test_unprovided_tags_are_deduplicated_in_first_seen_order():
    unknown = f"[knowledge:{_id('never-provided')}@r1]"
    wrong_revision = _tag(A, revision=2)
    claims = (
        _claim("fact", f"{_tag(A)} ok but also {wrong_revision}", [EV_1]),
        _claim("hypothesis", f"{unknown} and {wrong_revision} again"),
        _claim("recommendation", f"{unknown} {_tag(B)}"),
    )
    report = _report(
        *claims, summary=f"Summary mentions {_tag(C, revision=9)} outside claims."
    )
    outcome = _project(_rebuilt(_fact(A, B), report=report))
    assert [_norm_tag(t) for t in outcome.unprovided_tags] == [
        _norm_tag(wrong_revision),
        _norm_tag(unknown),
    ]
    # A correct tag elsewhere still counts for the provided entry.
    assert _by_id(outcome)[A["entry_id"]].verdict == "consistent"


def test_no_unprovided_tags_when_every_tag_matches():
    claims = (
        _claim("fact", f"{_tag(A)} confirmed", [EV_1]),
        _claim("rejected_hypothesis", f"{_tag(B)} refuted", [EV_2]),
    )
    outcome = _project(_rebuilt(_fact(A, B), report=_report(*claims)))
    assert outcome.unprovided_tags == ()


# -- status (items 3, 5) -------------------------------------------------------------


def test_an_unavailable_retrieval_is_copied():
    fact = unavailable_fact(MAX_AGE)
    outcome = _project(_rebuilt(fact, report=_report(_claim("fact", "x", [EV_1]))))
    assert outcome.status == "unavailable"
    assert outcome.unknown_reasons == ()
    assert outcome.policy_version == POLICY
    assert outcome.retrieved_at is None
    assert outcome.max_age_seconds == MAX_AGE
    assert outcome.omitted_for_size == 0
    assert outcome.entries == ()
    assert outcome.report_available is True
    assert outcome.run_evidence_ids == (EV_1, EV_2)
    assert outcome.model_requests == ()


def test_an_input_without_knowledge_is_absent():
    rebuilt = _rebuilt(None, report=_report(_claim("fact", "x", [EV_1])))
    assert "knowledge" not in rebuilt["run"]["input"]["scope_facts"]
    outcome = _project(rebuilt)
    assert outcome.status == "absent"
    assert outcome.unknown_reasons == ()
    assert outcome.policy_version is None
    assert outcome.retrieved_at is None
    assert outcome.max_age_seconds is None
    assert outcome.omitted_for_size is None
    assert outcome.entries == ()
    assert outcome.run_id == str(RUN)
    assert outcome.model_requests == ()


def _assert_unknown(outcome, reason: str) -> None:
    assert outcome.status == "unknown"
    assert reason in outcome.unknown_reasons
    assert outcome.entries == ()
    assert outcome.run_evidence_ids == ()
    assert outcome.unprovided_tags == ()
    assert outcome.report_available is False
    assert outcome.policy_version is None
    assert outcome.retrieved_at is None
    assert outcome.max_age_seconds is None
    assert outcome.omitted_for_size is None
    assert outcome.model_requests == ()


_TAGGED = _report(
    _claim("fact", f"{_tag(A)} confirmed", [EV_1]),
    _claim("hypothesis", f"[knowledge:{_id('stray')}@r1] stray"),
)


@pytest.mark.parametrize(
    "where", ["records", "rebuilt", "run"], ids=["records", "rebuilt", "run"]
)
def test_any_subject_mismatch_is_unknown_without_partial_projection(where):
    rebuilt = _rebuilt(
        _fact(A, B),
        report=_TAGGED,
        incident=OTHER_INCIDENT if where == "rebuilt" else INCIDENT,
        run_incident=OTHER_INCIDENT if where == "run" else INCIDENT,
    )
    records_for = OTHER_INCIDENT if where == "records" else INCIDENT
    outcome = _project(rebuilt, records_for=records_for)
    assert outcome.subject_id == str(INCIDENT)
    _assert_unknown(outcome, "SUBJECT_MISMATCH")


def test_the_subject_is_compared_as_a_string():
    # UUID objects in the rows, a string subject in the scenario: a match.
    outcome = _project(_rebuilt(_fact(A), report=_TAGGED), subject=str(INCIDENT))
    assert outcome.status == "ok"
    # Directly built records with a string id match too.
    rebuilt = _rebuilt(_fact(A), report=_TAGGED)
    direct = knowledge_use_outcome(
        _scenario(), KnowledgeUseRecords(str(INCIDENT), rebuilt)
    )
    assert direct.status == "ok"
    assert direct.entries == outcome.entries


def _inconsistent_age(fact):
    fact["entries"][0]["age_seconds"] += 1


def _wrong_freshness(fact):
    fact["entries"][1]["freshness"] = "fresh"


def _missing_key(fact):
    del fact["entries"][0]["approved_by"]


def _bad_policy(fact):
    fact["policy_version"] = "knowledge-hint-v0"


@pytest.mark.parametrize(
    "tamper",
    [_inconsistent_age, _wrong_freshness, _missing_key, _bad_policy],
    ids=["age", "freshness", "missing-key", "policy"],
)
def test_an_invalid_knowledge_fact_is_unknown(tamper):
    rebuilt = _rebuilt(_fact(A, B), report=_TAGGED)
    tamper(rebuilt["run"]["input"]["scope_facts"]["knowledge"])
    _assert_unknown(_project(rebuilt), "INVALID_KNOWLEDGE_FACT")
