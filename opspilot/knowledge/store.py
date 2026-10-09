"""Postmortem versions, review actions and knowledge revisions on PostgreSQL.

M1-03 step 1 (F13; decisions D1-D16 in the M1-03 task record). Storage
primitives for step 2 (draft generation on the worker side) and step 3
(review in the workbench); no generation logic, no page, no retrieval.

Who may do what (D6, D14, D15): the worker generates a draft -- which
enters review in the same transaction when every citation validated -- and
marks a version stale; a Basic Auth principal approves, rejects, returns
for revision, supersedes (by approving a version whose proposal names the
entry) and revokes. Disputes are written with the draft and never removed
(D16). Web and worker share one database role (D8 as revised), so the
split is kept in code: the review primitives are called only from the web
layer (``tests/test_knowledge_store_callers.py``).

Every mutation is one transaction that

1. locks the object row (postmortem, or knowledge entry);
2. replays the recorded result when the idempotency key was already used
   for the same request, or refuses (``IDEMPOTENCY_CONFLICT``) when it was
   used for a different one -- before any generation check, so a retry
   after a lost response replays instead of conflicting;
3. checks the caller's expected generation of that object (D9, independent
   of the incident control generation), the transition against the domain
   ``POSTMORTEM`` machine and, for a draft or an approval, that the
   incident's control/observation generations, Run count, input watermark
   and evidence snapshot still equal the version's watermark (D1);
4. writes the audit row(s) -- actor id, authenticated principal kind,
   action, reason, expected/resulting generation -- the state change, and
   last the request row with the result.

Migration 0008 enforces the same invariants at the database layer (append
only, approved content frozen, every change bound to an audit row of the
same transaction, worker and review actions bound to their principal kind);
the checks here exist to give callers a precise error code instead of a raw
trigger exception, which surfaces as ``INTEGRITY_REFUSED``.

Error codes (``PersistenceError``): ``INVALID_INPUT``, ``NOT_FOUND``,
``PRINCIPAL_NOT_ALLOWED``, ``IDEMPOTENCY_CONFLICT``, ``GENERATION_CONFLICT``,
``ILLEGAL_TRANSITION``, ``OPEN_VERSION_EXISTS``, ``WATERMARK_MOVED``,
``CITATIONS_INVALID``, ``DISPUTED``, ``ENTRY_GENERATION_CONFLICT``,
``INTEGRITY_REFUSED``, plus the transient codes of ``_StoreBase``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, get_args
from uuid import UUID, uuid4

from psycopg import errors

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

# Worker actions (D14, D15); every other action is one of the five review
# actions of a Basic Auth principal (D5, D6). Exclusive both ways.
WORKER_ACTIONS = frozenset({"generate", "submit", "mark_stale"})
REVIEW_ACTIONS = frozenset({"approve", "reject", "return", "revoke"})
_AUTHORS = ("code", "model")
_SHA256_HEX = frozenset("0123456789abcdef")
# Postmortem action -> domain POSTMORTEM trigger (``submit`` happens only
# inside ``record_draft``).
_TRIGGERS = {
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
    """A dispute the generation raises on one of its conclusions (e.g.
    contradicting evidence); written with the draft, never removed (D16)."""

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
    if (actor.principal_kind == "worker") != (action in WORKER_ACTIONS):
        raise PersistenceError("PRINCIPAL_NOT_ALLOWED")
    return actor


def _require_int(value: object, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise PersistenceError("INVALID_INPUT")
    return value


def _require_generation(value: object) -> int:
    return _require_int(value)


def _require_uuid(value: object) -> UUID:
    if not isinstance(value, UUID):
        raise PersistenceError("INVALID_INPUT")
    return value


def _require_sha256(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or set(value) - _SHA256_HEX:
        raise PersistenceError("INVALID_INPUT")
    return value


def _validate_watermark(watermark: object) -> Watermark:
    if not isinstance(watermark, Watermark):
        raise PersistenceError("INVALID_INPUT")
    _require_int(watermark.incident_control_generation)
    _require_int(watermark.observation_generation)
    _require_uuid(watermark.observation_session_id)
    _require_uuid(watermark.observation_ending_id)
    _require_int(watermark.run_count)
    _require_int(watermark.input_watermark)
    if (watermark.run_count == 0) != (watermark.last_run_id is None):
        raise PersistenceError("INVALID_INPUT")
    if watermark.last_run_id is not None:
        _require_uuid(watermark.last_run_id)
    _require_sha256(watermark.evidence_snapshot_sha256)
    return watermark


def _validate_conclusion(conclusion: object) -> ConclusionDraft:
    if not isinstance(conclusion, ConclusionDraft):
        raise PersistenceError("INVALID_INPUT")
    _require_text(conclusion.key)
    _require_text(conclusion.section)
    if not isinstance(conclusion.body, str):
        raise PersistenceError("INVALID_INPUT")
    if conclusion.author not in _AUTHORS or conclusion.certainty not in get_args(
        Certainty
    ):
        raise PersistenceError("INVALID_INPUT")
    if not isinstance(conclusion.citations_valid, bool):
        raise PersistenceError("INVALID_INPUT")
    # code-assembled facts are deterministic; a failed citation check makes
    # a conclusion uncertain (D2)
    if (conclusion.author == "code") != (conclusion.certainty == "deterministic"):
        raise PersistenceError("INVALID_INPUT")
    if not conclusion.citations_valid and conclusion.certainty != "uncertain":
        raise PersistenceError("INVALID_INPUT")
    if not isinstance(conclusion.evidence_refs, (list, tuple)) or not all(
        isinstance(ref, Mapping) for ref in conclusion.evidence_refs
    ):
        raise PersistenceError("INVALID_INPUT")
    return conclusion


def _validate_proposal(proposal: object) -> ProposalDraft:
    if not isinstance(proposal, ProposalDraft):
        raise PersistenceError("INVALID_INPUT")
    _require_text(proposal.key)
    _require_text(proposal.name)
    if not isinstance(proposal.tags, (list, tuple)) or not all(
        isinstance(tag, str) and tag for tag in proposal.tags
    ):
        raise PersistenceError("INVALID_INPUT")
    if not isinstance(proposal.content, Mapping):
        raise PersistenceError("INVALID_INPUT")
    if proposal.supersedes_entry_id is not None:
        _require_uuid(proposal.supersedes_entry_id)
    return proposal


class KnowledgeStore(_StoreBase):
    """Postmortem drafts, review actions and knowledge revisions."""

    @staticmethod
    def _error_code(exc: Exception) -> str:
        """A refused write is a deterministic caller or state error, not a
        storage outage: constraint and guard-trigger refusals map to
        ``INTEGRITY_REFUSED``; a key raced in by another request for a
        different object to ``IDEMPOTENCY_CONFLICT``."""
        if isinstance(exc, errors.UniqueViolation):
            if exc.diag.constraint_name == "opspilot_f13_requests_pkey":
                return "IDEMPOTENCY_CONFLICT"
            return "INTEGRITY_REFUSED"
        # CheckViolation, ForeignKeyViolation, NotNullViolation... derive from
        # IntegrityError (SQLSTATE class 23), not IntegrityConstraintViolation
        if isinstance(exc, (errors.IntegrityError, errors.InsufficientPrivilege)):
            return "INTEGRITY_REFUSED"
        return _StoreBase._error_code(exc)  # type: ignore[arg-type]

    # --- worker side (D1, D14, D15)

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
        """Write the next version of the incident's postmortem (worker only).

        ``expected_generation`` 0 means "no postmortem yet". The watermark
        must equal the incident's current state: control and observation
        generations, Run count, input watermark and
        :meth:`evidence_snapshot_sha256` (``WATERMARK_MOVED``
        otherwise), and name an ended observation session of this incident
        with its ending record (``INVALID_INPUT``). When every conclusion's
        citations validated the version enters review in this transaction
        (``state="under_review"``, two audit rows, generation + 2); otherwise
        it stays a draft that can never be reviewed (D2, D15). Refused with
        ``OPEN_VERSION_EXISTS`` while a version is under review.
        ``revises_version`` names the returned version this one regenerates
        (D14).
        """
        actor = _require_actor(actor, "generate")
        _require_text(idempotency_key)
        _require_uuid(incident_id)
        expected = _require_generation(expected_generation)
        _validate_watermark(watermark)
        if (
            not isinstance(content, Mapping)
            or not isinstance(conclusions, (list, tuple))
            or not isinstance(proposals, (list, tuple))
            or not isinstance(disputes, (list, tuple))
            or not conclusions
        ):
            raise PersistenceError("INVALID_INPUT")
        for conclusion in conclusions:
            _validate_conclusion(conclusion)
        for proposal in proposals:
            _validate_proposal(proposal)
        if revises_version is not None:
            _require_int(revises_version, minimum=1)
        conclusion_keys = [c.key for c in conclusions]
        if len(set(conclusion_keys)) != len(conclusion_keys) or len(
            {p.key for p in proposals}
        ) != len(proposals):
            raise PersistenceError("INVALID_INPUT")
        superseded = [p.supersedes_entry_id for p in proposals if p.supersedes_entry_id]
        if len(set(superseded)) != len(superseded):
            raise PersistenceError("INVALID_INPUT")
        for dispute in disputes:
            if not isinstance(dispute, DisputeDraft):
                raise PersistenceError("INVALID_INPUT")
            _require_text(dispute.reason)
            if dispute.conclusion_key not in conclusion_keys:
                raise PersistenceError("INVALID_INPUT")
        text = canonical_json(dict(content))
        reviewable = all(c.citations_valid for c in conclusions)
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
            self._check_observation(conn, incident_id, watermark)
            if self._watermark_moved(conn, incident_id, asdict(watermark)):
                raise PersistenceError("WATERMARK_MOVED")
            open_row = conn.execute(
                "SELECT version FROM opspilot_postmortem_versions "
                "WHERE postmortem_id=%s AND state='under_review'",
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
                (expected + 1, version, postmortem_id),
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
            generation, state = expected + 1, "draft"
            if reviewable:
                # D15: a draft whose citations all validated enters review
                # automatically, in the transaction that generated it
                self._audit(
                    conn,
                    idempotency_key,
                    "postmortem",
                    postmortem_id,
                    "submit",
                    actor,
                    generation,
                    version=version,
                )
                self._bump_postmortem(conn, postmortem_id, generation)
                conn.execute(
                    "UPDATE opspilot_postmortem_versions SET state=%s "
                    "WHERE postmortem_id=%s AND version=%s",
                    (
                        POSTMORTEM.fire("draft", "submit_for_review"),
                        postmortem_id,
                        version,
                    ),
                )
                generation, state = generation + 1, "under_review"
            result = ActionResult(
                action="generate",
                object_kind="postmortem",
                object_id=postmortem_id,
                generation=generation,
                version=version,
                state=state,
            )
            self._record_request(conn, idempotency_key, "generate", request, result)
            return result

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
        """draft/under_review -> stale with the watermark that moved (D1;
        worker only)."""
        if reason not in get_args(StaleReason):
            raise PersistenceError("INVALID_INPUT")
        return self._transition(
            "mark_stale",
            postmortem_id,
            version,
            expected_generation=expected_generation,
            idempotency_key=idempotency_key,
            actor=actor,
            reason=reason,
        )

    # --- review side (D5, D6: Basic Auth principals only)

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
        Refused while any conclusion is disputed (``DISPUTED``, D3, D16) or
        failed its citation check, and when the incident moved past the
        version's watermark (``WATERMARK_MOVED``, D1: such a draft is stale
        and only a new version can be reviewed).
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
        """under_review -> returned; the version stays read-only and the
        worker regenerates a version naming it as ``revises_version``, with
        the reason as input (D5, D14)."""
        return self._transition(
            "return",
            postmortem_id,
            version,
            expected_generation=expected_generation,
            idempotency_key=idempotency_key,
            actor=actor,
            reason=reason,
        )

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
        _require_uuid(entry_id)
        _require_int(revision, minimum=1)
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

    def active_revision(self, entry_id: UUID) -> dict[str, Any] | None:
        """The knowledge read (D3): the entry's ``active`` revision, or
        ``None`` when it has none (every revision superseded or revoked)."""
        with self.transaction(snapshot=True) as conn:
            return conn.execute(
                "SELECT r.*, s.state FROM opspilot_knowledge_revisions r "
                "JOIN opspilot_knowledge_revision_states s USING (entry_id, revision) "
                "WHERE r.entry_id=%s AND s.state='active'",
                (entry_id,),
            ).fetchone()

    def knowledge_history(self, entry_id: UUID) -> dict[str, Any]:
        """Review/audit history of an entry, not a knowledge read: every
        revision with its derived state and tombstone (D5, D7)."""
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

    def audit_trail(
        self, object_kind: Literal["postmortem", "knowledge_entry"], object_id: UUID
    ) -> list[dict[str, Any]]:
        with self.transaction(snapshot=True) as conn:
            return conn.execute(
                "SELECT * FROM opspilot_f13_audit WHERE object_kind=%s AND object_id=%s "
                "ORDER BY resulting_generation",
                (object_kind, object_id),
            ).fetchall()

    def evidence_snapshot_sha256(self, incident_id: UUID) -> str:
        """The evidence snapshot hash a draft's watermark must carry: what
        ``record_draft`` and ``approve`` recompute and compare (D1)."""
        with self.transaction(snapshot=True) as conn:
            return self._evidence_snapshot(conn, incident_id)

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
        _require_uuid(postmortem_id)
        _require_int(version, minimum=1)
        expected = _require_generation(expected_generation)
        if action in ("reject", "return", "mark_stale"):
            _require_text(reason)
        for entry_id, value in (entry_generations or {}).items():
            _require_uuid(entry_id)
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
            if action == "approve":
                invalid = conn.execute(
                    "SELECT 1 FROM opspilot_postmortem_conclusions "
                    "WHERE postmortem_id=%s AND version=%s AND NOT citations_valid",
                    (postmortem_id, version),
                ).fetchone()
                if invalid is not None:
                    raise PersistenceError("CITATIONS_INVALID")
                disputed = conn.execute(
                    "SELECT 1 FROM opspilot_postmortem_disputes WHERE postmortem_id=%s AND version=%s",
                    (postmortem_id, version),
                ).fetchone()
                if disputed is not None:
                    raise PersistenceError("DISPUTED")
                if self._watermark_moved(conn, head["incident_id"], row):
                    raise PersistenceError("WATERMARK_MOVED")
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
            "SELECT incident_id, generation, latest_version FROM opspilot_postmortems "
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
            "SELECT state, incident_control_generation, observation_generation, run_count, "
            "input_watermark, evidence_snapshot_sha256 FROM opspilot_postmortem_versions "
            "WHERE postmortem_id=%s AND version=%s",
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
    def _check_observation(
        conn: Connection, incident_id: UUID, watermark: Watermark
    ) -> None:
        """D1 gate data: the named observation session belongs to this
        incident, has ended, carries the watermark's observation generation,
        and the ending record is that session's; the last Run is this
        incident's."""
        row = conn.execute(
            "SELECT 1 FROM opspilot_observation_endings e "
            "JOIN opspilot_observation_sessions s ON s.session_id = e.session_id "
            "WHERE e.ending_id=%s AND e.session_id=%s AND s.incident_id=%s "
            "AND e.incident_id=%s AND s.state <> 'authorized' AND s.observation_generation=%s",
            (
                watermark.observation_ending_id,
                watermark.observation_session_id,
                incident_id,
                incident_id,
                watermark.observation_generation,
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
    def _watermark_moved(
        conn: Connection, incident_id: UUID, watermark: Mapping[str, Any]
    ) -> bool:
        """Has the incident moved past ``watermark`` (D1)? Control and
        observation generations, Run count, input watermark and the evidence
        snapshot are compared under a share lock on the incident row, so a
        concurrent control action or Run is ordered after this transaction."""
        current = conn.execute(
            "SELECT i.control_generation, i.observation_generation, "
            "(SELECT count(*) FROM opspilot_runs r WHERE r.incident_id = i.incident_id) AS run_count, "
            "(SELECT COALESCE(max(n.sequence), 0) FROM opspilot_inputs n "
            "WHERE n.incident_id = i.incident_id) AS input_watermark "
            "FROM opspilot_incidents i WHERE i.incident_id=%s FOR SHARE OF i",
            (incident_id,),
        ).fetchone()
        if current is None:
            raise PersistenceError("NOT_FOUND")
        return bool(
            current["control_generation"] != watermark["incident_control_generation"]
            or current["observation_generation"] != watermark["observation_generation"]
            or current["run_count"] != watermark["run_count"]
            or current["input_watermark"] != watermark["input_watermark"]
            or KnowledgeStore._evidence_snapshot(conn, incident_id)
            != watermark["evidence_snapshot_sha256"]
        )

    @staticmethod
    def _evidence_snapshot(conn: Connection, incident_id: UUID) -> str:
        """sha256 of the canonical list ``[evidence_id, raw_sha256,
        view_sha256, adopted]`` of the incident's committed evidence (the
        evidence of its Runs that a tool result has cited), ordered by id."""
        rows = conn.execute(
            "SELECT e.evidence_id, e.raw_sha256, e.view_sha256, e.adopted "
            "FROM opspilot_evidence e WHERE e.committed AND e.run_id IN "
            "(SELECT r.run_id::text FROM opspilot_runs r WHERE r.incident_id=%s) "
            "ORDER BY e.evidence_id",
            (incident_id,),
        ).fetchall()
        return content_sha256(
            canonical_json(
                [
                    [r["evidence_id"], r["raw_sha256"], r["view_sha256"], r["adopted"]]
                    for r in rows
                ]
            )
        )

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
