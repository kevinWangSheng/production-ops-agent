"""Engineer-side driver for the M1-02 step 6 real-lab F6 acceptance (#88).

Not product code. Every product-side step goes through the product's own
entry points: the incident is submitted and the remediation registered over
the workbench HTTP API (``/intake/ui`` and ``/incidents/<id>/control``
``register_remediation``, the step 3 human action), the samples are taken by
the separately started Observer process (``python -m opspilot.observer``)
with its own login and Prometheus account, the projection is the F6
acceptance entry ``opspilot.acceptance.recovery_outcome`` over
``ObservationStore.incident_records`` plus the privileges measured on the
Observer login, and the offline replay is ``python -m
opspilot.observer.replay``. This script only drives, waits and freezes:

    lab_run.py intake    --web ... --user-file ... --scenario <json> --step <n> --kind <k>
    lab_run.py register  --web ... --user-file ... --scenario <json> --owner-dsn ... --revision ...
    lab_run.py status    --owner-dsn ... --scenario <json>
    lab_run.py summarize --owner-dsn ... --observer-dsn ... --scenario <json> --out <summary.json>
    lab_run.py page      --web ... --user-file ... --scenario <json> --out <page.html>

``summary.json`` carries no raw response bytes (they stay in the database,
ADR-0006): per reading only status, value, point count, ``raw_sha256``, the
bundle size and the freshness timestamp the Observer judged. The UI
password is read from ``--user-file`` (``name:password``) and never printed.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from opspilot.acceptance import (  # noqa: E402
    IncidentScenario,
    RecoveryRecords,
    recovery_outcome,
)
from opspilot.observation import ObservationStore  # noqa: E402

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _auth(user_file: Path) -> str:
    raw = user_file.read_text().strip().encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _post(web: str, path: str, fields: dict[str, str], user_file: Path) -> dict:
    request = urllib.request.Request(
        f"{web}{path}",
        data=urllib.parse.urlencode(fields).encode(),
        headers={
            "Authorization": _auth(user_file),
            "Origin": web,
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with OPENER.open(request, timeout=30) as response:
            return {"status": response.status, "body": json.loads(response.read())}
    except urllib.error.HTTPError as error:
        body = error.read().decode(errors="replace")
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = body[:500]
        return {"status": error.code, "body": parsed}


def _load(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _save(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()  # noqa: TID251 - engineer clock


def intake(args: argparse.Namespace) -> int:
    record = _load(args.scenario)
    key = f"f6-live-{args.step}-{args.kind}-{args.experiment_id}"
    result = _post(
        args.web,
        "/intake/ui",
        {
            "target_id": args.target_id,
            "question": (
                f"F6 step {args.step} ({args.kind}): checkout shows elevated "
                "PlaceOrder errors in the lab; the investigation Run stays queued "
                "(no worker in this acceptance), the human handles it outside."
            ),
        }
        | {"idempotency_key": key},
        args.user_file,
    )
    record.update(
        {
            "experiment_id": args.experiment_id,
            "scenario_id": f"F6:{args.step}:{args.kind}",
            "acceptance_step": str(args.step),
            "kind": args.kind,
            "target_id": args.target_id,
            "intake": {
                "at": _now(),
                "status": result["status"],
                "body": result["body"],
            },
        }
    )
    if result["status"] in (200, 201):
        record["incident_id"] = result["body"]["incident_id"]
    _save(args.scenario, record)
    print(json.dumps(record["intake"], indent=2))
    return 0 if "incident_id" in record else 1


def register(args: argparse.Namespace) -> int:
    record = _load(args.scenario)
    incident = UUID(record["incident_id"])
    store = ObservationStore(args.owner_dsn)
    try:
        with store.transaction() as conn:
            row = conn.execute(
                "SELECT control_generation, lifecycle FROM opspilot_incidents WHERE incident_id=%s",
                (incident,),
            ).fetchone()
        generation = int(row["control_generation"])
        result = _post(
            args.web,
            f"/incidents/{incident}/control",
            {
                "action": "register_remediation",
                "expected_generation": str(generation),
                "idempotency_key": f"rem-{record['scenario_id']}-{args.experiment_id}",
                "revision": args.revision,
            },
            args.user_file,
        )
        sessions = store.incident_sessions(incident)
    finally:
        store.close()
    record["register"] = {
        "at": _now(),
        "lifecycle_before": row["lifecycle"],
        "expected_generation": generation,
        "revision": args.revision,
        "status": result["status"],
        "body": result["body"],
    }
    if sessions:
        latest = sessions[-1]
        record["session_id"] = str(latest["session_id"])
        record["health_profile_revision"] = latest["health_profile_revision"]
    _save(args.scenario, record)
    print(json.dumps(record["register"], indent=2))
    return 0 if result["status"] == 200 else 1


def status(args: argparse.Namespace) -> int:
    record = _load(args.scenario)
    store = ObservationStore(args.owner_dsn)
    try:
        history = store.session_history(UUID(record["session_id"]))
    finally:
        store.close()
    session = history["session"]
    print(
        json.dumps(
            {
                "now": _now(),
                "incident_lifecycle": history["incident_lifecycle"],
                "state": session["state"],
                "ended_reason": session["ended_reason"],
                "adopted_count": session["adopted_count"],
                "healthy_since": _jsonable(session["healthy_since"]),
                "samples": [
                    {
                        "sequence": s["sequence"],
                        "window_end": _jsonable(s["window_end"]),
                        "outcome": s["outcome"],
                        "disposition": s["disposition"],
                        "health_basis": s["health_basis"],
                        "transition": s["transition"],
                        "readings": {
                            r["signal_name"]: (
                                r["status"],
                                r["value"],
                                r["sample_count"],
                            )
                            for r in s["readings"]
                        },
                    }
                    for s in history["samples"]
                ],
                "endings": [
                    {k: _jsonable(v) for k, v in e.items()} for e in history["endings"]
                ],
            },
            indent=2,
            default=str,
        )
    )
    return 0


def _jsonable(value):
    if isinstance(value, datetime):
        # rows come back in the connection's session time zone; freeze UTC
        return (value.astimezone(timezone.utc) if value.tzinfo else value).isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)
        }
    return value


def _latest(raw) -> str | None:
    if raw is None:
        return None
    try:
        bundle = json.loads(bytes(raw))
        body = json.loads(base64.b64decode(bundle["freshness"]["body_b64"]))
        result = body["data"]["result"]
        if not result:
            return None
        return datetime.fromtimestamp(
            float(result[0]["value"][1]), tz=timezone.utc
        ).isoformat()
    except (KeyError, ValueError, TypeError):
        return None


def summarize(args: argparse.Namespace) -> int:
    record = _load(args.scenario)
    incident = UUID(record["incident_id"])
    session_id = UUID(record["session_id"])
    controller = ObservationStore(args.owner_dsn)
    observer = ObservationStore(args.observer_dsn)
    try:
        history = controller.session_history(session_id)
        replay = controller.replay_session(session_id)
        records = controller.incident_records(incident)
        grants = observer.table_privileges()
    finally:
        controller.close()
        observer.close()
    outcome = recovery_outcome(
        IncidentScenario(
            scenario_id=record["scenario_id"],
            feature_id="F6",
            acceptance_step=record["acceptance_step"],
            kind=record["kind"],
            subject_id=str(incident),
        ),
        RecoveryRecords(
            incident=records["incident"],
            sessions=tuple(records["sessions"]),
            controls=tuple(records["controls"]),
            grants=grants,
        ),
    )
    session = history["session"]
    samples = []
    for sample in history["samples"]:
        readings = []
        for reading in sample["readings"]:
            raw = reading["raw"]
            readings.append(
                {
                    "signal_name": reading["signal_name"],
                    "status": reading["status"],
                    "value": reading["value"],
                    "sample_count": reading["sample_count"],
                    "query": reading["query"],
                    "source": reading["source"],
                    "window_start": _jsonable(reading["window_start"]),
                    "window_end": _jsonable(reading["window_end"]),
                    "raw_sha256": reading["raw_sha256"],
                    "raw_bytes": None if raw is None else len(bytes(raw)),
                    "latest_sample_at": _latest(raw),
                }
            )
        samples.append(
            {
                key: _jsonable(sample[key])
                for key in (
                    "sample_id",
                    "sequence",
                    "epoch",
                    "window_start",
                    "window_end",
                    "outcome",
                    "required_signals_present",
                    "disposition",
                    "reason",
                    "confirms_health",
                    "health_basis",
                    "subject_lifecycle",
                    "transition",
                    "submitted_at",
                    "lease_valid",
                    "readings_consistent",
                )
            }
            | {"readings": readings}
        )
    projection = _jsonable(outcome)
    # the frozen profile content is in the database and in the repository
    # (shipped) or next to this record (bounded variant); keep the hash only
    content = projection.pop("recovery_profile_content", None)
    projection["recovery_profile_content_sha256"] = (
        None
        if content is None
        else __import__("hashlib").sha256(content.encode()).hexdigest()
    )
    summary = {
        "experiment_id": record["experiment_id"],
        "scenario_id": record["scenario_id"],
        "frozen_at": _now(),
        "incident_id": str(incident),
        "session_id": str(session_id),
        "intake": record.get("intake"),
        "register": record.get("register"),
        "lab_actions": record.get("lab_actions", []),
        "health_profile_revision": session["health_profile_revision"],
        "stored_profile_sha256": (history["health_profile"] or {}).get(
            "content_sha256"
        ),
        "session": {
            key: _jsonable(session[key])
            for key in (
                "state",
                "ended_reason",
                "authorized_at",
                "deadline_at",
                "max_samples",
                "sample_interval_seconds",
                "sustained_window_seconds",
                "adopted_sequence",
                "adopted_count",
                "adopted_window_end",
                "healthy_since",
            )
        },
        "incident_lifecycle": history["incident_lifecycle"],
        "samples": samples,
        "endings": [
            {k: _jsonable(v) for k, v in ending.items()}
            for ending in history["endings"]
        ],
        "replay_session": {
            "consistent": replay.consistent,
            "session_consistent": replay.session_consistent,
            "lifecycle_consistent": replay.lifecycle_consistent,
            "samples": [
                {
                    "sequence": item.sequence,
                    "matches": item.matches,
                    "stored": list(item.stored),
                    "replayed": list(item.replayed),
                    "raw_mismatches": list(item.raw_mismatches),
                    "signal_mismatches": list(item.signal_mismatches),
                }
                for item in replay.samples
            ],
        },
        "recovery_outcome": projection,
    }
    _save(args.out, summary)
    print(
        json.dumps(
            {
                "incident_lifecycle": summary["incident_lifecycle"],
                "session": summary["session"]["state"],
                "ended_reason": summary["session"]["ended_reason"],
                "samples": [
                    (s["sequence"], s["outcome"], s["disposition"]) for s in samples
                ],
                "replay_consistent": replay.consistent,
                "recovery_verdict": projection["recovery_verdict"],
                "recovery_confirmed": projection["recovery_confirmed"],
                "recovery_reasons": projection["recovery_reasons"],
                "human_interaction": projection["human_interaction"],
                "handoff_reasons": projection["handoff_reasons"],
                "permissions": projection["permissions"],
                "actions_count": len(projection["actions"]),
            },
            indent=2,
        )
    )
    return 0


def page(args: argparse.Namespace) -> int:
    record = _load(args.scenario)
    request = urllib.request.Request(
        f"{args.web}/incidents/{record['incident_id']}",
        headers={"Authorization": _auth(args.user_file)},
    )
    with OPENER.open(request, timeout=30) as response:
        html = response.read().decode()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html)
    print(f"{len(html)} bytes -> {args.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")  # noqa: TID251

    p = sub.add_parser("intake")
    p.add_argument("--web", required=True)
    p.add_argument("--user-file", type=Path, required=True)
    p.add_argument("--scenario", type=Path, required=True)
    p.add_argument("--step", required=True)
    p.add_argument("--kind", required=True)
    p.add_argument("--target-id", default="checkout-lab")
    p.add_argument("--experiment-id", default=stamp)
    p.set_defaults(func=intake)

    p = sub.add_parser("register")
    p.add_argument("--web", required=True)
    p.add_argument("--user-file", type=Path, required=True)
    p.add_argument("--scenario", type=Path, required=True)
    p.add_argument("--owner-dsn", required=True)
    p.add_argument("--revision", default="otel-demo-0.37.8")
    p.add_argument("--experiment-id", default=stamp)
    p.set_defaults(func=register)

    p = sub.add_parser("status")
    p.add_argument("--owner-dsn", required=True)
    p.add_argument("--scenario", type=Path, required=True)
    p.set_defaults(func=status)

    p = sub.add_parser("summarize")
    p.add_argument("--owner-dsn", required=True)
    p.add_argument("--observer-dsn", required=True)
    p.add_argument("--scenario", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.set_defaults(func=summarize)

    p = sub.add_parser("page")
    p.add_argument("--web", required=True)
    p.add_argument("--user-file", type=Path, required=True)
    p.add_argument("--scenario", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.set_defaults(func=page)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
