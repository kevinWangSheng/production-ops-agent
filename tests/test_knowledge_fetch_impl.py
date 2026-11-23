"""Implementer unit tests for C2b (#566): fetch_skill, the v7 runbook list,
the conditional prompt revision and the fixture import script. No PostgreSQL.

The contract, acceptance and safety tests are the independent test author's;
these pin the implementation's own pieces.
"""

from __future__ import annotations

import io
import json
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from opspilot.investigation.context import (
    INPUT_VERSION_RUNBOOK_CATALOG,
    RUNBOOK_CATALOG,
    RUNBOOK_CATALOG_BOUNDARY,
    ContextError,
    InvestigationInput,
    catalog_max_age,
    initial_messages,
)
from opspilot.investigation.limits import M1_FROZEN_LIMITS
from opspilot.investigation.loop import DISCIPLINE_VARIANT, prompt_revision_versions
from opspilot.knowledge.catalog import (
    MAX_ENTRIES,
    build_catalog,
    unavailable_catalog,
    valid_catalog,
)
from opspilot.persistence import PersistenceError
from opspilot.tools import knowledge_fetch, otel_demo_ext
from opspilot.tools.executor import (
    TransportRequest,
    TransportResponse,
    TransportUnavailable,
)
from opspilot.tools.outcomes import Window
from opspilot.tools.registry import ToolRegistry
from scripts.holmes_evals import import_skills
from tests.m1_tool_support import body, build, registration, request

NOW = datetime(2026, 11, 20, 8, 0, tzinfo=timezone.utc)


def _rows(*names: str, age: int = 60) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "entry_id": uuid4(),
            "revision": 1,
            "description": f"{name} steps password=hunter2",
            "approved_at": NOW - timedelta(seconds=age),
            "age_seconds": age,
        }
        for name in names
    ]


# -- the catalog fact ------------------------------------------------------------


def test_the_catalog_is_sorted_redacted_and_valid() -> None:
    fact = build_catalog(_rows("b-db", "a-net"), retrieved_at=NOW, max_age_seconds=30)
    assert [e["skill_id"] for e in fact["entries"]] == ["a-net", "b-db"]
    assert all("hunter2" not in e["description"] for e in fact["entries"])
    assert {e["freshness"] for e in fact["entries"]} == {"stale"}
    assert valid_catalog(fact)
    assert valid_catalog(unavailable_catalog(30))


def test_the_catalog_drops_past_its_bounds_and_counts_them() -> None:
    names = [f"rb-{i:03d}" for i in range(MAX_ENTRIES + 5)]
    fact = build_catalog(_rows(*names), retrieved_at=NOW, max_age_seconds=3600)
    assert len(fact["entries"]) == MAX_ENTRIES
    assert fact["omitted_for_size"] == 5
    assert fact["entries"][-1]["skill_id"] == names[MAX_ENTRIES - 1]


@pytest.mark.parametrize(
    "tamper",
    [
        lambda f: f["entries"].reverse(),
        lambda f: f["entries"][0].update(freshness="fresh"),
        lambda f: f["entries"][0].update(revision=0),
        lambda f: f["entries"][0].update(skill_id="Bad Name"),
        lambda f: f.update(extra=1),
        lambda f: f.update(retrieved_at=None),
    ],
)
def test_a_tampered_catalog_is_invalid(tamper: Any) -> None:
    fact = build_catalog(_rows("a", "b"), retrieved_at=NOW, max_age_seconds=30)
    tamper(fact)
    assert not valid_catalog(fact)


# -- input v7 ----------------------------------------------------------------------


def _input(facts: dict[str, Any]) -> InvestigationInput:
    return InvestigationInput(
        question="Why is checkout failing?",
        model_requests=4,
        limits=M1_FROZEN_LIMITS,
        tool_schemas=(),
        evidence_context=None,
        variant_id=DISCIPLINE_VARIANT,
        bound_target_id="checkout",
        scope_facts={"target_ids": ["checkout"], **facts},
    )


def test_the_catalog_makes_v7_and_one_message_after_knowledge() -> None:
    fact = build_catalog(_rows("db"), retrieved_at=NOW, max_age_seconds=3600)
    value = _input({RUNBOOK_CATALOG: fact})
    assert value.version == INPUT_VERSION_RUNBOOK_CATALOG
    assert InvestigationInput.from_json(value.as_json()) == value
    messages, _ = initial_messages(value, evidence_context=None)
    last = messages[-1]["content"]
    assert last.startswith(RUNBOOK_CATALOG_BOUNDARY)
    listed = json.loads(last.split("\n", 1)[1])
    assert listed == [
        {
            "skill_id": "db",
            "description": fact["entries"][0]["description"],
            "revision": 1,
            "freshness": "fresh",
        }
    ]
    assert catalog_max_age(value.scope_facts) == 3600


def test_an_empty_catalog_is_recorded_but_sends_nothing() -> None:
    plain, _ = initial_messages(_input({}), evidence_context=None)
    empty = build_catalog([], retrieved_at=NOW, max_age_seconds=10)
    with_fact, _ = initial_messages(
        _input({RUNBOOK_CATALOG: empty}), evidence_context=None
    )
    assert with_fact == plain
    assert _input({RUNBOOK_CATALOG: empty}).version == INPUT_VERSION_RUNBOOK_CATALOG


def test_a_v7_row_with_a_bad_catalog_is_refused() -> None:
    fact = build_catalog(_rows("db"), retrieved_at=NOW, max_age_seconds=3600)
    fact["entries"][0]["freshness"] = "stale"
    with pytest.raises(ContextError, match="INPUT_INVALID"):
        _input({RUNBOOK_CATALOG: fact})


# -- versions ------------------------------------------------------------------------


def test_switch_off_keeps_every_revision() -> None:
    assert prompt_revision_versions() == prompt_revision_versions(knowledge_tool=False)
    assert (
        prompt_revision_versions(knowledge_tool=True)["prompt_revision"]
        != prompt_revision_versions()["prompt_revision"]
    )
    assert otel_demo_ext.tool_schema_revision() == otel_demo_ext.TOOL_SCHEMA_REVISION
    assert otel_demo_ext.TOOL_SCHEMA_REVISION == "otel-demo-ext-a44404dc7e6b"
    assert (
        otel_demo_ext.tool_schema_revision(live_metrics=True)
        == "otel-demo-ext-0fe319ba4792"
    )
    assert otel_demo_ext.KNOWLEDGE_TOOL_SCHEMA_REVISION not in {
        otel_demo_ext.TOOL_SCHEMA_REVISION,
        otel_demo_ext.LIVE_METRICS_TOOL_SCHEMA_REVISION,
    }
    on = otel_demo_ext.otel_demo_ext_versions(knowledge=True)
    off = otel_demo_ext.otel_demo_ext_versions()
    assert on["prompt_revision"] != off["prompt_revision"]
    assert otel_demo_ext.KNOWLEDGE_TOOL_NAMES[-1] == "fetch_skill"


def test_switch_values() -> None:
    assert knowledge_fetch.knowledge_tool_enabled({}) is False
    assert knowledge_fetch.knowledge_tool_enabled({knowledge_fetch.ENV: "0"}) is False
    assert knowledge_fetch.knowledge_tool_enabled({knowledge_fetch.ENV: "1"}) is True
    with pytest.raises(SystemExit, match="KNOWLEDGE_TOOL_INVALID"):
        knowledge_fetch.knowledge_tool_enabled({knowledge_fetch.ENV: "on"})


def test_a_not_citable_registration_is_fingerprinted_only_when_false() -> None:
    plain = registration()
    assert ToolRegistry([plain]).revision == ToolRegistry([registration()]).revision
    assert (
        ToolRegistry([registration(citable_as_fact=False)]).revision
        != ToolRegistry([plain]).revision
    )


def test_an_ok_view_of_a_not_citable_tool_is_not_citable() -> None:
    executor, transport, _, _ = build(
        registrations=[registration(citable_as_fact=False)]
    )
    transport.response = TransportResponse(body=body([{"metric": "x", "value": 1}]))
    outcome = executor.execute(request())
    assert outcome.status == "ok"
    assert outcome.model_view["citable_as_fact"] is False


# -- the transport ---------------------------------------------------------------------


class _Reader:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def read(self, resource_uid: str, skill_id: str) -> Any:
        self.calls.append((resource_uid, skill_id))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _request(skill_id: object) -> TransportRequest:
    return TransportRequest(
        operation_id="op",
        source="otel-demo",
        verb="get",
        endpoint="http://lab.invalid",
        selector={},
        params={"skill_id": skill_id},
        window=Window(NOW - timedelta(minutes=10), NOW),
        timeout_seconds=5.0,
        max_result_bytes=knowledge_fetch.MAX_RESULT_BYTES,
        credential_ref="",
        tool="fetch_skill",
    )


def _transport(result: Any, target: str | None = "checkout") -> tuple[Any, _Reader]:
    reader = _Reader(result)
    return (
        knowledge_fetch.KnowledgeTransport(
            reader=reader,  # type: ignore[arg-type]
            resource_uid=target,
            max_age_seconds=3600,
        ),
        reader,
    )


@pytest.mark.parametrize("skill_id", ["", "  ", "x" * 201, "a\x00b", 7, None])
def test_bad_skill_ids_are_refused_before_any_read(skill_id: object) -> None:
    transport, reader = _transport({"status": "not_found", "available": []})
    response = transport.fetch(_request(skill_id))
    assert response.source_status == "INVALID_SKILL_ID" and not response.sent
    assert reader.calls == []


def test_an_unbound_run_reads_nothing() -> None:
    transport, reader = _transport({"status": "not_found", "available": []}, None)
    assert transport.fetch(_request("db")).source_status == "KNOWLEDGE_NOT_FOUND"
    assert reader.calls == []


def test_not_found_lists_the_available_names() -> None:
    transport, reader = _transport({"status": "not_found", "available": ["a", "b"]})
    response = transport.fetch(_request("custom-crd"))
    assert response.source_status == "KNOWLEDGE_NOT_FOUND"
    assert reader.calls == [("checkout", "custom-crd")]
    assert response.view_fields is not None
    notes = response.view_fields["notes"]
    assert notes[0].startswith("KNOWLEDGE_NOT_FOUND: ")
    assert "a, b" in notes[1]


def test_not_available_returns_no_content() -> None:
    transport, _ = _transport({"status": "not_available", "state": "revoked"})
    response = transport.fetch(_request("db"))
    assert response.source_status == "KNOWLEDGE_NOT_AVAILABLE"
    assert b"content" not in response.body


def test_storage_failure_is_unavailable() -> None:
    transport, _ = _transport(PersistenceError("STORAGE_UNAVAILABLE"))
    with pytest.raises(TransportUnavailable):
        transport.fetch(_request("db"))


def test_an_ok_runbook_projection() -> None:
    approved = NOW - timedelta(seconds=7200)
    row = {
        "entry_id": uuid4(),
        "revision": 2,
        "name": "db",
        "description": "d",
        "content": "steps",
        "content_sha256": "0" * 64,
        "source_label": "HolmesGPT 评测夹具",
        "source_ref": "ref",
        "approved_by": "alice",
        "approved_at": approved,
        "fresh_from": approved,
        "state": "active",
    }
    transport, _ = _transport(
        {"status": "ok", "kind": "runbook_import", "row": row, "now": NOW}
    )
    response = transport.fetch(_request("db"))
    [result] = json.loads(response.body)["results"]
    assert result["skill_id"] == "db" and result["content"] == "steps"
    assert result["freshness"] == {
        "age_seconds": 7200,
        "state": "stale",
        "max_age_seconds": 3600,
    }
    assert result["source"]["source_label"] == "HolmesGPT 评测夹具"
    assert response.data_as_of == approved
    assert response.view_fields is not None
    assert response.view_fields["cannot_prove"] == knowledge_fetch._CANNOT_PROVE


def test_a_postmortem_projection_hides_historical_evidence_ids() -> None:
    content = json.dumps({"symptoms": ["x"], "evidence_refs": [{"evidence_id": "old"}]})
    row = {
        "entry_id": uuid4(),
        "revision": 1,
        "name": "pm",
        "tags": ["t"],
        "content": content,
        "content_sha256": "0" * 64,
        "approved_by": "bob",
        "approved_at": NOW,
        "fresh_from": NOW,
        "source_postmortem_id": uuid4(),
        "source_version": 1,
        "source_incident_id": uuid4(),
        "state": "active",
    }
    transport, _ = _transport(
        {"status": "ok", "kind": "postmortem", "row": row, "now": NOW}
    )
    [result] = json.loads(transport.fetch(_request(str(row["entry_id"]))).body)[
        "results"
    ]
    assert "old" not in result["content"]
    assert json.loads(result["content"])["evidence_refs"] == {
        "omitted": True,
        "count": 1,
    }


# -- the import script -----------------------------------------------------------------


def _question(tmp_path: Path) -> Path:
    question = tmp_path / "94_skill_transparency"
    skill = question / "database-troubleshooting"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        '---\nname: database-troubleshooting\ndescription: "DB"\n---\n# Steps\n1. a\n'
    )
    (question / "test_case.yaml").write_text("expected_output: secret\n")
    (question / "global_instructions.json").write_text("{}")
    return question


def test_only_the_skill_file_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    question = _question(tmp_path)
    opened: list[str] = []
    original = Path.read_bytes

    def spy(self: Path) -> bytes:
        opened.append(self.name)
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", spy)
    [fixture] = import_skills.read_fixtures(question)
    assert opened == ["SKILL.md"]
    assert fixture.name == "database-troubleshooting"
    assert fixture.content == "# Steps\n1. a\n"
    assert fixture.source_ref.startswith(
        "holmesgpt@a0009195:tests/llm/fixtures/test_ask_holmes/94_skill_transparency/"
    )


class _Opener:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, list[str]], dict[str, str]]] = []

    def open(self, req: Any, timeout: float) -> Any:
        fields = urllib.parse.parse_qs(req.data.decode())
        self.sent.append((req.full_url, fields, dict(req.header_items())))
        if req.full_url.endswith("/runbooks"):
            reply = {"submission_id": "s-1"}
        else:
            reply = {"entry_id": "e-1", "revision": 1, "entry_generation": 1}
        stream = io.BytesIO(json.dumps(reply).encode())
        stream.status = 200  # type: ignore[attr-defined]
        return _Context(stream)


class _Context:
    def __init__(self, stream: Any) -> None:
        self.stream = stream

    def __enter__(self) -> Any:
        return self.stream

    def __exit__(self, *exc: object) -> None:
        return None


def test_import_submits_and_approves_through_the_workbench(tmp_path: Path) -> None:
    account = import_skills.Account(
        "http://127.0.0.1:8080", "eval", "pw", "http://127.0.0.1:8080"
    )
    opener = _Opener()
    rows, refusals = import_skills.import_question(
        account,
        _question(tmp_path),
        "m0-otel-20260909",
        opener=opener,  # type: ignore[arg-type]
    )
    assert refusals == [] and rows[0]["entry_id"] == "e-1"
    submit, approve = opener.sent
    assert submit[0].endswith("/runbooks")
    assert submit[1]["source_label"] == ["HolmesGPT 评测夹具"]
    assert submit[1]["resource_uid"] == ["m0-otel-20260909"]
    assert submit[2]["Origin"] == "http://127.0.0.1:8080"
    assert approve[0].endswith("/runbooks/submissions/s-1/review")
    assert approve[1]["action"] == ["approve"]


# -- the revision a resumed Run records (lead item 3) -------------------------------


@pytest.mark.parametrize("with_catalog", [False, True])
def test_a_resumed_run_records_the_revision_its_input_implies(
    with_catalog: bool,
) -> None:
    from dataclasses import replace

    from opspilot.investigation.context import Transcript, delivered_from_context
    from tests.m1_investigation_support import assemble, reply, report_json

    loop, request, _, _, _, _ = assemble(
        replies=[
            reply(content=report_json(evidence_id="missing", status="incomplete"))
        ],
        model_requests=1,
    )
    value = request.as_input()
    if with_catalog:
        empty = build_catalog([], retrieved_at=NOW, max_age_seconds=60)
        value = replace(
            value, scope_facts={**value.scope_facts, RUNBOOK_CATALOG: empty}
        )
    messages, _ = initial_messages(value, evidence_context=request.evidence_context)
    delivered = delivered_from_context(request.evidence_context, run_id=request.run_id)
    transcript = Transcript(
        run_id=request.run_id,
        input=value,
        evidence_context=request.evidence_context,
        messages=messages,
        delivered=list(delivered),
        evidence_ids=[view.evidence_id for view in delivered],
        next_round=1,
        segment="initial",
        live_steps=0,
        prefix_len=len(messages),
    )
    outcome = loop.resume(transcript)
    expected = prompt_revision_versions(knowledge_tool=with_catalog)["prompt_revision"]
    assert outcome.prompt_revision == expected


# -- runbook report sentence (2026-11-21, after the 2026-11-20 rental) -------------


class _StillClock:
    def now(self) -> datetime:
        return NOW


def test_the_runbook_report_sentence_rides_only_with_the_knowledge_tool() -> None:
    from opspilot.knowledge.catalog import RUNBOOK_REPORT_SENTENCE

    for word in ("skill_id", "summary", "next_steps", "which tool was missing"):
        assert word in RUNBOOK_REPORT_SENTENCE, word

    def sent(knowledge: bool) -> list[str]:
        face = otel_demo_ext.otel_demo_ext_face(_StillClock(), knowledge=knowledge)
        texts = [s["function"]["description"] for s in face.tool_schemas]
        facts = (
            {
                RUNBOOK_CATALOG: build_catalog(
                    _rows("db"), retrieved_at=NOW, max_age_seconds=3600
                )
            }
            if knowledge
            else {}
        )
        messages, _ = initial_messages(_input(facts), evidence_context=None)
        return texts + [str(m["content"]) for m in messages]

    on = [t for t in sent(True) if RUNBOOK_REPORT_SENTENCE in t]
    assert len(on) == 2
    assert any(t.startswith(RUNBOOK_CATALOG_BOUNDARY) for t in on)
    assert knowledge_fetch.TOOL_SCHEMAS[0]["function"]["description"] in on
    assert not [t for t in sent(False) if RUNBOOK_REPORT_SENTENCE in t]
