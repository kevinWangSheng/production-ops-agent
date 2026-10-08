"""Engineer-side driver for the M1-02 step 4 real run (issue #86).

Not product code. ``prepare`` plays the Controller side that step 3 (#85)
will deliver as the human "handling registered" action: it registers the
lab target, accepts one incident and authorizes an observation session bound
to the shipped checkout profile, using the store primitive with the owner
DSN. The Observer process (``python -m opspilot.observer``) is started
separately with its own login and Prometheus endpoint. ``summarize`` freezes
what the Observer committed into ``summary.json``: no raw response bytes
(they stay in the database and out of the repository, ADR-0006), only
statuses, values, hashes and the decisions.

    .venv/bin/python docs/evidence/m1-02-observer/lab_run.py prepare \\
        --owner-dsn "$OWNER_DSN" --out tmp/m1-02-observer/session.json
    .venv/bin/python docs/evidence/m1-02-observer/lab_run.py summarize \\
        --owner-dsn "$OWNER_DSN" --session tmp/m1-02-observer/session.json \\
        --out docs/evidence/m1-02-observer/summary.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from opspilot.domain.intake import Target  # noqa: E402
from opspilot.observation import ObservationStore  # noqa: E402
from opspilot.observer import PROFILE_DIRECTORY, load_health_profile  # noqa: E402
from opspilot.persistence import DurableStore  # noqa: E402

PROFILE_PATH = PROFILE_DIRECTORY / "otel-demo-checkout.json"


def _canonical(profile) -> str:
    return json.dumps(
        profile.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def prepare(args: argparse.Namespace) -> int:
    profile = load_health_profile(PROFILE_PATH)
    owner = DurableStore(args.owner_dsn)
    owner.install()
    controller = ObservationStore(args.owner_dsn)
    # engineer script, not product code: the authorization time is the
    # engineer's clock, the Observer itself only reads the database clock
    now = datetime.now(timezone.utc)  # noqa: TID251
    incident, run = uuid4(), uuid4()
    resource_uid = f"deployment/checkout-{args.experiment_id}"
    target_id = owner.register_target(resource_uid)
    # Intake registers the uid alone; step 3's register_remediation completes
    # the identity (migration 0004) from the configured registry. This driver
    # stands in for that action, so it completes the lab identity directly.
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_targets SET integration_id=%s,cluster_uid=%s,namespace=%s,workload=%s WHERE target_id=%s",
            (
                "m0-otel-20260909",
                "kind-opspilot-m1",
                "otel-demo",
                "checkout",
                target_id,
            ),
        )
    owner.accept(
        incident,
        run,
        f"m1-02-observer-{args.experiment_id}",
        deadline=now + timedelta(hours=1),
        budget_limit=10,
        versions={"experiment": args.experiment_id},
        target_id=target_id,
    )
    target = Target(
        integration_id="m0-otel-20260909",
        cluster_uid="kind-opspilot-m1",
        namespace="otel-demo",
        resource_uid=resource_uid,
        revision="otel-demo-0.37.8",
    )
    # The profile's own session parameters (interface contract item 5).
    session = controller.authorize_session(
        incident,
        revision=target.revision,
        actor=f"engineer:{args.experiment_id}",
        deadline_at=now + timedelta(seconds=profile.session.deadline_seconds),
        max_samples=profile.session.max_samples,
        sample_interval_seconds=profile.session.sample_interval_seconds,
        sustained_window_seconds=profile.session.sustained_window_seconds,
        health_profile_revision=profile.revision,
        health_profile=_canonical(profile),
        first_sample_due_at=now,
    )
    record = {
        "experiment_id": args.experiment_id,
        "incident_id": str(incident),
        "target_id": str(target_id),
        "session_id": str(session),
        "health_profile_revision": profile.revision,
        "authorized_at": now.isoformat(),
        "session_parameters": profile.session.model_dump(),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps(record, indent=2))
    owner.close()
    controller.close()
    return 0


def _jsonable(value):
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    return value


def summarize(args: argparse.Namespace) -> int:
    record = json.loads(args.session.read_text())
    controller = ObservationStore(args.owner_dsn)
    session_id = UUID(record["session_id"])
    history = controller.session_history(session_id)
    replay = controller.replay_session(session_id)
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
                    # the freshness timestamp the Observer judged, from the bundle
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
    summary = {
        "experiment_id": record["experiment_id"],
        "frozen_at": datetime.now(timezone.utc).isoformat(),  # noqa: TID251
        "incident_id": record["incident_id"],
        "session_id": record["session_id"],
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
        "replay": {
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
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    controller.close()
    return 0


def _latest(raw) -> str | None:
    if raw is None:
        return None
    try:
        bundle = json.loads(bytes(raw))
        import base64

        body = json.loads(base64.b64decode(bundle["freshness"]["body_b64"]))
        result = body["data"]["result"]
        if not result:
            return None
        return datetime.fromtimestamp(
            float(result[0]["value"][1]), tz=timezone.utc
        ).isoformat()
    except (KeyError, ValueError, TypeError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--owner-dsn", required=True)
    prep.add_argument(
        "--experiment-id",
        default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),  # noqa: TID251
    )
    prep.add_argument("--out", type=Path, required=True)
    prep.set_defaults(func=prepare)
    summ = sub.add_parser("summarize")
    summ.add_argument("--owner-dsn", required=True)
    summ.add_argument("--session", type=Path, required=True)
    summ.add_argument("--out", type=Path, required=True)
    summ.set_defaults(func=summarize)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
