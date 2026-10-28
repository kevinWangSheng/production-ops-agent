"""F11 PR-5c: freeze one release's summary.json from the lab window.

``python -m scripts.f11_pr5c_collect --release <A|B|C> --run <harness run>
--observation <id> --expected <verdict> --out <summary.json>
[--snap-pairs a:b ...] [--controls label ...]``

Reads only: the workbench page JSON over HTTP (Basic Auth; the product's own
projection), the harness ledger (``tmp/lab-ledgers/<run>/``) and this lab
batch's ledger (``tmp/lab-ledgers/<batch>/``: snapshots, page controls),
read-only SQL on the one-off PG (``F11_5C_DSN``, a read-only transaction),
and ``replay_session`` via ``python -m opspilot.observer.replay`` under the
web login (``F11_5C_WEB_DSN``). It writes nothing but ``--out``; no token,
password or absolute local path goes into it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import psycopg

from scripts import f11_pr5c_lab as lab
from scripts.f11_u2_harness import resolve_ledger_root


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rows(conn: Any, sql: str, *params: Any) -> list[dict[str, Any]]:
    cur = conn.execute(sql, params)
    names = [d.name for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def replay(sessions: list[str]) -> list[dict[str, Any]]:
    if not sessions:
        return []
    argv = [
        sys.executable,
        "-m",
        "opspilot.observer.replay",
        "--dsn",
        os.environ["F11_5C_WEB_DSN"],
    ]
    for s in sessions:
        argv += ["--session", s]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    out = json.loads(proc.stdout)
    return [
        dict(
            session_id=s,
            exit_code=proc.returncode,
            consistent=r.get("consistent"),
            integrity=r.get("integrity"),
            verdict=r.get("verdict"),
            stored_status=r.get("stored_status"),
            replayed_status=r.get("replayed_status"),
            used_sample_count=r.get("used_sample_count"),
            external_queries=r.get("external_queries"),
            model_requests=r.get("model_requests"),
        )
        for s, r in zip(sessions, out)
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--release", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--observation", required=True)
    ap.add_argument("--expected", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--batch", default="f11-5c-live")
    ap.add_argument("--snap-pairs", nargs="*", default=[])
    ap.add_argument("--controls", nargs="*", default=[])
    ap.add_argument("--code-revision", required=True)
    args = ap.parse_args()

    root = resolve_ledger_root()
    run_dir = root / args.run
    batch_dir = root / args.batch
    page = lab.page(args.observation)
    release = json.loads((run_dir / "release.json").read_text())
    webhooks = []
    for result in sorted(
        (run_dir / "webhook").glob("*/result.json"), key=lambda p: p.stat().st_mtime
    ):
        w = json.loads(result.read_text())
        webhooks.append(
            dict(
                status=w["status"],
                response_code=(w.get("response") or {}).get("code"),
                outcome=(w.get("response") or {}).get("outcome"),
                request_sha256=w["request_sha256"],
                response_sha256=w["response_sha256"],
                at=None,
            )
        )
    timeline = [
        json.loads(line)
        for line in (run_dir / "timeline.jsonl").read_text().splitlines()
    ]
    hooks = [e for e in timeline if e.get("kind") == "release_webhook"]
    for w, e in zip(webhooks, hooks):
        w["at"] = e["request_before"]
    patch = next(
        (e for e in timeline if e.get("kind") == "release_patch_requested"), {}
    )

    with psycopg.connect(os.environ["F11_5C_DSN"]) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        samples = rows(
            conn,
            "SELECT s.session_id::text, s.sequence, s.outcome, s.reason, s.disposition, s.confirms_health,"
            " s.window_start, s.window_end, s.submitted_at FROM opspilot_observation_samples s"
            " JOIN opspilot_observation_sessions o ON o.session_id=s.session_id"
            " WHERE o.release_observation_id=%s ORDER BY s.submitted_at",
            args.observation,
        )
        link_rows = rows(
            conn,
            "SELECT * FROM opspilot_release_incident_links WHERE observation_id=%s",
            args.observation,
        )
        incidents, runs = [], []
        for link in link_rows:
            if link.get("incident_id"):
                incidents += rows(
                    conn,
                    "SELECT incident_id::text, intake_key, lifecycle, mode, priority, control_generation, current_run_id::text, created_at FROM opspilot_incidents WHERE incident_id=%s",
                    link["incident_id"],
                )
                runs += rows(
                    conn,
                    "SELECT run_id::text, incident_id::text, state, budget_spent AS model_requests, tool_operations_used, input->>'input_version' AS input_version, input->>'version' AS input_version_alt, versions->>'prompt_revision' AS prompt_revision FROM opspilot_runs WHERE incident_id=%s",
                    link["incident_id"],
                )
        totals = rows(
            conn,
            "SELECT (SELECT count(*) FROM opspilot_incidents) AS incidents, (SELECT count(*) FROM opspilot_runs) AS runs,"
            " (SELECT count(*) FROM opspilot_release_incident_links) AS links, (SELECT count(*) FROM opspilot_alert_deliveries) AS alert_deliveries",
        )[0]

    sessions = [s["session_id"] for s in page["sessions"]]
    snaps = {}
    for pair in args.snap_pairs:
        first, second = pair.split(":")
        d = lab.diff(args.batch, first, second)
        snaps[pair] = dict(
            objects_compared=d["objects_compared"],
            added=d["added"],
            removed=d["removed"],
            changed=sorted(d["changed"]),
            new_manager_entries=d["new_manager_entries"],
            changed_outside_deployment_replicaset_flagd=d[
                "changed_outside_deployment_replicaset_flagd"
            ],
        )
    controls = []
    for label in args.controls:
        c = json.loads((batch_dir / "controls" / f"{label}.json").read_text())
        controls.append(
            dict(
                action=c["action"],
                idempotency_label=label,
                route="POST /version-changes/{id}/control",
                origin_header=c["origin"],
                expected_control_generation=c["expected_control_generation"],
                request_before=c["request_before"],
                http_status=c["http_status"],
                outcome=(c["response"] or {}).get("outcome")
                if isinstance(c["response"], dict)
                else None,
                control_generation_after=c["after"]["control_generation"],
                sessions_after=c["after"]["sessions"],
                used_samples_at_control=c["after"]["used_samples"],
                authorization_seen_at=c.get("authorization_seen_at"),
            )
        )
    authorize = [a for a in page["audit"] if a["action"] == "authorize"]
    raw_files = sorted(
        str(p.relative_to(root)) for p in run_dir.rglob("*") if p.is_file()
    )
    summary = dict(
        batch=args.batch,
        release=args.release,
        harness_run=args.run,
        code_revision=args.code_revision,
        expected=args.expected,
        actual_verdict=page["verdict"],
        matches_expected=page["verdict"] == args.expected,
        product_path=dict(
            intake="POST /intake/version-changes (release-deployer event token, harness webhook)",
            authorization="web process release_authorize_pass (actor system), no direct primitive call",
            controls="workbench page route only",
        ),
        rollout=dict(
            source_release_id=release["release_id"],
            released_at_R=release["released_at"],
            patch_requested_at=patch.get("at"),
            deployment_uid=release.get("deployment_uid"),
            before_replicaset_uid=release.get("before_replicaset_uid"),
            after_replicaset_uid=release.get("after_replicaset_uid"),
            before_pod_template_hash=release.get("before_pod_template_hash"),
            after_pod_template_hash=release.get("after_pod_template_hash"),
        ),
        webhook_attempts=webhooks,
        page=dict(
            observation_id=page["observation_id"],
            status=page["status"],
            verdict=page["verdict"],
            ended_reason=page["ended_reason"],
            ended_at=page["ended_at"],
            paused=page["paused"],
            authorization_hold=page["authorization_hold"],
            source=page["source"],
            source_release_id=page["source_release_id"],
            target=page["target"],
            before=page["before"],
            after=page["after"],
            auxiliary=page["auxiliary"],
            released_at=page["released_at"],
            received_at=page["received_at"],
            deadline_at=page["deadline_at"],
            min_tracking_seconds=page["min_tracking_seconds"],
            calibrated_policy_revision=page["calibrated_policy_revision"],
            used_sample_count=page["used_sample_count"],
            max_samples=page["max_samples"],
            control_generation=page["control_generation"],
            observation_generation=page["observation_generation"],
            incident_id=page["incident_id"],
            actions=page["actions"],
            notice=page["notice"],
            sessions=page["sessions"],
            endings=page["endings"],
            audit=page["audit"],
            deliveries=page["deliveries"],
            comparisons=page["comparisons"],
            link=page["link"],
        ),
        page_matches_payload=dict(
            source_release_id=page["source_release_id"] == release["release_id"],
            released_at=page["released_at"].replace("+00:00", "").rstrip("Z")
            == release["released_at"].replace("+00:00", "").rstrip("Z"),
            before=page["before"] == release.get("before_revision"),
            after=page["after"] == release.get("after_revision"),
        ),
        authorization=dict(
            count=len(authorize),
            actor=[a["actor"] for a in authorize],
            principal_kind=[a["principal_kind"] for a in authorize],
            outcome=[a["outcome"] for a in authorize],
            recorded_at=[a["recorded_at"] for a in authorize],
        ),
        page_controls=controls,
        samples=jsonable(samples),
        replay=replay(sessions),
        k8s_policy_surface=dict(kinds=len(lab.KINDS), diffs=snaps),
        incident=jsonable(incidents),
        runs=jsonable(runs),
        db_totals_at_collection=jsonable(totals),
        raw_ledger=dict(
            location="main repository tmp/lab-ledgers/"
            + args.run
            + "/ (not committed)",
            files={f: sha(root / f) for f in raw_files},
        ),
    )
    text = json.dumps(jsonable(summary), indent=1, sort_keys=True)
    if re.search(r"/Users/|Bearer |password", text):
        raise SystemExit("summary would contain a local path or credential marker")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(text + "\n")
    print(args.out, page["verdict"], "expected", args.expected)
    return 0


if __name__ == "__main__":
    sys.exit(main())
