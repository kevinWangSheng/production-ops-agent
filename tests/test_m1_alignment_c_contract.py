"""Contract tests for M1-01 batch C
(``docs/tasks/2026-09-28-m1-01-alignment-c.md``, C1-C3) -- written from the
contract text and the public interface of ``opspilot.instructions.discipline``
/ ``opspilot.investigation.{loop,reports}`` / ``opspilot.tools.otel_demo`` /
``opspilot.tools.registry``, not from any batch-C implementation, which does
not exist yet on this tree. No implementation code for C1-C3 was read while
writing these assertions. Hermetic: no network, no PostgreSQL.

Interpretive notes (the contract text names the required *observable*
behavior, not the exact wording or field names the implementer will choose;
flagged so a mismatch reads as a contract question, not a silent assertion
change):

* C2: the four example "normalized reason names" the contract lists are
  asserted by structure -- a claim-index marker (a ``claim``/``claims`` token
  immediately followed by the 0-based index), the claim's own evidence_id
  nearby, and (for the JSON-parse-failure case) a position description
  resembling ``json.JSONDecodeError``'s own ``str()`` (``line N column M
  (char K)``) -- never by a hardcoded reason string, since the checker does
  not have one yet for the citation-side failures (only the schema-side
  pydantic validators already carry ``ALL_CAPS`` codes such as
  ``FACT_SCOPE_REQUIRED``; ``unsupported_citations`` today returns a bare
  bool with no reason vocabulary at all).
* C2's "去重" (dedup) is asserted only in the direction that is safe to pin
  without guessing the implementation's item identity: two *different*
  claims that cite the *same* bad evidence_id must not be collapsed into one
  item (each claim gets its own indexed entry). The other direction -- what,
  if anything, counts as a true duplicate item worth suppressing -- has no
  reproducible fixture without inventing an implementation shape, so it is
  left untested here.
* C2's "仍只允许 1 次修复" is not re-tested here: it is an unchanged L1a/B3
  invariant, already covered by
  ``tests/test_m1_upstream_alignment_b_contract.py::test_b3_merged_cap_a_second_validation_failure_ends_report_invalid_with_no_third_request``,
  which does not touch the feedback's content and so is insensitive to C2.
* C3: "span_groups 计入视图哈希" is tested directly (the emitted
  ``view_sha256`` must equal ``canonical_hash`` of the view that already
  carries ``span_groups``). "计入上限" is tested with a fixture of 180
  distinct-operation spans (see the M1-01 A note below): the row array alone
  fits ``MAX_VIEW_TOKENS`` while the whole view, with its 180-entry
  ``span_groups``, does not, so the view must be refused whole; and
  ``span_groups`` must summarize exactly the rows in the delivered view.
* C3's status/error/duration fields are asserted with only numeric-valued
  status tags (``rpc.grpc.status_code``, ``http.status_code``), matching the
  contract's own literal example (``"rpc.grpc.status_code=0": 13``) and
  avoiding the reference script's own idiosyncratic string/bool formatting
  (``json.dumps`` / bare ``str()``) for tag values, which is an
  implementation detail of that script, not contract text.

2026-09-28 independent-review follow-up (2 required + 1 optional, requested
by the team lead after the first review pass): the view-cap fixture above
(C3), a schema-failure analogue of the C2 cap-at-50 test (120 claims with an
empty ``text`` field -- a pydantic ``ValidationError`` with one ``loc`` entry
per claim, not a citation failure), and a light guard that ``span_groups``
is additive and does not disturb the generic (non-trace-specific) view
fields ``_record()`` already builds for every tool.

M1-01 A (v3) update (docs/tasks/2026-09-29-m1-01-view-bytes-timeout.md): the
byte-truncation half of the C3 tests below no longer exists. An over-limit
view is refused whole (``RESULT_TOO_LARGE``) instead of losing tail rows, and
``span_groups`` is computed over every row of the delivered view. The two
tests that pinned truncation were rewritten to the surviving intent: span_groups
summarizes exactly the rows in the view, and it counts toward the cap (now
``MAX_VIEW_TOKENS``, measured in real DeepSeek tokens).

Conflicting existing tests (not modified here; the implementer's call how to
resolve, per the task's own "既有测试因合同变化的修改逐条记录"):

* ``tests/test_instruction_discipline.py:613`` --
  ``assert d.FIXED_WINDOW in candidate, "候选臂的窗口句是无条件的，不得被一并裁掉"``
  inside ``test_scope_conditional_segments_are_structural_not_a_suffix_trim``
  asserts the exact sentence C1 requires removed from the live
  ``replay-candidate`` variant. It was correct for the prior contract (the
  candidate's window clause was unconditional, unlike the baselines'); C1
  supersedes it for that one variant.
* ``tests/test_instruction_discipline.py:275`` --
  ``assert variant_sentences("replay-candidate") == 14`` inside
  ``test_sentence_census_matches_the_recorded_layer_analysis`` will drop to
  13 once C1 removes ``replay-candidate``'s one ``FIXED_WINDOW`` sentence;
  the surrounding docstring (lines ~251-260) narrates 14 as the "M1-01
  2026-09-28" outcome, which C1 (a later 2026-09-28 decision) revises again.
"""

from __future__ import annotations

import json
import re
import urllib.request
from collections.abc import Mapping
from datetime import timedelta
from uuid import uuid4

from opspilot.instructions import discipline as d
from opspilot.investigation.loop import DISCIPLINE_VARIANT, prompt_revision_versions
from opspilot.investigation.reports import REPORT_CONTRACT
from opspilot.persistence import Lease
from opspilot.tools import ReadOnlyToolExecutor, ToolRequest
from opspilot.tools.otel_demo import (
    MAX_VIEW_TOKENS,
    TARGET_ID,
    TOOL_SCHEMAS,
    TRACES_TOOL,
    OtelDemoConfig,
    otel_demo_executor_factory,
    otel_demo_face,
    project_traces,
)
from opspilot.tools.registry import canonical, canonical_hash
from opspilot.tools.tokens import count_tokens
from tests.m1_investigation_support import assemble, reply, tool_call
from tests.m1_tool_support import FakeClock, RecordingSink, body
from tests.test_m1_otel_demo_contract import (
    CHECKOUT_TRACES,
    JAEGER_URL,
    NOW,
    PROMETHEUS_URL,
    WINDOW,
    FakeOpener,
    FakeStore,
)

# =============================================================================
# C1 -- drop the stale FIXED_WINDOW sentence from the live replay-candidate
#        variant; the two frozen baseline variants keep it byte-for-byte
# =============================================================================

# Captured on this pre-batch-C tree (``git log`` HEAD at authoring time), via
# ``prompt_revision_versions()["prompt_revision"]`` / ``d.discipline_revision(
# "replay-candidate")`` respectively. Deliberately the OLD value, not a guess
# at the new one (team-lead instruction): the live call below is compared for
# *inequality* to this frozen anchor, so the test is red on this tree (they
# are equal today) and turns green the moment either revision moves.
_PRE_BATCH_C_PROMPT_REVISION = "prompt-replay-candidate-3507addfe1e6"
_PRE_BATCH_C_DISCIPLINE_REVISION = "l1a-replay-candidate-7ce7eab9a7aa"


def test_c1_replay_candidate_render_no_longer_contains_the_fixed_window_sentence():
    rendered = d.render(
        DISCIPLINE_VARIANT, model_requests=2, report_contract=REPORT_CONTRACT
    )
    assert d.FIXED_WINDOW not in rendered
    assert "fixed by the trusted runner" not in rendered
    assert "do not supply start/end tool parameters" not in rendered


def test_c1_baseline_variants_still_render_the_fixed_window_sentence_unchanged():
    """Green today and after C1: the two frozen baseline variants are out of
    scope for C1 ("历史 baseline 变体仍含原句（字节不变）") -- this is a
    fixture-sanity check that they still carry the clause, not new C1
    behavior."""
    rendered = d.render(
        "baseline-multi-step",
        model_requests=4,
        report_contract=REPORT_CONTRACT,
        authorized_services=("checkoutservice",),
    )
    assert d.FIXED_WINDOW in rendered


def test_c1_discipline_revision_moves_off_the_pre_batch_c_snapshot():
    """``discipline_revision`` hashes only L1a (the variant's template
    segments), so this isolates the C1 template edit from any C2 change to
    the L2 retry template, which only ``prompt_revision`` would also catch."""
    assert d.discipline_revision(DISCIPLINE_VARIANT) != _PRE_BATCH_C_DISCIPLINE_REVISION


def test_c1_prompt_revision_moves_off_the_pre_batch_c_snapshot():
    """The loop's actual single source for the live ``replay-candidate``
    prompt's version (C3 §5 ``versions``); moves from either C1 (L1a) or C2
    (the new L2 retry template), matching the task doc's "prompt_revision
    随之变化"."""
    assert prompt_revision_versions()["prompt_revision"] != _PRE_BATCH_C_PROMPT_REVISION


# =============================================================================
# C2 -- repair feedback names, per failed claim: its index, the evidence_id
#        it cited, and a normalized error type; sorted, deduped, capped at 50
#        with a truncation note, never raw view content; still one retry
# =============================================================================


def _final_retry_feedback(model) -> str:
    """The isolated new diagnostic message of the forced final round, i.e.
    ``loop.py``'s ``_final_messages()``'s middle element
    (``[FINAL_REPORT_INSTRUCTION, retry_feedback, run_coverage_message]``),
    not the whole transcript -- so assertions below never accidentally match
    text from an earlier turn still sitting in history."""
    messages = model.calls[-1].messages
    assert len(messages) >= 3
    return str(messages[-2]["content"])


def _claim_marker_positions(text: str) -> dict[int, int]:
    """Every ``claim``/``claims`` token immediately (within 20 non-digit
    characters) followed by a 1-to-3-digit index (fixtures below go up to
    119), mapped to its first position in *text*. Structural, not a pinned
    reason string -- see the module docstring's interpretive note on C2."""
    positions: dict[int, int] = {}
    for match in re.finditer(r"claims?[^0-9]{0,20}(\d{1,3})\b", text, re.IGNORECASE):
        index = int(match.group(1))
        positions.setdefault(index, match.start())
    return positions


def _bad_citation_report(call) -> "reply":  # type: ignore[valid-type]
    """Round-2 reply for the main C2 scenario: a schema-valid report whose
    two fact claims each fail a different citation rule -- claim 0 cites the
    round-1 view, which is ``no_data`` (never citable as fact); claim 1 cites
    an evidence_id nothing in this Run delivered."""
    no_data_id = None
    for message in call.messages:
        if message.get("role") == "tool":
            no_data_id = json.loads(message["content"])["evidence_id"]
            break
    assert no_data_id, "fixture sanity: round 1 must have delivered a view"
    payload = {
        "schema_version": "m0-report-v2",
        "assessment_status": "completed",
        "conclusion": "supported",
        "summary": "Two claims, each citing a different bad reference.",
        "claims": [
            {
                "kind": "fact",
                "text": "The no_data view is treated as a supported fact.",
                "evidence_ids": [no_data_id],
                "target_refs": ["checkout-prod"],
                "time_scope_ref": "policy-window-1",
            },
            {
                "kind": "fact",
                "text": "An evidence_id nothing in this Run delivered.",
                "evidence_ids": ["not-a-real-id"],
                "target_refs": ["checkout-prod"],
                "time_scope_ref": "policy-window-1",
            },
        ],
        "gaps": [],
        "next_steps": [],
    }
    return reply(content=json.dumps(payload, ensure_ascii=False))


def _clean_incomplete_report() -> str:
    payload = {
        "schema_version": "m0-report-v2",
        "assessment_status": "incomplete",
        "conclusion": "inconclusive",
        "summary": "Ending after the retry without citing any evidence.",
        "claims": [],
        "gaps": ["No supportable claim was available after the retry."],
        "next_steps": [],
    }
    return json.dumps(payload, ensure_ascii=False)


def _run_bad_citation_scenario():
    loop, request, model, transport, store, sink = assemble(
        replies=[
            reply(tool_calls=[tool_call(call_id="c1")], finish="tool_calls"),
            _bad_citation_report,
            reply(content=_clean_incomplete_report(), finish="stop"),
        ],
        model_requests=5,
    )
    from opspilot.tools import TransportResponse

    transport.response = TransportResponse(body=body([]))  # empty rows -> no_data
    outcome = loop.run(request)
    return outcome, model


def test_sanity_the_bad_citation_scenario_completes_via_the_existing_retry_mechanism():
    """Green on this tree: proves the double setup (no_data view delivered,
    two-claim bad report, forced final retry, clean recovery) is sound
    independent of C2's new feedback content -- what changes under C2 is
    *what the retry message says*, not whether the retry happens."""
    outcome, model = _run_bad_citation_scenario()
    assert outcome.execution == "completed", outcome.handoff_reasons
    assert outcome.model_requests_used == 3
    assert len(model.calls) == 3


def test_c2_citation_failures_list_claim_index_evidence_id_and_are_sorted_ascending():
    outcome, model = _run_bad_citation_scenario()
    assert outcome.execution == "completed", outcome.handoff_reasons
    feedback = _final_retry_feedback(model)
    assert "REPORT_INVALID" in feedback

    no_data_id = None
    for message in model.calls[1].messages:
        if message.get("role") == "tool":
            no_data_id = json.loads(message["content"])["evidence_id"]
    assert no_data_id

    markers = _claim_marker_positions(feedback)
    assert 0 in markers and 1 in markers, (
        f"expected both claim 0 and claim 1 named in the retry feedback: {feedback!r}"
    )
    assert markers[0] < markers[1], "items must be listed in ascending claim order"

    item0 = feedback[markers[0] : markers[1]]
    item1 = feedback[markers[1] :]
    assert no_data_id in item0, "claim 0's item must name the no_data evidence_id"
    assert "not-a-real-id" in item1, "claim 1's item must name the unknown evidence_id"
    # The two failure reasons are different in kind (a non-ok/not-citable
    # view vs. an id nothing delivered); their items must read differently,
    # not repeat one templated phrase with only the id swapped in.
    assert item0.replace(no_data_id, "") != item1.replace("not-a-real-id", "")


def test_c2_a_time_scope_mismatch_failure_is_itemized_and_omits_raw_view_content():
    """Third example error type from the contract text ("缺少 target/time
    引用"): a fact claim cites a real, ok, citable view but names a
    time_scope_ref this Run never delivered. Also re-checks (B3's own
    concern, still binding under C2) that the new feedback never carries a
    view's raw content -- here using an ``ok`` view (unlike the main C2 test,
    whose ``no_data`` view has no row content to leak) so there is something
    real to fail to leak."""
    raw_marker = "raw-marker-c2-7f1e9-not-in-feedback"

    def _mismatched_time_scope_report(call):
        ok_id = None
        for message in call.messages:
            if message.get("role") == "tool":
                ok_id = json.loads(message["content"])["evidence_id"]
        assert ok_id
        payload = {
            "schema_version": "m0-report-v2",
            "assessment_status": "completed",
            "conclusion": "supported",
            "summary": "A fact naming a time_scope_ref this Run never delivered.",
            "claims": [
                {
                    "kind": "fact",
                    "text": "The ok view is cited under an undelivered time policy.",
                    "evidence_ids": [ok_id],
                    "target_refs": ["checkout-prod"],
                    "time_scope_ref": "policy-window-not-delivered",
                }
            ],
            "gaps": [],
            "next_steps": [],
        }
        return reply(content=json.dumps(payload, ensure_ascii=False))

    loop, request, model, transport, store, sink = assemble(
        replies=[
            reply(tool_calls=[tool_call(call_id="c1")], finish="tool_calls"),
            _mismatched_time_scope_report,
            reply(content=_clean_incomplete_report(), finish="stop"),
        ],
        model_requests=5,
    )
    from opspilot.tools import TransportResponse
    from tests.m1_tool_support import WINDOW_START

    transport.response = TransportResponse(
        body=body([{"metric": "checkout", "value": raw_marker}]),
        data_as_of=WINDOW_START,
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed", outcome.handoff_reasons

    feedback = _final_retry_feedback(model)
    assert "REPORT_INVALID" in feedback
    markers = _claim_marker_positions(feedback)
    assert 0 in markers, f"expected claim 0 named in the retry feedback: {feedback!r}"
    assert raw_marker not in feedback, (
        "the retry feedback must never carry a view's raw content"
    )


def test_c2_claims_sharing_an_undelivered_id_are_not_collapsed_into_one_item():
    """Two *different* claims citing the exact same bad evidence_id must
    each still get their own indexed item -- dedup must not erase which
    claim(s) are actually broken (see the module docstring's interpretive
    note on the direction "去重" is tested)."""
    payload = {
        "schema_version": "m0-report-v2",
        "assessment_status": "incomplete",
        "conclusion": "inconclusive",
        "summary": "Two hypotheses, both citing the same undelivered id.",
        "claims": [
            {
                "kind": "hypothesis",
                "text": "First hypothesis citing the shared bad id.",
                "evidence_ids": ["shared-bad-id"],
            },
            {
                "kind": "hypothesis",
                "text": "Second hypothesis citing the same shared bad id.",
                "evidence_ids": ["shared-bad-id"],
            },
        ],
        "gaps": ["Placeholder."],
        "next_steps": [],
    }
    loop, request, model, transport, store, sink = assemble(
        replies=[
            reply(content=json.dumps(payload, ensure_ascii=False), finish="stop"),
            reply(content=_clean_incomplete_report(), finish="stop"),
        ],
        model_requests=5,
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed", outcome.handoff_reasons
    feedback = _final_retry_feedback(model)
    markers = _claim_marker_positions(feedback)
    assert 0 in markers and 1 in markers, (
        f"both claims must keep their own item despite sharing an id: {feedback!r}"
    )
    assert markers[0] != markers[1]


def test_c2_feedback_is_capped_at_50_items_and_notes_truncation():
    claims = [
        {
            "kind": "hypothesis",
            "text": f"Hypothesis {i}.",
            "evidence_ids": [f"bad-id-{i}"],
        }
        for i in range(60)
    ]
    payload = {
        "schema_version": "m0-report-v2",
        "assessment_status": "incomplete",
        "conclusion": "inconclusive",
        "summary": "Sixty claims, all citing distinct undelivered ids.",
        "claims": claims,
        "gaps": ["Placeholder."],
        "next_steps": [],
    }
    loop, request, model, transport, store, sink = assemble(
        replies=[
            reply(content=json.dumps(payload, ensure_ascii=False), finish="stop"),
            reply(content=_clean_incomplete_report(), finish="stop"),
        ],
        model_requests=5,
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed", outcome.handoff_reasons
    feedback = _final_retry_feedback(model)
    markers = _claim_marker_positions(feedback)
    assert len(markers) <= 50
    for index in (0, 1, 25, 49):
        assert index in markers, f"claim {index} must still be listed: {feedback!r}"
    assert 50 not in markers and 59 not in markers, (
        "at most 50 items are listed, in ascending claim order -- claims 50+ "
        f"must not appear: {feedback!r}"
    )
    assert re.search(r"truncat\w*|\bmore\b|\b60\b", feedback, re.IGNORECASE), (
        f"a truncated list must say so: {feedback!r}"
    )


def test_c2_json_parse_failure_feedback_includes_a_position_description():
    """Today, a JSON-decode failure's retry feedback carries only the bare
    ``REPORT_INVALID`` reason code (``report_retry_feedback``'s ``detail`` is
    only ever computed from ``unsupported_evidence_ids``, which needs a
    parsed report and gets ``None`` here) -- red until C2 adds a position
    description for this case too."""
    loop, request, model, transport, store, sink = assemble(
        replies=[
            reply(content='```json\n{"not": "bare json"}\n```', finish="stop"),
            reply(content=_clean_incomplete_report(), finish="stop"),
        ],
        model_requests=5,
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed", outcome.handoff_reasons
    feedback = _final_retry_feedback(model)
    assert "REPORT_INVALID" in feedback
    assert re.search(
        r"\bline\b.{0,20}\bcolumn\b|\bchar(?:acter)?\b\s*\d+", feedback, re.IGNORECASE
    ), f"a JSON parse failure must include a position description: {feedback!r}"


def test_c2_schema_failure_feedback_is_also_capped_at_50_with_truncation_note():
    """Review follow-up: the cap-at-50-with-truncation-note requirement is
    not specific to citation failures -- a schema (pydantic) failure with
    many broken claims must get the same treatment. 120 claims each with an
    empty ``text`` field fail ``ReportV2``'s ``Text = Annotated[str,
    Field(min_length=1)]`` constraint; ``ReportV2.model_validate`` raises one
    ``ValidationError`` whose ``.errors()`` carries exactly 120 entries, each
    ``loc`` naming its own claim index (``('claims', i, 'text')``) --
    verified against pydantic directly before writing this fixture. Today
    this is a bare parse failure (``report`` is ``None``), so
    ``report_retry_feedback`` has no per-claim detail to draw on at all."""
    claims = [
        {"kind": "hypothesis", "text": "", "evidence_ids": []} for _ in range(120)
    ]
    payload = {
        "schema_version": "m0-report-v2",
        "assessment_status": "incomplete",
        "conclusion": "inconclusive",
        "summary": "One hundred twenty claims, each with an empty text field.",
        "claims": claims,
        "gaps": ["Placeholder."],
        "next_steps": [],
    }
    loop, request, model, transport, store, sink = assemble(
        replies=[
            reply(content=json.dumps(payload, ensure_ascii=False), finish="stop"),
            reply(content=_clean_incomplete_report(), finish="stop"),
        ],
        model_requests=5,
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed", outcome.handoff_reasons
    feedback = _final_retry_feedback(model)
    assert "REPORT_INVALID" in feedback
    markers = _claim_marker_positions(feedback)
    assert len(markers) <= 50
    for index in (0, 1, 25, 49):
        assert index in markers, f"claim {index} must still be listed: {feedback!r}"
    assert 50 not in markers and 119 not in markers, (
        "at most 50 items, in ascending claim order -- claims 50+ must not "
        f"appear: {feedback!r}"
    )
    assert re.search(r"truncat\w*|\bmore\b|\b120\b", feedback, re.IGNORECASE), (
        f"a truncated list must say so: {feedback!r}"
    )


# =============================================================================
# C3 -- traces_search views gain span_groups: per (service, operation),
#        computed only from displayed rows, sorted, counted into the hash
# =============================================================================


def _trace_lease() -> Lease:
    return Lease(
        incident_id=uuid4(),
        run_id=uuid4(),
        owner=uuid4(),
        epoch=1,
        control_generation=3,
        global_suspension_generation=1,
        target_suspension_generation=2,
    )


def _trace_input(run_id: str):
    face = otel_demo_face(FakeClock(start=WINDOW.end))
    return face.input_for(
        run_id=run_id,
        question="Why is checkout erroring?",
        target_id=TARGET_ID,
        deadline=NOW + timedelta(minutes=10),
        model_requests=2,
    )


def _trace_executor(monkeypatch, opener: FakeOpener) -> ReadOnlyToolExecutor:
    store = FakeStore(_trace_lease(), deadline=NOW + timedelta(minutes=10))
    lease = store.lease
    clock = FakeClock(start=NOW)
    sink = RecordingSink()

    def build_opener(*handlers):
        return opener

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    config = OtelDemoConfig(
        prometheus_url=PROMETHEUS_URL, jaeger_url=JAEGER_URL, token=None
    )
    factory = otel_demo_executor_factory(
        store, evidence=sink, clock=clock, config=config
    )
    executor = factory(lease, _trace_input(str(lease.run_id)))
    assert isinstance(executor, ReadOnlyToolExecutor)
    return executor


def _trace_call(params: dict) -> ToolRequest:
    return ToolRequest(
        step_id="step-1",
        tool_index=0,
        tool_name=TRACES_TOOL,
        target_ref=TARGET_ID,
        params=params,
        window=WINDOW.as_json(),
    )


def _traces_description() -> str:
    (schema,) = [s for s in TOOL_SCHEMAS if s["function"]["name"] == TRACES_TOOL]
    return schema["function"]["description"]


def _make_span(
    span_id: str, process_id: str, operation: str, *, duration, start_us, tags
):
    return {
        "spanID": span_id,
        "operationName": operation,
        "startTime": start_us,
        "duration": duration,
        "processID": process_id,
        "tags": tags,
        "logs": [],
        "references": [],
    }


def _grouping_payload() -> bytes:
    """Five spans across two (service, operation) pairs, deliberately queried
    for the service that projects *first* in view content (the requested
    service always sorts first -- see ``project_traces``'s own span sort) so
    a naive "first-appearance-in-content" grouping and the contract-required
    "sorted by (service, operation)" grouping disagree, and only the latter
    passes: querying "payment" puts its one span first in ``content``, but
    "checkout" < "payment" alphabetically.

    All status tags are numeric-valued (``rpc.grpc.status_code``,
    ``http.status_code``), matching the contract's own literal example and
    sidestepping the reference script's string/bool tag-value formatting
    (an implementation detail of that script, not of the contract text).
    """
    start_us = int(WINDOW.start.timestamp() * 1_000_000) + 1_000_000
    spans = [
        _make_span(
            "s1",
            "pC",
            "op-a",
            duration=1000,
            start_us=start_us + 1,
            tags=[{"key": "rpc.grpc.status_code", "value": 0}],
        ),
        _make_span(
            "s2",
            "pC",
            "op-a",
            duration=3000,
            start_us=start_us + 2,
            tags=[{"key": "rpc.grpc.status_code", "value": 0}],
        ),
        _make_span("s3", "pC", "op-a", duration=500, start_us=start_us + 3, tags=[]),
        _make_span(
            "s4",
            "pC",
            "op-a",
            duration=2000,
            start_us=start_us + 4,
            tags=[{"key": "rpc.grpc.status_code", "value": 500}],
        ),
        _make_span(
            "s5",
            "pP",
            "op-b",
            duration=750,
            start_us=start_us + 5,
            tags=[{"key": "http.status_code", "value": 503}],
        ),
    ]
    trace = {
        "traceID": "t-group",
        "spans": spans,
        "processes": {
            "pC": {"serviceName": "checkout", "tags": []},
            "pP": {"serviceName": "payment", "tags": []},
        },
    }
    return json.dumps({"data": [trace]}).encode()


def test_sanity_the_synthetic_grouping_fixture_has_the_expected_rows_and_services(
    monkeypatch,
):
    """Green today: proves the fixture itself (not span_groups) projects the
    5 rows across the 2 services as intended."""
    opener = FakeOpener(routes={"/api/traces": _grouping_payload()})
    executor = _trace_executor(monkeypatch, opener)
    outcome = executor.execute(_trace_call({"service": "payment", "limit": 10}))
    assert outcome.status == "ok"
    rows = outcome.model_view["content"]
    assert len(rows) == 5
    assert sum(1 for r in rows if r["service"] == "checkout") == 4
    assert sum(1 for r in rows if r["service"] == "payment") == 1
    # The requested service ("payment") sorts first in content -- the
    # fixture's whole point (see _grouping_payload's docstring).
    assert rows[0]["service"] == "payment"


def test_c3_span_groups_are_grouped_by_service_operation_and_sorted_ascending(
    monkeypatch,
):
    opener = FakeOpener(routes={"/api/traces": _grouping_payload()})
    executor = _trace_executor(monkeypatch, opener)
    outcome = executor.execute(_trace_call({"service": "payment", "limit": 10}))
    view = outcome.model_view
    groups = view["span_groups"]
    pairs = [(g["service"], g["operation"]) for g in groups]
    assert pairs == [("checkout", "op-a"), ("payment", "op-b")], (
        "groups must be sorted by (service, operation), not by first "
        f"appearance in content: {pairs!r}"
    )
    checkout_group, payment_group = groups
    assert checkout_group["rows"] == 4
    assert checkout_group["error_rows"] == 1  # only the rpc.grpc.status_code=500 row
    assert checkout_group["duration_us_min"] == 500
    assert checkout_group["duration_us_max"] == 3000
    assert checkout_group["status"]["rpc.grpc.status_code=0"] == 2
    assert checkout_group["status"]["not_recorded"] == 1
    assert checkout_group["status"]["rpc.grpc.status_code=500"] == 1
    assert sum(checkout_group["status"].values()) == 4

    assert payment_group["rows"] == 1
    assert payment_group["error_rows"] == 1  # http.status_code=503
    assert payment_group["duration_us_min"] == payment_group["duration_us_max"] == 750
    assert payment_group["status"] == {"http.status_code=503": 1}


def test_c3_span_groups_summarize_exactly_the_rows_in_the_delivered_view(
    monkeypatch,
):
    """Was ``..._only_summarize_shown_rows_when_truncated`` (40 heavy spans cut
    to 28, the omitted lowest-duration rows must not leak into
    ``duration_us_min``). M1-01 A (v3) no longer drops rows for size, so the
    surviving intent is: ``span_groups`` summarizes exactly the rows in the
    view. 20 heavy spans (~19.9k real tokens, under the cap) with duration
    ``1000 + i`` are all delivered; the single group covers all 20 and its
    duration range is the full range."""
    start_us = int(WINDOW.start.timestamp() * 1_000_000) + 1_000_000

    def make_span(i: int) -> dict:
        return {
            "traceID": f"trace{i:04x}",
            "spanID": f"span{i:04x}",
            "operationName": "op-" + "x" * 140,
            "startTime": start_us + i,
            "duration": 1000 + i,
            "processID": "p1",
            "tags": [
                {"key": "error", "value": True},
                {"key": "otel.status_code", "value": "ERROR"},
            ],
            "logs": [
                {
                    "timestamp": start_us + i,
                    "fields": [
                        {"key": "exception.stacktrace", "value": "E" * 700},
                        {"key": "exception.message", "value": "M" * 700},
                        {"key": "otel.status_description", "value": "D" * 700},
                        {"key": "error.description", "value": "F" * 700},
                    ],
                }
            ],
            "references": [],
        }

    SPAN_COUNT = 20
    traces = [
        {
            "traceID": f"trace{i:04x}",
            "spans": [make_span(i)],
            "processes": {"p1": {"serviceName": "checkout", "tags": []}},
        }
        for i in range(SPAN_COUNT)
    ]
    synthetic_body = json.dumps({"data": traces}).encode()
    opener = FakeOpener(routes={"/api/traces": synthetic_body})
    executor = _trace_executor(monkeypatch, opener)
    outcome = executor.execute(
        _trace_call({"service": "checkout", "limit": SPAN_COUNT})
    )
    view = outcome.model_view
    assert outcome.status == "ok", view
    assert view["truncated"] is False and view["omitted_rows"] == 0
    assert view["spans_shown"] == len(view["content"]) == SPAN_COUNT

    groups = view["span_groups"]
    assert len(groups) == 1, "every synthetic span shares one (service, operation)"
    (group,) = groups
    assert group["rows"] == SPAN_COUNT
    assert group["duration_us_min"] == 1000
    assert group["duration_us_max"] == 1000 + SPAN_COUNT - 1


def _all_strings(value: object):
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _all_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _all_strings(v)


def test_c3_view_explains_span_groups_only_covers_shown_rows(monkeypatch):
    opener = FakeOpener(routes={"/api/traces": _grouping_payload()})
    executor = _trace_executor(monkeypatch, opener)
    outcome = executor.execute(_trace_call({"service": "payment", "limit": 10}))
    texts = list(_all_strings(outcome.model_view))
    assert any(
        "span_groups" in text.lower()
        and re.search(r"shown|displayed|visible", text.lower())
        for text in texts
    ), (
        "the view must state, in its own field text, that span_groups only covers shown rows"
    )


def test_c3_traces_tool_description_mentions_span_groups():
    assert "span_groups" in _traces_description()


def test_c3_span_groups_counts_toward_the_view_hash(monkeypatch):
    opener = FakeOpener(routes={"/api/traces": _grouping_payload()})
    executor = _trace_executor(monkeypatch, opener)
    outcome = executor.execute(_trace_call({"service": "payment", "limit": 10}))
    assert "span_groups" in outcome.model_view
    assert canonical_hash(outcome.model_view) == outcome.evidence.view_sha256, (
        "view_sha256 must be computed over the view that already carries "
        "span_groups, not a pre-addition snapshot"
    )


def test_c3_span_groups_present_and_internally_consistent_on_the_real_checkout_fixture(
    monkeypatch,
):
    """A second, non-synthetic data point (the same real Jaeger fixture other
    contract files already use) checking structural invariants that must
    hold regardless of the exact grouping: every shown row is accounted for
    exactly once, groups are sorted, and each group's own numbers are
    self-consistent."""
    opener = FakeOpener(routes={"/api/traces": CHECKOUT_TRACES})
    executor = _trace_executor(monkeypatch, opener)
    outcome = executor.execute(_trace_call({"service": "checkout", "limit": 10}))
    view = outcome.model_view
    groups = view["span_groups"]
    assert sum(g["rows"] for g in groups) == len(view["content"]) == 90
    pairs = [(g["service"], g["operation"]) for g in groups]
    assert pairs == sorted(pairs)
    for g in groups:
        assert sum(g["status"].values()) == g["rows"]
        assert 0 <= g["error_rows"] <= g["rows"]
        assert g["duration_us_min"] <= g["duration_us_max"]


def _distinct_operation_spans(count: int) -> list[dict]:
    start_us = int(WINDOW.start.timestamp() * 1_000_000) + 1_000_000
    return [
        {
            "traceID": "t-cardinality",
            "spanID": f"span{i:05d}",
            "operationName": f"op-{i:05d}",
            "startTime": start_us + i,
            "duration": 100 + i,
            "processID": "p1",
            "tags": [],
            "logs": [],
            "references": [],
        }
        for i in range(count)
    ]


def test_c3_span_groups_counts_toward_the_view_token_cap(monkeypatch):
    """Was ``..._counts_toward_the_view_byte_cap`` (260 spans, span_groups
    must fit the byte cap by dropping more rows). Surviving intent under
    M1-01 A (v3): span_groups counts toward ``MAX_VIEW_TOKENS`` too, not just
    the rows. 180 spans, each its own operation: the row array alone is under
    the cap (asserted below with the real counter, not assumed), but with its
    180 span_groups entries the whole view is over it, so the view is refused
    whole, reporting the count of the complete view."""
    ROW_COUNT = 180
    trace = {
        "traceID": "t-cardinality",
        "spans": _distinct_operation_spans(ROW_COUNT),
        "processes": {"p1": {"serviceName": "checkout", "tags": []}},
    }
    payload = {"data": [trace]}
    record, _start, _end = project_traces(
        payload,
        service="checkout",
        limit=ROW_COUNT,
        window=WINDOW,
        source_sha256="0" * 64,
        source_bytes=100,
    )
    rows = record["data"]["sampled_spans"]
    assert len(rows) == ROW_COUNT
    assert count_tokens(canonical(rows)) <= MAX_VIEW_TOKENS, (
        "test invariant: the rows alone fit the cap"
    )

    opener = FakeOpener(routes={"/api/traces": json.dumps(payload).encode()})
    executor = _trace_executor(monkeypatch, opener)
    outcome = executor.execute(_trace_call({"service": "checkout", "limit": ROW_COUNT}))
    view = outcome.model_view

    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE"), (
        "span_groups must count toward the cap: rows alone fit, the whole "
        f"view does not: {view!r}"
    )
    assert view["max_view_tokens"] == MAX_VIEW_TOKENS
    assert view["view_tokens"] > MAX_VIEW_TOKENS
    assert view["content"] is None and "span_groups" not in view


def test_c3_span_groups_does_not_disturb_the_generic_view_fields(monkeypatch):
    """Review follow-up (optional item): span_groups must be purely additive
    -- it must not overwrite or hide any of the generic (non-trace-specific)
    fields ``_record()`` already builds for every tool (``tool``, ``status``,
    ``adopted``, ``citable_as_fact``, ``content``, ``truncated``,
    ``omitted_rows``, ``result_count``/``returned_count``, ``evidence_id``)."""
    opener = FakeOpener(routes={"/api/traces": _grouping_payload()})
    executor = _trace_executor(monkeypatch, opener)
    outcome = executor.execute(_trace_call({"service": "payment", "limit": 10}))
    view = outcome.model_view
    assert view["tool"] == TRACES_TOOL
    assert view["status"] == "ok"
    assert view["adopted"] is True
    assert view["citable_as_fact"] is True
    assert isinstance(view["content"], list) and len(view["content"]) == 5
    assert view["truncated"] is False and view["omitted_rows"] == 0
    assert view["result_count"] == view["returned_count"] == 5
    assert isinstance(view["evidence_id"], str) and view["evidence_id"]
    # span_groups is its own, additively-present field -- not something that
    # replaced or hid any of the checks above.
    assert "span_groups" in view
