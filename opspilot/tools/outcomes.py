"""Result, operation and evidence types of one read-only tool operation.

Five outcome classes are kept apart on purpose, because acceptance requires
input errors, connectivity failures, timeouts and no-data results to stay
distinguishable (``feature_list.json`` F3) and requires denied/unknown/handoff
to be explicit (``PRODUCT-CONSTRAINTS.md``, "Product workflow" and "Runtime and
human control requirements"):

``ok``       the query ran and returned data.
``no_data``  the query ran and the source legitimately returned nothing. It is
             evidence of absence of *data*, never evidence of health, and it is
             never an error.
``error``    input, source or result-handling failure, further separated by a
             fixed ``reason`` code.
``timeout``  the *request* bound was reached: the transport timed out, or the
             fetch outlasted the timeout the gateway handed it. Status at the
             source is unknown, never success.
``denied``   authorization, scope, control-state or budget refusal. The request
             was never sent, unless an in-flight control change or deadline
             expiry invalidated a result that had already arrived.

The two late cases are separated by *which* bound was exceeded, not by how late
the result was: outlasting the per-request timeout produces ``timeout``, while
outlasting the Run's authorization deadline produces ``denied`` /
``DEADLINE_EXCEEDED``.

``source_contact`` is tracked separately from the status because technical plan
section 7 forbids treating unknown state as success, and section 8 states that
cancellation cannot recall a read-only request that already reached the source.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

__all__ = [
    "DENIED_REASONS",
    "ERROR_REASONS",
    "PROJECTION_REVISION",
    "EvidenceRecord",
    "SourceContact",
    "ToolOperation",
    "ToolOutcome",
    "ToolStatus",
    "Window",
]

# Version of the raw -> view projection. Stored with every evidence record so a
# later reader can tell which projection produced the view it is reading.
PROJECTION_REVISION = "m1-01-tool-view-v4"

ToolStatus = Literal["ok", "no_data", "error", "timeout", "denied"]
SourceContact = Literal["none", "possible", "confirmed"]

DENIED_REASONS = frozenset(
    {
        "CONTROL_GENERATION_CHANGED",
        "CONTROL_UNAVAILABLE",
        "DEADLINE_EXCEEDED",
        "OPERATION_BUDGET_EXHAUSTED",
        "PARAM_NOT_ALLOWED",
        "SUSPENDED",
        "TARGET_NOT_AUTHORIZED",
        "TARGET_NOT_REGISTERED",
        "TARGET_REGISTRY_CHANGED",
        "TARGET_SOURCE_MISMATCH",
        "TIME_BUDGET_EXHAUSTED",
        "TOOL_NOT_IN_SCOPE",
        "TOOL_NOT_REGISTERED",
        "TOOL_REGISTRY_CHANGED",
        "WINDOW_OUT_OF_SCOPE",
        "WINDOW_TOO_LARGE",
    }
)

ERROR_REASONS = frozenset(
    {
        "EVIDENCE_NOT_COMMITTED",
        "INVALID_PARAMS",
        "MALFORMED_RESULT",
        "RESULT_TOO_LARGE",
        "SOURCE_ERROR",
        "SOURCE_UNAVAILABLE",
    }
)

_TIMEOUT_REASONS = frozenset({"GATEWAY_TIMEOUT", "TOOL_TIMEOUT"})


def _aware(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


@dataclass(frozen=True)
class Window:
    """An absolute, timezone-aware query window.

    Technical plan section 8 requires an absolute query window in the tool
    contract; a relative expression such as "last 5m" is refused so a replay of
    the stored operation queries the same interval it originally queried.
    """

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if not (_aware(self.start) and _aware(self.end)) or self.start >= self.end:
            raise ValueError("INVALID_WINDOW")
        object.__setattr__(self, "start", self.start.astimezone(timezone.utc))
        object.__setattr__(self, "end", self.end.astimezone(timezone.utc))

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()

    def contains(self, other: "Window") -> bool:
        return self.start <= other.start and other.end <= self.end

    def as_json(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}

    @classmethod
    def parse(cls, value: object) -> "Window | None":
        """Build a window from model-proposed JSON; ``None`` if unusable.

        Model output is untrusted input, so an unparsable or relative window is
        reported as an outcome by the executor rather than raised here.
        """

        if not isinstance(value, Mapping) or set(value) != {"start", "end"}:
            return None
        bounds = []
        for key in ("start", "end"):
            text = value[key]
            if not isinstance(text, str):
                return None
            try:
                moment = datetime.fromisoformat(text)
            except ValueError:
                return None
            if not _aware(moment):
                return None  # A naive timestamp is not an absolute instant.
            bounds.append(moment)
        try:
            return cls(*bounds)
        except ValueError:
            return None


@dataclass(frozen=True)
class ToolOperation:
    """Audit header of one tool operation, present on every outcome.

    ``requested_tool`` and ``requested_target`` keep what the model asked for;
    ``tool`` and ``target_id`` keep what the gateway actually resolved from its
    own registries. The two are never merged, so an audit reader can see that a
    model-supplied name did not select the target.
    """

    operation_id: str
    scope_id: str
    subject_kind: str
    subject_id: str
    run_id: str
    requested_tool: object
    requested_target: object
    registry_revision: str
    tool_registry_revision: str
    tool: str | None = None
    tool_version: str | None = None
    source: str | None = None
    target_id: str | None = None
    query: str | None = None
    window: Window | None = None
    started_at: datetime | None = None
    # Trusted-clock instant at which control state and the scope deadline were
    # verified immediately before dispatch. Set once, by the pre-dispatch
    # check; the in-flight re-check after the fetch does not update it, and
    # keys on ``finished_at`` instead. It is recorded separately from
    # ``started_at`` because the control lookup sits between them: on a
    # pre-dispatch ``DEADLINE_EXCEEDED`` denial it is the only field showing
    # when the expiry was observed, and nothing else in the record can be used
    # to derive it.
    authorized_at: datetime | None = None
    # Set only immediately before ``transport.fetch()`` is actually called,
    # never inferred from ``timeout_seconds`` being populated: ``_reserve()``
    # computes ``timeout_seconds`` before the pre-dispatch ledger charge and
    # the new pre-fetch control/deadline re-check run, so a denial from
    # either of those (or the charge itself) would otherwise leave
    # ``timeout_seconds`` set on an operation that never reached the
    # transport, making ``sent`` claim dispatch that never happened.
    dispatched: bool = False
    finished_at: datetime | None = None
    elapsed_seconds: float | None = None
    timeout_seconds: float | None = None
    credential_ref: str | None = None

    @property
    def sent(self) -> bool:
        """Whether the gateway handed this operation to the transport."""

        return self.dispatched

    def audit_json(self) -> dict[str, object]:
        """Audit-facing record. Not the model-facing view."""

        return {
            "operation_id": self.operation_id,
            "scope_id": self.scope_id,
            "subject_kind": self.subject_kind,
            "subject_id": self.subject_id,
            "run_id": self.run_id,
            "requested_tool": self.requested_tool
            if isinstance(self.requested_tool, str)
            else repr(self.requested_tool),
            "requested_target": self.requested_target
            if isinstance(self.requested_target, str)
            else repr(self.requested_target),
            "registry_revision": self.registry_revision,
            "tool_registry_revision": self.tool_registry_revision,
            "tool": self.tool,
            "tool_version": self.tool_version,
            "source": self.source,
            "target_id": self.target_id,
            "query": self.query,
            "window": self.window.as_json() if self.window else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "authorized_at": self.authorized_at.isoformat()
            if self.authorized_at
            else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "elapsed_seconds": self.elapsed_seconds,
            "timeout_seconds": self.timeout_seconds,
            "credential_ref": self.credential_ref,
            "sent": self.sent,
        }


@dataclass(frozen=True)
class EvidenceRecord:
    """Registered evidence: the exact source bytes, the projection, both hashes.

    ``PRODUCT-CONSTRAINTS.md`` requires every observation to carry source,
    query, target, time window, freshness and an inspectable evidence
    reference, and requires a report link to resolve to the captured evidence
    rather than to model prose. ``adopted`` records whether the observation may
    inform the current conclusion; an invalidated result is kept as history
    only.
    """

    evidence_id: str
    operation: ToolOperation
    status: ToolStatus
    raw: bytes
    raw_sha256: str
    _view: Mapping[str, object]
    view_sha256: str
    projection_revision: str
    observed_at: datetime
    data_as_of: datetime | None
    source_start_at: datetime | None
    source_end_at: datetime | None
    result_count: int
    incomplete: bool
    truncated: bool
    omitted_rows: int
    omitted_bytes: int
    adopted: bool

    @property
    def view(self) -> Mapping[str, object]:
        """A detached copy: mutating it can never change committed evidence.

        ``EvidenceRecord`` is otherwise frozen, but a ``dict``/``list`` value is
        still mutable through any reference to it. Handing out the same object
        on every access let a consumer -- or an evidence sink that retained the
        record -- mutate "committed" evidence in place while ``view_sha256``
        kept hashing the original contents (bot review finding).
        """

        return deepcopy(self._view)

    @property
    def freshness_seconds(self) -> float | None:
        """Age of the newest datum, or ``None`` when the source did not say.

        ``None`` means unknown and must stay visible as unknown; it is not the
        same as fresh.
        """

        if self.data_as_of is None:
            return None
        return (self.observed_at - self.data_as_of).total_seconds()


@dataclass(frozen=True)
class ToolOutcome:
    """What one tool operation produced, in exactly one of five classes."""

    operation: ToolOperation
    status: ToolStatus
    reason: str | None
    source_contact: SourceContact
    model_view: Mapping[str, object]
    evidence: EvidenceRecord | None = None

    def __post_init__(self) -> None:
        if self.status == "ok":
            valid = self.reason is None
        elif self.status == "no_data":
            valid = self.reason == "NO_DATA"
        elif self.status == "error":
            valid = self.reason in ERROR_REASONS
        elif self.status == "timeout":
            valid = self.reason in _TIMEOUT_REASONS
        else:
            valid = self.reason in DENIED_REASONS
        if not valid:
            raise ValueError("INVALID_OUTCOME_REASON")

    @property
    def adopted(self) -> bool:
        """Whether this observation may inform the current conclusion."""

        return self.evidence is not None and self.evidence.adopted
