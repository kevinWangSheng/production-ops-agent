"""Freeze the #245 PR-1 bounded real Run (not product code).

Reads the lab database read-only with the owner DSN, the pages and control
records ``ctl.py`` saved under ``tmp/f245-live/out``, and the LangSmith root
runs of the round's project; writes the raw ledger to
``tmp/f245-live/ledger.json`` and the frozen summary (checks, hashes, trace
links, balance) next to this file (ADR-0006).

    collect.py <executed_commit>
"""

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from scripts import lab_evidence  # noqa: E402

HERE = Path(__file__).parent
RUN = ROOT / "tmp" / "f245-live"
OUT = RUN / "out"
DSN = "host=127.0.0.1 port=55701 dbname=f245_live user=m0_lab"
A, B = "checkout-misbound", "m0-otel-20260909"


def rows(sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    with psycopg.connect(DSN, row_factory=dict_row) as c:
        c.execute("SET TRANSACTION READ ONLY")
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def check(name: str, ok: bool, observed: Any) -> dict[str, Any]:
    return {"check": name, "pass": bool(ok), "observed": jsonable(observed)}


def main(executed_commit: str) -> None:
    incident = (RUN / "incident").read_text().strip()
    act = {
        n: json.loads((OUT / f"{n}.json").read_text())
        for n in ("s0-intake", "s1-register", "s2-rebind", "s3-follow-up")
    }
    pages = {p.stem: p.read_text() for p in sorted(OUT.glob("p*.html"))}
    old_run = act["s0-intake"]["http_body"]["run_id"]
    new_run = act["s3-follow-up"]["http_body"]["run_id"]
    data = {
        "incident": rows(
            "SELECT i.*,t.resource_uid AS target FROM opspilot_incidents i LEFT JOIN opspilot_targets t USING (target_id) WHERE incident_id=%s",
            (incident,),
        ),
        "runs": rows(
            "SELECT run_id,state,control_generation,input,versions,deadline FROM opspilot_runs WHERE incident_id=%s ORDER BY control_generation",
            (incident,),
        ),
        "controls": rows(
            "SELECT action,expected_generation,resulting_generation,actor,payload,created_at FROM opspilot_controls WHERE incident_id=%s ORDER BY resulting_generation",
            (incident,),
        ),
        "sessions": rows(
            "SELECT s.session_id,s.state,s.ended_reason,t.resource_uid AS target FROM opspilot_observation_sessions s JOIN opspilot_targets t USING (target_id) WHERE incident_id=%s",
            (incident,),
        ),
        "endings": rows(
            "SELECT ended_reason,lifecycle_before,lifecycle_after,recorded_at FROM opspilot_observation_endings WHERE incident_id=%s",
            (incident,),
        ),
        "steps": rows(
            "SELECT run_id,sequence,status,jsonb_array_length(COALESCE(tool_results,'[]'::jsonb)) AS tools FROM opspilot_steps WHERE run_id IN (%s,%s) ORDER BY run_id,sequence",
            (old_run, new_run),
        ),
        "evidence": rows(
            "SELECT run_id,status,view->>'target_id' AS target,view->>'source' AS source FROM opspilot_evidence WHERE run_id IN (%s,%s)",
            (old_run, new_run),
        ),
        "postmortem_attempts": rows(
            "SELECT status,error_code,model_requests FROM opspilot_postmortem_generation_attempts WHERE incident_id=%s",
            (incident,),
        ),
    }
    inc = data["incident"][0]
    runs = {str(r["run_id"]): r for r in data["runs"]}
    rebind = [c for c in data["controls"] if c["action"] == "rebind_target"]
    new_input = runs[new_run]["input"]
    intake_question = (
        "Checkout PlaceOrder errors since the last deploy: what is failing and why?"
    )
    new_evidence = [e for e in data["evidence"] if str(e["run_id"]) == new_run]
    new_steps = [s for s in data["steps"] if str(s["run_id"]) == new_run]
    old_input = runs[old_run]["input"]
    checks = [
        check(
            "intake bound the incident to A",
            act["s0-intake"]["after"]["incident"][0]["target"] == A
            and old_input["bound_target_id"] == A,
            {
                "target": act["s0-intake"]["after"]["incident"][0]["target"],
                "first_run_bound_target_id": old_input["bound_target_id"],
            },
        ),
        check(
            "register_remediation authorized an observation on A (observing_recovery)",
            act["s1-register"]["http_status"] == 200
            and act["s1-register"]["after"]["incident"][0]["lifecycle"]
            == "observing_recovery"
            and [s["state"] for s in act["s1-register"]["after"]["sessions"]]
            == ["authorized"],
            act["s1-register"]["after"],
        ),
        check(
            "rebind via the workbench form: 200, generation +1, no Run started",
            act["s2-rebind"]["http_status"] == 200
            and act["s2-rebind"]["http_body"]["generation"]
            == act["s2-rebind"]["before"]["incident"][0]["control_generation"] + 1
            and "run_id" not in act["s2-rebind"]["http_body"]
            and len(act["s2-rebind"]["after"]["runs"]) == 1,
            act["s2-rebind"]["http_body"],
        ),
        check(
            "after rebind: target B, lifecycle open, old Run cancelled, session revoked",
            act["s2-rebind"]["after"]["incident"][0]["target"] == B
            and act["s2-rebind"]["after"]["incident"][0]["lifecycle"] == "open"
            and [r["state"] for r in act["s2-rebind"]["after"]["runs"]] == ["cancelled"]
            and [
                (s["state"], s["ended_reason"])
                for s in act["s2-rebind"]["after"]["sessions"]
            ]
            == [("revoked", "authority_revoked")],
            act["s2-rebind"]["after"],
        ),
        check(
            "observation ending recorded from observing_recovery (authority_revoked)",
            [(e["ended_reason"], e["lifecycle_before"]) for e in data["endings"]]
            == [("authority_revoked", "observing_recovery")],
            data["endings"],
        ),
        check(
            "audit row: rebind_target by the operator with from A / to B",
            len(rebind) == 1
            and rebind[0]["actor"] == "f245-lab"
            and rebind[0]["payload"]["from_target"] == A
            and rebind[0]["payload"]["to_target"] == B,
            [
                {k: v for k, v in c.items() if k != "payload"}
                | {
                    "payload": {
                        k: v for k, v in c["payload"].items() if k != "idempotency_key"
                    }
                }
                for c in rebind
            ],
        ),
        check(
            "page shows the current target B and the rebind record",
            f'<code id="current-target">{B}</code>' in pages["p2-rebound"]
            and f"Rebound from <code>{A}</code> to <code>{B}</code>"
            in pages["p2-rebound"],
            None,
        ),
        check(
            "follow_up started a new Run; the old Run stays cancelled",
            new_run != old_run
            and runs[old_run]["state"] == "cancelled"
            and act["s3-follow-up"]["http_status"] == 200,
            {"old": runs[old_run]["state"], "new_run": new_run},
        ),
        check(
            "new Run input is bound to B: bound_target_id and target_ids",
            new_input["bound_target_id"] == B
            and new_input["scope_facts"]["target_ids"] == [B],
            {
                "bound_target_id": new_input["bound_target_id"],
                "target_ids": new_input["scope_facts"]["target_ids"],
            },
        ),
        check(
            "new Run input is fresh, not a continuation (intake question, no handoff note)",
            new_input["question"] == intake_question,
            {"question": new_input["question"]},
        ),
        check(
            "new Run's read-only queries ran against B and returned data",
            bool(new_evidence)
            and {e["target"] for e in new_evidence} == {B}
            and any(e["status"] == "ok" for e in new_evidence),
            {
                "evidence": len(new_evidence),
                "by_status": {
                    s: sum(1 for e in new_evidence if e["status"] == s)
                    for s in sorted({e["status"] for e in new_evidence})
                },
                "targets": sorted({e["target"] for e in new_evidence}),
                "tool_steps": sum(1 for s in new_steps if s["tools"]),
            },
        ),
        check(
            "new Run completed and its conclusion is published on the incident",
            runs[new_run]["state"] == "completed"
            and inc["conclusion"] is not None
            and str(inc["current_run_id"]) == new_run,
            {"state": runs[new_run]["state"], "current_run_id": inc["current_run_id"]},
        ),
        check(
            "the old Run on A never ran (no steps, no evidence)",
            not [s for s in data["steps"] if str(s["run_id"]) == old_run]
            and not [e for e in data["evidence"] if str(e["run_id"]) == old_run],
            None,
        ),
    ]
    from opspilot.worker_main import _read_env_file

    os.environ["LANGSMITH_API_KEY"] = (
        _read_env_file(Path(os.environ["OPSPILOT_ENV_FILE"]), "LANGSMITH_API_KEY") or ""
    )
    project = json.loads((RUN / "project.json").read_text())
    traces: list[dict[str, Any]] = []
    try:
        client = lab_evidence.langsmith_client(os.environ)
        for r in client.list_runs(
            project_name=project["project"], is_root=True, limit=100
        ):
            md = (r.extra or {}).get("metadata", {})
            traces.append(
                {
                    "name": r.name,
                    "incident_id": md.get("incident_id"),
                    "run_id": md.get("run_id"),
                    "start_time": str(r.start_time),
                    "url": lab_evidence.run_url(
                        client,
                        project_id=project["project_id"],
                        langsmith_run_id=str(r.id),
                    ),
                }
            )
    except Exception as exc:  # noqa: BLE001
        traces.append({"error": type(exc).__name__})
    traces.sort(key=lambda t: t.get("start_time", ""))
    checks.append(
        check(
            "a LangSmith root trace exists for the new Run",
            any(t.get("run_id") == new_run for t in traces),
            [t.get("run_id") for t in traces],
        )
    )
    ledger = {"rows": data, "actions": act, "pages": pages}
    raw = json.dumps(jsonable(ledger), indent=1, sort_keys=True).encode()
    (RUN / "ledger.json").write_bytes(raw)
    summary = {
        "experiment": "f12-245-rebind-20261027",
        "executed_commit": executed_commit,
        "incident_id": incident,
        "targets": {"A": A, "B": B},
        "runs": {"old": old_run, "new": new_run},
        "checks": checks,
        "all_pass": all(c["pass"] for c in checks),
        "pages": {k: hashlib.sha256(v.encode()).hexdigest() for k, v in pages.items()},
        "postmortem_attempts": jsonable(data["postmortem_attempts"]),
        "trace": {"project": project, "roots": traces},
        "balance": [
            json.loads(x)
            for x in (RUN / "balance.log").read_text().splitlines()
            if x.strip()
        ],
        "raw_ledger": {
            "path": "tmp/f245-live/ledger.json",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
        },
    }
    (HERE / "summary.json").write_text(
        json.dumps(jsonable(summary), indent=1, sort_keys=True) + "\n"
    )
    print(
        json.dumps(
            {
                "all_pass": summary["all_pass"],
                "failed": [c["check"] for c in checks if not c["pass"]],
            }
        )
    )


if __name__ == "__main__":
    main(sys.argv[1])
