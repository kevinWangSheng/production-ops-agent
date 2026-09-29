"""Rebuild the report-producing request of a Run from its ``ledger.json`` alone.

usage: OUT=<dir> .venv/bin/python rebuild_offline.py <ledger.json> <case> ...

No PostgreSQL. ``ledger.json`` is the table export (runs / steps / inputs /
incident); it holds every field ``rebuild_transcript`` reads EXCEPT the
private ``reasoning_content`` of earlier assistant turns, which the export
replaces with a fixed placeholder (C3 section 12). Consequences, all checked
by ``fidelity`` below and reported in the README:

* ``rebuild_transcript``'s per-step ``input_snapshot_hash`` check cannot pass
  (the hash covers reasoning_content), so it is disabled here. The check for
  round 1 (no earlier reasoning) still passes and is asserted.
* every other message byte is produced by the product's own code path.

Two request shapes exist in the third batch:

* ``forced_final``: the last step has ``context.final`` (a C2 retry). Messages
  = rebuilt transcript (incl. the rejected report turn) + report instruction
  + the retry feedback + run coverage message; JSON mode, no tools.
* ``self_terminated``: the last step is a non-final round whose reply had no
  tool calls and was accepted as the report. Messages = rebuilt transcript;
  tools attached, JSON mode off.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from opspilot.investigation import context as ctx
from opspilot.investigation.context import (
    inputs_message,
    messages_hash,
    rebuild_transcript,
)
from opspilot.investigation.loop import ModelCall, serialized_request
from opspilot.investigation.reports import (
    FINAL_REPORT_INSTRUCTION,
    context_target_catalog,
    context_time_policy_ids,
    parse_report,
    report_retry_feedback,
    run_coverage_message,
    unsupported_citations,
)

PLACEHOLDER_PREFIX = "[reasoning_content withheld"


def snapshot_from_ledger(ledger: dict) -> dict:
    run = ledger["runs"][0]
    incident = ledger["incident"][0]
    steps = [{**s, "step_id": UUID(s["step_id"])} for s in ledger["steps"]]
    return {
        "inputs": ledger["inputs"],
        "input_rounds": [],
        "incident_id": incident["incident_id"],
        "state": incident["state"],
        "control_generation": incident["control_generation"],
        "run": run,
        "steps": steps,
        "conclusion": incident["conclusion"],
    }


def _retry_feedback(rejected_content: str, transcript, targets) -> str:
    """Mirror InvestigationLoop._validated_report for the rejected turn."""
    catalog = context_target_catalog(
        transcript.evidence_context, authorized_targets=targets
    )
    policy = context_time_policy_ids(transcript.evidence_context)
    report, reason = parse_report(rejected_content, finish_reason="stop")
    if report is None:
        return report_retry_feedback(
            reason,
            report=None,
            views=transcript.delivered,
            content=rejected_content,
            authorized_targets=targets,
            time_policy_ids=policy,
            target_catalog=catalog,
        )
    if not unsupported_citations(
        report,
        views=transcript.delivered,
        authorized_targets=targets,
        time_policy_ids=policy,
        target_catalog=catalog,
    ):
        raise SystemExit("rejected turn is actually valid; shape mismatch")
    return report_retry_feedback(
        "REPORT_INVALID",
        report=report,
        views=transcript.delivered,
        authorized_targets=targets,
        time_policy_ids=policy,
        target_catalog=catalog,
    )


def rebuild(ledger_path: str, *, upto: int | None = None) -> dict:
    """Request of the report-producing step (the last non-conclusion step)."""
    ledger = json.load(open(ledger_path))
    snap = snapshot_from_ledger(ledger)
    run = snap["run"]
    steps = [s for s in snap["steps"] if s["response"].get("kind") is None]
    last = steps[-1] if upto is None else steps[upto]
    before = [s for s in snap["steps"] if s["sequence"] < last["sequence"]]
    targets = frozenset(run["input"]["scope_facts"]["target_ids"])
    # Round 1 has no earlier reasoning_content, so its recorded
    # input_snapshot_hash is still checkable: rebuild the first step with the
    # product's own check enabled. A mismatch raises ContextError and stops.
    rebuild_transcript(
        {**snap, "steps": before[:1], "conclusion": None},
        run_id=str(run["run_id"]),
        authorized_targets=targets,
    )
    # From round 2 on the hash covers reasoning_content that the export
    # replaced with a placeholder, so the check cannot pass; it is disabled
    # for the rebuild below (README section 1 gives the token-count check
    # that stands in for it).
    orig = ctx._check_snapshot_hash
    ctx._check_snapshot_hash = lambda *a, **k: None
    try:
        transcript = rebuild_transcript(
            {**snap, "steps": before, "conclusion": None},
            run_id=str(run["run_id"]),
            authorized_targets=targets,
        )
    finally:
        ctx._check_snapshot_hash = orig
    sent = list(transcript.messages)
    context = last["response"]["context"]
    watermark = context.get("input_watermark")
    if isinstance(watermark, int) and watermark > 0:
        trailing = inputs_message(snap["inputs"], watermark=watermark)
        if trailing is not None:
            sent.append(trailing)
    final = context.get("final") is True
    feedback = ""
    if final:
        prev = before[-1]["response"]
        rejected = (prev.get("assistant") or {}).get("content") or ""
        feedback = _retry_feedback(rejected, transcript, targets)
        sent.append({"role": "user", "content": FINAL_REPORT_INSTRUCTION})
        if feedback:
            sent.append({"role": "user", "content": feedback})
        sent.append(
            {"role": "user", "content": run_coverage_message(transcript.delivered)}
        )
    tools = None if final else tuple(run["input"]["tool_schemas"])
    call = ModelCall(
        messages=tuple(sent),
        tools=tools,
        json_mode=final,
        max_tokens=transcript.input.limits.output_tokens,
        timeout_seconds=transcript.input.limits.model_request_timeout_seconds,
        model=str(last["response"]["response_model"]),
    )
    body = serialized_request(call)
    earlier_reasoning = sum(
        int(
            (s["response"].get("usage") or {})
            .get("completion_tokens_details", {})
            .get("reasoning_tokens", 0)
        )
        for s in before
    )
    return {
        "mode": "forced_final" if final else "self_terminated",
        "call": call,
        "last": last,
        "transcript": transcript,
        "feedback": feedback,
        "info": {
            "round1_input_snapshot_hash_verified": True,
            "last_logical_key": last["logical_key"],
            "recorded_prompt_tokens": last["response"]["usage"]["prompt_tokens"],
            "recorded_completion_tokens": last["response"]["usage"][
                "completion_tokens"
            ],
            "earlier_reasoning_tokens": earlier_reasoning,
            "recorded_input_snapshot_hash": context.get("input_snapshot_hash"),
            "rebuilt_hash_with_placeholder_reasoning": messages_hash(sent),
            "request_bytes": len(body),
            "request_sha256": hashlib.sha256(body).hexdigest(),
            "recorded_request_sha256": last["response"].get("request_sha256"),
            "message_count": len(sent),
            "placeholder_reasoning_messages": sum(
                1
                for m in sent
                if str(m.get("reasoning_content", "")).startswith(PLACEHOLDER_PREFIX)
            ),
        },
    }


def main() -> None:
    out = Path(os.environ["OUT"])
    args = sys.argv[1:]
    manifest = []
    for path, case in zip(args[0::2], args[1::2]):
        # ``case@k`` rebuilds the request of the k-th (0-based) report-kind
        # step instead of the last one, e.g. ``fault-1@3`` = the rejected
        # round-4 request whose reply the forced-final retry then repeats.
        name, _, upto = case.partition("@")
        r = rebuild(path, upto=int(upto) if upto else None)
        d = out / case.replace("@", "-at")
        d.mkdir(parents=True, exist_ok=True)
        (d / "messages.json").write_text(
            json.dumps(list(r["call"].messages), ensure_ascii=False)
        )
        (d / "tools.json").write_text(json.dumps(r["call"].tools))
        (d / "params.json").write_text(
            json.dumps(
                {
                    "model": r["call"].model,
                    "json_mode": r["call"].json_mode,
                    "max_tokens": r["call"].max_tokens,
                    "timeout_seconds": r["call"].timeout_seconds,
                    "mode": r["mode"],
                },
                indent=1,
            )
        )
        (d / "feedback.txt").write_text(r["feedback"])
        entry = {"case": case, "ledger": path, "mode": r["mode"], **r["info"]}
        manifest.append(entry)
        print(json.dumps(entry))
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))


if __name__ == "__main__":
    main()
