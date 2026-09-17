"""Evidence read-back store: exact raw bytes, the projected view, both hashes.

The tool executor registers every observation through the ``EvidenceSink``
protocol; the workbench reads it back so a report citation resolves to the
captured bytes rather than to model prose (PRODUCT-CONSTRAINTS, "Evidence
and context requirements"). Registration is idempotent on ``evidence_id``;
a second registration with different bytes is an identity conflict.
"""

from __future__ import annotations

import hashlib
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
    raw: bytes
    raw_sha256: str
    view: Mapping[str, Any]
    view_sha256: str
    projection_revision: str
    observed_at: datetime
    data_as_of: datetime | None

    @property
    def hashes_verified(self) -> bool:
        """Recompute both digests from what is stored, not from what was said."""
        return (
            hashlib.sha256(self.raw).hexdigest() == self.raw_sha256
            and canonical_hash(self.view) == self.view_sha256
        )


class EvidenceStore(Protocol):
    def register(self, record: EvidenceRecord) -> str: ...

    def get(self, evidence_id: str) -> StoredEvidence | None: ...


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
    """Idempotent re-registration keeps the first committed observation.

    A worker that stopped between ``register()`` and ``commit_tool()`` is
    replayed with the same stable ``evidence_id``; the re-executed query
    returns the same source bytes but a later ``observed_at``, so the view
    digest differs. C3 section 7 says a committed tool result is reused as
    the historical observation with its original time, so identity is the
    raw bytes: same bytes reuse the stored record, different bytes conflict.
    """
    if existing_raw_sha256 != raw_sha256:
        raise PersistenceError("IDENTITY_CONFLICT")


class MemoryEvidenceStore:
    def __init__(self) -> None:
        self._records: dict[str, StoredEvidence] = {}

    def register(self, record: EvidenceRecord) -> str:
        stored = _stored(record)
        existing = self._records.get(stored.evidence_id)
        if existing is not None:
            _require_same_bytes(existing.raw_sha256, stored.raw_sha256)
            return existing.evidence_id
        self._records[stored.evidence_id] = stored
        return stored.evidence_id

    def get(self, evidence_id: str) -> StoredEvidence | None:
        return self._records.get(evidence_id)


class DurableEvidenceStore:
    def __init__(self, store: DurableStore) -> None:
        self._store = store

    def install(self) -> None:
        with self._store.transaction() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS opspilot_evidence (
              evidence_id text PRIMARY KEY, run_id text NOT NULL, subject_id text NOT NULL,
              status text NOT NULL, adopted boolean NOT NULL, raw bytea NOT NULL,
              raw_sha256 text NOT NULL, view jsonb NOT NULL, view_sha256 text NOT NULL,
              projection_revision text NOT NULL, observed_at timestamptz NOT NULL,
              data_as_of timestamptz
            );
            CREATE INDEX IF NOT EXISTS opspilot_evidence_run_id_idx ON opspilot_evidence(run_id);
            """)

    def register(self, record: EvidenceRecord) -> str:
        stored = _stored(record)
        with self._store.transaction() as conn:
            conn.execute(
                "INSERT INTO opspilot_evidence(evidence_id,run_id,subject_id,status,adopted,raw,raw_sha256,view,view_sha256,projection_revision,observed_at,data_as_of) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (evidence_id) DO NOTHING",
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
        return stored.evidence_id

    def get(self, evidence_id: str) -> StoredEvidence | None:
        if not isinstance(evidence_id, str) or not evidence_id:
            return None
        with self._store.transaction(snapshot=True) as conn:
            row = conn.execute(
                "SELECT evidence_id,run_id,subject_id,status,adopted,raw,raw_sha256,view,view_sha256,projection_revision,observed_at,data_as_of FROM opspilot_evidence WHERE evidence_id=%s",
                (evidence_id,),
            ).fetchone()
        if row is None:
            return None
        return StoredEvidence(
            evidence_id=row["evidence_id"],
            run_id=row["run_id"],
            subject_id=row["subject_id"],
            status=row["status"],
            adopted=bool(row["adopted"]),
            raw=bytes(row["raw"]),
            raw_sha256=row["raw_sha256"],
            view=row["view"],
            view_sha256=row["view_sha256"],
            projection_revision=row["projection_revision"],
            observed_at=row["observed_at"],
            data_as_of=row["data_as_of"],
        )
