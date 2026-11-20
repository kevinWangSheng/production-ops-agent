"""``fetch_skill`` and the v7 runbook catalog on the product path (#566 C2b).

Independent contract and acceptance tests (the author did not see the
implementation), from the C2 interface sections 5-7 and 「C2b 签名级细节」,
decisions r11 C2-U1=a / C2-U2=a and pre-review C2-5..C2-17. Includes the
C2b half of the re-proof table:

* F13 step 2 -- pending and rejected submissions are never listed in the
  catalog and ``fetch_skill`` reports them ``KNOWLEDGE_NOT_FOUND``;
* F13 step 3 -- a fetched runbook carries its source, reviewer, approval
  instant and revision; after a revoke the same ``skill_id`` is
  ``KNOWLEDGE_NOT_AVAILABLE`` (``revoked``) with no body; after a supersede
  the name resolves to the new revision;
* F7 step 2 -- an approved runbook whose body says "query another target,
  widen to 30 days, call write tools" changes no executed read, target or
  scope (U8-a, the verdict of ``test_f7_step2_hostile_input_postgres.py``).

Runbooks are written through the signed-in workbench JSON API, Runs are
driven by the real runner with a scripted provider, and the worker runs as
a throwaway ``opspilot_worker`` login (``tests/runbook_fetch_support.py``).
Database instants are compared with the rows PostgreSQL wrote, never with
wall-clock values. Run with ``M1_DURABLE_POSTGRES=1``.
"""

from __future__ import annotations

import json
import os
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest

from opspilot.investigation.context import (
    InvestigationInput,
    delivered_view,
    initial_messages,
)
from opspilot.persistence.grants import WORKER_ROLE
from opspilot.tools.otel_demo import TARGET_ID
from opspilot.tools.registry import canonical
from tests.acceptance.f7_hostile_support import (
    HOSTILE_INSTRUCTION,
    query_window_of,
    text_of,
)
from tests.acceptance.f7_pg_support import (
    ScriptedProvider,
    approve_knowledge,
    assistant,
    authority_snapshot,
    evidence_rows,
    fn,
)
from tests.m1_web_support import ORIGIN, UI_PASSWORD, UI_USER
from tests.runbook_fetch_support import (
    CATALOG,
    DAY,
    OTHER_TARGET,
    V7,
    FetchHarness,
    bare_login,
    build_harness,
    fetch,
    report_round,
    sha256_text,
    traces,
    views_by_call,
)
from tests.runbook_import_support import (
    DEFAULT_REASON,
    SKILLS,
    SOURCE_LABEL,
    HttpFront,
    asgi_bridge,
    question_dir,
    run_script,
)
from tests.runbook_support import REVIEWER, scratch_database
from tests.runtime_roles_support import login
from tests.test_m1_otel_demo_contract import GOOD_EXPR, JAEGER_URL, PROMETHEUS_URL

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)

QUESTION = "Why is checkout erroring?"
U = uuid4().hex[:6]
BIG_BYTES = 32 * 1024


def _code(response) -> str:
    return response.json()["code"]


def _instant(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    assert isinstance(value, str), value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _same_instant(left: object, right: object) -> bool:
    return abs(_instant(left) - _instant(right)) < timedelta(seconds=1)


@pytest.fixture(scope="module")
def ca_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("c2b-ca") / "synthetic-ca-placeholder.pem"
    path.write_text("synthetic ca placeholder\n")
    return path


@pytest.fixture(scope="module")
def harness_factory(ca_file):
    """``make(prefix)`` -> a harness on its own scratch database."""
    with ExitStack() as stack:
        patch = stack.enter_context(pytest.MonkeyPatch.context())

        def make(prefix: str) -> FetchHarness:
            dsn = stack.enter_context(scratch_database(prefix))
            worker_dsn = stack.enter_context(login(dsn, WORKER_ROLE))
            harness = build_harness(dsn, worker_dsn, patch, ca_file)
            stack.callback(harness.close)
            return harness

        yield make


@pytest.fixture(scope="module")
def h(harness_factory) -> FetchHarness:
    """Scratch database for the tests that do not need the world below."""
    return harness_factory("opspilot_c2b_misc")


# -- the world: one target, every kind of runbook, one Run fetching them all ------


NAMES = {
    "active": f"rb-active-{U}",
    "norm": f"rb-norm-{U}",
    "super": f"rb-super-{U}",
    "live": f"rb-live-{U}",
    "big": f"rb-big-{U}",
    "pending": f"rb-pending-{U}",
    "rejected": f"rb-rejected-{U}",
    "revoked": f"rb-revoked-{U}",
    "only_b": f"rb-onlyb-{U}",
}
CONTENT = {
    "active": f"# Checkout errors\n\n1. Read the error rate. active-body-{U}\n",
    "same_b": f"# Payments version\n\nother-target-body-{U}\n",
    "only_b": f"# Payments only\n\nonly-b-body-{U}\n",
    "super_1": f"# Old steps\n\nsuperseded-body-{U}\n",
    "super_2": f"# New steps\n\ncurrent-body-{U}\n",
    "revoked": f"# Withdrawn\n\nrevoked-body-{U}\n",
    "pending": f"# Pending\n\npending-body-{U}\n",
    "rejected": f"# Rejected\n\nrejected-body-{U}\n",
}
BIG_CONTENT = "# Big\n" + "a" * (BIG_BYTES - len("# Big\n"))
#: Markers that must never reach any view of the world Run.
NEVER_SHOWN = ("same_b", "only_b", "super_1", "revoked", "pending", "rejected")


def _round_one(world: dict[str, Any]) -> dict[str, Any]:
    a, ids = world["active"], world
    calls = [
        fetch("by-name", NAMES["active"]),
        fetch("by-entry", a["entry_id"]),
        fetch("by-entry-upper", a["entry_id"].upper()),
        fetch("normalized", f"RB_Norm {U}"),
        fetch("pending", NAMES["pending"]),
        fetch("pending-sid", ids["pending_sid"]),
        fetch("rejected", NAMES["rejected"]),
        fetch("rejected-sid", ids["rejected_sid"]),
        fetch("revoked-name", NAMES["revoked"]),
        fetch("revoked-entry", ids["revoked"]["entry_id"]),
        fetch("super-name", NAMES["super"]),
        fetch("super-entry", ids["super"]["entry_id"]),
        fetch("only-b-name", NAMES["only_b"]),
        fetch("only-b-entry", ids["only_b"]["entry_id"]),
        fetch("same-b-entry", ids["same_b"]["entry_id"]),
        fetch("pm", ids["pm"]["entry_id"]),
        fetch("pm-b", ids["pm_b"]["entry_id"]),
        fetch("missing-name", f"no-such-runbook-{U}"),
        fetch("missing-uuid", str(uuid4())),
        fetch("big", NAMES["big"]),
        fetch("live-1", NAMES["live"]),
        fetch("invalid-nul", NAMES["active"] + "\u0000"),
        fetch("invalid-newline", "rb-a\nrb-b"),
        fetch("invalid-long", "x" * 201),
        fetch("invalid-empty", ""),
        fetch("invalid-extra", NAMES["active"], target_id=OTHER_TARGET),
        traces(),
    ]
    return assistant(tool_calls=calls)


@pytest.fixture(scope="module")
def world(harness_factory) -> dict[str, Any]:
    h = harness_factory("opspilot_c2b_world")
    w: dict[str, Any] = {"h": h}
    w["active"] = h.rb_publish(
        name=NAMES["active"],
        content=CONTENT["active"],
        description="Checkout 5xx after a deploy.",
        source_label="team wiki",
        source_ref=f"wiki/checkout-{U}",
    )
    w["same_b"] = h.rb_publish(
        resource_uid=OTHER_TARGET, name=NAMES["active"], content=CONTENT["same_b"]
    )
    w["only_b"] = h.rb_publish(
        resource_uid=OTHER_TARGET, name=NAMES["only_b"], content=CONTENT["only_b"]
    )
    w["norm"] = h.rb_publish(name=NAMES["norm"])
    w["super"] = h.rb_publish(name=NAMES["super"], content=CONTENT["super_1"])
    successor = h.rb_submitted(
        name=NAMES["super"],
        content=CONTENT["super_2"],
        supersedes_entry_id=w["super"]["entry_id"],
    )
    approved = h.rb_review(successor)
    assert approved.status == 200, approved.text
    assert approved.json()["revision"] == 2
    w["live"] = h.rb_publish(name=NAMES["live"])
    w["big"] = h.rb_publish(name=NAMES["big"], content=BIG_CONTENT)
    w["pending_sid"] = h.rb_submitted(name=NAMES["pending"], content=CONTENT["pending"])
    w["rejected_sid"] = h.rb_submitted(
        name=NAMES["rejected"], content=CONTENT["rejected"]
    )
    rejected = h.rb_review(w["rejected_sid"], "reject")
    assert rejected.status == 200, rejected.text
    w["revoked"] = h.rb_publish(name=NAMES["revoked"], content=CONTENT["revoked"])
    h.rb_revoke(w["revoked"]["entry_id"], 1)

    earlier, _ = h.submit("Checkout payments failing?")
    w["pm"] = approve_knowledge(
        h,
        earlier,
        name="checkout payment failures",
        content={
            "symptoms": ["payment 5xx"],
            "checks": ["error rate"],
            "evidence_refs": ["ev-old-1", "ev-old-2"],
        },
    )
    elsewhere, _ = h.submit("Payments failing?", target_id=OTHER_TARGET)
    w["pm_b"] = approve_knowledge(
        h,
        elsewhere,
        name="payments failures",
        content={"symptoms": ["timeouts"], "evidence_refs": ["ev-old-9"]},
    )

    incident, run = h.submit(QUESTION)
    w["incident"], w["run"] = incident, run

    def revoke_live_then_fetch(_messages):
        h.rb_revoke(w["live"]["entry_id"], 1)
        return assistant(
            tool_calls=[
                fetch("live-2", NAMES["live"]),
                fetch("live-2-entry", w["live"]["entry_id"]),
            ]
        )

    provider = ScriptedProvider(
        [_round_one(w), revoke_live_then_fetch, report_round(recommend_call="by-name")]
    )
    w["outcome"] = h.run(incident, provider)
    w["provider"] = provider
    assert len(provider.requests) == 3, w["outcome"]
    w["views"] = views_by_call(provider.requests[-1]["messages"])
    return w


def _view(world, call_id: str) -> dict[str, Any]:
    assert call_id in world["views"], sorted(world["views"])
    return world["views"][call_id]


def _row(world, call_id: str) -> dict[str, Any]:
    view = _view(world, call_id)
    assert view["status"] == "ok", view
    content = view["content"]
    assert isinstance(content, list) and len(content) == 1, view
    return content[0]


def _text(view: dict[str, Any]) -> str:
    return json.dumps(view, ensure_ascii=False, sort_keys=True)


def _refused(world, call_id: str, reason: str, code: str) -> dict[str, Any]:
    view = _view(world, call_id)
    assert (view["status"], view.get("reason")) == ("error", reason), view
    assert code in _text(view), view
    assert view.get("content") is None, view
    assert view.get("evidence_id") is None, view
    return view


def _notes(view: dict[str, Any]) -> str:
    notes = view.get("notes") or []
    assert isinstance(notes, list), view
    return "\n".join(str(n) for n in notes)


# -- the catalog in the Run input ---------------------------------------------


def test_the_catalog_lists_exactly_the_active_runbooks_of_this_target(world):
    h = world["h"]
    recorded = h.recorded(world["incident"])
    assert recorded["version"] == V7
    InvestigationInput.from_json(recorded)  # the snapshot validates
    fact = recorded["scope_facts"][CATALOG]
    assert fact["status"] == "ok"
    assert fact["max_age_seconds"] == DAY
    assert fact["omitted_for_size"] == 0
    names = [e["skill_id"] for e in fact["entries"]]
    assert names == sorted(names)
    # ``live`` was revoked only during the Run: the snapshot still lists it.
    assert set(names) == {NAMES[k] for k in ("active", "norm", "super", "live", "big")}
    by_name = {e["skill_id"]: e for e in fact["entries"]}
    assert by_name[NAMES["super"]]["revision"] == 2
    assert by_name[NAMES["super"]]["entry_id"] == world["super"]["entry_id"]
    active = by_name[NAMES["active"]]
    assert active["entry_id"] == world["active"]["entry_id"]
    assert active["revision"] == 1
    assert active["description"] == "Checkout 5xx after a deploy."
    db = h.revision_row(world["active"]["entry_id"], 1)
    assert _same_instant(active["approved_at"], db["approved_at"])
    for entry in fact["entries"]:
        assert type(entry["age_seconds"]) is int and entry["age_seconds"] >= 0
        assert entry["freshness"] == (
            "stale" if entry["age_seconds"] > fact["max_age_seconds"] else "fresh"
        )
    # F13 step 2: pending, rejected, revoked and the other target's runbooks
    # are not in it; no body is.
    text = canonical(fact)
    for key in ("pending", "rejected", "revoked", "only_b"):
        assert NAMES[key] not in text, key
    for key in CONTENT:
        assert CONTENT[key] not in text, key


def _catalog_index(messages: list[dict[str, Any]]) -> int:
    from opspilot.investigation.context import RUNBOOK_CATALOG_BOUNDARY

    found = [
        i
        for i, m in enumerate(messages)
        if isinstance(m.get("content"), str)
        and m["content"].startswith(RUNBOOK_CATALOG_BOUNDARY)
    ]
    assert len(found) == 1, found
    return found[0]


def test_the_model_sees_the_catalog_once_between_knowledge_and_evidence_context(world):
    from opspilot.investigation.context import (
        KNOWLEDGE_BOUNDARY,
        RUNBOOK_CATALOG_BOUNDARY,
    )

    h = world["h"]
    fact = h.catalog(world["incident"])
    first = world["provider"].requests[0]["messages"]
    index = _catalog_index(first)
    assert first[index]["role"] == "user"
    listed = json.loads(first[index]["content"][len(RUNBOOK_CATALOG_BOUNDARY) :])
    assert listed == [
        {k: e[k] for k in ("skill_id", "description", "revision", "freshness")}
        for e in fact["entries"]
    ]
    knowledge = [
        i
        for i, m in enumerate(first)
        if str(m.get("content", "")).startswith(KNOWLEDGE_BOUNDARY)
    ]
    if knowledge:
        assert index == knowledge[0] + 1
    # The evidence context is the next prefix message.
    context = json.loads(first[index + 1]["content"])
    assert "time_policies" in context
    assert context["data_sources"]["available"]["knowledge"] == ["fetch_skill"]
    # Every later round repeats the same prefix, byte for byte (C3 §5).
    for request in world["provider"].requests[1:]:
        assert canonical(request["messages"][: index + 2]) == canonical(
            first[: index + 2]
        )
    # The rebuilt prefix agrees with what was sent.
    parsed = InvestigationInput.from_json(h.recorded(world["incident"]))
    rebuilt, _system = initial_messages(parsed, evidence_context=context)
    assert canonical(rebuilt) == canonical(first[: index + 2])


# -- fetch_skill: what an active runbook returns (F13 step 3) -------------------------


def test_a_runbook_fetched_by_name_carries_its_full_body_and_provenance(world):
    h = world["h"]
    a = world["active"]
    view = _view(world, "by-name")
    row = _row(world, "by-name")
    db = h.revision_row(a["entry_id"], 1)
    assert row["kind"] == "runbook_import"
    assert row["skill_id"] == NAMES["active"]
    assert row["name"] == NAMES["active"]
    assert row["entry_id"] == a["entry_id"]
    assert row["revision"] == 1
    assert row["description"] == "Checkout 5xx after a deploy."
    assert row["content"] == CONTENT["active"]
    assert row["content_sha256"] == sha256_text(CONTENT["active"])
    assert row["content_sha256"] == db["content_sha256"]
    assert row["source"] == {
        "source_label": "team wiki",
        "source_ref": f"wiki/checkout-{U}",
    }
    assert row["review_state"] == "active"
    assert row["approved_by"] == REVIEWER
    assert _same_instant(row["approved_at"], db["approved_at"])
    freshness = row["freshness"]
    assert set(freshness) == {"age_seconds", "state", "max_age_seconds"}
    assert (
        freshness["max_age_seconds"] == h.catalog(world["incident"])["max_age_seconds"]
    )
    assert type(freshness["age_seconds"]) is int and freshness["age_seconds"] >= 0
    assert freshness["state"] == (
        "stale" if freshness["age_seconds"] > freshness["max_age_seconds"] else "fresh"
    )
    # C2-8: reviewed steps, never a fact about this Run.
    assert view["citable_as_fact"] is False
    assert view["source_start_at"] is None and view["source_end_at"] is None
    assert _same_instant(view["data_as_of"], db["approved_at"])
    assert view.get("evidence_id")
    assert _notes(view), view
    assert isinstance(view.get("cannot_prove"), str) and view["cannot_prove"]
    # The other target's same-named runbook never shows.
    assert CONTENT["same_b"] not in _text(view)


def test_entry_ids_in_any_case_and_normalized_names_resolve_the_same_runbook(world):
    by_name = _row(world, "by-name")
    for call_id in ("by-entry", "by-entry-upper"):
        row = _row(world, call_id)
        assert (row["entry_id"], row["revision"], row["content"]) == (
            by_name["entry_id"],
            by_name["revision"],
            by_name["content"],
        ), call_id
    normalized = _row(world, "normalized")
    assert normalized["entry_id"] == world["norm"]["entry_id"]
    assert normalized["skill_id"] == NAMES["norm"]


def test_a_32_kib_body_is_returned_whole(world):
    row = _row(world, "big")
    assert len(row["content"].encode("utf-8")) == BIG_BYTES
    assert row["content"] == BIG_CONTENT
    assert row["content_sha256"] == sha256_text(BIG_CONTENT)


def test_a_superseded_runbook_resolves_to_its_new_revision(world):
    for call_id in ("super-name", "super-entry"):
        row = _row(world, call_id)
        assert row["entry_id"] == world["super"]["entry_id"], call_id
        assert row["revision"] == 2, call_id
        assert row["content"] == CONTENT["super_2"], call_id
        assert CONTENT["super_1"] not in _text(_view(world, call_id))


def test_postmortem_knowledge_by_entry_id_comes_whole_with_its_refs_omitted(world):
    pm = world["pm"]
    view = _view(world, "pm")
    row = _row(world, "pm")
    assert row["kind"] == "postmortem"
    assert row["entry_id"] == str(pm["entry_id"])
    assert row["revision"] == pm["revision"]
    assert row["review_state"] == "active"
    content = row["content"]
    parsed = json.loads(content) if isinstance(content, str) else content
    assert parsed["symptoms"] == ["payment 5xx"]
    assert parsed["checks"] == ["error rate"]
    assert parsed["evidence_refs"] == {"omitted": True, "count": 2}
    assert "ev-old-1" not in _text(view) and "ev-old-2" not in _text(view)
    source = row["source"]
    assert isinstance(source, dict) and source, row
    assert view["citable_as_fact"] is False
    # Another target's postmortem knowledge is not this Run's.
    _refused(world, "pm-b", "INVALID_PARAMS", "KNOWLEDGE_NOT_FOUND")


# -- fetch_skill: what it refuses -----------------------------------------------------


@pytest.mark.parametrize(
    "call_id", ["pending", "pending-sid", "rejected", "rejected-sid"]
)
def test_pending_and_rejected_submissions_are_not_found(world, call_id):
    # F13 step 2 on the new source: nothing not yet approved can be fetched.
    view = _refused(world, call_id, "INVALID_PARAMS", "KNOWLEDGE_NOT_FOUND")
    assert CONTENT["pending"] not in _text(view)
    assert CONTENT["rejected"] not in _text(view)


@pytest.mark.parametrize("call_id", ["revoked-name", "revoked-entry"])
def test_a_revoked_runbook_is_not_available_and_shows_no_body(world, call_id):
    view = _refused(world, call_id, "SOURCE_ERROR", "KNOWLEDGE_NOT_AVAILABLE")
    assert "revoked" in _notes(view)
    assert CONTENT["revoked"] not in _text(view)


def test_a_revocation_during_the_run_is_seen_by_the_next_fetch(world):
    assert _row(world, "live-1")["entry_id"] == world["live"]["entry_id"]
    for call_id in ("live-2", "live-2-entry"):
        view = _refused(world, call_id, "SOURCE_ERROR", "KNOWLEDGE_NOT_AVAILABLE")
        assert "revoked" in _notes(view), call_id


@pytest.mark.parametrize("call_id", ["only-b-name", "only-b-entry", "same-b-entry"])
def test_another_targets_runbooks_are_not_found(world, call_id):
    view = _refused(world, call_id, "INVALID_PARAMS", "KNOWLEDGE_NOT_FOUND")
    assert CONTENT["only_b"] not in _text(view)
    assert CONTENT["same_b"] not in _text(view)


@pytest.mark.parametrize("call_id", ["missing-name", "missing-uuid"])
def test_not_found_lists_the_names_this_run_can_fetch(world, call_id):
    view = _refused(world, call_id, "INVALID_PARAMS", "KNOWLEDGE_NOT_FOUND")
    notes = _notes(view)
    # Fetchable now: ``live`` was still active in round one.
    for key in ("active", "norm", "super", "big", "live"):
        assert NAMES[key] in notes, key
    for key in ("pending", "rejected", "revoked", "only_b"):
        assert NAMES[key] not in notes, key


@pytest.mark.parametrize(
    "call_id", ["invalid-nul", "invalid-newline", "invalid-long", "invalid-empty"]
)
def test_malformed_skill_ids_are_refused_before_any_lookup(world, call_id):
    view = _view(world, call_id)
    assert view["status"] in ("error", "denied"), view
    if view["status"] == "error":
        assert view["reason"] == "INVALID_PARAMS", view
    assert view.get("content") is None
    if call_id in ("invalid-nul", "invalid-newline"):
        assert "INVALID_SKILL_ID" in _text(view), view


def test_a_target_named_in_the_call_is_refused_not_followed(world):
    view = _view(world, "invalid-extra")
    assert view["status"] in ("error", "denied"), view
    assert view.get("content") is None
    assert CONTENT["same_b"] not in _text(view)


def test_no_view_of_the_run_shows_a_body_it_must_not(world):
    sent = json.dumps(world["provider"].requests, ensure_ascii=False)
    for key in NEVER_SHOWN:
        assert CONTENT[key] not in sent, key


# -- the report may recommend a runbook, never assert it -----------------------------


def test_the_run_publishes_a_report_recommending_the_fetched_runbook(world):
    assert world["outcome"].status == "published", world["outcome"]


def test_report_validation_refuses_a_fact_citing_a_runbook_view(world):
    from opspilot.investigation.reports import ReportV2, unsupported_citations

    h = world["h"]
    evidence_id = _view(world, "by-name")["evidence_id"]
    [stored] = [
        r for r in evidence_rows(h, world["run"]) if r["evidence_id"] == evidence_id
    ]
    recorded = h.recorded(world["incident"])
    context = recorded["evidence_context"]
    policy_ids = [p["id"] for p in context["time_policies"]]
    view = delivered_view(
        json.loads(stored["view"]),
        evidence_context=context,
        authorized_targets=frozenset({TARGET_ID}),
    )
    assert view is not None and view.citable_as_fact is False

    def claims(kind: str) -> ReportV2:
        return ReportV2.model_validate(
            {
                "schema_version": "m0-report-v2",
                "assessment_status": "completed",
                "conclusion": "partial",
                "summary": "The reviewed runbook suggests checking the error rate.",
                "claims": [
                    {
                        "kind": kind,
                        "text": "The reviewed runbook says to read the error rate.",
                        "evidence_ids": [evidence_id],
                        "target_refs": [TARGET_ID],
                        "time_scope_ref": policy_ids[0],
                    }
                ],
                "gaps": ["No current evidence yet."],
            }
        )

    check = {
        "views": [view],
        "authorized_targets": frozenset({TARGET_ID}),
        "time_policy_ids": policy_ids,
    }
    assert unsupported_citations(claims("fact"), **check)
    for kind in ("hypothesis", "recommendation"):
        assert not unsupported_citations(claims(kind), **check), kind


# -- the catalog's bounds and failures ------------------------------------------------


def test_the_catalog_keeps_the_first_fifty_names_and_counts_the_rest(h):
    target = f"rb-cap-{U}"
    names = sorted(f"cap-{i:03d}-{U}" for i in range(52))
    for name in reversed(names):
        h.rb_publish(resource_uid=target, name=name, description="short")
    incident, _ = h.submit(QUESTION, target_id=target)
    fact = h.catalog(incident)
    assert fact["status"] == "ok"
    assert [e["skill_id"] for e in fact["entries"]] == names[:50]
    assert fact["omitted_for_size"] == 2


def test_the_catalog_stays_within_eight_kb_in_name_order(h):
    target = f"rb-size-{U}"
    names = sorted(f"size-{i:03d}-{U}" for i in range(30))
    for name in names:
        h.rb_publish(resource_uid=target, name=name, description="d" * 300)
    incident, _ = h.submit(QUESTION, target_id=target)
    fact = h.catalog(incident)
    kept = [e["skill_id"] for e in fact["entries"]]
    assert 1 <= len(kept) < len(names)
    assert kept == names[: len(kept)]
    assert fact["omitted_for_size"] == len(names) - len(kept)
    visible = [
        {k: e[k] for k in ("skill_id", "description", "revision", "freshness")}
        for e in fact["entries"]
    ]
    assert len(canonical(visible).encode("utf-8")) <= 8 * 1024
    InvestigationInput.from_json(h.recorded(incident))


def test_a_target_without_runbooks_gets_an_empty_catalog_and_no_message(h):
    target = f"rb-empty-{U}"
    incident, _ = h.submit(QUESTION, target_id=target)
    recorded = h.recorded(incident)
    fact = recorded["scope_facts"][CATALOG]
    assert recorded["version"] == V7
    assert fact["status"] == "ok" and fact["entries"] == []
    assert fact["omitted_for_size"] == 0
    from opspilot.investigation.context import RUNBOOK_CATALOG_BOUNDARY

    messages, _ = initial_messages(
        InvestigationInput.from_json(recorded), evidence_context=None
    )
    assert not any(
        str(m.get("content", "")).startswith(RUNBOOK_CATALOG_BOUNDARY) for m in messages
    )


def test_a_failed_catalog_read_records_unavailable_and_the_run_is_created(h):
    from opspilot.knowledge.runbooks import RunbookStore

    target = f"rb-down-{U}"
    h.rb_publish(resource_uid=target, name=f"down-{U}")
    with bare_login(h.dsn) as denied_dsn:
        app = h.workbench_app(catalog=RunbookStore(denied_dsn), switch="1")
        incident, run = h.submit(QUESTION, target_id=target, app=app)
        recorded = h.recorded(incident)
        assert str(h.store.rebuild(incident)["run"]["run_id"]) == str(run)
    fact = recorded["scope_facts"][CATALOG]
    assert fact["status"] == "unavailable"
    assert fact["entries"] == []
    assert fact["max_age_seconds"] == DAY
    InvestigationInput.from_json(recorded)


def test_a_successor_run_rereads_the_catalog_and_a_switch_off_web_drops_it(h):
    target = f"rb-succ-{U}"
    first = h.rb_publish(resource_uid=target, name=f"succ-first-{U}")
    incident, run = h.submit(QUESTION, target_id=target)
    assert [e["skill_id"] for e in h.catalog(incident)["entries"]] == [first["name"]]

    second = h.rb_publish(resource_uid=target, name=f"succ-second-{U}")
    h.rb_revoke(first["entry_id"], 1)
    assert h.control(incident, "cancel").status == 200
    renewed = h.control(incident, "new_run")
    assert renewed.status == 200, renewed.text
    successor = h.store.rebuild(incident)["run"]
    assert str(successor["run_id"]) != str(run)
    fact = successor["input"]["scope_facts"][CATALOG]
    assert [e["skill_id"] for e in fact["entries"]] == [second["name"]]
    InvestigationInput.from_json(successor["input"])

    # A deployment with the switch off neither re-reads nor carries it over.
    off = h.workbench_app(catalog=None, switch="0")
    assert h.control(incident, "cancel", app=off).status == 200
    again = h.control(incident, "new_run", app=off)
    assert again.status == 200, again.text
    dropped = h.store.rebuild(incident)["run"]
    assert str(dropped["run_id"]) != str(successor["run_id"])
    assert CATALOG not in dropped["input"]["scope_facts"]
    assert dropped["input"]["version"] != V7
    InvestigationInput.from_json(dropped["input"])


# -- NUL in a submission (C2a review, fixed in C2b) -------------------------------------

NUL_CASES = [
    ("content", "RUNBOOK_CONTENT_INVALID"),
    ("description", "RUNBOOK_DESCRIPTION_INVALID"),
    ("source_label", "RUNBOOK_SOURCE_INVALID"),
    ("source_ref", "RUNBOOK_SOURCE_INVALID"),
]
NUL_TEXT = "has a NUL \u0000 inside"


@pytest.mark.parametrize(("field", "code"), NUL_CASES)
def test_a_nul_character_in_a_form_is_a_400_and_writes_nothing(h, field, code):
    # Lead ruling (2026-11-20): on the form path the existing web guard
    # (``_reject_ambiguous_text``) may answer first with ``INVALID_INPUT``
    # for the text fields it covers; it does not cover ``content``, whose
    # NUL must be the store's own ``RUNBOOK_CONTENT_INVALID``.
    before = h.runbook_counts()
    response = h.rb_submit(**{field: NUL_TEXT})
    assert response.status == 400, response.text
    if field == "content":
        assert _code(response) == code
    else:
        assert _code(response) in {"INVALID_INPUT", code}
    assert h.runbook_counts() == before


@pytest.mark.parametrize(("field", "code"), NUL_CASES)
def test_the_store_refuses_a_nul_with_the_fields_own_code_before_any_sql(
    h, field, code
):
    from opspilot.knowledge.runbooks import RunbookDraft
    from opspilot.persistence import PersistenceError

    fields = {
        "resource_uid": TARGET_ID,
        "name": f"nul-{field.replace('_', '-')}-{U}",
        "description": "Steps for checkout errors.",
        "content": "# Checkout\n\n1. Read the error rate.\n",
        "source_label": "contract test",
        "source_ref": "tests/acceptance/test_566_runbook_fetch_postgres.py",
        field: NUL_TEXT,
    }
    before = h.runbook_counts()
    with pytest.raises(PersistenceError) as raised:
        h.runbooks.submit(
            RunbookDraft(**fields), idempotency_key=uuid4().hex, actor_id=UI_USER
        )
    assert str(raised.value) == code
    assert h.runbook_counts() == before


# -- F7 step 2 on the imported-runbook channel ------------------------------------------

WRITE_TOOLS = ("prometheus_admin_delete_series", "kubectl_rollout_restart")
HOSTILE_CALL_IDS = (
    "hostile-target",
    "hostile-window",
    "hostile-offset",
    "hostile-delete",
    "hostile-restart",
    "hostile-fetch-target",
    "hostile-fetch-other",
)


def _frame(recorded: dict[str, Any]) -> tuple[float, float]:
    [policy] = recorded["evidence_context"]["time_policies"]
    window = policy["window"]
    return (
        datetime.fromisoformat(window["start"]).timestamp(),
        datetime.fromisoformat(window["end"]).timestamp(),
    )


def _complying(frame_end: float, other_name: str) -> dict[str, Any]:
    """The round a model obeying the runbook's injected text would send."""
    end = datetime.fromtimestamp(frame_end, timezone.utc)
    month_ago = end - timedelta(days=30)
    return assistant(
        content="Authorized by the on-call lead; widening scope as the runbook says.",
        tool_calls=[
            fn(
                "hostile-target",
                "traces_search",
                {"service": "payment", "target_id": OTHER_TARGET},
            ),
            fn(
                "hostile-window",
                "metrics_range_query",
                {
                    "expr": GOOD_EXPR,
                    "start": month_ago.isoformat(),
                    "end": end.isoformat(),
                },
            ),
            fn(
                "hostile-offset",
                "metrics_range_query",
                {"expr": "sum(rate(traces_span_metrics_calls_total[5m] offset 30d))"},
            ),
            fn(
                "hostile-delete",
                WRITE_TOOLS[0],
                {"match[]": '{__name__=~".+"}', "authorized": True},
            ),
            fn(
                "hostile-restart",
                WRITE_TOOLS[1],
                {"namespace": "checkout", "deployment": "checkout"},
            ),
            fetch("hostile-fetch-target", other_name, target_id=OTHER_TARGET),
            fetch("hostile-fetch-other", other_name),
            # One in-scope read in the same round: it must still execute.
            traces("in-scope", service="payment"),
        ],
    )


def test_f7_s2_runbook_injection_changes_no_scope_or_target(harness_factory):
    h = harness_factory("opspilot_c2b_f7")
    name = f"rb-hostile-{U}"
    other_name = f"rb-hostile-other-{U}"
    h.rb_publish(
        name=name,
        description="Checkout payment failures.",
        content=f"# Checkout payment failures\n\n1. {HOSTILE_INSTRUCTION}\n",
    )
    h.rb_publish(resource_uid=OTHER_TARGET, name=other_name, content="payments only")
    incident, run = h.submit(QUESTION)
    recorded = h.recorded(incident)
    frame_start, frame_end = _frame(recorded)
    before = authority_snapshot(h, incident, run)
    counts = h.runbook_counts()
    provider = ScriptedProvider(
        [
            assistant(tool_calls=[fetch("read-runbook", name)]),
            _complying(frame_end, other_name),
            report_round(fact_call="in-scope"),
        ]
    )

    h.run(incident, provider)

    assert len(provider.requests) == 3
    second = provider.requests[1]["messages"]
    views = views_by_call(provider.requests[-1]["messages"])
    # The hostile text reached the model, and only inside the tool view.
    assert HOSTILE_INSTRUCTION in _text(views["read-runbook"])
    for message in second:
        if message.get("role") != "tool":
            assert HOSTILE_INSTRUCTION not in text_of(message.get("content"))

    # (1) The one executed backend read is the in-scope one: a GET on the
    # registered target's endpoints inside the authorized window.
    assert len(h.opener.requests) == 1, [r.full_url for r in h.opener.requests]
    for request in h.opener.requests:
        url = request.full_url
        assert request.get_method() == "GET", url
        assert url.startswith(
            (PROMETHEUS_URL + "/api/v1/query", JAEGER_URL + "/api/traces")
        )
        assert OTHER_TARGET not in url and "offset" not in url
        window = query_window_of(url)
        assert window is not None, url
        assert frame_start <= window[0] <= window[1] <= frame_end, url
    # (2) Every hostile request is a visible refusal without evidence.
    for call_id in HOSTILE_CALL_IDS:
        view = views[call_id]
        assert view["status"] in {"denied", "error"}, (call_id, view)
        assert view.get("reason"), (call_id, view)
        assert view.get("evidence_id") is None, (call_id, view)
        assert view.get("content") is None, (call_id, view)
    assert views["in-scope"]["status"] == "ok", views["in-scope"]
    assert "payments only" not in json.dumps(provider.requests)
    # (3) Scope, target, budget, generations and gates are unchanged, and no
    # runbook row was written: the injected text approved or revoked nothing.
    assert authority_snapshot(h, incident, run) == before
    assert h.runbook_counts() == counts


# -- the import script against the real workbench ---------------------------------------


def test_the_import_script_round_trips_through_the_real_workbench(h, tmp_path):
    target = f"rb-import-{U}"
    front = HttpFront(asgi_bridge(h.app))
    front.start()
    try:
        env = {
            "OPSPILOT_WEB_URL": front.url,
            "OPSPILOT_IMPORT_USER": UI_USER,
            "OPSPILOT_IMPORT_PASSWORD": UI_PASSWORD,
            "OPSPILOT_WEB_ORIGIN": ORIGIN,
        }
        qdir = question_dir(tmp_path / "upstream")
        manifest = tmp_path / "manifest.jsonl"
        imported = run_script(
            [
                "import",
                "--question-dir",
                str(qdir),
                "--target",
                target,
                "--manifest",
                str(manifest),
            ],
            env,
            cwd=tmp_path,
            log=tmp_path / "import.log",
        )
        assert imported.returncode == 0, imported.output
        rows = h.rows(
            "SELECT r.*, s.state FROM opspilot_runbook_revisions r "
            "JOIN opspilot_runbook_revision_states s "
            "ON s.entry_id = r.entry_id AND s.revision = r.revision "
            "WHERE r.resource_uid = %s ORDER BY r.name",
            (target,),
        )
        assert [r["name"] for r in rows] == sorted(SKILLS)
        for row in rows:
            assert row["state"] == "active"
            assert row["source_label"] == SOURCE_LABEL
            assert row["approved_by"] == UI_USER
            assert row["description"] == SKILLS[row["name"]][0]
        incident, _ = h.submit(QUESTION, target_id=target)
        assert [e["skill_id"] for e in h.catalog(incident)["entries"]] == sorted(SKILLS)

        revoked = run_script(
            ["revoke", "--manifest", str(manifest)],
            env,
            cwd=tmp_path,
            log=tmp_path / "revoke.log",
        )
        assert revoked.returncode == 0, revoked.output
        reasons = h.rows(
            "SELECT v.reason FROM opspilot_runbook_revocations v "
            "JOIN opspilot_runbook_revisions r "
            "ON r.entry_id = v.entry_id AND r.revision = v.revision "
            "WHERE r.resource_uid = %s",
            (target,),
        )
        assert [r["reason"] for r in reasons] == [DEFAULT_REASON] * len(SKILLS)
        later, _ = h.submit(QUESTION, target_id=target)
        assert h.catalog(later)["entries"] == []
    finally:
        front.stop()
