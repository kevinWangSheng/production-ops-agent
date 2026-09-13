"""One bounded DeepSeek request around a persisted pause/resume control."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from scripts import m0_pg_live_probe as probe
from scripts.m0.budget import PostgresBudget
from scripts.m0.contracts import BudgetError, RunContext
from scripts.m0.postgres_lab import DSN
from scripts.m0.step_store import StepStore
from scripts.m0_environment import round02

LEDGER = Path("docs/evidence/m0-real-investigation/round-07-wp23-real-ledger.json")
RECORD = Path("docs/evidence/m0-real-investigation/round-07-wp23-real-run.json")
VERSION = {"state": "v3", "provider": "deepseek", "tool": "fixture-v1"}


def main():
    deadline = time.time() + 300
    round02.PROFILE = replace(
        round02.PROFILE,
        allocation="m0-10-wp23-20260912",
        deadline=deadline,
        reservation_cny=1.0,
    )
    probe.LEDGER = LEDGER
    ledger = PostgresBudget(DSN)
    ledger.install()
    store = StepStore(ledger)
    store.install()
    with ledger._transaction() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS m0_pg_wire_response (request uuid PRIMARY KEY REFERENCES m0_requests(id),status integer NOT NULL,body bytea NOT NULL,code text NOT NULL DEFAULT 'CAPTURED',captured_at timestamptz NOT NULL DEFAULT clock_timestamp())"
        )
    run = RunContext(
        uuid4(), uuid4(), "deepseek", datetime.fromtimestamp(deadline, timezone.utc)
    )
    ledger.initialize(run.experiment_id, 3_500_000, run.deadline)
    subject = store.accept(
        run,
        "wp23-pause-real",
        {"request": "pause real probe"},
        VERSION,
        target="checkout",
    )
    denied = None
    fence = store.claim(subject, run, uuid4(), VERSION, lease_seconds=60)
    store.pause("target", target="checkout", reason="round-07 control probe")
    try:
        store.dispatch(
            fence,
            "paused",
            0,
            {"messages": []},
            uuid4(),
            1_000_000,
            2,
            lambda: (_ for _ in ()).throw(AssertionError("paused starter called")),
        )
    except BudgetError as exc:
        denied = str(exc)
    store.resume("target", target="checkout")
    new_run = RunContext(run.experiment_id, uuid4(), "deepseek", run.deadline)
    generation = store.new_run(
        subject, 1, new_run, {"request": "pause real probe resumed"}, VERSION
    )
    new_fence = store.claim(subject, new_run, uuid4(), VERSION, lease_seconds=60)
    fixture = json.loads(Path("tests/fixtures/m0/protocol-v1.json").read_text())
    body = {
        "model": "deepseek-v4-flash",
        "thinking": {"type": "enabled"},
        "reasoning_effort": "high",
        "max_tokens": 32768,
        "messages": fixture["messages"],
        "tools": fixture["tools"],
    }
    request_id = uuid4()
    step = store.prepare_request(
        new_fence, "resumed", 0, body, request_id, 1_000_000, 2
    )
    child = probe.start_transport(
        json.dumps(body, separators=(",", ":")).encode(),
        new_run.run_id,
        request_id,
        deadline,
        fence=new_fence,
    )
    status, payload = probe.wait_transport(child, deadline, store, new_fence)
    with ledger._transaction() as conn:
        conn.execute(
            "INSERT INTO m0_pg_wire_response(request,status,body) VALUES(%s,%s,%s)",
            (request_id, status, payload),
        )
    reply = json.loads(payload)
    assistant = reply["choices"][0]["message"]
    accepted = store.commit_response(new_fence, step, assistant, request_id=request_id)
    probe.finish_global(request_id, status, reply.get("usage"), deadline)
    result = {
        "run_id": str(run.run_id),
        "new_run_id": str(new_run.run_id),
        "subject_id": str(subject),
        "pause_denied_code": denied,
        "new_generation": generation,
        "model_http": 1,
        "http_status": status,
        "response_model": reply.get("model"),
        "response_accepted": accepted,
        "state": store.summary(subject)["state"],
        "audit": store.summary(subject)["audit"],
    }
    RECORD.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "pause_denied_code",
                    "new_generation",
                    "model_http",
                    "http_status",
                    "response_accepted",
                )
            }
        )
    )


if __name__ == "__main__":
    main()
