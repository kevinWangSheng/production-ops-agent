"""#456 acceptance: the forced-final retry feedback names what is wrong inside a
Markdown json fence, even when the fence sits inside prose.

External behaviour only (contract: the #456 pre-review decision table D1-D7
and C2's closed vocabulary). A real Run is driven through ``InvestigationLoop``
with a scripted model; the assertions read the bytes the model is sent in the
forced final round, the Run's outcome and its recorded revisions. Written
without the implementation: nothing here names a helper the fix may add, and
no assertion depends on the wording of a new layer prefix (D2) -- only on
facts the feedback must carry (the C2 reason code, the offending id, the inner
JSON error) and on byte equality where the contract demands it.

Case found in #432 probe a26613d9: the first report reply was a sentence plus a
```json fence holding a schema-valid report whose claims cited an
``operation_id`` (``<uuid>-t0``) instead of the delivered ``evidence_id``
(``<operation_id>:<uuid>``). The feedback named only the outer JSON parse
error, so the single retry fixed the fence and still failed the citation gate.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest

from opspilot.investigation.context import messages_hash, rebuild_transcript
from opspilot.investigation.limits import RunLimits
from opspilot.investigation.loop import DISCIPLINE_VARIANT, prompt_revision_versions
from opspilot.investigation.reports import validated_report
from tests.m1_investigation_support import (
    assemble,
    reply,
    report_from_transcript,
    report_json,
    tool_call,
)

WIDE = RunLimits(model_requests=120)
PREFIX = "The previous reply was not accepted as the final report (REPORT_INVALID)."
OUTER_PARSE = " JSON parse error: Expecting value: line 1 column 1 (char 0)."
SUFFIX = (
    " This is the forced final retry: return exactly one corrected json object "
    "in the report format already given, citing only evidence_id values already "
    "delivered in this transcript."
)
# The bytes main sends today for a reply that is not JSON at the outer layer
# and carries nothing wrong inside its fence (the #304 pinned value).
MAIN_PARSE_ONLY_FEEDBACK = PREFIX + OUTER_PARSE + SUFFIX
PROMPT_REVISION = "prompt-replay-candidate-bef70649a3a8"
PROSE = "I have sufficient evidence to report.\n\n"


# -- harness ----------------------------------------------------------------------


def _delivered_ids(call):
    """(evidence_id, operation_id) of every tool view delivered so far."""
    ids = []
    for message in call.messages:
        if message.get("role") == "tool":
            view = json.loads(message["content"])
            ids.append((view["evidence_id"], view["operation_id"]))
    return ids


def _tool_rounds(count):
    return [
        reply(
            tool_calls=[
                tool_call(
                    call_id=f"call-{i}",
                    arguments=f'{{"expr":"rate(http_errors[{i}m])"}}',
                )
            ],
            finish="tool_calls",
        )
        for i in range(1, count + 1)
    ]


def _run(first_report, final_reply, *, tools=1):
    """A Run with ``tools`` tool rounds, then ``first_report(call)`` as the
    first no-tool reply, then ``final_reply`` in the forced final round."""
    loop, request, model, _, store, _ = assemble(
        replies=[*_tool_rounds(tools), first_report, final_reply],
        budget_limit=12,
        model_requests=8,
    )
    request = replace(request, limits=WIDE)
    outcome = loop.run(request)
    return outcome, model, store, request


def _forced_final_feedback(model):
    """The retry-feedback user message of the forced final round (the middle
    one of FINAL_REPORT_INSTRUCTION / feedback / run coverage)."""
    final_call = model.calls[-1]
    tail = final_call.messages[-3:]
    assert tail[0]["content"].startswith("Collection is now CLOSED.")
    assert tail[2]["content"].startswith("Run coverage summary")
    return tail[1]["content"]


def _bad(call):
    return reply(content="not a report")


def _report_payload(evidence_ids_per_claim):
    payload = json.loads(report_json(evidence_id="placeholder"))
    claim = payload["claims"][0]
    payload["claims"] = [
        {**claim, "evidence_ids": list(ids)} for ids in evidence_ids_per_claim
    ]
    return payload


def _prose_fence(body, *, before=PROSE, after=""):
    return f"{before}```json\n{body}\n```{after}"


def _whole_fence(body):
    return f"```json\n{body}\n```"


def _citing_operation_id(wrap):
    def first(call):
        _, operation_id = _delivered_ids(call)[-1]
        return reply(content=wrap(report_json(evidence_id=operation_id)))

    return first


def _citing_evidence_id(wrap):
    def first(call):
        evidence_id, _ = _delivered_ids(call)[-1]
        return reply(content=wrap(report_json(evidence_id=evidence_id)))

    return first


def _detail(feedback):
    assert feedback.startswith(PREFIX) and feedback.endswith(SUFFIX)
    return feedback[len(PREFIX) : -len(SUFFIX)]


def _paired_runs(make_body, wrap):
    """Oracle: run the same report twice -- once wrapped, once unfenced (the
    path the citation gate already names, C2) -- and return both forced-final
    details with this Run's operation_id normalised to ``OP``."""
    details = []
    for wrapper in (wrap, lambda body: body):
        seen = {}

        def first(call, wrapper=wrapper, seen=seen):
            seen["op"] = _delivered_ids(call)[-1][1]
            return reply(content=wrapper(make_body(seen["op"])))

        _, model, _, _ = _run(first, _bad)
        details.append(_detail(_forced_final_feedback(model)).replace(seen["op"], "OP"))
    return details


# -- 1. the #432 case: prose + fence, claims cite an operation_id --------------------


@pytest.mark.parametrize(
    "wrap",
    [
        pytest.param(_prose_fence, id="prose-before-fence"),
        pytest.param(
            lambda b: _prose_fence(b, after="\n\nLet me know."), id="prose-around-fence"
        ),
        pytest.param(_whole_fence, id="whole-reply-fence"),
    ],
)
def test_operation_id_cited_inside_a_fence_is_named_as_unknown_evidence_id(wrap):
    seen = {}

    def first(call):
        _, operation_id = _delivered_ids(call)[-1]
        seen["operation_id"] = operation_id
        return reply(content=wrap(report_json(evidence_id=operation_id)))

    outcome, model, _, _ = _run(first, _bad)
    feedback = _forced_final_feedback(model)

    assert feedback.startswith(PREFIX + OUTER_PARSE)
    assert feedback.endswith(SUFFIX)
    assert "unknown_evidence_id" in feedback
    assert seen["operation_id"] in feedback
    # the citation item follows the outer parse error, inside the template
    assert feedback.index(OUTER_PARSE) < feedback.index("unknown_evidence_id")
    # acceptance unchanged: the Run still hands off on the second bad reply
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("REPORT_INVALID",)
    assert outcome.model_requests_used == 3


def test_fenced_citation_detail_matches_what_the_citation_gate_says_unfenced():
    """Same views, same report: the fenced feedback carries the very per-claim
    items the citation gate reports when the report arrives without a fence."""
    fenced, unfenced = _paired_runs(
        lambda op: report_json(evidence_id=op), _prose_fence
    )
    oracle = unfenced.strip()
    assert "unknown_evidence_id" in oracle and "OP" in oracle
    assert oracle in fenced


def test_every_failing_claim_is_named_and_the_valid_one_is_not():
    seen = {}

    def first(call):
        delivered = _delivered_ids(call)
        assert len(delivered) == 2
        (ev0, op0), (_, op1) = delivered
        seen.update(ev0=ev0, op0=op0, op1=op1)
        body = json.dumps(_report_payload([[op0], [ev0], [op1]]))
        return reply(content=_prose_fence(body))

    _, model, _, _ = _run(first, _bad, tools=2)
    feedback = _forced_final_feedback(model)
    assert seen["op0"] in feedback
    assert seen["op1"] in feedback
    assert feedback.count("unknown_evidence_id") >= 2
    # the correctly cited evidence_id is not reported as a failure
    assert seen["ev0"] not in feedback
    assert "claim 1" not in feedback


def test_more_than_fifty_failing_claims_in_a_fence_are_capped():
    def body(op):
        return json.dumps(_report_payload([[f"{op}-bogus-{i:03d}"] for i in range(60)]))

    fenced, unfenced = _paired_runs(body, _prose_fence)
    named = [i for i in range(60) if f"OP-bogus-{i:03d}" in fenced]
    assert 0 < len(named) <= 50
    # same cap and truncation note as the citation gate's own (C2)
    assert unfenced.strip() in fenced


# -- 2. schema and JSON errors inside a fence surrounded by prose --------------------


def test_prose_fence_with_broken_inner_json_names_the_inner_parse_error():
    inner = '{"schema_version": nope}'
    try:
        json.loads(inner)
    except ValueError as exc:
        inner_error = str(exc)
    else:  # pragma: no cover
        raise AssertionError("inner body must be broken JSON")
    assert inner_error != "Expecting value: line 1 column 1 (char 0)"

    outcome, model, _, _ = _run(lambda call: reply(content=_prose_fence(inner)), _bad)
    feedback = _forced_final_feedback(model)
    assert feedback.startswith(PREFIX + OUTER_PARSE)
    assert feedback.endswith(SUFFIX)
    assert inner_error in feedback
    assert outcome.handoff_reasons == ("REPORT_INVALID",)


def test_prose_fence_breaking_the_incomplete_rule_names_the_schema_layer():
    payload = json.loads(report_json(evidence_id="ev-1"))
    payload["assessment_status"] = "incomplete"
    payload["conclusion"] = "partial"
    body = json.dumps(payload)

    _, model, _, _ = _run(lambda call: reply(content=_prose_fence(body)), _bad)
    feedback = _forced_final_feedback(model)
    assert feedback.startswith(PREFIX + OUTER_PARSE)
    assert "INCOMPLETE_REPORT_CONTRACT" in feedback
    assert feedback.index(OUTER_PARSE) < feedback.index("INCOMPLETE_REPORT_CONTRACT")


# -- 3. nothing wrong inside the fence: feedback bytes identical to main ------------


@pytest.mark.parametrize(
    "wrap",
    [
        pytest.param(_prose_fence, id="prose-before-fence"),
        pytest.param(
            lambda b: _prose_fence(b, after="\nDone."), id="prose-around-fence"
        ),
        pytest.param(_whole_fence, id="whole-reply-fence"),
    ],
)
def test_fully_valid_fenced_report_leaves_the_feedback_bytes_unchanged(wrap):
    outcome, model, _, _ = _run(_citing_evidence_id(wrap), _bad)
    assert _forced_final_feedback(model) == MAIN_PARSE_ONLY_FEEDBACK
    assert outcome.handoff_reasons == ("REPORT_INVALID",)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("not a report", id="garbage"),
        pytest.param(
            PROSE + "```json\n" + report_json(evidence_id="x"), id="unclosed-fence"
        ),
        pytest.param(PROSE + "no fence here at all", id="prose-only"),
    ],
)
def test_replies_without_a_closed_fence_keep_the_main_feedback_bytes(content):
    _, model, _, _ = _run(lambda call: reply(content=content), _bad)
    assert _forced_final_feedback(model) == MAIN_PARSE_ONLY_FEEDBACK


# -- 4. accept / reject decisions unchanged --------------------------------------------


@pytest.mark.parametrize(
    "wrap",
    [pytest.param(_prose_fence, id="prose"), pytest.param(_whole_fence, id="whole")],
)
def test_a_valid_fenced_report_is_still_not_accepted(wrap):
    """A fenced reply is still REPORT_INVALID: the Run goes to the forced
    final round, and a correct unfenced reply there completes it."""
    outcome, model, _, _ = _run(_citing_evidence_id(wrap), report_from_transcript)
    assert outcome.model_requests_used == 3
    assert len(model.calls) == 3
    assert outcome.execution != "failed"
    assert outcome.handoff_reasons == ()
    assert outcome.report is not None


def test_retry_that_still_cites_an_operation_id_is_still_rejected():
    def final(call):
        _, operation_id = _delivered_ids(call)[-1]
        return reply(content=report_json(evidence_id=operation_id))

    outcome, _, _, _ = _run(_citing_operation_id(_prose_fence), final)
    assert outcome.execution == "failed"
    assert outcome.handoff_reasons == ("REPORT_INVALID",)
    assert outcome.report is None


def test_retry_that_fixes_the_citation_is_accepted():
    outcome, _, _, _ = _run(_citing_operation_id(_prose_fence), report_from_transcript)
    assert outcome.handoff_reasons == ()
    assert outcome.report is not None


# -- 5. revisions unchanged and the forced-final row still rebuilds --------------------


def test_prompt_revision_is_unchanged():
    assert prompt_revision_versions(DISCIPLINE_VARIANT) == {
        "prompt_revision": PROMPT_REVISION
    }
    outcome, _, _, _ = _run(_citing_operation_id(_prose_fence), _bad)
    assert outcome.prompt_revision == PROMPT_REVISION


def test_forced_final_row_with_the_new_feedback_rebuilds_byte_for_byte():
    """The richer feedback is a pure function of committed rows: the
    forced-final row's input hash covers it and the transcript rebuilds."""
    _, model, store, request = _run(_citing_operation_id(_prose_fence), _bad)
    final_call = model.calls[-1]
    assert "unknown_evidence_id" in _forced_final_feedback(model)
    rows = [
        step["response"]["context"]
        for key, step in store.steps.items()
        if key.startswith("ctx0:") and step["response"].get("context", {}).get("final")
    ]
    assert len(rows) == 1
    assert rows[0]["input_snapshot_hash"] == messages_hash(final_call.messages)
    rebuild_transcript(
        store.snapshot(),
        run_id=request.run_id,
        authorized_targets=request.scope.target_ids,
        input=request.as_input(),
    )


# -- 6. degenerate input stays linear (#453 lesson) --------------------------------


@pytest.mark.parametrize(
    "content",
    [
        PROSE + "```json\n" + "{" * 200_000,
        PROSE + "```" * 300_000,
        PROSE + "`" * 900_000,
        "x" * 500_000 + "```json\n{}\n```" + " " * 500_000,
        PROSE + "```json\n" + " " * 1_000_000,
    ],
)
def test_degenerate_prose_fence_input_stays_fast(content):
    started = time.perf_counter()
    validated_report(
        content,
        finish_reason="stop",
        delivered=(),
        evidence_context=None,
        authorized_targets=frozenset({"checkout-prod"}),
    )
    assert time.perf_counter() - started < 0.5
