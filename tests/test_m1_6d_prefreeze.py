"""Three pre-freeze follow-ups to PR #56 before the v4 rerun candidate.

PR #56 added ``citable_as_fact``, ``lookback_seconds`` and
``lookback_start_at`` to every tool view. The user decided (2026-09-27) three
follow-ups before freezing the candidate:

A. The oversized-view stub (``view_stub`` / ``visible_view`` /
   ``fold_digest``) keeps the two lookback fields as provenance, so the
   model does not lose how far back a sample reached exactly when the view
   is largest. The stub shape is hashed into ``context_policy_revision``,
   so the revision recorded in ``docs/evidence/m1-01-report-contract`` must
   move.
B. ``citable_as_fact`` is part of the v4 evidence context: the projection
   carries a bool through and drops any other shape, and
   ``delivered_from_context`` fails closed -- a status-ok binding without
   the key is not fact evidence (PR #56 bot review P2, deferred item).
C. The executor rejects a ``lookback_seconds`` longer than the requested
   window as an adapter error, the same way it already rejects a negative
   one: nothing the adapter reports may claim a read the window did not
   authorize.

RED on the pre-fix tree except the accepted-boundary and unchanged-shape
regression pins.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from opspilot.investigation.context import (
    context_policy_revision,
    fold_digest,
    view_stub,
    visible_view,
)
from opspilot.investigation.limits import RunLimits
from opspilot.investigation.reports import (
    delivered_from_context,
    evidence_context_projection,
    parse_report,
    unsupported_citations,
)
from opspilot.tools import TransportResponse
from tests.m1_tool_support import WINDOW_END, WINDOW_START, body, build, request

# Pre-fix literal, pinned from docs/evidence/m1-01-report-contract/run.md
# (worker.log first line, ``versions.context_policy_revision``). The stub
# shape gains two keys, so a tree still reporting this value has not changed
# what the model sees after a spill.
PRE_FIX_CONTEXT_POLICY_REVISION = "ctx-ctx-policy-v1-26cf1129e6c7"

LOOKBACK_KEYS = ("lookback_seconds", "lookback_start_at", "citable_as_fact")

FIXTURE_RUN_ID = "run-9"
FIXTURE_TARGET = "checkout-prod"
FIXTURE_TARGETS = frozenset({FIXTURE_TARGET})
POLICY = "policy-window-1"
FACT_LIKE = frozenset({"fact", "counter_evidence", "rejected_hypothesis"})

# The requested window is the ToolRequest's own (``plan.window``); the tests
# below request exactly the authorized fixture window so its length is the
# bound, derived from the constants rather than restated.
WINDOW_SECONDS = int((WINDOW_END - WINDOW_START).total_seconds())
FULL_WINDOW = {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()}


# -- A. Oversized view stubs keep the lookback fields ------------------------


def _ok_view_with_lookback(lookback: int = 300) -> dict:
    """A real persisted ok view from the fixture executor carrying a lookback."""
    executor, transport, _, _ = build()
    transport.response = TransportResponse(
        body=body([{"metric": "x", "value": 1}]),
        lookback_seconds=lookback,
    )
    outcome = executor.execute(request())
    assert outcome.status == "ok"
    view = dict(outcome.model_view)
    assert view["lookback_seconds"] == lookback
    assert isinstance(view["lookback_start_at"], str)
    assert view["citable_as_fact"] is True
    return view


def _ok_view_without_lookback() -> dict:
    """The fixture transport reports no lookback: both view fields are None."""
    executor, transport, _, _ = build()
    transport.response = TransportResponse(body=body([{"metric": "x", "value": 1}]))
    view = dict(executor.execute(request()).model_view)
    assert view["lookback_seconds"] is None and view["lookback_start_at"] is None
    return view


def test_view_stub_keeps_lookback_seconds_and_lookback_start_at_verbatim():
    """A spilled view must still tell the model how far before the window
    its samples reached; the stub carries both lookback fields unchanged."""
    view = _ok_view_with_lookback(300)

    stub = view_stub(view, preview_chars=16)

    assert stub["spilled"] is True
    assert stub["lookback_seconds"] == 300
    assert stub["lookback_start_at"] == view["lookback_start_at"]
    assert stub["citable_as_fact"] is True


def test_view_stub_carries_none_when_the_view_has_no_lookback():
    """Like the other provenance keys, an absent value is present as None
    rather than missing, so the stub shape is fixed."""
    view = _ok_view_without_lookback()
    del view["lookback_seconds"]
    del view["lookback_start_at"]

    stub = view_stub(view, preview_chars=16)

    for key in ("lookback_seconds", "lookback_start_at"):
        assert key in stub
        assert stub[key] is None


def test_visible_view_over_the_single_tool_cap_keeps_the_lookback_fields():
    """Mechanism 1 through its public entry: the cap replaces the view by the
    stub, and the stub is what the model reads for lookback."""
    view = _ok_view_with_lookback(300)
    limits = RunLimits(context_tokens=200, output_tokens=100)

    visible = visible_view(view, limits=limits)

    assert visible is not view and visible.get("spilled") is True
    assert visible["lookback_seconds"] == 300
    assert visible["lookback_start_at"] == view["lookback_start_at"]
    assert visible["citable_as_fact"] is True


def test_fold_digest_tool_entries_keep_the_lookback_fields():
    """Compaction (mechanism 2) folds the tool view into a provenance entry;
    the lookback fields are provenance and must be in that entry verbatim."""
    view = _ok_view_with_lookback(300)
    messages = [{"role": "tool", "tool_call_id": "call-1", "content": json.dumps(view)}]

    digest = fold_digest(messages)

    (entry,) = digest["views"]
    assert entry["tool_call_id"] == "call-1"
    assert entry["lookback_seconds"] == 300
    assert entry["lookback_start_at"] == view["lookback_start_at"]
    assert entry["citable_as_fact"] is True


def test_fold_digest_entry_has_none_lookback_for_an_unparseable_tool_message():
    """The digest entry shape is fixed: even a tool message without a JSON
    view contributes every provenance key, the lookback ones included."""
    messages = [{"role": "tool", "tool_call_id": "call-1", "content": "not json"}]

    digest = fold_digest(messages)

    (entry,) = digest["views"]
    for key in LOOKBACK_KEYS:
        assert key in entry
        assert entry[key] is None


def test_context_policy_revision_moved_with_the_lookback_stub_keys():
    """C3 §5: the stub shape is hashed into the revision, so the value the
    6c Run recorded must not be reclaimable under the new shape."""
    assert context_policy_revision() != PRE_FIX_CONTEXT_POLICY_REVISION


# -- B. citable_as_fact in the v4 evidence context -----------------------------


def _v4_context(evidence_id: str, **binding_extra) -> dict:
    return {
        "type": "opspilot-evidence-context-v4",
        "run_id": FIXTURE_RUN_ID,
        "time_policies": [{"id": POLICY}],
        "view_bindings": {
            evidence_id: {
                "status": "ok",
                "target_refs": [FIXTURE_TARGET],
                "time_scope_refs": [POLICY],
                **binding_extra,
            }
        },
    }


def _claim(evidence_id: str, *, kind: str = "fact") -> dict:
    """A fully scoped claim: valid target_ref and time_scope_ref, so the only
    thing left for the checker to reject is the cited view's flag."""
    return {
        "kind": kind,
        "text": f"{kind} citing {evidence_id}",
        "evidence_ids": [evidence_id],
        "target_refs": [FIXTURE_TARGET],
        "time_scope_ref": POLICY,
    }


def _report(*claims: dict):
    conclusion = "supported" if any(c["kind"] == "fact" for c in claims) else "partial"
    report, reason = parse_report(
        json.dumps(
            {
                "schema_version": "m0-report-v2",
                "assessment_status": "completed",
                "conclusion": conclusion,
                "summary": "Citation flag test.",
                "claims": list(claims),
                "gaps": [],
                "next_steps": [],
            }
        ),
        finish_reason="stop",
    )
    assert report is not None, reason
    return report


def _rejected(report, views) -> bool:
    return unsupported_citations(
        report,
        views=views,
        authorized_targets=FIXTURE_TARGETS,
        time_policy_ids=(POLICY,),
        target_catalog=None,
    )


@pytest.mark.parametrize("flag", [True, False])
def test_projection_carries_a_bool_citable_as_fact_through(flag):
    """The flag is part of the binding shape the loop reads, so the
    allowlist projection must keep it exactly as given when it is a bool."""
    context = _v4_context("ev-bound", citable_as_fact=flag)

    projected = evidence_context_projection(context, run_id=FIXTURE_RUN_ID)

    assert projected is not None
    binding = projected["view_bindings"]["ev-bound"]
    assert "citable_as_fact" in binding
    assert binding["citable_as_fact"] is flag


@pytest.mark.parametrize("value", ["true", 1, None, {}], ids=repr)
def test_projection_drops_a_non_bool_citable_as_fact(value):
    """Same rule as every other binding field: a value of the wrong shape is
    dropped, not coerced, so a string ``"true"`` or an int cannot reach the
    citation check as a truthy flag."""
    context = _v4_context("ev-bound", citable_as_fact=value)

    projected = evidence_context_projection(context, run_id=FIXTURE_RUN_ID)

    assert projected is not None
    binding = projected["view_bindings"]["ev-bound"]
    assert "citable_as_fact" not in binding
    # The rest of the binding survives; only the malformed field is gone.
    assert binding["status"] == "ok"


def test_projection_leaves_a_binding_without_the_key_without_it():
    """No fabrication: a binding that never had the key does not gain one."""
    context = _v4_context("ev-bound")

    projected = evidence_context_projection(context, run_id=FIXTURE_RUN_ID)

    assert projected is not None
    assert "citable_as_fact" not in projected["view_bindings"]["ev-bound"]


def test_delivered_from_context_without_the_key_is_not_citable_as_fact():
    """Fail closed (PR #56 bot review P2, deferred; user decision
    2026-09-27): a status-ok binding that does not say ``citable_as_fact``
    is delivered, but not as fact evidence."""
    context = _v4_context("ev-bound")

    views = delivered_from_context(context, run_id=FIXTURE_RUN_ID)

    assert [view.evidence_id for view in views] == ["ev-bound"]
    (view,) = views
    assert view.status == "ok"
    assert view.citable_as_fact is False


@pytest.mark.parametrize("kind", sorted(FACT_LIKE))
def test_fact_like_claim_cannot_cite_a_context_binding_without_the_key(kind):
    """Each fact-like kind is rejected against the key-less binding; the same
    claim binds once the binding says ``citable_as_fact: true``."""
    report = _report(_claim("ev-bound", kind=kind))

    without = delivered_from_context(_v4_context("ev-bound"), run_id=FIXTURE_RUN_ID)
    with_true = delivered_from_context(
        _v4_context("ev-bound", citable_as_fact=True), run_id=FIXTURE_RUN_ID
    )

    assert _rejected(report, without) is True
    assert _rejected(report, with_true) is False


@pytest.mark.parametrize("kind", ["hypothesis", "recommendation"])
def test_non_fact_claim_may_cite_a_context_binding_without_the_key(kind):
    """Non-fact kinds are unaffected by the flag."""
    report = _report(_claim("ev-bound", kind=kind))

    views = delivered_from_context(_v4_context("ev-bound"), run_id=FIXTURE_RUN_ID)

    assert _rejected(report, views) is False


def test_delivered_from_context_explicit_true_is_citable():
    views = delivered_from_context(
        _v4_context("ev-bound", citable_as_fact=True), run_id=FIXTURE_RUN_ID
    )

    assert views[0].citable_as_fact is True
    assert _rejected(_report(_claim("ev-bound")), views) is False


def test_delivered_from_context_explicit_false_is_not_citable():
    views = delivered_from_context(
        _v4_context("ev-bound", citable_as_fact=False), run_id=FIXTURE_RUN_ID
    )

    assert views[0].citable_as_fact is False
    assert _rejected(_report(_claim("ev-bound")), views) is True


@pytest.mark.parametrize("value", ["true", 1], ids=repr)
def test_delivered_from_context_non_bool_flag_is_not_citable(value):
    """Only the bool ``True`` opens the fact path; a truthy string or int is
    not the flag and fails closed."""
    views = delivered_from_context(
        _v4_context("ev-bound", citable_as_fact=value), run_id=FIXTURE_RUN_ID
    )

    assert [view.evidence_id for view in views] == ["ev-bound"]
    assert views[0].citable_as_fact is False
    assert _rejected(_report(_claim("ev-bound")), views) is True


def test_projection_then_delivered_from_context_keeps_explicit_true_citable():
    """The loop feeds the projected context, not the raw one, into
    ``delivered_from_context``; composed, an explicit True still binds."""
    projected = evidence_context_projection(
        _v4_context("ev-bound", citable_as_fact=True), run_id=FIXTURE_RUN_ID
    )
    assert projected is not None

    views = delivered_from_context(projected, run_id=FIXTURE_RUN_ID)

    assert [view.evidence_id for view in views] == ["ev-bound"]
    assert views[0].citable_as_fact is True
    assert _rejected(_report(_claim("ev-bound")), views) is False


def test_projection_then_delivered_from_context_fails_closed_on_a_string_flag():
    """Composed: the projection drops ``"true"`` and the delivery then sees
    no key, so the two layers agree on not-citable."""
    projected = evidence_context_projection(
        _v4_context("ev-bound", citable_as_fact="true"), run_id=FIXTURE_RUN_ID
    )
    assert projected is not None

    views = delivered_from_context(projected, run_id=FIXTURE_RUN_ID)

    assert views[0].citable_as_fact is False
    assert _rejected(_report(_claim("ev-bound")), views) is True


# -- C. Defensive upper bound on lookback_seconds in the executor --------------


def _execute_with_lookback(lookback):
    """Execute the fixture request over the full authorized window with a
    transport that reports ``lookback``; the request is otherwise the one
    ``test_view_carries_lookback_seconds_and_lookback_start_at`` uses."""
    executor, transport, sink, _ = build()
    transport.response = TransportResponse(
        body=body([{"metric": "x", "value": 1}]),
        lookback_seconds=lookback,
    )
    return executor.execute(request(window=FULL_WINDOW)), sink


def test_lookback_longer_than_the_requested_window_is_a_malformed_result():
    """A read that reaches further back than the window is long is not a
    read the window authorized; the adapter claiming one is rejected exactly
    like a negative lookback."""
    outcome, sink = _execute_with_lookback(WINDOW_SECONDS + 1)

    assert (outcome.status, outcome.reason) == ("error", "MALFORMED_RESULT")
    assert outcome.adopted is False
    assert sink.records == [] and outcome.evidence is None


def test_enormous_lookback_is_a_malformed_result_without_overflow():
    """``10**20`` seconds does not fit a ``timedelta``; the bound must reject
    it before any arithmetic so no ``OverflowError`` escapes the executor."""
    outcome, _ = _execute_with_lookback(10**20)

    assert (outcome.status, outcome.reason) == ("error", "MALFORMED_RESULT")


def test_lookback_equal_to_the_requested_window_is_accepted():
    """Boundary: a selector of exactly the window length is what
    ``promql_problem`` admits, so the executor must accept it too.
    ``lookback_start_at`` is the earliest instant read, the window start
    (window-points contract, rule 3)."""
    outcome, _ = _execute_with_lookback(WINDOW_SECONDS)

    assert (outcome.status, outcome.reason) == ("ok", None)
    view = outcome.model_view
    assert view["lookback_seconds"] == WINDOW_SECONDS
    assert datetime.fromisoformat(view["lookback_start_at"]) == WINDOW_START
    assert outcome.evidence.view["lookback_start_at"] == view["lookback_start_at"]


def test_zero_lookback_over_the_full_window_is_still_accepted():
    """Regression pin: the bound is an upper bound only; an instant selector
    reads nothing before the window and stays accepted."""
    outcome, _ = _execute_with_lookback(0)

    assert (outcome.status, outcome.reason) == ("ok", None)
    assert outcome.model_view["lookback_seconds"] == 0
    assert datetime.fromisoformat(outcome.model_view["lookback_start_at"]) == (
        WINDOW_START
    )


# Pinned from docs/evidence/m1-01-report-contract/run.md (worker.log first
# line): the 6c literal the one-constraint-per-sentence rewrite must move.
PRE_6D_TOOL_SCHEMA_REVISION = "otel-demo-79788429f835"


def test_tool_schema_revision_moved_with_the_sentence_rewrite():
    """C3 §5: the metrics description and ``values_format`` were re-split
    one constraint per sentence (DeepSeek reference §2.5); the model-visible
    bytes changed, so the content hash must not equal the 6c value
    (independent review P3-1)."""
    from opspilot.tools.otel_demo import TOOL_SCHEMA_REVISION

    assert TOOL_SCHEMA_REVISION != PRE_6D_TOOL_SCHEMA_REVISION
