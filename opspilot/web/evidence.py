"""Evidence read-back store: exact raw bytes, the projected view, both hashes.

The tool executor registers every observation through the ``EvidenceSink``
protocol; the workbench reads it back so a report citation resolves to the
captured bytes rather than to model prose (PRODUCT-CONSTRAINTS, "Evidence
and context requirements"). Registration is idempotent on ``evidence_id``;
a second registration with different bytes is an identity conflict.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from psycopg.types.json import Jsonb

from opspilot.persistence import DurableStore, PersistenceError
from opspilot.tools.outcomes import EvidenceRecord
from opspilot.tools.registry import canonical_hash


@dataclass(frozen=True)
class StoredEvidence:
    evidence_id: str
    run_id: str
    subject_id: str
    status: str
    adopted: bool
    #: ``None`` once the retention cleanup removed the payload (#244); the
    #: hash, view and times stay.
    raw: bytes | None
    raw_sha256: str
    view: Mapping[str, Any]
    view_sha256: str
    projection_revision: str
    observed_at: datetime
    data_as_of: datetime | None
    #: Set once the tool result citing this evidence committed; from then on
    #: the projection is final and a same-bytes replay cannot replace it.
    committed: bool = False
    #: ``present`` / ``cleaned`` (0023); a cleaned row has no ``raw``.
    payload_state: str = "present"

    @property
    def hashes_verified(self) -> bool | None:
        """Recompute both digests from what is stored, not from what was said.

        ``None`` when the payload was cleaned: the raw digest can no longer be
        checked, which is not a mismatch -- unless the kept digest is not one
        (``False``; the table does not constrain its shape)."""
        if self.raw is None:
            if re.fullmatch(r"[0-9a-f]{64}", self.raw_sha256 or "") is None:
                return False
            return None
        return (
            hashlib.sha256(self.raw).hexdigest() == self.raw_sha256
            and canonical_hash(self.view) == self.view_sha256
        )

    @property
    def view_verified(self) -> bool:
        """The stored view still hashes to ``view_sha256``: what a reader of
        the view alone (charts, report citations) needs, cleaned or not."""
        return canonical_hash(self.view) == self.view_sha256


class EvidenceStore(Protocol):
    def register(self, record: EvidenceRecord) -> str: ...

    def commit(self, evidence_id: str, view: Mapping[str, Any]) -> None:
        """Pin the projection to the committed tool result and freeze it."""
        ...

    def get(self, evidence_id: str) -> StoredEvidence | None: ...

    def count_spilled(self, run_id: str) -> tuple[int, int]:
        """``(records, raw_bytes)`` of the Run's kept over-limit results (#308)."""
        ...

    def register_spilled(
        self, record: EvidenceRecord, *, max_records: int, max_bytes: int
    ) -> bool:
        """Count and register one over-limit result atomically per Run (#308)."""
        ...


def _spilled(stored: StoredEvidence) -> bool:
    """An un-adopted over-limit result kept by the executor (#308 PR1).

    Its view is the ``RESULT_TOO_LARGE`` refusal the model received plus the
    evidence identity; no other record carries that reason.
    """
    return (
        not stored.adopted
        and stored.status == "error"
        and stored.view.get("reason") == "RESULT_TOO_LARGE"
    )


def _stored(record: EvidenceRecord) -> StoredEvidence:
    if not isinstance(record, EvidenceRecord):
        raise ValueError("INVALID_INPUT")
    if hashlib.sha256(record.raw).hexdigest() != record.raw_sha256:
        raise PersistenceError("IDENTITY_CONFLICT")
    return StoredEvidence(
        evidence_id=record.evidence_id,
        run_id=record.operation.run_id,
        subject_id=record.operation.subject_id,
        status=record.status,
        adopted=record.adopted,
        raw=bytes(record.raw),
        raw_sha256=record.raw_sha256,
        view=dict(record.view),
        view_sha256=record.view_sha256,
        projection_revision=record.projection_revision,
        observed_at=record.observed_at,
        data_as_of=record.data_as_of,
    )


def _require_same_bytes(existing_raw_sha256: str, raw_sha256: str) -> None:
    """Re-registration is allowed only for the same source bytes.

    A worker that stopped between ``register()`` and ``commit_tool()`` is
    replayed with the same stable ``evidence_id``; the re-executed query
    returns the same source bytes but a later ``observed_at``. While no tool
    result has committed, the replayed observation is what the loop will
    consume, so same bytes replace the projection (view, time, status).
    Once ``commit()`` pinned the projection to the committed tool result,
    a later same-bytes replay (an expired worker) leaves it untouched.
    Different bytes always conflict.
    """
    if existing_raw_sha256 != raw_sha256:
        raise PersistenceError("IDENTITY_CONFLICT")


def _pinned(existing: StoredEvidence, view: Mapping[str, Any]) -> StoredEvidence:
    """The committed tool result's view becomes the stored projection."""
    pinned = dict(view)
    observed = pinned.get("observed_at")
    data_as_of = pinned.get("data_as_of")
    return StoredEvidence(
        evidence_id=existing.evidence_id,
        run_id=existing.run_id,
        subject_id=existing.subject_id,
        status=str(pinned.get("status", existing.status)),
        adopted=bool(pinned.get("adopted", existing.adopted)),
        raw=existing.raw,
        raw_sha256=existing.raw_sha256,
        payload_state=existing.payload_state,
        view=pinned,
        view_sha256=canonical_hash(pinned),
        projection_revision=str(
            pinned.get("projection_revision", existing.projection_revision)
        ),
        observed_at=(
            datetime.fromisoformat(observed)
            if isinstance(observed, str)
            else existing.observed_at
        ),
        data_as_of=(
            datetime.fromisoformat(data_as_of)
            if isinstance(data_as_of, str)
            else existing.data_as_of
        ),
        committed=True,
    )


class MemoryEvidenceStore:
    def __init__(self) -> None:
        self._records: dict[str, StoredEvidence] = {}
        self._lock = threading.RLock()

    def register(self, record: EvidenceRecord) -> str:
        stored = _stored(record)
        with self._lock:
            existing = self._records.get(stored.evidence_id)
            if existing is not None:
                _require_same_bytes(existing.raw_sha256, stored.raw_sha256)
                if existing.committed:
                    return existing.evidence_id
            self._records[stored.evidence_id] = stored
        return stored.evidence_id

    def register_spilled(
        self, record: EvidenceRecord, *, max_records: int, max_bytes: int
    ) -> bool:
        with self._lock:
            spilled = [
                stored
                for stored in self._records.values()
                if stored.run_id == record.operation.run_id
                and stored.evidence_id != record.evidence_id
                and _spilled(stored)
            ]
            size = sum(len(stored.raw or b"") for stored in spilled)
            if len(spilled) >= max_records or size + len(record.raw) > max_bytes:
                return False
            return self.register(record) == record.evidence_id

    def commit(self, evidence_id: str, view: Mapping[str, Any]) -> None:
        existing = self._records.get(evidence_id)
        if existing is None:
            raise PersistenceError("UNKNOWN_IDENTITY")
        if existing.committed:
            return
        self._records[evidence_id] = _pinned(existing, view)

    def get(self, evidence_id: str) -> StoredEvidence | None:
        return self._records.get(evidence_id)

    def count_spilled(self, run_id: str) -> tuple[int, int]:
        spilled = [
            stored
            for stored in self._records.values()
            if stored.run_id == run_id and _spilled(stored)
        ]
        return len(spilled), sum(len(stored.raw or b"") for stored in spilled)


class DurableEvidenceStore:
    def __init__(self, store: DurableStore) -> None:
        self._store = store

    def install(self) -> None:
        # DDL is in opspilot/migrations; this only checks the version (ADR-0007).
        self._store.install()

    def register(self, record: EvidenceRecord) -> str:
        stored = _stored(record)
        with self._store.transaction() as conn:
            _insert(conn, stored)
        return stored.evidence_id

    def register_spilled(
        self, record: EvidenceRecord, *, max_records: int, max_bytes: int
    ) -> bool:
        """Admit one over-limit result under the per-Run cap, or store nothing.

        Count and insert share one transaction serialized per Run by a
        transaction-scoped advisory lock, so parallel tool calls (#309) and a
        superseded worker still finishing after a lease takeover cannot both
        pass the count (contract R13). The key is namespaced so it never meets
        the subject-event or alert locks taken on raw ids.
        """
        stored = _stored(record)
        with self._store.transaction() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("opspilot_evidence:spilled:" + stored.run_id,),
            )
            row = conn.execute(
                # Same predicate as ``_spilled``; a replay of this record does
                # not count against itself.
                "SELECT count(*) AS records,COALESCE(sum(octet_length(raw)),0) AS raw_bytes FROM opspilot_evidence WHERE run_id=%s AND evidence_id<>%s AND NOT adopted AND status='error' AND view->>'reason'='RESULT_TOO_LARGE'",
                (stored.run_id, stored.evidence_id),
            ).fetchone()
            if row is None:
                raise PersistenceError("INCONSISTENT_STATE")
            if (
                int(row["records"]) >= max_records
                or int(row["raw_bytes"]) + len(record.raw) > max_bytes
            ):
                return False
            _insert(conn, stored)
        return True

    def commit(self, evidence_id: str, view: Mapping[str, Any]) -> None:
        with self._store.transaction() as conn:
            row = conn.execute(
                "SELECT evidence_id,run_id,subject_id,status,adopted,raw,raw_sha256,view,view_sha256,projection_revision,observed_at,data_as_of,committed,payload_state FROM opspilot_evidence WHERE evidence_id=%s FOR UPDATE",
                (evidence_id,),
            ).fetchone()
            if row is None:
                raise PersistenceError("UNKNOWN_IDENTITY")
            if row["committed"]:
                return
            pinned = _pinned(_from_row(row), view)
            conn.execute(
                "UPDATE opspilot_evidence SET status=%s,adopted=%s,view=%s,view_sha256=%s,projection_revision=%s,observed_at=%s,data_as_of=%s,committed=true WHERE evidence_id=%s",
                (
                    pinned.status,
                    pinned.adopted,
                    Jsonb(dict(pinned.view)),
                    pinned.view_sha256,
                    pinned.projection_revision,
                    pinned.observed_at,
                    pinned.data_as_of,
                    evidence_id,
                ),
            )
            # M1-03 D18: committed evidence moves the incident's evidence
            # snapshot; a draft assembled before it is stale.
            run = conn.execute(
                "SELECT incident_id FROM opspilot_runs WHERE run_id::text=%s",
                (row["run_id"],),
            ).fetchone()
            if run is not None:
                from opspilot.knowledge.store import mark_stale_in

                mark_stale_in(
                    conn,
                    run["incident_id"],
                    reason="evidence_changed",
                    source="evidence.commit",
                )

    def get(self, evidence_id: str) -> StoredEvidence | None:
        if not isinstance(evidence_id, str) or not evidence_id:
            return None
        with self._store.transaction(snapshot=True) as conn:
            row = conn.execute(
                "SELECT evidence_id,run_id,subject_id,status,adopted,raw,raw_sha256,view,view_sha256,projection_revision,observed_at,data_as_of,committed,payload_state FROM opspilot_evidence WHERE evidence_id=%s",
                (evidence_id,),
            ).fetchone()
        if row is None:
            return None
        return _from_row(row)

    def count_spilled(self, run_id: str) -> tuple[int, int]:
        # Same predicate as ``_spilled``, under the worker's existing SELECT
        # grant. The cap itself is enforced by ``register_spilled`` (R13).
        with self._store.transaction() as conn:
            row = conn.execute(
                "SELECT count(*) AS records,COALESCE(sum(octet_length(raw)),0) AS raw_bytes FROM opspilot_evidence WHERE run_id=%s AND NOT adopted AND status='error' AND view->>'reason'='RESULT_TOO_LARGE'",
                (run_id,),
            ).fetchone()
        if row is None:
            raise PersistenceError("INCONSISTENT_STATE")
        return int(row["records"]), int(row["raw_bytes"])


def _insert(conn: Any, stored: StoredEvidence) -> None:
    conn.execute(
        # Same bytes: the replayed projection replaces the earlier,
        # never-committed one. Different bytes: leave the row and let
        # the check below raise IDENTITY_CONFLICT.
        "INSERT INTO opspilot_evidence(evidence_id,run_id,subject_id,status,adopted,raw,raw_sha256,view,view_sha256,projection_revision,observed_at,data_as_of) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (evidence_id) DO UPDATE SET status=EXCLUDED.status,adopted=EXCLUDED.adopted,view=EXCLUDED.view,view_sha256=EXCLUDED.view_sha256,projection_revision=EXCLUDED.projection_revision,observed_at=EXCLUDED.observed_at,data_as_of=EXCLUDED.data_as_of WHERE opspilot_evidence.raw_sha256=EXCLUDED.raw_sha256 AND NOT opspilot_evidence.committed",
        (
            stored.evidence_id,
            stored.run_id,
            stored.subject_id,
            stored.status,
            stored.adopted,
            stored.raw,
            stored.raw_sha256,
            Jsonb(dict(stored.view)),
            stored.view_sha256,
            stored.projection_revision,
            stored.observed_at,
            stored.data_as_of,
        ),
    )
    row = conn.execute(
        "SELECT raw_sha256,view_sha256 FROM opspilot_evidence WHERE evidence_id=%s",
        (stored.evidence_id,),
    ).fetchone()
    if row is None:
        raise PersistenceError("INCONSISTENT_STATE")
    _require_same_bytes(row["raw_sha256"], stored.raw_sha256)


def _from_row(row: Mapping[str, Any]) -> StoredEvidence:
    return StoredEvidence(
        evidence_id=row["evidence_id"],
        run_id=row["run_id"],
        subject_id=row["subject_id"],
        status=row["status"],
        adopted=bool(row["adopted"]),
        raw=None if row["raw"] is None else bytes(row["raw"]),
        raw_sha256=row["raw_sha256"],
        payload_state=row["payload_state"],
        view=row["view"],
        view_sha256=row["view_sha256"],
        projection_revision=row["projection_revision"],
        observed_at=row["observed_at"],
        data_as_of=row["data_as_of"],
        committed=bool(row["committed"]),
    )
