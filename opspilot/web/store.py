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
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from psycopg.types.json import Jsonb

from opspilot.investigation.store import DurableStepStore, StepCommitter
from opspilot.observation.store import ObservationStore
from opspilot.persistence import DurableStore, Lease, PersistenceError
from opspilot.schema import load_target_identities


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
    run_state: str | None = None

    @property
    def control(self) -> str | None:
        """Human-control marker (C3 section 4): only ``paused``/``cancelled``.

        ``state`` is a mirror of human control, not execution progress; every
        other stored value (``queued``, ``running``, ...) is "in progress" and
        is not shown. The Run badge carries progress.
        """
        return self.state if self.state in ("paused", "cancelled") else None


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


@dataclass(frozen=True)
class TargetIdentity:
    """The immutable identity intake registers for an operator's target id.

    The four fields of ``opspilot.domain.intake.Target`` that identify a
    target (user decision 2026-10-07); ``revision`` is not one of them and is
    recorded only when an observation is authorized. ``resource_uid`` is the
    registry key, which is also the operator's ``target_id`` today. Intake
    does not need it (it registers the uid alone); registering a remediation
    completes the registry row from it the first time.
    """

    integration_id: str
    cluster_uid: str
    namespace: str
    resource_uid: str
    #: ``profile_id`` of the HealthProfile whose recovery definition applies
    #: to this target; a remediation is registered only under that profile.
    #: Not part of the identity written to the registry.
    health_profile_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("integration_id", "cluster_uid", "namespace", "resource_uid"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"TARGET_IDENTITY_{name.upper()}_REQUIRED")


class TargetRegistry(Protocol):
    def resolve(self, target_id: str) -> TargetIdentity | None:
        """The configured identity for an operator's target id, or ``None``
        when the id is unknown (a remediation on it is then refused for
        lack of identity; nothing is guessed)."""
        ...


class MappingTargetRegistry:
    """The deployment's target registry: the ``OPSPILOT_TARGET_IDENTITIES``
    file (``opspilot.schema.load_target_identities``) read once at start."""

    def __init__(self, identities: Mapping[str, Mapping[str, str]]) -> None:
        self._targets = {
            uid: TargetIdentity(
                integration_id=entry["integration_id"],
                cluster_uid=entry["cluster_uid"],
                namespace=entry["namespace"],
                resource_uid=uid,
                health_profile_id=entry.get("health_profile_id"),
            )
            for uid, entry in identities.items()
        }

    @classmethod
    def from_file(cls, path: str) -> MappingTargetRegistry:
        return cls(load_target_identities(path))

    def resolve(self, target_id: str) -> TargetIdentity | None:
        return self._targets.get(target_id)


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
        input: dict[str, Any] | None = None,
        target_id: UUID | None = None,
    ) -> None:
        """Create the incident and its first Run; ``input`` is the Run's
        investigation input snapshot (``InvestigationInput.as_json``), what
        the real driver rebuilds the model context from; ``target_id`` is the
        registered target identity the incident is bound to, so a target
        suspension fences its Runs (an incident without one is fenced by the
        global gate only)."""
        ...

    def register_target(self, resource_uid: str) -> UUID:
        """The registered identity for ``resource_uid``; idempotent."""
        ...

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
        input: dict[str, Any] | None = None,
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

    def sweep_expired_runs_with_generations(
        self, *, incident_id: UUID | None = None, limit: int = 100
    ) -> tuple[tuple[UUID, UUID, int], ...]: ...

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

    def register_remediation(
        self,
        incident_id: UUID,
        *,
        expected_generation: int,
        actor: str,
        revision: str,
        deadline_at: datetime,
        max_samples: int,
        sample_interval_seconds: int,
        sustained_window_seconds: int,
        health_profile_revision: str | None,
        health_profile: str | None,
        session_id: UUID,
        identity: TargetIdentity | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        """Record the human handling and authorize a bounded observation
        session in one transaction (``ObservationStore.register_remediation``);
        ``identity`` completes the registry row the first time (from the
        configured registry, never the operator); returns the new control
        generation."""
        ...

    def observation_sessions(self, incident_id: UUID) -> tuple[Mapping[str, Any], ...]:
        """Every observation session row of the incident, oldest first."""
        ...

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
        # DDL is in opspilot/migrations; this only checks the version (ADR-0007).
        self._store.install()

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
        self._observation_store: ObservationStore | None = None
        # PR #31 adds ``payload`` to DurableStore.control (opspilot_controls.payload
        # + opspilot_inputs). Detect it once so this adapter works on both bases.
        parameters = inspect.signature(store.control).parameters
        self._payload_supported = "payload" in parameters
        # A note on a timed-out Run starts a fresh Run (user decision
        # 2026-09-25) only where the store's control() knows the renewal
        # keywords; an older base gets the positional call it accepts and
        # keeps re-queueing (bot review, PR #47).
        self._renewal_supported = "renew_run_id" in parameters

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
        input: dict[str, Any] | None = None,
        target_id: UUID | None = None,
    ) -> None:
        self._store.accept(
            incident_id,
            run_id,
            intake_key,
            deadline=deadline,
            budget_limit=budget_limit,
            versions=versions,
            input=input,
            target_id=target_id,
        )

    def register_target(self, resource_uid: str) -> UUID:
        return self._store.register_target(resource_uid)

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
        renewal: dict[str, Any] = {}
        if self._renewal_supported:
            renewal = {
                "renew_run_id": renew_run_id,
                "renew_deadline": renew_deadline,
                "renew_input": renew_input,
            }
        # The signature guards above prove which keywords exist at runtime;
        # mypy only sees the current DurableStore signature.
        control: Any = self._store.control
        if payload is None or not self._payload_supported:
            # Base without PR #31: the store has no payload column. The
            # workbench then keeps the note in its ledger and composes it
            # into the next attempt's question (read under the lease).
            result: int = control(
                incident_id, expected_generation, action, actor, **renewal
            )
            return result
        applied: int = control(
            incident_id, expected_generation, action, actor, payload, **renewal
        )
        return applied

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
        input: dict[str, Any] | None = None,
    ) -> int:
        return self._store.new_run(
            incident_id,
            run_id,
            expected_generation=expected_generation,
            deadline=deadline,
            budget_limit=budget_limit,
            versions=versions,
            actor=actor,
            input=input,
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

    def sweep_expired_runs_with_generations(
        self, *, incident_id: UUID | None = None, limit: int = 100
    ) -> tuple[tuple[UUID, UUID, int], ...]:
        return self._store.sweep_expired_runs_with_generations(
            incident_id=incident_id, limit=limit
        )

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

    @property
    def _observation(self) -> ObservationStore:
        # Controller-side observation authorization (M1-02 step 3) on the
        # same DSN and pool configuration as the business store, so the two
        # share one connection pool. Built on first use: adapters over an
        # older base (tests) never touch it.
        if self._observation_store is None:
            self._observation_store = ObservationStore(
                self._store.dsn, pool=self._store.pool_config
            )
        return self._observation_store

    def register_remediation(
        self,
        incident_id: UUID,
        *,
        expected_generation: int,
        actor: str,
        revision: str,
        deadline_at: datetime,
        max_samples: int,
        sample_interval_seconds: int,
        sustained_window_seconds: int,
        health_profile_revision: str | None,
        health_profile: str | None,
        session_id: UUID,
        identity: TargetIdentity | None = None,
        payload: dict[str, Any] | None = None,
    ) -> int:
        return self._observation.register_remediation(
            incident_id,
            expected_generation=expected_generation,
            actor=actor,
            revision=revision,
            deadline_at=deadline_at,
            max_samples=max_samples,
            sample_interval_seconds=sample_interval_seconds,
            sustained_window_seconds=sustained_window_seconds,
            health_profile_revision=health_profile_revision,
            health_profile=health_profile,
            session_id=session_id,
            identity=(
                None
                if identity is None
                else {
                    field: value
                    for field, value in asdict(identity).items()
                    if field != "health_profile_id"
                }
            ),
            payload=payload,
        )

    def observation_sessions(self, incident_id: UUID) -> tuple[Mapping[str, Any], ...]:
        return tuple(self._observation.incident_sessions(incident_id))

    def list_incidents(self, *, limit: int = 50) -> tuple[IncidentSummary, ...]:
        with self._store.transaction(snapshot=True) as conn:
            rows = conn.execute(
                "SELECT i.incident_id,i.intake_key,i.state,i.lifecycle,i.control_generation,i.current_run_id,r.state AS run_state,i.conclusion IS NOT NULL AS concluded,i.created_at FROM opspilot_incidents i LEFT JOIN opspilot_runs r ON r.run_id=i.current_run_id AND r.incident_id=i.incident_id ORDER BY i.created_at DESC, i.incident_id LIMIT %s",
                (limit,),
            ).fetchall()
        return tuple(_summary(row) for row in rows)

    def find_incident(self, incident_id: UUID) -> IncidentSummary | None:
        with self._store.transaction(snapshot=True) as conn:
            row = conn.execute(
                "SELECT i.incident_id,i.intake_key,i.state,i.lifecycle,i.control_generation,i.current_run_id,NULL::text AS run_state,i.conclusion IS NOT NULL AS concluded,i.created_at FROM opspilot_incidents i WHERE i.incident_id=%s",
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
        run_state=row["run_state"],
    )
