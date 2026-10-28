#!/usr/bin/env python3
"""Dev-only F2 B2 storm driver: synthetic Alertmanager v4 webhooks + sampling.

``send`` posts one webhook per alert to ``POST /intake/alertmanager`` (the
real intake, Bearer event token), lowest priority first by default: four
alerts without ``severity``, then four ``warning``, then four ``critical``
(B2 contract section 6, F2 R8). Each alert has its own ``alertname`` and
``fingerprint``, so each opens its own incident. Prints the intake results
as JSON lines; the token is read from a file and never printed.

``sample`` prints, every ``--interval`` seconds, one JSON line of Run
counts by state (``running`` with a live lease counted apart) and each
current Run's state, from a read-only owner connection to ``--dsn``.

Example::

    python scripts/f2_backlog_storm.py send --url http://127.0.0.1:8094 \
        --token-file tok --labels namespace=otel-demo,service=checkout --tag r1
    python scripts/f2_backlog_storm.py sample --dsn "host=... dbname=..." \
        --interval 2 --count 300
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ORDER = (None,) * 4 + ("warning",) * 4 + ("critical",) * 4


def _alerts(tag: str, labels: dict[str, str], order: tuple[str | None, ...]):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    starts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for n, severity in enumerate(order):
        alert_labels = {"alertname": f"F2B2Storm{tag}N{n:02d}", **labels}
        if severity is not None:
            alert_labels["severity"] = severity
        fingerprint = hashlib.sha256(
            json.dumps(alert_labels, sort_keys=True).encode()
        ).hexdigest()[:16]
        yield (
            severity,
            {
                "status": "firing",
                "labels": alert_labels,
                "annotations": {
                    "summary": f"checkout error ratio high (F2 B2 storm {tag} #{n})"
                },
                "startsAt": starts,
                "endsAt": "0001-01-01T00:00:00Z",
                "fingerprint": fingerprint,
                "generatorURL": "http://prometheus:9090/graph",
            },
        )


def send(args: argparse.Namespace) -> None:
    token = Path(args.token_file).read_text().strip()
    labels = dict(item.split("=", 1) for item in args.labels.split(",") if item)
    order = ORDER if not args.reverse else tuple(reversed(ORDER))
    for severity, alert in _alerts(args.tag, labels, order):
        body = {
            "receiver": "opspilot",
            "status": "firing",
            "version": "4",
            "groupKey": "{}:{}",
            "truncatedAlerts": 0,
            "groupLabels": {},
            "commonLabels": {},
            "commonAnnotations": {},
            "externalURL": "http://alertmanager:9093",
            "alerts": [alert],
        }
        request = urllib.request.Request(
            args.url.rstrip("/") + "/intake/alertmanager",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read())
        print(
            json.dumps(
                {
                    "sent_at": datetime.now(timezone.utc).isoformat(),
                    "alertname": alert["labels"]["alertname"],
                    "severity": severity,
                    "results": result.get("results"),
                }
            ),
            flush=True,
        )
        time.sleep(args.gap)


def sample(args: argparse.Namespace) -> None:
    import psycopg

    for _ in range(args.count):
        with psycopg.connect(args.dsn) as conn:
            rows = conn.execute(
                "SELECT r.run_id,i.priority,r.state,"
                "(r.state='running' AND r.lease_until>clock_timestamp()) AS live,"
                "clock_timestamp() FROM opspilot_incidents i "
                "JOIN opspilot_runs r ON r.run_id=i.current_run_id ORDER BY i.priority"
            ).fetchall()
            now = conn.execute("SELECT clock_timestamp()").fetchone()[0]
        counts: dict[str, int] = {}
        for row in rows:
            counts[row[2]] = counts.get(row[2], 0) + 1
        print(
            json.dumps(
                {
                    "at": now.astimezone(timezone.utc).isoformat(),
                    "running_live": sum(1 for row in rows if row[3]),
                    "counts": counts,
                    "runs": {str(row[0])[:8]: [row[1], row[2]] for row in rows},
                }
            ),
            flush=True,
        )
        time.sleep(args.interval)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("send")
    s.add_argument("--url", required=True)
    s.add_argument("--token-file", required=True)
    s.add_argument("--labels", required=True, help="k=v,k=v binding the target")
    s.add_argument("--tag", required=True)
    s.add_argument("--gap", type=float, default=0.2)
    s.add_argument("--reverse", action="store_true", help="critical first")
    p = sub.add_parser("sample")
    p.add_argument("--dsn", required=True)
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--count", type=int, default=300)
    args = parser.parse_args(argv)
    {"send": send, "sample": sample}[args.command](args)


if __name__ == "__main__":
    main(sys.argv[1:])
