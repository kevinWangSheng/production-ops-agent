"""Alertmanager alert intake: one alert, one transaction (M1-04 E1, E3).

The identity row (``opspilot_alert_identities``), the incident it opens
(with its first Run, or none for a handoff), the intake ledger rows and the
delivery audit row commit together or not at all. Deliveries of one
identity are serialized by a transaction-scoped advisory lock on the
delivery key, so a concurrent first delivery waits and then finds the
identity instead of opening a second incident; the identity's primary key
is the backstop. Nothing here decides a target or composes a question: the
workbench hands both in.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, cast
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from opspilot.persistence.base import Connection, PersistenceError
from opspilot.persistence.incidents import _IncidentOps


class _AlertOps(_IncidentOps):
    def record_alert(
        self,
        delivery: Mapping[str, Any],
        *,
        opening: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record one delivery; open its incident if it is the first firing one.

        ``delivery``: ``delivery_key``, ``fingerprint``, ``starts_at``
        (aware datetime), ``starts_at_raw``, ``status``, ``actor``,
        ``raw_sha256``, ``annotations_sha256``, ``alert_json``,
        ``truncated`` and optionally ``received_at`` (the intake's database
        instant, which an opening delivery's Run frame ends at; the
        database clock when absent). ``opening`` (firing only): the incident to open when
        the identity is new -- ``incident_id``, ``intake_key``, and either
        ``run_id`` with
        ``resource_uid``, ``namespace``, ``workload``, ``deadline``,
        ``budget_limit``, ``versions``, ``input`` and ``ledger`` rows
        ``(namespace, key, value)``, or ``handoff_reason``.

        Returns ``outcome``, ``incident_id``, ``run_id``, ``handoff_reason``
        and ``annotation_revision`` as committed.
        """
        status = delivery["status"]
        if status == "firing" and opening is None:
            raise PersistenceError("INVALID_INPUT")
        key = delivery["delivery_key"]
        with self.transaction() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,)
            )
            identity = conn.execute(
                "SELECT incident_id,run_id,handoff_reason FROM opspilot_alert_identities WHERE delivery_key=%s",
                (key,),
            ).fetchone()
            if status == "firing":
                if identity is None:
                    assert opening is not None
                    identity = self._open(conn, delivery, opening)
                    outcome = "created" if identity["run_id"] else "handoff_created"
                else:
                    outcome = "replayed" if identity["run_id"] else "handoff_replayed"
            elif identity is None:
                outcome = "resolved_recorded"
            else:
                outcome = "resolved_attached"
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
            incident_id = None if identity is None else identity["incident_id"]
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
        }

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
            )
            _ledger(conn, opening.get("ledger") or ())
        else:
            # E5: an incident handed to a human at intake. No Run, no target
            # binding; ``waiting_human`` like a parked Run, and invisible to
            # the claim listing, which joins the current Run.
            inserted = conn.execute(
                "INSERT INTO opspilot_incidents(incident_id,intake_key,state,lifecycle,current_run_id,target_id) VALUES(%s,%s,'waiting_human','open',NULL,NULL) ON CONFLICT DO NOTHING RETURNING incident_id",
                (incident_id, opening["intake_key"]),
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
