"""Independent #184 step-3 read-only acceptance checks (contract r6).

Sources: task contract C1/C4/C5/C6/C7 and adopted step-3 prereview D3-09/12,
S1/S2 deterministic criteria. The prereview's guessed values are checked
against public records: expired/deadline_expired ends with
observation_ended_unconfirmed; automatic authorization stays authorized.
F6 actions may contain human_handoff (a system ending), not a human control.
There is no HTTP snapshot JSON route (web/app.py); the rendered snapshot's
recovery_status is exposed as data-status on the visible recovery statement
(templates/incident.html). Actual snapshot fields live in recovery[*],
not at the top level guessed by the step-2 task note. Text is taken only
from that recovery section's visible content. Measured permission extras
(e.g. documented database_temp) are reported; the adopted criterion rejects
human_control, not every extra privilege flag.

This checks committed recovery evidence, not harness ordering, live traffic
between samples, real-model provenance or LangSmith read-back. Those need the
lead's separately frozen experiment/trace evidence; all_pass is scoped to the
checks below, not a declaration that the entire live experiment passed.
No product writes, POSTs, model calls or kind operations.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

# Support the documented direct-file CLI without requiring installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import psycopg  # noqa: E402
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from opspilot.acceptance import (  # noqa: E402
    IncidentScenario,
    RecoveryRecords,
    recovery_outcome,
)
from opspilot.observation.store import ObservationStore  # noqa: E402
from opspilot.observer.replay import replay_history, summary  # noqa: E402

RECOVERED = "recovered, no remediation recorded"


def json_value(value: Any) -> Any:
    """Keep actual values serializable without copying raw telemetry bundles."""
    if isinstance(value, (datetime, UUID)):
        return str(value) if isinstance(value, UUID) else value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(v) for v in value]
    return value


def judge(scenario: str, incident_id: str, records: dict[str, Any]) -> dict[str, Any]:
    """Pure verdicts on already-read records and public projections.

    Input: incident, run, authorizations, sessions (incident_records histories),
    controls, projection (selected RecoveryOutcome fields), replays (public
    replay summaries), page (parsed public recovery section). No I/O here.
    """
    if scenario not in {"S1", "S2"}:
        raise ValueError("scenario must be S1 or S2")
    verdicts: list[dict[str, Any]] = []

    def check(key: str, requirement: str, expected: Any, actual: Any, ok: bool) -> None:
        verdicts.append(
            {
                "id": key,
                "requirement": requirement,
                "expected": json_value(expected),
                "actual": json_value(actual),
                "pass": bool(ok),
            }
        )

    inc, run = records["incident"], records["run"] or {}
    generation = inc["control_generation"]
    auth = [
        a for a in records["authorizations"] if a["control_generation"] == generation
    ]
    histories = records["sessions"]
    current = [
        h for h in histories if h["session"]["subject_control_generation"] == generation
    ]
    session = current[0]["session"] if len(current) == 1 else {}
    endings = current[0]["endings"] if len(current) == 1 else []
    controls = records["controls"]
    p, page, replays = records["projection"], records["page"], records["replays"]
    c1 = {
        "conclusion_present": inc["conclusion_present"],
        "run_state": run.get("state"),
        "current_run_id": inc.get("current_run_id"),
        "run_id": run.get("run_id"),
        "run_incident_id": run.get("incident_id"),
        "incident_generation": generation,
        "run_generation": run.get("control_generation"),
    }
    check(
        "C1",
        "Published current completed Run at incident generation",
        True,
        c1,
        str(inc.get("incident_id")) == incident_id
        and inc["conclusion_present"] is True
        and run.get("state") == "completed"
        and run.get("control_generation") == generation
        and str(run.get("run_id")) == str(inc.get("current_run_id"))
        and str(run.get("incident_id")) == incident_id,
    )
    check(
        "C2",
        "Exactly one authorized decision linked to one current session",
        {"decisions": 1, "sessions": 1, "state": "authorized"},
        {
            "authorizations": auth,
            "session_ids": [h["session"]["session_id"] for h in current],
        },
        len(auth) == len(current) == 1
        and auth[0]["state"] == "authorized"
        and str(auth[0]["session_id"]) == str(session.get("session_id")),
    )
    auto = [
        c
        for c in controls
        if c.get("action") == "auto_authorize_observation"
        and c.get("actor") == "system"
    ]
    check(
        "C3",
        "One system authorization preserves current control generation",
        generation,
        auto,
        len(auto) == 1
        and auto[0]["expected_generation"] == generation
        and auto[0]["resulting_generation"] == generation
        and str((auto[0].get("payload") or {}).get("session_id"))
        == str(session.get("session_id")),
    )
    human = [c for c in controls if c not in auto]
    check("C4", "No human controls anywhere in incident history", [], human, not human)
    source = {
        "authorization_source": session.get("authorization_source"),
        "target": session.get("target"),
        "generation": session.get("subject_control_generation"),
    }
    target = session.get("target") or {}
    check(
        "C5",
        "Automatic session with explicitly unknown revision at current generation",
        {
            "authorization_source": "automatic",
            "revision": None,
            "generation": generation,
        },
        source,
        session.get("authorization_source") == "automatic"
        and "revision" in target
        and target["revision"] is None
        and session.get("subject_control_generation") == generation,
    )
    human_actions = [
        a
        for a in p["actions"]
        if a in {"record_handling", "human_takeover"} or a.startswith("human_control:")
    ]
    check(
        "C6",
        "F6 automatic source, no human permission or human actions",
        {"source": "automatic", "human_actions": [], "human_control": False},
        {
            "source": p["authorization_source"],
            "actions": p["actions"],
            "permissions": p["permissions"],
        },
        p["authorization_source"] == "automatic"
        and not human_actions
        and "human_control" not in p["permissions"]
        and "read_only" in p["permissions"],
    )
    check(
        "C7",
        "All public offline replays consistent; latest agrees with F6",
        True,
        {
            "replays": [
                {
                    k: r.get(k)
                    for k in (
                        "session_id",
                        "consistent",
                        "recovery_verdict",
                        "integrity",
                    )
                }
                for r in replays
            ],
            "f6_replay": None
            if p["replay"] is None
            else {
                k: p["replay"].get(k)
                for k in ("session_id", "consistent", "recovery_verdict", "integrity")
            },
        },
        bool(replays)
        and all(r["consistent"] is True for r in replays)
        and p["replay"] is not None
        and p["replay"]["consistent"] is True
        and replays[-1]["session_id"] == str(session.get("session_id"))
        and all(
            replays[-1][k] == p[k]
            for k in (
                "recovery_confirmed",
                "healthy_window_seconds",
                "recovery_verdict",
            )
        ),
    )
    expected_state, expected_reason, lifecycle = (
        ("completed", "recovery_confirmed", "resolved")
        if scenario == "S1"
        else ("expired", "deadline_expired", "open")
    )
    check(
        f"{scenario}-1",
        "Session terminal state and reason",
        [expected_state, expected_reason],
        [session.get("state"), session.get("ended_reason")],
        session.get("state") == expected_state
        and session.get("ended_reason") == expected_reason,
    )
    check(
        f"{scenario}-2",
        "Incident lifecycle agrees with public F6 projection",
        lifecycle,
        {"stored": inc["lifecycle"], "f6": p["incident_lifecycle"]},
        inc["lifecycle"] == p["incident_lifecycle"] == lifecycle,
    )
    if scenario == "S1":
        content = (
            json.loads(current[0]["health_profile"]["content"])
            if len(current) == 1 and current[0].get("health_profile")
            else {}
        )
        sustained = content.get("session", {}).get("sustained_window_seconds")
        check(
            "S1-3",
            "Confirmed recovery with complete frozen profile healthy window",
            {"confirmed": True, "minimum_seconds": sustained},
            {
                "confirmed": p["recovery_confirmed"],
                "healthy_window_seconds": p["healthy_window_seconds"],
                "session_sustained": session.get("sustained_window_seconds"),
            },
            p["recovery_confirmed"] is True
            and isinstance(sustained, int)
            and sustained >= 600
            and session.get("sustained_window_seconds") == sustained
            and p["healthy_window_seconds"] >= sustained,
        )
        check(
            "S1-4",
            "Visible automatic recovery statement and rendered snapshot status",
            {
                "text": RECOVERED,
                "source": "automatic",
                "status": "recovered_no_remediation",
            },
            page,
            RECOVERED in page["text"]
            and "automatic" in page["sources"]
            and "recovered_no_remediation" in page["statuses"]
            and str(session.get("session_id")) in page["text"],
        )
    else:
        ending = endings[0] if len(endings) == 1 else {}
        at, deadline = ending.get("recorded_at"), session.get("deadline_at")
        check(
            "S2-3",
            "One unconfirmed ending at or after frozen DB deadline",
            {
                "count": 1,
                "transition": "observation_ended_unconfirmed",
                "lifecycle_after": "open",
                "ended_reason": "deadline_expired",
                "at_or_after_deadline": True,
            },
            {"endings": endings, "deadline_at": deadline},
            len(endings) == 1
            and ending.get("transition") == "observation_ended_unconfirmed"
            and ending.get("lifecycle_after") == "open"
            and ending.get("ended_reason") == "deadline_expired"
            and at is not None
            and deadline is not None
            and at >= deadline,
        )
        check(
            "S2-4",
            "F6 deadline handoff without recovery",
            {
                "confirmed": False,
                "ended": True,
                "reason": "deadline_expired",
                "interaction": "handoff",
                "handoff_reason": "DEADLINE_EXPIRED",
            },
            {
                k: p[k]
                for k in (
                    "recovery_confirmed",
                    "observation_ended",
                    "observation_ended_reason",
                    "human_interaction",
                    "handoff_reasons",
                )
            },
            p["recovery_confirmed"] is False
            and p["observation_ended"] is True
            and p["observation_ended_reason"] == "deadline_expired"
            and p["human_interaction"] == "handoff"
            and "DEADLINE_EXPIRED" in p["handoff_reasons"],
        )
        check(
            "S2-5",
            "Sample budget exhaustion cannot substitute for deadline",
            "no max_samples_exhausted",
            [
                session.get("ended_reason"),
                p["observation_ended_reason"],
                *[e.get("ended_reason") for e in endings],
            ],
            all(
                v != "max_samples_exhausted"
                for v in [
                    session.get("ended_reason"),
                    p["observation_ended_reason"],
                    *[e.get("ended_reason") for e in endings],
                ]
            ),
        )
        check(
            "S2-6",
            "Authorization failure cannot substitute for observation handoff",
            "authorized",
            auth,
            len(auth) == 1 and auth[0]["state"] == "authorized",
        )
        samples = current[0]["samples"] if len(current) == 1 else []
        adopted = [s for s in samples if s["disposition"] == "adopted"]
        replayed_samples = {
            str(s["sample_id"]): s
            for r in replays
            if r["session_id"] == str(session.get("session_id"))
            for s in r.get("samples", [])
        }
        sampled_basis = [replayed_samples.get(str(s["sample_id"]), {}) for s in adopted]
        evidence = [
            {
                "sample_id": s["sample_id"],
                "window_end": s["window_end"],
                "outcome": s["outcome"],
                "required_signals_present": s["required_signals_present"],
            }
            for s in adopted
        ]
        check(
            "S2-7",
            "Sampled persistent degradation with required telemetry and traffic, not unknown",
            "nonempty adopted degraded samples; F6 degraded",
            {
                "samples": evidence,
                "verdict": p["recovery_verdict"],
                "reasons": p["recovery_reasons"],
                "sampled_basis": [
                    {
                        "sample_id": s.get("sample_id"),
                        "replayed_outcome": s.get("replayed_outcome"),
                        "replayed_required_signals_present": s.get(
                            "replayed_required_signals_present"
                        ),
                        "readings": [
                            {"signal_name": r["signal_name"], "verdict": r["verdict"]}
                            for r in s.get("readings", [])
                        ],
                    }
                    for s in sampled_basis
                ],
            },
            bool(adopted)
            and all(
                s["outcome"] == "degraded" and s["required_signals_present"] is True
                for s in adopted
            )
            and all(
                s.get("replayed_outcome") == "degraded"
                and s.get("replayed_required_signals_present") is True
                and bool(s.get("readings"))
                and all(r["verdict"] != "below_traffic_gate" for r in s["readings"])
                for s in sampled_basis
            )
            and p["recovery_verdict"] == "degraded"
            and "INSUFFICIENT_TRAFFIC" not in p["recovery_reasons"],
        )
        check(
            "S2-8",
            "Public recovery section makes no recovery success claim",
            "no success statement",
            page,
            bool(page["text"])
            and str(session.get("session_id")) in page["text"]
            and "automatic" in page["sources"]
            and RECOVERED not in page["text"]
            and "recovery confirmed" not in page["text"]
            and "recovered_no_remediation" not in page["statuses"],
        )
        automatic = [
            h["session"]["session_id"]
            for h in current
            if h["session"].get("authorization_source") == "automatic"
        ]
        check(
            "S2-9",
            "No second automatic session at the same generation (at read time)",
            1,
            automatic,
            len(automatic) == 1,
        )
    return {
        "scenario": scenario,
        "incident_id": incident_id,
        "checked_at": records["checked_at"],
        "verdicts": verdicts,
        "all_pass": all(v["pass"] for v in verdicts),
    }


class RecoveryPage(HTMLParser):
    """Extract visible recovery text and its public source/status markers."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, bool]] = []
        self.parts: list[str] = []
        self.sources: list[str] = []
        self.statuses: list[str] = []
        self.active = False
        self.source_level: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "section" and attributes.get("id") == "recovery-verdict":
            self.active = True
        if not self.active:
            return
        hidden = (
            tag in {"script", "style", "template"}
            or "hidden" in attributes
            or attributes.get("aria-hidden") == "true"
            or "display:none"
            in (attributes.get("style") or "").replace(" ", "").lower()
            or "visibility:hidden"
            in (attributes.get("style") or "").replace(" ", "").lower()
            or any(h for _, h in self.stack)
        )
        classes = (attributes.get("class") or "").split()
        if (
            not hidden
            and "recovery-statement" in classes
            and attributes.get("data-status")
        ):
            self.statuses.append(str(attributes["data-status"]))
        if tag not in {"br", "hr", "input", "img", "meta", "link"}:
            self.stack.append((tag, hidden))
        if not hidden and "authorization-source" in classes:
            self.sources.append("")
            self.source_level = len(self.stack)

    def handle_endtag(self, tag: str) -> None:
        if not self.active:
            return
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if self.source_level is not None and len(self.stack) < self.source_level:
            self.source_level = None
        if tag == "section" and not self.stack:
            self.active = False

    def handle_data(self, data: str) -> None:
        if self.active and not any(h for _, h in self.stack):
            self.parts.append(data)
            if self.source_level is not None and self.sources and not self.sources[-1]:
                # Only accept actual source enum text, not another badge.
                if data.strip() in {"automatic", "post_remediation", "none"}:
                    self.sources[-1] = data.strip()

    def document(self) -> dict[str, Any]:
        return {
            "text": " ".join(" ".join(self.parts).split()),
            "sources": self.sources,
            "statuses": self.statuses,
        }


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def read_page(web: str, incident: UUID, user_file: Path) -> dict[str, Any]:
    url = urlsplit(web)
    if (
        url.scheme not in {"http", "https"}
        or not url.netloc
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("invalid web base URL")
    credential = user_file.read_text().strip()
    name, separator, password = credential.partition(":")
    if (
        not separator
        or not name
        or not password
        or "\n" in credential
        or "\r" in credential
    ):
        raise ValueError("user-file must contain one name:password line")
    token = base64.b64encode(credential.encode()).decode()
    request = Request(
        f"{web.rstrip('/')}/incidents/{incident}",
        headers={"Authorization": f"Basic {token}"},
        method="GET",
    )
    with build_opener(NoRedirect()).open(request, timeout=20) as response:
        body = response.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            raise ValueError("page too large")
        if response.status != 200 or "text/html" not in response.headers.get(
            "Content-Type", ""
        ):
            raise ValueError("expected HTML incident page")
    page = RecoveryPage()
    page.feed(body.decode("utf-8"))
    return page.document()


def readonly_dsn(dsn: str) -> str:
    options = conninfo_to_dict(dsn).get("options", "")
    return make_conninfo(
        dsn,
        options=f"{options} -c default_transaction_read_only=on",
        connect_timeout="5",
    )


def read_records(
    incident: UUID, owner_dsn: str, observer_dsn: str, web: str, user_file: Path
) -> dict[str, Any]:
    owner = readonly_dsn(owner_dsn)
    with psycopg.connect(owner, row_factory=dict_row) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        conn.execute("SET LOCAL statement_timeout='5000ms'")
        clock = conn.execute("SELECT CURRENT_TIMESTAMP AS checked_at").fetchone()
        if clock is None:
            raise ValueError("database clock unavailable")
        checked_at = clock["checked_at"].isoformat()
        inc = conn.execute(
            "SELECT incident_id,current_run_id,lifecycle,control_generation,conclusion IS NOT NULL AS conclusion_present FROM opspilot_incidents WHERE incident_id=%s",
            (incident,),
        ).fetchone()
        if inc is None:
            raise ValueError("unknown incident")
        run = conn.execute(
            "SELECT run_id,incident_id,state,control_generation FROM opspilot_runs WHERE run_id=%s",
            (inc["current_run_id"],),
        ).fetchone()
        authorizations = conn.execute(
            "SELECT incident_id,control_generation,state,reason,session_id,attempts FROM opspilot_self_heal_authorizations WHERE incident_id=%s",
            (incident,),
        ).fetchall()
    owner_store, observer = (
        ObservationStore(owner),
        ObservationStore(readonly_dsn(observer_dsn)),
    )
    try:
        records = owner_store.incident_records(incident)
        grants = observer.table_privileges()
    finally:
        owner_store.close()
        observer.close()
    if any(
        inc[k] != records["incident"][k]
        for k in ("incident_id", "lifecycle", "control_generation")
    ):
        raise ValueError("incident changed between read snapshots; repeat collection")
    outcome = recovery_outcome(
        IncidentScenario(
            f"184-{incident}", "F6", "184-step3", "incident_recovery", str(incident)
        ),
        RecoveryRecords(
            records["incident"],
            tuple(records["sessions"]),
            tuple(records["controls"]),
            grants,
        ),
    )
    keys = (
        "authorization_source",
        "actions",
        "permissions",
        "incident_lifecycle",
        "recovery_confirmed",
        "healthy_window_seconds",
        "recovery_verdict",
        "recovery_reasons",
        "observation_ended",
        "observation_ended_reason",
        "human_interaction",
        "handoff_reasons",
    )
    projection = {k: getattr(outcome, k) for k in keys}
    projection["replay"] = None if outcome.replay is None else summary(outcome.replay)
    return {
        **records,
        "incident": inc,
        "run": run,
        "authorizations": authorizations,
        "projection": projection,
        "replays": [summary(replay_history(h)) for h in records["sessions"]],
        "page": read_page(web, incident, user_file),
        "checked_at": checked_at,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("S1", "S2"), required=True)
    parser.add_argument("--incident", type=UUID, required=True)
    parser.add_argument("--owner-dsn", required=True)
    parser.add_argument("--observer-dsn", required=True)
    parser.add_argument("--web", required=True)
    parser.add_argument("--user-file", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        verdict = judge(
            args.scenario,
            str(args.incident),
            read_records(
                args.incident,
                args.owner_dsn,
                args.observer_dsn,
                args.web,
                args.user_file,
            ),
        )
        document = json.dumps(verdict, ensure_ascii=False, indent=2) + "\n"
        if args.out:
            args.out.write_text(document)
        sys.stdout.write(document)
        return 0 if verdict["all_pass"] else 1
    except (
        Exception
    ):  # Never echo connection errors, URLs, credentials or row payloads.
        print("READ_ERROR: unable to collect or evaluate records", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
