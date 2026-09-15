"""`Postmortem / KnowledgeRevision` (C3 section 4, review rules from section 10).

A postmortem starts as a draft. Only a human review produces an immutable
knowledge revision, which can later be superseded or revoked with its audit
intact. The approver type is ``human`` by construction, so a platform score or a
model self-assessment cannot publish knowledge.
"""

from typing import Literal

from pydantic import AwareDatetime

from .base import DTO, DomainError, StateMachine, Text
from .subjects import SubjectRef

PostmortemState = Literal["draft", "under_review", "approved", "rejected"]
KnowledgeState = Literal["active", "superseded", "revoked"]

POSTMORTEM = StateMachine(
    "postmortem",
    {
        "draft": {"submit_for_review": "under_review"},
        "under_review": {
            "human_approve": "approved",
            "human_reject": "rejected",
            "human_return_to_draft": "draft",
        },
        "approved": {},
        "rejected": {},
    },
)

KNOWLEDGE_REVISION = StateMachine(
    "knowledge_revision",
    {
        "active": {"supersede": "superseded", "revoke": "revoked"},
        "superseded": {},
        "revoked": {},
    },
)


class Postmortem(DTO):
    """A reviewable draft account of one subject; never trusted knowledge."""

    postmortem_id: Text
    subject: SubjectRef
    content_hash: Text
    created_at: AwareDatetime
    state: PostmortemState = "draft"
    reviewed_by: Text | None = None
    reviewed_at: AwareDatetime | None = None


class KnowledgeRevision(DTO):
    """An immutable knowledge version with its provenance and review record."""

    revision_id: Text
    postmortem_id: Text
    content_hash: Text
    approver_kind: Literal["human"]
    approved_by: Text
    approved_at: AwareDatetime
    state: KnowledgeState = "active"
    supersedes_revision_id: Text | None = None


def advance_postmortem(
    postmortem: Postmortem, trigger: str, *, reviewer: str | None = None
) -> Postmortem:
    """Fire one postmortem trigger, or raise ``ILLEGAL_TRANSITION``."""
    if not isinstance(postmortem, Postmortem):
        raise DomainError("INVALID_INPUT", "postmortem is required")
    state = POSTMORTEM.fire(postmortem.state, trigger)
    update: dict[str, object] = {"state": state}
    if trigger in ("human_approve", "human_reject"):
        if not isinstance(reviewer, str) or not reviewer:
            raise DomainError("INVALID_INPUT", "a human reviewer is required")
        update["reviewed_by"] = reviewer
    return postmortem.model_copy(update=update)


def advance_knowledge(revision: KnowledgeRevision, trigger: str) -> KnowledgeRevision:
    """Fire one knowledge revision trigger, or raise ``ILLEGAL_TRANSITION``."""
    if not isinstance(revision, KnowledgeRevision):
        raise DomainError("INVALID_INPUT", "knowledge revision is required")
    return revision.model_copy(
        update={"state": KNOWLEDGE_REVISION.fire(revision.state, trigger)}
    )


def publish_knowledge(
    postmortem: Postmortem,
    *,
    revision_id: str,
    approved_by: str,
    approved_at: AwareDatetime,
    supersedes_revision_id: str | None = None,
) -> KnowledgeRevision:
    """Produce an immutable knowledge revision from an approved postmortem."""
    if not isinstance(postmortem, Postmortem):
        raise DomainError("INVALID_INPUT", "postmortem is required")
    if postmortem.state != "approved":
        raise DomainError(
            "ILLEGAL_TRANSITION", f"{postmortem.state} is not human approved"
        )
    if not isinstance(approved_by, str) or not approved_by:
        raise DomainError("INVALID_INPUT", "a human approver is required")
    return KnowledgeRevision(
        revision_id=revision_id,
        postmortem_id=postmortem.postmortem_id,
        content_hash=postmortem.content_hash,
        approver_kind="human",
        approved_by=approved_by,
        approved_at=approved_at,
        supersedes_revision_id=supersedes_revision_id,
    )
