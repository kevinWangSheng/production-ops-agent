"""Pure in-memory implementer checks for the PR-C public projection."""

import json
from dataclasses import FrozenInstanceError, fields, replace
from uuid import UUID

import pytest

from opspilot.acceptance import (
    IncidentScenario,
    KnowledgeHintOutcome,
    KnowledgeUseOutcome,
    KnowledgeUseRecords,
    knowledge_use_outcome,
    knowledge_use_records,
)
from opspilot.investigation.reports import parse_report
from opspilot.knowledge.hints import knowledge_checks, unavailable_fact

INCIDENT = UUID(int=1)
RUN = UUID(int=2)
ENTRY = str(UUID(int=3))
TIME = "2026-10-15T00:00:00+00:00"
TAG = f"[knowledge:{ENTRY}@r2]"
SCENARIO = IncidentScenario("knowledge", "F13", "4", "incident", str(INCIDENT))


def fact():
    return dict(
        policy_version="knowledge-hint-v1",
        status="ok",
        retrieved_at=TIME,
        max_age_seconds=100,
        omitted_for_size=1,
        entries=[
            dict(
                entry_id=ENTRY,
                revision=2,
                name="checkout",
                tags=["payments", "errors"],
                content="historical hint",
                content_sha256="a" * 64,
                source_incident_id=str(UUID(int=4)),
                source_postmortem_id=str(UUID(int=5)),
                source_version=3,
                approved_by="alice",
                approved_at=TIME,
                observation_ended_at=TIME,
                age_seconds=0,
                freshness="fresh",
                truncated=True,
            )
        ],
    )


def claim(kind="fact", text=TAG, evidence=("e1",)):
    return dict(
        kind=kind,
        text=text,
        evidence_ids=list(evidence),
        target_refs=["checkout"],
        time_scope_ref="window",
    )


def published(claims):
    content = json.dumps(
        dict(
            schema_version="m0-report-v2",
            assessment_status="completed",
            conclusion="inconclusive",
            summary="Checked current evidence.",
            claims=claims,
            gaps=[],
            next_steps=[],
        )
    )
    return dict(
        kind="conclusion",
        assistant={"content": "not the report"},
        conclusion=dict(
            report_content=content,
            handoff_reasons=[],
            evidence_ids=["e1", "e2"],
        ),
    )


def records(knowledge, conclusion=None):
    return KnowledgeUseRecords(
        INCIDENT,
        dict(
            incident_id=INCIDENT,
            run=dict(
                incident_id=INCIDENT,
                run_id=RUN,
                state="completed",
                input={"scope_facts": {"knowledge": knowledge}},
            ),
            conclusion=conclusion,
            steps=[{"response": published([claim()])}],  # Unpublished: ignored.
        ),
    )


def test_reader_calls_rebuild_once_and_keeps_object():
    rebuilt = records(fact()).rebuilt

    class Reader:
        calls = []

        def rebuild(self, incident_id):
            self.calls.append(incident_id)
            return rebuilt

    reader = Reader()
    result = knowledge_use_records(reader, INCIDENT)
    assert reader.calls == [INCIDENT]
    assert result.incident_id == INCIDENT
    assert result.rebuilt is rebuilt
    with pytest.raises(FrozenInstanceError):
        result.incident_id = UUID(int=99)


def test_copies_snapshot_metadata_provenance_and_published_claims():
    knowledge = fact()
    extra = {**knowledge["entries"][0], "entry_id": str(UUID(int=8)), "revision": 1}
    knowledge["entries"].append(extra)
    claims = [claim("hypothesis", evidence=()), claim(), claim("recommendation")]
    committed = published(claims)
    result = knowledge_use_outcome(SCENARIO, records(knowledge, committed))
    assert isinstance(result, KnowledgeUseOutcome)
    assert result.scenario_id == SCENARIO.scenario_id
    assert result.subject_id == SCENARIO.subject_id
    assert (result.run_id, result.run_state) == (str(RUN), "completed")
    assert result.status == "ok" and result.unknown_reasons == ()
    for key in (
        "policy_version",
        "retrieved_at",
        "max_age_seconds",
        "omitted_for_size",
    ):
        assert getattr(result, key) == knowledge[key]
    assert result.report_available and result.run_evidence_ids == ("e1", "e2")
    assert result.model_requests == () and result.unprovided_tags == ()
    assert [e.entry_id for e in result.entries] == [ENTRY, extra["entry_id"]]
    hint = result.entries[0]
    assert isinstance(hint, KnowledgeHintOutcome)
    for key, value in knowledge["entries"][0].items():
        if key != "content":
            assert getattr(hint, key) == (tuple(value) if key == "tags" else value)
    assert "content" not in {f.name for f in fields(hint)}
    parsed, _ = parse_report(
        committed["conclusion"]["report_content"], finish_reason="stop"
    )
    expected = knowledge_checks(knowledge, parsed)
    for entry, check in zip(result.entries, expected):
        assert entry.verdict == check["verdict"]
        assert entry.claim_kinds == tuple(check["claim_kinds"])
    assert hint.tagged_claims == tuple(
        {
            k: tuple(v) if k == "evidence_ids" else v
            for k, v in c.items()
            if k in {"kind", "text", "evidence_ids"}
        }
        for c in claims
    )
    assert hint.handled and not result.entries[1].handled
    with pytest.raises(FrozenInstanceError):
        hint.handled = False
    with pytest.raises(FrozenInstanceError):
        result.status = "unknown"


@pytest.mark.parametrize(
    "claims,verdict,handled",
    [
        ([claim()], "consistent", True),
        ([claim("rejected_hypothesis")], "contradicted", True),
        ([claim(), claim("rejected_hypothesis")], "conflicting", False),
        ([claim("hypothesis", evidence=())], "unverified", False),
        ([claim("counter_evidence")], "unverified", False),
        ([claim(text="no knowledge tag")], "untagged", False),
        ([claim(evidence=("e1", "foreign"))], "consistent", False),
        ([claim(), claim(evidence=("foreign",))], "consistent", False),
        ([claim("rejected_hypothesis", evidence=("foreign",))], "contradicted", False),
    ],
)
def test_verdict_and_current_run_evidence_membership(claims, verdict, handled):
    result = knowledge_use_outcome(SCENARIO, records(fact(), published(claims)))
    assert result.report_available
    assert result.entries[0].verdict == verdict
    assert result.entries[0].handled is handled


@pytest.mark.parametrize(
    "failure",
    ["unpublished", "malformed", "length", "empty_evidence", "missing_claim_evidence"],
)
def test_unavailable_report_and_missing_evidence_never_handle(failure):
    committed = published([claim()])
    if failure == "unpublished":
        committed = None
    elif failure == "malformed":
        committed["conclusion"]["report_content"] = "invalid json"
    elif failure == "length":
        committed["conclusion"]["handoff_reasons"] = ["OUTPUT_LENGTH"]
    elif failure == "empty_evidence":
        committed["conclusion"]["evidence_ids"] = []
    else:
        committed = published([claim(evidence=())])
    result = knowledge_use_outcome(SCENARIO, records(fact(), committed))
    assert not result.entries[0].handled
    assert result.report_available is (failure == "empty_evidence")
    if not result.report_available:
        assert result.entries[0].verdict == "untagged"
        assert result.entries[0].tagged_claims == ()
    assert result.run_evidence_ids == (
        () if failure in {"unpublished", "empty_evidence"} else ("e1", "e2")
    )


def test_unknown_tags_preserve_spelling_deduplicate_and_follow_claim_order():
    old = f"[knowledge:{ENTRY}@r1]"
    other = f"[knowledge:{UUID(int=9)}@r1]"
    malformed = "[knowledge:broken]"
    result = knowledge_use_outcome(
        SCENARIO,
        records(
            fact(),
            published(
                [
                    claim(text=f"{TAG} {old} {malformed} {old}"),
                    claim(text=f"{other} {old}"),
                ]
            ),
        ),
    )
    assert result.unprovided_tags == (old, malformed, other)


@pytest.mark.parametrize("status", ["absent", "unavailable", "ok"])
def test_empty_knowledge_states_still_project_published_report(status):
    knowledge = unavailable_fact(100) if status == "unavailable" else fact()
    knowledge["entries"] = []
    source = records(knowledge, published([claim()]))
    if status == "absent":
        del source.rebuilt["run"]["input"]["scope_facts"]["knowledge"]
    result = knowledge_use_outcome(SCENARIO, source)
    assert result.status == status and result.entries == ()
    assert result.report_available and result.run_evidence_ids == ("e1", "e2")
    assert result.unprovided_tags == (TAG,)
    assert result.unknown_reasons == ()
    if status == "absent":
        assert (
            result.policy_version,
            result.retrieved_at,
            result.max_age_seconds,
            result.omitted_for_size,
        ) == (None,) * 4


@pytest.mark.parametrize("location", ["records", "incident", "run"])
def test_each_subject_mismatch_suppresses_whole_projection(location):
    source = records(fact(), published([claim()]))
    if location == "records":
        source = replace(source, incident_id=UUID(int=99))
    elif location == "incident":
        source.rebuilt["incident_id"] = UUID(int=99)
    else:
        source.rebuilt["run"]["incident_id"] = UUID(int=99)
    assert_unknown(knowledge_use_outcome(SCENARIO, source), "SUBJECT_MISMATCH")


def assert_unknown(result, reason):
    assert result.status == "unknown" and result.unknown_reasons == (reason,)
    assert result.subject_id == SCENARIO.subject_id
    assert result.entries == result.run_evidence_ids == result.unprovided_tags == ()
    assert not result.report_available
    assert (
        result.policy_version,
        result.retrieved_at,
        result.max_age_seconds,
        result.omitted_for_size,
    ) == (None,) * 4
    assert result.model_requests == ()


@pytest.mark.parametrize(
    "knowledge", [None, {}, {**fact(), "status": "bad"}, {**fact(), "entries": [{}]}]
)
def test_invalid_present_knowledge_is_unknown(knowledge):
    assert_unknown(
        knowledge_use_outcome(SCENARIO, records(knowledge, published([claim()]))),
        "INVALID_KNOWLEDGE_FACT",
    )


def test_string_identity_comparison_and_stale_snapshot_are_preserved():
    knowledge = fact()
    knowledge["max_age_seconds"] = 0
    knowledge["retrieved_at"] = "2026-10-15T00:00:01+00:00"
    knowledge["entries"][0].update(age_seconds=1, freshness="stale")
    source = records(knowledge, published([claim("rejected_hypothesis")]))
    source.rebuilt["incident_id"] = str(INCIDENT)
    source.rebuilt["run"]["incident_id"] = str(INCIDENT)
    result = knowledge_use_outcome(SCENARIO, source)
    assert result.status == "ok"
    assert result.entries[0].freshness == "stale"
    assert result.entries[0].age_seconds == 1
    assert result.entries[0].handled


def test_model_round_reading_uses_assistant_content_and_finish_reason():
    committed = published([claim()])
    model_round = dict(
        kind="model_round",
        assistant={"content": committed["conclusion"]["report_content"]},
        finish_reason="stop",
    )
    result = knowledge_use_outcome(SCENARIO, records(fact(), model_round))
    assert result.report_available and result.entries[0].verdict == "consistent"
    assert result.run_evidence_ids == () and not result.entries[0].handled
