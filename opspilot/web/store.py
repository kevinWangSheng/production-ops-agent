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
import json
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from psycopg.types.json import Jsonb

from opspilot.acceptance_alert import AlertIntakeRecords, alert_intake_records
from opspilot.investigation.store import DurableStepStore, StepCommitter
from opspilot.observation.store import ObservationStore, SelfHealGrant
from opspilot.persistence import DurableStore, Lease, PersistenceError
from opspilot.schema import (
    TARGET_MATCH_FIELD,
    load_target_identities,
    target_match,
)

#: Web UX spec 3.3/3.4: the list tabs, the default first.
LIST_TABS = ("needs_human", "in_progress", "resolved", "closed", "all")
LIST_PER_PAGE = 25
LIST_QUERY_CHARS = 200


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
    #: ``automatic`` or ``human_owned`` (C3 section 10; migration 0005).
    mode: str = "automatic"
    #: Why an alert intake handed this incident to a human without a Run
    #: (``TARGET_UNRESOLVED`` / ``TARGET_AMBIGUOUS``; M1-04 E5, migration
    #: 0010); ``None`` for every incident that has a Run.
    handoff_reason: str | None = None
    #: F2 PR-C: the closed incident this one was opened after (C3).
    related_incident_id: UUID | None = None
    #: R-C6: resolved, and an alert episode was attached after it resolved
    #: (an attachment opens no Run, so only the page shows it).
    refired_after_resolved: bool = False
    #: #245 G6: the ``resource_uid`` the incident row is bound to now (a
    #: rebind changes it); ``None`` for an incident without a bound target.
    #: Observation identity and successor authorization resolve from it,
    #: never from the intake envelope, which only records the first intake.
    target_uid: str | None = None
    #: #245 G3: the surviving incident this one was merged into (it is then
    #: ``closed``); ``None`` for an incident never merged.
    merged_into_incident_id: UUID | None = None

    @property
    def handoff_only(self) -> bool:
        """An alert incident handed to a human at intake: it has no Run."""
        return self.handoff_reason is not None

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
    #: The Deployment / service name that profile observes on this target;
    #: completed on the registry row with the identity and compared with the
    #: profile's ``subject`` at every authorization.
    workload: str | None = None

    def __post_init__(self) -> None:
        for name in ("integration_id", "cluster_uid", "namespace", "resource_uid"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"TARGET_IDENTITY_{name.upper()}_REQUIRED")


def identity_fields(identity: TargetIdentity) -> dict[str, str]:
    """The registry columns an observation authorization completes a bare
    target row from (``ObservationStore._complete_identity``): the identity
    plus ``workload``, without the profile binding."""
    return {
        field: value
        for field, value in asdict(identity).items()
        if field != "health_profile_id"
    }


class TargetRegistry(Protocol):
    def keys(self) -> tuple[str, ...]:
        """All configured operator target keys, in stable order."""
        ...

    def resolve(self, target_id: str) -> TargetIdentity | None:
        """The configured identity for an operator's target id, or ``None``
        when the id is unknown (a remediation on it is then refused for
        lack of identity; nothing is guessed)."""
        ...


class MappingTargetRegistry:
    """The deployment's target registry: the ``OPSPILOT_TARGET_IDENTITIES``
    file (``opspilot.schema.load_target_identities``) read once at start."""

    def __init__(self, identities: Mapping[str, Mapping[str, Any]]) -> None:
        self._targets = {
            uid: TargetIdentity(
                integration_id=entry["integration_id"],
                cluster_uid=entry["cluster_uid"],
                namespace=entry["namespace"],
                resource_uid=uid,
                health_profile_id=entry.get("health_profile_id"),
                workload=entry.get("workload"),
            )
            for uid, entry in identities.items()
        }
        # Alertmanager label matches (M1-04 I4), validated here as well so a
        # registry built from a mapping is held to the file's rule.
        self._matches = {
            uid: dict(target_match(entry[TARGET_MATCH_FIELD])["labels"])
            for uid, entry in identities.items()
            if TARGET_MATCH_FIELD in entry
        }

    @classmethod
    def from_file(cls, path: str) -> MappingTargetRegistry:
        return cls(load_target_identities(path))

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._targets))

    def resolve(self, target_id: str) -> TargetIdentity | None:
        return self._targets.get(target_id)

    def match_alert(self, labels: Mapping[str, str]) -> tuple[TargetIdentity, ...]:
        """Every entry whose ``match.labels`` the alert's labels contain, in
        ``resource_uid`` order; the caller binds only a single hit (I4)."""
        return tuple(
            self._targets[uid]
            for uid in sorted(self._matches)
            if all(labels.get(k) == v for k, v in self._matches[uid].items())
        )


class IncidentStore(Protocol):
    def suspension_state(self) -> dict[str, Any]: ...

    def set_global_suspension(
        self, suspended: bool, *, expected_generation: int, actor: str
    ) -> int: ...

    def set_target_suspension(
        self,
        target_id: UUID | None,
        suspended: bool,
        *,
        expected_generation: int,
        actor: str,
        resource_uid: str | None = None,
    ) -> int: ...

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
        renew_basis: dict[str, Any] | None = None,
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
        payload: dict[str, Any] | None = None,
    ) -> int: ...

    def rebuild(self, incident_id: UUID) -> dict[str, Any]: ...

    def investigation_history(self, incident_id: UUID) -> list[dict[str, Any]]: ...

    def control_times(self, incident_id: UUID) -> list[dict[str, Any]]: ...

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

    def auto_observation_candidates(
        self, *, limit: int = 20
    ) -> tuple[Mapping[str, Any], ...]:
        """Incidents the automatic-observation pass decides on next
        (``ObservationStore.auto_observation_candidates``)."""
        ...

    def authorize_automatic_observation(
        self,
        incident_id: UUID,
        *,
        control_generation: int,
        grant: SelfHealGrant | None,
        refusal: str | None = None,
    ) -> Mapping[str, Any] | None:
        """Decide one automatic-observation candidate in one transaction
        (``ObservationStore.authorize_automatic_observation``); ``None`` when another
        writer holds the incident or the generation is already decided."""
        ...

    def record_auto_observation_failure(
        self, incident_id: UUID, *, control_generation: int, reason: str
    ) -> Mapping[str, Any]:
        """Count one operational failure of an automatic-observation decision
        (``ObservationStore.record_auto_observation_failure``)."""
        ...

    def observation_sessions(self, incident_id: UUID) -> tuple[Mapping[str, Any], ...]:
        """Every observation session row of the incident, oldest first."""
        ...

    def observation_history(self, session_id: UUID) -> Mapping[str, Any]:
        """One session with its profile content, samples (each with its
        readings and raw bundles) and ending records, one snapshot
        (``ObservationStore.session_history``); the page's recovery basis."""
        ...

    def list_incidents(self, *, limit: int = 50) -> tuple[IncidentSummary, ...]: ...

    def record_alert(
        self,
        delivery: Mapping[str, Any],
        *,
        opening: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Commit one Alertmanager delivery, opening its incident when it is
        the identity's first firing one (``DurableStore.record_alert``)."""
        ...

    def alert_records(self, incident_id: UUID) -> AlertIntakeRecords | None:
        """The incident's alert intake records in one snapshot, ``None``
        when no alert opened it."""
        ...

    def attached_alert_deliveries(
        self, incident_id: UUID
    ) -> tuple[Mapping[str, Any], ...]:
        """Every alert delivery filed as ``attached`` to the incident, in
        receipt order, whichever source opened it (#347: a release-anomaly
        incident has no alert identity, so ``alert_records`` is ``None``)."""
        ...

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
    return _like_escape(prefix) + "%"


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
        self._renew_basis_supported = "renew_basis" in parameters
        # #128 adds ``payload`` to DurableStore.new_run (the request key on
        # the audit row); an older base gets the call it accepts.
        new_run = getattr(store, "new_run", None)
        self._new_run_payload_supported = (
            new_run is not None and "payload" in inspect.signature(new_run).parameters
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

    def suspension_state(self) -> dict[str, Any]:
        return self._store.suspension_state()

    def set_global_suspension(
        self, suspended: bool, *, expected_generation: int, actor: str
    ) -> int:
        return self._store.set_global_suspension(
            suspended, expected_generation=expected_generation, actor=actor
        )

    def set_target_suspension(
        self,
        target_id: UUID | None,
        suspended: bool,
        *,
        expected_generation: int,
        actor: str,
        resource_uid: str | None = None,
    ) -> int:
        return self._store.set_target_suspension(
            target_id,
            suspended,
            expected_generation=expected_generation,
            actor=actor,
            resource_uid=resource_uid,
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
        renew_basis: dict[str, Any] | None = None,
    ) -> int:
        renewal: dict[str, Any] = {}
        if self._renewal_supported:
            renewal = {
                "renew_run_id": renew_run_id,
                "renew_deadline": renew_deadline,
                "renew_input": renew_input,
            }
        if self._renew_basis_supported:
            renewal["renew_basis"] = renew_basis
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
        payload: dict[str, Any] | None = None,
    ) -> int:
        extra: dict[str, Any] = {}
        if payload is not None and self._new_run_payload_supported:
            extra["payload"] = payload
        return self._store.new_run(
            incident_id,
            run_id,
            expected_generation=expected_generation,
            deadline=deadline,
            budget_limit=budget_limit,
            versions=versions,
            actor=actor,
            input=input,
            **extra,
        )

    def rebuild(self, incident_id: UUID) -> dict[str, Any]:
        return self._store.rebuild(incident_id)

    def investigation_history(self, incident_id: UUID) -> list[dict[str, Any]]:
        """Read Run history and its database-clock step timestamps."""
        return self._store.investigation_history(incident_id)

    def control_times(self, incident_id: UUID) -> list[dict[str, Any]]:
        """Read committed human-decision timestamps by resulting generation."""
        return self._store.control_times(incident_id)

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
            identity=None if identity is None else identity_fields(identity),
            payload=payload,
        )

    def auto_observation_candidates(
        self, *, limit: int = 20
    ) -> tuple[Mapping[str, Any], ...]:
        return tuple(self._observation.auto_observation_candidates(limit=limit))

    def authorize_automatic_observation(
        self,
        incident_id: UUID,
        *,
        control_generation: int,
        grant: SelfHealGrant | None,
        refusal: str | None = None,
    ) -> Mapping[str, Any] | None:
        return self._observation.authorize_automatic_observation(
            incident_id,
            control_generation=control_generation,
            grant=grant,
            refusal=refusal,
        )

    def record_auto_observation_failure(
        self, incident_id: UUID, *, control_generation: int, reason: str
    ) -> Mapping[str, Any]:
        return self._observation.record_auto_observation_failure(
            incident_id, control_generation=control_generation, reason=reason
        )

    def observation_sessions(self, incident_id: UUID) -> tuple[Mapping[str, Any], ...]:
        return tuple(self._observation.incident_sessions(incident_id))

    def observation_history(self, session_id: UUID) -> Mapping[str, Any]:
        return self._observation.session_history(session_id)

    def list_incidents(self, *, limit: int = 50) -> tuple[IncidentSummary, ...]:
        with self._store.transaction(snapshot=True) as conn:
            rows = conn.execute(
                f"SELECT {_LIST_COLUMNS} FROM {_LIST_FROM} ORDER BY i.created_at DESC, i.incident_id LIMIT %s",
                (limit,),
            ).fetchall()
        return tuple(_summary(row) for row in rows)

    def list_incident_items(self, *, limit: int = 50) -> tuple[IncidentListItem, ...]:
        """The list page's rows (UI redesign 3.2): ``list_incidents`` plus the
        row's subject, raw conclusion, latest recovery session and the
        postmortem version waiting for review, all in one statement so the
        query count does not grow with the row count (list contract 3)."""
        with self._store.transaction(snapshot=True) as conn:
            rows = conn.execute(
                f"SELECT {_LIST_COLUMNS},{_ITEM_COLUMNS} FROM {_LIST_FROM} {_ITEM_JOINS} ORDER BY i.created_at DESC, i.incident_id LIMIT %s",
                (limit,),
            ).fetchall()
        return tuple(_item(row) for row in rows)

    def list_page(
        self,
        *,
        tab: str = "needs_human",
        service: str | None = None,
        q: str | None = None,
        page: int = 1,
        per_page: int = LIST_PER_PAGE,
    ) -> IncidentPage:
        """One page of the incident list (web UX spec 3.7): the Service
        values, the five tab counts over the whole database under the
        filters, and the page's rows, in three statements of one snapshot
        whatever the row count. A ``service`` that is not one of the values
        is ignored and a ``page`` past the last shows the last one; an
        unknown ``tab`` reads as the default (3.4)."""
        tab = tab if tab in LIST_TABS else LIST_TABS[0]
        page = max(1, page)
        with self._store.transaction(snapshot=True) as conn:
            services = tuple(
                row["service"]
                for row in conn.execute(
                    f"{_PAGE_BASE} SELECT DISTINCT service FROM base WHERE service IS NOT NULL ORDER BY 1 LIMIT {_SERVICE_LIMIT}"
                ).fetchall()
            )
            service = service if service in services else None
            where, params = _page_filters(service, q)
            row = conn.execute(
                f"{_PAGE_BASE} SELECT count(*) FILTER (WHERE grp='needs_human') AS needs_human,"
                "count(*) FILTER (WHERE grp='in_progress') AS in_progress,"
                "count(*) FILTER (WHERE lifecycle='resolved') AS resolved,"
                "count(*) FILTER (WHERE lifecycle='closed') AS closed,"
                f'count(*) AS "all" FROM base WHERE {where}',
                params,
            ).fetchone()
            if row is None:
                raise PersistenceError("STORAGE_UNAVAILABLE")
            counts = {key: int(row[key]) for key in LIST_TABS}
            pages = max(1, -(-counts[tab] // per_page))
            page = min(page, pages)
            rows = conn.execute(
                f"{_PAGE_BASE} SELECT {_LIST_COLUMNS},{_ITEM_COLUMNS} FROM {_LIST_FROM} {_ITEM_JOINS}"
                " WHERE i.incident_id IN (SELECT incident_id FROM base"
                f" WHERE {_TAB_PREDICATES[tab]} AND {where}"
                " ORDER BY created_at DESC, incident_id LIMIT %s OFFSET %s)"
                " ORDER BY i.created_at DESC, i.incident_id",
                (*params, per_page, (page - 1) * per_page),
            ).fetchall()
        return IncidentPage(
            items=tuple(_item(r) for r in rows),
            counts=counts,
            services=services,
            tab=tab,
            service=service,
            page=page,
            pages=pages,
        )

    def page_groups(self) -> dict[UUID, str]:
        """Every incident's list group as SQL computes it (``base.grp``): the
        consistency guard against ``incident_list.status_of`` (3.7) reads
        it. Unbounded; tests only."""
        with self._store.transaction(snapshot=True) as conn:
            rows = conn.execute(
                f"{_PAGE_BASE} SELECT incident_id,grp FROM base"
            ).fetchall()
        return {row["incident_id"]: row["grp"] for row in rows}

    def incident_item(self, incident_id: UUID) -> IncidentListItem | None:
        """One incident's list-page row (UI redesign 3.3): the postmortem
        page's heading reads the same subject (alert name or question,
        service) the list shows. At most one row."""
        with self._store.transaction(snapshot=True) as conn:
            row = conn.execute(
                f"SELECT {_LIST_COLUMNS},{_ITEM_COLUMNS} FROM {_LIST_FROM} {_ITEM_JOINS} WHERE i.incident_id=%s",
                (incident_id,),
            ).fetchone()
        return None if row is None else _item(row)

    def find_incident(self, incident_id: UUID) -> IncidentSummary | None:
        with self._store.transaction(snapshot=True) as conn:
            row = conn.execute(
                f"SELECT i.incident_id,i.intake_key,i.state,i.lifecycle,i.mode,i.control_generation,i.current_run_id,NULL::text AS run_state,i.conclusion IS NOT NULL AS concluded,i.created_at,COALESCE(a.handoff_reason,{_RELEASE_HANDOFF}) AS handoff_reason,i.related_incident_id,{_REFIRED},t.resource_uid AS target_uid,i.merged_into_incident_id FROM opspilot_incidents i LEFT JOIN opspilot_targets t ON t.target_id=i.target_id LEFT JOIN opspilot_alert_identities a ON a.incident_id=i.incident_id WHERE i.incident_id=%s",
                (incident_id,),
            ).fetchone()
        return None if row is None else _summary(row)

    def record_alert(
        self,
        delivery: Mapping[str, Any],
        *,
        opening: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._store.record_alert(delivery, opening=opening)

    def merged_from(self, incident_id: UUID) -> tuple[UUID, ...]:
        """Incidents merged into ``incident_id`` (#245), oldest first."""
        with self._store.transaction(snapshot=True) as conn:
            rows = conn.execute(
                "SELECT incident_id FROM opspilot_incidents WHERE merged_into_incident_id=%s ORDER BY created_at,incident_id",
                (incident_id,),
            ).fetchall()
        return tuple(row["incident_id"] for row in rows)

    def alert_records(self, incident_id: UUID) -> AlertIntakeRecords | None:
        with self._store.transaction(snapshot=True) as conn:
            records = alert_intake_records(conn, incident_id)
        return None if records.identity is None else records

    def attached_alert_deliveries(
        self, incident_id: UUID
    ) -> tuple[Mapping[str, Any], ...]:
        with self._store.transaction(snapshot=True) as conn:
            rows = conn.execute(
                "SELECT delivery_key,status,outcome,received_at,starts_at,starts_at_raw FROM opspilot_alert_deliveries WHERE incident_id=%s AND outcome='attached' ORDER BY delivery_id",
                (incident_id,),
            ).fetchall()
        return tuple(rows)

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

    def control_log(self, incident_id: UUID, limit: int) -> list[dict[str, Any]]:
        """The latest ``limit`` committed ``opspilot_controls`` rows with their
        database time, oldest first: the incident page's decision log (#572),
        which shows system rows (``auto_authorize_observation``) next to
        human decisions. Bounded read (PRODUCT-CONSTRAINTS: result volume
        is constrained even for read-only operations); callers ask for one
        row more than they show to learn that older rows exist.
        ``control_audit`` keeps its shape."""
        with self._store.transaction(snapshot=True) as conn:
            rows = conn.execute(
                "SELECT action,actor,expected_generation,resulting_generation,created_at FROM opspilot_controls WHERE incident_id=%s ORDER BY created_at DESC, resulting_generation DESC LIMIT %s",
                (incident_id, limit),
            ).fetchall()
        return list(reversed(rows))

    def now(self) -> datetime:
        return self._clock.now()


# R-C6: a resolved incident that took an attached alert episode after its
# latest recovery confirmation (the only way into ``resolved``).
#: F11 PR-4b R7: an incident a release anomaly handed to a human (no
#: resolvable target) is a handoff incident like an alert's (E5), with the
#: reason its consumption link recorded. A scalar subquery, so the listing
#: stays one row per incident.
_RELEASE_HANDOFF = (
    "(SELECT l.handoff_reason FROM opspilot_release_incident_links l"
    " WHERE l.incident_id=i.incident_id AND l.disposition='handoff_created')"
)
_REFIRED = (
    "(i.lifecycle='resolved' AND EXISTS (SELECT 1 FROM opspilot_alert_deliveries d "
    "WHERE d.incident_id=i.incident_id AND d.outcome='attached' AND d.received_at > "
    "COALESCE((SELECT max(e.recorded_at) FROM opspilot_observation_endings e "
    "WHERE e.incident_id=i.incident_id AND e.lifecycle_after='resolved'), "
    "'-infinity'::timestamptz))) AS refired_after_resolved"
)
_LIST_COLUMNS = (
    "i.incident_id,i.intake_key,i.state,i.lifecycle,i.mode,i.control_generation,"
    "i.current_run_id,r.state AS run_state,i.conclusion IS NOT NULL AS concluded,"
    f"i.created_at,COALESCE(a.handoff_reason,{_RELEASE_HANDOFF}) AS handoff_reason,"
    f"i.related_incident_id,{_REFIRED},t.resource_uid AS target_uid,"
    "i.merged_into_incident_id"
)
_LIST_FROM = (
    "opspilot_incidents i LEFT JOIN opspilot_targets t ON t.target_id=i.target_id"
    " LEFT JOIN opspilot_runs r ON r.run_id=i.current_run_id AND r.incident_id=i.incident_id"
    " LEFT JOIN opspilot_alert_identities a ON a.incident_id=i.incident_id"
)
#: UI redesign 3.2 / section 6. The question is the intake envelope the web
#: ledger keeps under the incident's own key (``Workbench.submit`` writes
#: namespace ``intake``, key ``intake:{idempotency_key}``, which is also the
#: incident's ``intake_key``). The alert text is the latest delivery's stored
#: (redacted, bounded) record, parsed in Python because a record cut at its
#: byte bound is not JSON. Recovery is the newest incident-recovery session;
#: the postmortem is the version under review when one is, else the newest.
_ITEM_COLUMNS = (
    "i.conclusion AS conclusion,wl.value #>> '{envelope,request,question}' AS question,"
    "a.namespace AS alert_namespace,a.workload AS alert_workload,ld.alert_json,"
    "o.state AS recovery_state,o.ended_reason AS recovery_ended_reason,"
    "o.adopted_count AS recovery_samples,o.ended_at AS recovery_ended_at,"
    "pm.postmortem_id,pm.version AS postmortem_version,pm.state AS postmortem_state"
)
_ITEM_JOINS = (
    "LEFT JOIN opspilot_web_ledger wl ON wl.namespace='intake' AND wl.key=i.intake_key"
    " LEFT JOIN LATERAL (SELECT d.alert_json FROM opspilot_alert_deliveries d"
    " WHERE d.incident_id=i.incident_id ORDER BY d.delivery_id DESC LIMIT 1) ld ON true"
    " LEFT JOIN LATERAL (SELECT s.state,s.ended_reason,s.adopted_count,"
    "(SELECT max(e.recorded_at) FROM opspilot_observation_endings e"
    " WHERE e.session_id=s.session_id) AS ended_at"
    " FROM opspilot_observation_sessions s WHERE s.incident_id=i.incident_id"
    " AND s.purpose='incident_recovery'"
    " ORDER BY s.authorized_at DESC,s.session_id DESC LIMIT 1) o ON true"
    " LEFT JOIN LATERAL (SELECT p.postmortem_id,v.version,v.state"
    " FROM opspilot_postmortems p JOIN opspilot_postmortem_versions v"
    " ON v.postmortem_id=p.postmortem_id WHERE p.incident_id=i.incident_id"
    " ORDER BY (v.state='under_review') DESC,v.version DESC LIMIT 1) pm ON true"
)

_SERVICE_LIMIT = 200
#: The list group ``incident_list.status_of`` gives a row, in SQL, in the
#: same first-match order as ``status.incident_status`` (redesign contract
#: A8). Change both together.
_PAGE_GROUP = (
    "CASE"
    " WHEN i.lifecycle='closed' THEN CASE WHEN pm.state='under_review'"
    " AND pm.postmortem_id IS NOT NULL THEN 'needs_human' ELSE 'closed' END"
    " WHEN i.lifecycle='resolved' THEN 'needs_human'"
    " WHEN i.state IN ('cancelled','paused') OR r.state='paused' THEN 'needs_human'"
    " WHEN i.mode='human_owned' THEN 'needs_human'"
    " WHEN r.state IN ('queued','running') THEN 'in_progress'"
    " WHEN i.conclusion IS NOT NULL AND i.lifecycle='observing_recovery'"
    " THEN 'in_progress'"
    " ELSE 'needs_human' END"
)
#: ``incident_list.service_of`` in SQL: ``namespace/workload`` of the alert,
#: else the bound target's ``resource_uid``.
_PAGE_SERVICE = (
    "COALESCE(NULLIF(a.namespace,'')||'/'||NULLIF(a.workload,''),"
    "NULLIF(t.resource_uid,''))"
)
_PAGE_BASE = (
    "WITH base AS (SELECT i.incident_id,i.created_at,i.lifecycle,"
    + _PAGE_GROUP
    + " AS grp,"
    + _PAGE_SERVICE
    + " AS service,wl.value #>> '{envelope,request,question}' AS question,"
    "ld.alert_json FROM "
    + _LIST_FROM
    + " LEFT JOIN LATERAL (SELECT p.postmortem_id,v.state"
    " FROM opspilot_postmortems p JOIN opspilot_postmortem_versions v"
    " ON v.postmortem_id=p.postmortem_id WHERE p.incident_id=i.incident_id"
    " ORDER BY (v.state='under_review') DESC,v.version DESC LIMIT 1) pm ON true"
    " LEFT JOIN opspilot_web_ledger wl ON wl.namespace='intake' AND wl.key=i.intake_key"
    " LEFT JOIN LATERAL (SELECT d.alert_json FROM opspilot_alert_deliveries d"
    " WHERE d.incident_id=i.incident_id ORDER BY d.delivery_id DESC LIMIT 1) ld ON true)"
)
_TAB_PREDICATES = {
    "needs_human": "grp='needs_human'",
    "in_progress": "grp='in_progress'",
    "resolved": "lifecycle='resolved'",
    "closed": "lifecycle='closed'",
    "all": "TRUE",
}


def _page_filters(service: str | None, q: str | None) -> tuple[str, tuple[Any, ...]]:
    """The list filters as a ``WHERE`` fragment over ``base`` and its
    parameters: the Service value, and ``q`` as a case-insensitive
    substring of the question, the latest delivery's stored alert text or
    the service, or a prefix of the incident id."""
    clauses = ["TRUE"]
    params: list[Any] = []
    if service is not None:
        clauses.append("service=%s")
        params.append(service)
    if q:
        text = _like_escape(q)
        clauses.append(
            "(question ILIKE '%%'||%s||'%%' ESCAPE '\\'"
            " OR alert_json ILIKE '%%'||%s||'%%' ESCAPE '\\'"
            " OR service ILIKE '%%'||%s||'%%' ESCAPE '\\'"
            " OR incident_id::text LIKE %s||'%%' ESCAPE '\\')"
        )
        params.extend([text, text, text, text.lower()])
    return " AND ".join(clauses), tuple(params)


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass(frozen=True)
class IncidentPage:
    """One page of the incident list (web UX spec 3.7). ``counts`` has
    every tab of ``LIST_TABS``; ``tab``, ``service`` and ``page`` are the
    values the page was read with after the fallbacks of 3.4."""

    items: tuple[IncidentListItem, ...]
    counts: Mapping[str, int]
    services: tuple[str, ...]
    tab: str
    service: str | None
    page: int
    pages: int


@dataclass(frozen=True, eq=False)
class IncidentListItem:
    """One list-page row: the summary plus what only the list shows. Every
    extra is ``None`` when the store has no such record."""

    summary: IncidentSummary
    #: The committed conclusion payload (``opspilot_incidents.conclusion``);
    #: the view parses it with the incident page's ``_report_view``.
    conclusion: Mapping[str, Any] | None = None
    question: str | None = None
    alert_name: str | None = None
    #: The latest delivery's ``annotations.summary`` (the incident page's
    #: title reads the same record); ``None`` when absent or blank.
    alert_summary: str | None = None
    alert_namespace: str | None = None
    alert_workload: str | None = None
    recovery_state: str | None = None
    recovery_ended_reason: str | None = None
    recovery_samples: int | None = None
    recovery_ended_at: datetime | None = None
    postmortem_id: UUID | None = None
    postmortem_version: int | None = None
    postmortem_state: str | None = None


def _alert_fields(alert_json: str | None) -> tuple[str | None, str | None]:
    """``labels.alertname`` and ``annotations.summary`` of a stored delivery
    record; a record cut at its byte bound is not JSON and gives neither."""
    if alert_json is None:
        return None, None
    try:
        alert = json.loads(alert_json)
    except ValueError:
        return None, None
    if not isinstance(alert, dict):
        return None, None
    labels = alert.get("labels")
    name = labels.get("alertname") if isinstance(labels, dict) else None
    annotations = alert.get("annotations")
    summary = annotations.get("summary") if isinstance(annotations, dict) else None
    return (
        name if isinstance(name, str) and name else None,
        summary if isinstance(summary, str) and summary.strip() else None,
    )


def _item(row: Mapping[str, Any]) -> IncidentListItem:
    conclusion = row.get("conclusion")
    samples = row.get("recovery_samples")
    version = row.get("postmortem_version")
    question = row.get("question")
    alert_name, alert_summary = _alert_fields(row.get("alert_json"))
    return IncidentListItem(
        summary=_summary(row),
        conclusion=conclusion if isinstance(conclusion, Mapping) else None,
        question=question if isinstance(question, str) else None,
        alert_name=alert_name,
        alert_summary=alert_summary,
        alert_namespace=row.get("alert_namespace"),
        alert_workload=row.get("alert_workload"),
        recovery_state=row.get("recovery_state"),
        recovery_ended_reason=row.get("recovery_ended_reason"),
        recovery_samples=None if samples is None else int(samples),
        recovery_ended_at=row.get("recovery_ended_at"),
        postmortem_id=row.get("postmortem_id"),
        postmortem_version=None if version is None else int(version),
        postmortem_state=row.get("postmortem_state"),
    )


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
        mode=str(row.get("mode", "automatic")),
        handoff_reason=row.get("handoff_reason"),
        related_incident_id=row.get("related_incident_id"),
        refired_after_resolved=bool(row.get("refired_after_resolved")),
        target_uid=row.get("target_uid"),
        merged_into_incident_id=row.get("merged_into_incident_id"),
    )
