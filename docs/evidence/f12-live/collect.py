"""Engineer-side freeze for the F12 live run (not product code; read-only).

    collect.py --owner-dsn ... --pages <dir> --project-file <pre.json>
               --executed-commit <sha> --timeline <file> --balance <file>
               --incident-a <uuid> --incident-b <uuid> --out <evidence dir>

Reads the product rows of the two incidents in a read-only session, the page
HTML saved during the run (lab helper ``ctl.py``) and the LangSmith root runs
of the lab project. The raw ledger (rows, pages, control responses) goes to the
ignored ``tmp/lab-ledgers/f12-live`` via ``lab_evidence.freeze``; summary.json
keeps hashes, the per-step checks and trace links (ADR-0006).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from scripts import lab_evidence  # noqa: E402

TABLES = {
    "incident": "SELECT incident_id,lifecycle,state,mode,control_generation,current_run_id,conclusion IS NOT NULL AS concluded FROM opspilot_incidents WHERE incident_id=%s",
    "runs": "SELECT run_id,state,control_generation,deadline,input FROM opspilot_runs WHERE incident_id=%s ORDER BY control_generation,run_id",
    "steps": "SELECT s.run_id,s.sequence,s.control_generation,s.status,s.observed_at,s.logical_key,md5(coalesce(s.response::text,'')) AS response_md5 FROM opspilot_steps s JOIN opspilot_runs r USING(run_id) WHERE r.incident_id=%s ORDER BY s.observed_at",
    "controls": "SELECT action,actor,expected_generation,resulting_generation,created_at,payload->>'text' AS text,payload->>'revision' AS revision FROM opspilot_controls WHERE incident_id=%s ORDER BY resulting_generation",
    "inputs": "SELECT sequence,kind,actor,control_generation,content->>'text' AS text FROM opspilot_inputs WHERE incident_id=%s ORDER BY sequence",
    "alert_deliveries": "SELECT delivery_id,status,outcome,fingerprint,starts_at_raw,received_at FROM opspilot_alert_deliveries WHERE incident_id=%s ORDER BY delivery_id",
    "sessions": "SELECT session_id,state,ended_reason,authorization_source,authorized_at,updated_at FROM opspilot_observation_sessions WHERE incident_id=%s ORDER BY authorized_at",
    "endings": "SELECT ended_reason,transition,lifecycle_before,lifecycle_after,recorded_at FROM opspilot_observation_endings WHERE incident_id=%s ORDER BY recorded_at",
    "postmortem_versions": "SELECT v.version,v.state FROM opspilot_postmortem_versions v JOIN opspilot_postmortems p USING(postmortem_id) WHERE p.incident_id=%s ORDER BY v.version",
}


def rows(conn, incident: str) -> dict[str, Any]:
    return {
        k: [dict(r) for r in conn.execute(q, (incident,)).fetchall()]
        for k, q in TABLES.items()
    }


def page_text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def badge(html: str, ident: str) -> str | None:
    m = re.search(rf'id="{ident}">([^<]*)<', html)
    return m.group(1) if m else None


def buttons(html: str) -> list[str]:
    return sorted(set(re.findall(r'name="action" value="([a-z_]+)"', html)))


def check(name: str, ok: bool, detail: Any) -> dict[str, Any]:
    return {"check": name, "pass": bool(ok), "detail": detail}


def main() -> int:
    p = argparse.ArgumentParser()
    for a in ("owner-dsn", "executed-commit", "incident-a", "incident-b"):
        p.add_argument(f"--{a}", required=True)
    for a in ("pages", "project-file", "timeline", "balance", "out"):
        p.add_argument(f"--{a}", type=Path, required=True)
    a = p.parse_args()
    with psycopg.connect(a.owner_dsn, row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        data = {"A": rows(conn, a.incident_a), "B": rows(conn, a.incident_b)}
    pages = {f.stem: f.read_text() for f in sorted(a.pages.glob("*.html"))}
    actions = {
        f.stem: json.loads(f.read_text()) for f in sorted(a.pages.glob("*.json"))
    }
    A, B = data["A"], data["B"]
    run_state = {str(r["run_id"]): r["state"] for r in A["runs"] + B["runs"]}

    def steps_of(run: str) -> list[dict[str, Any]]:
        return [s for s in A["steps"] + B["steps"] if str(s["run_id"]).startswith(run)]

    def ctl(label: str) -> dict[str, Any]:
        return actions[label]

    corr = ctl("04-correct-hypothesis")
    corr_run = corr["http_body"]["run_id"]
    corr_input = json.dumps(
        next(r["input"] for r in A["runs"] if str(r["run_id"]) == corr_run), default=str
    )
    checks = {
        "step1": [
            check(
                "evidence link resolves to an HTML evidence page",
                "01b-evidence" in pages
                and "Evidence" in page_text(pages["01b-evidence"]),
                len(pages.get("01b-evidence", "")),
            ),
            check(
                "follow_up after a published conclusion: 200, new Run, old Run completed",
                ctl("02-follow-up")["http_status"] == 200
                and run_state[
                    str(ctl("02-follow-up")["before"]["incident"][0]["current_run_id"])
                ]
                == "completed",
                {
                    "new_run": ctl("02-follow-up")["http_body"].get("run_id"),
                    "generation": ctl("02-follow-up")["http_body"].get("generation"),
                },
            ),
            check(
                "new Run published an updated conclusion",
                run_state[ctl("02-follow-up")["http_body"]["run_id"]] == "completed",
                None,
            ),
            check(
                "page after the update shows Investigation history with the prior Run",
                "Investigation history" in page_text(pages["03-second-conclusion"])
                and str(ctl("02-follow-up")["before"]["incident"][0]["current_run_id"])
                in pages["03-second-conclusion"],
                None,
            ),
        ],
        "step2": [
            check(
                "correct of a hypothesis: 200 and a new Run",
                corr["http_status"] == 200 and bool(corr_run),
                corr["http_body"],
            ),
            check(
                "audit and input rows carry the correction",
                any(
                    c["action"] == "correct" and c["text"] == corr["text"]
                    for c in A["controls"]
                )
                and any(
                    i["kind"] == "correct" and i["text"] == corr["text"]
                    for i in A["inputs"]
                ),
                None,
            ),
            check(
                "successor input carries the prior conclusion labelled not instructions",
                "not instructions" in corr_input.lower(),
                None,
            ),
            check(
                "corrected Run published; the prior conclusions stay in history",
                run_state[corr_run] == "completed"
                and "Investigation history" in page_text(pages["07-third-conclusion"]),
                None,
            ),
        ],
        "step3": [
            check(
                f"{lbl} accepted with operator and generation",
                ctl(lbl)["http_status"] == 200
                and any(
                    c["action"] == act
                    and c["resulting_generation"] == ctl(lbl)["http_body"]["generation"]
                    and c["actor"]
                    for c in (A if not lbl.startswith("B") else B)["controls"]
                ),
                {
                    "generation": ctl(lbl)["http_body"].get("generation"),
                    "after": ctl(lbl)["after"]["incident"][0],
                },
            )
            for lbl, act in (
                ("05-pause", "pause"),
                ("06-resume", "resume"),
                ("09-cancel", "cancel"),
                ("11-takeover-mid-run", "takeover"),
                ("15-close", "close"),
                ("17-reopen", "reopen"),
                ("B02-close-running", "close"),
                ("B04-reopen", "reopen"),
            )
        ]
        + [
            check(
                "every human decision row has a database timestamp",
                all(c["created_at"] for c in A["controls"] + B["controls"]),
                len(A["controls"]) + len(B["controls"]),
            ),
            check(
                "page badges after pause / close / reopen",
                badge(pages["05b-paused"], "incident-control") == "paused"
                and badge(pages["15b-closed"], "incident-lifecycle") == "closed"
                and badge(pages["17b-reopened"], "incident-lifecycle") == "open",
                {
                    "paused": badge(pages["05b-paused"], "incident-control"),
                    "closed": badge(pages["15b-closed"], "incident-lifecycle"),
                    "reopened": badge(pages["17b-reopened"], "incident-lifecycle"),
                },
            ),
            check(
                "closed page offers reopen and not close; reopened page offers close and not reopen",
                "reopen" in buttons(pages["15b-closed"])
                and "close" not in buttons(pages["15b-closed"])
                and "close" in buttons(pages["17b-reopened"])
                and "reopen" not in buttons(pages["17b-reopened"]),
                {
                    "closed": buttons(pages["15b-closed"]),
                    "reopened": buttons(pages["17b-reopened"]),
                },
            ),
            check(
                "A: close during observation revoked the session and cancelled the waiting_human Run",
                any(
                    s["authorization_source"] == "post_remediation"
                    and s["state"] == "revoked"
                    for s in A["sessions"]
                )
                and ctl("15-close")["before"]["runs"][-1]["state"] == "waiting_human"
                and ctl("15-close")["after"]["runs"][-1]["state"] == "cancelled",
                ctl("15-close")["after"]["sessions"],
            ),
            check(
                "A: new_run on the closed incident refused REOPEN_REQUIRED, nothing written",
                ctl("16-new-run-on-closed")["http_status"] == 409
                and ctl("16-new-run-on-closed")["http_body"].get("code")
                == "REOPEN_REQUIRED"
                and ctl("16-new-run-on-closed")["after"]
                == ctl("16-new-run-on-closed")["before"],
                None,
            ),
            check(
                "A: reopen started no Run and no observation",
                len(ctl("17-reopen")["after"]["runs"])
                == len(ctl("17-reopen")["before"]["runs"])
                and all(
                    s["state"] != "authorized"
                    for s in ctl("17-reopen")["after"]["sessions"]
                ),
                None,
            ),
            check(
                "B: close of a running automatic Run cancelled it; its later write is late_result only",
                ctl("B02-close-running")["after"]["runs"][0]["state"] == "cancelled"
                and [
                    s["status"]
                    for s in steps_of(
                        str(ctl("B02-close-running")["after"]["runs"][0]["run_id"])
                    )
                    if s["control_generation"] == 0
                ][-1]
                == "late_result"
                and B["incident"][0]["concluded"] is True,
                [
                    (s["sequence"], s["status"])
                    for s in steps_of(
                        str(ctl("B02-close-running")["after"]["runs"][0]["run_id"])
                    )
                ],
            ),
            check(
                "B: follow_up on the closed incident refused REOPEN_REQUIRED",
                ctl("B03-follow-up-on-closed")["http_status"] == 409
                and ctl("B03-follow-up-on-closed")["http_body"].get("code")
                == "REOPEN_REQUIRED",
                None,
            ),
            check(
                "B: reopen started no Run; the follow_up after it opened one that published",
                len(ctl("B04-reopen")["after"]["runs"]) == 1
                and run_state[ctl("B05-follow-up-after-reopen")["http_body"]["run_id"]]
                == "completed",
                None,
            ),
        ],
        "step4": [
            check(
                "takeover: mode human_owned, Run waiting_human, audit row",
                ctl("11-takeover-mid-run")["after"]["incident"][0]["mode"]
                == "human_owned"
                and ctl("11-takeover-mid-run")["after"]["runs"][-1]["state"]
                == "waiting_human",
                None,
            ),
            check(
                "(1) worker dispatched before takeover: its later round is late_result only, no publish",
                [
                    s["status"]
                    for s in steps_of(
                        str(ctl("11-takeover-mid-run")["after"]["runs"][-1]["run_id"])
                    )
                ][-1]
                == "late_result",
                [
                    (s["sequence"], s["control_generation"], s["status"])
                    for s in steps_of(
                        str(ctl("11-takeover-mid-run")["after"]["runs"][-1]["run_id"])
                    )
                ],
            ),
            check(
                "(2) same firing alert delivered again (hand redelivery): replayed, no new incident or Run",
                [d["outcome"] for d in A["alert_deliveries"]][:2]
                == ["created", "replayed"]
                and ctl("13-fact-under-takeover")["before"]["runs"]
                == ctl("11-takeover-mid-run")["after"]["runs"],
                [
                    (d["delivery_id"], d["status"], d["outcome"])
                    for d in A["alert_deliveries"]
                ],
            ),
            check(
                "(3) fact under human ownership: recorded, Run and mode unchanged",
                ctl("13-fact-under-takeover")["http_status"] == 200
                and ctl("13-fact-under-takeover")["after"]["runs"]
                == ctl("13-fact-under-takeover")["before"]["runs"]
                and ctl("13-fact-under-takeover")["after"]["incident"][0]["mode"]
                == "human_owned"
                and any(
                    i["kind"] == "correct"
                    and i["text"] == ctl("13-fact-under-takeover")["text"]
                    for i in A["inputs"]
                ),
                None,
            ),
            check(
                "handoff kept: takeover audit row still present at the end",
                any(c["action"] == "takeover" for c in A["controls"])
                and A["incident"][0]["mode"] == "human_owned",
                None,
            ),
        ],
    }
    os.environ.setdefault("LANGSMITH_API_KEY", "")
    project = json.loads(a.project_file.read_text())
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
    ledger = {
        "rows": data,
        "pages": pages,
        "actions": actions,
        "timeline": a.timeline.read_text(),
        "balance": a.balance.read_text(),
    }
    verdicts = {k: all(c["pass"] for c in v) for k, v in checks.items()}
    summary = {
        "versions": {"executed_commit": a.executed_commit},
        "incidents": {"A": a.incident_a, "B": a.incident_b},
        "checks": checks,
        "verdicts": verdicts,
        "all_steps_pass": all(verdicts.values()),
        "pages": {k: hashlib.sha256(v.encode()).hexdigest() for k, v in pages.items()},
        "trace": {"project": project, "roots": traces},
        "balance": [
            json.loads(x) for x in a.balance.read_text().splitlines() if x.strip()
        ],
    }
    frozen, path = lab_evidence.freeze(
        ledger,
        experiment="f12-live",
        run_id=a.incident_a,
        evidence_dir=a.out,
        summary=summary,
    )
    print(
        json.dumps(
            {
                "summary": str(path),
                "verdicts": verdicts,
                "failed": [c for v in checks.values() for c in v if not c["pass"]],
            },
            indent=2,
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
