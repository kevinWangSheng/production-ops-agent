#!/usr/bin/env python3
"""Dev-only: seed the incident list's scale fixture (web UX spec 2026-11-16, section 7).

Creates 2003 incidents through the product's own paths: Alertmanager alerts
via ``Workbench.intake_alert`` (a registry match opens a Run; no match or
two matches hands the incident to a human), human decisions via
``Workbench.control``, and one Run handed back through the store's
``claim``/``hand_off`` (what a worker does when the Agent stops). Newest
first, a database that held no incidents then reads:

- Needs a human: exactly 30. 6 ``TARGET_UNRESOLVED`` and 3
  ``TARGET_AMBIGUOUS`` handoffs, 6 taken over, 6 paused, 5 cancelled, 1
  waiting for a human, 3 resolved. The oldest of them sits at position
  1500 by creation time and is titled
  "checkout payment latency p99 regression".
- In progress: 12 (queued Runs; do not start a worker on this database).
- Resolved: 3 (also under Needs a human, D-L1).
- Closed: 1961.  All: 2003.

Eight services; every alert title starts with ``[seed]``; 43 titles are
longer than 200 characters; ``shop/checkout`` titles mention "latency".

Direct writes, because no product path reaches the state: the 3 resolved
incidents are cancelled through ``control`` and then get
``lifecycle='resolved'`` by one ``UPDATE`` (only a confirmed recovery
watch moves an incident there).

Everything the script creates is tagged: every alert delivery has actor
``dev-seed`` and the targets are the registry's ``dev-seed-*`` keys.
``--reset`` deletes exactly the incidents of ``dev-seed`` deliveries and
the rows keyed to them (foreign keys, ``incident_id`` columns, their web
ledger rows by intake key and incident id), never anything by text pattern;
a seed target is deleted only when no remaining row refers to it. The
connecting role needs DELETE on those tables (the migration owner does;
the runtime web role does not).

Usage::

    OPSPILOT_DSN="host=127.0.0.1 port=55431 dbname=opspilot_dev user=..." \
        .venv/bin/python scripts/dev_seed_incidents.py [--reset] [--reset-only]

The database must already be migrated and local (Unix socket, localhost,
127.0.0.1 or ::1), and its name must hold ``dev``, ``test`` or ``seed`` as a
whole ``_``-separated token; ``service=`` in the DSN and ``PGSERVICE`` /
``PGSERVICEFILE`` are refused. There is no override. Seeding refuses when
seed rows exist; pass ``--reset`` to replace them. Non-seed incidents are
left alone and show up in the printed counts.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

TOTAL = 2003
OLDEST_WAITING_POSITION = 1500
ACTOR = "dev-seed"
TARGET_PREFIX = "dev-seed-"
TITLE_PREFIX = "[seed]"
MARKER_TITLE = "checkout payment latency p99 regression"
#: name -> (namespace, workload, title)
SERVICES = {
    "checkout": ("shop", "checkout", "Checkout p99 latency above 2s for 10 minutes"),
    "cart": ("shop", "cart", "Cart service error ratio above 5%"),
    "payment": ("shop", "payment", "Payment gateway timeouts rising"),
    "inventory": ("shop", "inventory", "Inventory sync lagging behind orders"),
    "search": ("search", "api", "Search API saturation: CPU throttling"),
    "login": ("auth", "login", "Login failures above baseline"),
    "ingest": ("ingest", "worker", "Ingest worker queue backlog growing"),
    "notify": ("comms", "notifier", "Notification delivery delayed"),
}

#: The 29 kinds besides the marker, spread over the newer positions.
_WAITING = (
    ["handoff"] * 6
    + ["ambiguous"] * 3
    + ["takeover"] * 5
    + ["pause"] * 6
    + ["cancel"] * 5
    + ["waiting"] * 1
    + ["resolved"] * 3
)
_IN_PROGRESS = ["queued"] * 12
NEEDS_HUMAN_KINDS = frozenset(_WAITING) | {"marker"}
_LONG = (
    " — the upstream dependency reported intermittent connection resets,"
    " retries amplified load on the shared pool, and the canary in the second"
    " region showed the same pattern before the rollout paused"
)


def plan(total: int = TOTAL, oldest: int = OLDEST_WAITING_POSITION) -> list[str]:
    """Kind of each incident in creation order (oldest first). Position
    ``p`` (1 = newest) is creation index ``total - p``; the marker is the
    oldest incident that needs a human."""
    kinds = ["closed"] * total
    first = total - oldest
    kinds[first] = "marker"
    mixed = _WAITING + _IN_PROGRESS
    random.Random(684).shuffle(mixed)
    step = (total - first - 1) / len(mixed)
    for n, kind in enumerate(mixed):
        kinds[first + 1 + int(n * step)] = kind
    return kinds


def service_for(index: int, kind: str) -> str:
    return "checkout" if kind == "marker" else list(SERVICES)[index % len(SERVICES)]


def title_for(index: int, kind: str) -> str:
    service = service_for(index, kind)
    if kind == "marker":
        base = MARKER_TITLE
    elif kind in ("handoff", "ambiguous"):
        base = f"Unmapped alert from the {service} pipeline"
    else:
        base = SERVICES[service][2]
    if index % 47 == 0:
        base += _LONG
    return f"{TITLE_PREFIX} {base} (#{index})"


def alert_for(index: int, kind: str) -> dict[str, Any]:
    service = service_for(index, kind)
    if kind == "handoff":
        labels = {
            "alertname": f"DevSeedUnmapped{index}",
            "service": f"unmapped-{index}",
        }
    elif kind == "ambiguous":
        labels = {"alertname": f"DevSeedAmbiguous{index}", "service": "ambiguous"}
    else:
        labels = {
            "alertname": f"DevSeed{service.title()}{index}",
            "service": service,
            "namespace": SERVICES[service][0],
        }
    starts = datetime(2026, 11, 1, tzinfo=UTC) + timedelta(minutes=index)
    fingerprint = hashlib.sha256(f"{ACTOR}-{index}-{uuid4()}".encode()).hexdigest()
    return {
        "status": "firing",
        "labels": labels,
        "annotations": {"summary": title_for(index, kind)},
        "startsAt": starts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endsAt": "0001-01-01T00:00:00Z",
        "fingerprint": fingerprint[:16],
        "generatorURL": "http://prometheus:9090/graph",
    }


def registry_entries() -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for name, (namespace, workload, _) in SERVICES.items():
        entries[f"{TARGET_PREFIX}{name}"] = {
            "integration_id": ACTOR,
            "cluster_uid": f"{ACTOR}-cluster",
            "namespace": namespace,
            "workload": workload,
            "match": {"version": 1, "labels": {"service": name}},
        }
    # Two entries with one match: an alert matching both is ambiguous (E7).
    for suffix in ("a", "b"):
        entries[f"{TARGET_PREFIX}ambiguous-{suffix}"] = {
            "integration_id": ACTOR,
            "cluster_uid": f"{ACTOR}-cluster",
            "namespace": "shared",
            "workload": f"ambiguous-{suffix}",
            "match": {"version": 1, "labels": {"service": "ambiguous"}},
        }
    return entries


_DEV_NAME = re.compile(r"^[a-z0-9_]*(^|_)(dev|test|seed)(_[a-z0-9_]*)?$")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def check_dsn(dsn: str, environ: Mapping[str, str] | None = None) -> str:
    """Refuse anything but a local dev or test database: the name must hold
    ``dev``, ``test`` or ``seed`` as a whole ``_``-separated token, and every
    host (from the DSN, else ``PGHOST``/``PGHOSTADDR``) must be a Unix socket
    directory or a loopback address. There is no override."""
    from psycopg.conninfo import conninfo_to_dict

    env = os.environ if environ is None else environ
    info = conninfo_to_dict(dsn)
    # A libpq service file can supply host and dbname over everything
    # checked below (and point anywhere), so none may be in play.
    if info.get("service"):
        raise SystemExit("refusing a DSN with service=: give host and dbname directly")
    for var in ("PGSERVICE", "PGSERVICEFILE"):
        if env.get(var):
            raise SystemExit(f"refusing to run with {var} set: unset it first")
    name = str(info.get("dbname") or env.get("PGDATABASE") or "")
    if not _DEV_NAME.fullmatch(name):
        raise SystemExit(
            f"refusing database {name!r}: the name needs a whole 'dev', 'test'"
            " or 'seed' token (for example opspilot_dev)"
        )
    hosts: list[str] = []
    for field, var in (("host", "PGHOST"), ("hostaddr", "PGHOSTADDR")):
        value = info.get(field)
        value = env.get(var) if value is None else value
        if value:
            hosts.extend(str(value).split(","))
    for host in hosts:
        host = host.strip()
        if host and not host.startswith("/") and host not in _LOCAL_HOSTS:
            raise SystemExit(f"refusing host {host!r}: only a local database is seeded")
    return name


# -- reset ---------------------------------------------------------------------

_FKS = """
SELECT c.conrelid::regclass::text AS child, c.confrelid::regclass::text AS parent,
       array_agg(ca.attname ORDER BY k.n) AS child_cols,
       array_agg(pa.attname ORDER BY k.n) AS parent_cols
FROM pg_constraint c
CROSS JOIN LATERAL unnest(c.conkey, c.confkey) WITH ORDINALITY AS k(ck, pk, n)
JOIN pg_attribute ca ON ca.attrelid=c.conrelid AND ca.attnum=k.ck
JOIN pg_attribute pa ON pa.attrelid=c.confrelid AND pa.attnum=k.pk
WHERE c.contype='f' AND c.connamespace='public'::regnamespace
GROUP BY c.oid, c.conrelid, c.confrelid
"""


def _purge(
    conn: Any,
    fks: list[dict[str, Any]],
    table: str,
    where: str,
    params: tuple[Any, ...],
    depth: int = 0,
) -> int:
    """Delete ``table`` rows matching ``where`` after every row that refers
    to them through a foreign key, depth first. Self references are skipped
    (the seed creates none)."""
    if depth > 12:
        raise SystemExit(f"foreign-key chain too deep at {table}")
    for fk in fks:
        if fk["parent"] != table or fk["child"] == table:
            continue
        child_cols = ",".join(fk["child_cols"])
        parent_cols = ",".join(fk["parent_cols"])
        _purge(
            conn,
            fks,
            fk["child"],
            f"({child_cols}) IN (SELECT {parent_cols} FROM {table} WHERE {where})",
            params,
            depth + 1,
        )
    return int(conn.execute(f"DELETE FROM {table} WHERE {where}", params).rowcount)


def reset(dsn: str, out: Any = sys.stdout) -> int:
    """Delete the incidents this script created and everything attached."""
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        deleted = reset_rows(conn)
    print(f"reset: deleted {deleted} seed incidents", file=out)
    return deleted


def reset_rows(conn: Any) -> int:
    """One transaction's deletes. The seed set is exactly the incidents of
    ``dev-seed`` deliveries; every delete is keyed to those ids (or rows
    that refer to them by foreign key), never by a text pattern. A
    ``dev-seed-`` target goes only when nothing else still refers to it."""
    ids = [
        r["incident_id"]
        for r in conn.execute(
            "SELECT DISTINCT incident_id FROM opspilot_alert_deliveries"
            " WHERE actor=%s AND incident_id IS NOT NULL",
            (ACTOR,),
        ).fetchall()
    ]
    if not ids:
        return 0
    texts = [str(i) for i in ids]
    keys = [
        r["intake_key"]
        for r in conn.execute(
            "SELECT intake_key FROM opspilot_incidents WHERE incident_id = ANY(%s)",
            (ids,),
        ).fetchall()
    ]
    fks = list(conn.execute(_FKS).fetchall())
    tables = [
        r["table_name"]
        for r in conn.execute(
            "SELECT c.table_name FROM information_schema.columns c"
            " JOIN information_schema.tables t ON t.table_schema=c.table_schema"
            " AND t.table_name=c.table_name"
            " WHERE c.table_schema='public' AND c.column_name='incident_id'"
            " AND c.table_name<>'opspilot_incidents' AND t.table_type='BASE TABLE'"
        ).fetchall()
    ]
    for table in tables:
        _purge(conn, fks, table, "incident_id = ANY(%s)", (ids,))
    # The web ledger rows of these incidents (``Workbench.submit``,
    # ``intake_alert`` and ``control`` key them so): the intake envelope
    # under the incident's intake key, the intake sequence under its id,
    # and control rows under ``<incident id>:<idempotency key>``.
    conn.execute(
        "DELETE FROM opspilot_web_ledger WHERE"
        " (namespace='intake' AND key = ANY(%s))"
        " OR (namespace='intake_event' AND key = ANY(%s))"
        " OR (namespace IN ('control_intent','control')"
        " AND split_part(key, ':', 1) = ANY(%s))",
        (keys, texts, texts),
    )
    deleted = _purge(conn, fks, "opspilot_incidents", "incident_id = ANY(%s)", (ids,))
    unreferenced = " AND ".join(
        f"NOT EXISTS (SELECT 1 FROM {fk['child']} x WHERE "
        + " AND ".join(
            f"x.{c}=t.{p}"
            for c, p in zip(fk["child_cols"], fk["parent_cols"], strict=True)
        )
        + ")"
        for fk in fks
        if fk["parent"] == "opspilot_targets"
    )
    conn.execute(
        "DELETE FROM opspilot_targets t WHERE t.resource_uid = ANY(%s)"
        + (f" AND {unreferenced}" if unreferenced else ""),
        (list(registry_entries()),),
    )
    return deleted


# -- seed ----------------------------------------------------------------------


def seed(workbench: Any, kinds: list[str], *, dsn: str, out: Any = sys.stdout) -> None:
    import psycopg

    from opspilot.intake import Principal

    principal = Principal(actor_id=ACTOR, channel="event_token", auth_revision=ACTOR)
    incidents = workbench.incidents

    def control(incident_id: UUID, action: str) -> None:
        summary = incidents.find_incident(incident_id)
        workbench.control(
            incident_id,
            actor_id=ACTOR,
            action=action,
            expected_generation=int(summary.control_generation),
            idempotency_key=f"{ACTOR}-{action}-{uuid4()}",
        )

    resolved: list[UUID] = []
    for index, kind in enumerate(kinds):
        result = workbench.intake_alert(
            principal, alert_for(index, kind), incidents.now()
        )
        if result.incident_id is None:
            raise SystemExit(f"alert {index} opened no incident: {result}")
        expected = "handoff_created" if kind in ("handoff", "ambiguous") else "created"
        if result.outcome != expected:
            raise SystemExit(f"alert {index} ({kind}): outcome {result.outcome}")
        incident_id = UUID(result.incident_id)
        if kind == "closed":
            control(incident_id, "close")
        elif kind in ("marker", "takeover"):
            control(incident_id, "takeover")
        elif kind in ("pause", "cancel"):
            control(incident_id, kind)
        elif kind == "resolved":
            control(incident_id, "cancel")
            resolved.append(incident_id)
        elif kind == "waiting":
            # What a worker does when the Agent stops (ADR-0005).
            lease = incidents.claim(
                incident_id,
                UUID(result.run_id),
                uuid4(),
                dict(workbench.run_versions),
                600,
            )
            incidents.hand_off(lease)
        if index % 200 == 0:
            print(f"{index}/{len(kinds)}", file=out, flush=True)
    if resolved:
        with psycopg.connect(dsn) as conn:
            conn.execute(
                "UPDATE opspilot_incidents SET lifecycle='resolved'"
                " WHERE incident_id = ANY(%s)",
                (resolved,),
            )


def build(dsn: str) -> Any:
    from opspilot.persistence import DurableStore
    from opspilot.web import (
        DurableEventLog,
        DurableEvidenceStore,
        DurableIncidentStore,
        DurableWebLedger,
        Workbench,
    )
    from opspilot.web.store import MappingTargetRegistry

    store = DurableStore(dsn)
    store.install()
    events = DurableEventLog(store)
    events.install()
    evidence = DurableEvidenceStore(store)
    evidence.install()
    ledger = DurableWebLedger(store)
    ledger.install()
    return Workbench(
        incidents=DurableIncidentStore(store),
        events=events,
        evidence=evidence,
        ledger=ledger,
        targets=MappingTargetRegistry(registry_entries()),
        run_versions={"state": ACTOR},
        run_seconds=7 * 24 * 3600,
    )


def print_counts(workbench: Any, out: Any = sys.stdout) -> dict[str, int]:
    counts = dict(workbench.incidents.list_page(tab="needs_human").counts)
    print("counts per tab (whole database):", file=out)
    for key, value in counts.items():
        print(f"  {key}: {value}", file=out)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument(
        "--dsn",
        default=os.environ.get("OPSPILOT_DSN") or os.environ.get("OPSPILOT_WEB_DSN"),
    )
    parser.add_argument("--reset", action="store_true", help="delete seed rows first")
    parser.add_argument("--reset-only", action="store_true", help="delete and stop")
    args = parser.parse_args(argv)
    if not args.dsn:
        raise SystemExit("set OPSPILOT_DSN or OPSPILOT_WEB_DSN, or pass --dsn")
    check_dsn(args.dsn)
    if args.reset or args.reset_only:
        reset(args.dsn)
    workbench = build(args.dsn)
    if not args.reset_only:
        import psycopg

        with psycopg.connect(args.dsn) as conn:
            row = conn.execute(
                "SELECT count(*) FROM opspilot_alert_deliveries WHERE actor=%s",
                (ACTOR,),
            ).fetchone()
        if row and int(row[0]):
            raise SystemExit("seed rows already exist; pass --reset to replace them")
        seed(workbench, plan(), dsn=args.dsn)
    print_counts(workbench)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
