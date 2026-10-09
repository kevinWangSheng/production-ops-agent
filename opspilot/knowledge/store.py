"""Postmortem versions, review actions and knowledge revisions on PostgreSQL.

M1-03 step 1 (F13; decisions D1-D9 in the M1-03 task record). Storage
primitives for step 2 (draft generation on the worker side) and step 3
(review in the workbench); no generation logic, no page, no retrieval.

Every mutation is one transaction that

1. locks the object row (postmortem, or knowledge entry);
2. replays the recorded result when the idempotency key was already used
   for the same request, or refuses (``IDEMPOTENCY_CONFLICT``) when it was
   used for a different one -- before any generation check, so a retry
   after a lost response replays instead of conflicting;
3. checks the caller's expected generation of that object (D9, independent
   of the incident control generation) and the transition against the
   domain ``POSTMORTEM`` machine;
4. writes the audit row(s) -- actor id, authenticated principal kind,
   action, reason, expected/resulting generation -- the state change, and
   last the request row with the result.

Migration 0008 enforces the same invariants at the database layer (append
only, approved content frozen, every change bound to an audit row of the
same transaction, review actions only for ``basic_auth`` principals); the
checks here exist to give callers a precise error code instead of a raw
trigger exception.

Error codes (``PersistenceError``): ``INVALID_INPUT``, ``NOT_FOUND``,
``PRINCIPAL_NOT_ALLOWED``, ``IDEMPOTENCY_CONFLICT``, ``GENERATION_CONFLICT``,
``ILLEGAL_TRANSITION``, ``OPEN_VERSION_EXISTS``, ``CITATIONS_INVALID``,
``DISPUTED``, ``ENTRY_GENERATION_CONFLICT``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal
from uuid import UUID, uuid4

from opspilot.domain.base import DomainError
from opspilot.domain.knowledge import POSTMORTEM
from opspilot.persistence.base import Connection, PersistenceError, _StoreBase

PrincipalKind = Literal["basic_auth", "worker"]
StaleReason = Literal[
    "control_generation_changed",
    "incident_reopened",
    "observation_changed",
    "run_added",
    "input_added",
    "evidence_changed",
]
Certainty = Literal["deterministic", "supported", "uncertain"]

# Actions a worker principal may take (D8: drafts and generation events);
# everything else is a human review action of a Basic Auth principal (D6).
WORKER_ACTIONS = frozenset({"generate", "submit", "mark_stale", "dispute"})
# Postmortem action -> domain POSTMORTEM trigger.
_TRIGGERS = {
    "submit": "submit_for_review",
    "mark_stale": "mark_stale",
    "approve": "human_approve",
    "reject": "human_reject",
    "return": "human_return_for_revision",
}


@dataclass(frozen=True)
class Actor:
    """Who acts: the authenticated principal, never a free-form claim.

    ``basic_auth`` is a workbench user authenticated by the web layer;
    ``worker`` is the draft generator. The web layer builds this from the
    authenticated principal; nothing here can verify the authentication.
    """

    actor_id: str
    principal_kind: PrincipalKind


@dataclass(frozen=True)
class Watermark:
    """What a draft was assembled from (D1); any later move makes it stale."""

    incident_control_generation: int
    observation_generation: int
    observation_session_id: UUID
    observation_ending_id: UUID
    run_count: int
    last_run_id: UUID | None
    input_watermark: int
    evidence_snapshot_sha256: str


@dataclass(frozen=True)
class ConclusionDraft:
    """One section/claim of a draft (D2, D3).

    ``author="code"`` is assembled deterministically from PostgreSQL and
    has ``certainty="deterministic"``; model narrative is ``supported`` only
    when ``citations_valid``, otherwise ``uncertain``. ``evidence_refs``
    are objects naming ``evidence_id``, ``scope`` and the time window.
    """

    key: str
    section: str
    body: str
    author: Literal["code", "model"]
    certainty: Certainty
    citations_valid: bool
    evidence_refs: Sequence[Mapping[str, Any]] = ()


@dataclass(frozen=True)
class ProposalDraft:
    """A structured knowledge entry the draft proposes (D4).

    ``supersedes_entry_id`` names the existing entry an approval would
    replace with a new revision; ``None`` creates a new entry.
    """

    key: str
    name: str
    tags: Sequence[str]
    content: Mapping[str, Any]
    supersedes_entry_id: UUID | None = None


@dataclass(frozen=True)
class DisputeDraft:
    """A dispute raised with the draft itself (e.g. contradicting evidence)."""

    conclusion_key: str
    reason: str


@dataclass(frozen=True)
class ActionResult:
    """The committed outcome of one request (identical on replay)."""

    action: str
    object_kind: Literal["postmortem", "knowledge_entry"]
    object_id: UUID
    generation: int
    version: int | None = None
    revision: int | None = None
    state: str | None = None
    # approve: proposal_key -> {"entry_id", "revision", "generation", "action"}
    published: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    replayed: bool = False

    def _payload(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("replayed")
        data["object_id"] = str(self.object_id)
        data["published"] = {
            key: {**value, "entry_id": str(value["entry_id"])}
            for key, value in self.published.items()
        }
        return data

    @classmethod
    def _from_payload(cls, data: Mapping[str, Any]) -> ActionResult:
        return cls(
            action=data["action"],
            object_kind=data["object_kind"],
            object_id=UUID(data["object_id"]),
            generation=data["generation"],
            version=data["version"],
            revision=data["revision"],
            state=data["state"],
            published={
                key: {**value, "entry_id": UUID(value["entry_id"])}
                for key, value in data["published"].items()
            },
            replayed=True,
        )


def canonical_json(value: Any) -> str:
    """The exact text stored and hashed (sorted keys, no whitespace)."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PersistenceError("INVALID_INPUT")
    return value


def _require_actor(actor: object, action: str) -> Actor:
    if not isinstance(actor, Actor) or actor.principal_kind not in (
        "basic_auth",
        "worker",
    ):
        raise PersistenceError("INVALID_INPUT")
    _require_text(actor.actor_id)
    if actor.principal_kind != "basic_auth" and action not in WORKER_ACTIONS:
        raise PersistenceError("PRINCIPAL_NOT_ALLOWED")
    return actor


def _require_generation(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PersistenceError("INVALID_INPUT")
    return value


class KnowledgeStore(_StoreBase):
    """Postmortem drafts, review actions and knowledge revisions."""

    # --- postmortem mutations

    def record_draft(
        self,
        incident_id: UUID,
        *,
        expected_generation: int,
        idempotency_key: str,
        actor: Actor,
        content: Mapping[str, Any],
        watermark: Watermark,
        conclusions: Sequence[ConclusionDraft],
        proposals: Sequence[ProposalDraft] = (),
        disputes: Sequence[DisputeDraft] = (),
        revises_version: int | None = None,
    ) -> ActionResult:
        """Write the next draft version of the incident's postmortem.

        ``expected_generation`` 0 means "no postmortem yet". Refused with
        ``OPEN_VERSION_EXISTS`` while another version is draft or under
        review (mark it stale or finish its review first). ``revises_version``
        names the returned version this draft revises. The observation
        session and ending must belong to the incident (D1 gate data; the
        gate decision itself is step 2's).
        """
        actor = _require_actor(actor, "generate")
        _require_text(idempotency_key)
        expected = _require_generation(expected_generation)
        if not isinstance(watermark, Watermark) or not conclusions:
            raise PersistenceError("INVALID_INPUT")
        conclusion_keys = [c.key for c in conclusions]
        if len(set(conclusion_keys)) != len(conclusion_keys) or len(
            {p.key for p in proposals}
        ) != len(proposals):
            raise PersistenceError("INVALID_INPUT")
        superseded = [p.supersedes_entry_id for p in proposals if p.supersedes_entry_id]
        if len(set(superseded)) != len(superseded):
            raise PersistenceError("INVALID_INPUT")
        if any(d.conclusion_key not in conclusion_keys for d in disputes):
            raise PersistenceError("INVALID_INPUT")
        for dispute in disputes:
            _require_text(dispute.reason)
        text = canonical_json(dict(content))
        request = {
            "action": "generate",
            "incident_id": str(incident_id),
            "expected_generation": expected,
            "actor": [actor.actor_id, actor.principal_kind],
            "content_sha256": content_sha256(text),
            "watermark": canonical_json(asdict(watermark)),
            "conclusions": canonical_json([asdict(c) for c in conclusions]),
            "proposals": canonical_json([asdict(p) for p in proposals]),
            "disputes": canonical_json([asdict(d) for d in disputes]),
            "revises_version": revises_version,
        }
        with self.transaction() as conn:
            known = conn.execute(
                "SELECT 1 FROM opspilot_incidents WHERE incident_id=%s", (incident_id,)
            ).fetchone()
            if known is None:
                raise PersistenceError("NOT_FOUND")
            conn.execute(
                "INSERT INTO opspilot_postmortems(postmortem_id, incident_id) "
                "VALUES (%s, %s) ON CONFLICT (incident_id) DO NOTHING",
                (uuid4(), incident_id),
            )
            head = self._require_row(
                conn.execute(
                    "SELECT postmortem_id, generation, latest_version FROM opspilot_postmortems "
                    "WHERE incident_id=%s FOR UPDATE",
                    (incident_id,),
                )
            )
            replay = self._replay(conn, idempotency_key, "generate", request)
            if replay is not None:
                return replay
            self._check_generation(head["generation"], expected)
            self._check_watermark(conn, incident_id, watermark)
            open_row = conn.execute(
                "SELECT version FROM opspilot_postmortem_versions "
                "WHERE postmortem_id=%s AND state IN ('draft','under_review')",
                (head["postmortem_id"],),
            ).fetchone()
            if open_row is not None:
                raise PersistenceError("OPEN_VERSION_EXISTS")
            if revises_version is not None:
                revised = conn.execute(
                    "SELECT state FROM opspilot_postmortem_versions "
                    "WHERE postmortem_id=%s AND version=%s",
                    (head["postmortem_id"], revises_version),
                ).fetchone()
                if revised is None or revised["state"] != "returned":
                    raise PersistenceError("ILLEGAL_TRANSITION")
            postmortem_id = head["postmortem_id"]
            version = head["latest_version"] + 1
            generation = expected + 1
            event_id = self._audit(
                conn,
                idempotency_key,
                "postmortem",
                postmortem_id,
                "generate",
                actor,
                expected,
                version=version,
            )
            conn.execute(
                "UPDATE opspilot_postmortems SET generation=%s, latest_version=%s, "
                "updated_at=clock_timestamp() WHERE postmortem_id=%s",
                (generation, version, postmortem_id),
            )
            conn.execute(
                "INSERT INTO opspilot_postmortem_versions(postmortem_id, version, revises_version, "
                "content, content_sha256, incident_control_generation, observation_generation, "
                "observation_session_id, observation_ending_id, run_count, last_run_id, "
                "input_watermark, evidence_snapshot_sha256, generated_by, generated_by_kind) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    postmortem_id,
                    version,
                    revises_version,
                    text,
                    content_sha256(text),
                    watermark.incident_control_generation,
                    watermark.observation_generation,
                    watermark.observation_session_id,
                    watermark.observation_ending_id,
                    watermark.run_count,
                    watermark.last_run_id,
                    watermark.input_watermark,
                    watermark.evidence_snapshot_sha256,
                    actor.actor_id,
                    actor.principal_kind,
                ),
            )
            for ordinal, conclusion in enumerate(conclusions):
                conn.execute(
                    "INSERT INTO opspilot_postmortem_conclusions(postmortem_id, version, "
                    "conclusion_key, ordinal, section, body, author, certainty, "
                    "citations_valid, evidence_refs) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        postmortem_id,
                        version,
                        conclusion.key,
                        ordinal,
                        conclusion.section,
                        conclusion.body,
                        conclusion.author,
                        conclusion.certainty,
                        conclusion.citations_valid,
                        canonical_json(list(conclusion.evidence_refs)),
                    ),
                )
            for proposal in proposals:
                proposal_text = canonical_json(dict(proposal.content))
                conn.execute(
                    "INSERT INTO opspilot_postmortem_proposals(postmortem_id, version, "
                    "proposal_key, name, tags, content, content_sha256, supersedes_entry_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        postmortem_id,
                        version,
                        proposal.key,
                        proposal.name,
                        list(proposal.tags),
                        proposal_text,
                        content_sha256(proposal_text),
                        proposal.supersedes_entry_id,
                    ),
                )
            for dispute in disputes:
                self._insert_dispute(
                    conn, postmortem_id, version, dispute, actor, event_id
                )
            result = ActionResult(
                action="generate",
                object_kind="postmortem",
                object_id=postmortem_id,
                generation=generation,
                version=version,
                state="draft",
            )
            self._record_request(conn, idempotency_key, "generate", request, result)
            return result

    def submit_for_review(
        self,
        postmortem_id: UUID,
        version: int,
        *,
        expected_generation: int,
        idempotency_key: str,
        actor: Actor,
    ) -> ActionResult:
        """draft -> under_review; refused (``CITATIONS_INVALID``) while any
        conclusion failed its citation check (D2)."""
        return self._transition(
            "submit",
            postmortem_id,
            version,
            expected_generation=expected_generation,
            idempotency_key=idempotency_key,
            actor=actor,
        )

    def mark_stale(
        self,
        postmortem_id: UUID,
        version: int,
        *,
        reason: StaleReason,
        expected_generation: int,
        idempotency_key: str,
        actor: Actor,
    ) -> ActionResult:
        """draft/under_review -> stale with the watermark that moved (D1)."""
        return self._transition(
            "mark_stale",
            postmortem_id,
            version,
            expected_generation=expected_generation,
            idempotency_key=idempotency_key,
            actor=actor,
            reason=reason,
        )

    def approve(
        self,
        postmortem_id: UUID,
        version: int,
        *,
        expected_generation: int,
        idempotency_key: str,
        actor: Actor,
        entry_generations: Mapping[UUID, int] | None = None,
    ) -> ActionResult:
        """under_review -> approved, publishing every proposal of the version.

        A proposal without ``supersedes_entry_id`` creates a knowledge entry
        at revision 1; one naming an entry appends its next revision, which
        supersedes the active one (or follows a revoked one). Each named
        entry's expected generation is required in ``entry_generations``.
        Refused while any conclusion is disputed (``DISPUTED``, D3) or failed
        its citation check.
        """
        return self._transition(
            "approve",
            postmortem_id,
            version,
            expected_generation=expected_generation,
            idempotency_key=idempotency_key,
            actor=actor,
            entry_generations=dict(entry_generations or {}),
        )

    def reject(
        self,
        postmortem_id: UUID,
        version: int,
        *,
        reason: str,
        expected_generation: int,
        idempotency_key: str,
        actor: Actor,
    ) -> ActionResult:
        """under_review -> rejected (terminal for this version)."""
        return self._transition(
            "reject",
            postmortem_id,
            version,
            expected_generation=expected_generation,
            idempotency_key=idempotency_key,
            actor=actor,
            reason=reason,
        )

    def return_for_revision(
        self,
        postmortem_id: UUID,
        version: int,
        *,
        reason: str,
        expected_generation: int,
        idempotency_key: str,
        actor: Actor,
    ) -> ActionResult:
        """under_review -> returned; the version stays read-only and a new
        draft names it as ``revises_version`` (D5)."""
        return self._transition(
            "return",
            postmortem_id,
            version,
            expected_generation=expected_generation,
            idempotency_key=idempotency_key,
            actor=actor,
            reason=reason,
        )

    def raise_dispute(
        self,
        postmortem_id: UUID,
        version: int,
        conclusion_key: str,
        *,
        reason: str,
        expected_generation: int,
        idempotency_key: str,
        actor: Actor,
    ) -> ActionResult:
        """Append a dispute to a conclusion of a draft or a version under
        review. Nothing removes a dispute; the version can no longer be
        approved (D3)."""
        actor = _require_actor(actor, "dispute")
        _require_text(idempotency_key)
        _require_text(reason)
        expected = _require_generation(expected_generation)
        request = {
            "action": "dispute",
            "postmortem_id": str(postmortem_id),
            "version": version,
            "conclusion_key": conclusion_key,
            "reason": reason,
            "expected_generation": expected,
            "actor": [actor.actor_id, actor.principal_kind],
        }
        with self.transaction() as conn:
            head = self._lock_postmortem(conn, postmortem_id)
            replay = self._replay(conn, idempotency_key, "dispute", request)
            if replay is not None:
                return replay
            self._check_generation(head["generation"], expected)
            row = self._version_row(conn, postmortem_id, version)
            if row["state"] not in ("draft", "under_review"):
                raise PersistenceError("ILLEGAL_TRANSITION")
            known = conn.execute(
                "SELECT 1 FROM opspilot_postmortem_conclusions "
                "WHERE postmortem_id=%s AND version=%s AND conclusion_key=%s",
                (postmortem_id, version, conclusion_key),
            ).fetchone()
            if known is None:
                raise PersistenceError("NOT_FOUND")
            event_id = self._audit(
                conn,
                idempotency_key,
                "postmortem",
                postmortem_id,
                "dispute",
                actor,
                expected,
                version=version,
                reason=reason,
            )
            self._bump_postmortem(conn, postmortem_id, expected)
            self._insert_dispute(
                conn,
                postmortem_id,
                version,
                DisputeDraft(conclusion_key=conclusion_key, reason=reason),
                actor,
                event_id,
            )
            result = ActionResult(
                action="dispute",
                object_kind="postmortem",
                object_id=postmortem_id,
                generation=expected + 1,
                version=version,
                state=row["state"],
            )
            self._record_request(conn, idempotency_key, "dispute", request, result)
            return result

    # --- knowledge mutations

    def revoke(
        self,
        entry_id: UUID,
        revision: int,
        *,
        reason: str,
        expected_generation: int,
        idempotency_key: str,
        actor: Actor,
    ) -> ActionResult:
        """Tombstone an active revision with its reason (D5, D7). The
        revision row is untouched; it only stops being retrievable."""
        actor = _require_actor(actor, "revoke")
        _require_text(idempotency_key)
        _require_text(reason)
        expected = _require_generation(expected_generation)
        request = {
            "action": "revoke",
            "entry_id": str(entry_id),
            "revision": revision,
            "reason": reason,
            "expected_generation": expected,
            "actor": [actor.actor_id, actor.principal_kind],
        }
        with self.transaction() as conn:
            head = conn.execute(
                "SELECT generation FROM opspilot_knowledge_entries WHERE entry_id=%s FOR UPDATE",
                (entry_id,),
            ).fetchone()
            if head is None:
                raise PersistenceError("NOT_FOUND")
            replay = self._replay(conn, idempotency_key, "revoke", request)
            if replay is not None:
                return replay
            self._check_generation(head["generation"], expected)
            state = conn.execute(
                "SELECT state FROM opspilot_knowledge_revision_states "
                "WHERE entry_id=%s AND revision=%s",
                (entry_id, revision),
            ).fetchone()
            if state is None:
                raise PersistenceError("NOT_FOUND")
            if state["state"] != "active":
                raise PersistenceError("ILLEGAL_TRANSITION")
            event_id = self._audit(
                conn,
                idempotency_key,
                "knowledge_entry",
                entry_id,
                "revoke",
                actor,
                expected,
                revision=revision,
                reason=reason,
            )
            conn.execute(
                "UPDATE opspilot_knowledge_entries SET generation=%s, updated_at=clock_timestamp() "
                "WHERE entry_id=%s",
                (expected + 1, entry_id),
            )
            conn.execute(
                "INSERT INTO opspilot_knowledge_revocations(entry_id, revision, reason, "
                "revoked_by, revoked_by_kind, event_id) VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    entry_id,
                    revision,
                    reason,
                    actor.actor_id,
                    actor.principal_kind,
                    event_id,
                ),
            )
            result = ActionResult(
                action="revoke",
                object_kind="knowledge_entry",
                object_id=entry_id,
                generation=expected + 1,
                revision=revision,
                state="revoked",
            )
            self._record_request(conn, idempotency_key, "revoke", request, result)
            return result

    # --- reads

    def postmortem_for_incident(self, incident_id: UUID) -> dict[str, Any] | None:
        """The postmortem object with every version, conclusion, dispute and
        proposal, read in one snapshot; ``None`` when none was generated."""
        with self.transaction(snapshot=True) as conn:
            head = conn.execute(
                "SELECT * FROM opspilot_postmortems WHERE incident_id=%s",
                (incident_id,),
            ).fetchone()
            if head is None:
                return None
            return self._postmortem_snapshot(conn, head)

    def postmortem(self, postmortem_id: UUID) -> dict[str, Any]:
        with self.transaction(snapshot=True) as conn:
            head = conn.execute(
                "SELECT * FROM opspilot_postmortems WHERE postmortem_id=%s",
                (postmortem_id,),
            ).fetchone()
            if head is None:
                raise PersistenceError("NOT_FOUND")
            return self._postmortem_snapshot(conn, head)

    def knowledge_entry(self, entry_id: UUID) -> dict[str, Any]:
        """An entry with every revision, its derived state and tombstone."""
        with self.transaction(snapshot=True) as conn:
            head = conn.execute(
                "SELECT * FROM opspilot_knowledge_entries WHERE entry_id=%s",
                (entry_id,),
            ).fetchone()
            if head is None:
                raise PersistenceError("NOT_FOUND")
            revisions = conn.execute(
                "SELECT r.*, s.state, x.reason AS revoked_reason, x.revoked_by, "
                "x.revoked_by_kind, x.revoked_at FROM opspilot_knowledge_revisions r "
                "JOIN opspilot_knowledge_revision_states s USING (entry_id, revision) "
                "LEFT JOIN opspilot_knowledge_revocations x USING (entry_id, revision) "
                "WHERE r.entry_id=%s ORDER BY r.revision",
                (entry_id,),
            ).fetchall()
            return {**head, "revisions": revisions}

    def audit_trail(
        self, object_kind: Literal["postmortem", "knowledge_entry"], object_id: UUID
    ) -> list[dict[str, Any]]:
        with self.transaction(snapshot=True) as conn:
            return conn.execute(
                "SELECT * FROM opspilot_f13_audit WHERE object_kind=%s AND object_id=%s "
                "ORDER BY resulting_generation",
                (object_kind, object_id),
            ).fetchall()

    # --- internals

    def _transition(
        self,
        action: str,
        postmortem_id: UUID,
        version: int,
        *,
        expected_generation: int,
        idempotency_key: str,
        actor: Actor,
        reason: str | None = None,
        entry_generations: dict[UUID, int] | None = None,
    ) -> ActionResult:
        actor = _require_actor(actor, action)
        _require_text(idempotency_key)
        expected = _require_generation(expected_generation)
        if action in ("reject", "return", "mark_stale"):
            _require_text(reason)
        for value in (entry_generations or {}).values():
            _require_generation(value)
        request: dict[str, Any] = {
            "action": action,
            "postmortem_id": str(postmortem_id),
            "version": version,
            "reason": reason,
            "expected_generation": expected,
            "actor": [actor.actor_id, actor.principal_kind],
        }
        if entry_generations is not None:
            request["entry_generations"] = {
                str(k): v for k, v in sorted(entry_generations.items(), key=str)
            }
        with self.transaction() as conn:
            head = self._lock_postmortem(conn, postmortem_id)
            replay = self._replay(conn, idempotency_key, action, request)
            if replay is not None:
                return replay
            self._check_generation(head["generation"], expected)
            row = self._version_row(conn, postmortem_id, version)
            try:
                target = POSTMORTEM.fire(row["state"], _TRIGGERS[action])
            except DomainError as exc:
                raise PersistenceError("ILLEGAL_TRANSITION") from exc
            if action in ("submit", "approve"):
                invalid = conn.execute(
                    "SELECT 1 FROM opspilot_postmortem_conclusions "
                    "WHERE postmortem_id=%s AND version=%s AND NOT citations_valid",
                    (postmortem_id, version),
                ).fetchone()
                if invalid is not None:
                    raise PersistenceError("CITATIONS_INVALID")
            if action == "approve":
                disputed = conn.execute(
                    "SELECT 1 FROM opspilot_postmortem_disputes WHERE postmortem_id=%s AND version=%s",
                    (postmortem_id, version),
                ).fetchone()
                if disputed is not None:
                    raise PersistenceError("DISPUTED")
            self._audit(
                conn,
                idempotency_key,
                "postmortem",
                postmortem_id,
                action,
                actor,
                expected,
                version=version,
                reason=reason,
            )
            self._bump_postmortem(conn, postmortem_id, expected)
            conn.execute(
                "UPDATE opspilot_postmortem_versions SET state=%s, stale_reason=%s "
                "WHERE postmortem_id=%s AND version=%s",
                (
                    target,
                    reason if action == "mark_stale" else None,
                    postmortem_id,
                    version,
                ),
            )
            published: dict[str, dict[str, Any]] = {}
            if action == "approve":
                published = self._publish(
                    conn,
                    postmortem_id,
                    version,
                    idempotency_key,
                    actor,
                    entry_generations or {},
                )
            result = ActionResult(
                action=action,
                object_kind="postmortem",
                object_id=postmortem_id,
                generation=expected + 1,
                version=version,
                state=target,
                published=published,
            )
            self._record_request(conn, idempotency_key, action, request, result)
            return result

    def _publish(
        self,
        conn: Connection,
        postmortem_id: UUID,
        version: int,
        idempotency_key: str,
        actor: Actor,
        entry_generations: dict[UUID, int],
    ) -> dict[str, dict[str, Any]]:
        proposals = conn.execute(
            "SELECT * FROM opspilot_postmortem_proposals WHERE postmortem_id=%s AND version=%s "
            "ORDER BY proposal_key",
            (postmortem_id, version),
        ).fetchall()
        superseded = {
            p["supersedes_entry_id"] for p in proposals if p["supersedes_entry_id"]
        }
        if set(entry_generations) != superseded:
            raise PersistenceError("ENTRY_GENERATION_CONFLICT")
        published: dict[str, dict[str, Any]] = {}
        # entries locked in id order: two approvals naming the same entries
        # take their locks in the same order
        for proposal in sorted(
            proposals, key=lambda p: str(p["supersedes_entry_id"] or "")
        ):
            entry_id = proposal["supersedes_entry_id"]
            if entry_id is None:
                entry_id = uuid4()
                conn.execute(
                    "INSERT INTO opspilot_knowledge_entries(entry_id) VALUES (%s)",
                    (entry_id,),
                )
                expected = 0
            else:
                expected = entry_generations[entry_id]
                head = conn.execute(
                    "SELECT generation FROM opspilot_knowledge_entries WHERE entry_id=%s FOR UPDATE",
                    (entry_id,),
                ).fetchone()
                if head is None or head["generation"] != expected:
                    raise PersistenceError("ENTRY_GENERATION_CONFLICT")
            latest = self._require_row(
                conn.execute(
                    "SELECT latest_revision FROM opspilot_knowledge_entries WHERE entry_id=%s",
                    (entry_id,),
                )
            )["latest_revision"]
            revision = latest + 1
            supersedes = None
            if latest:
                previous = self._require_row(
                    conn.execute(
                        "SELECT state FROM opspilot_knowledge_revision_states "
                        "WHERE entry_id=%s AND revision=%s",
                        (entry_id, latest),
                    )
                )
                if previous["state"] == "active":
                    supersedes = latest
            action = "supersede" if supersedes is not None else "publish"
            event_id = self._audit(
                conn,
                idempotency_key,
                "knowledge_entry",
                entry_id,
                action,
                actor,
                expected,
                revision=revision,
            )
            conn.execute(
                "UPDATE opspilot_knowledge_entries SET generation=%s, latest_revision=%s, "
                "updated_at=clock_timestamp() WHERE entry_id=%s",
                (expected + 1, revision, entry_id),
            )
            conn.execute(
                "INSERT INTO opspilot_knowledge_revisions(entry_id, revision, name, tags, content, "
                "content_sha256, source_postmortem_id, source_version, source_proposal_key, "
                "supersedes_revision, approved_by, approved_by_kind, event_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    entry_id,
                    revision,
                    proposal["name"],
                    proposal["tags"],
                    proposal["content"],
                    proposal["content_sha256"],
                    postmortem_id,
                    version,
                    proposal["proposal_key"],
                    supersedes,
                    actor.actor_id,
                    actor.principal_kind,
                    event_id,
                ),
            )
            published[proposal["proposal_key"]] = {
                "entry_id": entry_id,
                "revision": revision,
                "generation": expected + 1,
                "action": action,
            }
        return published

    def _lock_postmortem(self, conn: Connection, postmortem_id: UUID) -> dict[str, Any]:
        head = conn.execute(
            "SELECT generation, latest_version FROM opspilot_postmortems "
            "WHERE postmortem_id=%s FOR UPDATE",
            (postmortem_id,),
        ).fetchone()
        if head is None:
            raise PersistenceError("NOT_FOUND")
        return head

    @staticmethod
    def _version_row(
        conn: Connection, postmortem_id: UUID, version: int
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT state FROM opspilot_postmortem_versions WHERE postmortem_id=%s AND version=%s",
            (postmortem_id, version),
        ).fetchone()
        if row is None:
            raise PersistenceError("NOT_FOUND")
        return row

    @staticmethod
    def _check_generation(current: int, expected: int) -> None:
        if current != expected:
            raise PersistenceError("GENERATION_CONFLICT")

    @staticmethod
    def _check_watermark(
        conn: Connection, incident_id: UUID, watermark: Watermark
    ) -> None:
        """The observation session and ending named by the watermark belong
        to this incident; the last Run too."""
        row = conn.execute(
            "SELECT 1 FROM opspilot_observation_endings e "
            "JOIN opspilot_observation_sessions s ON s.session_id = e.session_id "
            "WHERE e.ending_id=%s AND e.session_id=%s AND s.incident_id=%s AND e.incident_id=%s",
            (
                watermark.observation_ending_id,
                watermark.observation_session_id,
                incident_id,
                incident_id,
            ),
        ).fetchone()
        if row is None:
            raise PersistenceError("INVALID_INPUT")
        if watermark.last_run_id is not None:
            run = conn.execute(
                "SELECT 1 FROM opspilot_runs WHERE run_id=%s AND incident_id=%s",
                (watermark.last_run_id, incident_id),
            ).fetchone()
            if run is None:
                raise PersistenceError("INVALID_INPUT")

    @staticmethod
    def _bump_postmortem(conn: Connection, postmortem_id: UUID, expected: int) -> None:
        conn.execute(
            "UPDATE opspilot_postmortems SET generation=%s, updated_at=clock_timestamp() "
            "WHERE postmortem_id=%s",
            (expected + 1, postmortem_id),
        )

    @staticmethod
    def _audit(
        conn: Connection,
        idempotency_key: str,
        object_kind: str,
        object_id: UUID,
        action: str,
        actor: Actor,
        expected: int,
        *,
        version: int | None = None,
        revision: int | None = None,
        reason: str | None = None,
    ) -> UUID:
        event_id = uuid4()
        conn.execute(
            "INSERT INTO opspilot_f13_audit(event_id, idempotency_key, object_kind, object_id, "
            "action, version, revision, actor_id, principal_kind, reason, expected_generation, "
            "resulting_generation) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                event_id,
                idempotency_key,
                object_kind,
                object_id,
                action,
                version,
                revision,
                actor.actor_id,
                actor.principal_kind,
                reason,
                expected,
                expected + 1,
            ),
        )
        return event_id

    @staticmethod
    def _insert_dispute(
        conn: Connection,
        postmortem_id: UUID,
        version: int,
        dispute: DisputeDraft,
        actor: Actor,
        event_id: UUID,
    ) -> None:
        conn.execute(
            "INSERT INTO opspilot_postmortem_disputes(dispute_id, postmortem_id, version, "
            "conclusion_key, reason, raised_by, raised_by_kind, event_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                uuid4(),
                postmortem_id,
                version,
                dispute.conclusion_key,
                dispute.reason,
                actor.actor_id,
                actor.principal_kind,
                event_id,
            ),
        )

    @staticmethod
    def _replay(
        conn: Connection, idempotency_key: str, action: str, request: dict[str, Any]
    ) -> ActionResult | None:
        row = conn.execute(
            "SELECT action, request_sha256, result FROM opspilot_f13_requests "
            "WHERE idempotency_key=%s",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["request_sha256"] != content_sha256(
            canonical_json(request)
        ):
            raise PersistenceError("IDEMPOTENCY_CONFLICT")
        return ActionResult._from_payload(row["result"])

    @staticmethod
    def _record_request(
        conn: Connection,
        idempotency_key: str,
        action: str,
        request: dict[str, Any],
        result: ActionResult,
    ) -> None:
        conn.execute(
            "INSERT INTO opspilot_f13_requests(idempotency_key, action, request_sha256, result) "
            "VALUES (%s,%s,%s,%s)",
            (
                idempotency_key,
                action,
                content_sha256(canonical_json(request)),
                canonical_json(result._payload()),
            ),
        )

    @staticmethod
    def _postmortem_snapshot(conn: Connection, head: dict[str, Any]) -> dict[str, Any]:
        postmortem_id = head["postmortem_id"]
        versions = conn.execute(
            "SELECT * FROM opspilot_postmortem_versions WHERE postmortem_id=%s ORDER BY version",
            (postmortem_id,),
        ).fetchall()
        conclusions = conn.execute(
            "SELECT * FROM opspilot_postmortem_conclusions WHERE postmortem_id=%s "
            "ORDER BY version, ordinal",
            (postmortem_id,),
        ).fetchall()
        disputes = conn.execute(
            "SELECT * FROM opspilot_postmortem_disputes WHERE postmortem_id=%s "
            "ORDER BY raised_at, dispute_id",
            (postmortem_id,),
        ).fetchall()
        proposals = conn.execute(
            "SELECT * FROM opspilot_postmortem_proposals WHERE postmortem_id=%s "
            "ORDER BY version, proposal_key",
            (postmortem_id,),
        ).fetchall()
        for version in versions:
            number = version["version"]
            version["conclusions"] = [
                {
                    **c,
                    "dispute_state": "disputed"
                    if any(
                        d["version"] == number
                        and d["conclusion_key"] == c["conclusion_key"]
                        for d in disputes
                    )
                    else "undisputed",
                    "disputes": [
                        d
                        for d in disputes
                        if d["version"] == number
                        and d["conclusion_key"] == c["conclusion_key"]
                    ],
                }
                for c in conclusions
                if c["version"] == number
            ]
            version["proposals"] = [p for p in proposals if p["version"] == number]
        return {**head, "versions": versions}
