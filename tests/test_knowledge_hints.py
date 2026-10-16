"""Implementer checks for the bounded historical knowledge hint interface."""

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from opspilot.investigation.context import (
    INPUT_VERSION_KNOWLEDGE,
    KNOWLEDGE_BOUNDARY,
    ContextError,
    InvestigationInput,
    initial_messages,
)
from opspilot.investigation.loop import prompt_revision_versions
from opspilot.knowledge.hints import (
    bound_entries,
    hint_content,
    knowledge_checks,
    unavailable_fact,
    valid_knowledge,
)
from opspilot.persistence import PersistenceError
from opspilot.tools.fixture import fixture_face
from opspilot.tools.registry import canonical
from tests.m1_web_support import basic, build_workbench, call, submit_incident

NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)


def fact():
    return dict(
        policy_version="knowledge-hint-v1",
        status="ok",
        retrieved_at=NOW.isoformat(),
        max_age_seconds=100,
        omitted_for_size=0,
        entries=[
            dict(
                entry_id=str(uuid4()),
                revision=1,
                name="checkout",
                tags=["errors"],
                content=canonical(
                    {
                        "checks": ["verify"],
                        "evidence_refs": {"omitted": True, "count": 1},
                    }
                ),
                content_sha256="a" * 64,
                source_incident_id=str(uuid4()),
                source_postmortem_id=str(uuid4()),
                source_version=1,
                approved_by="alice",
                approved_at=NOW.isoformat(),
                observation_ended_at=NOW.isoformat(),
                age_seconds=0,
                freshness="fresh",
                truncated=False,
            )
        ],
    )


def input_for(knowledge):
    fresh = fixture_face().input_for(
        run_id=str(uuid4()),
        question="why?",
        target_id="checkout",
        deadline=NOW + timedelta(minutes=10),
        model_requests=4,
    )
    return replace(fresh, scope_facts={**fresh.scope_facts, "knowledge": knowledge})


def test_content_bounds_and_omitted_references():
    content, cut = hint_content(
        canonical({"checks": "查看" * 3000, "evidence_refs": ["old-id"]})
    )
    assert cut and len(content.encode()) <= 4096 and "old-id" not in content
    content, cut = hint_content(canonical({"checks": [], "evidence_refs": ["old-id"]}))
    assert not cut and json.loads(content)["evidence_refs"] == {
        "omitted": True,
        "count": 1,
    }
    entries = [{"content": "x" * 4096, "rank": i} for i in range(3)]
    kept, omitted = bound_entries(entries)
    assert kept == entries[:2] and omitted == 1
    assert len(canonical(kept).encode()) <= 12288


def test_v5_roundtrip_and_prefix_order():
    knowledge = fact()
    fresh = input_for(knowledge)
    assert fresh.version == INPUT_VERSION_KNOWLEDGE
    assert InvestigationInput.from_json(fresh.as_json()) == fresh
    messages, _ = initial_messages(fresh, evidence_context={"current": "evidence"})
    assert messages[-2] == {
        "role": "user",
        "content": KNOWLEDGE_BOUNDARY + "\n" + canonical(knowledge["entries"]),
    }
    assert messages[-1]["content"] == canonical({"current": "evidence"})
    assert "old-id" not in str(messages)


@pytest.mark.parametrize(
    "change",
    [
        {"status": "broken"},
        {"retrieved_at": "yesterday"},
        {"entries": {}},
        {"max_age_seconds": True},
        {"omitted_for_size": -1},
        {"policy_version": "other"},
    ],
)
def test_malformed_facts_refused(change):
    with pytest.raises(ContextError, match="INPUT_INVALID"):
        input_for({**fact(), **change})


@pytest.mark.parametrize(
    "change",
    [
        {"revision": True},
        {"entry_id": "no"},
        {"truncated": 1},
        {"content": "a" * 4097},
        {"freshness": "stale"},
        {"approved_at": "bad"},
        {"age_seconds": 5},
        {"observation_ended_at": "2020-01-01T00:00:00+00:00"},
    ],
)
def test_malformed_entries_refused(change):
    knowledge = fact()
    knowledge["entries"][0].update(change)
    assert not valid_knowledge(knowledge)
    with pytest.raises(ContextError, match="INPUT_INVALID"):
        input_for(knowledge)


@pytest.mark.parametrize(
    "knowledge",
    [
        unavailable_fact(100),
        {**unavailable_fact(100), "status": "ok", "retrieved_at": NOW.isoformat()},
    ],
)
def test_empty_knowledge_is_recorded_but_not_sent(knowledge):
    fresh = input_for(knowledge)
    messages, _ = initial_messages(fresh, evidence_context=None)
    assert len(messages) == 2 and fresh.version == INPUT_VERSION_KNOWLEDGE


@pytest.mark.parametrize(
    "kinds,expected",
    [
        ([], "untagged"),
        ([("hypothesis", [])], "unverified"),
        ([("fact", [])], "unverified"),
        ([("fact", ["current"])], "consistent"),
        ([("rejected_hypothesis", ["current"])], "contradicted"),
        ([("fact", ["a"]), ("rejected_hypothesis", ["b"])], "conflicting"),
    ],
)
def test_tag_verdicts(kinds, expected):
    knowledge = fact()
    e = knowledge["entries"][0]
    tag = f"[knowledge:{e['entry_id']}@r1]"
    claims = [dict(kind=k, text=tag, evidence_ids=ids) for k, ids in kinds]
    result = knowledge_checks(knowledge, {"claims": claims})[0]
    assert result["verdict"] == expected
    assert result["claim_kinds"] == list(dict.fromkeys(k for k, _ in kinds))
    assert knowledge_checks(
        knowledge, SimpleNamespace(claims=[SimpleNamespace(**c) for c in claims])
    ) == [result]
    assert knowledge_checks(knowledge, None)[0]["verdict"] == "untagged"
    assert (
        knowledge_checks(
            knowledge,
            {
                "claims": [
                    {
                        "kind": "fact",
                        "text": tag.replace("@r1", "@r2"),
                        "evidence_ids": ["x"],
                    }
                ]
            },
        )[0]["verdict"]
        == "untagged"
    )


def test_boundary_changes_prompt_revision(monkeypatch):
    from opspilot.investigation import loop

    before = prompt_revision_versions()
    monkeypatch.setattr(loop, "KNOWLEDGE_BOUNDARY", "changed")
    assert prompt_revision_versions() != before


class Reader:
    def __init__(self, knowledge=None, error=None):
        self.knowledge = knowledge
        self.error = error
        self.calls = []

    def knowledge_for_target(self, resource_uid, query_text, **kwargs):
        self.calls.append((resource_uid, query_text, kwargs))
        if self.error:
            raise self.error
        return self.knowledge


def test_intake_successor_refresh_and_page():
    app, workbench, clock = build_workbench(tool_face=fixture_face())
    first = fact()
    reader = Reader(first)
    workbench.knowledge = reader
    result = submit_incident(app)
    assert result.status == 201
    incident_id = UUID(result.json()["incident_id"])
    view = workbench.snapshot(incident_id)["knowledge"]
    assert (
        view["entries"] == first["entries"]
        and view["checks"][0]["verdict"] == "untagged"
    )
    page = call(app, "GET", f"/incidents/{incident_id}", headers=basic())
    assert page.status == 200
    assert 'id="knowledge-hints"' in page.text
    assert f"/knowledge/{first['entries'][0]['entry_id']}" in page.text
    reader.knowledge = {**fact(), "entries": []}
    summary = workbench.incidents.find_incident(incident_id)
    successor = workbench._successor_input(
        summary, uuid4(), clock.now() + timedelta(minutes=10)
    )
    assert successor["scope_facts"]["knowledge"]["entries"] == []
    assert len(reader.calls) == 2 and reader.calls[0] == reader.calls[1]
    workbench.knowledge = None
    successor = workbench._successor_input(
        summary, uuid4(), clock.now() + timedelta(minutes=10)
    )
    assert "knowledge" not in successor["scope_facts"]


@pytest.mark.parametrize(
    "error",
    [
        PersistenceError("STORAGE_UNAVAILABLE"),
        ConnectionError("secret dsn"),
        TimeoutError("secret dsn"),
    ],
)
def test_retrieval_failure_keeps_intake_and_hides_secrets(error, caplog):
    app, workbench, _ = build_workbench(tool_face=fixture_face())
    workbench.knowledge = Reader(error=error)
    result = submit_incident(app)
    assert result.status == 201
    view = workbench.snapshot(UUID(result.json()["incident_id"]))["knowledge"]
    assert view["status"] == "unavailable" and view["retrieved_at"] is None
    assert "secret dsn" not in caplog.text
