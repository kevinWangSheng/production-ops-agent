"""Two explicit, supervised fixture/PG stages. No active investigation claims.

Only the parent operator invokes --execute. Private transport uses bounded pipes;
the child must signal actual body transmission before dispatch releases control.
"""

import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import selectors
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from scripts.m0.budget import PostgresBudget
from scripts.m0.contracts import BudgetError, RequestIdentity, RunContext
from scripts.m0.postgres_lab import DSN
from scripts.m0.protocol import tool_result
from scripts.m0.step_store import StepStore, digest
from scripts.m0_environment import round02

ROOT = Path(__file__).resolve().parents[1]
ENV_ROOT = Path("/Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment")

LEDGER = ENV_ROOT / "tmp/m0-environment/m0-02-request-ledger.json"
VERSIONS = {
    "state": "m0-step-v3",
    "adapter": "pg-private-pipe-v2-content-normalization",
    "tool": "read_fixture-v1",
    "model": "deepseek-v4-flash",
}


class ProbeFailure(ValueError):
    def __init__(self, code, http_status=None):
        super().__init__(code)
        self.http_status = http_status


def reject_response(store, request_id, code, status):
    with store.ledger._transaction() as conn:
        conn.execute(
            "UPDATE m0_pg_wire_response SET code=%s WHERE request=%s",
            (code, request_id),
        )
    raise ProbeFailure(code, status)


def wire_history(messages):
    """Official V4 wire compatibility only; persisted protocol remains intact."""
    copied = copy.deepcopy(messages)
    normalized = False
    for message in copied:
        if (
            message.get("role") == "assistant"
            and message.get("tool_calls")
            and message.get("content") is None
        ):
            message["content"] = ""
            normalized = True
    return copied, normalized


def reap(child):
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)
    for pipe in (child.stdin, child.stdout):
        if pipe and not pipe.closed:
            pipe.close()


def reserve_global(run_id, request_id, size, deadline):
    with LEDGER.with_suffix(".lock").open("a") as lock:
        while True:
            if time.time() >= deadline:
                raise TimeoutError("LEDGER_LOCK_TIMEOUT")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.05)
        budget = round02.Budget(LEDGER)
        entry = budget.reserve(str(run_id), "pg", size)
        entry["pg_request_id"] = str(request_id)
        round02.save(LEDGER, budget.data)


def finish_global(request_id, status, usage, deadline):
    with LEDGER.with_suffix(".lock").open("a") as lock:
        while True:
            if time.time() >= deadline:
                raise TimeoutError("LEDGER_LOCK_TIMEOUT")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(0.05)
        budget = round02.Budget(LEDGER)
        entries = [
            e
            for e in budget.data["attempts"]
            if e.get("pg_request_id") == str(request_id)
        ]
        if len(entries) != 1:
            raise ValueError("PG_LEDGER_IDENTITY")
        budget.finish(entries[0], status, usage)
        return entries[0].get("cost_upper_cny")


def start_transport(raw, run_id, request_id, deadline, *, fence, handshake_seconds=10):
    if not 0 < handshake_seconds <= 10:
        raise ValueError("HANDSHAKE_LIMIT")
    round02.envelope_check(raw)
    handshake_deadline = min(deadline, time.time() + handshake_seconds)
    reserve_global(run_id, request_id, len(raw), handshake_deadline)
    read_fd, write_fd = os.pipe()
    child = None
    try:
        timeout = min(360, deadline - time.time())
        if timeout <= 0:
            raise TimeoutError
        child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "scripts.m0_pg_private_transport",
                str(write_fd),
                str(deadline),
                json.dumps(
                    {
                        "experiment": str(fence.run.experiment_id),
                        "run": str(fence.run.run_id),
                        "run_deadline": fence.run.deadline.isoformat(),
                        "subject": str(fence.subject),
                        "owner": str(fence.owner),
                        "generation": fence.generation,
                        "epoch": fence.epoch,
                        "request": str(request_id),
                    }
                ),
            ],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            pass_fds=(write_fd,),
        )
        os.close(write_fd)
        write_fd = -1
        os.set_blocking(child.stdin.fileno(), False)
        sent = 0
        with selectors.DefaultSelector() as selector:
            selector.register(read_fd, selectors.EVENT_READ, "signal")
            selector.register(child.stdin, selectors.EVENT_WRITE, "input")
            while time.time() < handshake_deadline:
                for key, _ in selector.select(
                    min(0.25, max(0, handshake_deadline - time.time()))
                ):
                    if key.data == "input":
                        sent += os.write(child.stdin.fileno(), raw[sent : sent + 65536])
                        if sent == len(raw):
                            selector.unregister(child.stdin)
                            child.stdin.close()
                    elif os.read(read_fd, 1) == b"1":
                        return child
                    else:
                        raise RuntimeError("NO_SEND_SIGNAL")
        raise TimeoutError("SEND_HANDSHAKE_TIMEOUT")
    except BaseException:
        if child:
            reap(child)  # Must complete before dispatch releases its PG lock.
        raise
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def renew(store, fence):
    with store.ledger._transaction() as conn:
        store._lock(conn, fence.subject)
        if not store._valid(conn, fence):
            raise BudgetError("CONTROL_DENIED")
        conn.execute(
            "UPDATE m0_v3_subject SET lease_until=LEAST(%s,clock_timestamp()+interval '60 seconds') WHERE id=%s",
            (fence.run.deadline, fence.subject),
        )


def wait_transport(child, deadline, store, fence):
    raw = bytearray()
    next_renewal = time.monotonic() + 10
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while True:
                if time.time() >= deadline:
                    raise TimeoutError
                if time.monotonic() >= next_renewal:
                    renew(store, fence)
                    next_renewal = time.monotonic() + 10
                ready = selector.select(min(0.25, max(0, deadline - time.time())))
                if not ready:
                    continue
                chunk = os.read(child.stdout.fileno(), 65536)
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > 2097152 + 16:
                    raise ValueError("RESPONSE_LIMIT")
        child.wait(timeout=min(2, max(0.01, deadline - time.time())))
        if child.returncode != 0:
            raise ValueError("TRANSPORT_FAILED")
        status, payload = bytes(raw).split(b"\n", 1)
        return int(status), payload
    finally:
        reap(child)


def request(store, fence, round_number, body, deadline):
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    request_id = uuid4()
    handle = None
    status, usage = 0, None
    try:
        step = store.prepare_request(
            fence,
            "pg-fixture",
            round_number,
            body,
            request_id,
            3_440_640,
            4,
        )
        handle = start_transport(
            raw, fence.run.run_id, request_id, deadline, fence=fence
        )
        status, payload = wait_transport(handle, deadline, store, fence)
        # Persist the complete bounded wire payload before interpretation. This
        # table is restricted protocol state; diagnostics export only fixed codes.
        with store.ledger._transaction() as conn:
            conn.execute(
                "INSERT INTO m0_pg_wire_response(request,status,body) VALUES(%s,%s,%s)",
                (request_id, status, payload),
            )
        if status != 200:
            code = "PROVIDER_HTTP_REJECTED"
            try:
                rejected = json.loads(payload)
                if (
                    status == 400
                    and rejected.get("error", {}).get("message")
                    == "Thinking mode does not support this tool_choice"
                ):
                    code = "THINKING_TOOL_CHOICE_UNSUPPORTED"
            except (ValueError, AttributeError):
                pass
            reject_response(store, request_id, code, status)
        try:
            reply = json.loads(payload)
        except ValueError:
            reject_response(store, request_id, "PROVIDER_JSON_INVALID", status)
        if not isinstance(reply, dict):
            reject_response(store, request_id, "PROVIDER_JSON_INVALID", status)
        if reply.get("model") not in round02.PROFILE.reported_models:
            reject_response(store, request_id, "RESPONSE_MODEL_MISMATCH", status)
        usage = reply.get("usage")
        choice = reply["choices"][0]
        expected = "tool_calls" if round_number == 0 else "stop"
        if choice["finish_reason"] != expected:
            raise ValueError("INCOMPLETE_RESPONSE")
        assistant = choice["message"]
        if not store.commit_response(fence, step, assistant, request_id=request_id):
            raise ValueError("LATE_RESPONSE")
        return step, assistant, reply["model"]
    finally:
        if handle:
            reap(handle)
        # The global entry may not exist if PG denied before starter; retain the
        # original error without manufacturing another reservation or send.
        try:
            upper = finish_global(request_id, status, usage, deadline)
        except (ValueError, TimeoutError):
            upper = None
        try:
            identity = RequestIdentity(fence.run, request_id)
            if upper is None:
                store.ledger.retain_unknown(identity)
            else:
                from decimal import Decimal

                store.ledger.settle(identity, int(Decimal(str(upper)) * 1_000_000))
        except BudgetError:
            pass


def code_hashes():
    paths = (
        Path(__file__),
        ROOT / "scripts/m0_pg_private_transport.py",
        ROOT / "scripts/m0/step_store.py",
        ROOT / "scripts/m0/step_store.sql",
        ROOT / "scripts/m0/budget.py",
        Path(round02.__file__),
    )
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def execute(stage, record_path):
    started = time.time()
    stage_deadline = min(started + 360, round02.PROFILE.deadline)
    ledger = PostgresBudget(DSN)
    store = StepStore(ledger)
    fixture = json.loads((ROOT / "tests/fixtures/m0/protocol-v1.json").read_text())
    if stage == "first":
        if record_path.exists():
            raise ValueError("RECORD_EXISTS")
        store.install()
        with ledger._transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS m0_pg_wire_response (request uuid PRIMARY KEY REFERENCES m0_requests(id),status integer NOT NULL,body bytea NOT NULL,code text NOT NULL DEFAULT 'CAPTURED',captured_at timestamptz NOT NULL DEFAULT clock_timestamp())"
            )
        deadline = min(started + 1800, round02.PROFILE.deadline)
        run = RunContext(
            uuid4(), uuid4(), "deepseek", datetime.fromtimestamp(deadline, timezone.utc)
        )
        ledger.initialize(run.experiment_id, 3_500_000, run.deadline)
        subject = store.accept(
            run,
            "pg-fixture",
            {"synthetic": True, "request": "Read the fixed fixture."},
            VERSIONS,
        )
        record = {
            "experiment_id": str(run.experiment_id),
            "run_id": str(run.run_id),
            "subject_id": str(subject),
            "deadline": deadline,
            "first_pid": os.getpid(),
            "status": "first_started",
            "model": "deepseek-v4-flash",
            "provider_models_response_sha256": round02.PROFILE.provider_models_response_sha256,
            "boundary": "synthetic fixture plus real model/PG protocol; not active investigation",
            "code_hashes": code_hashes(),
            "profile_hash": round02.canonical_hash(round02.asdict(round02.PROFILE)),
        }
        round02.save(record_path, record)
        fence = store.claim(subject, run, uuid4(), VERSIONS, lease_seconds=60)
        body = {
            "model": "deepseek-v4-flash",
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
            "max_tokens": 32768,
            "messages": fixture["messages"],
            "tools": fixture["tools"],
        }
        step, assistant, reported = request(store, fence, 0, body, stage_deadline)
        calls = assistant.get("tool_calls", [])
        if len(calls) != 1:
            raise ValueError("ONE_TOOL_REQUIRED")
        attempt_id = uuid4()
        message = store.dispatch_tool(
            fence, step, 0, attempt_id, 2, lambda: tool_result(calls[0], fixture)
        )
        if not store.commit_tool(fence, step, 0, message, attempt_id=attempt_id):
            raise ValueError("TOOL_COMMIT_REJECTED")
        record.update(
            status="first_committed",
            step_id=str(step),
            evidence_hash=digest(fixture["evidence"]),
            observed_at=fixture["evidence"]["observed_at"],
            reported_models=[reported],
        )
    else:
        record = json.loads(record_path.read_text())
        if record.get("code_hashes") != code_hashes() or record.get(
            "profile_hash"
        ) != round02.canonical_hash(round02.asdict(round02.PROFILE)):
            raise ValueError("INCOMPATIBLE_STATE")
        if record["status"] != "first_committed" or record["first_pid"] == os.getpid():
            raise ValueError("FIRST_STAGE_REQUIRED")
        run = RunContext(
            UUID(record["experiment_id"]),
            UUID(record["run_id"]),
            "deepseek",
            datetime.fromtimestamp(record["deadline"], timezone.utc),
        )
        subject = UUID(record["subject_id"])
        stage_deadline = min(stage_deadline, record["deadline"])
        while True:
            if time.time() >= stage_deadline:
                raise TimeoutError
            try:
                fence = store.claim(subject, run, uuid4(), VERSIONS, lease_seconds=60)
                break
            except BudgetError as exc:
                if str(exc) != "LEASE_ACTIVE":
                    raise
                time.sleep(0.25)
        rebuilt = store.rebuild(fence, UUID(record["step_id"]))
        if rebuilt["status"] != "ready" or len(rebuilt["messages"]) != 2:
            raise ValueError("RECONSTRUCTION_FAILED")
        observed = json.loads(rebuilt["messages"][1]["content"])
        if (
            digest(observed) != record["evidence_hash"]
            or observed["observed_at"] != record["observed_at"]
        ):
            raise ValueError("EVIDENCE_CHANGED")
        history, normalized = wire_history(rebuilt["messages"])
        body = {
            "model": "deepseek-v4-flash",
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
            "max_tokens": 32768,
            "messages": [
                *rebuilt["input"]["messages"],
                *history,
                {
                    "role": "user",
                    "content": 'Return exactly a JSON object {"target":"m0-target-a","evidence_ids":["m0-evidence-a"],"status":"observed"}. This is only a synthetic observation, not a recovery conclusion.',
                },
            ],
            "response_format": {"type": "json_object"},
        }
        step, assistant, reported = request(store, fence, 1, body, stage_deadline)
        final = json.loads(assistant["content"])
        if final != {
            "target": "m0-target-a",
            "evidence_ids": ["m0-evidence-a"],
            "status": "observed",
        }:
            raise ValueError("FINAL_CONTRACT_MISMATCH")
        if not store.publish(fence, final, step=step):
            raise ValueError("PUBLICATION_REJECTED")
        record.update(
            status="completed",
            second_pid=os.getpid(),
            pid_changed=True,
            evidence_preserved=True,
            message_pairing=True,
            final_contract=True,
            reported_models=[*record["reported_models"], reported],
            wire_assistant_content_normalized=normalized,
            wire_history_version="v4-non-null-assistant-content-v1",
        )
    record["safe_pg_summary"] = store.summary(subject)
    round02.save(record_path, record)
    return {k: record[k] for k in ("status", "run_id", "boundary")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("first", "second"))
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        parser.error("explicit --execute required")
    try:
        print(json.dumps(execute(args.stage, args.record)))
    except BaseException as exc:
        code = str(exc)
        if len(code) > 80 or not re.fullmatch(r"[A-Z][A-Z_]+", code):
            code = "PG_PROBE_FAILED"
        print(
            json.dumps(
                {
                    "status": "failed",
                    "code": code,
                    "http_status": getattr(exc, "http_status", None),
                    "private_details_exported": False,
                }
            )
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
