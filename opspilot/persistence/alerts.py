"""Alertmanager alert intake: one alert, one transaction (M1-04 E1, E3).

The identity row (``opspilot_alert_identities``), the incident it opens
(with its first Run, or none for a handoff), the intake ledger rows and the
delivery audit row commit together or not at all. Deliveries of one
fingerprint are serialized by a transaction-scoped advisory lock on the
fingerprint alone (F2 R-C1), so a concurrent first delivery waits and then
finds the identity -- or, for another ``startsAt`` of the same fingerprint,
the incident it should attach to -- instead of opening a second incident;
the identity's primary key is the backstop. Nothing here decides a target or
composes a question: the workbench hands both in.

A firing alert with a new identity on a target whose same-fingerprint alert
incident is not closed is attached to it (F2 U1-A, C1-A): an ``attached``
delivery row and one event input, no identity row, no Run; a higher
severity raises the incident's claim priority (B1 U-B1-2). An incident a
release anomaly opened on the same target is a candidate too, unless another
fingerprint already attached to it (#343 U1-A, the claim), so an alert that
arrives after the release incident joins it the way a release joins an
earlier alert incident. When every candidate is closed, the new incident
relates to the latest of them (``choose_correlation``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

from psycopg.types.json import Jsonb

from opspilot.persistence.base import Connection, PersistenceError
from opspilot.persistence.controls import append_input_in
from opspilot.persistence.correlation import (
    Correlation,
    CorrelationCandidate,
    choose_correlation,
)
from opspilot.persistence.incidents import _check_priority, _IncidentOps

#: The delivery outcomes migration 0018 allows.
OUTCOMES = (
    "created",
    "replayed",
    "handoff_created",
    "handoff_replayed",
    "resolved_attached",
    "resolved_recorded",
    "attached",
)
#: Equal to ``opspilot.web.service._INTAKE_NAMESPACE`` (R-C4): an attached
#: episode's event id derives from its delivery key like the intake ids do.
ATTACH_NAMESPACE = UUID("0f4c9d3e-2b7a-4a6e-9c1d-5e8f7a6b3c21")
#: #245 U5 ②: the most merge pointers one correlation follows.
_MERGE_CHAIN_LIMIT = 16


class _AlertOps(_IncidentOps):
    def record_alert(
        self,
        delivery: Mapping[str, Any],
        *,
        opening: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record one delivery; open its incident if it is the first firing
        one, or attach it to the open incident of the same fingerprint and
        target, or to an unclaimed release incident on that target (#343).

        ``delivery``: ``delivery_key``, ``fingerprint``, ``starts_at``
        (aware datetime), ``starts_at_raw``, ``status``, ``actor``,
        ``raw_sha256``, ``annotations_sha256``, ``alert_json``,
        ``truncated`` and optionally ``received_at`` (the intake's database
        instant, which an opening delivery's Run frame ends at; the
        database clock when absent). ``opening`` (firing only): the incident to open when
        the identity is new and no open incident takes it -- ``incident_id``,
        ``intake_key``, and either
        ``run_id`` with
        ``resource_uid``, ``namespace``, ``workload``, ``deadline``,
        ``budget_limit``, ``versions``, ``input`` and ``ledger`` rows
        ``(namespace, key, value)``, or ``handoff_reason``.

        Returns ``outcome``, ``incident_id``, ``run_id``, ``handoff_reason``,
        ``annotation_revision`` and ``related_incident_id`` as committed.
        """
        status = delivery["status"]
        if status == "firing" and opening is None:
            raise PersistenceError("INVALID_INPUT")
        key = delivery["delivery_key"]
        with self.transaction() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("alertmanager:fp:" + delivery["fingerprint"],),
            )
            identity = conn.execute(
                "SELECT incident_id,run_id,handoff_reason FROM opspilot_alert_identities WHERE delivery_key=%s",
                (key,),
            ).fetchone()
            # C1-A: an attached episode has no identity row; its first
            # ``attached`` delivery is what keeps it on that incident.
            attached = (
                None
                if identity is not None
                else conn.execute(
                    "SELECT incident_id FROM opspilot_alert_deliveries WHERE delivery_key=%s AND outcome='attached' ORDER BY delivery_id LIMIT 1",
                    (key,),
                ).fetchone()
            )
            last = conn.execute(
                "SELECT annotation_revision,annotations_sha256 FROM opspilot_alert_deliveries WHERE delivery_key=%s ORDER BY delivery_id DESC LIMIT 1",
                (key,),
            ).fetchone()
            if last is None:
                revision = 1
            elif last["annotations_sha256"] == delivery["annotations_sha256"]:
                revision = int(last["annotation_revision"])
            else:
                revision = int(last["annotation_revision"]) + 1
            incident_id: UUID | None = None
            related: UUID | None = None
            if identity is not None:
                incident_id = identity["incident_id"]
                if status == "firing":
                    outcome = "replayed" if identity["run_id"] else "handoff_replayed"
                else:
                    outcome = "resolved_attached"
            elif attached is not None:
                # R-C3: a repeat, an annotation change or the resolved
                # notification of an attached episode adds no event.
                incident_id = attached["incident_id"]
                outcome = "attached" if status == "firing" else "resolved_attached"
            elif status != "firing":
                outcome = "resolved_recorded"
            else:
                assert opening is not None
                correlation, released = self._correlate(conn, delivery, opening)
                if correlation.attach_to is not None:
                    incident_id = correlation.attach_to
                    _append_attached_event(
                        conn,
                        delivery,
                        incident_id,
                        revision,
                        release=incident_id in released,
                    )
                    _escalate_priority(conn, incident_id, opening.get("priority", 2))
                    outcome = "attached"
                else:
                    related = correlation.related_to
                    identity = self._open(conn, delivery, opening, related)
                    incident_id = identity["incident_id"]
                    outcome = "created" if identity["run_id"] else "handoff_created"
            conn.execute(
                "INSERT INTO opspilot_alert_deliveries(delivery_key,fingerprint,starts_at,starts_at_raw,status,outcome,incident_id,actor,raw_sha256,annotations_sha256,annotation_revision,alert_json,truncated,received_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,COALESCE(%s,clock_timestamp()))",
                (
                    key,
                    delivery["fingerprint"],
                    delivery["starts_at"],
                    delivery["starts_at_raw"],
                    status,
                    outcome,
                    incident_id,
                    delivery["actor"],
                    delivery["raw_sha256"],
                    delivery["annotations_sha256"],
                    revision,
                    delivery["alert_json"],
                    delivery["truncated"],
                    delivery.get("received_at"),
                ),
            )
        return {
            "outcome": outcome,
            "incident_id": incident_id,
            "run_id": None if identity is None else identity["run_id"],
            "handoff_reason": None if identity is None else identity["handoff_reason"],
            "annotation_revision": revision,
            "related_incident_id": related,
        }

    def _correlate(
        self,
        conn: Connection,
        delivery: Mapping[str, Any],
        opening: Mapping[str, Any],
    ) -> tuple[Correlation, frozenset[UUID]]:
        """C3 for an alert: the candidates are the incidents opened by the
        same fingerprint on the same resolved target, and those a release
        anomaly opened on that target (``created``/``created_related`` link)
        that no other fingerprint has attached to (#343 U1-A); a handoff (no
        target) is never correlated, nor is a manual incident. Either way the
        candidate must be bound to that target now (#245 U5 ①): an incident a
        human rebound elsewhere keeps its identity row as history, but its old
        target's alerts no longer attach to it. Returns the decision and the
        release-opened candidates.

        Lock order (R-C2): the fingerprint lock is held; then the scope and
        the target suspension row (FOR SHARE, the order every control and
        suspension write takes) and only then the candidate incident rows,
        FOR UPDATE in id order, so a concurrent close or reopen either
        commits before the lifecycle is read here or waits for this commit.
        The claim is read by a statement of its own after those row locks
        (#343 R2): under READ COMMITTED it sees an attachment another
        fingerprint committed while this one waited, so two fingerprints
        never both claim one release incident.
        """
        resource_uid = opening.get("resource_uid")
        if opening.get("run_id") is None or resource_uid is None:
            return Correlation(attach_to=None, related_to=None), frozenset()
        self._lock_scope(conn, None)
        target = conn.execute(
            "SELECT target_id FROM opspilot_targets WHERE resource_uid=%s",
            (resource_uid,),
        ).fetchone()
        target_id = None if target is None else target["target_id"]
        if target_id is not None:
            self._require_row(
                conn.execute(
                    "SELECT suspended FROM opspilot_target_suspensions WHERE target_id=%s FOR SHARE",
                    (target_id,),
                )
            )
        fingerprint = delivery["fingerprint"]
        rows = conn.execute(
            "SELECT i.incident_id,i.lifecycle,i.created_at,i.merged_into_incident_id,EXISTS(SELECT 1 FROM opspilot_release_incident_links l WHERE l.incident_id=i.incident_id AND l.disposition IN ('created','created_related')) AS released FROM opspilot_incidents i WHERE (i.target_id=%s AND EXISTS(SELECT 1 FROM opspilot_alert_identities a WHERE a.incident_id=i.incident_id AND a.fingerprint=%s AND a.target_id=%s AND a.handoff_reason IS NULL)) OR (i.target_id=%s AND EXISTS(SELECT 1 FROM opspilot_release_incident_links l WHERE l.incident_id=i.incident_id AND l.disposition IN ('created','created_related'))) ORDER BY i.incident_id FOR UPDATE OF i",
            (target_id, fingerprint, resource_uid, target_id),
        ).fetchall()
        released = [row["incident_id"] for row in rows if row["released"]]
        claimed: set[UUID] = set()
        if released:
            claimed = {
                row["incident_id"]
                for row in conn.execute(
                    "SELECT DISTINCT incident_id FROM opspilot_alert_deliveries WHERE incident_id=ANY(%s) AND outcome='attached' AND fingerprint<>%s",
                    (released, fingerprint),
                ).fetchall()
            }
        candidates: dict[UUID, CorrelationCandidate] = {}
        for row in rows:
            if row["incident_id"] in claimed:
                continue
            resolved = self._merge_survivor(conn, row, target_id)
            if resolved is not None:
                candidates[resolved.incident_id] = resolved
        correlation = choose_correlation(list(candidates.values()))
        return correlation, frozenset(released) - claimed

    def _merge_survivor(
        self, conn: Connection, row: Mapping[str, Any], target_id: UUID | None
    ) -> CorrelationCandidate | None:
        """#245 U5 ②: a candidate merged into another incident stands for
        its survivor, followed along the pointer chain (acyclic: a merged
        incident is never merged again, G4; bounded all the same). Each
        survivor row is locked FOR UPDATE as it is read; one now bound to
        another target (rebound after the merge) is not a candidate (U5 ①).
        A chain longer than the bound stops at the last row read."""
        current = row
        for _ in range(_MERGE_CHAIN_LIMIT):
            if current["merged_into_incident_id"] is None:
                break
            survivor = conn.execute(
                "SELECT incident_id,lifecycle,created_at,merged_into_incident_id,target_id FROM opspilot_incidents WHERE incident_id=%s FOR UPDATE",
                (current["merged_into_incident_id"],),
            ).fetchone()
            if survivor is None:
                break
            if survivor["target_id"] != target_id:
                return None
            current = survivor
        return CorrelationCandidate(
            current["incident_id"], current["lifecycle"], current["created_at"]
        )

    def _alert_target(self, conn: Connection, resource_uid: str) -> UUID:
        """Register the bound target inside the alert's transaction, so a
        failed alert leaves no target row either (independent review P1).
        Same rows as ``register_target``; a concurrent first registration of
        the same uid is waited for and reused instead of failing."""
        inserted = conn.execute(
            "INSERT INTO opspilot_targets(target_id,resource_uid) VALUES(%s,%s) ON CONFLICT (resource_uid) DO NOTHING RETURNING target_id",
            (uuid4(), resource_uid),
        ).fetchone()
        if inserted is not None:
            conn.execute(
                "INSERT INTO opspilot_target_suspensions(target_id) VALUES(%s)",
                (inserted["target_id"],),
            )
            return cast(UUID, inserted["target_id"])
        existing = self._require_row(
            conn.execute(
                "SELECT target_id FROM opspilot_targets WHERE resource_uid=%s",
                (resource_uid,),
            )
        )
        return cast(UUID, existing["target_id"])

    def _open(
        self,
        conn: Connection,
        delivery: Mapping[str, Any],
        opening: Mapping[str, Any],
        related_incident_id: UUID | None = None,
    ) -> dict[str, Any]:
        incident_id: UUID = opening["incident_id"]
        run_id: UUID | None = opening.get("run_id")
        if run_id is not None:
            self._accept_in(
                conn,
                incident_id,
                run_id,
                opening["intake_key"],
                deadline=opening["deadline"],
                budget_limit=opening["budget_limit"],
                versions=dict(opening["versions"]),
                input=opening.get("input"),
                target_id=self._alert_target(conn, opening["resource_uid"]),
                priority=opening.get("priority", 2),
                related_incident_id=related_incident_id,
            )
            _ledger(conn, opening.get("ledger") or ())
        else:
            # E5: an incident handed to a human at intake. No Run, no target
            # binding; ``waiting_human`` like a parked Run, and invisible to
            # the claim listing, which joins the current Run.
            # Its priority is recorded like any alert incident's (U-B1-2),
            # though without a Run nothing ever claims it.
            priority = opening.get("priority", 2)
            _check_priority(priority)
            inserted = conn.execute(
                "INSERT INTO opspilot_incidents(incident_id,intake_key,state,lifecycle,current_run_id,target_id,priority,related_incident_id) VALUES(%s,%s,'waiting_human','open',NULL,NULL,%s,%s) ON CONFLICT DO NOTHING RETURNING incident_id",
                (incident_id, opening["intake_key"], priority, related_incident_id),
            ).fetchone()
            if inserted is None:
                raise PersistenceError("IDENTITY_CONFLICT")
        conn.execute(
            "INSERT INTO opspilot_alert_identities(delivery_key,fingerprint,starts_at,starts_at_raw,incident_id,run_id,target_id,namespace,workload,handoff_reason) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                delivery["delivery_key"],
                delivery["fingerprint"],
                delivery["starts_at"],
                delivery["starts_at_raw"],
                incident_id,
                run_id,
                opening.get("resource_uid"),
                opening.get("namespace"),
                opening.get("workload"),
                opening.get("handoff_reason"),
            ),
        )
        return {
            "incident_id": incident_id,
            "run_id": run_id,
            "handoff_reason": opening.get("handoff_reason"),
        }


def _ledger(
    conn: Connection, rows: Sequence[tuple[str, str, Mapping[str, Any]]]
) -> None:
    """The workbench's intake rows, insert-if-absent like ``DurableWebLedger``."""
    for namespace, key, value in rows:
        conn.execute(
            "INSERT INTO opspilot_web_ledger(namespace,key,value) VALUES(%s,%s,%s) ON CONFLICT (namespace, key) DO NOTHING",
            (namespace, key, Jsonb(dict(value))),
        )


def _escalate_priority(conn: Connection, incident_id: UUID, priority: int) -> None:
    """U-B1-2: an attached episode only ever raises its incident's claim
    priority (lower value first), never lowers it. Nothing else of the
    incident changes: no Run, no state or control field."""
    _check_priority(priority)
    conn.execute(
        "UPDATE opspilot_incidents SET priority=%s WHERE incident_id=%s AND priority>%s",
        (priority, incident_id, priority),
    )


def _append_attached_event(
    conn: Connection,
    delivery: Mapping[str, Any],
    incident_id: UUID,
    annotation_revision: int,
    *,
    release: bool = False,
) -> None:
    """The one event of an attached episode (R-C4): whitelisted fields of
    the validated identity only. Annotations stay out; they reach a model
    only as the opening Run's redacted alert context (M1-04 K2). On an
    incident a release anomaly opened (#343 R3) the alert has not fired
    there before, so the text says so; the keys are the same."""
    key = delivery["delivery_key"]
    starts_at = (
        delivery["starts_at"].astimezone(UTC).replace(microsecond=0).isoformat()
    ).replace("+00:00", "Z")
    fingerprint = delivery["fingerprint"]
    status = delivery["status"]
    if release:
        text = (
            f"Alertmanager alert {fingerprint} fired with startsAt "
            f"{starts_at} (status {status}) on this incident's target; "
            "attached to this release-anomaly incident without a new Run."
        )
    else:
        text = (
            f"Alertmanager alert {fingerprint} fired again with startsAt "
            f"{starts_at} (status {status}) on this incident's target; "
            "attached to this incident without a new Run."
        )
    content = {
        "channel": "alertmanager",
        "text": text,
        "fingerprint": fingerprint,
        "starts_at": starts_at,
        "status": status,
        "annotation_revision": annotation_revision,
        "delivery_key": key,
    }
    append_input_in(
        conn,
        incident_id,
        uuid5(ATTACH_NAMESPACE, f"attach:{key}:{incident_id}"),
        content,
        actor=delivery["actor"],
    )
