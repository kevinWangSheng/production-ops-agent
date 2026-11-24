"""Imported runbooks: submission, review and revocation on PostgreSQL (C2a, #566).

User ruling r11 C2-U1=a (2026-11-19); interface in
``docs/tasks/2026-11-19-tool-expansion-c2.md`` sections 1-4. A second
reviewed knowledge source next to the postmortem knowledge of
``opspilot.knowledge.store``, sharing none of its tables.

* ``submit`` -- a signed-in user submits a runbook for one target: pending.
* ``approve`` -- a signed-in user approves a pending submission: a new entry
  at revision 1, or (when the submission supersedes an entry) the entry's
  next revision, the previous one becoming ``superseded``.
* ``reject`` -- a signed-in user rejects a pending submission with a reason
  (terminal; nothing becomes retrievable).
* ``revoke`` -- a signed-in user revokes the active revision of an entry with
  a reason; the entry then has no retrievable revision. Not reversible:
  to use the runbook again it is submitted again.

Every mutation is one transaction that locks its object, replays the result
recorded under the same idempotency key for the same request (or refuses a
different request, ``IDEMPOTENCY_CONFLICT``), checks the expected
generation, writes the audit row(s), the rows, and last the request row with
the result. Migration 0028 enforces the same invariants in the database;
the checks here give callers a precise code.

Generations: a submission is at 1 while pending and 2 once decided; an
entry starts at 0 and moves by one with every approval and revocation.

Error codes (``PersistenceError``): ``INVALID_INPUT``,
``RUNBOOK_NAME_INVALID``, ``RUNBOOK_DESCRIPTION_INVALID``,
``RUNBOOK_CONTENT_INVALID``, ``RUNBOOK_SOURCE_INVALID``, ``REASON_REQUIRED``,
``NOT_FOUND``, ``IDEMPOTENCY_CONFLICT``, ``GENERATION_CONFLICT``,
``RUNBOOK_NAME_TAKEN``, ``SUPERSEDE_TARGET_INACTIVE``,
``SUPERSEDE_MISMATCH``, ``REVISION_NOT_ACTIVE``, ``INTEGRITY_REFUSED``, plus the transient codes of
``_StoreBase``. Nothing here reads or writes a postmortem table.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal
from uuid import UUID, uuid4

from psycopg import errors

from opspilot.persistence.base import Connection, PersistenceError, _StoreBase

NAME_MAX_CHARS = 100
DESCRIPTION_MAX_CHARS = 300
CONTENT_MAX_BYTES = 32 * 1024
SOURCE_LABEL_MAX_CHARS = 100
SOURCE_REF_MAX_CHARS = 500
RESOURCE_UID_MAX_CHARS = 512
#: The label the HolmesGPT eval fixtures are imported under (R8-14 as
#: revised by r11).
HOLMES_FIXTURE_LABEL = "HolmesGPT 评测夹具"

_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_SEPARATORS = re.compile(r"[\s_]+")
_TARGET_LOCK = "opspilot_runbook_target:"
_SUBMISSION_LOCK = "opspilot_runbook_submission:"

# Explicit column lists: product SQL never reads ``*``
# (``tests/test_sql_column_order_independence.py``).
_SUBMISSION_COLUMNS = (
    "submission_id, resource_uid, name, description, content, content_sha256, "
    "source_label, source_ref, supersedes_entry_id, submitted_by, event_id, "
    "submitted_at"
)
_REVISION_COLUMNS = (
    "entry_id, revision, submission_id, resource_uid, name, description, content, "
    "content_sha256, source_label, source_ref, supersedes_revision, approved_by, "
    "event_id, approved_at"
)
_ENTRY_COLUMNS = (
    "entry_id, resource_uid, generation, latest_revision, created_at, updated_at"
)
_AUDIT_COLUMNS = (
    "event_id, idempotency_key, object_kind, object_id, action, revision, actor_id, "
    "principal_kind, reason, expected_generation, resulting_generation, recorded_at"
)

ObjectKind = Literal["runbook_submission", "runbook_entry"]
Action = Literal["submit", "approve", "reject", "revoke"]


def normalize_name(raw: object) -> str:
    """Upstream ``normalize_skill_name``: lowercase, whitespace and
    underscores to hyphens (``skill_loader.py:125-127``); the result must be
    hyphen-separated ``[a-z0-9]`` words of at most 100 characters."""
    if not isinstance(raw, str):
        raise PersistenceError("RUNBOOK_NAME_INVALID")
    name = _SEPARATORS.sub("-", raw.strip().lower())
    if len(name) > NAME_MAX_CHARS or not _NAME.fullmatch(name):
        raise PersistenceError("RUNBOOK_NAME_INVALID")
    return name


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def _require_text(value: object, code: str = "INVALID_INPUT") -> str:
    if not isinstance(value, str) or not value.strip():
        raise PersistenceError(code)
    return value


def _require_generation(value: object, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise PersistenceError("INVALID_INPUT")
    return value


def _require_uuid(value: object) -> UUID:
    if not isinstance(value, UUID):
        raise PersistenceError("INVALID_INPUT")
    return value


def _require_reason(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PersistenceError("REASON_REQUIRED")
    return value


@dataclass(frozen=True)
class RunbookDraft:
    """What a user submits. ``name`` is normalized by ``submit``."""

    resource_uid: str
    name: str
    description: str
    content: str
    source_label: str
    source_ref: str = ""
    supersedes_entry_id: UUID | None = None


@dataclass(frozen=True)
class RunbookResult:
    """The committed outcome of one request (identical on replay)."""

    action: Action
    object_kind: ObjectKind
    object_id: UUID
    generation: int
    submission_id: UUID | None = None
    entry_id: UUID | None = None
    revision: int | None = None
    entry_generation: int | None = None
    state: str | None = None
    replayed: bool = False

    def payload(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("replayed")
        for key in ("object_id", "submission_id", "entry_id"):
            if data[key] is not None:
                data[key] = str(data[key])
        return data

    @classmethod
    def from_payload(cls, data: Mapping[str, Any]) -> RunbookResult:
        values = dict(data)
        for key in ("object_id", "submission_id", "entry_id"):
            if values.get(key) is not None:
                values[key] = UUID(values[key])
        return cls(**values, replayed=True)


def _validated(draft: object) -> tuple[RunbookDraft, str]:
    if not isinstance(draft, RunbookDraft):
        raise PersistenceError("INVALID_INPUT")
    resource_uid = _require_text(draft.resource_uid)
    if len(resource_uid) > RESOURCE_UID_MAX_CHARS:
        raise PersistenceError("INVALID_INPUT")
    name = normalize_name(draft.name)
    description = draft.description
    if (
        not isinstance(description, str)
        or not description.strip()
        or len(description) > DESCRIPTION_MAX_CHARS
        or "\x00" in description
    ):
        raise PersistenceError("RUNBOOK_DESCRIPTION_INVALID")
    content = draft.content
    if (
        not isinstance(content, str)
        or not content.strip()
        or len(content.encode("utf-8")) > CONTENT_MAX_BYTES
        # PostgreSQL text cannot hold NUL: refused here (400), not as a
        # storage failure (C2a review, C2b interface)
        or "\x00" in content
    ):
        raise PersistenceError("RUNBOOK_CONTENT_INVALID")
    label, ref = draft.source_label, draft.source_ref
    if (
        not isinstance(label, str)
        or not label.strip()
        or len(label) > SOURCE_LABEL_MAX_CHARS
        or not isinstance(ref, str)
        or len(ref) > SOURCE_REF_MAX_CHARS
        or "\x00" in label
        or "\x00" in ref
    ):
        raise PersistenceError("RUNBOOK_SOURCE_INVALID")
    if draft.supersedes_entry_id is not None:
        _require_uuid(draft.supersedes_entry_id)
    return draft, name


class RunbookStore(_StoreBase):
    """Imported runbooks: the workbench side (submit, decide, revoke, pages)."""

    @staticmethod
    def _error_code(exc: Exception) -> str:
        if isinstance(exc, errors.UniqueViolation):
            constraint = exc.diag.constraint_name
            if constraint == "opspilot_runbook_requests_pkey":
                return "IDEMPOTENCY_CONFLICT"
            if constraint == "opspilot_runbook_active_name":
                return "RUNBOOK_NAME_TAKEN"
            if constraint in (
                "opspilot_runbook_decisions_pkey",
                "opspilot_runbook_audit_generation_key",
            ):
                return "GENERATION_CONFLICT"
            return "INTEGRITY_REFUSED"
        if isinstance(exc, (errors.IntegrityError, errors.InsufficientPrivilege)):
            return "INTEGRITY_REFUSED"
        return _StoreBase._error_code(exc)  # type: ignore[arg-type]

    # --- mutations

    def submit(
        self, draft: RunbookDraft, *, idempotency_key: str, actor_id: str
    ) -> RunbookResult:
        draft, name = _validated(draft)
        _require_text(idempotency_key)
        _require_text(actor_id)
        supersedes = draft.supersedes_entry_id
        request = {
            "action": "submit",
            "resource_uid": draft.resource_uid,
            "name": name,
            "description": draft.description,
            "content_sha256": content_sha256(draft.content),
            "source_label": draft.source_label,
            "source_ref": draft.source_ref,
            "supersedes_entry_id": None if supersedes is None else str(supersedes),
            "actor_id": actor_id,
        }
        with self.transaction() as conn:
            self._lock(conn, _TARGET_LOCK, draft.resource_uid)
            replay = self._replay(conn, idempotency_key, "submit", request)
            if replay is not None:
                return replay
            if supersedes is None:
                if self._active_named(conn, draft.resource_uid, name) is not None:
                    raise PersistenceError("RUNBOOK_NAME_TAKEN")
            else:
                self._check_supersede(conn, supersedes, draft.resource_uid, name)
            submission_id = uuid4()
            event_id = self._audit(
                conn,
                idempotency_key,
                "runbook_submission",
                submission_id,
                "submit",
                actor_id,
                0,
            )
            conn.execute(
                "INSERT INTO opspilot_runbook_submissions(submission_id, resource_uid, "
                "name, description, content, content_sha256, source_label, source_ref, "
                "supersedes_entry_id, submitted_by, event_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    submission_id,
                    draft.resource_uid,
                    name,
                    draft.description,
                    draft.content,
                    content_sha256(draft.content),
                    draft.source_label,
                    draft.source_ref,
                    supersedes,
                    actor_id,
                    event_id,
                ),
            )
            result = RunbookResult(
                action="submit",
                object_kind="runbook_submission",
                object_id=submission_id,
                generation=1,
                submission_id=submission_id,
                entry_id=supersedes,
                state="pending",
            )
            self._record_request(conn, idempotency_key, "submit", request, result)
            return result

    def approve(
        self,
        submission_id: UUID,
        *,
        expected_generation: int,
        idempotency_key: str,
        actor_id: str,
        entry_generation: int | None = None,
        reason: str | None = None,
    ) -> RunbookResult:
        """Approve a pending submission. Superseding needs the superseded
        entry's ``entry_generation``; a new entry ignores it (must be None)."""
        _require_uuid(submission_id)
        expected = _require_generation(expected_generation)
        _require_text(idempotency_key)
        _require_text(actor_id)
        if entry_generation is not None:
            _require_generation(entry_generation)
        if reason is not None and not isinstance(reason, str):
            raise PersistenceError("INVALID_INPUT")
        reason = reason if reason and reason.strip() else None
        request = {
            "action": "approve",
            "submission_id": str(submission_id),
            "expected_generation": expected,
            "entry_generation": entry_generation,
            "reason": reason,
            "actor_id": actor_id,
        }
        with self.transaction() as conn:
            submission = self._pending(conn, submission_id)
            replay = self._replay(conn, idempotency_key, "approve", request)
            if replay is not None:
                return replay
            self._check_generation(
                self._submission_generation(conn, submission_id), expected
            )
            uid, name = submission["resource_uid"], submission["name"]
            self._lock(conn, _TARGET_LOCK, uid)
            supersedes = submission["supersedes_entry_id"]
            if supersedes is None:
                if entry_generation is not None:
                    raise PersistenceError("INVALID_INPUT")
                if self._active_named(conn, uid, name) is not None:
                    raise PersistenceError("RUNBOOK_NAME_TAKEN")
                entry_id, entry_expected, revision = uuid4(), 0, 1
            else:
                if entry_generation is None:
                    raise PersistenceError("INVALID_INPUT")
                head = self._check_supersede(conn, supersedes, uid, name, lock=True)
                self._check_generation(head["generation"], entry_generation)
                entry_id = supersedes
                entry_expected = head["generation"]
                revision = head["latest_revision"] + 1
            sub_event = self._audit(
                conn,
                idempotency_key,
                "runbook_submission",
                submission_id,
                "approve",
                actor_id,
                expected,
                reason=reason,
            )
            entry_event = self._audit(
                conn,
                idempotency_key,
                "runbook_entry",
                entry_id,
                "approve",
                actor_id,
                entry_expected,
                revision=revision,
                reason=reason,
            )
            if supersedes is None:
                conn.execute(
                    "INSERT INTO opspilot_runbook_entries(entry_id, resource_uid) "
                    "VALUES (%s,%s)",
                    (entry_id, uid),
                )
            conn.execute(
                "UPDATE opspilot_runbook_entries SET generation=%s, latest_revision=%s, "
                "updated_at=clock_timestamp() WHERE entry_id=%s",
                (entry_expected + 1, revision, entry_id),
            )
            conn.execute(
                "INSERT INTO opspilot_runbook_decisions(submission_id, decision, reason, "
                "decided_by, event_id) VALUES (%s,'approved',%s,%s,%s)",
                (submission_id, reason, actor_id, sub_event),
            )
            conn.execute(
                "INSERT INTO opspilot_runbook_revisions(entry_id, revision, submission_id, "
                "resource_uid, name, description, content, content_sha256, source_label, "
                "source_ref, supersedes_revision, approved_by, event_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    entry_id,
                    revision,
                    submission_id,
                    uid,
                    name,
                    submission["description"],
                    submission["content"],
                    submission["content_sha256"],
                    submission["source_label"],
                    submission["source_ref"],
                    None if revision == 1 else revision - 1,
                    actor_id,
                    entry_event,
                ),
            )
            result = RunbookResult(
                action="approve",
                object_kind="runbook_submission",
                object_id=submission_id,
                generation=expected + 1,
                submission_id=submission_id,
                entry_id=entry_id,
                revision=revision,
                entry_generation=entry_expected + 1,
                state="approved",
            )
            self._record_request(conn, idempotency_key, "approve", request, result)
            return result

    def reject(
        self,
        submission_id: UUID,
        *,
        reason: str,
        expected_generation: int,
        idempotency_key: str,
        actor_id: str,
    ) -> RunbookResult:
        _require_uuid(submission_id)
        reason = _require_reason(reason)
        expected = _require_generation(expected_generation)
        _require_text(idempotency_key)
        _require_text(actor_id)
        request = {
            "action": "reject",
            "submission_id": str(submission_id),
            "expected_generation": expected,
            "reason": reason,
            "actor_id": actor_id,
        }
        with self.transaction() as conn:
            self._pending(conn, submission_id)
            replay = self._replay(conn, idempotency_key, "reject", request)
            if replay is not None:
                return replay
            self._check_generation(
                self._submission_generation(conn, submission_id), expected
            )
            event_id = self._audit(
                conn,
                idempotency_key,
                "runbook_submission",
                submission_id,
                "reject",
                actor_id,
                expected,
                reason=reason,
            )
            conn.execute(
                "INSERT INTO opspilot_runbook_decisions(submission_id, decision, reason, "
                "decided_by, event_id) VALUES (%s,'rejected',%s,%s,%s)",
                (submission_id, reason, actor_id, event_id),
            )
            result = RunbookResult(
                action="reject",
                object_kind="runbook_submission",
                object_id=submission_id,
                generation=expected + 1,
                submission_id=submission_id,
                state="rejected",
            )
            self._record_request(conn, idempotency_key, "reject", request, result)
            return result

    def revoke(
        self,
        entry_id: UUID,
        revision: int,
        *,
        reason: str,
        expected_generation: int,
        idempotency_key: str,
        actor_id: str,
    ) -> RunbookResult:
        _require_uuid(entry_id)
        _require_generation(revision, minimum=1)
        reason = _require_reason(reason)
        expected = _require_generation(expected_generation)
        _require_text(idempotency_key)
        _require_text(actor_id)
        request = {
            "action": "revoke",
            "entry_id": str(entry_id),
            "revision": revision,
            "reason": reason,
            "expected_generation": expected,
            "actor_id": actor_id,
        }
        with self.transaction() as conn:
            head = conn.execute(
                "SELECT generation FROM opspilot_runbook_entries WHERE entry_id=%s FOR UPDATE",
                (entry_id,),
            ).fetchone()
            if head is None:
                raise PersistenceError("NOT_FOUND")
            replay = self._replay(conn, idempotency_key, "revoke", request)
            if replay is not None:
                return replay
            self._check_generation(head["generation"], expected)
            state = conn.execute(
                "SELECT state FROM opspilot_runbook_revision_states "
                "WHERE entry_id=%s AND revision=%s",
                (entry_id, revision),
            ).fetchone()
            if state is None:
                raise PersistenceError("NOT_FOUND")
            if state["state"] != "active":
                raise PersistenceError("REVISION_NOT_ACTIVE")
            event_id = self._audit(
                conn,
                idempotency_key,
                "runbook_entry",
                entry_id,
                "revoke",
                actor_id,
                expected,
                revision=revision,
                reason=reason,
            )
            conn.execute(
                "UPDATE opspilot_runbook_entries SET generation=%s, "
                "updated_at=clock_timestamp() WHERE entry_id=%s",
                (expected + 1, entry_id),
            )
            conn.execute(
                "INSERT INTO opspilot_runbook_revocations(entry_id, revision, reason, "
                "revoked_by, event_id) VALUES (%s,%s,%s,%s,%s)",
                (entry_id, revision, reason, actor_id, event_id),
            )
            result = RunbookResult(
                action="revoke",
                object_kind="runbook_entry",
                object_id=entry_id,
                generation=expected + 1,
                entry_id=entry_id,
                revision=revision,
                entry_generation=expected + 1,
                state="revoked",
            )
            self._record_request(conn, idempotency_key, "revoke", request, result)
            return result

    # --- the catalog a Run starts with (C2b, ``opspilot.knowledge.catalog``)

    def active_catalog(self, resource_uid: str) -> dict[str, Any]:
        """Every active runbook of one target, read in its own snapshot with
        the database clock: name, entry, revision, description, approval
        time and age in seconds since approval."""
        _require_text(resource_uid)
        with self.transaction(snapshot=True) as conn:
            clock = conn.execute("SELECT clock_timestamp() AS now").fetchone()
            assert clock is not None
            now = clock["now"]
            rows = conn.execute(
                "SELECT r.name, r.entry_id, r.revision, r.description, r.approved_at, "
                "floor(extract(epoch FROM (%s - r.approved_at)))::bigint AS age_seconds "
                "FROM opspilot_runbook_revisions r "
                "JOIN opspilot_runbook_revision_states s USING (entry_id, revision) "
                "WHERE r.resource_uid=%s AND s.state='active' ORDER BY r.name",
                (now, resource_uid),
            ).fetchall()
        return {"retrieved_at": now, "rows": rows}

    # --- reads (workbench pages; review history, not a knowledge read)

    def overview(self) -> dict[str, Any]:
        """Every entry with its latest revision and state, and every
        submission with its decision, newest first, grouped by target."""
        with self.transaction(snapshot=True) as conn:
            entries = conn.execute(
                "SELECT e.entry_id, e.resource_uid, e.generation, e.latest_revision, "
                "r.name, r.description, r.source_label, r.approved_by, r.approved_at, "
                "s.state FROM opspilot_runbook_entries e "
                "JOIN opspilot_runbook_revisions r ON r.entry_id=e.entry_id "
                "AND r.revision=e.latest_revision "
                "JOIN opspilot_runbook_revision_states s ON s.entry_id=r.entry_id "
                "AND s.revision=r.revision "
                "ORDER BY e.resource_uid, r.name, e.created_at DESC"
            ).fetchall()
            submissions = conn.execute(
                "SELECT s.submission_id, s.resource_uid, s.name, s.description, "
                "s.source_label, s.supersedes_entry_id, s.submitted_by, s.submitted_at, "
                "d.decision, d.decided_by, d.decided_at, d.reason "
                "FROM opspilot_runbook_submissions s "
                "LEFT JOIN opspilot_runbook_decisions d USING (submission_id) "
                "ORDER BY s.submitted_at DESC, s.submission_id"
            ).fetchall()
        for row in submissions:
            row["state"] = _submission_state(row["decision"])
        return {"entries": entries, "submissions": submissions}

    def submission(self, submission_id: UUID) -> dict[str, Any]:
        _require_uuid(submission_id)
        with self.transaction(snapshot=True) as conn:
            row = conn.execute(
                "SELECT "
                + _prefixed(_SUBMISSION_COLUMNS, "s")
                + ", d.decision, d.decided_by, d.decided_at, d.reason, "
                "r.entry_id AS approved_entry_id, r.revision AS approved_revision "
                "FROM opspilot_runbook_submissions s "
                "LEFT JOIN opspilot_runbook_decisions d USING (submission_id) "
                "LEFT JOIN opspilot_runbook_revisions r USING (submission_id) "
                "WHERE s.submission_id=%s",
                (submission_id,),
            ).fetchone()
            if row is None:
                raise PersistenceError("NOT_FOUND")
            row["state"] = _submission_state(row["decision"])
            row["generation"] = 1 if row["decision"] is None else 2
            row["supersedes"] = None
            if row["supersedes_entry_id"] is not None:
                row["supersedes"] = conn.execute(
                    "SELECT e.generation, e.latest_revision, s.state "
                    "FROM opspilot_runbook_entries e "
                    "LEFT JOIN opspilot_runbook_revision_states s "
                    "ON s.entry_id=e.entry_id AND s.revision=e.latest_revision "
                    "WHERE e.entry_id=%s",
                    (row["supersedes_entry_id"],),
                ).fetchone()
            row["audit"] = self._audit_rows(conn, "runbook_submission", submission_id)
            return row

    def entry(self, entry_id: UUID) -> dict[str, Any]:
        _require_uuid(entry_id)
        with self.transaction(snapshot=True) as conn:
            head = conn.execute(
                f"SELECT {_ENTRY_COLUMNS} FROM opspilot_runbook_entries WHERE entry_id=%s",
                (entry_id,),
            ).fetchone()
            if head is None:
                raise PersistenceError("NOT_FOUND")
            revisions = conn.execute(
                "SELECT "
                + _prefixed(_REVISION_COLUMNS, "r")
                + ", s.state, x.reason AS revoked_reason, x.revoked_by, "
                "x.revoked_at FROM opspilot_runbook_revisions r "
                "JOIN opspilot_runbook_revision_states s USING (entry_id, revision) "
                "LEFT JOIN opspilot_runbook_revocations x USING (entry_id, revision) "
                "WHERE r.entry_id=%s ORDER BY r.revision DESC",
                (entry_id,),
            ).fetchall()
            head["revisions"] = revisions
            head["active"] = next(
                (r for r in revisions if r["state"] == "active"), None
            )
            head["audit"] = self._audit_rows(conn, "runbook_entry", entry_id)
            return head

    # --- helpers

    @staticmethod
    def _lock(conn: Connection, prefix: str, key: str) -> None:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (prefix + key,)
        )

    def _pending(self, conn: Connection, submission_id: UUID) -> dict[str, Any]:
        self._lock(conn, _SUBMISSION_LOCK, str(submission_id))
        row = conn.execute(
            f"SELECT {_SUBMISSION_COLUMNS} FROM opspilot_runbook_submissions "
            "WHERE submission_id=%s",
            (submission_id,),
        ).fetchone()
        if row is None:
            raise PersistenceError("NOT_FOUND")
        return row

    @staticmethod
    def _submission_generation(conn: Connection, submission_id: UUID) -> int:
        decided = conn.execute(
            "SELECT 1 FROM opspilot_runbook_decisions WHERE submission_id=%s",
            (submission_id,),
        ).fetchone()
        return 1 if decided is None else 2

    @staticmethod
    def _check_generation(current: int, expected: int) -> None:
        if current != expected:
            raise PersistenceError("GENERATION_CONFLICT")

    @staticmethod
    def _active_named(conn: Connection, resource_uid: str, name: str) -> UUID | None:
        row = conn.execute(
            "SELECT r.entry_id FROM opspilot_runbook_revisions r "
            "JOIN opspilot_runbook_revision_states s USING (entry_id, revision) "
            "WHERE r.resource_uid=%s AND r.name=%s AND s.state='active'",
            (resource_uid, name),
        ).fetchone()
        return None if row is None else row["entry_id"]

    @staticmethod
    def _check_supersede(
        conn: Connection,
        entry_id: UUID,
        resource_uid: str,
        name: str,
        *,
        lock: bool = False,
    ) -> dict[str, Any]:
        head = conn.execute(
            "SELECT e.entry_id, e.resource_uid, e.generation, e.latest_revision, "
            "r.name, s.state FROM opspilot_runbook_entries e "
            "LEFT JOIN opspilot_runbook_revisions r ON r.entry_id=e.entry_id "
            "AND r.revision=e.latest_revision "
            "LEFT JOIN opspilot_runbook_revision_states s ON s.entry_id=e.entry_id "
            "AND s.revision=e.latest_revision WHERE e.entry_id=%s"
            + (" FOR UPDATE OF e" if lock else ""),
            (entry_id,),
        ).fetchone()
        if head is None or head["state"] != "active":
            raise PersistenceError("SUPERSEDE_TARGET_INACTIVE")
        if head["resource_uid"] != resource_uid or head["name"] != name:
            raise PersistenceError("SUPERSEDE_MISMATCH")
        return head

    @staticmethod
    def _audit(
        conn: Connection,
        idempotency_key: str,
        object_kind: ObjectKind,
        object_id: UUID,
        action: Action,
        actor_id: str,
        expected: int,
        *,
        revision: int | None = None,
        reason: str | None = None,
    ) -> UUID:
        event_id = uuid4()
        conn.execute(
            "INSERT INTO opspilot_runbook_audit(event_id, idempotency_key, object_kind, "
            "object_id, action, revision, actor_id, principal_kind, reason, "
            "expected_generation, resulting_generation) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,'basic_auth',%s,%s,%s)",
            (
                event_id,
                idempotency_key,
                object_kind,
                object_id,
                action,
                revision,
                actor_id,
                reason,
                expected,
                expected + 1,
            ),
        )
        return event_id

    @staticmethod
    def _audit_rows(
        conn: Connection, object_kind: ObjectKind, object_id: UUID
    ) -> list[dict[str, Any]]:
        return conn.execute(
            f"SELECT {_AUDIT_COLUMNS} FROM opspilot_runbook_audit "
            "WHERE object_kind=%s AND object_id=%s "
            "ORDER BY resulting_generation",
            (object_kind, object_id),
        ).fetchall()

    @staticmethod
    def _replay(
        conn: Connection, idempotency_key: str, action: str, request: dict[str, Any]
    ) -> RunbookResult | None:
        row = conn.execute(
            "SELECT action, request_sha256, result FROM opspilot_runbook_requests "
            "WHERE idempotency_key=%s",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["request_sha256"] != content_sha256(
            _canonical(request)
        ):
            raise PersistenceError("IDEMPOTENCY_CONFLICT")
        return RunbookResult.from_payload(row["result"])

    @staticmethod
    def _record_request(
        conn: Connection,
        idempotency_key: str,
        action: str,
        request: dict[str, Any],
        result: RunbookResult,
    ) -> None:
        conn.execute(
            "INSERT INTO opspilot_runbook_requests(idempotency_key, action, "
            "request_sha256, result) VALUES (%s,%s,%s,%s)",
            (
                idempotency_key,
                action,
                content_sha256(_canonical(request)),
                _canonical(result.payload()),
            ),
        )


def _submission_state(decision: str | None) -> str:
    return "pending" if decision is None else decision


def _prefixed(columns: str, alias: str) -> str:
    return ", ".join(f"{alias}.{column.strip()}" for column in columns.split(","))
