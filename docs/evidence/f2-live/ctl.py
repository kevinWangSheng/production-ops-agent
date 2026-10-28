"""Lab helper for the F2 PR-E kind session (not product code).

    ctl.py pre <label>                       lab readings before a scenario
    ctl.py intake <label> --question Q       POST /intake/ui (workbench form)
    ctl.py act <incident> <action> <label> [--text T]
                                             POST /incidents/{id}/control
    ctl.py page <incident> <label>           save the incident page HTML
    ctl.py state <incident>                  incident, Runs, deliveries (read-only)
    ctl.py inflight <run_id> <action> <label> [--hold 1.0] [--timeout 600]
                                             wait until one model reservation
                                             stays unsettled >= hold seconds,
                                             then post <action> on its incident
    ctl.py threads <pid> <label> [--seconds N]
                                             sample ``ps -M`` thread counts
    ctl.py balance <label>                   DeepSeek CNY balance (key never printed)
    ctl.py authorize <observation_id> <label>
                                             authorize a release observation with
                                             the calibrated catalog revision, as
                                             the web login role (F2_LIVE_WEB_DSN;
                                             no release page before F11 PR-5)

Environment (paths only; no value is printed or written to the ledger):
F2_LIVE_DSN (owner DSN, every read in a READ ONLY transaction), F2_LIVE_WEB,
F2_LIVE_LEDGER (git-ignored ledger dir), F2_LIVE_UI_FILE (``user:password``),
F2_LIVE_SECRETS (private env file), KUBECONFIG (lab kubeconfig) and the lab
Prometheus account (``prometheus-lab.env``) for ``pre``.
Control requests carry the same fields as the page form and its Origin.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
from psycopg.rows import dict_row

DSN = os.environ["F2_LIVE_DSN"]
BASE = os.environ.get("F2_LIVE_WEB", "http://127.0.0.1:8080")
LEDGER = Path(os.environ["F2_LIVE_LEDGER"])
PROMETHEUS = os.environ.get("F2_LIVE_PROMETHEUS", "http://127.0.0.1:19090")
TARGET = "m0-otel-20260909"
P95 = 'histogram_quantile(0.95, sum by (le) (rate(traces_span_metrics_duration_milliseconds_bucket{k8s_namespace_name="otel-demo",service_name="checkout",span_kind="SPAN_KIND_SERVER",span_name=~".*CheckoutService/PlaceOrder"}[5m])))'
FRONTEND_RATE = (
    'sum(rate(traces_span_metrics_calls_total{service_name="frontend"}[2m]))'
)
ALERTS = 'ALERTS{alertname="CheckoutPlaceOrderErrorRatioHigh"}'


def now() -> str:
    # Engineer script: local send/observe time, not a product clock.
    return datetime.now(timezone.utc).isoformat()  # noqa: TID251


def out(label: str, payload: dict[str, Any]) -> None:
    LEDGER.mkdir(parents=True, exist_ok=True)
    (LEDGER / f"{label}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    )


def auth() -> dict[str, str]:
    user, pw = Path(os.environ["F2_LIVE_UI_FILE"]).read_text().strip().split(":", 1)
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"authorization": "Basic " + token, "origin": BASE}


def read(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute(sql, params).fetchall()
    return [
        {
            k: (v if isinstance(v, (int, float, bool, type(None))) else str(v))
            for k, v in r.items()
        }
        for r in rows
    ]


def state(incident: str) -> dict[str, Any]:
    return {
        "db_now": read("SELECT clock_timestamp() AS t")[0]["t"],
        "incident": read(
            "SELECT control_generation,lifecycle,state,mode,current_run_id,priority,"
            "conclusion IS NOT NULL AS concluded FROM opspilot_incidents WHERE incident_id=%s",
            (incident,),
        ),
        "runs": read(
            "SELECT run_id,state,epoch,control_generation,budget_spent,budget_reserved,"
            "tool_operations_used,deadline FROM opspilot_runs WHERE incident_id=%s "
            "ORDER BY control_generation,run_id",
            (incident,),
        ),
        "sessions": read(
            "SELECT session_id,state,ended_reason,authorization_source "
            "FROM opspilot_observation_sessions WHERE incident_id=%s ORDER BY authorized_at",
            (incident,),
        ),
    }


def prom(query: str) -> Any:
    sys.path.insert(0, str(Path.cwd()))
    from scripts.kind_lab import lab_authorization

    header = lab_authorization()
    r = httpx.get(
        f"{PROMETHEUS}/api/v1/query",
        params={"query": query},
        headers={"authorization": header} if header else {},
        timeout=20,
    )
    body = r.json()
    return [
        {"metric": x.get("metric", {}), "value": x["value"][1]}
        for x in body.get("data", {}).get("result", [])
    ]


def flagd() -> dict[str, Any]:
    raw = subprocess.run(
        ["kubectl", "-n", "otel-demo", "get", "cm", "flagd-config", "-o", "json"],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout
    import hashlib

    data = json.loads(raw)["data"]["demo.flagd.json"]
    flags = json.loads(data)["flags"]
    return {
        "sha256": hashlib.sha256(data.encode()).hexdigest(),
        "non_off": {
            k: v["defaultVariant"]
            for k, v in flags.items()
            if v["defaultVariant"] != "off"
        },
    }


def host() -> dict[str, Any]:
    swap = subprocess.run(
        ["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True
    ).stdout.strip()
    mp = subprocess.run(["memory_pressure"], capture_output=True, text=True).stdout
    free = next(
        (
            line.split(":")[-1].strip()
            for line in mp.splitlines()
            if "free percentage" in line
        ),
        None,
    )
    return {"swapusage": swap, "memory_free_percentage": free}


def cmd_pre(a: argparse.Namespace) -> None:
    res = {
        "at": now(),
        "frontend_span_rate_2m": prom(FRONTEND_RATE),
        "checkout_placeorder_p95_ms": prom(P95),
        "alerts": prom(ALERTS),
        "flagd": flagd(),
        "host": host(),
        "fault_active": (
            Path(os.environ["F2_LIVE_LAB_LEDGERS"]) / "fault-active.json"
        ).exists()
        if os.environ.get("F2_LIVE_LAB_LEDGERS")
        else None,
    }
    out(a.label, res)
    print(json.dumps(res, default=str))


def cmd_intake(a: argparse.Namespace) -> None:
    key = f"f2-live-{a.label}-{uuid4().hex[:8]}"
    at = now()
    r = httpx.post(
        f"{BASE}/intake/ui",
        data={"target_id": TARGET, "idempotency_key": key, "question": a.question},
        headers={**auth(), "accept": "application/json"},
        timeout=60,
    )
    try:
        body: Any = r.json()
    except ValueError:
        body = r.text[:500]
    res = {
        "at": at,
        "idempotency_label": a.label,
        "question": a.question,
        "http_status": r.status_code,
        "http_body": body,
    }
    out(a.label, res)
    print(json.dumps(res, default=str))


def post_control(
    incident: str, action: str, label: str, text: str | None
) -> dict[str, Any]:
    before = state(incident)
    data = {
        "action": action,
        "idempotency_key": f"f2-live-{label}-{uuid4().hex[:8]}",
        "expected_generation": str(before["incident"][0]["control_generation"]),
    }
    if text:
        data["text"] = text
    at = now()
    r = httpx.post(
        f"{BASE}/incidents/{incident}/control",
        data=data,
        headers={**auth(), "accept": "application/json"},
        timeout=60,
    )
    try:
        body: Any = r.json()
    except ValueError:
        body = r.text[:500]
    res = {
        "at": at,
        "action": action,
        "idempotency_label": label,
        "text": text,
        "http_status": r.status_code,
        "http_body": body,
        "before": before,
        "after": state(incident),
    }
    out(label, res)
    return res


def cmd_act(a: argparse.Namespace) -> None:
    res = post_control(a.incident, a.action, a.label, a.text)
    print(
        json.dumps(
            {k: res[k] for k in ("at", "action", "http_status", "http_body")}
            | {
                "after": res["after"]["incident"],
                "runs": [(x["run_id"][:8], x["state"]) for x in res["after"]["runs"]],
            },
            default=str,
        )
    )


def cmd_page(a: argparse.Namespace) -> None:
    r = httpx.get(
        f"{BASE}/incidents/{a.incident}",
        headers={**auth(), "accept": "text/html"},
        timeout=30,
    )
    LEDGER.mkdir(parents=True, exist_ok=True)
    (LEDGER / f"{a.label}.html").write_text(r.text)
    print(
        json.dumps(
            {
                "at": now(),
                "page": a.label,
                "status": r.status_code,
                "bytes": len(r.text),
            }
        )
    )


def cmd_state(a: argparse.Namespace) -> None:
    res = state(a.incident)
    res["deliveries"] = read(
        "SELECT received_at,outcome,status,starts_at,fingerprint,annotation_revision FROM opspilot_alert_deliveries "
        "WHERE incident_id=%s ORDER BY received_at",
        (a.incident,),
    )
    print(json.dumps(res, indent=1, default=str))


def cmd_inflight(a: argparse.Namespace) -> None:
    seen: dict[str, float] = {}
    samples = []
    deadline = time.monotonic() + a.timeout
    while time.monotonic() < deadline:
        rows = read(
            "SELECT r.reservation_id,r.state,u.state AS run_state,u.incident_id,clock_timestamp() AS db_now "
            "FROM opspilot_runs u LEFT JOIN opspilot_budget_reservations r ON r.run_id=u.run_id AND r.state='reserved' "
            "WHERE u.run_id=%s",
            (a.run_id,),
        )
        reserved = [r["reservation_id"] for r in rows if r["reservation_id"]]
        mono = time.monotonic()
        samples.append(
            {
                "db_now": rows[0]["db_now"],
                "run_state": rows[0]["run_state"],
                "reserved": reserved,
            }
        )
        for rid in list(seen):
            if rid not in reserved:
                del seen[rid]
        for rid in reserved:
            seen.setdefault(rid, mono)
        if (
            len(reserved) == 1
            and mono - seen[reserved[0]] >= a.hold
            and rows[0]["run_state"] == "running"
        ):
            res = post_control(rows[0]["incident_id"], a.action, a.label, None)
            res["located"] = {
                "reservation_id": reserved[0],
                "held_seconds": round(mono - seen[reserved[0]], 3),
                "samples": samples[-20:],
            }
            res["after_reservations"] = read(
                "SELECT reservation_id,state,seconds FROM opspilot_budget_reservations WHERE run_id=%s ORDER BY reservation_id",
                (a.run_id,),
            )
            out(a.label, res)
            print(
                json.dumps(
                    {
                        "at": res["at"],
                        "action": a.action,
                        "http_status": res["http_status"],
                        "located": res["located"]["reservation_id"],
                        "held": res["located"]["held_seconds"],
                        "after": res["after"]["incident"],
                    },
                    default=str,
                )
            )
            return
        if rows[0]["run_state"] in (
            "completed",
            "cancelled",
            "waiting_human",
            "failed",
            "blocked",
        ):
            break
        time.sleep(0.2)
    out(a.label, {"located": None, "samples": samples[-50:]})
    sys.exit("in-flight point not located")


def cmd_threads(a: argparse.Namespace) -> None:
    rows = []
    end = time.monotonic() + a.seconds
    while True:
        ps = subprocess.run(
            ["ps", "-M", "-p", str(a.pid)], capture_output=True, text=True
        ).stdout
        rows.append({"at": now(), "threads": max(len(ps.splitlines()) - 1, 0)})
        if time.monotonic() >= end:
            break
        time.sleep(a.interval)
    path = LEDGER / f"{a.label}.jsonl"
    with path.open("a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(json.dumps(rows[-1]))


def cmd_balance(a: argparse.Namespace) -> None:
    sys.path.insert(0, str(Path.cwd()))
    from scripts.f2_kill_resume import deepseek_balance, read_env_file

    key = read_env_file(Path(os.environ["F2_LIVE_SECRETS"])).get("DEEPSEEK_API_KEY", "")
    res = {"at": now(), "cny": deepseek_balance(key)}
    with (LEDGER / "balance.jsonl").open("a") as f:
        f.write(json.dumps({"label": a.label, **res}) + "\n")
    print(json.dumps(res))


CALIBRATED_POLICY = "otel-demo-checkout-release@e2ed7b69b96b"


def cmd_authorize(a: argparse.Namespace) -> None:
    from opspilot.observation.releases import ReleaseStore

    store = ReleaseStore(os.environ["F2_LIVE_WEB_DSN"])
    at = now()
    receipt = store.authorize_release(
        UUID(a.observation_id),
        actor="f2live",
        expected_control_generation=0,
        idempotency_key=f"f2-live-{a.label}-{uuid4().hex[:8]}",
        calibrated_policy_revision=CALIBRATED_POLICY,
    )
    store.close()
    res = {
        "at": at,
        "idempotency_label": a.label,
        "policy_revision": CALIBRATED_POLICY,
        "receipt": receipt.model_dump(mode="json"),
    }
    out(a.label, res)
    print(json.dumps(res, default=str))


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("pre")
    s.add_argument("label")
    s = sub.add_parser("intake")
    s.add_argument("label")
    s.add_argument("--question", required=True)
    s = sub.add_parser("act")
    s.add_argument("incident")
    s.add_argument("action")
    s.add_argument("label")
    s.add_argument("--text")
    s = sub.add_parser("page")
    s.add_argument("incident")
    s.add_argument("label")
    s = sub.add_parser("state")
    s.add_argument("incident")
    s = sub.add_parser("inflight")
    s.add_argument("run_id")
    s.add_argument("action")
    s.add_argument("label")
    s.add_argument("--hold", type=float, default=1.0)
    s.add_argument("--timeout", type=float, default=600)
    s = sub.add_parser("threads")
    s.add_argument("pid", type=int)
    s.add_argument("label")
    s.add_argument("--seconds", type=float, default=0)
    s.add_argument("--interval", type=float, default=1.0)
    s = sub.add_parser("balance")
    s.add_argument("label")
    s = sub.add_parser("authorize")
    s.add_argument("observation_id")
    s.add_argument("label")
    a = p.parse_args()
    globals()[f"cmd_{a.cmd}"](a)


if __name__ == "__main__":
    main()
