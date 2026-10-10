"""External F13 seam: committed postmortem and knowledge records ->
``PostmortemOutcome`` (M1-03 step 4, contract r6: D11, D34, R8-R13).

The acceptance entry for postmortems and reviewed knowledge (AGENTS.md:
``IncidentScenario -> IncidentOutcome``), a type of its own beside the
investigation's ``IncidentOutcome`` and F6's ``RecoveryOutcome`` (R13): the
postmortem has its own state machine and is read from its own records, so
nothing here goes into an investigation ``final_state``. Like
``opspilot.acceptance_recovery`` this module has no model, no telemetry, no
transport and no SQL of its own (R12): ``postmortem_records`` calls only the
``KnowledgeStore`` reads the workbench uses, and ``postmortem_outcome``
copies what they returned.

Field sources (``PostmortemOutcome``):

* ``subject_id``: ``scenario.subject_id``, which must be the incident id the
  records were read for. A record of another incident -- or a version,
  conclusion, dispute or proposal filed under another postmortem or
  version, an attempt of another incident, a knowledge revision the
  postmortem did not publish, an audit row of another object -- makes the
  whole outcome ``unknown`` with ``SUBJECT_MISMATCH`` (D11); nothing is
  relabelled and no partial record is projected.
* ``generation_status`` / ``schedule`` / ``recent_attempts``:
  ``KnowledgeStore.incident_postmortem`` copied (R10): the store's status
  rule, not a second one; ``not_generated`` when nothing was written.
  ``recent_attempts`` is the store's newest-first window of at most
  ``ATTEMPTS_SHOWN`` rows, not a complete history (R9).
* ``versions``: every version of the snapshot in version order: state,
  stale reason, watermark, content hash, generator, the code-assembled
  sections and generation/validation records of the stored document, every
  conclusion with certainty, citation validity, evidence references and
  disputes, every proposal, and the review audit of that version. Times are
  copied as stored (R8).
* ``review_actions``: the postmortem's audit trail (``audit_trail
  ("postmortem", id)``), in generation order.
* ``knowledge``: every knowledge revision an approval of this postmortem
  published, with its current state (``knowledge_from_postmortem`` names
  them, ``knowledge_history`` supplies provenance, approver, hash and
  tombstone); a revision another postmortem published on the same entry is
  not this postmortem's and is left out, though it shows in the state of the
  one it superseded;
  ``retrievable`` is whether the knowledge read (``active_revision``)
  returns that very revision (D3), with its ``freshness``.
* ``knowledge_actions``: the full audit trail of each such entry.
* ``model_requests``: empty -- this projection makes no model call.

Consistency (D34): ``postmortem_records`` reads inside a stable-generation
boundary: it reads everything, then re-reads the postmortem's and every
published entry's generation; when one moved it reads again (at most
``attempts`` times, then ``RECORDS_UNSTABLE``). The worker's schedule and
attempts carry no generation and are outside that boundary.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from opspilot.knowledge.contract import CODE_SECTIONS

__all__ = [
    "KnowledgeRevisionOutcome",
    "PostmortemConclusion",
    "PostmortemOutcome",
    "PostmortemRecords",
    "PostmortemVersionOutcome",
    "ReviewRecord",
    "postmortem_outcome",
    "postmortem_records",
]


class KnowledgeReads(Protocol):
    """The ``KnowledgeStore`` reads the projection uses (all read-only)."""

    def incident_postmortem(self, incident_id: UUID) -> dict[str, Any]: ...

    def knowledge_from_postmortem(
        self, postmortem_id: UUID
    ) -> list[dict[str, Any]]: ...

    def knowledge_history(self, entry_id: UUID) -> dict[str, Any]: ...

    def active_revision(self, entry_id: UUID) -> dict[str, Any] | None: ...

    def audit_trail(
        self, object_kind: Literal["postmortem", "knowledge_entry"], object_id: UUID
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class PostmortemRecords:
    """What the product committed for one incident's postmortem, read by
    ``postmortem_records`` inside one stable-generation boundary (D34)."""

    incident_id: UUID
    # ``IncidentPostmortemView``
    view: Mapping[str, Any]
    # ``PublishedRevisionView`` rows of the view's postmortem
    published: tuple[Mapping[str, Any], ...]
    # entry id -> ``knowledge_history``
    entries: Mapping[UUID, Mapping[str, Any]]
    # entry id -> ``active_revision`` (``None``: nothing retrievable)
    active: Mapping[UUID, Mapping[str, Any] | None]
    postmortem_audit: tuple[Mapping[str, Any], ...]
    # entry id -> ``audit_trail("knowledge_entry", entry id)``
    entry_audit: Mapping[UUID, tuple[Mapping[str, Any], ...]]


@dataclass(frozen=True)
class ReviewRecord:
    """One audited action on a postmortem or a knowledge entry."""

    event_id: UUID
    object_kind: str
    object_id: UUID
    action: str
    version: int | None
    revision: int | None
    actor_id: str
    principal_kind: str
    reason: str | None
    expected_generation: int
    resulting_generation: int
    recorded_at: datetime


@dataclass(frozen=True)
class PostmortemConclusion:
    conclusion_key: str
    ordinal: int
    section: str
    body: str
    author: str
    certainty: str
    citations_valid: bool
    evidence_refs: tuple[Mapping[str, Any], ...]
    dispute_state: str
    disputes: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class PostmortemVersionOutcome:
    version: int
    revises_version: int | None
    state: str
    stale_reason: str | None
    content_sha256: str
    # D1: incident control / observation generation, the ended session and
    # its ending record, Run count and last Run, input watermark, evidence
    # snapshot hash
    watermark: Mapping[str, Any]
    generated_by: str
    generated_by_kind: str
    created_at: datetime
    # code-assembled sections of the stored document (D2), as stored:
    # ``incident``, ``timeline``, ``runs``, ``human_actions``, ``recovery``
    sections: Mapping[str, Any]
    evidence_catalog: tuple[Mapping[str, Any], ...]
    generation_record: Mapping[str, Any] | None
    validation: Mapping[str, Any] | None
    conclusions: tuple[PostmortemConclusion, ...]
    proposals: tuple[Mapping[str, Any], ...]
    review_actions: tuple[ReviewRecord, ...]


@dataclass(frozen=True)
class KnowledgeRevisionOutcome:
    entry_id: UUID
    revision: int
    state: str
    # the knowledge read returns this revision (D3)
    retrievable: bool
    name: str
    tags: tuple[str, ...]
    content: str
    content_sha256: str
    source_postmortem_id: UUID
    source_version: int
    source_proposal_key: str
    supersedes_revision: int | None
    approved_by: str
    approved_by_kind: str
    approved_at: datetime
    # the knowledge entry's audit event (``publish`` / ``supersede``) the
    # approval request wrote with this revision; the postmortem's own
    # ``approve`` row is another event of the same request
    approval_event_id: UUID
    revoked_reason: str | None
    revoked_by: str | None
    revoked_by_kind: str | None
    revoked_at: datetime | None
    # the knowledge read's ``freshness`` when retrievable, else ``None``
    freshness: Mapping[str, Any] | None


@dataclass(frozen=True)
class PostmortemOutcome:
    """Only externally inspectable state, content, review actions and
    knowledge of one incident's postmortem."""

    scenario_id: str
    subject_id: str
    # ``GenerationStatus``, or ``unknown`` when the records do not belong to
    # the subject
    generation_status: str
    unknown_reasons: tuple[str, ...]
    postmortem_id: UUID | None
    postmortem_generation: int | None
    latest_version: int | None
    versions: tuple[PostmortemVersionOutcome, ...]
    schedule: Mapping[str, Any] | None
    recent_attempts: tuple[Mapping[str, Any], ...]
    review_actions: tuple[ReviewRecord, ...]
    knowledge: tuple[KnowledgeRevisionOutcome, ...]
    knowledge_actions: tuple[ReviewRecord, ...]
    model_requests: tuple[Any, ...] = field(default=())


# --- reading --------------------------------------------------------------


def _generations(
    knowledge: KnowledgeReads, records: PostmortemRecords
) -> tuple[int | None, dict[UUID, int]]:
    view = knowledge.incident_postmortem(records.incident_id)
    snapshot = view.get("postmortem")
    return (
        None if snapshot is None else int(snapshot["generation"]),
        {
            entry_id: int(knowledge.knowledge_history(entry_id)["generation"])
            for entry_id in records.entries
        },
    )


def _read_once(knowledge: KnowledgeReads, incident_id: UUID) -> PostmortemRecords:
    view = knowledge.incident_postmortem(incident_id)
    snapshot = view.get("postmortem")
    if snapshot is None:
        return PostmortemRecords(incident_id, view, (), {}, {}, (), {})
    postmortem_id = snapshot["postmortem_id"]
    published = tuple(knowledge.knowledge_from_postmortem(postmortem_id))
    entry_ids = list(dict.fromkeys(row["entry_id"] for row in published))
    return PostmortemRecords(
        incident_id=incident_id,
        view=view,
        published=published,
        entries={e: knowledge.knowledge_history(e) for e in entry_ids},
        active={e: knowledge.active_revision(e) for e in entry_ids},
        postmortem_audit=tuple(knowledge.audit_trail("postmortem", postmortem_id)),
        entry_audit={
            e: tuple(knowledge.audit_trail("knowledge_entry", e)) for e in entry_ids
        },
    )


def postmortem_records(
    knowledge: KnowledgeReads, incident_id: UUID, *, attempts: int = 3
) -> PostmortemRecords:
    """Read one incident's postmortem records inside a stable-generation
    boundary (D34): the postmortem's and every published entry's generation
    are the same before and after the read; otherwise read again, at most
    ``attempts`` times (``ValueError("RECORDS_UNSTABLE")``)."""
    for _ in range(attempts):
        records = _read_once(knowledge, incident_id)
        snapshot = records.view.get("postmortem")
        before = (
            None if snapshot is None else int(snapshot["generation"]),
            {e: int(h["generation"]) for e, h in records.entries.items()},
        )
        if _generations(knowledge, records) == before:
            return records
    raise ValueError("RECORDS_UNSTABLE")


# --- projection -------------------------------------------------------------


class _Mismatch(Exception):
    pass


def _same(left: object, right: object) -> None:
    if str(left) != str(right):
        raise _Mismatch


def _review_record(
    row: Mapping[str, Any], kind: str, object_id: object
) -> ReviewRecord:
    _same(row.get("object_kind"), kind)
    _same(row.get("object_id"), object_id)
    return ReviewRecord(
        event_id=row["event_id"],
        object_kind=row["object_kind"],
        object_id=row["object_id"],
        action=row["action"],
        version=row.get("version"),
        revision=row.get("revision"),
        actor_id=row["actor_id"],
        principal_kind=row["principal_kind"],
        reason=row.get("reason"),
        expected_generation=row["expected_generation"],
        resulting_generation=row["resulting_generation"],
        recorded_at=row["recorded_at"],
    )


def _document(content: object) -> dict[str, Any]:
    try:
        document = json.loads(str(content))
    except ValueError:
        return {}
    return document if isinstance(document, dict) else {}


def _conclusion(
    row: Mapping[str, Any], postmortem_id: object, version: int
) -> PostmortemConclusion:
    _same(row.get("postmortem_id"), postmortem_id)
    _same(row.get("version"), version)
    disputes = tuple(dict(d) for d in row.get("disputes") or ())
    for dispute in disputes:
        _same(dispute.get("postmortem_id"), postmortem_id)
        _same(dispute.get("version"), version)
        _same(dispute.get("conclusion_key"), row.get("conclusion_key"))
    return PostmortemConclusion(
        conclusion_key=row["conclusion_key"],
        ordinal=row["ordinal"],
        section=row["section"],
        body=row["body"],
        author=row["author"],
        certainty=row["certainty"],
        citations_valid=row["citations_valid"],
        evidence_refs=tuple(dict(ref) for ref in row.get("evidence_refs") or ()),
        dispute_state=row["dispute_state"],
        disputes=disputes,
    )


_WATERMARK_FIELDS = (
    "incident_control_generation",
    "observation_generation",
    "observation_session_id",
    "observation_ending_id",
    "run_count",
    "last_run_id",
    "input_watermark",
    "evidence_snapshot_sha256",
)


def _version(
    row: Mapping[str, Any],
    postmortem_id: object,
    audit: Sequence[ReviewRecord],
) -> PostmortemVersionOutcome:
    _same(row.get("postmortem_id"), postmortem_id)
    number = int(row["version"])
    proposals = tuple(dict(p) for p in row.get("proposals") or ())
    for proposal in proposals:
        _same(proposal.get("postmortem_id"), postmortem_id)
        _same(proposal.get("version"), number)
    document = _document(row["content"])
    generation = document.get("generation")
    validation = document.get("validation")
    return PostmortemVersionOutcome(
        version=number,
        revises_version=row.get("revises_version"),
        state=row["state"],
        stale_reason=row.get("stale_reason"),
        content_sha256=row["content_sha256"],
        watermark={name: row.get(name) for name in _WATERMARK_FIELDS},
        generated_by=row["generated_by"],
        generated_by_kind=row["generated_by_kind"],
        created_at=row["created_at"],
        sections={name: document.get(name) for name in CODE_SECTIONS},
        evidence_catalog=tuple(
            dict(item) for item in document.get("evidence_catalog") or ()
        ),
        generation_record=generation if isinstance(generation, dict) else None,
        validation=validation if isinstance(validation, dict) else None,
        conclusions=tuple(
            _conclusion(c, postmortem_id, number) for c in row.get("conclusions") or ()
        ),
        proposals=proposals,
        review_actions=tuple(a for a in audit if a.version == number),
    )


def _revisions(
    records: PostmortemRecords, postmortem_id: object
) -> tuple[KnowledgeRevisionOutcome, ...]:
    found: list[KnowledgeRevisionOutcome] = []
    for published in records.published:
        entry_id = published["entry_id"]
        history = records.entries.get(entry_id)
        if history is None:
            raise _Mismatch
        _same(history.get("entry_id"), entry_id)
        if not any(
            str(r.get("entry_id")) == str(entry_id)
            and r.get("revision") == published["revision"]
            and str(r.get("source_postmortem_id")) == str(postmortem_id)
            and r.get("source_version") == published["source_version"]
            for r in history.get("revisions") or ()
        ):
            raise _Mismatch
    ours = {(str(r["entry_id"]), r["revision"]) for r in records.published}
    for entry_id, history in records.entries.items():
        active = records.active.get(entry_id)
        if active is not None:
            _same(active.get("entry_id"), entry_id)
            # the knowledge read is one of the entry's own revisions, active
            # in its history, with the same provenance and content
            if not any(
                r.get("revision") == active.get("revision")
                and r.get("state") == "active"
                and all(
                    str(r.get(f)) == str(active.get(f))
                    for f in (
                        "source_postmortem_id",
                        "source_version",
                        "source_proposal_key",
                        "content_sha256",
                    )
                )
                for r in history.get("revisions") or ()
            ):
                raise _Mismatch
        for row in history.get("revisions") or ():
            _same(row.get("entry_id"), entry_id)
            if (str(entry_id), row["revision"]) not in ours:
                continue
            retrievable = (
                active is not None and active.get("revision") == row["revision"]
            )
            found.append(
                KnowledgeRevisionOutcome(
                    entry_id=row["entry_id"],
                    revision=row["revision"],
                    state=row["state"],
                    retrievable=retrievable,
                    name=row["name"],
                    tags=tuple(row.get("tags") or ()),
                    content=row["content"],
                    content_sha256=row["content_sha256"],
                    source_postmortem_id=row["source_postmortem_id"],
                    source_version=row["source_version"],
                    source_proposal_key=row["source_proposal_key"],
                    supersedes_revision=row.get("supersedes_revision"),
                    approved_by=row["approved_by"],
                    approved_by_kind=row["approved_by_kind"],
                    approved_at=row["approved_at"],
                    approval_event_id=row["event_id"],
                    revoked_reason=row.get("revoked_reason"),
                    revoked_by=row.get("revoked_by"),
                    revoked_by_kind=row.get("revoked_by_kind"),
                    revoked_at=row.get("revoked_at"),
                    freshness=(
                        dict(active["freshness"])
                        if retrievable and active is not None
                        else None
                    ),
                )
            )
    return tuple(found)


def _unknown(scenario_id: str, subject_id: str) -> PostmortemOutcome:
    return PostmortemOutcome(
        scenario_id=scenario_id,
        subject_id=subject_id,
        generation_status="unknown",
        unknown_reasons=("SUBJECT_MISMATCH",),
        postmortem_id=None,
        postmortem_generation=None,
        latest_version=None,
        versions=(),
        schedule=None,
        recent_attempts=(),
        review_actions=(),
        knowledge=(),
        knowledge_actions=(),
    )


def postmortem_outcome(scenario: Any, records: PostmortemRecords) -> PostmortemOutcome:
    """Project one incident's committed postmortem records for ``scenario``
    (``scenario.subject_id`` is the incident id). Records of another subject
    yield ``generation_status="unknown"`` (D11)."""
    scenario_id = str(scenario.scenario_id)
    subject_id = str(scenario.subject_id)
    try:
        return _project(scenario_id, subject_id, records)
    except _Mismatch:
        return _unknown(scenario_id, subject_id)


def _project(
    scenario_id: str, subject_id: str, records: PostmortemRecords
) -> PostmortemOutcome:
    view = records.view
    _same(records.incident_id, subject_id)
    _same(view.get("incident_id"), subject_id)
    attempts = tuple(dict(a) for a in view.get("attempts") or ())
    for attempt in attempts:
        _same(attempt.get("incident_id"), subject_id)
    snapshot = view.get("postmortem")
    if snapshot is None:
        if (
            records.published
            or records.entries
            or records.active
            or records.postmortem_audit
            or records.entry_audit
        ):
            raise _Mismatch
        return PostmortemOutcome(
            scenario_id=scenario_id,
            subject_id=subject_id,
            generation_status=str(view["status"]),
            unknown_reasons=(),
            postmortem_id=None,
            postmortem_generation=None,
            latest_version=None,
            versions=(),
            schedule=None if view.get("schedule") is None else dict(view["schedule"]),
            recent_attempts=attempts,
            review_actions=(),
            knowledge=(),
            knowledge_actions=(),
        )
    _same(snapshot.get("incident_id"), subject_id)
    postmortem_id = snapshot["postmortem_id"]
    audit = tuple(
        _review_record(row, "postmortem", postmortem_id)
        for row in records.postmortem_audit
    )
    published = {row["entry_id"] for row in records.published}
    # every published entry carries its history, knowledge read and audit
    # trail (``postmortem_records`` reads all three); a missing or extra key
    # is an incomplete or foreign record set
    if not (
        set(records.entries)
        == set(records.active)
        == set(records.entry_audit)
        == published
    ):
        raise _Mismatch
    knowledge_actions = tuple(
        _review_record(row, "knowledge_entry", entry_id)
        for entry_id in records.entries
        for row in records.entry_audit.get(entry_id, ())
    )
    return PostmortemOutcome(
        scenario_id=scenario_id,
        subject_id=subject_id,
        generation_status=str(view["status"]),
        unknown_reasons=(),
        postmortem_id=postmortem_id,
        postmortem_generation=snapshot["generation"],
        latest_version=snapshot["latest_version"],
        versions=tuple(
            _version(row, postmortem_id, audit) for row in snapshot["versions"]
        ),
        schedule=None if view.get("schedule") is None else dict(view["schedule"]),
        recent_attempts=attempts,
        review_actions=audit,
        knowledge=_revisions(records, postmortem_id),
        knowledge_actions=knowledge_actions,
    )
