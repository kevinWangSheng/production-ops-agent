"""Business-state seam the workbench drives, plus the small web ledger.

``IncidentStore`` is the slice of ``DurableStore`` the pages and intake
need. ``DurableIncidentStore`` forwards the write paths unchanged and adds
the read-only listings the pages render; it does not touch
``opspilot/persistence.py``. ``WebLedger`` is an insert-if-absent map used
for intake and control idempotency keys; it carries no authority over
state, generation or conclusion.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from psycopg.types.json import Jsonb

from opspilot.investigation.store import DurableStepStore, StepCommitter
from opspilot.persistence import DurableStore, Lease, PersistenceError


@dataclass(frozen=True)
class IncidentSummary:
    incident_id: UUID
    intake_key: str
    state: str
    lifecycle: str
    control_generation: int
    current_run_id: UUID | None
    concluded: bool
    created_at: datetime | None


@dataclass(frozen=True)
class ControlAudit:
    """One committed row of ``opspilot_controls`` (read-only view)."""

    action: str
    expected_generation: int
    resulting_generation: int
    actor: str
    #: ``opspilot_controls.payload`` (PR #31); ``None`` when the column is
    #: absent or the row carried no content.
    payload: Mapping[str, Any] | None = None


class IncidentStore(Protocol):
    def accept(
        self,
        incident_id: UUID,
        run_id: UUID,
        intake_key: str,
        *,
        deadline: datetime,
        budget_limit: int,
        versions: dict[str, str],
    ) -> None: ...

    def control(
        self,
        incident_id: UUID,
        expected_generation: int,
        action: str,
        actor: str,
        payload: dict[str, Any] | None = None,
        *,
        renew_run_id: UUID | None = None,
        renew_deadline: datetime | None = None,
        renew_input: dict[str, Any] | None = None,
    ) -> int:
        """Apply one human decision; ``payload`` carries follow-up/correction content.

        ``renew_run_id`` / ``renew_deadline`` are the fresh Run a note on a
        timed-out Run starts (``DurableStore.control``); unused otherwise.
        """
        ...

    def new_run(
        self,
        incident_id: UUID,
        run_id: UUID,
        *,
        expected_generation: int,
        deadline: datetime,
        budget_limit: int,
        versions: dict[str, str],
        actor: str,
    ) -> int: ...

    def rebuild(self, incident_id: UUID) -> dict[str, Any]: ...

    def claim(
        self,
        incident_id: UUID,
        run_id: UUID,
        owner: UUID,
        versions: dict[str, str],
        lease_seconds: int = 30,
    ) -> Lease: ...

    def publish(
        self, lease: Lease, conclusion: dict[str, Any], *, step_id: UUID
    ) -> bool: ...

    def abandon(self, lease: Lease) -> None: ...

    def hand_off(self, lease: Lease) -> None:
        """Park the leased Run for a human (ADR-0005); ``CONTROL_DENIED`` if fenced."""
        ...

    def sweep_expired_runs(
        self, *, incident_id: UUID | None = None, limit: int = 100
    ) -> tuple[tuple[UUID, UUID], ...]:
        """Park overdue ``running`` Runs (ADR-0005 decision 2); returns what was parked."""
        ...

    def renew_lease(self, lease: Lease, extend_seconds: int) -> datetime | None:
        """Extend a held lease under the write-path fence (PR #35).

        Returns the new ``lease_until``, or ``None`` when the underlying store
        has no renewal capability yet; refusal is ``PersistenceError``
        ``CONTROL_DENIED`` exactly like a fenced commit.
        """
        ...

    @property
    def renewal_supported(self) -> bool:
        """Whether ``renew_lease`` actually extends (PR #35) or is a no-op."""
        ...

    def committer(self, lease: Lease) -> StepCommitter: ...

    def list_incidents(self, *, limit: int = 50) -> tuple[IncidentSummary, ...]: ...

    def find_incident(self, incident_id: UUID) -> IncidentSummary | None: ...

    def run_ids(self, incident_id: UUID) -> frozenset[str]: ...

    def control_audit(self, incident_id: UUID) -> tuple[ControlAudit, ...]: ...

    @property
    def payload_supported(self) -> bool:
        """Whether ``control()`` can carry follow-up/correction content (PR #31)."""
        ...

    def now(self) -> datetime: ...


class WebLedger(Protocol):
    def put(
        self, namespace: str, key: str, value: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], bool]:
        """Insert if absent. Return the stored value and whether this call inserted it."""
        ...

    def get(self, namespace: str, key: str) -> Mapping[str, Any] | None: ...

    def delete(self, namespace: str, key: str) -> None: ...

    def list_prefix(
        self, namespace: str, prefix: str
    ) -> tuple[tuple[str, Mapping[str, Any]], ...]:
        """Rows whose key starts with ``prefix``, in insertion order."""
        ...


class MemoryWebLedger:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}

    def put(
        self, namespace: str, key: str, value: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], bool]:
        existing = self._rows.get((namespace, key))
        if existing is not None:
            return existing, False
        stored = dict(value)
        self._rows[(namespace, key)] = stored
        return stored, True

    def get(self, namespace: str, key: str) -> Mapping[str, Any] | None:
        return self._rows.get((namespace, key))

    def delete(self, namespace: str, key: str) -> None:
        self._rows.pop((namespace, key), None)

    def list_prefix(
        self, namespace: str, prefix: str
    ) -> tuple[tuple[str, Mapping[str, Any]], ...]:
        return tuple(
            (key, value)
            for (space, key), value in self._rows.items()
            if space == namespace and key.startswith(prefix)
        )


class DurableWebLedger:
    def __init__(self, store: DurableStore) -> None:
        self._store = store

    def install(self) -> None:
        with self._store.transaction() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS opspilot_web_ledger (
              namespace text NOT NULL, key text NOT NULL, value jsonb NOT NULL,
              created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
              PRIMARY KEY (namespace, key)
            );
            """)

    def put(
        self, namespace: str, key: str, value: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], bool]:
        with self._store.transaction() as conn:
            inserted = conn.execute(
                "INSERT INTO opspilot_web_ledger(namespace,key,value) VALUES(%s,%s,%s) ON CONFLICT (namespace, key) DO NOTHING RETURNING key",
                (namespace, key, Jsonb(dict(value))),
            ).fetchone()
            row = conn.execute(
                "SELECT value FROM opspilot_web_ledger WHERE namespace=%s AND key=%s",
                (namespace, key),
            ).fetchone()
            if row is None:
                raise PersistenceError("INCONSISTENT_STATE")
            stored: dict[str, Any] = row["value"]
            return stored, inserted is not None

    def list_prefix(
        self, namespace: str, prefix: str
    ) -> tuple[tuple[str, Mapping[str, Any]], ...]:
        with self._store.transaction(snapshot=True) as conn:
            rows = conn.execute(
                "SELECT key,value FROM opspilot_web_ledger WHERE namespace=%s AND key LIKE %s ORDER BY created_at, key",
                (namespace, _like_prefix(prefix)),
            ).fetchall()
        return tuple((row["key"], row["value"]) for row in rows)

    def get(self, namespace: str, key: str) -> Mapping[str, Any] | None:
        with self._store.transaction(snapshot=True) as conn:
            row = conn.execute(
                "SELECT value FROM opspilot_web_ledger WHERE namespace=%s AND key=%s",
                (namespace, key),
            ).fetchone()
        if row is None:
            return None
        stored: dict[str, Any] = row["value"]
        return stored

    def delete(self, namespace: str, key: str) -> None:
        with self._store.transaction() as conn:
            conn.execute(
                "DELETE FROM opspilot_web_ledger WHERE namespace=%s AND key=%s",
                (namespace, key),
            )


def _like_prefix(prefix: str) -> str:
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"


class DurableClock:
    """Product clock: ``now()`` is the database clock, as the lint rule requires."""

    def __init__(self, store: DurableStore) -> None:
        self._store = store

    def now(self) -> datetime:
        with self._store.transaction(snapshot=True) as conn:
            row = conn.execute("SELECT clock_timestamp() AS now").fetchone()
        if row is None:
            raise PersistenceError("STORAGE_UNAVAILABLE")
        stamp: datetime = row["now"]
        return stamp

    def monotonic(self) -> float:
        return time.monotonic()


class DurableIncidentStore:
    """``DurableStore`` plus the read-only listings the pages need."""

    def __init__(self, store: DurableStore) -> None:
        self._store = store
        self._clock = DurableClock(store)
        # PR #31 adds ``payload`` to DurableStore.control (opspilot_controls.payload
        # + opspilot_inputs). Detect it once so this adapter works on both bases.
        self._payload_supported = (
            "payload" in inspect.signature(store.control).parameters
        )

    @property
    def payload_supported(self) -> bool:
        return self._payload_supported

    def accept(
        self,
        incident_id: UUID,
        run_id: UUID,
        intake_key: str,
        *,
        deadline: datetime,
        budget_limit: int,
        versions: dict[str, str],
    ) -> None:
        self._store.accept(
            incident_id,
            run_id,
            intake_key,
            deadline=deadline,
            budget_limit=budget_limit,
            versions=versions,
        )

    def control(
        self,
        incident_id: UUID,
        expected_generation: int,
        action: str,
        actor: str,
        payload: dict[str, Any] | None = None,
        *,
        renew_run_id: UUID | None = None,
        renew_deadline: datetime | None = None,
        renew_input: dict[str, Any] | None = None,
    ) -> int:
        if payload is None or not self._payload_supported:
            # Base without PR #31: the store has no payload column. The
            # workbench then keeps the note in its ledger and composes it
            # into the next attempt's question (read under the lease).
            return self._store.control(
                incident_id,
                expected_generation,
                action,
                actor,
                renew_run_id=renew_run_id,
                renew_deadline=renew_deadline,
                renew_input=renew_input,
            )
        # mypy sees the pre-#31 signature on this branch; the guard above
        # proves the parameter exists at runtime.
        control: Any = self._store.control
        result: int = control(
            incident_id,
            expected_generation,
            action,
            actor,
            payload,
            renew_run_id=renew_run_id,
            renew_deadline=renew_deadline,
            renew_input=renew_input,
        )
        return result

    def new_run(
        self,
        incident_id: UUID,
        run_id: UUID,
        *,
        expected_generation: int,
        deadline: datetime,
        budget_limit: int,
        versions: dict[str, str],
        actor: str,
    ) -> int:
        return self._store.new_run(
            incident_id,
            run_id,
            expected_generation=expected_generation,
            deadline=deadline,
            budget_limit=budget_limit,
            versions=versions,
            actor=actor,
        )

    def rebuild(self, incident_id: UUID) -> dict[str, Any]:
        return self._store.rebuild(incident_id)

    def claim(
        self,
        incident_id: UUID,
        run_id: UUID,
        owner: UUID,
        versions: dict[str, str],
        lease_seconds: int = 30,
    ) -> Lease:
        return self._store.claim(incident_id, run_id, owner, versions, lease_seconds)

    def publish(
        self, lease: Lease, conclusion: dict[str, Any], *, step_id: UUID
    ) -> bool:
        return self._store.publish(lease, conclusion, step_id=step_id)

    def abandon(self, lease: Lease) -> None:
        self._store.abandon(lease)

    def hand_off(self, lease: Lease) -> None:
        self._store.hand_off(lease)

    def sweep_expired_runs(
        self, *, incident_id: UUID | None = None, limit: int = 100
    ) -> tuple[tuple[UUID, UUID], ...]:
        return self._store.sweep_expired_runs(incident_id=incident_id, limit=limit)

    def renew_lease(self, lease: Lease, extend_seconds: int) -> datetime | None:
        # PR #35 adds DurableStore.renew_lease; without it the lease simply
        # keeps the length claim() granted, exactly as before.
        renew = getattr(self._store, "renew_lease", None)
        if renew is None:
            return None
        renewed: datetime = renew(lease, extend_seconds)
        return renewed

    @property
    def renewal_supported(self) -> bool:
        return getattr(self._store, "renew_lease", None) is not None

    def committer(self, lease: Lease) -> StepCommitter:
        return DurableStepStore(self._store, lease)

    def list_incidents(self, *, limit: int = 50) -> tuple[IncidentSummary, ...]:
        with self._store.transaction(snapshot=True) as conn:
            rows = conn.execute(
                "SELECT incident_id,intake_key,state,lifecycle,control_generation,current_run_id,conclusion IS NOT NULL AS concluded,created_at FROM opspilot_incidents ORDER BY created_at DESC, incident_id LIMIT %s",
                (limit,),
            ).fetchall()
        return tuple(_summary(row) for row in rows)

    def find_incident(self, incident_id: UUID) -> IncidentSummary | None:
        with self._store.transaction(snapshot=True) as conn:
            row = conn.execute(
                "SELECT incident_id,intake_key,state,lifecycle,control_generation,current_run_id,conclusion IS NOT NULL AS concluded,created_at FROM opspilot_incidents WHERE incident_id=%s",
                (incident_id,),
            ).fetchone()
        return None if row is None else _summary(row)

    def run_ids(self, incident_id: UUID) -> frozenset[str]:
        with self._store.transaction(snapshot=True) as conn:
            rows = conn.execute(
                "SELECT run_id FROM opspilot_runs WHERE incident_id=%s", (incident_id,)
            ).fetchall()
        return frozenset(str(row["run_id"]) for row in rows)

    def control_audit(self, incident_id: UUID) -> tuple[ControlAudit, ...]:
        columns = "action,expected_generation,resulting_generation,actor"
        if self._payload_supported:
            columns += ",payload"
        with self._store.transaction(snapshot=True) as conn:
            rows = conn.execute(
                f"SELECT {columns} FROM opspilot_controls WHERE incident_id=%s ORDER BY resulting_generation",
                (incident_id,),
            ).fetchall()
        return tuple(
            ControlAudit(
                row["action"],
                int(row["expected_generation"]),
                int(row["resulting_generation"]),
                row["actor"],
                row.get("payload"),
            )
            for row in rows
        )

    def now(self) -> datetime:
        return self._clock.now()


def _summary(row: Mapping[str, Any]) -> IncidentSummary:
    return IncidentSummary(
        incident_id=row["incident_id"],
        intake_key=row["intake_key"],
        state=row["state"],
        lifecycle=row["lifecycle"],
        control_generation=int(row["control_generation"]),
        current_run_id=row["current_run_id"],
        concluded=bool(row["concluded"]),
        created_at=row["created_at"],
    )
