"""#242: the live runner's frozen compaction fields and its offline PG rebuild.

A scripted model drives the product loop into one accepted compaction over the
in-memory store, whose ``snapshot()`` has the ``DurableStore.rebuild`` shape;
the runner helpers then read those rows the way they read PostgreSQL.
"""

from __future__ import annotations

from dataclasses import replace

from opspilot.investigation import context as ctx
from opspilot.investigation.context import estimate_tokens
from opspilot.investigation.limits import RunLimits
from scripts.lab_evidence import parse_report
from scripts.m1_live_runner import (
    citation_provenance,
    compaction_record,
    compaction_summary,
    offline_rebuild,
)
from tests.m1_investigation_support import (
    TOOL_SCHEMAS,
    ScriptedModel,
    assemble,
    reply,
    report_json,
    tool_call,
)


def _rounds(count):
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


def _limits_compacting_after_two_rounds():
    # Same calibration as tests/test_m1_investigation_compaction.py: the
    # threshold falls between the call carrying one and two tool groups.
    loop, request, model, _, _, _ = assemble(
        replies=_rounds(3), budget_limit=16, model_requests=5
    )
    loop.run(replace(request, limits=RunLimits(model_requests=16)))
    tools = tuple(TOOL_SCHEMAS)
    before = estimate_tokens(model.calls[1].messages, tools)
    after = estimate_tokens(model.calls[2].messages, tools)
    budget = int((before + after) / 2 / ctx.CONTEXT_POLICY.compaction_pct)
    return RunLimits(model_requests=16, context_tokens=budget + 64, output_tokens=64)


def _run(*, compact=True):
    loop, request, _, _, store, _ = assemble(
        replies=[], budget_limit=16, model_requests=8
    )
    if compact:
        request = replace(request, limits=_limits_compacting_after_two_rounds())
    reports = []

    def cite_first(call):
        first = next(iter(store.tool_results.values()))[0]["result"]["evidence_id"]
        reports.append(report_json(evidence_id=first))
        return reply(content=reports[-1])

    summary = reply(content="Facts: checkout errors. Next: final report.")
    loop.model = ScriptedModel(
        [*_rounds(2), *([summary] if compact else []), cite_first]
    )
    outcome = loop.run(request)
    assert outcome.execution == "completed", outcome.handoff_reasons
    target = next(iter(request.scope.target_ids))
    snapshot = store.snapshot()
    # ``DurableStore.accept`` records the input on the run row; the memory
    # store does not, so the snapshot gets the one the loop ran on.
    snapshot["run"]["input"] = request.as_input().as_json()
    return snapshot, target, parse_report(reports[-1])


def test_an_accepted_compaction_is_counted_and_described_per_row():
    snapshot, _, _ = _run()
    record = compaction_record(snapshot)
    assert record["attempted"] == 1 and record["accepted"] == 1
    (row,) = record["rows"]
    assert row["accepted"] is True
    assert (row["from_segment"], row["to_segment"]) == ("ctx0", "ctx1")
    assert row["folded_step_ids"] == 2
    assert record["step_keys"][:3] == [
        "ctx0:round-1",
        "ctx0:round-2",
        row["logical_key"],
    ]
    assert record["rounds_after_last_accepted"] >= 1
    assert any(key.startswith("ctx1:round-") for key in record["step_keys"])


def test_the_offline_rebuild_crosses_the_compaction_row():
    snapshot, target, _ = _run()
    rebuilt = offline_rebuild(snapshot, target)
    assert rebuilt["error"] is None
    assert rebuilt["segment"] == "ctx1"
    assert rebuilt["compactions"] == compaction_record(snapshot)["accepted"]
    assert rebuilt["compaction_attempts"] == 1


def test_a_tampered_compaction_row_is_recorded_as_a_failed_rebuild():
    snapshot, target, _ = _run()
    for step in snapshot["steps"]:
        record = step["response"].get("compaction")
        if record is not None:
            record["folded_step_ids"] = record["folded_step_ids"][:1]
    rebuilt = offline_rebuild(snapshot, target)
    assert rebuilt == {
        "segment": None,
        "compactions": None,
        "error": "INCOMPATIBLE_STATE",
    }
    summary = compaction_summary(snapshot, target, None)
    assert summary["offline_rebuild"]["error"] == "INCOMPATIBLE_STATE"


def test_a_citation_from_a_folded_round_is_traced_and_still_delivered():
    snapshot, target, report = _run()
    provenance = citation_provenance(
        report, snapshot, offline_rebuild(snapshot, target)
    )
    (entry,) = provenance["cited"]
    assert entry["segment"] == "ctx0"
    assert entry["folded_by_accepted_compaction"] is True
    assert entry["delivered_after_rebuild"] is True
    assert provenance["cited_from_folded_rounds"] == 1
    assert provenance["cited_not_delivered"] == []


def test_a_run_without_compaction_reports_zero_and_stays_on_ctx0():
    snapshot, target, report = _run(compact=False)
    record = compaction_record(snapshot)
    assert (record["attempted"], record["accepted"]) == (0, 0)
    assert record["rounds_after_last_accepted"] == 0
    rebuilt = offline_rebuild(snapshot, target)
    assert rebuilt["error"] is None and rebuilt["segment"] == "ctx0"
    provenance = citation_provenance(report, snapshot, rebuilt)
    assert provenance["cited_from_folded_rounds"] == 0


def test_no_parsed_report_means_no_citation_block():
    snapshot, target, _ = _run()
    rebuilt = offline_rebuild(snapshot, target)
    assert citation_provenance(None, snapshot, rebuilt) is None
    assert citation_provenance("not json", snapshot, rebuilt) is None


def test_the_summary_fields_carry_the_agreed_keys():
    snapshot, target, report = _run()
    summary = compaction_summary(snapshot, target, report)
    assert summary["compactions"] == {"accepted": 1, "attempted": 1}
    assert summary["offline_rebuild"] == {
        "segment": "ctx1",
        "compactions": 1,
        "error": None,
    }
    (row,) = summary["compaction_rows"]["rows"]
    assert row["to_segment"] == summary["offline_rebuild"]["segment"]
    assert summary["offline_rebuild_detail"]["compaction_attempts"] == 1
    assert summary["citations"]["cited_from_folded_rounds"] == 1
