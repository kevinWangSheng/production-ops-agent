"""#358: one bounded real Run for #347 (an alert attached to a release
incident; the successor Run re-frames its box). Engineer-only, kind window.

``python -m scripts.r358_live <command>`` from the worktree:

* ``plan``             the steps and the frozen expectations; no external call.
* ``trace-project``    create the round's LangSmith project with the longest
                       retention before the worker starts (``OPSPILOT_TRACE=lab``;
                       LangSmith entries read from ``OPSPILOT_ENV_FILE``).
* ``silence-create``   one Alertmanager silence on the checkout alert rule
                       (through the already-forwarded Alertmanager API).
* ``silence-delete``   expire it again.
* ``drive``            after the PR-5c release path opened the release
                       incident: as soon as its first Run exists, delete the
                       silence; when the alert episode is filed ``attached``
                       to the incident, ``cancel`` and then ``new_run``
                       through the workbench control route. If the attach
                       does not arrive in time the attempt is recorded as an
                       environment cause (exit 4) and the round is rerun.
* ``collect``          freeze ``summary.json`` (ADR-0006) from read-only SQL,
                       the drive ledger, an offline gateway judgment and the
                       LangSmith read-back.

The release, its webhook and the fault are the F11 harness's
(``scripts.f11_u2_harness release/watch/webhook/fault``) exactly as in the
PR-5c window (``docs/evidence/f11-acceptance-live/run.md`` release B); the
page helpers are ``scripts.f11_pr5c_lab``'s. Main evidence (ruling Q11):
deterministic box/anchor assertions on the stored Run inputs (successor box
end = the successor's creation instant, anchor = ``released_at``); the
gateway's allow/deny records corroborate. A successor that never queried the
attached interval is recorded as "未观察到", not as a failure. "The first Run
would refuse it" is an offline gateway judgment over the first Run's stored
input: no network, no model.

Credentials come from private files named by environment variables and are
never printed: ``F11_5C_UI_CRED`` (workbench Basic Auth, JSON),
``R358_DSN`` (read-only readouts on the one-off PG).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from opspilot.investigation.context import InvestigationInput
from opspilot.investigation.inputs import RELEASE_ANCHOR_RULE, ReleaseAnchor
from opspilot.persistence import Lease
from opspilot.tools import ToolRequest, Window
from opspilot.tools.otel_demo import (
    METRICS_TOOL,
    TARGET_ID,
    OtelDemoConfig,
    otel_demo_executor_factory,
    scope_window,
)
from scripts import f11_pr5c_lab as lab
from scripts.f11_u2_harness import resolve_ledger_root

BATCH = "358-live"
ALERTNAME = "CheckoutPlaceOrderErrorRatioHigh"
ALERTMANAGER = "http://127.0.0.1:19093"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
EMPTY_MATRIX = b'{"status":"success","data":{"resultType":"matrix","result":[]}}'
ENVIRONMENT_TIMEOUT = 4


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ledger(batch: str) -> Path:
    path = resolve_ledger_root() / batch
    path.mkdir(parents=True, exist_ok=True)
    return path


def append(batch: str, record: Mapping[str, Any]) -> None:
    with (ledger(batch) / "drive.jsonl").open("a") as handle:
        handle.write(
            json.dumps({"at": now(), **record}, sort_keys=True, default=str) + "\n"
        )


# -- Alertmanager silence ----------------------------------------------------


def _am(
    method: str, url: str, body: Mapping[str, Any] | None = None
) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with OPENER.open(request, timeout=20) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()[:300].decode(errors="replace")


def silence_body(hours: float, at: datetime) -> dict[str, Any]:
    return {
        "matchers": [{"name": "alertname", "value": ALERTNAME, "isRegex": False}],
        "startsAt": at.isoformat(),
        "endsAt": (at + timedelta(hours=hours)).isoformat(),
        "createdBy": "r358-live",
        "comment": "#358: hold the alert until the release incident's first Run exists",
    }


def silence_create(batch: str, base: str, hours: float) -> str:
    status, body = _am(
        "POST",
        f"{base}/api/v2/silences",
        silence_body(hours, datetime.now(timezone.utc)),
    )
    if status != 200 or not isinstance(body, dict) or "silenceID" not in body:
        raise SystemExit(f"silence create: HTTP {status} {body!r}")
    append(batch, {"kind": "silence_created", "silence_id": body["silenceID"]})
    return str(body["silenceID"])


def silence_delete(batch: str, base: str, silence_id: str) -> int:
    status, body = _am("DELETE", f"{base}/api/v2/silence/{silence_id}")
    append(
        batch,
        {"kind": "silence_deleted", "silence_id": silence_id, "http_status": status},
    )
    return status


# -- read-only SQL -----------------------------------------------------------


def connect() -> Any:
    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(os.environ["R358_DSN"], row_factory=dict_row)
    # every transaction on this session is read-only, not just the first
    conn.read_only = True
    return conn


def incident_state(conn: Any, incident_id: str) -> dict[str, Any]:
    incident = conn.execute(
        "SELECT incident_id::text, intake_key, lifecycle, control_generation,"
        " current_run_id::text FROM opspilot_incidents WHERE incident_id=%s",
        (incident_id,),
    ).fetchone()
    link = conn.execute(
        "SELECT l.observation_id::text, l.run_id::text, l.consumed_at, o.released_at"
        " FROM opspilot_release_incident_links l"
        " JOIN opspilot_release_observations o ON o.observation_id=l.observation_id"
        " WHERE l.incident_id=%s",
        (incident_id,),
    ).fetchone()
    runs = conn.execute(
        "SELECT run_id::text, state, deadline, input, versions, budget_spent,"
        " tool_operations_used FROM opspilot_runs WHERE incident_id=%s ORDER BY deadline",
        (incident_id,),
    ).fetchall()
    deliveries = conn.execute(
        "SELECT delivery_id, delivery_key, status, outcome, received_at, starts_at"
        " FROM opspilot_alert_deliveries WHERE incident_id=%s ORDER BY delivery_id",
        (incident_id,),
    ).fetchall()
    controls = conn.execute(
        "SELECT action, expected_generation, resulting_generation, actor, created_at"
        " FROM opspilot_controls WHERE incident_id=%s ORDER BY resulting_generation",
        (incident_id,),
    ).fetchall()
    conn.rollback()
    return {
        "incident": incident,
        "link": link,
        "runs": runs,
        "deliveries": deliveries,
        "controls": controls,
    }


def run_steps(conn: Any, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT sequence, status, response, tool_results, observed_at FROM opspilot_steps"
        " WHERE run_id=%s ORDER BY sequence",
        (run_id,),
    ).fetchall()
    conn.rollback()
    return list(rows)


# -- pure checks over stored rows --------------------------------------------


def time_policy(run_input: Mapping[str, Any]) -> Mapping[str, Any]:
    policies = (run_input.get("evidence_context") or {}).get("time_policies") or []
    if not policies:
        raise SystemExit("run input carries no time policy")
    return policies[0]


def first_attached_episode(
    deliveries: list[Mapping[str, Any]], after: datetime
) -> Mapping[str, Any] | None:
    """#305 V1-B: the first ``attached`` firing delivery of each key, the
    earliest of those received (whole UTC seconds) strictly after ``after``."""
    seen: set[str] = set()
    found = []
    for row in deliveries:
        if row["outcome"] != "attached" or row["status"] != "firing":
            continue
        if row["delivery_key"] in seen:
            continue
        seen.add(row["delivery_key"])
        received = row["received_at"].astimezone(timezone.utc).replace(microsecond=0)
        if received > after:
            found.append(row)
    return min(found, key=lambda r: r["received_at"]) if found else None


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def box_assertions(
    first: Mapping[str, Any],
    successor: Mapping[str, Any],
    released_at: datetime,
    consumed_at: datetime,
    run_seconds: float,
) -> dict[str, Any]:
    """Q11 main evidence. The successor's creation instant is the control's
    database instant, its deadline less the Run wall (service.py, #305
    W1-B); the expected box comes from the product's own ``ReleaseAnchor``."""
    created = successor["deadline"] - timedelta(seconds=run_seconds)
    expected = ReleaseAnchor(
        released_at=released_at, consumed_at=consumed_at, frame_end=created
    )
    expected_first = ReleaseAnchor(released_at=released_at, consumed_at=consumed_at)
    old, new = time_policy(first["input"]), time_policy(successor["input"])
    start, end = expected.frame
    first_start, first_end = expected_first.frame
    default = expected.default_query_window
    checks = {
        "successor_box_end_is_creation": new["window"]["end"] == end.isoformat(),
        "successor_box_is_24h": new["window"]["start"] == start.isoformat(),
        "successor_anchor_is_released_at": new.get("anchor") == _iso(released_at)
        and new.get("anchor_rule") == RELEASE_ANCHOR_RULE,
        "successor_default_window": new.get("default_query_window")
        == {"start": default[0].isoformat(), "end": default[1].isoformat()},
        "first_box_end_is_consumption": old["window"]["end"] == first_end.isoformat()
        and old["window"]["start"] == first_start.isoformat(),
        "first_anchor_is_released_at": old.get("anchor") == _iso(released_at),
        "release_observation_fact_unchanged": (
            first["input"].get("scope_facts") or {}
        ).get("release_observation")
        == (successor["input"].get("scope_facts") or {}).get("release_observation"),
    }
    return {
        "successor_created_at": created.isoformat(),
        "run_seconds": run_seconds,
        "first_box": old["window"],
        "successor_box": new["window"],
        "successor_anchor": new.get("anchor"),
        "successor_default_query_window": new.get("default_query_window"),
        "checks": checks,
        "ok": all(checks.values()),
    }


def _views(tool_results: Any) -> Iterator[Mapping[str, Any]]:
    """Gateway views in a step's ``tool_results``, wherever they nest."""
    if isinstance(tool_results, Mapping):
        if (
            "status" in tool_results
            and "tool" in tool_results
            and "window" in tool_results
        ):
            yield tool_results
        for value in tool_results.values():
            yield from _views(value)
    elif isinstance(tool_results, list):
        for value in tool_results:
            yield from _views(value)
    elif isinstance(tool_results, str) and tool_results[:1] == "{":
        try:
            yield from _views(json.loads(tool_results))
        except ValueError:
            return


def gateway_records(steps: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for step in steps:
        for view in _views(step.get("tool_results")):
            out.append(
                {
                    "sequence": step.get("sequence"),
                    "tool": view.get("tool"),
                    "status": view.get("status"),
                    "reason": view.get("reason"),
                    "window": view.get("window"),
                }
            )
    return out


def covers_episode(
    window: Mapping[str, str] | None, first_box_end: datetime, episode_at: datetime
) -> bool:
    """A query reaches the attached episode when its window runs past the
    first Run's box end and up to (or beyond) the episode's receipt."""
    if not isinstance(window, Mapping):
        return False
    parsed = Window.parse(window)
    if parsed is None:
        return False
    return parsed.end > first_box_end and parsed.end >= episode_at.replace(
        microsecond=0
    )


def episode_queries(
    records: list[Mapping[str, Any]], first_box_end: datetime, episode_at: datetime
) -> dict[str, Any]:
    hits = [
        r
        for r in records
        if r["tool"] == METRICS_TOOL
        and covers_episode(r["window"], first_box_end, episode_at)
    ]
    return {
        "observed": "observed" if hits else "未观察到",
        "queries": hits,
        "ok": [r for r in hits if r["status"] == "ok"],
    }


# -- offline gateway judgment ------------------------------------------------


class _Reply(io.BytesIO):
    status = 200


class _Opener:
    """Takes the request object and answers locally; nothing leaves."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    def open(self, request: Any, timeout: float | None = None) -> _Reply:
        self.requests.append(request)
        return _Reply(EMPTY_MATRIX)


class _RunStore:
    def __init__(self, lease: Lease, deadline: datetime) -> None:
        self.lease, self.deadline = lease, deadline

    def rebuild(self, incident_id: Any) -> dict[str, Any]:
        return {
            "run": {
                "run_id": self.lease.run_id,
                "deadline": self.deadline,
                "tool_operations_used": 0,
                "tool_seconds_used": 0.0,
            }
        }

    def charge_tool(
        self, lease: Any, operation_id: Any, seconds: Any, **_: Any
    ) -> None:
        return None

    def control_state(self, incident_id: Any) -> dict[str, Any]:
        return {
            "incident_generation": self.lease.control_generation,
            "incident_state": "running",
            "global_suspended": False,
            "global_generation": 0,
            "target_suspended": False,
            "target_generation": 0,
        }


class _Clock:
    def __init__(self, at: datetime) -> None:
        self._at = at

    def now(self) -> datetime:
        return self._at

    def monotonic(self) -> float:
        return 1000.0


class _Sink:
    def register(self, record: Any) -> str:
        return str(record.evidence_id)


@contextmanager
def _local_opener(opener: _Opener) -> Iterator[None]:
    original: Callable[..., Any] = urllib.request.build_opener
    urllib.request.build_opener = lambda *handlers: opener  # type: ignore[assignment]
    try:
        yield
    finally:
        urllib.request.build_opener = original  # type: ignore[assignment]


def gateway_judgment(
    run_input: Mapping[str, Any], window: Window, expr: str = "sum(up)"
) -> dict[str, Any]:
    """The product gateway's decision for one metrics query over ``window``
    against a Run's stored input, offline: the transport's opener is local,
    no model is involved. ``requests_built`` counts requests the transport
    built (and the local opener swallowed)."""
    parsed = InvestigationInput.from_json(dict(run_input))
    frame = scope_window(parsed)
    lease = Lease(
        incident_id=uuid4(),
        run_id=uuid4(),
        owner=uuid4(),
        epoch=1,
        control_generation=1,
        global_suspension_generation=0,
        target_suspension_generation=0,
    )
    opener = _Opener()
    # the transport builds its opener when the factory wires it, so the
    # local opener must already be in place then
    with _local_opener(opener):
        factory = otel_demo_executor_factory(
            _RunStore(lease, frame.end + timedelta(minutes=30)),
            evidence=_Sink(),
            clock=_Clock(frame.end + timedelta(seconds=1)),
            config=OtelDemoConfig(
                prometheus_url="http://127.0.0.1:19090",
                jaeger_url="http://127.0.0.1:16686/jaeger/ui",
            ),
        )
        outcome = factory(lease, parsed).execute(
            ToolRequest(
                step_id="r358-offline",
                tool_index=0,
                tool_name=METRICS_TOOL,
                target_ref=TARGET_ID,
                params={"expr": expr},
                window=window.as_json(),
            )
        )
    return {
        "frame": frame.as_json(),
        "window": window.as_json(),
        "status": outcome.status,
        "reason": outcome.reason,
        "requests_built": len(opener.requests),
        "network": False,
        "model": False,
    }


# -- drive -------------------------------------------------------------------


def _control(
    incident_id: str, action: str, generation: int, key: str
) -> dict[str, Any]:
    status, raw, _ = lab.http(
        "POST",
        f"/incidents/{incident_id}/control",
        form={
            "action": action,
            "expected_generation": str(generation),
            "idempotency_key": key,
        },
    )
    body = json.loads(raw) if raw[:1] == b"{" else raw[:300].decode(errors="replace")
    return {"action": action, "http_status": status, "response": body, "at": now()}


def drive(args: argparse.Namespace) -> int:
    batch = args.batch
    deadline = time.monotonic() + args.incident_wait
    incident_id = None
    while time.monotonic() < deadline:
        page = lab.page(args.observation)
        incident_id = page.get("incident_id")
        if incident_id:
            break
        time.sleep(args.every)
    if not incident_id:
        append(
            batch, {"kind": "environment_timeout", "waiting_for": "release incident"}
        )
        return ENVIRONMENT_TIMEOUT
    with connect() as conn:
        while True:
            state = incident_state(conn, incident_id)
            if state["runs"]:
                break
            if time.monotonic() > deadline:
                append(
                    batch, {"kind": "environment_timeout", "waiting_for": "first run"}
                )
                return ENVIRONMENT_TIMEOUT
            time.sleep(args.every)
        first = state["runs"][0]
        append(
            batch,
            {
                "kind": "first_run_seen",
                "incident_id": incident_id,
                "run_id": first["run_id"],
            },
        )
        # Q11 procedure: delete the silence as soon as the first Run exists
        code = silence_delete(batch, args.alertmanager, args.silence_id)
        if code != 200:
            raise SystemExit(f"silence delete: HTTP {code}")
        box_end = datetime.fromisoformat(time_policy(first["input"])["window"]["end"])
        attach_deadline = time.monotonic() + args.attach_wait
        episode = None
        while time.monotonic() < attach_deadline:
            state = incident_state(conn, incident_id)
            episode = first_attached_episode(list(state["deliveries"]), box_end)
            if episode is not None:
                break
            time.sleep(args.every)
        if episode is None:
            append(
                batch,
                {
                    "kind": "environment_timeout",
                    "waiting_for": "attached alert episode",
                },
            )
            return ENVIRONMENT_TIMEOUT
        append(
            batch,
            {
                "kind": "episode_attached",
                "delivery_id": episode["delivery_id"],
                "received_at": episode["received_at"],
            },
        )
        generation = int(state["incident"]["control_generation"])
    # cancel right after the attach, then new_run (Q11)
    cancel = _control(incident_id, "cancel", generation, f"r358-cancel-{args.round}")
    append(batch, {"kind": "control", **cancel})
    if cancel["http_status"] != 200:
        raise SystemExit(f"cancel refused: {cancel}")
    new_gen = int(cancel["response"]["generation"])
    renew = _control(incident_id, "new_run", new_gen, f"r358-new-run-{args.round}")
    append(batch, {"kind": "control", **renew})
    if renew["http_status"] != 200:
        raise SystemExit(f"new_run refused: {renew}")
    print(
        json.dumps(
            {"incident_id": incident_id, "successor": renew["response"].get("run_id")}
        )
    )
    return 0


# -- collect -----------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def trace_links(project: str | None, run_id: str) -> dict[str, Any]:
    """LangSmith root runs of the successor (metadata ``run_id``), read back
    best effort; ``None`` project means tracing was not on."""
    if not project:
        return {"project": None, "read_back": "not_exported", "roots": []}
    from scripts.lab_evidence import langsmith_client, run_url

    try:
        client = langsmith_client()
        roots = list(
            client.list_runs(
                project_name=project,
                is_root=True,
                filter=f'and(eq(metadata_key, "run_id"), eq(metadata_value, "{run_id}"))',
                limit=10,
            )
        )
        session = client.read_project(project_name=project)
        return {
            "project": project,
            "read_back": "found" if roots else "root_run_not_found",
            "roots": [
                {
                    "langsmith_run_id": str(r.id),
                    "url": run_url(
                        client, project_id=str(session.id), langsmith_run_id=str(r.id)
                    ),
                }
                for r in roots
            ],
        }
    except Exception as exc:  # noqa: BLE001 - recorded, the summary still freezes
        return {
            "project": project,
            "read_back": f"error:{type(exc).__name__}",
            "roots": [],
        }


def summarize(
    state: Mapping[str, Any],
    first_steps: list[Mapping[str, Any]],
    successor_steps: list[Mapping[str, Any]],
    run_seconds: float,
) -> dict[str, Any]:
    """Everything ``collect`` freezes that comes from the database."""
    runs = state["runs"]
    if len(runs) < 2:
        raise SystemExit("no successor Run on the incident")
    first, successor = runs[0], runs[-1]
    link = state["link"]
    box = box_assertions(
        first, successor, link["released_at"], link["consumed_at"], run_seconds
    )
    first_end = datetime.fromisoformat(time_policy(first["input"])["window"]["end"])
    episode = first_attached_episode(list(state["deliveries"]), first_end)
    if episode is None:
        raise SystemExit("no attached episode after the first Run's box")
    successor_records = gateway_records(successor_steps)
    queries = episode_queries(successor_records, first_end, episode["received_at"])
    received = episode["received_at"].astimezone(timezone.utc).replace(microsecond=0)
    if queries["queries"]:
        probe = Window.parse(queries["queries"][0]["window"])
        probe_source = "successor's first metrics query covering the episode"
    else:
        probe = Window(received - timedelta(minutes=5), received + timedelta(seconds=1))
        probe_source = "synthetic 5 min window ending just after the episode receipt"
    assert probe is not None
    offline = {
        "probe_window_source": probe_source,
        "first_run": gateway_judgment(first["input"], probe),
        "successor": gateway_judgment(successor["input"], probe),
    }
    offline["first_run_refused"] = (
        offline["first_run"]["status"],
        offline["first_run"]["reason"],
        offline["first_run"]["requests_built"],
    ) == ("denied", "WINDOW_OUT_OF_SCOPE", 0)
    offline["successor_admitted"] = (
        offline["successor"]["reason"] != "WINDOW_OUT_OF_SCOPE"
        and offline["successor"]["status"] != "denied"
    )
    return _jsonable(
        {
            "incident_id": state["incident"]["incident_id"],
            "observation_id": link["observation_id"],
            "released_at": link["released_at"],
            "consumed_at": link["consumed_at"],
            "first_run": {
                k: first[k]
                for k in ("run_id", "state", "budget_spent", "tool_operations_used")
            },
            "successor": {
                k: successor[k]
                for k in ("run_id", "state", "budget_spent", "tool_operations_used")
            },
            "versions": successor["versions"],
            "box": box,
            "episode": {
                "delivery_id": episode["delivery_id"],
                "status": episode["status"],
                "outcome": episode["outcome"],
                "received_at": episode["received_at"],
                "starts_at": episode["starts_at"],
                "after_first_box_end": received > first_end,
            },
            "deliveries": [
                {k: d[k] for k in ("delivery_id", "status", "outcome", "received_at")}
                for d in state["deliveries"]
            ],
            "controls": state["controls"],
            "successor_episode_queries": queries,
            "gateway_records": {
                "first_run": gateway_records(first_steps),
                "successor": successor_records,
            },
            "offline_gateway_judgment": offline,
        }
    )


def collect(args: argparse.Namespace) -> int:
    with connect() as conn:
        state = incident_state(conn, args.incident)
        runs = state["runs"]
        first_steps = run_steps(conn, runs[0]["run_id"]) if runs else []
        successor_steps = run_steps(conn, runs[-1]["run_id"]) if len(runs) > 1 else []
    summary = summarize(state, first_steps, successor_steps, args.run_seconds)
    root = ledger(args.batch)
    raw = sorted(p for p in root.rglob("*") if p.is_file())
    summary.update(
        schema="opspilot-358-release-successor-1",
        code_revision=args.code_revision,
        trace=trace_links(args.trace_project, summary["successor"]["run_id"]),
        raw_ledger={
            "location": f"tmp/lab-ledgers/{args.batch}/ (not committed)",
            "files": {
                str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in raw
            },
        },
    )
    text = json.dumps(summary, indent=1, sort_keys=True, ensure_ascii=False)
    if re.search(r"/Users/|Bearer |password", text):
        raise SystemExit("summary would contain a local path or credential marker")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(text + "\n")
    print(
        json.dumps(
            {
                "box_ok": summary["box"]["ok"],
                "episode_queries": summary["successor_episode_queries"]["observed"],
                "first_run_refused": summary["offline_gateway_judgment"][
                    "first_run_refused"
                ],
                "successor_admitted": summary["offline_gateway_judgment"][
                    "successor_admitted"
                ],
                "trace": summary["trace"]["read_back"],
            }
        )
    )
    return 0


def trace_project(round_name: str) -> dict[str, Any]:
    """``opspilot-lab-<round>`` with the longest retention (ADR-0006); the
    same fail-closed lab-target check as the tracer, zero calls on failure."""
    from scripts.lab_evidence import langsmith_client, prepare_lab_project
    from scripts.m1_live_flash_loop import load_langsmith_env

    load_langsmith_env(Path(os.environ["OPSPILOT_ENV_FILE"]))
    env = {**os.environ, "OPSPILOT_TRACE": "lab"}
    project = prepare_lab_project(env, round_name, client_factory=langsmith_client)
    return {"project": env["LANGSMITH_PROJECT"], **(project or {})}


PLAN = """#358 window (after F14), same kind lab, ~1.5 h:
 0. occupation claim; swap/frontend span rate/flagd off/no open checkout incident
 1. web + worker (OPSPILOT_TRACE=lab, LANGSMITH_PROJECT=opspilot-lab-358-live-<date>)
    + Observer from this worktree, one-off PG; Alertmanager and flagd forwarded
 2. silence-create (alertname=CheckoutPlaceOrderErrorRatioHigh, 2 h)
 3. f11_u2_harness release/watch/webhook; fault --offset 300 --duration 2400
 4. drive --observation <id> --silence-id <id>
    (first Run seen -> delete silence -> attached -> cancel -> new_run)
 5. wait for the successor Run to end; fault restores itself (harness finally)
 6. collect --incident <id> --out docs/evidence/358-release-successor-live/summary.json
Expectations: box end = successor creation (deadline - run_seconds), anchor =
released_at; first-Run judgment denied WINDOW_OUT_OF_SCOPE offline; episode
query 'observed' or '未观察到' (not a failure)."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="r358_live", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("--batch", default=BATCH)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    tp = sub.add_parser("trace-project")
    tp.add_argument("--round", required=True)
    create = sub.add_parser("silence-create")
    create.add_argument("--alertmanager", default=ALERTMANAGER)
    create.add_argument("--hours", type=float, default=2.0)
    delete = sub.add_parser("silence-delete")
    delete.add_argument("--alertmanager", default=ALERTMANAGER)
    delete.add_argument("--silence-id", required=True)
    d = sub.add_parser("drive")
    d.add_argument("--observation", required=True)
    d.add_argument("--silence-id", required=True)
    d.add_argument("--alertmanager", default=ALERTMANAGER)
    d.add_argument("--round", default="1")
    d.add_argument("--incident-wait", type=float, default=1800)
    d.add_argument("--attach-wait", type=float, default=900)
    d.add_argument("--every", type=float, default=2.0)
    c = sub.add_parser("collect")
    c.add_argument("--incident", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--run-seconds", type=float, required=True)
    c.add_argument("--code-revision", required=True)
    c.add_argument("--trace-project", default=None)
    args = parser.parse_args(argv)
    if args.command == "plan":
        print(PLAN)
        return 0
    if args.command == "trace-project":
        print(json.dumps(trace_project(args.round)))
        return 0
    if args.command == "silence-create":
        print(silence_create(args.batch, args.alertmanager, args.hours))
        return 0
    if args.command == "silence-delete":
        return (
            0
            if silence_delete(args.batch, args.alertmanager, args.silence_id) == 200
            else 1
        )
    if args.command == "drive":
        return drive(args)
    return collect(args)


if __name__ == "__main__":
    sys.exit(main())
