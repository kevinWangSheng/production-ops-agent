"""Deterministic doubles for the workbench: no PostgreSQL, no network, no model.

``MemoryIncidentStore`` mirrors the refusal codes and generation rules of
``DurableStore`` closely enough to drive the web layer; the PostgreSQL
integration test proves the same flows against the real store. The ASGI
driver speaks spec 2.3 so Starlette listens for ``http.disconnect`` and
cancels a streaming response when the test has read enough.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

from opspilot.investigation.loop import (
    InvestigationLoop,
    InvestigationRequest,
    LoopOutcome,
)
from opspilot.investigation.store import MemoryStepStore, StepStoreError
from opspilot.persistence import Lease, PersistenceError
from opspilot.tools import TransportResponse
from opspilot.tools.outcomes import Window
from opspilot.web import (
    AuthConfig,
    Authenticator,
    MemoryEventLog,
    MemoryEvidenceStore,
    MemoryWebLedger,
    Workbench,
    create_app,
    hash_password,
    token_digest,
)
from opspilot.web.store import ControlAudit, IncidentSummary
from tests.m1_investigation_support import (
    TOOL_SCHEMAS,
    ScriptedModel,
    reply,
    report_from_transcript,
    tool_call,
)
from tests.m1_tool_support import (
    NOW,
    WINDOW_END,
    WINDOW_START,
    FakeClock,
    FixedControl,
    body,
    build,
)

UI_USER = "alice"
UI_PASSWORD = "correct horse battery staple"
EVENT_TOKEN = "event-token-for-tests-only"
ORIGIN = "https://ops.example.test"
_CONTROL_OPEN = frozenset({"queued", "running", "paused", "waiting_human"})


def basic(user: str = UI_USER, password: str = UI_PASSWORD) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"authorization": f"Basic {token}"}


def bearer(token: str = EVENT_TOKEN) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def same_origin() -> dict[str, str]:
    return {"origin": ORIGIN}


class MemoryIncidentStore:
    """In-memory ``IncidentStore`` with DurableStore's refusal vocabulary."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.incidents: dict[UUID, dict[str, Any]] = {}
        self.runs: dict[UUID, dict[str, Any]] = {}
        self.controls: list[dict[str, Any]] = []
        self.inputs: list[dict[str, Any]] = []
        self._by_key: dict[str, UUID] = {}

    # -- helpers ------------------------------------------------------------

    payload_supported = True

    def now(self):
        return self.clock.now()

    def _revoked(self, run: dict[str, Any], lease: Lease) -> bool:
        incident = self.incidents[lease.incident_id]
        return (
            run["owner"] != lease.owner
            or run["epoch"] != lease.epoch
            or incident["control_generation"] != lease.control_generation
            or run["lease_until"] is None
            or run["lease_until"] <= self.now()
            or run["deadline"] <= self.now()
        )

    def _late(self, run: dict[str, Any], key: str, payload: dict[str, Any], gen: int):
        if any(s["logical_key"] == key for s in run["steps"]):
            return
        run["steps"].append(
            {
                "step_id": uuid4(),
                "run_id": run["run_id"],
                "sequence": len(run["steps"]),
                "logical_key": key,
                "status": "late_result",
                "response": payload,
                "tool_results": [],
                "control_generation": gen,
            }
        )

    # -- IncidentStore ------------------------------------------------------

    def accept(
        self, incident_id, run_id, intake_key, *, deadline, budget_limit, versions
    ):
        existing = self._by_key.get(intake_key)
        if existing is not None:
            row = self.incidents[existing]
            if row["incident_id"] != incident_id or row["current_run_id"] != run_id:
                raise PersistenceError("IDENTITY_CONFLICT")
            return
        if incident_id in self.incidents:
            raise PersistenceError("IDENTITY_CONFLICT")
        self.incidents[incident_id] = {
            "incident_id": incident_id,
            "intake_key": intake_key,
            "state": "queued",
            "lifecycle": "open",
            "control_generation": 0,
            "current_run_id": run_id,
            "conclusion": None,
            "created_at": self.now(),
        }
        self.runs[run_id] = self._run(
            run_id, incident_id, 0, deadline, budget_limit, versions
        )
        self._by_key[intake_key] = incident_id

    @staticmethod
    def _run(run_id, incident_id, generation, deadline, budget_limit, versions):
        return {
            "run_id": run_id,
            "incident_id": incident_id,
            "state": "queued",
            "epoch": 0,
            "owner": None,
            "lease_until": None,
            "control_generation": generation,
            "budget_limit": budget_limit,
            "budget_reserved": 0,
            "budget_spent": 0,
            "budget_unknown": 0,
            "deadline": deadline,
            "versions": dict(versions),
            "input_watermark": 0,
            "steps": [],
            "reservations": {},
        }

    def control(self, incident_id, expected_generation, action, actor, payload=None):
        row = self.incidents.get(incident_id)
        if row is None:
            raise PersistenceError("UNKNOWN_IDENTITY")
        if row["control_generation"] != expected_generation:
            raise PersistenceError("CONTROL_CONFLICT")
        if action not in {"cancel", "pause", "resume", "follow_up", "correct"}:
            raise PersistenceError("INVALID_INPUT")
        run = self.runs.get(row["current_run_id"])
        if run is None or run["incident_id"] != incident_id:
            raise PersistenceError("INCONSISTENT_STATE")
        if row["state"] in {"cancelled", "completed"} or row["conclusion"] is not None:
            raise PersistenceError("ILLEGAL_TRANSITION")
        # PR #31: a follow_up/correct that carries content is recorded while
        # paused (input only; the run stays paused), a bare one is refused.
        if (
            row["state"] == "paused"
            and action in {"pause", "follow_up", "correct"}
            and payload is None
        ):
            raise PersistenceError("ILLEGAL_TRANSITION")
        if action != "cancel" and run["state"] not in _CONTROL_OPEN:
            raise PersistenceError("ILLEGAL_TRANSITION")
        nxt = expected_generation + 1
        row["control_generation"] = nxt
        # PR #31 keep_paused: a note recorded while paused keeps the incident
        # paused and parks a running run; it never resumes anything.
        keep_paused = row["state"] == "paused" and action in {"follow_up", "correct"}
        row["state"] = (
            "cancelled"
            if action == "cancel"
            else "paused"
            if action == "pause" or keep_paused
            else "running"
        )
        transitions = {
            "cancel": (
                "cancelled",
                {"queued", "paused", "running", "waiting_human", "blocked"},
            ),
            "pause": ("paused", {"running", "waiting_human"}),
            "resume": ("queued", {"queued", "paused", "running", "waiting_human"}),
            "follow_up": ("queued", {"queued", "running", "waiting_human"}),
            "correct": ("queued", {"queued", "running", "waiting_human"}),
        }
        target, allowed = transitions[action]
        if keep_paused:
            target, allowed = "paused", {"running", "waiting_human"}
        for candidate in self.runs.values():
            if (
                candidate["incident_id"] == incident_id
                and candidate["state"] in allowed
            ):
                candidate.update(
                    state=target, owner=None, lease_until=None, control_generation=nxt
                )
        self.controls.append(
            {
                "incident_id": incident_id,
                "action": action,
                "expected": expected_generation,
                "resulting": nxt,
                "actor": actor,
                "payload": payload,
            }
        )
        if action in {"follow_up", "correct"}:
            self.inputs.append(
                {
                    "incident_id": incident_id,
                    "sequence": len(self.inputs) + 1,
                    "kind": action,
                    "content": dict(payload or {}),
                    "actor": actor,
                    "control_generation": nxt,
                }
            )
        return nxt

    def new_run(self, incident_id, run_id, *, deadline, budget_limit, versions, actor):
        row = self.incidents.get(incident_id)
        if row is None:
            raise PersistenceError("UNKNOWN_IDENTITY")
        if run_id in self.runs:
            generation = self.runs[run_id]["control_generation"]
            if (
                row["state"] == "queued"
                and row["current_run_id"] == run_id
                and row["control_generation"] == generation
            ):
                return generation
            raise PersistenceError("IDENTITY_CONFLICT")
        if row["state"] != "cancelled":
            raise PersistenceError("ILLEGAL_TRANSITION")
        nxt = row["control_generation"] + 1
        self.runs[run_id] = self._run(
            run_id, incident_id, nxt, deadline, budget_limit, versions
        )
        row.update(
            state="queued",
            lifecycle="open",
            control_generation=nxt,
            current_run_id=run_id,
            conclusion=None,
        )
        self.controls.append(
            {
                "incident_id": incident_id,
                "action": "new_run",
                "expected": nxt - 1,
                "resulting": nxt,
                "actor": actor,
            }
        )
        return nxt

    def rebuild(self, incident_id):
        row = self.incidents.get(incident_id)
        if row is None:
            raise PersistenceError("UNKNOWN_IDENTITY")
        run = self.runs[row["current_run_id"]]
        steps = sorted(run["steps"], key=lambda s: s["sequence"])
        pending = [
            {
                "step_id": s["step_id"],
                "ordinal": i,
                "operation_id": f"{s['step_id']}:{i}",
                "tool_call": call,
            }
            for s in steps
            if s["control_generation"] == row["control_generation"]
            and s["status"] in {"response_committed", "tool_result_committed"}
            for i, call in enumerate((s["response"] or {}).get("tool_calls", []))
            if i not in {item["ordinal"] for item in s["tool_results"]}
        ]
        return {
            "incident_id": incident_id,
            "state": row["state"],
            "control_generation": row["control_generation"],
            "run": {k: v for k, v in run.items() if k not in {"steps", "reservations"}},
            "steps": [dict(s) for s in steps],
            "pending_tools": pending,
            "conclusion": row["conclusion"],
        }

    def claim(self, incident_id, run_id, owner, versions, lease_seconds=30):
        row = self.incidents.get(incident_id)
        run = self.runs.get(run_id)
        if row is None or run is None or run["incident_id"] != incident_id:
            raise PersistenceError("UNKNOWN_IDENTITY")
        now = self.now()
        if (
            run["state"] == "running"
            and run["lease_until"]
            and run["lease_until"] > now
        ):
            raise PersistenceError("LEASE_ACTIVE")
        if run["deadline"] <= now:
            raise PersistenceError("DEADLINE_EXCEEDED")
        if run["versions"] != versions:
            run["state"] = "blocked"
            raise PersistenceError("INCOMPATIBLE_STATE")
        if row["state"] in {"completed", "cancelled", "paused"}:
            raise PersistenceError("CONTROL_DENIED")
        if run["state"] not in {"queued", "running"}:
            raise PersistenceError("CONTROL_DENIED")
        run["epoch"] += 1
        run.update(
            state="running",
            owner=owner,
            control_generation=row["control_generation"],
            lease_until=now + timedelta(seconds=lease_seconds),
        )
        return Lease(
            incident_id, run_id, owner, run["epoch"], row["control_generation"]
        )

    def publish(self, lease, conclusion, *, step_id):
        row = self.incidents[lease.incident_id]
        run = self.runs[row["current_run_id"]]
        if row["current_run_id"] == lease.run_id and row["conclusion"] == conclusion:
            return True
        if (
            row["current_run_id"] != lease.run_id
            or run["state"] != "running"
            or row["state"] in {"completed", "cancelled", "paused"}
            or self._revoked(run, lease)
        ):
            self._late(
                self.runs[lease.run_id],
                f"late_result:publish:{step_id}",
                conclusion,
                lease.control_generation,
            )
            return False
        final = next((s for s in run["steps"] if s["step_id"] == step_id), None)
        if (
            final is None
            or final["control_generation"] != lease.control_generation
            or final["status"] not in {"response_committed", "tool_result_committed"}
            or final["response"] != conclusion
        ):
            raise PersistenceError("FINAL_STEP_REQUIRED")
        # DurableStore.publish() leaves the incident state column alone: the
        # conclusion column and the run state carry completion.
        row["conclusion"] = conclusion
        run.update(state="completed", owner=None, lease_until=None)
        return True

    def renew_lease(self, lease, extend_seconds):
        """Mirror DurableStore.renew_lease (PR #35): same fence, capped at deadline."""
        if extend_seconds <= 0:
            raise PersistenceError("INVALID_INPUT")
        run = self.runs.get(lease.run_id)
        if run is None or run["state"] != "running" or self._revoked(run, lease):
            raise PersistenceError("CONTROL_DENIED")
        now = self.now()
        run["lease_until"] = min(
            max(run["lease_until"], now + timedelta(seconds=extend_seconds)),
            run["deadline"],
        )
        self.renewals = getattr(self, "renewals", 0) + 1
        return run["lease_until"]

    def abandon(self, lease):
        run = self.runs[lease.run_id]
        if (
            run["owner"] == lease.owner
            and run["epoch"] == lease.epoch
            and run["control_generation"] == lease.control_generation
        ):
            run.update(owner=None, lease_until=None)

    def committer(self, lease):
        return _MemoryCommitter(self, lease)

    def list_incidents(self, *, limit=50):
        rows = sorted(
            self.incidents.values(), key=lambda r: r["created_at"], reverse=True
        )
        return tuple(self._summary(r) for r in rows[:limit])

    def find_incident(self, incident_id):
        row = self.incidents.get(incident_id)
        return None if row is None else self._summary(row)

    def control_audit(self, incident_id):
        return tuple(
            ControlAudit(a["action"], a["expected"], a["resulting"], a["actor"])
            for a in self.controls
            if a["incident_id"] == incident_id
        )

    def run_ids(self, incident_id):
        return frozenset(
            str(r["run_id"])
            for r in self.runs.values()
            if r["incident_id"] == incident_id
        )

    @staticmethod
    def _summary(row):
        return IncidentSummary(
            incident_id=row["incident_id"],
            intake_key=row["intake_key"],
            state=row["state"],
            lifecycle=row["lifecycle"],
            control_generation=row["control_generation"],
            current_run_id=row["current_run_id"],
            concluded=row["conclusion"] is not None,
            created_at=row["created_at"],
        )


class _MemoryCommitter:
    def __init__(self, store: MemoryIncidentStore, lease: Lease) -> None:
        self._store = store
        self._lease = lease

    @property
    def authorized_run_id(self):
        return str(self._lease.run_id)

    def _run(self):
        run = self._store.runs[self._lease.run_id]
        if self._store._revoked(run, self._lease):
            raise StepStoreError("CONTROL_DENIED")
        return run

    def begin_round(self, logical_key):
        run = self._run()
        # Mirror DurableStore.begin_round (PR #31): hand the recorded human
        # inputs of this incident to the round, keyed per attempt.
        key = f"g{self._lease.control_generation}:e{run['epoch']}:{logical_key}"
        inputs = [
            {"sequence": i["sequence"], "kind": i["kind"], "content": i["content"]}
            for i in self._store.inputs
            if i["incident_id"] == self._lease.incident_id
        ]
        return key, inputs

    def assert_current(self) -> None:
        self._run()

    def reserve_budget(self, reservation_id, amount):
        if amount <= 0:
            raise StepStoreError("INVALID_INPUT")
        run = self._run()
        if reservation_id in run["reservations"]:
            if run["reservations"][reservation_id] != amount:
                raise StepStoreError("IDENTITY_CONFLICT")
            return
        if (
            run["budget_reserved"]
            + run["budget_spent"]
            + run["budget_unknown"]
            + amount
            > run["budget_limit"]
        ):
            raise StepStoreError("BUDGET_EXHAUSTED")
        run["reservations"][reservation_id] = amount
        run["budget_reserved"] += amount

    def settle_budget(self, reservation_id, outcome):
        run = self._run()
        if reservation_id not in run["reservations"]:
            raise StepStoreError("UNKNOWN_IDENTITY")
        settled = run.setdefault("settled", {})
        if reservation_id in settled:
            if settled[reservation_id] != outcome:
                raise StepStoreError("IDENTITY_CONFLICT")
            return
        settled[reservation_id] = outcome
        amount = run["reservations"][reservation_id]
        run["budget_reserved"] -= amount
        run["budget_spent" if outcome == "spent" else "budget_unknown"] += amount

    def commit_step(self, logical_key, response):
        run = self._store.runs[self._lease.run_id]
        if self._store._revoked(run, self._lease):
            self._store._late(
                run,
                f"late_result:step:{logical_key}",
                dict(response),
                self._lease.control_generation,
            )
            raise StepStoreError("CONTROL_DENIED")
        for step in run["steps"]:
            if step["logical_key"] == logical_key:
                return step["step_id"]
        step_id = uuid4()
        run["steps"].append(
            {
                "step_id": step_id,
                "run_id": run["run_id"],
                "sequence": len(run["steps"]),
                "logical_key": logical_key,
                "status": "response_committed",
                "response": dict(response),
                "tool_results": [],
                "control_generation": self._lease.control_generation,
            }
        )
        return step_id

    def commit_tool(self, step_id, ordinal, result):
        run = self._store.runs[self._lease.run_id]
        step = next((s for s in run["steps"] if s["step_id"] == step_id), None)
        if step is None:
            raise StepStoreError("UNKNOWN_IDENTITY")
        if (
            step["control_generation"] != self._lease.control_generation
            or step["status"] == "late_result"
            or self._store._revoked(run, self._lease)
        ):
            self._store._late(
                run,
                f"late_result:tool:{step_id}:{ordinal}",
                dict(result),
                self._lease.control_generation,
            )
            raise StepStoreError("CONTROL_DENIED")
        if any(item["ordinal"] == ordinal for item in step["tool_results"]):
            return
        step["tool_results"].append({"ordinal": ordinal, "result": dict(result)})
        step["status"] = "tool_result_committed"


class ScriptedInvestigator:
    """Drives the real loop with a scripted model and the test tool doubles.

    ``replies`` defaults to one tool round followed by a report that cites
    the evidence just produced; ``model_requests`` bounds the Run.
    """

    def __init__(self, clock: FakeClock, *, replies=None, model_requests=2):
        self.clock = clock
        self.replies = replies
        self.model_requests = model_requests
        self.contexts = []
        self.models = []

    def investigate(self, context, committer, evidence):
        self.contexts.append(context)
        executor, transport, _, _ = build(
            clock=self.clock,
            sink=evidence,
            control=FixedControl(generation=context.control_generation),
            scope_overrides={
                "run_id": str(context.run_id),
                "subject_id": str(context.incident_id),
                "control_generation": context.control_generation,
                "deadline": context.deadline,
                "window": Window(WINDOW_START, WINDOW_END),
            },
        )
        transport.response = TransportResponse(
            body=body([{"metric": "checkout", "value": 3}]), data_as_of=WINDOW_START
        )
        replies = self.replies
        if replies is None:
            replies = [
                reply(tool_calls=[tool_call()], finish="tool_calls"),
                report_from_transcript,
            ]
        model = ScriptedModel(list(replies))
        self.models.append(model)
        loop = InvestigationLoop(
            model=model, executor=executor, store=committer, clock=self.clock
        )
        request = InvestigationRequest(
            run_id=str(context.run_id),
            question=context.question,
            scope=executor.scope,
            tool_schemas=TOOL_SCHEMAS,
            model_requests=self.model_requests,
            evidence_context={
                "type": "opspilot-evidence-context-v4",
                "time_policies": [
                    {
                        "id": "policy-window-1",
                        "mode": "historical_window",
                        "window": {
                            "start": WINDOW_START.isoformat(),
                            "end": WINDOW_END.isoformat(),
                        },
                    }
                ],
            },
        )
        outcome: LoopOutcome = loop.run(request)
        return outcome


def build_workbench(*, clock=None, sse_poll=0.01, sse_idle=0.2):
    clock = clock or FakeClock(start=NOW)
    incidents = MemoryIncidentStore(clock)
    events = MemoryEventLog()
    evidence = MemoryEvidenceStore()
    ledger = MemoryWebLedger()
    workbench = Workbench(
        incidents=incidents,
        events=events,
        evidence=evidence,
        ledger=ledger,
        run_versions={"state": "v1"},
        run_seconds=600,
    )
    config = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD, salt=b"fixed-salt-16byt")},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager-prod"},
        auth_revision="auth-rev-1",
        allowed_origins=frozenset({ORIGIN}),
    )
    app = create_app(
        workbench,
        Authenticator(config),
        clock,
        sse_poll_seconds=sse_poll,
        sse_idle_seconds=sse_idle,
    )
    return app, workbench, clock


# -- ASGI driver -----------------------------------------------------------


class Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self):
        return self.body.decode("utf-8")

    def json(self):
        return json.loads(self.body)

    def sse_events(self):
        """Parse the collected stream into (id, event, data) tuples."""
        events = []
        for block in self.text.split("\n\n"):
            fields = {}
            for line in block.splitlines():
                if ":" not in line or line.startswith(":"):
                    continue
                name, value = line.split(":", 1)
                fields[name.strip()] = value.strip()
            if "event" in fields:
                data = json.loads(fields.get("data", "null"))
                events.append((fields.get("id"), fields["event"], data))
        return events


async def _call(app, method, path, *, headers=None, body=b"", stop_after=None):
    query = ""
    if "?" in path:
        path, query = path.split("?", 1)
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    raw_headers.append((b"content-length", str(len(body)).encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": raw_headers,
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 443),
    }
    collected = {"status": None, "headers": {}, "body": bytearray()}
    sent_body = False
    disconnect = asyncio.Event()

    async def receive():
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            collected["status"] = message["status"]
            collected["headers"] = {
                k.decode(): v.decode() for k, v in message["headers"]
            }
        elif message["type"] == "http.response.body":
            collected["body"] += message.get("body", b"")
            if stop_after is not None and stop_after(bytes(collected["body"])):
                disconnect.set()
            if not message.get("more_body", False):
                disconnect.set()

    await app(scope, receive, send)
    return Response(collected["status"], collected["headers"], bytes(collected["body"]))


def call(app, method, path, **kwargs):
    return asyncio.run(_call(app, method, path, **kwargs))


def post_form(app, path, fields, *, headers):
    merged = {"content-type": "application/x-www-form-urlencoded", **headers}
    return call(app, "POST", path, headers=merged, body=urlencode(fields).encode())


def post_json(app, path, payload, *, headers):
    merged = {"content-type": "application/json", **headers}
    return call(app, "POST", path, headers=merged, body=json.dumps(payload).encode())


def stream(app, path, *, headers, until_events):
    """Read the SSE stream until ``until_events`` events were seen, then disconnect."""

    def enough(chunk: bytes) -> bool:
        return chunk.count(b"\nevent: ") + chunk.count(
            b"event: "
        ) // 2 >= until_events or (chunk.count(b"event: ") >= until_events)

    return call(app, "GET", path, headers=headers, stop_after=enough)


def submit_incident(app, *, key="demo-key-1", question="Why is checkout erroring?"):
    return post_form(
        app,
        "/intake/ui",
        {"target_id": "checkout-prod", "question": question, "idempotency_key": key},
        headers={**basic(), **same_origin()},
    )


LOOP_HAS_INPUT_ROUNDS = hasattr(MemoryStepStore, "begin_round")


def note_reached_investigation(workbench, investigator, text):
    """The note is a recorded input and, where the loop has begin_round, was sent."""
    recorded = any(i["content"].get("text") == text for i in workbench.incidents.inputs)
    if not LOOP_HAS_INPUT_ROUNDS:
        return recorded
    sent = any(
        "investigation_inputs" in str(m.get("content"))
        and text in str(m.get("content"))
        for model in investigator.models
        for call in model.calls
        for m in call.messages
    )
    return recorded and sent
