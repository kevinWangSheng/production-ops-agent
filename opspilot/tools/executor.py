"""The read-only tool executor: scope, target resolution, bounds, evidence.

This is the pure-logic half of the Tool Gateway (technical plan section 3). It
decides whether a model-proposed tool call may run at all, resolves the exact
target from the registry, bounds the request in time and size, classifies the
result into one of five outcome classes and registers raw/view/hash evidence
before any of it may reach model context.

What it deliberately does not do:

- It never mutates anything. There is no write entry point, the registry
  refuses a registration with a mutating verb, and every transport request
  carries ``read_only=True``.
- It never holds credentials. A target carries an opaque ``credential_ref``;
  the transport resolves the secret on its own authenticated channel, outside
  this package (``PRODUCT-CONSTRAINTS.md``, "Data flow contract").
- It never takes authorization, target identity, scope, budget or control state
  from a tool result. Results are untrusted evidence, never instructions.
- It does not redact secrets that a *data source* may itself return inside a
  payload. That is a separate, unbuilt redaction policy; until it exists, a
  source whose payload can contain secret material must not be registered.

Failure vocabulary: contract violations by the operator raise
``ToolContractError``; everything caused by model-proposed input or by the data
source is returned as a :class:`~opspilot.tools.outcomes.ToolOutcome`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from hashlib import sha256
from typing import Protocol, runtime_checkable

from .outcomes import (
    PROJECTION_REVISION,
    EvidenceRecord,
    SourceContact,
    ToolOperation,
    ToolOutcome,
    ToolStatus,
    Window,
)
from .registry import (
    EMPTY_VIEW_BYTES,
    READ_ONLY_VERBS,
    RegisteredTarget,
    TargetRegistry,
    ToolContractError,
    ToolRegistration,
    ToolRegistry,
    canonical,
    canonical_hash,
)

__all__ = [
    "MAX_OPERATIONS_PER_RUN",
    "MAX_TOOL_SECONDS_PER_RUN",
    "ControlAuthority",
    "ControlSnapshot",
    "ControlUnavailable",
    "EvidenceSink",
    "QueryScope",
    "ReadOnlyToolExecutor",
    "ReadOnlyTransport",
    "ToolRequest",
    "ToolUsage",
    "ToolUsageLedger",
    "TransportError",
    "TransportRequest",
    "TransportResponse",
    "TransportResultTooLarge",
    "TransportTimeout",
    "TransportUnavailable",
]

# Frozen M1-01 per-Run ceilings (acceptance packet
# docs/testing/first-investigation-v4-2026-09-10.md, user approved 2026-09-13):
# at most 20 tool calls per Run and 240 s of cumulative tool wall time.
MAX_OPERATIONS_PER_RUN = 20
MAX_TOOL_SECONDS_PER_RUN = 240.0

_STEP_ID_MAX = 128


class TransportError(Exception):
    """Base class for transport failures the gateway must classify."""


class TransportTimeout(TransportError):
    """The request deadline elapsed. Whether the source ran it is unknown."""


class TransportUnavailable(TransportError):
    """The source could not be reached or did not answer."""


class TransportResultTooLarge(TransportError):
    """The transport stopped reading because the byte ceiling was reached."""


class ControlUnavailable(Exception):
    """Control state could not be read, so no new call may be made."""


@dataclass(frozen=True)
class ControlSnapshot:
    """Controller-owned control state at one instant."""

    control_generation: int
    suspended: bool = False


@dataclass(frozen=True)
class QueryScope:
    """One Run's authorization, issued by the Controller, not by the model.

    Technical plan section 8: "authorization follows the real identity and
    control state stored by the Controller; permission fields supplied by the
    model are not trusted".
    """

    scope_id: str
    subject_kind: str
    subject_id: str
    run_id: str
    control_generation: int
    registry_revision: str
    tool_registry_revision: str
    target_ids: frozenset[str]
    tool_names: frozenset[str]
    window: Window
    deadline: datetime
    max_operations: int = MAX_OPERATIONS_PER_RUN
    max_tool_seconds: float = MAX_TOOL_SECONDS_PER_RUN

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (
                self.scope_id,
                self.subject_id,
                self.run_id,
                self.registry_revision,
                self.tool_registry_revision,
            )
        ):
            raise ToolContractError("INVALID_SCOPE")
        if self.subject_kind not in ("incident", "release_observation"):
            raise ToolContractError("INVALID_SUBJECT_KIND")
        if type(self.control_generation) is not int or self.control_generation < 0:
            raise ToolContractError("INVALID_CONTROL_GENERATION")
        if not isinstance(self.window, Window):
            raise ToolContractError("INVALID_SCOPE_WINDOW")
        if (
            not isinstance(self.deadline, datetime)
            or self.deadline.tzinfo is None
            or self.deadline.utcoffset() is None
        ):
            raise ToolContractError("INVALID_DEADLINE")
        if type(self.max_operations) is not int or not (
            0 < self.max_operations <= MAX_OPERATIONS_PER_RUN
        ):
            raise ToolContractError("OPERATION_BUDGET_OUT_OF_RANGE")
        if type(self.max_tool_seconds) not in (int, float) or not (
            0 < self.max_tool_seconds <= MAX_TOOL_SECONDS_PER_RUN
        ):
            raise ToolContractError("TIME_BUDGET_OUT_OF_RANGE")
        for names in (self.target_ids, self.tool_names):
            if not isinstance(names, frozenset) or any(
                not isinstance(name, str) or not name for name in names
            ):
                raise ToolContractError("INVALID_SCOPE_NAMES")
        object.__setattr__(self, "deadline", self.deadline.astimezone(timezone.utc))


@dataclass(frozen=True)
class ToolRequest:
    """One model-proposed tool call.

    ``step_id`` and ``tool_index`` come from the investigation loop and give the
    stable operation identity required by technical plan section 7. Everything
    else is untrusted model output and is typed as ``object`` on purpose.
    """

    step_id: str
    tool_index: int
    tool_name: object
    target_ref: object
    params: object = field(default_factory=dict)
    window: object = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.step_id, str)
            or not self.step_id
            or len(self.step_id) > _STEP_ID_MAX
            or type(self.tool_index) is not int
            or self.tool_index < 0
        ):
            raise ToolContractError("INVALID_REQUEST")

    @property
    def operation_id(self) -> str:
        return f"{self.step_id}-t{self.tool_index}"


@dataclass(frozen=True)
class TransportRequest:
    """Everything the transport needs, and nothing secret."""

    operation_id: str
    source: str
    verb: str
    endpoint: str
    selector: Mapping[str, str]
    params: Mapping[str, object]
    window: Window
    timeout_seconds: float
    max_result_bytes: int
    credential_ref: str
    read_only: bool = True

    def __post_init__(self) -> None:
        if self.read_only is not True or self.verb not in READ_ONLY_VERBS:
            raise ToolContractError("WRITE_CAPABILITY_FORBIDDEN")


@dataclass(frozen=True)
class TransportResponse:
    """Exact source bytes plus adapter-verified metadata.

    ``source_start_at``/``source_end_at`` bound the actual source timestamps
    represented by this response (a single instant is allowed). They are not
    the requested window, collection times, or a claim of gap-free coverage.
    Both default to None when unknown; one missing, naive, non-datetime or
    reversed bounds cause MALFORMED_RESULT before evidence registration.
    Consumers must not infer time-policy eligibility from unknown bounds or
    substitute the requested window/data_as_of. The adapter must derive these
    timestamps from source semantics, never model-supplied parameters.
    """

    body: bytes
    source_status: str | None = None
    data_as_of: datetime | None = None
    source_start_at: datetime | None = None
    source_end_at: datetime | None = None


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class ReadOnlyTransport(Protocol):
    """The only outbound seam. One method, and it only reads."""

    def fetch(self, request: TransportRequest) -> TransportResponse: ...


class EvidenceSink(Protocol):
    """Committed evidence store. Returns the stored evidence reference."""

    def register(self, record: EvidenceRecord) -> str: ...


class ControlAuthority(Protocol):
    """Controller-owned control state. Raises ``ControlUnavailable`` if unknown."""

    def snapshot(self, scope: QueryScope) -> ControlSnapshot: ...


@dataclass(frozen=True)
class ToolUsage:
    """Tool budget already consumed by a Run, across every execution attempt."""

    operations_used: int = 0
    tool_seconds_used: float = 0.0

    def __post_init__(self) -> None:
        if type(self.operations_used) is not int or self.operations_used < 0:
            raise ToolContractError("INVALID_USAGE")
        seconds = self.tool_seconds_used
        if (
            type(seconds) not in (int, float)
            or seconds != seconds
            or seconds in (float("inf"), float("-inf"))
            or seconds < 0
        ):
            raise ToolContractError("INVALID_USAGE")


@runtime_checkable
class ToolUsageLedger(Protocol):
    """Durable per-Run tool budget authority (technical plan section 13).

    The frozen per-Run ceilings are per *Run*, not per execution attempt, and
    a worker restart must not reset them. The executor therefore starts from
    ``usage()`` instead of zero and charges every dispatched operation back
    through ``charge``: once before the transport call, so the operation is
    counted even if the process dies mid-flight, and once after it with the
    measured wall time. Both calls carry the same ``operation_id``, so a
    repeated charge settles seconds instead of counting the operation twice.
    """

    def usage(self) -> ToolUsage: ...

    def charge(self, operation_id: str, seconds: float) -> None: ...


@dataclass(frozen=True)
class _Plan:
    registration: ToolRegistration
    target: RegisteredTarget
    window: Window
    params: Mapping[str, object]


class ReadOnlyToolExecutor:
    """Executes authorized read-only tool operations for exactly one scope."""

    def __init__(
        self,
        *,
        scope: QueryScope,
        tools: ToolRegistry,
        targets: TargetRegistry,
        transport: ReadOnlyTransport,
        evidence: EvidenceSink,
        control: ControlAuthority,
        clock: Clock,
        ledger: ToolUsageLedger,
    ) -> None:
        if not isinstance(scope, QueryScope):
            raise ToolContractError("INVALID_SCOPE")
        if not isinstance(tools, ToolRegistry) or not isinstance(
            targets, TargetRegistry
        ):
            raise ToolContractError("INVALID_REGISTRY")
        self._scope = scope
        self._tools = tools
        self._targets = targets
        self._transport = transport
        self._evidence = evidence
        self._control = control
        if not isinstance(clock, Clock):
            raise ToolContractError("INVALID_CLOCK")
        self._clock = clock
        if not isinstance(ledger, ToolUsageLedger):
            raise ToolContractError("INVALID_LEDGER")
        self._ledger = ledger
        try:
            usage = ledger.usage()
        except Exception:
            # Without the durable starting point the cap cannot be enforced
            # per Run; refuse to build rather than start from zero. Storage
            # error text stays inside the ledger.
            raise ToolContractError("LEDGER_UNAVAILABLE") from None
        if not isinstance(usage, ToolUsage):
            raise ToolContractError("INVALID_LEDGER")
        # Earlier attempts of this Run already spent part of the budget; the
        # ceilings in ``scope`` are per Run, so counting resumes from here.
        self._operations_used = usage.operations_used
        self._tool_seconds_used = float(usage.tool_seconds_used)

    @property
    def scope(self) -> QueryScope:
        return self._scope

    @property
    def operations_used(self) -> int:
        return self._operations_used

    @property
    def tool_seconds_used(self) -> float:
        return self._tool_seconds_used

    def execute(self, request: ToolRequest) -> ToolOutcome:
        """Run one model-proposed tool call and return exactly one outcome."""

        if not isinstance(request, ToolRequest):
            raise ToolContractError("INVALID_REQUEST")
        operation = ToolOperation(
            operation_id=request.operation_id,
            scope_id=self._scope.scope_id,
            subject_kind=self._scope.subject_kind,
            subject_id=self._scope.subject_id,
            run_id=self._scope.run_id,
            requested_tool=request.tool_name,
            requested_target=request.target_ref,
            registry_revision=self._targets.revision,
            tool_registry_revision=self._tools.revision,
            started_at=self._clock.now(),
        )
        resolved = self._authorize(operation, request)
        if isinstance(resolved, ToolOutcome):
            return resolved
        operation, plan = resolved
        budgeted = self._reserve(operation, plan)
        if isinstance(budgeted, ToolOutcome):
            return budgeted
        operation, timeout = budgeted
        return self._run(operation, plan, timeout)

    # -- authorization -------------------------------------------------

    def _authorize(
        self, operation: ToolOperation, request: ToolRequest
    ) -> tuple[ToolOperation, _Plan] | ToolOutcome:
        scope = self._scope
        if self._targets.revision != scope.registry_revision:
            # The authorization was written against a different target set.
            return self._refuse(operation, "denied", "TARGET_REGISTRY_CHANGED")
        if self._tools.revision != scope.tool_registry_revision:
            return self._refuse(operation, "denied", "TOOL_REGISTRY_CHANGED")
        registration = self._tools.lookup(request.tool_name)
        if registration is None:
            return self._refuse(operation, "denied", "TOOL_NOT_REGISTERED")
        if registration.name not in scope.tool_names:
            return self._refuse(operation, "denied", "TOOL_NOT_IN_SCOPE")
        operation = replace(
            operation,
            tool=registration.name,
            tool_version=registration.version,
            source=registration.source,
        )
        # Target identity comes from the registry only. A model-supplied
        # display name, endpoint or service label never selects a target.
        target = self._targets.resolve(request.target_ref)
        if target is None:
            return self._refuse(operation, "denied", "TARGET_NOT_REGISTERED")
        if target.target_id not in scope.target_ids:
            return self._refuse(operation, "denied", "TARGET_NOT_AUTHORIZED")
        if target.source != registration.source:
            return self._refuse(operation, "denied", "TARGET_SOURCE_MISMATCH")
        operation = replace(operation, target_id=target.target_id)
        window = Window.parse(request.window)
        if window is None:
            return self._refuse(operation, "error", "INVALID_PARAMS")
        # Authorization first, then the per-query volume limit: a window the
        # Run was never authorized to read is reported as out of scope even
        # when it is also too wide.
        if not scope.window.contains(window):
            return self._refuse(operation, "denied", "WINDOW_OUT_OF_SCOPE")
        if window.seconds > registration.max_window_seconds:
            return self._refuse(operation, "denied", "WINDOW_TOO_LARGE")
        operation = replace(operation, window=window)
        params, problem = _accept_params(registration, request.params)
        if params is None:
            return self._refuse(operation, _status_for(problem), problem)
        operation = replace(operation, query=canonical(params))
        return operation, _Plan(registration, target, window, params)

    def _reserve(
        self, operation: ToolOperation, plan: _Plan
    ) -> tuple[ToolOperation, float] | ToolOutcome:
        scope = self._scope
        control = self._read_control()
        if control is None:
            return self._refuse(operation, "denied", "CONTROL_UNAVAILABLE")
        if control.suspended:
            return self._refuse(operation, "denied", "SUSPENDED")
        if control.control_generation != scope.control_generation:
            return self._refuse(operation, "denied", "CONTROL_GENERATION_CHANGED")
        if self._operations_used >= scope.max_operations:
            return self._refuse(operation, "denied", "OPERATION_BUDGET_EXHAUSTED")
        # Re-read the trusted clock here rather than reusing ``started_at``.
        # The control lookup above is a Controller round trip of unbounded
        # duration, so an authorization that was still valid when the request
        # was accepted may have expired by the time the snapshot comes back.
        # Measuring from ``started_at`` would charge none of that lookup to the
        # deadline and still hand the transport a positive timeout, dispatching
        # a read after the authorization ended.
        authorized_at = self._clock.now()
        operation = replace(operation, authorized_at=authorized_at)
        remaining_deadline = (scope.deadline - authorized_at).total_seconds()
        if remaining_deadline <= 0:
            return self._refuse(operation, "denied", "DEADLINE_EXCEEDED")
        remaining_budget = scope.max_tool_seconds - self._tool_seconds_used
        if remaining_budget <= 0:
            return self._refuse(operation, "denied", "TIME_BUDGET_EXHAUSTED")
        # Section 8 requires the SDK timeout and the gateway bound together;
        # the authorization deadline and the remaining budget always win.
        timeout = min(
            plan.registration.request_timeout_seconds,
            remaining_deadline,
            remaining_budget,
        )
        operation = replace(
            operation,
            timeout_seconds=timeout,
            credential_ref=plan.target.credential_ref,
        )
        return operation, timeout

    def _read_control(self) -> ControlSnapshot | None:
        try:
            snapshot = self._control.snapshot(self._scope)
        except Exception:
            # Control text may be arbitrary; never re-raise it outward.
            return None
        return snapshot if isinstance(snapshot, ControlSnapshot) else None

    # -- execution -----------------------------------------------------

    def _run(
        self, operation: ToolOperation, plan: _Plan, timeout: float
    ) -> ToolOutcome:
        request = TransportRequest(
            operation_id=operation.operation_id,
            source=plan.registration.source,
            verb=plan.registration.verb,
            endpoint=plan.target.endpoint,
            selector=plan.target.selector,
            params=plan.params,
            window=plan.window,
            timeout_seconds=timeout,
            max_result_bytes=plan.registration.max_result_bytes,
            credential_ref=plan.target.credential_ref,
        )
        # Count the operation durably *before* the read goes out: if the
        # process dies while the request is in flight, the next attempt still
        # sees it as spent (section 13: unknown cost stays occupied). A budget
        # authority that cannot record it stops the call, as control does, and
        # an operation that was never recorded is not counted locally either.
        if not self._charge(operation.operation_id, 0.0):
            return self._refuse(operation, "denied", "CONTROL_UNAVAILABLE")
        self._operations_used += 1
        # The charge above is itself a ledger round trip of unbounded
        # duration, exactly like the Controller lookup ``_reserve()`` already
        # accounts for (see its comment). A slow ledger write can let the
        # authorization deadline pass, or a human suspend the investigation,
        # in the gap between the decision ``_reserve()`` made and the read
        # actually leaving this process. Re-check both -- control first, then
        # the deadline, the same order and priority the post-fetch re-check
        # below uses -- before dispatch: a query must never go out once its
        # authorization has lapsed. The charge already recorded above is not
        # refunded on a denial here; once billed it stays spent, the same
        # "unknown cost stays occupied" rule that justifies charging before
        # the read goes out at all.
        control = self._read_control()
        if control is None:
            return self._refuse(operation, "denied", "CONTROL_UNAVAILABLE")
        if control.suspended:
            return self._refuse(operation, "denied", "SUSPENDED")
        if control.control_generation != self._scope.control_generation:
            return self._refuse(operation, "denied", "CONTROL_GENERATION_CHANGED")
        now = self._clock.now()
        if now >= self._scope.deadline:
            return self._refuse(operation, "denied", "DEADLINE_EXCEEDED")
        # The control read and the charge above may themselves have consumed
        # real time without crossing the deadline outright -- rejecting only
        # when it has *fully* passed would still hand the transport the
        # stale, larger timeout ``_reserve()`` computed, letting the read
        # stay outstanding against the source well past the authorization
        # window even though this very check passed. Shrink it to whatever
        # authorization actually remains right now (bot review finding).
        remaining = (self._scope.deadline - now).total_seconds()
        if remaining < timeout:
            timeout = remaining
            request = replace(request, timeout_seconds=timeout)
        # Mark dispatch only now, right before the transport is actually
        # called -- not earlier, when ``_reserve()`` merely computed a
        # timeout. Every ``_refuse()`` return above this line therefore
        # correctly reports ``sent: false`` in the audit record.
        operation = replace(operation, dispatched=True, timeout_seconds=timeout)
        started = self._clock.monotonic()
        failure: tuple[ToolStatus, str, SourceContact] | None = None
        response: object = None
        try:
            response = self._transport.fetch(request)
        except TransportTimeout:
            failure = ("timeout", "TOOL_TIMEOUT", "possible")
        except TransportResultTooLarge:
            failure = ("error", "RESULT_TOO_LARGE", "confirmed")
        except TransportUnavailable:
            failure = ("error", "SOURCE_UNAVAILABLE", "possible")
        except Exception:
            # Vendor exception text may carry payload or credential material.
            failure = ("error", "SOURCE_ERROR", "possible")
        elapsed = max(0.0, self._clock.monotonic() - started)
        self._tool_seconds_used += elapsed
        operation = replace(
            operation, finished_at=self._clock.now(), elapsed_seconds=elapsed
        )
        # Settle the measured wall time. A result whose cost could not be
        # recorded is not adopted: the same fail-closed rule as an evidence
        # store that cannot commit (``EVIDENCE_NOT_COMMITTED``). The durable
        # record then keeps this operation at its reserved 0 s (the count is
        # kept); the attempt-local total still carries the measured time.
        if not self._charge(operation.operation_id, elapsed):
            return self._refuse(operation, "denied", "CONTROL_UNAVAILABLE", "confirmed")
        if failure is not None:
            return self._refuse(operation, *failure)
        if elapsed > timeout:
            # A result that arrives after its deadline must not update state.
            return self._refuse(operation, "timeout", "GATEWAY_TIMEOUT", "confirmed")
        if not isinstance(response, TransportResponse) or not isinstance(
            response.body, bytes
        ):
            return self._refuse(operation, "error", "MALFORMED_RESULT", "confirmed")
        if len(response.body) > plan.registration.max_result_bytes:
            return self._refuse(operation, "error", "RESULT_TOO_LARGE", "confirmed")
        if response.source_status is not None:
            # A source error is not an observation of the target's state, so it
            # is classified onto a fixed reason and the operation record keeps
            # the audit trail; the error body itself is not registered as
            # evidence and its vendor text never reaches model context.
            source_reason = plan.registration.classify(str(response.source_status))
            return self._refuse(operation, "error", source_reason, "confirmed")
        rows, payload = _result_rows(plan.registration, response.body)
        if rows is None:
            return self._refuse(operation, "error", "MALFORMED_RESULT", "confirmed")
        if response.data_as_of is not None and (
            not isinstance(response.data_as_of, datetime)
            or response.data_as_of.tzinfo is None
            or response.data_as_of.utcoffset() is None
        ):
            return self._refuse(operation, "error", "MALFORMED_RESULT", "confirmed")
        source_start_at = response.source_start_at
        source_end_at = response.source_end_at
        if (source_start_at is None) != (source_end_at is None) or (
            source_start_at is not None
            and (
                not isinstance(source_start_at, datetime)
                or source_start_at.tzinfo is None
                or source_start_at.utcoffset() is None
                or not isinstance(source_end_at, datetime)
                or source_end_at.tzinfo is None
                or source_end_at.utcoffset() is None
                or source_start_at > source_end_at
            )
        ):
            return self._refuse(operation, "error", "MALFORMED_RESULT", "confirmed")
        status: ToolStatus = "ok" if rows else "no_data"
        reason: str | None = None if rows else "NO_DATA"
        # Re-check the control state and the authorization deadline: a
        # suspension or an expiry that took effect while the request was in
        # flight invalidates the result, which then survives as history only
        # (technical plan sections 4 and 8).
        #
        # Control is read first, in the same order as ``_reserve()``, so that a
        # human suspension is still reported as ``SUSPENDED`` when the deadline
        # has also passed. ``PRODUCT-CONSTRAINTS.md`` ("Runtime and human
        # control requirements") forbids a late completion from erasing a newer
        # human decision, and letting the deadline short-circuit the control
        # read would leave no trace of the suspension in the outcome or the
        # audit record.
        #
        # The deadline test uses the instant the read *arrived*, not the
        # instant adoption finishes. A read that completed inside the window
        # was authorized, and keying it on a later reading would discard
        # lawfully obtained evidence whenever the gateway's own control or
        # evidence store happened to be slow. That choice has a cost, and it is
        # deliberate rather than overlooked: when the control re-read itself
        # crosses the deadline, an observation fetched inside the window is
        # still adopted after the deadline has passed. What this check
        # guarantees is that nothing is adopted whose *read* completed outside
        # the authorization window -- not that adoption finishes inside it.
        assert operation.finished_at is not None
        control = self._read_control()
        if control is None:
            invalid = "CONTROL_UNAVAILABLE"
        elif control.suspended:
            invalid = "SUSPENDED"
        elif control.control_generation != self._scope.control_generation:
            invalid = "CONTROL_GENERATION_CHANGED"
        elif operation.finished_at >= self._scope.deadline:
            invalid = "DEADLINE_EXCEEDED"
        else:
            invalid = ""
        record = self._record(
            operation,
            plan,
            response,
            payload,
            rows,
            status if not invalid else "denied",
            adopted=not invalid,
        )
        if invalid:
            registered = self._register(
                record
            )  # history only; adoption already refused
            return self._refuse(
                operation,
                "denied",
                invalid,
                "confirmed",
                evidence=record if registered else None,
            )
        if not self._register(record):
            # Evidence must be committed before it may be consumed.
            return self._refuse(
                operation, "error", "EVIDENCE_NOT_COMMITTED", "confirmed"
            )
        return ToolOutcome(
            operation=operation,
            status=status,
            reason=reason,
            source_contact="confirmed",
            # A detached copy, not the same object as ``record.view``: a
            # caller mutating the model-facing view must never be able to
            # change the evidence already committed under ``view_sha256``,
            # or a sink that retained the record would see its "committed"
            # evidence drift out from under it (bot review finding).
            model_view=deepcopy(record.view),
            evidence=record,
        )

    def _charge(self, operation_id: str, seconds: float) -> bool:
        try:
            self._ledger.charge(operation_id, seconds)
        except Exception:
            # Ledger error text may carry storage detail; never re-raise it.
            return False
        return True

    def _register(self, record: EvidenceRecord) -> bool:
        try:
            reference = self._evidence.register(record)
        except Exception:
            return False
        return reference == record.evidence_id

    # -- evidence ------------------------------------------------------

    def _record(
        self,
        operation: ToolOperation,
        plan: _Plan,
        response: TransportResponse,
        payload: object,
        rows: Sequence[object],
        status: ToolStatus,
        *,
        adopted: bool,
    ) -> EvidenceRecord:
        registration = plan.registration
        observed_at = operation.finished_at or operation.started_at
        assert observed_at is not None
        incomplete = _incomplete(registration, payload)
        if adopted:
            kept, omitted_rows, omitted_bytes = _fit_rows(
                rows, registration.max_view_bytes
            )
        else:
            # An invalidated observation survives as history; none of its rows
            # are handed to the model, and the view says so.
            kept, omitted_rows, omitted_bytes = _fit_rows(rows, 0)
        data_as_of = response.data_as_of
        source_start_at = response.source_start_at
        source_end_at = response.source_end_at
        freshness = (
            None if data_as_of is None else (observed_at - data_as_of).total_seconds()
        )
        view: dict[str, object] = {
            "evidence_id": operation.operation_id,
            "operation_id": operation.operation_id,
            "trust": "untrusted-evidence",
            "status": status,
            "adopted": adopted,
            "tool": registration.name,
            "tool_version": registration.version,
            "source": registration.source,
            "target_id": plan.target.target_id,
            "registry_revision": operation.registry_revision,
            "tool_registry_revision": operation.tool_registry_revision,
            "projection_revision": PROJECTION_REVISION,
            "query": dict(plan.params),
            "window": plan.window.as_json(),
            "observed_at": observed_at.isoformat(),
            "data_as_of": None if data_as_of is None else data_as_of.isoformat(),
            "source_start_at": None
            if source_start_at is None
            else source_start_at.isoformat(),
            "source_end_at": None
            if source_end_at is None
            else source_end_at.isoformat(),
            "freshness_seconds": freshness,
            "result_count": len(rows),
            "returned_count": len(kept),
            "incomplete": incomplete,
            "truncated": bool(omitted_rows),
            "omitted_rows": omitted_rows,
            "omitted_bytes": omitted_bytes,
            "content": None if not adopted else kept,
        }
        return EvidenceRecord(
            evidence_id=operation.operation_id,
            operation=operation,
            status=status,
            raw=response.body,
            raw_sha256=sha256(response.body).hexdigest(),
            view=view,
            view_sha256=canonical_hash(view),
            projection_revision=PROJECTION_REVISION,
            observed_at=observed_at,
            data_as_of=data_as_of,
            source_start_at=source_start_at,
            source_end_at=source_end_at,
            result_count=len(rows),
            incomplete=incomplete,
            truncated=bool(omitted_rows),
            omitted_rows=omitted_rows,
            omitted_bytes=omitted_bytes,
            adopted=adopted,
        )

    # -- refusals ------------------------------------------------------

    def _refuse(
        self,
        operation: ToolOperation,
        status: ToolStatus,
        reason: str,
        contact: SourceContact = "none",
        *,
        evidence: EvidenceRecord | None = None,
    ) -> ToolOutcome:
        """Build a refusal outcome whose model view carries no source content."""

        view: dict[str, object] = {
            "operation_id": operation.operation_id,
            "trust": "gateway",
            "status": status,
            "reason": reason,
            "source_contact": contact,
            "tool": operation.tool,
            "target_id": operation.target_id,
            "window": operation.window.as_json() if operation.window else None,
            "requested_at": operation.started_at.isoformat()
            if operation.started_at
            else None,
            "content": None,
        }
        return ToolOutcome(
            operation=operation,
            status=status,
            reason=reason,
            source_contact=contact,
            model_view=view,
            evidence=evidence,
        )


def _status_for(problem: str) -> ToolStatus:
    return "denied" if problem == "PARAM_NOT_ALLOWED" else "error"


def _accept_params(
    registration: ToolRegistration, params: object
) -> tuple[Mapping[str, object] | None, str]:
    """Accept only declared parameters with declared types."""

    if not isinstance(params, Mapping):
        return None, "INVALID_PARAMS"
    accepted: dict[str, object] = {}
    for key, value in params.items():
        spec = registration.parameters.get(key) if isinstance(key, str) else None
        if spec is None:
            # Anything undeclared is an attempt to widen the query surface,
            # including headers or credentials the model tried to inject.
            return None, "PARAM_NOT_ALLOWED"
        if not spec.accepts(value):
            return None, "INVALID_PARAMS"
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except ValueError:
                # A `\uD800`-style lone surrogate is a valid Python str (the
                # model can supply one in a tool-call argument) but is not
                # valid UTF-8: left unchecked here it reaches _record()'s
                # view["query"] = dict(plan.params) and crashes
                # canonical_hash(view) with an uncaught UnicodeEncodeError --
                # the same failure class as the source-response-row finding,
                # but via the params input instead (independent review
                # finding on top of that fix).
                return None, "INVALID_PARAMS"
        accepted[key] = value
    if any(
        spec.required and key not in accepted
        for key, spec in registration.parameters.items()
    ):
        return None, "INVALID_PARAMS"
    return accepted, ""


def _result_rows(
    registration: ToolRegistration, body: bytes
) -> tuple[list[object] | None, object]:
    """Navigate the declared result path; ``None`` rows means malformed."""

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        # ValueError also covers json.JSONDecodeError (a subclass) and the
        # decoder's own digit-count guard on an oversized integer literal;
        # RecursionError covers a body nested deep enough to exceed the
        # interpreter's recursion limit. An untrusted, otherwise
        # size-compliant source body must not be able to crash execute()
        # outright by tripping either one (bot review finding).
        return None, None
    cursor: object = payload
    for step in registration.result_path:
        if not isinstance(cursor, Mapping) or step not in cursor:
            return None, payload
        cursor = cursor[step]
    if not isinstance(cursor, list):
        return None, payload
    try:
        for row in cursor:
            canonical(row).encode("utf-8")
    except (ValueError, RecursionError):
        # A `\uD800`-style escape decodes into a Python str holding a lone
        # surrogate codepoint -- json.loads accepts it without error -- but
        # re-encoding it to UTF-8 for canonicalization (here, and later in
        # _fit_rows()) raises UnicodeEncodeError, a ValueError subclass. This
        # is a fresh failure mode past decoding succeeding, not a duplicate
        # of the decoder-limit check above (bot review finding).
        return None, payload
    return cursor, payload


def _incomplete(registration: ToolRegistration, payload: object) -> bool:
    marker = registration.incomplete_marker
    if marker is None or not isinstance(payload, Mapping):
        return False
    return bool(payload.get(marker))


def _fit_rows(rows: Sequence[object], budget: int) -> tuple[list[object], int, int]:
    """Keep the leading rows that fit the view budget; report what was cut.

    The budget bounds ``canonical(kept)`` exactly, so ``max_view_bytes`` is a
    real ceiling on what one observation can add to model context.
    """

    kept: list[object] = []
    used = EMPTY_VIEW_BYTES  # the enclosing brackets of the JSON array
    for index, row in enumerate(rows):
        size = len(canonical(row).encode("utf-8")) + (1 if kept else 0)
        if used + size > budget:
            omitted = sum(len(canonical(rest).encode("utf-8")) for rest in rows[index:])
            return kept, len(rows) - index, omitted
        kept.append(row)
        used += size
    return kept, 0, 0
