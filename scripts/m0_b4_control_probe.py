"""Real-PG control probes for B4 cancellation and incompatible handoff."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from scripts import m0_pg_live_probe as probe
from scripts.m0.contracts import BudgetError, RunContext
from scripts.m0.postgres_lab import DSN
from scripts.m0.step_store import PostgresBudget, StepStore
from scripts.m0_environment import round02


def profile(deadline):
    return replace(
        round02.PROFILE,
        allocation="m0-05-b4-20260912",
        deadline=deadline,
        reservation_cny=1.0,
    )


def setup(deadline, ledger_path):
    round02.PROFILE = profile(deadline)
    probe.LEDGER = ledger_path
    ledger = PostgresBudget(DSN)
    ledger.install()
    store = StepStore(ledger)
    store.install()
    with ledger._transaction() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS m0_pg_wire_response (request uuid PRIMARY KEY REFERENCES m0_requests(id),status integer NOT NULL,body bytea NOT NULL,code text NOT NULL DEFAULT 'CAPTURED',captured_at timestamptz NOT NULL DEFAULT clock_timestamp())"
        )
    return store, ledger


def fixture_body():
    fixture = json.loads(
        (
            Path(__file__).resolve().parents[1] / "tests/fixtures/m0/protocol-v1.json"
        ).read_text()
    )
    return {
        "model": "deepseek-v4-flash",
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
        "max_tokens": 32768,
        "messages": fixture["messages"],
        "tools": fixture["tools"],
    }


def cancel(deadline, ledger_path):
    store, ledger = setup(deadline, ledger_path)
    run = RunContext(
        uuid4(), uuid4(), "deepseek", datetime.fromtimestamp(deadline, timezone.utc)
    )
    ledger.initialize(run.experiment_id, 3_500_000, run.deadline)
    subject = store.accept(
        run, "b4-cancel", {"request": "cancel probe"}, {"state": "v3"}
    )
    fence = store.claim(subject, run, uuid4(), {"state": "v3"}, lease_seconds=60)
    request_id = uuid4()
    step = store.prepare_request(
        fence, "b4-cancel", 0, fixture_body(), request_id, 1_000_000, 4
    )
    child = probe.start_transport(
        json.dumps(fixture_body(), separators=(",", ":")).encode(),
        run.run_id,
        request_id,
        deadline,
        fence=fence,
    )
    generation = store.control(subject, 0, "cancel")
    status, payload = probe.wait_transport(child, deadline, store, fence)
    usage = None
    try:
        response = json.loads(payload)
        usage = response.get("usage")
        assistant = response["choices"][0]["message"]
    except (ValueError, KeyError, TypeError):
        assistant = None
    accepted = False
    if assistant is not None:
        try:
            accepted = store.commit_response(
                fence, step, assistant, request_id=request_id
            )
        except BudgetError:
            accepted = False
    probe.finish_global(request_id, "cancelled_after_send", usage, deadline)
    return {
        "run_id": str(run.run_id),
        "subject_id": str(subject),
        "model_http": 1,
        "http_status": status,
        "cancel_generation": generation,
        "late_response_accepted": accepted,
        "state": store.summary(subject)["state"],
    }


def incompatible(deadline, ledger_path):
    store, ledger = setup(deadline, ledger_path)
    run = RunContext(
        uuid4(), uuid4(), "deepseek", datetime.fromtimestamp(deadline, timezone.utc)
    )
    ledger.initialize(run.experiment_id, 3_500_000, run.deadline)
    versions = {"state": "v3", "provider": "deepseek", "tool": "fixture-v1"}
    subject = store.accept(
        run, "b4-incompatible", {"request": "version probe"}, versions
    )
    fence = store.claim(subject, run, uuid4(), versions, lease_seconds=1)
    step = store.prepare_request(
        fence, "b4-incompatible", 0, {"messages": []}, uuid4(), 1_000_000, 4
    )
    time.sleep(1.2)  # expire the original lease before the incompatible claim
    bad = dict(versions, tool="fixture-v2")
    code = None
    try:
        store.claim(subject, run, uuid4(), bad, lease_seconds=60)
    except BudgetError as exc:
        code = str(exc)
    summary = store.summary(subject)
    return {
        "run_id": str(run.run_id),
        "subject_id": str(subject),
        "model_http": 0,
        "prepared_step": str(step),
        "incompatible_code": code,
        "state": summary["state"],
        "audit": summary["audit"],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("cancel", "incompatible"))
    parser.add_argument("--deadline", type=float, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    args = parser.parse_args()
    result = (
        cancel(args.deadline, args.ledger)
        if args.mode == "cancel"
        else incompatible(args.deadline, args.ledger)
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
