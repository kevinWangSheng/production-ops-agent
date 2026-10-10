"""Frozen M1-03 interface between draft generation, review and acceptance (D26).

Contract revision r3 (decisions D1-D27, R1-R6 in the M1-03 task record and
its decision table). Step 2 (draft generation, worker side) produces these
shapes, step 3 (workbench review) and step 4 (acceptance projection) only
consume them. Changing a name, a field or an enumeration value here is a
contract change: report it before editing.

Contents:

* Version strings recorded with every generation (D20).
* Enumerations: version state, stale reason, certainty, dispute state,
  generation status of an incident, attempt status and error codes, review
  actions, events (R6), review error classes (R3).
* The model output schema (R1) as dataclasses with a strict parser lives in
  ``opspilot.knowledge.generation``; the field names are fixed here
  (``MODEL_OUTPUT_FIELDS``).
* The stored document of a version (``PostmortemDocument``), the snapshot of
  an incident's postmortem (``IncidentPostmortemView`` /
  ``PostmortemSnapshot``) and the review command (``ReviewCommand``).

Nothing in this module touches the database or the network.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, TypedDict, get_args
from uuid import UUID

CONTRACT_REVISION = "r7"

# --- versions recorded with every generation (D20) -------------------------

# The model is the investigation's (``deepseek-flash``, D20); everything
# below is the postmortem's own and moves independently of the investigation
# prompt and report schema.
POSTMORTEM_PROMPT_VERSION = "f13-postmortem-prompt-v2"
POSTMORTEM_OUTPUT_SCHEMA_VERSION = "f13-postmortem-output-v1"
POSTMORTEM_CONTENT_SCHEMA_VERSION = "f13-postmortem-content-v1"
POSTMORTEM_INPUT_POLICY_VERSION = "f13-postmortem-input-v1"
# The request profile: JSON mode, thinking enabled, reasoning effort high,
# no tools (``opspilot.investigation.loop.serialized_request``).
POSTMORTEM_MODEL_PROFILE = "deepseek-flash/json/thinking-high/no-tools"

# --- enumerations -------------------------------------------------------------

VersionState = Literal[
    "draft", "under_review", "approved", "rejected", "returned", "stale"
]
StaleReason = Literal[
    "control_generation_changed",
    "incident_reopened",
    "observation_changed",
    "run_added",
    "input_added",
    "evidence_changed",
]
Certainty = Literal["deterministic", "supported", "uncertain"]
DisputeState = Literal["undisputed", "disputed"]
ConclusionAuthor = Literal["code", "model"]
PrincipalKind = Literal["basic_auth", "worker"]

# Sections of a version. Code-assembled (deterministic, D2) first, then the
# model's (impact summary, findings, hypotheses/counter-evidence,
# recommendations).
CodeSection = Literal["incident", "timeline", "runs", "human_actions", "recovery"]
ModelSection = Literal["impact_summary", "findings", "hypotheses", "recommendations"]
CODE_SECTIONS: tuple[str, ...] = get_args(CodeSection)
MODEL_SECTIONS: tuple[str, ...] = get_args(ModelSection)
# A model statement's claim: only ``fact`` needs evidence citable as fact
# (D2); ``hypothesis``, ``counter_evidence`` and ``recommendation`` are never
# ``supported``.
Claim = Literal["fact", "hypothesis", "counter_evidence", "recommendation"]

# Why a model conclusion's citations failed (machine-visible, D22).
CitationError = Literal[
    # cites an evidence id that is not in the generation input
    "UNKNOWN_EVIDENCE",
    # a ``fact`` claim with no cited evidence citable as fact
    "NOT_CITABLE_AS_FACT",
]

# Where the incident's postmortem stands, as the workbench and the
# acceptance projection show it (D11, D22: "not generated / generation
# failed / citations failed" are distinct).
GenerationStatus = Literal[
    # no version and no attempt (also: observation not ended yet)
    "not_generated",
    # a worker holds the generation lease right now
    "generating",
    # the latest attempt failed before writing a version (provider failure,
    # input over the limits, output still invalid after the one repair)
    "generation_failed",
    # latest version is a draft whose citations failed (never reviewable)
    "citations_failed",
    "under_review",
    "approved",
    "rejected",
    # latest version returned for revision; the worker regenerates (D19)
    "returned",
    # latest version went stale; the worker regenerates (D18)
    "stale",
]

AttemptStatus = Literal[
    "running",
    # wrote a version (under review, or a draft whose citations failed)
    "succeeded",
    # provider / input / output failure: no version written (D22)
    "failed",
    # the incident moved, or another writer won, before the version was
    # written; nothing to retry at this watermark
    "superseded",
    # the lease lapsed with the attempt unfinished (crash); reaped by the
    # next claim
    "abandoned",
]
AttemptErrorCode = Literal[
    "MODEL_REJECTED",
    "MODEL_UNAVAILABLE",
    "INPUT_TOO_LARGE",
    "OUTPUT_INVALID",
    "WATERMARK_MOVED",
    "GENERATION_CONFLICT",
    "LEASE_LOST",
]
# Failures retried with backoff at the same watermark, at most three
# attempts together with citation-failed drafts (D22, D30, D31, R7); the
# others wait for the watermark to move (r5).
RETRYABLE_ATTEMPT_ERRORS = frozenset(
    {"MODEL_UNAVAILABLE", "MODEL_REJECTED", "OUTPUT_INVALID"}
)

# Page-level review actions (D5, D25). ``supersede`` is presented on its own
# and executed as an approval of a version whose proposals name existing
# entries; ``revoke`` acts on a knowledge entry.
ReviewAction = Literal["approve", "reject", "return", "supersede", "revoke"]
REVIEW_ACTIONS: tuple[str, ...] = get_args(ReviewAction)

# Audit actions written by the store (0008 ``ACTIONS``).
AuditAction = Literal[
    "generate",
    "submit",
    "mark_stale",
    "approve",
    "reject",
    "return",
    "publish",
    "supersede",
    "revoke",
]

# Events on the incident's event stream (R6). Payloads carry ids, version,
# state, generation and reason only; the page reloads the snapshot.
EventKind = Literal[
    "postmortem_generated",
    "postmortem_generation_failed",
    "postmortem_stale",
    "postmortem_reviewed",
    "knowledge_changed",
]
EVENT_PAYLOAD_KEYS: Mapping[str, tuple[str, ...]] = {
    "postmortem_generated": ("postmortem_id", "version", "state", "generation"),
    "postmortem_generation_failed": ("attempt_id", "reason"),
    "postmortem_stale": ("postmortem_id", "version", "state", "generation", "reason"),
    "postmortem_reviewed": (
        "postmortem_id",
        "version",
        "state",
        "generation",
        "reason",
    ),
    "knowledge_changed": ("entry_id", "revision", "state", "generation", "reason"),
}

# Store error codes (``opspilot.knowledge.store``) grouped for the page (R3):
# a conflict asks for a reload, a refusal is final for this request.
ReviewErrorClass = Literal["conflict", "refused", "invalid", "not_found", "unavailable"]
REVIEW_ERROR_CLASSES: Mapping[str, ReviewErrorClass] = {
    "GENERATION_CONFLICT": "conflict",
    "ENTRY_GENERATION_CONFLICT": "conflict",
    "WATERMARK_MOVED": "conflict",
    "OPEN_VERSION_EXISTS": "conflict",
    "IDEMPOTENCY_CONFLICT": "conflict",
    "ILLEGAL_TRANSITION": "refused",
    "DISPUTED": "refused",
    "CITATIONS_INVALID": "refused",
    "PRINCIPAL_NOT_ALLOWED": "refused",
    "INTEGRITY_REFUSED": "refused",
    "INVALID_INPUT": "invalid",
    "NOT_FOUND": "not_found",
    # transient codes of ``_StoreBase``
    "STORAGE_UNAVAILABLE": "unavailable",
    "TIMEOUT": "unavailable",
    "RETRY": "unavailable",
}

# --- model output (R1) --------------------------------------------------------

# Top-level keys and per-item keys of the model's JSON object; unknown keys,
# duplicate keys and missing required keys are refused
# (``opspilot.knowledge.generation.parse_model_output``).
MODEL_OUTPUT_FIELDS: Mapping[str, tuple[str, ...]] = {
    "": ("narrative_sections", "conclusions", "proposals", "disputes"),
    "narrative_sections": ("key", "section", "body", "evidence_ids"),
    "conclusions": ("key", "section", "claim", "body", "evidence_ids"),
    "proposals": ("key", "name", "tags", "symptoms", "checks", "evidence_ids"),
    "disputes": ("conclusion_key", "reason"),
}

# --- stored document of one version ------------------------------------------


class EvidenceRef(TypedDict):
    """One evidence binding of a conclusion (D2), resolved by code from the
    generation input, never taken from the model."""

    evidence_id: str
    scope: str
    window_start: str
    window_end: str
    citable_as_fact: bool


class CatalogEntry(EvidenceRef):
    # ``investigation``: an adopted, committed tool view of one of the
    # incident's Runs; ``recovery``: one signal reading of an adopted
    # observation sample (id ``<sample_id>:<signal_name>``, as F6's
    # ``recovery_outcome``)
    kind: Literal["investigation", "recovery"]


class GenerationRecord(TypedDict):
    """How a version was generated (D20, D22, D23)."""

    attempt_id: str
    model: str
    response_model: str | None
    model_profile: str
    prompt_version: str
    output_schema_version: str
    input_policy_version: str
    input_sha256: str
    input_bytes: int
    model_requests: int
    max_model_requests: int
    usage: dict[str, int]
    repaired: bool
    revises_version: int | None
    return_reason: str | None


class ValidationRecord(TypedDict):
    citations_valid: bool
    # conclusion key -> citation error codes; only keys that failed
    errors: dict[str, list[CitationError]]


class PostmortemDocument(TypedDict):
    """The canonical JSON stored as a version's ``content`` (and hashed).

    ``incident``/``timeline``/``runs``/``human_actions``/``recovery`` are
    code-assembled from PostgreSQL at the version's watermark; the
    conclusions table carries the same sections as rows. Model text appears
    only as conclusion rows (and ``proposals``)."""

    schema_version: str
    contract_revision: str
    incident: dict[str, Any]
    timeline: list[dict[str, Any]]
    runs: list[dict[str, Any]]
    human_actions: list[dict[str, Any]]
    recovery: dict[str, Any]
    evidence_catalog: list[CatalogEntry]
    generation: GenerationRecord
    validation: ValidationRecord


# --- snapshot read by the workbench and the acceptance projection -------------


class DisputeView(TypedDict):
    dispute_id: UUID
    postmortem_id: UUID
    version: int
    conclusion_key: str
    reason: str
    raised_by: str
    raised_by_kind: PrincipalKind
    event_id: UUID
    raised_at: datetime


class ConclusionView(TypedDict):
    postmortem_id: UUID
    version: int
    conclusion_key: str
    ordinal: int
    section: str
    body: str
    author: ConclusionAuthor
    certainty: Certainty
    citations_valid: bool
    evidence_refs: list[EvidenceRef]
    dispute_state: DisputeState
    disputes: list[DisputeView]


class ProposalView(TypedDict):
    postmortem_id: UUID
    version: int
    proposal_key: str
    name: str
    tags: list[str]
    # canonical JSON text: {"symptoms", "checks", "evidence_refs"}
    content: str
    content_sha256: str
    supersedes_entry_id: UUID | None


class VersionView(TypedDict):
    postmortem_id: UUID
    version: int
    revises_version: int | None
    state: VersionState
    stale_reason: StaleReason | None
    content: str
    content_sha256: str
    incident_control_generation: int
    observation_generation: int
    observation_session_id: UUID
    observation_ending_id: UUID
    run_count: int
    last_run_id: UUID | None
    input_watermark: int
    evidence_snapshot_sha256: str
    generated_by: str
    generated_by_kind: PrincipalKind
    created_at: datetime
    conclusions: list[ConclusionView]
    proposals: list[ProposalView]


class PostmortemSnapshot(TypedDict):
    """``KnowledgeStore.postmortem_for_incident`` / ``postmortem``: one
    snapshot read of the postmortem and every version."""

    postmortem_id: UUID
    incident_id: UUID
    generation: int
    latest_version: int
    created_at: datetime
    updated_at: datetime
    versions: list[VersionView]


class AttemptView(TypedDict):
    attempt_id: UUID
    incident_id: UUID
    status: AttemptStatus
    error_code: AttemptErrorCode | None
    revises_version: int | None
    version: int | None
    model_requests: int
    max_model_requests: int
    started_at: datetime
    finished_at: datetime | None


class ScheduleView(TypedDict):
    # D19: the returned version waiting for regeneration, or None
    pending_regeneration_version: int | None
    lease_active: bool
    consecutive_failures: int
    next_attempt_at: datetime | None
    last_error_code: AttemptErrorCode | None


class IncidentPostmortemView(TypedDict):
    """``KnowledgeStore.incident_postmortem(incident_id)``: what the
    workbench page and the acceptance projection read for one incident."""

    incident_id: UUID
    status: GenerationStatus
    postmortem: PostmortemSnapshot | None
    schedule: ScheduleView
    # newest first, at most ``ATTEMPTS_SHOWN``
    attempts: list[AttemptView]


ATTEMPTS_SHOWN = 10


class PublishedRevisionView(TypedDict):
    """``KnowledgeStore.knowledge_from_postmortem(postmortem_id)``: one
    knowledge revision an approval of the postmortem published, with its
    current state. Review navigation and acceptance projection (D11), not a
    knowledge read: superseded and revoked revisions are included (D3's read
    is ``active_revision``). Appended after the D26 freeze (lead-approved,
    read-only, steps 3 and 4)."""

    entry_id: UUID
    revision: int
    name: str
    source_version: int
    source_proposal_key: str
    supersedes_revision: int | None
    state: Literal["active", "superseded", "revoked"]


# --- review command (step 3 consumes; D5, D6, D9, D25) -----------------------


@dataclass(frozen=True)
class ReviewCommand:
    """One workbench review request, before the store call.

    * ``approve`` / ``supersede``: ``postmortem_id``, ``version``,
      ``expected_generation`` (the postmortem's) and ``entry_generations``:
      the expected generation of every entry the version's proposals name
      (complete, D25); ``supersede`` requires at least one.
    * ``reject`` / ``return``: ``postmortem_id``, ``version``,
      ``expected_generation`` and a non-empty ``reason``.
    * ``revoke``: ``entry_id``, ``revision``, ``expected_generation`` (the
      entry's) and a non-empty ``reason``.

    The actor is never part of the command: the web layer builds it from the
    authenticated Basic Auth principal (R4).
    """

    action: ReviewAction
    idempotency_key: str
    expected_generation: int
    postmortem_id: UUID | None = None
    version: int | None = None
    reason: str | None = None
    entry_generations: Mapping[UUID, int] = field(default_factory=dict)
    entry_id: UUID | None = None
    revision: int | None = None

    def problems(self) -> tuple[str, ...]:
        """Field names that are missing or not allowed for ``action``;
        empty when the command is well formed."""
        found: list[str] = []
        if self.action not in REVIEW_ACTIONS:
            return ("action",)
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key:
            found.append("idempotency_key")
        if (
            not isinstance(self.expected_generation, int)
            or isinstance(self.expected_generation, bool)
            or self.expected_generation < 0
        ):
            found.append("expected_generation")
        on_entry = self.action == "revoke"
        if on_entry:
            if not isinstance(self.entry_id, UUID):
                found.append("entry_id")
            if not _positive(self.revision):
                found.append("revision")
            if self.postmortem_id is not None or self.version is not None:
                found.append("postmortem_id")
        else:
            if not isinstance(self.postmortem_id, UUID):
                found.append("postmortem_id")
            if not _positive(self.version):
                found.append("version")
            if self.entry_id is not None or self.revision is not None:
                found.append("entry_id")
        if self.action in ("reject", "return", "revoke"):
            if not isinstance(self.reason, str) or not self.reason.strip():
                found.append("reason")
        elif self.reason is not None:
            found.append("reason")
        if self.action in ("approve", "supersede"):
            if not isinstance(self.entry_generations, Mapping) or not all(
                isinstance(k, UUID) and _non_negative(v)
                for k, v in self.entry_generations.items()
            ):
                found.append("entry_generations")
            elif self.action == "supersede" and not self.entry_generations:
                found.append("entry_generations")
        elif self.entry_generations:
            found.append("entry_generations")
        return tuple(found)


def _positive(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _non_negative(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


__all__ = [
    "ATTEMPTS_SHOWN",
    "CODE_SECTIONS",
    "CONTRACT_REVISION",
    "EVENT_PAYLOAD_KEYS",
    "MODEL_OUTPUT_FIELDS",
    "MODEL_SECTIONS",
    "POSTMORTEM_CONTENT_SCHEMA_VERSION",
    "POSTMORTEM_INPUT_POLICY_VERSION",
    "POSTMORTEM_MODEL_PROFILE",
    "POSTMORTEM_OUTPUT_SCHEMA_VERSION",
    "POSTMORTEM_PROMPT_VERSION",
    "RETRYABLE_ATTEMPT_ERRORS",
    "REVIEW_ACTIONS",
    "REVIEW_ERROR_CLASSES",
    "AttemptErrorCode",
    "AttemptStatus",
    "AttemptView",
    "AuditAction",
    "CatalogEntry",
    "Certainty",
    "CitationError",
    "Claim",
    "CodeSection",
    "ConclusionAuthor",
    "ConclusionView",
    "DisputeState",
    "DisputeView",
    "EventKind",
    "EvidenceRef",
    "GenerationRecord",
    "GenerationStatus",
    "IncidentPostmortemView",
    "ModelSection",
    "PostmortemDocument",
    "PostmortemSnapshot",
    "PrincipalKind",
    "ProposalView",
    "PublishedRevisionView",
    "ReviewAction",
    "ReviewCommand",
    "ReviewErrorClass",
    "ScheduleView",
    "StaleReason",
    "ValidationRecord",
    "VersionState",
]
