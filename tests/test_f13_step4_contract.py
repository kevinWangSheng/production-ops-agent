"""Independent contract tests for F13 step 4 (PR-B): reviewed knowledge in
a later investigation's input (no database).

Written from the task record's public interface
(``docs/tasks/2026-10-15-f13-step4.md``, items 1-7 and the reversible
details) and the contract decision tables
(``docs/tasks/2026-10-15-f13-step4-contract.md``, U1-U8, R1-R10), without
reading the implementation. Covered here: input v5 (round trip, older
versions, malformed facts, version/fact agreement), the one knowledge
message in the fixed prefix, ``prompt_revision``, ``knowledge_checks``
verdicts, and the workbench recording the retrieval in the Run input
(web submit, successor Run, unavailable store, disabled). The PostgreSQL
retrieval itself is in ``tests/integration/test_f13_step4_contract_postgres.py``.

New symbols are looked up inside each test so a missing one fails only the
tests that need it.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid5

import pytest

from opspilot.alertmanager import alert_context
from opspilot.investigation import context as context_module
from opspilot.investigation import loop as loop_module
from opspilot.investigation.context import (
    ALERT_CONTEXT_BOUNDARY,
    ContextError,
    InvestigationInput,
    initial_messages,
)
from opspilot.investigation.inputs import AlertAnchor
from opspilot.investigation.loop import prompt_revision_versions
from opspilot.persistence import PersistenceError
from opspilot.tools.fixture import fixture_face
from opspilot.tools.otel_demo import otel_demo_face
from opspilot.tools.registry import canonical
from tests.m1_tool_support import FakeClock
from tests.m1_web_support import (
    basic,
    build_workbench,
    call,
    post_form,
    same_origin,
    submit_incident,
)

# Idempotency labels for submitted incidents.
LABEL_WEB = "f13s4-web"
LABEL_THRESHOLD = "f13s4-threshold"
LABEL_UNAVAILABLE = "f13s4-unavailable"
LABEL_OFF = "f13s4-off"
LABEL_SUCCESSOR = "f13s4-successor"
LABEL_PAGE = "f13s4-page"
LABEL_PAGE_UNAVAILABLE = "f13s4-page-unavailable"

V1 = "opspilot-investigation-input-v1"
V2 = "opspilot-investigation-input-v2"
V3 = "opspilot-investigation-input-v3"
V4 = "opspilot-investigation-input-v4"
V5 = "opspilot-investigation-input-v5"
POLICY = "knowledge-hint-v1"
DEFAULT_MAX_AGE = 2592000
QUESTION = "Why is checkout erroring?"
RECEIVED = datetime(2026, 10, 10, 12, 0, 30, tzinfo=timezone.utc)
STARTS = datetime(2026, 10, 10, 11, 41, 54, tzinfo=timezone.utc)
DEADLINE = RECEIVED + timedelta(minutes=10)
RETRIEVED_AT = datetime(2026, 10, 14, 12, 0, 0, tzinfo=timezone.utc)
_NS = UUID("6f6b6e6f-776c-6564-6765-000000000013")
HTML = {**basic(), **same_origin(), "accept": "text/html"}


def _id(name: str) -> str:
    return str(uuid5(_NS, name))


def _entry(name: str, *, revision: int = 1, freshness: str = "fresh", age=None):
    """One retrieved entry, internally consistent: ``age_seconds`` is the
    distance from ``RETRIEVED_AT`` to ``observation_ended_at`` and
    ``freshness`` agrees with it under ``DEFAULT_MAX_AGE``."""
    if age is None:
        age = 3600 if freshness == "fresh" else DEFAULT_MAX_AGE + 3600
    observed = RETRIEVED_AT - timedelta(seconds=age)
    content = canonical(
        {
            "symptoms": [f"{name} 5xx"],
            "checks": ["error rate"],
            "evidence_refs": {"omitted": True, "count": 1},
        }
    )
    return {
        "entry_id": _id(f"entry-{name}"),
        "revision": revision,
        "name": f"knowledge {name}",
        "tags": ["checkout", name],
        "content": content,
        "content_sha256": hashlib.sha256(name.encode()).hexdigest(),
        "source_incident_id": _id(f"incident-{name}"),
        "source_postmortem_id": _id(f"postmortem-{name}"),
        "source_version": 1,
        "approved_by": "alice",
        "approved_at": (observed + timedelta(minutes=30)).isoformat(),
        "observation_ended_at": observed.isoformat(),
        "age_seconds": age,
        "freshness": freshness,
        "truncated": False,
    }


def _fact(*entries, status="ok", omitted_for_size=0, max_age=DEFAULT_MAX_AGE):
    if status == "unavailable":
        retrieved_at = None
    else:
        retrieved_at = RETRIEVED_AT.isoformat()
    return {
        "policy_version": POLICY,
        "status": status,
        "retrieved_at": retrieved_at,
        "max_age_seconds": max_age,
        "omitted_for_size": omitted_for_size,
        "entries": list(entries),
    }


def _unavailable():
    return _fact(status="unavailable")


def _base(*, alert: bool = False) -> InvestigationInput:
    face = otel_demo_face(FakeClock(start=RECEIVED))
    anchor = (
        AlertAnchor(
            received_at=RECEIVED, starts_at=STARTS, original="2026-10-10T11:41:54Z"
        )
        if alert
        else None
    )
    return face.input_for(
        run_id="run-1",
        question=QUESTION,
        target_id="checkout-prod",
        deadline=DEADLINE,
        model_requests=4,
        alert=anchor,
    )


def _with(base: InvestigationInput, **facts) -> InvestigationInput:
    return replace(base, scope_facts={**base.scope_facts, **facts})


def _v5(fact=None, *, alert_ctx: bool = False) -> InvestigationInput:
    fact = _fact(_entry("a"), _entry("b", freshness="stale")) if fact is None else fact
    if alert_ctx:
        base = _with(_base(alert=True), alert_context=alert_context({"a": "b"}, {}))
    else:
        base = _base()
    return _with(base, knowledge=fact)


# -- input v5 (R1, interface item 3) ---------------------------------------------


def test_v5_constants_are_published():
    assert context_module.INPUT_VERSION_KNOWLEDGE == V5
    assert context_module.KNOWLEDGE == "knowledge"
    assert V5 in context_module.KNOWN_INPUT_VERSIONS


@pytest.mark.parametrize(
    "fact",
    [
        _fact(_entry("a"), _entry("b", freshness="stale")),
        _fact(),  # retrieval enabled, nothing found: still recorded
        _unavailable(),
        _fact(_entry("a"), omitted_for_size=2),
    ],
    ids=["entries", "empty", "unavailable", "omitted"],
)
def test_v5_round_trips(fact):
    fresh = _v5(fact)
    assert fresh.version == V5
    raw = fresh.as_json()
    assert raw["version"] == V5
    assert raw["scope_facts"]["knowledge"] == fact
    assert InvestigationInput.from_json(raw) == fresh


def test_knowledge_takes_precedence_over_alert_context():
    fresh = _v5(alert_ctx=True)
    assert fresh.version == V5
    raw = fresh.as_json()
    assert raw["version"] == V5
    assert InvestigationInput.from_json(raw) == fresh


def test_older_versions_still_read():
    base = _base()
    v2 = _with(base, affected_service={"namespace": "otel-demo", "workload": "co"})
    v3 = _base(alert=True)
    v4 = _with(v3, alert_context=alert_context({"a": "b"}, {}))
    for item, version in ((base, V1), (v2, V2), (v3, V3), (v4, V4)):
        raw = item.as_json()
        assert raw["version"] == version
        assert "knowledge" not in raw["scope_facts"]
        assert InvestigationInput.from_json(raw) == item


def test_version_and_knowledge_fact_must_agree():
    raw = _v5().as_json()
    for older in (V1, V2, V3, V4):
        with pytest.raises(ContextError, match="INPUT_INVALID"):
            InvestigationInput.from_json({**raw, "version": older})
    # A v5 row without the fact.
    without = {
        **raw,
        "scope_facts": {
            k: v for k, v in raw["scope_facts"].items() if k != "knowledge"
        },
    }
    with pytest.raises(ContextError, match="INPUT_INVALID"):
        InvestigationInput.from_json(without)
    # A v4 row (alert context) that also carries knowledge.
    alert_raw = _v5(alert_ctx=True).as_json()
    with pytest.raises(ContextError, match="INPUT_INVALID"):
        InvestigationInput.from_json({**alert_raw, "version": V4})


def test_a_worker_without_v5_blocks_the_row(monkeypatch):
    raw = _v5().as_json()
    monkeypatch.setattr(
        context_module,
        "KNOWN_INPUT_VERSIONS",
        tuple(v for v in context_module.KNOWN_INPUT_VERSIONS if v != V5),
    )
    with pytest.raises(ContextError, match="INCOMPATIBLE_STATE"):
        InvestigationInput.from_json(raw)


def _drop(mapping, key):
    return {k: v for k, v in mapping.items() if k != key}


_GOOD = _fact(_entry("a"))
_GOOD_ENTRY = _entry("a")


@pytest.mark.parametrize(
    "fact",
    [
        "text",
        [],
        None,
        _drop(_GOOD, "entries"),
        _drop(_GOOD, "status"),
        _drop(_GOOD, "policy_version"),
        _drop(_GOOD, "retrieved_at"),
        _drop(_GOOD, "max_age_seconds"),
        _drop(_GOOD, "omitted_for_size"),
        {**_GOOD, "entries": {"a": _GOOD_ENTRY}},
        {**_GOOD, "entries": ["not an entry"]},
        {**_GOOD, "status": "maybe"},
        {**_GOOD, "policy_version": "knowledge-hint-v0"},
        {**_GOOD, "max_age_seconds": "30d"},
        {**_GOOD, "omitted_for_size": -1},
        {**_GOOD, "entries": [_drop(_GOOD_ENTRY, "content_sha256")]},
        {**_GOOD, "entries": [_drop(_GOOD_ENTRY, "freshness")]},
        {**_GOOD, "entries": [_drop(_GOOD_ENTRY, "entry_id")]},
        {**_GOOD, "entries": [{**_GOOD_ENTRY, "freshness": "old"}]},
        {**_GOOD, "entries": [{**_GOOD_ENTRY, "truncated": "no"}]},
        {**_GOOD, "entries": [{**_GOOD_ENTRY, "revision": "1"}]},
        {**_GOOD, "entries": [{**_GOOD_ENTRY, "content": {"symptoms": []}}]},
    ],
)
def test_a_malformed_knowledge_fact_is_invalid(fact):
    raw = _v5().as_json()
    raw = {
        **raw,
        "version": V5,
        "scope_facts": {**raw["scope_facts"], "knowledge": fact},
    }
    with pytest.raises(ContextError, match="INPUT_INVALID"):
        InvestigationInput.from_json(raw)


# -- the model-visible prefix (U3, R6, interface item 4) ----------------------------


def _knowledge_messages(messages):
    boundary = context_module.KNOWLEDGE_BOUNDARY
    return [
        (index, m) for index, m in enumerate(messages) if boundary in str(m["content"])
    ]


def test_one_knowledge_message_between_question_and_evidence_context():
    fact = _fact(_entry("a"), _entry("b", freshness="stale"))
    fresh = _v5(fact)
    messages, _ = initial_messages(fresh, evidence_context=fresh.evidence_context)
    assert [m["role"] for m in messages] == ["system", "user", "user", "user"]
    found = _knowledge_messages(messages)
    assert len(found) == 1
    index, message = found[0]
    assert index == 2
    assert message["role"] == "user"
    text = message["content"]
    assert text.startswith(context_module.KNOWLEDGE_BOUNDARY)
    assert canonical(fact["entries"]) in text
    assert messages[1]["content"] == QUESTION
    assert messages[3]["content"] == canonical(fresh.evidence_context)


def test_knowledge_follows_the_alert_context():
    fresh = _v5(alert_ctx=True)
    messages, _ = initial_messages(fresh, evidence_context=fresh.evidence_context)
    assert [m["role"] for m in messages] == ["system", "user", "user", "user", "user"]
    assert messages[2]["content"].startswith(ALERT_CONTEXT_BOUNDARY)
    found = _knowledge_messages(messages)
    assert [index for index, _ in found] == [3]
    assert messages[4]["content"] == canonical(fresh.evidence_context)


@pytest.mark.parametrize(
    "fact", [_fact(), _unavailable()], ids=["empty", "unavailable"]
)
def test_no_knowledge_message_without_entries(fact):
    fresh = _v5(fact)
    plain = _base()
    messages, _ = initial_messages(fresh, evidence_context=fresh.evidence_context)
    expected, _ = initial_messages(plain, evidence_context=plain.evidence_context)
    assert messages == expected
    assert _knowledge_messages(messages) == []


def test_no_knowledge_message_without_the_fact():
    plain = _base()
    messages, _ = initial_messages(plain, evidence_context=plain.evidence_context)
    assert _knowledge_messages(messages) == []


def test_the_boundary_states_the_tag_convention():
    text = context_module.KNOWLEDGE_BOUNDARY
    assert isinstance(text, str) and text
    assert "[knowledge:" in text
    for kind in ("fact", "rejected_hypothesis", "hypothesis"):
        assert kind in text, kind


def test_the_boundary_is_part_of_prompt_revision(monkeypatch):
    current = prompt_revision_versions()["prompt_revision"]
    monkeypatch.setattr(context_module, "KNOWLEDGE_BOUNDARY", "changed wording")
    monkeypatch.setattr(
        loop_module, "KNOWLEDGE_BOUNDARY", "changed wording", raising=False
    )
    assert prompt_revision_versions()["prompt_revision"] != current


# -- knowledge_checks (U8, interface item 5) -----------------------------------------


def _checks(fact, report):
    from opspilot.knowledge.hints import knowledge_checks

    result = knowledge_checks(fact, report)
    assert isinstance(result, list)
    return result


def _tag(entry, revision=None):
    return f"[knowledge:{entry['entry_id']}@r{entry['revision'] if revision is None else revision}]"


def _claim(kind, text, *, evidence=True):
    return {
        "kind": kind,
        "text": text,
        "evidence_ids": ["ev-run-1"] if evidence else [],
        "target_refs": ["checkout-prod"] if evidence else [],
        "time_scope_ref": "policy-window-1" if evidence else None,
    }


def _report(*claims):
    return {
        "schema_version": "m0-report-v2",
        "assessment_status": "completed",
        "conclusion": "partial",
        "summary": "checkout assessment",
        "claims": list(claims),
        "gaps": [],
        "next_steps": [],
    }


def _by_entry(result):
    by = {item["entry_id"]: item for item in result}
    assert len(by) == len(result)
    for item in result:
        assert set(item) >= {"entry_id", "revision", "verdict", "claim_kinds"}
    return by


def test_verdicts_follow_the_tag_convention():
    names = "abcdefghij"
    e = {name: _entry(name) for name in names}
    unknown = _entry("not-provided")
    report = _report(
        _claim("fact", f"payment errors confirmed {_tag(e['a'])}"),
        _claim("rejected_hypothesis", f"no payment errors now {_tag(e['b'])}"),
        _claim("hypothesis", f"maybe the deploy again {_tag(e['c'])}", evidence=False),
        _claim("fact", f"errors seen {_tag(e['d'])}"),
        _claim("rejected_hypothesis", f"but not on payment {_tag(e['d'])}"),
        # e: no tag anywhere
        _claim("recommendation", f"check the cache {_tag(e['f'])}", evidence=False),
        # g: tagged with a revision that was not provided
        _claim("fact", f"old revision cited {_tag(e['g'], revision=2)}"),
        _claim("hypothesis", f"first a guess {_tag(e['h'])}", evidence=False),
        _claim("fact", f"then confirmed {_tag(e['h'])}"),
        # i: shares one claim with a
        _claim("fact", f"both hold {_tag(e['a'])} {_tag(e['i'])}"),
        _claim("counter_evidence", f"evidence against {_tag(e['j'])}"),
        # a tag for an entry that was not provided is ignored
        _claim("fact", f"unrelated {_tag(unknown)}"),
    )
    fact = _fact(*(e[name] for name in names))
    by = _by_entry(_checks(fact, report))
    assert set(by) == {e[name]["entry_id"] for name in names}
    assert unknown["entry_id"] not in by
    expected = {
        "a": ("consistent", {"fact"}),
        "b": ("contradicted", {"rejected_hypothesis"}),
        "c": ("unverified", {"hypothesis"}),
        "d": ("conflicting", {"fact", "rejected_hypothesis"}),
        "e": ("untagged", set()),
        "f": ("unverified", {"recommendation"}),
        "g": ("untagged", set()),
        "h": ("consistent", {"hypothesis", "fact"}),
        "i": ("consistent", {"fact"}),
        "j": ("unverified", {"counter_evidence"}),
    }
    for name, (verdict, kinds) in expected.items():
        item = by[e[name]["entry_id"]]
        assert item["revision"] == 1, name
        assert item["verdict"] == verdict, name
        assert set(item["claim_kinds"]) == kinds, name


def test_a_missing_report_leaves_every_entry_untagged():
    entries = [_entry("a"), _entry("b", freshness="stale")]
    by = _by_entry(_checks(_fact(*entries), None))
    assert set(by) == {item["entry_id"] for item in entries}
    for item in by.values():
        assert item["verdict"] == "untagged"
        assert list(item["claim_kinds"]) == []


@pytest.mark.parametrize(
    "fact", [_fact(), _unavailable()], ids=["empty", "unavailable"]
)
def test_no_entries_no_checks(fact):
    report = _report(_claim("fact", f"x {_tag(_entry('a'))}"))
    assert _checks(fact, report) == []


# -- Run creation in the workbench (U5, R5, R7, interface item 6) -----------------------


class FakeKnowledge:
    """A ``knowledge_for_target`` double: returns its scripted facts in
    order, or raises a scripted exception."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls: list[dict] = []

    def knowledge_for_target(
        self, resource_uid, query_text, *, limit=3, max_age_seconds=None
    ):
        self.calls.append(
            {
                "resource_uid": resource_uid,
                "query_text": query_text,
                "limit": limit,
                "max_age_seconds": max_age_seconds,
            }
        )
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception):
            raise result
        return result


def _bench(knowledge):
    app, workbench, clock = build_workbench(tool_face=fixture_face())
    workbench.knowledge = knowledge
    return app, workbench, clock


def _run_input(workbench, run_id) -> InvestigationInput:
    recorded = workbench.incidents.runs[run_id]["input"]
    assert recorded is not None
    return InvestigationInput.from_json(recorded)


def _control(app, incident, fields):
    return post_form(
        app,
        f"/incidents/{incident}/control",
        fields,
        headers={**basic(), **same_origin()},
    )


def test_the_workbench_has_knowledge_settings():
    _, workbench, _ = build_workbench(tool_face=fixture_face())
    assert workbench.knowledge_max_age_seconds == DEFAULT_MAX_AGE
    assert workbench.knowledge is None


def test_a_web_submit_records_the_retrieved_knowledge():
    fact = _fact(_entry("a"), _entry("b", freshness="stale", age=DEFAULT_MAX_AGE + 1))
    knowledge = FakeKnowledge(fact)
    app, workbench, _ = _bench(knowledge)
    response = submit_incident(app, key=LABEL_WEB, question=QUESTION)
    assert response.status == 201, response.text
    recorded = _run_input(workbench, UUID(response.json()["run_id"]))
    assert recorded.version == V5
    assert recorded.scope_facts["knowledge"] == fact
    assert len(knowledge.calls) >= 1
    first = knowledge.calls[0]
    assert first["resource_uid"] == "checkout-prod"
    assert first["query_text"] == QUESTION
    assert first["max_age_seconds"] == DEFAULT_MAX_AGE
    assert first["limit"] == 3


def test_the_threshold_is_passed_through():
    knowledge = FakeKnowledge(_fact())
    app, workbench, _ = _bench(knowledge)
    workbench.knowledge_max_age_seconds = 60
    response = submit_incident(app, key=LABEL_THRESHOLD)
    assert response.status == 201, response.text
    assert knowledge.calls[0]["max_age_seconds"] == 60
    recorded = _run_input(workbench, UUID(response.json()["run_id"]))
    assert recorded.scope_facts["knowledge"] == _fact()


def test_an_unavailable_store_does_not_block_intake():
    knowledge = FakeKnowledge(PersistenceError("UNAVAILABLE"))
    app, workbench, _ = _bench(knowledge)
    response = submit_incident(app, key=LABEL_UNAVAILABLE)
    assert response.status == 201, response.text
    incident = UUID(response.json()["incident_id"])
    assert incident in workbench.incidents.incidents
    recorded = _run_input(workbench, UUID(response.json()["run_id"]))
    assert recorded.version == V5
    fact = recorded.scope_facts["knowledge"]
    assert fact["status"] == "unavailable"
    assert fact["retrieved_at"] is None
    assert fact["entries"] == []


def test_disabled_knowledge_records_nothing():
    app, workbench, _ = _bench(None)
    response = submit_incident(app, key=LABEL_OFF)
    assert response.status == 201, response.text
    recorded = _run_input(workbench, UUID(response.json()["run_id"]))
    assert "knowledge" not in recorded.scope_facts
    assert recorded.version != V5


def test_a_successor_run_retrieves_again_for_the_original_question():
    first = _fact(_entry("a"))
    second = _fact(_entry("b", revision=2))
    knowledge = FakeKnowledge(first, second)
    app, workbench, _ = _bench(knowledge)
    response = submit_incident(app, key=LABEL_SUCCESSOR, question=QUESTION)
    assert response.status == 201, response.text
    incident = response.json()["incident_id"]
    old = UUID(response.json()["run_id"])
    assert _run_input(workbench, old).scope_facts["knowledge"] == first
    calls_before = len(knowledge.calls)
    assert (
        _control(
            app,
            incident,
            {"action": "cancel", "expected_generation": "0", "idempotency_key": "c"},
        ).status
        == 200
    )
    assert (
        _control(
            app,
            incident,
            {"action": "new_run", "expected_generation": "1", "idempotency_key": "n"},
        ).status
        == 200
    )
    successor = workbench.list_incidents()[0].current_run_id
    assert successor != old
    recorded = _run_input(workbench, successor)
    assert recorded.version == V5
    # Re-retrieved (U5-A), replacing the previous Run's knowledge.
    assert recorded.scope_facts["knowledge"] == second
    assert len(knowledge.calls) > calls_before
    assert knowledge.calls[-1]["query_text"] == QUESTION
    assert knowledge.calls[-1]["resource_uid"] == "checkout-prod"


# -- the incident page (U8, interface item 7) -------------------------------------


def test_the_incident_page_shows_the_knowledge_hints():
    fresh = _entry("a")
    stale = _entry("b", freshness="stale", age=DEFAULT_MAX_AGE + 5)
    knowledge = FakeKnowledge(_fact(fresh, stale))
    app, workbench, _ = _bench(knowledge)
    response = submit_incident(app, key=LABEL_PAGE)
    assert response.status == 201, response.text
    incident = response.json()["incident_id"]
    page = call(app, "GET", f"/incidents/{incident}", headers=HTML)
    assert page.status == 200
    text = page.text
    assert 'id="knowledge-hints"' in text
    for entry in (fresh, stale):
        assert f"/knowledge/{entry['entry_id']}" in text
        assert f"/incidents/{entry['source_incident_id']}" in text
    assert "stale" in text and "fresh" in text


def test_the_incident_page_shows_an_unavailable_retrieval():
    knowledge = FakeKnowledge(PersistenceError("UNAVAILABLE"))
    app, workbench, _ = _bench(knowledge)
    response = submit_incident(app, key=LABEL_PAGE_UNAVAILABLE)
    assert response.status == 201, response.text
    incident = response.json()["incident_id"]
    page = call(app, "GET", f"/incidents/{incident}", headers=HTML)
    assert page.status == 200
    text = page.text
    assert 'id="knowledge-hints"' in text
    section = text[text.index('id="knowledge-hints"') :]
    assert "unavailable" in section
