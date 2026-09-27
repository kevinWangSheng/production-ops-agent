"""Rebuild the final-report request of each 6f Run through the product's own path.

usage: ABLATION_WORK=<work dir> .venv/bin/python rebuild_final_requests.py <intake_key>...

For every intake key (``intake:6f-normal-1`` ...) the script reads the durable
rows with ``DurableStore.rebuild`` (lab PostgreSQL on 55431), drops the final
round and the conclusion row from the snapshot, rebuilds the transcript with
``rebuild_transcript`` and re-adds the two trailing user messages exactly as
``InvestigationLoop._final_messages`` does (``FINAL_REPORT_INSTRUCTION`` then
``run_coverage_message`` over the delivered views). The resulting messages
must hash (``messages_hash``) to the final step's recorded
``context.input_snapshot_hash``; a mismatch stops the script.

Outputs, per Run, into ``$ABLATION_WORK/requests/<case>/``:

- ``messages.json``      the exact messages (contain ``reasoning_content``,
                          a private protocol field: never copied to evidence)
- ``params.json``        model, json_mode, max_tokens, tools, thinking, effort
- ``validation.json``    what ``_validated_report`` needs: delivered views,
                          authorized targets, time policy ids, target catalog
- ``original_report.txt`` the report the live Run produced (for reference)

and a small ``manifest.json`` (case, run id, hashes, sizes) that is also
copied to the evidence directory when ``ABLATION_EVIDENCE`` is set.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from opspilot.investigation.context import (
    Transcript,
    inputs_message,
    messages_hash,
    rebuild_transcript,
)
from opspilot.investigation.loop import ModelCall, serialized_request
from opspilot.investigation.reports import (
    FINAL_REPORT_INSTRUCTION,
    context_target_catalog,
    context_time_policy_ids,
    run_coverage_message,
)
from opspilot.persistence import DurableStore

DSN = "host=127.0.0.1 port=55431 dbname=m0_budget user=m0_lab"


def final_step(steps: list[dict]) -> dict:
    finals = [
        s
        for s in steps
        if isinstance(s.get("response"), dict)
        and s["response"].get("kind") is None
        and isinstance(s["response"].get("context"), dict)
        and s["response"]["context"].get("final") is True
    ]
    if len(finals) != 1:
        raise SystemExit(f"expected exactly one final step, found {len(finals)}")
    return finals[0]


def rebuild_case(store: DurableStore, intake_key: str) -> tuple[str, dict, Transcript, dict]:
    with store.transaction(snapshot=True) as conn:
        row = conn.execute(
            "SELECT incident_id FROM opspilot_incidents WHERE intake_key=%s",
            (intake_key,),
        ).fetchone()
    if row is None:
        raise SystemExit(f"unknown intake key {intake_key}")
    snapshot = store.rebuild(row["incident_id"])
    run = snapshot["run"]
    run_id = str(run["run_id"])
    final = final_step(snapshot["steps"])
    before = [s for s in snapshot["steps"] if s["sequence"] < final["sequence"]]
    trimmed = {**snapshot, "steps": before, "conclusion": None}
    targets = frozenset(run["input"]["scope_facts"]["target_ids"])
    transcript = rebuild_transcript(trimmed, run_id=run_id, authorized_targets=targets)
    sent = list(transcript.messages)
    watermark = final["response"]["context"].get("input_watermark")
    if isinstance(watermark, int) and watermark > 0:
        trailing = inputs_message(snapshot["inputs"], watermark=watermark)
        if trailing is not None:
            sent.append(trailing)
    sent.append({"role": "user", "content": FINAL_REPORT_INSTRUCTION})
    sent.append({"role": "user", "content": run_coverage_message(transcript.delivered)})
    recorded = final["response"]["context"]["input_snapshot_hash"]
    got = messages_hash(sent)
    if got != recorded:
        raise SystemExit(f"{intake_key}: rebuilt hash {got} != recorded {recorded}")
    call = ModelCall(
        messages=tuple(sent),
        tools=None,
        json_mode=True,
        max_tokens=transcript.input.limits.output_tokens,
        timeout_seconds=transcript.input.limits.model_request_timeout_seconds,
        model=str(final["response"]["response_model"]),
    )
    body = serialized_request(call)
    request_sha = hashlib.sha256(body).hexdigest()
    recorded_request_sha = final["response"].get("request_sha256")
    return run_id, final, transcript, {
        "messages": sent,
        "params": {
            "model": call.model,
            "json_mode": call.json_mode,
            "response_format": {"type": "json_object"},
            "max_tokens": call.max_tokens,
            "tools": None,
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
            "stream": False,
            "timeout_seconds": call.timeout_seconds,
            "request_bytes_limit": transcript.input.limits.request_bytes,
        },
        "hashes": {
            "input_snapshot_hash": got,
            "recorded_input_snapshot_hash": recorded,
            "request_sha256": request_sha,
            "recorded_request_sha256": recorded_request_sha,
            "request_bytes": len(body),
        },
        "validation": {
            "delivered": [
                {**asdict(v), "target_ids": sorted(v.target_ids),
                 "time_scope_refs": sorted(v.time_scope_refs)}
                for v in transcript.delivered
            ],
            "authorized_targets": sorted(targets),
            "time_policy_ids": list(context_time_policy_ids(transcript.evidence_context)),
            "target_catalog": context_target_catalog(
                transcript.evidence_context, authorized_targets=targets
            ),
        },
    }


def main() -> None:
    work = Path(os.environ["ABLATION_WORK"]) / "requests"
    evidence = os.environ.get("ABLATION_EVIDENCE")
    store = DurableStore(DSN)
    manifest = []
    for key in sys.argv[1:]:
        case = key.split(":", 1)[1].removeprefix("6f-")
        run_id, final, transcript, built = rebuild_case(store, key)
        out = work / case
        out.mkdir(parents=True, exist_ok=True)
        (out / "messages.json").write_text(json.dumps(built["messages"], ensure_ascii=False))
        (out / "params.json").write_text(json.dumps(built["params"], indent=1))
        (out / "validation.json").write_text(json.dumps(built["validation"], indent=1))
        original = final["response"]["assistant"].get("content") or ""
        (out / "original_report.txt").write_text(original)
        tool_msgs = [m for m in built["messages"] if m.get("role") == "tool"]
        entry = {
            "case": case,
            "intake_key": key,
            "run_id": run_id,
            "final_logical_key": final["logical_key"],
            "final_finish_reason": final["response"].get("finish_reason"),
            "message_count": len(built["messages"]),
            "tool_result_messages": len(tool_msgs),
            "delivered_views": len(transcript.delivered),
            "original_report_sha256": hashlib.sha256(original.encode()).hexdigest(),
            "original_usage": final["response"].get("usage"),
            **built["hashes"],
        }
        manifest.append(entry)
        print(json.dumps({k: v for k, v in entry.items() if k != "original_usage"}))
    (work / "manifest.json").write_text(json.dumps(manifest, indent=1))
    if evidence:
        Path(evidence).mkdir(parents=True, exist_ok=True)
        (Path(evidence) / "rebuild-manifest.json").write_text(json.dumps(manifest, indent=1))


if __name__ == "__main__":
    main()
