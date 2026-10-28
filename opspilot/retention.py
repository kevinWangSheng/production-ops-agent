"""Raw payload retention cleanup (#244 PR-2, C3 section 11).

``python -m opspilot.retention [--apply] [--days N]`` connects with
``OPSPILOT_RETENTION_DSN`` as a login in ``opspilot_retention`` (migration
0023) and empties the five raw payloads of incidents closed at least
``days`` (default and minimum 30) ago: the payload column becomes NULL and
``payload_state`` ``cleaned``; rows, hashes, times and ``evidence.view`` stay.
Without ``--apply`` it only reports. Contract r1-r3 of
``docs/tasks/2026-10-27-244-retention-cleanup.md``:

* Attribution (r3-A): evidence through its Run, an Observer reading through
  its session (an incident session, or a release session through the
  release's incident link), an alert delivery through ``incident_id``, a
  release query result or comparison query through the link's
  ``incident_id`` and ``related_incident_id``. A row is cleaned only when
  every incident it belongs to may be; a row that belongs to none stays.
* An incident may be cleaned when it is ``closed`` with a ``closed_at`` at
  least ``days`` old and nothing keeps it (U2): no Run outside a terminal
  state (the states ``close`` cancels), no ``authorized`` observation
  session, no generation pending (a ``running`` attempt, a live generation
  lease or a regeneration queued by a return), no version
  ``under_review``, no ``active`` knowledge revision sourced from its
  postmortem. Frozen evaluation data has no PostgreSQL carrier (r3-C).
* Merge groups (r3-B): incidents linked by ``merged_into_incident_id`` go
  together; one kept member keeps the group.
* Apply runs one transaction per unit -- a merge group, joined with any
  group a cleanable row also belongs to -- locking the incidents in id
  order, then their Runs and sessions (lock-only grants, r2-L1), and
  rechecks every condition under the locks; F13 rows take no lock (r2-L1:
  every path that adds a knowledge reference holds the incident row
  first). A changed unit writes nothing (``CHANGED_AFTER_SCAN``); a row
  whose update fails is reported and the rest of its unit and the other
  units commit.

Output is one JSON object per line (``candidate`` / ``cleaned`` / ``failed``
per row, ``blocked`` per kept incident that still has a present payload,
a final ``summary``); the exit code is 0, 1 when a row failed, 2 on a
configuration error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, TextIO
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from opspilot import schema

__all__ = ["MIN_DAYS", "RetentionReport", "main", "run_retention"]

MIN_DAYS = 30
DSN_VAR = "OPSPILOT_RETENTION_DSN"
#: the Run states ``control(close)`` cancels: everything not terminal
ACTIVE_RUN_STATES = ("queued", "paused", "running", "waiting_human", "blocked")
#: ``blocked_by`` codes in the order they are reported
CODES = (
    "NOT_CLOSED",
    "CLOSED_AT_MISSING",
    "WITHIN_RETENTION",
    "ACTIVE_RUN",
    "ACTIVE_OBSERVATION",
    "POSTMORTEM_GENERATION_PENDING",
    "DRAFT_UNDER_REVIEW",
    "ACTIVE_KNOWLEDGE",
)
#: table -> (payload column, key columns of the object id)
PAYLOADS: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "opspilot_evidence": ("raw", ("evidence_id",)),
    "opspilot_observation_signal_readings": ("raw", ("sample_id", "signal_name")),
    "opspilot_release_query_results": ("raw", ("sample_id", "query_key")),
    "opspilot_release_comparison_queries": ("raw", ("comparison_id", "query_key")),
    "opspilot_alert_deliveries": ("alert_json", ("delivery_id",)),
}


@dataclass(frozen=True)
class RetentionReport:
    lines: tuple[dict[str, Any], ...]
    failed: int


@dataclass(frozen=True)
class _Row:
    table: str
    keys: tuple[Any, ...]
    raw_sha256: str | None
    incidents: tuple[UUID, ...]

    @property
    def object_id(self) -> str:
        return ":".join(str(key) for key in self.keys)


# --- scan


def _links(conn: psycopg.Connection[Any]) -> dict[UUID, tuple[UUID, ...]]:
    """A release observation -> the incidents its anomaly link names."""
    return {
        row["observation_id"]: tuple(
            i for i in (row["incident_id"], row["related_incident_id"]) if i
        )
        for row in conn.execute(
            "SELECT observation_id, incident_id, related_incident_id "
            "FROM opspilot_release_incident_links"
        )
    }


def _present_rows(conn: psycopg.Connection[Any]) -> list[_Row]:
    """Every present payload row with the incidents it belongs to (r3-A);
    rows that belong to none are left out (they are never cleaned)."""
    links = _links(conn)
    rows: list[_Row] = []

    def add(
        table: str, keys: tuple[Any, ...], digest: Any, owners: Iterable[Any]
    ) -> None:
        incidents = tuple(sorted({o for o in owners if o is not None}, key=str))
        if incidents:
            rows.append(_Row(table, keys, digest, incidents))

    for r in conn.execute(
        "SELECT e.evidence_id, e.raw_sha256, r.incident_id FROM opspilot_evidence e "
        "LEFT JOIN opspilot_runs r ON r.run_id::text = e.run_id "
        "WHERE e.payload_state = 'present'"
    ):
        add(
            "opspilot_evidence",
            (r["evidence_id"],),
            r["raw_sha256"],
            [r["incident_id"]],
        )
    for r in conn.execute(
        "SELECT g.sample_id, g.signal_name, g.raw_sha256, o.incident_id, "
        "o.release_observation_id FROM opspilot_observation_signal_readings g "
        "JOIN opspilot_observation_samples s USING (sample_id) "
        "JOIN opspilot_observation_sessions o ON o.session_id = s.session_id "
        "WHERE g.payload_state = 'present'"
    ):
        owners = (
            [r["incident_id"]]
            if r["incident_id"] is not None
            else links.get(r["release_observation_id"], ())
        )
        add(
            "opspilot_observation_signal_readings",
            (r["sample_id"], r["signal_name"]),
            r["raw_sha256"],
            owners,
        )
    for r in conn.execute(
        "SELECT q.sample_id, q.query_key, q.raw_sha256, o.release_observation_id "
        "FROM opspilot_release_query_results q "
        "JOIN opspilot_observation_samples s USING (sample_id) "
        "JOIN opspilot_observation_sessions o ON o.session_id = s.session_id "
        "WHERE q.payload_state = 'present'"
    ):
        add(
            "opspilot_release_query_results",
            (r["sample_id"], r["query_key"]),
            r["raw_sha256"],
            links.get(r["release_observation_id"], ()),
        )
    for r in conn.execute(
        "SELECT q.comparison_id, q.query_key, q.raw_sha256, c.observation_id "
        "FROM opspilot_release_comparison_queries q "
        "JOIN opspilot_release_comparisons c USING (comparison_id) "
        "WHERE q.payload_state = 'present'"
    ):
        add(
            "opspilot_release_comparison_queries",
            (r["comparison_id"], r["query_key"]),
            r["raw_sha256"],
            links.get(r["observation_id"], ()),
        )
    for r in conn.execute(
        "SELECT delivery_id, raw_sha256, incident_id FROM opspilot_alert_deliveries "
        "WHERE payload_state = 'present' AND incident_id IS NOT NULL"
    ):
        add(
            "opspilot_alert_deliveries",
            (r["delivery_id"],),
            r["raw_sha256"],
            [r["incident_id"]],
        )
    return rows


def _merge_groups(
    conn: psycopg.Connection[Any], incidents: Iterable[UUID]
) -> dict[UUID, frozenset[UUID]]:
    """Each of ``incidents`` -> its merge group (r3-B), every pointer
    followed."""
    incidents = list(incidents)
    parent: dict[UUID, UUID] = {}

    def find(x: UUID) -> UUID:
        while parent.get(x, x) != x:
            x = parent[x]
        return x

    def union(a: UUID, b: UUID) -> None:
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb, key=str)] = min(ra, rb, key=str)

    for incident in incidents:
        parent.setdefault(incident, incident)
    for row in conn.execute(
        "SELECT incident_id, merged_into_incident_id FROM opspilot_incidents "
        "WHERE merged_into_incident_id IS NOT NULL"
    ):
        union(row["incident_id"], row["merged_into_incident_id"])
    members: dict[UUID, set[UUID]] = {}
    for incident in parent:
        members.setdefault(find(incident), set()).add(incident)
    wanted = set(incidents)
    return {
        incident: frozenset(members[find(incident)])
        for incident in parent
        if incident in wanted
    }


def _reasons(
    conn: psycopg.Connection[Any], incidents: Iterable[UUID], days: int
) -> dict[UUID, list[str]]:
    """What keeps each incident (U1, U2, U6); an empty list: nothing."""
    ids = sorted(set(incidents), key=str)
    found: dict[UUID, set[str]] = {i: set() for i in ids}
    for r in conn.execute(
        "SELECT incident_id, lifecycle, closed_at IS NULL AS missing, "
        "closed_at > clock_timestamp() - make_interval(days => %s) AS recent "
        "FROM opspilot_incidents WHERE incident_id = ANY(%s)",
        (days, ids),
    ):
        codes = found[r["incident_id"]]
        if r["lifecycle"] != "closed":
            codes.add("NOT_CLOSED")
        elif r["missing"]:
            codes.add("CLOSED_AT_MISSING")
        elif r["recent"]:
            codes.add("WITHIN_RETENTION")
    checks = (
        (
            "ACTIVE_RUN",
            "SELECT DISTINCT incident_id FROM opspilot_runs "
            "WHERE incident_id = ANY(%s) AND state = ANY(%s)",
            (ids, list(ACTIVE_RUN_STATES)),
        ),
        (
            "ACTIVE_OBSERVATION",
            "SELECT DISTINCT incident_id FROM opspilot_observation_sessions "
            "WHERE incident_id = ANY(%s) AND state = 'authorized'",
            (ids,),
        ),
        (
            "POSTMORTEM_GENERATION_PENDING",
            "SELECT incident_id FROM opspilot_postmortem_generation_attempts "
            "WHERE incident_id = ANY(%s) AND status = 'running' "
            "UNION SELECT incident_id FROM opspilot_postmortem_generation_jobs "
            "WHERE incident_id = ANY(%s) AND (pending_regeneration_version IS NOT NULL "
            "OR lease_until > clock_timestamp())",
            (ids, ids),
        ),
        (
            "DRAFT_UNDER_REVIEW",
            "SELECT DISTINCT p.incident_id FROM opspilot_postmortem_versions v "
            "JOIN opspilot_postmortems p USING (postmortem_id) "
            "WHERE p.incident_id = ANY(%s) AND v.state = 'under_review'",
            (ids,),
        ),
        (
            "ACTIVE_KNOWLEDGE",
            "SELECT DISTINCT p.incident_id FROM opspilot_knowledge_revision_states k "
            "JOIN opspilot_knowledge_revisions r "
            "ON r.entry_id = k.entry_id AND r.revision = k.revision "
            "JOIN opspilot_postmortems p ON p.postmortem_id = r.source_postmortem_id "
            "WHERE p.incident_id = ANY(%s) AND k.state = 'active'",
            (ids,),
        ),
    )
    for code, statement, args in checks:
        for r in conn.execute(statement, args):
            found[r["incident_id"]].add(code)
    return {i: [c for c in CODES if c in found[i]] for i in ids}


@dataclass
class _Plan:
    rows: list[_Row]
    groups: dict[UUID, frozenset[UUID]]
    reasons: dict[UUID, list[str]]

    def cleanable(self, incident: UUID) -> bool:
        return all(
            not self.reasons.get(m, ["NOT_CLOSED"]) for m in self.groups[incident]
        )

    def blocked_by(self, incident: UUID) -> list[str]:
        own = self.reasons[incident]
        return own or ["MERGE_GROUP_BLOCKED"]


def _scan(conn: psycopg.Connection[Any], days: int) -> _Plan:
    rows = _present_rows(conn)
    owners = {i for row in rows for i in row.incidents}
    groups = _merge_groups(conn, owners)
    members = {m for group in groups.values() for m in group}
    return _Plan(rows, groups, _reasons(conn, members, days))


def _units(plan: _Plan, rows: list[_Row]) -> list[tuple[frozenset[UUID], list[_Row]]]:
    """Merge groups joined by the rows that belong to several of them."""
    parent: dict[UUID, UUID] = {}

    def find(x: UUID) -> UUID:
        while parent.get(x, x) != x:
            x = parent[x]
        return x

    for row in rows:
        anchors = [min(plan.groups[i], key=str) for i in row.incidents]
        for a in anchors:
            parent.setdefault(a, a)
        for a in anchors[1:]:
            ra, rb = find(anchors[0]), find(a)
            if ra != rb:
                parent[max(ra, rb, key=str)] = min(ra, rb, key=str)
    units: dict[UUID, tuple[set[UUID], list[_Row]]] = {}
    for row in rows:
        root = find(min(plan.groups[row.incidents[0]], key=str))
        incidents, unit_rows = units.setdefault(root, (set(), []))
        for i in row.incidents:
            incidents |= plan.groups[i]
        unit_rows.append(row)
    return [
        (frozenset(incidents), unit_rows)
        for _, (incidents, unit_rows) in sorted(
            units.items(), key=lambda kv: str(kv[0])
        )
    ]


# --- apply


def _lock(conn: psycopg.Connection[Any], incidents: frozenset[UUID]) -> None:
    ids = sorted(incidents, key=str)
    for incident in ids:
        conn.execute(
            "SELECT 1 FROM opspilot_incidents WHERE incident_id = %s FOR UPDATE",
            (incident,),
        )
    conn.execute(
        "SELECT 1 FROM opspilot_runs WHERE incident_id = ANY(%s) "
        "ORDER BY run_id FOR UPDATE",
        (ids,),
    )
    conn.execute(
        "SELECT 1 FROM opspilot_observation_sessions WHERE incident_id = ANY(%s) "
        "ORDER BY session_id FOR UPDATE",
        (ids,),
    )


def _clean(conn: psycopg.Connection[Any], row: _Row) -> bool:
    """The guarded present -> cleaned update; False when the row is no
    longer present (cleaned meanwhile)."""
    column, keys = PAYLOADS[row.table]
    statement = sql.SQL(
        "UPDATE {} SET {} = NULL, payload_state = 'cleaned' WHERE {} "
        "AND payload_state = 'present'"
    ).format(
        sql.Identifier(row.table),
        sql.Identifier(column),
        sql.SQL(" AND ").join(
            sql.SQL("{} = %s").format(sql.Identifier(k)) for k in keys
        ),
    )
    return conn.execute(statement, row.keys).rowcount == 1


def _line(kind: str, row: _Row, **extra: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "incident_id": str(row.incidents[0]),
        "table": row.table,
        "object_id": row.object_id,
        "raw_sha256": row.raw_sha256,
        **extra,
    }


def run_retention(
    dsn: str,
    *,
    days: int = MIN_DAYS,
    apply: bool = False,
    after_scan: Callable[[], None] | None = None,
) -> RetentionReport:
    """One cleanup pass; see the module docstring. ``after_scan`` runs once
    after the scan and before any lock (a race seam)."""
    if isinstance(days, bool) or not isinstance(days, int) or days < MIN_DAYS:
        raise ValueError("RETENTION_DAYS_INVALID")
    lines: list[dict[str, Any]] = []
    candidates = cleaned = failed = 0
    # autocommit: every ``transaction()`` below is a real transaction
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        schema.verify_head(conn)
        with conn.transaction():
            plan = _scan(conn, days)
        ready = [r for r in plan.rows if all(plan.cleanable(i) for i in r.incidents)]
        candidates = len(ready)
        held = sorted(
            {i for row in plan.rows for i in row.incidents if not plan.cleanable(i)},
            key=str,
        )
        blocked = [
            {"kind": "blocked", "incident_id": str(i), "blocked_by": plan.blocked_by(i)}
            for i in held
        ]
        if after_scan is not None:
            after_scan()
        if not apply:
            lines.extend(_line("candidate", row) for row in ready)
        else:
            for incidents, rows in _units(plan, ready):
                outcome = _apply_unit(conn, incidents, rows, days)
                if outcome is None:
                    blocked.extend(
                        {
                            "kind": "blocked",
                            "incident_id": str(i),
                            "blocked_by": ["CHANGED_AFTER_SCAN"],
                        }
                        for i in sorted(
                            {i for row in rows for i in row.incidents}, key=str
                        )
                    )
                    continue
                for line in outcome:
                    lines.append(line)
                    if line["kind"] == "cleaned":
                        cleaned += 1
                    else:
                        failed += 1
    lines.extend(blocked)
    lines.append(
        {
            "kind": "summary",
            "apply": apply,
            "days": days,
            "candidates": candidates,
            "cleaned": cleaned,
            "failed": failed,
            "blocked": len(blocked),
        }
    )
    return RetentionReport(lines=tuple(lines), failed=failed)


def _apply_unit(
    conn: psycopg.Connection[Any],
    incidents: frozenset[UUID],
    rows: list[_Row],
    days: int,
) -> list[dict[str, Any]] | None:
    """One transaction: lock, recheck, clean row by row. ``None`` when the
    recheck fails (nothing written)."""
    out: list[dict[str, Any]] = []
    with conn.transaction():
        _lock(conn, incidents)
        groups = _merge_groups(conn, incidents)
        members = {m for group in groups.values() for m in group}
        if members != set(incidents) or any(_reasons(conn, members, days).values()):
            return None
        for row in rows:
            try:
                with conn.transaction():
                    if not _clean(conn, row):
                        continue
            except psycopg.Error as exc:
                out.append(_line("failed", row, error=type(exc).__name__))
                continue
            out.append(_line("cleaned", row))
    return out


# --- process entry


def main(argv: list[str] | None = None, *, stdout: TextIO | None = None) -> int:
    out = stdout or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m opspilot.retention")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--days", default=str(MIN_DAYS))
    args = parser.parse_args(argv)
    try:
        days = int(args.days)
    except ValueError:
        days = -1
    if days < MIN_DAYS:
        print("RETENTION_DAYS_INVALID", file=sys.stderr)
        return 2
    dsn = os.environ.get(DSN_VAR)
    if not dsn:
        print(f"{DSN_VAR} is not set", file=sys.stderr)
        return 2
    try:
        report = run_retention(dsn, days=days, apply=args.apply)
    except schema.SchemaNotMigrated as exc:
        print(f"SCHEMA_NOT_MIGRATED: {exc}", file=sys.stderr)
        return 2
    except psycopg.OperationalError as exc:
        print(f"STORAGE_UNAVAILABLE: {type(exc).__name__}", file=sys.stderr)
        return 2
    for line in report.lines:
        print(json.dumps(line, sort_keys=True), file=out)
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
