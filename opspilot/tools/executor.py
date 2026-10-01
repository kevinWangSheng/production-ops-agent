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
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from hashlib import sha256
from types import MappingProxyType
from typing import Protocol, cast, runtime_checkable
from uuid import UUID, uuid4

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
    READ_ONLY_VERBS,
    RegisteredTarget,
    TargetRegistry,
    ToolContractError,
    ToolRegistration,
    ToolRegistry,
    canonical,
    canonical_hash,
)
from .tokens import TokenCounter, default_counter

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
    "ToolBudgetExhausted",
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


class ToolTimeBudgetExhausted(Exception):
    """The ledger refused a new dispatch: the Run's durable seconds are spent.

    The sibling of :class:`ToolBudgetExhausted` for the cumulative time
    ceiling, which is enforced in the same atomic UPDATE. It is a distinct
    type so the executor can report ``TIME_BUDGET_EXHAUSTED`` -- the same
    authoritative reason its in-process pre-check uses -- rather than the
    operation-count reason or a generic ledger failure.
    """


class ToolControlDenied(Exception):
    """The ledger refused the write because control revoked the Run.

    A ``ToolUsageLedger`` implementation raises this specific, fixed-code
    exception when the authoritative store rejects the charge on control
    grounds (a human pause or cancel, a newer control generation, an expired
    lease). Collapsing it into the generic opaque failure made the executor
    return before its own control re-read, so a read that *did* reach the
    source was dropped from the evidence audit and reported as
    ``CONTROL_UNAVAILABLE`` instead of the authoritative human decision (bot
    review finding).
    """


class ToolBudgetExhausted(Exception):
    """The ledger refused a new operation: the Run's durable cap is spent.

    A ``ToolUsageLedger`` implementation raises this specific, fixed-code
    exception for exactly this one condition so the executor can propagate
    ``OPERATION_BUDGET_EXHAUSTED`` -- the same authoritative reason the
    in-process pre-check already reports -- instead of collapsing it into
    the generic ``CONTROL_UNAVAILABLE`` every other ledger failure maps to
    (bot review finding).
    """


@dataclass(frozen=True)
class ControlSnapshot:
    """Controller-owned control state at one instant.

    The field types are enforced, not assumed: ``bool`` is an ``int`` subclass,
    so a controller adapter returning ``control_generation=True`` compared
    equal to a scope generation of ``1`` and the executor dispatched, while
    ``suspended=0`` was read as an authoritative "not suspended". Malformed
    controller data must fail closed (the executor maps a snapshot it cannot
    obtain to ``CONTROL_UNAVAILABLE``), never open (bot review finding).
    """

    control_generation: int
    # 全局与目标两层暂停各有自己的版本号（domain 层 ScopeVersions 早已如此建模）。
    # 只带主体版本时，「全局暂停后又解除」这一序列无法被发现：布尔位已经归零，
    # 主体版本没变，一份暂停前的旧 scope 于是继续匹配——而 C3 第 4 节 :114 要求
    # 解除暂停后「新尝试使用当前所有控制版本」，旧授权不自动恢复（bot review 发现）。
    # 必填，不给默认值：默认 0 会让一个尚未填这两个字段的 controller 适配器在
    # 「全局/目标暂停后又解除」时与旧 scope 恰好相等而悄悄放行——正是这两个字段
    # 要挡住的那条路径。与 max_operations / dispatch_id 同一理由：不完整的接线
    # 必须 fail closed（bot review 发现）。
    global_suspension_generation: int
    target_suspension_generation: int
    suspended: bool = False

    def __post_init__(self) -> None:
        for name in (
            "control_generation",
            "global_suspension_generation",
            "target_suspension_generation",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ToolContractError("INVALID_CONTROL_SNAPSHOT")
        if type(self.suspended) is not bool:
            raise ToolContractError("INVALID_CONTROL_SNAPSHOT")


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
    # 与 ControlSnapshot 相同的三个版本，同样必填：授权在预留、派发与采纳三处
    # 都按当前全局/目标/主体版本复核（C3 第 4 节 :112）。
    global_suspension_generation: int
    target_suspension_generation: int
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
        for name in (
            "control_generation",
            "global_suspension_generation",
            "target_suspension_generation",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ToolContractError("INVALID_CONTROL_GENERATION")
        if not isinstance(self.window, Window):
            raise ToolContractError("INVALID_SCOPE_WINDOW")
        if (
            not isinstance(self.deadline, datetime)
            or self.deadline.tzinfo is None
            or self.deadline.utcoffset() is None
        ):
            raise ToolContractError("INVALID_DEADLINE")
        # M1-01 (2026-09-28 user decision, L2): these two fields are no
        # longer a ceiling the executor or the durable ledger enforces (see
        # ``ReadOnlyToolExecutor._reserve()`` and
        # ``opspilot.persistence.DurableStore.charge_tool``) -- only basic
        # positivity is worth checking here now, not an upper bound tied to
        # the old frozen constants, which would otherwise fail-closed refuse
        # a value that is not actually a ceiling violation of anything.
        if type(self.max_operations) is not int or self.max_operations < 1:
            raise ToolContractError("OPERATION_BUDGET_OUT_OF_RANGE")
        seconds = self.max_tool_seconds
        if (
            type(seconds) not in (int, float)
            or seconds != seconds  # NaN
            or seconds in (float("inf"), float("-inf"))
            or seconds <= 0
        ):
            raise ToolContractError("TIME_BUDGET_OUT_OF_RANGE")
        for names in (self.target_ids, self.tool_names):
            if not isinstance(names, frozenset) or any(
                not isinstance(name, str) or not name for name in names
            ):
                raise ToolContractError("INVALID_SCOPE_NAMES")
        try:
            normalized = self.deadline.astimezone(timezone.utc)
        except (OverflowError, OSError, ValueError) as exc:
            # `fromisoformat` accepts aware bounds that cannot be normalised
            # (`0001-01-01T00:00:00+14:00`), and the raw OverflowError escaped
            # this module's fixed-code boundary -- `Window.parse()` already
            # treats the same timestamp class as invalid input (bot review
            # finding).
            raise ToolContractError("INVALID_DEADLINE") from exc
        object.__setattr__(self, "deadline", normalized)


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
    # The registered tool this read serves, resolved by the gateway (never
    # the model's ``requested_tool``). A profile whose one target is read
    # through more than one backend (the OTel Demo: Prometheus and Jaeger)
    # routes on it; ``params`` alone cannot say which tool a call is for.
    tool: str = ""

    def __post_init__(self) -> None:
        if self.read_only is not True or self.verb not in READ_ONLY_VERBS:
            raise ToolContractError("WRITE_CAPABILITY_FORBIDDEN")
        if not isinstance(self.tool, str):
            raise ToolContractError("INVALID_REQUEST")


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

    ``query_window`` is the absolute window the adapter actually queried,
    when that can differ from the top-level authorized ``TransportRequest
    .window`` the executor bounds every call to (batch B, B2 review
    disposition P1: a profile whose tool lets the model pick a narrower
    window inside a wider authorized frame -- ``opspilot.tools.otel_demo``'s
    ``start``/``end`` parameters are the first such case). ``None`` means the
    adapter queried the full authorized window verbatim (every profile
    before B2, and any call with no narrower selection); the executor then
    falls back to ``TransportRequest.window`` for the view's own ``window``
    field, unchanged from before this field existed. When given, must be
    contained in ``TransportRequest.window``; a wider or disjoint claim is
    MALFORMED_RESULT, the same fail-closed treatment as an over-long
    ``lookback_seconds``.

    ``lookback_seconds`` is how far back each returned point reads (the
    longest PromQL range selector), derived by the adapter from the query it
    sent; ``None`` when the adapter does not report it. The adapter starts
    its evaluation that far after the *query* window's start (``query_window``
    when given, else the full authorized window), so the earliest instant any
    point reads is that query window's start itself, shown to the model as
    ``lookback_start_at``. It is not part of ``source_start_at``: that
    interval stays the returned samples the scope and time-policy checks
    bound (v4 acceptance packet, normal-1 review P2-3; v4 rerun P2).

    ``sent`` is ``False`` when the transport refused the request before
    anything left the process (a declared parameter it cannot turn into a
    read the source may answer) and reports the refusal as a fixed
    ``source_status`` code for the registration to classify. The executor
    then records ``sent: false`` and ``source_contact: none`` instead of a
    confirmed contact the source never had (independent review finding).

    ``row_unit`` names what one result row is (``"spans"``) and
    ``backend_rows_returned`` how many such rows the source returned before
    the adapter sampled; the view then carries
    ``backend_<unit>_returned``, ``<unit>_shown`` (rows in the view) and
    ``<unit>_omitted`` (the difference), so a count always states its unit
    (round 2 rule B). ``view_fields`` are further adapter-verified,
    tool-specific view fields copied onto the view verbatim; a key that
    collides with a generic view field is MALFORMED_RESULT.
    """

    body: bytes
    source_status: str | None = None
    data_as_of: datetime | None = None
    source_start_at: datetime | None = None
    source_end_at: datetime | None = None
    sent: bool = True
    lookback_seconds: int | None = None
    row_unit: str | None = None
    backend_rows_returned: int | None = None
    view_fields: Mapping[str, object] | None = None
    query_window: Window | None = None


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class ReadOnlyTransport(Protocol):
    """The only outbound seam. One method, and it only reads."""

    def fetch(self, request: TransportRequest) -> TransportResponse: ...


class EvidenceSink(Protocol):
    """Committed evidence store. Returns the stored evidence reference.

    **An implementation must validate the control generation inside the same
    transaction that commits.** The executor's pre-commit control read is a
    fast path, not the fence: a human pause or cancel landing between that
    read and this call would otherwise commit a record still marked
    ``adopted`` (bot review finding). The durable path already works this way
    -- ``DurableStore.commit_tool`` re-reads owner/epoch/generation under the
    row lock and diverts a result whose generation moved to
    ``_late_result``, history only -- and this protocol states the obligation
    so an implementation cannot satisfy the type and drop the guarantee.

    Rejecting is safe here: ``register`` may raise or return a reference that
    is not ``record.evidence_id``, and the executor then fails closed with
    ``EVIDENCE_NOT_COMMITTED`` without handing the content to the model.

    **Raise ``ToolControlDenied`` when the rejection is a control decision.**
    Any other exception is an opaque failure the executor reports as
    ``EVIDENCE_NOT_COMMITTED``; that generic mapping would otherwise hide the
    human decision the commit-time fence exists to enforce, which is exactly
    the outcome this protocol's obligation is meant to produce.
    """

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
    measured wall time. Both calls carry the same ``dispatch_id``, so a
    repeated charge settles seconds instead of counting the operation twice.
    ``operation_id`` deliberately does *not* key the charge: it is stable
    across deliveries and attempts (section 7), so a duplicate delivery or a
    same-epoch retry is a second *real* read that section 13 requires to be
    charged again ("重试计入次数和费用") rather than folded into the first.
    ``charge`` raises ``ToolBudgetExhausted`` specifically when it refuses a
    *new* operation because the Run's durable cap is spent; any other
    failure is an opaque, unclassified error the caller fails closed on.
    """

    def usage(self) -> ToolUsage: ...

    def charge(
        self, operation_id: str, seconds: float, *, dispatch_id: UUID
    ) -> None: ...


@dataclass(frozen=True)
class _Plan:
    registration: ToolRegistration
    target: RegisteredTarget
    window: Window
    params: Mapping[str, object]


class _ViewTooLarge(Exception):
    """An adopted view counted over its registration's ``max_view_tokens``."""

    def __init__(
        self,
        tokens: int,
        limit: int,
        query: Mapping[str, object],
        window: Mapping[str, object],
    ) -> None:
        super().__init__("RESULT_TOO_LARGE")
        self.tokens = tokens
        self.limit = limit
        self.query = query
        self.window = window


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
        token_counter: TokenCounter | None = None,
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
        # The model-view limit is metered in real DeepSeek tokens (M1-01, A).
        # ``None`` loads the vendored tokenizer now and fails closed
        # (``TOKENIZER_UNAVAILABLE``): a worker must not start and then find
        # out mid-Run, or quietly fall back to a byte estimate. A given
        # counter (a deterministic test double) touches no tokenizer file.
        self._count_tokens: TokenCounter = (
            default_counter() if token_counter is None else token_counter
        )
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
        try:
            query = canonical(params)
        except (ValueError, OverflowError):
            # A declared integer parameter can still be unencodable: Python
            # refuses ``str()`` past ``sys.get_int_max_str_digits()`` (4300 on
            # the supported runtime), so ``10**4300`` passes
            # ``ParameterSpec.accepts`` and then raises inside ``canonical``.
            # Model-proposed input must never escape ``execute()`` as an
            # exception and abort the investigation loop (bot review finding).
            return self._refuse(operation, "error", "INVALID_PARAMS")
        operation = replace(operation, query=query)
        return operation, _Plan(registration, target, window, params)

    def _reserve(
        self, operation: ToolOperation, plan: _Plan
    ) -> tuple[ToolOperation, float] | ToolOutcome:
        scope = self._scope
        decision = self._control_decision()
        if decision:
            return self._refuse(operation, "denied", decision)
        # M1-01 (2026-09-28 user decision, docs/tasks/2026-09-28-m1-01-loop-limits.md
        # L2): no per-Run ceiling on operation count or cumulative tool time --
        # the loop's single anti-loop ceiling is the model-request count (L1).
        # ``scope.max_operations``/``scope.max_tool_seconds`` still bound what a
        # ``QueryScope`` may declare (__post_init__), but this method no longer
        # refuses a dispatch on either one. The per-call timeout, result-byte
        # ceiling and view truncation are untouched (they guard one call, not
        # investigation length).
        #
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
        # Section 8 requires the SDK timeout and the gateway bound together;
        # the authorization deadline always wins.
        timeout = min(plan.registration.request_timeout_seconds, remaining_deadline)
        operation = replace(
            operation,
            timeout_seconds=timeout,
            credential_ref=plan.target.credential_ref,
        )
        return operation, timeout

    def _generations_changed(self, control: ControlSnapshot) -> bool:
        """Whether any control version this scope was authorized under moved.

        All three are compared, at every checkpoint: a global or target
        suspension that is activated and then released leaves the boolean
        cleared and the subject version untouched, so comparing only the
        subject version let a pre-suspension authorization keep issuing
        queries (bot review finding). C3 section 4 requires a new attempt to
        use *all* current control versions after a release.
        """
        scope = self._scope
        return (
            control.control_generation != scope.control_generation
            or control.global_suspension_generation
            != scope.global_suspension_generation
            or control.target_suspension_generation
            != scope.target_suspension_generation
        )

    def _control_decision(self) -> str:
        """The human/控制 decision that invalidates this operation, or "".

        Deliberately excludes the deadline: a lapsed authorization and a
        human decision are different facts, and only the latter outranks a
        transport failure's own classification (see ``_run``).
        """
        control = self._read_control()
        if control is None:
            return "CONTROL_UNAVAILABLE"
        if control.suspended:
            return "SUSPENDED"
        if self._generations_changed(control):
            return "CONTROL_GENERATION_CHANGED"
        return ""

    def _control_invalid(self, deadline_at: datetime) -> str:
        """The authoritative reason this operation may not proceed, or "".

        Control is read before the deadline is tested, the priority
        ``PRODUCT-CONSTRAINTS.md`` requires: a human decision must still be
        reported as such when the authorization has also lapsed. Shared by
        both ledger-denial paths and the post-fetch re-check so the three
        cannot drift apart.
        """
        decision = self._control_decision()
        if decision:
            return decision
        if deadline_at >= self._scope.deadline:
            return "DEADLINE_EXCEEDED"
        return ""

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
        # One identity for this read, reused by the pre-dispatch charge and
        # the settlement below so the two are the same dispatch. A duplicate
        # delivery of the same tool call gets its own id and is charged
        # separately: it really does issue a second query (section 13).
        dispatch_id = uuid4()
        request = TransportRequest(
            operation_id=operation.operation_id,
            source=plan.registration.source,
            verb=plan.registration.verb,
            endpoint=plan.target.endpoint,
            selector=plan.target.selector,
            # A detached, immutable copy: a transport adapter that normalizes
            # or otherwise mutates ``request.params`` in place must never be
            # able to reach ``plan.params``, which ``_record()`` reads again
            # afterward to build the evidence view (bot review finding).
            params=MappingProxyType(dict(plan.params)),
            window=plan.window,
            timeout_seconds=timeout,
            max_result_bytes=plan.registration.max_result_bytes,
            credential_ref=plan.target.credential_ref,
            tool=plan.registration.name,
        )
        # Count the operation durably *before* the read goes out: if the
        # process dies while the request is in flight, the next attempt still
        # sees it as spent (section 13: unknown cost stays occupied). A budget
        # authority that cannot record it stops the call, as control does, and
        # an operation that was never recorded is not counted locally either.
        try:
            # Reserve the full authorized duration, not 0.0: the reservation is
            # what keeps two executors from both starting a read the Run cannot
            # afford (bot review finding).
            charged = self._charge(operation.operation_id, timeout, dispatch_id)
        except ToolBudgetExhausted:
            # The durable cap is the authoritative one; a caller must see
            # this as the same denial the in-process pre-check reports, not
            # a transient control failure it might retry (bot review
            # finding).
            return self._refuse(operation, "denied", "OPERATION_BUDGET_EXHAUSTED")
        except ToolTimeBudgetExhausted:
            return self._refuse(operation, "denied", "TIME_BUDGET_EXHAUSTED")
        except ToolControlDenied:
            # A control race between ``_reserve()`` and this charge is normal,
            # not exceptional: the store refused because a human decision or a
            # newer generation landed in between. ``_charge()`` re-raises this
            # signal for the settlement path, so it must be caught here too --
            # otherwise it escapes ``execute()`` and aborts the investigation
            # loop (bot review finding). Nothing was dispatched yet, so there
            # is no observation to keep; name the authoritative reason.
            return self._refuse(
                operation,
                "denied",
                self._control_invalid(self._clock.now()) or "CONTROL_UNAVAILABLE",
            )
        if not charged:
            # A generic ledger failure before dispatch goes through the same
            # precedence as the control-denied branch above: a human decision
            # taken since ``_reserve()`` is reported as such, not hidden
            # behind the storage outage.
            return self._refuse(
                operation,
                "denied",
                self._control_invalid(self._clock.now()) or "CONTROL_UNAVAILABLE",
            )
        self._operations_used += 1

        # The charge above is itself a ledger round trip of unbounded
        # duration, exactly like the Controller lookup ``_reserve()`` already
        # accounts for (see its comment). A slow ledger write can let the
        # authorization deadline pass, or a human suspend the investigation,
        # in the gap between the decision ``_reserve()`` made and the read
        # actually leaving this process. Re-check both -- control first, then
        # the deadline, the same order and priority the post-fetch re-check
        # below uses -- before dispatch: a query must never go out once its
        # authorization has lapsed.
        #
        # A denial here settles the reservation at zero. "Unknown cost stays
        # occupied" is about a read whose cost nobody can know -- one still in
        # flight, or one whose process died; here the read provably never left,
        # and the audit record says so with ``sent: false``. Holding the full
        # reserved timeout anyway would let a control race permanently consume
        # the Run's time budget for queries that never happened (bot review
        # finding; the reservation was introduced two rounds ago and this
        # comment still described the older behaviour, where the pre-dispatch
        # charge carried 0.0 seconds and there was nothing to give back).
        #
        # The operation *count* deliberately stays: it is recorded before
        # dispatch precisely so that a crash in this gap cannot hide an attempt,
        # and releasing it would require telling "denied before dispatch" apart
        # from "died before dispatch" -- the distinction the durable pre-charge
        # exists to avoid needing.
        def deny_before_dispatch(reason: str) -> ToolOutcome:
            self._release(operation.operation_id, dispatch_id)
            return self._refuse(operation, "denied", reason)

        decision = self._control_decision()
        if decision:
            return deny_before_dispatch(decision)
        now = self._clock.now()
        # This is the authorization check immediately before dispatch, which is
        # what ``authorized_at`` documents itself to be. Leaving the earlier
        # ``_reserve()`` reading in place made the audit record understate when
        # authorization was last verified whenever the ledger charge or this
        # control snapshot was slow, and nothing else in the record can be used
        # to derive it (bot review finding).
        operation = replace(operation, authorized_at=now)
        if now >= self._scope.deadline:
            return deny_before_dispatch("DEADLINE_EXCEEDED")
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
        # Settling an already-counted operation is never subject to the
        # operation cap (the durable WHERE clause only gates a *new*
        # operation), so a real ledger cannot raise ToolBudgetExhausted
        # here -- but _charge() re-raises it unconditionally, so this is
        # still handled defensively rather than left to escape execute().
        # Whether the source was actually reached is decided by the fetch, not
        # by what happened afterwards. When the fetch already classified
        # contact as merely ``possible`` (a timeout, an unavailable source),
        # a failure to settle the cost must report that same uncertainty:
        # reporting ``confirmed`` here would let a transient PostgreSQL error
        # manufacture a false audit fact (bot review finding).
        # A transport that decided *not* to send (``sent=False`` with a fixed
        # refusal status) never contacted the source: the audit record must
        # say so rather than claim a dispatch and a confirmed contact.
        refused_before_send = (
            failure is None
            and isinstance(response, TransportResponse)
            and response.sent is False
            and response.source_status is not None
        )
        if refused_before_send:
            operation = replace(operation, dispatched=False)
        contact: SourceContact = (
            failure[2]
            if failure is not None
            else ("none" if refused_before_send else "confirmed")
        )
        settlement_denied = False

        def refuse_after_fetch(
            status: ToolStatus,
            reason: str,
            detail: Mapping[str, object] | None = None,
        ) -> ToolOutcome:
            """Refuse a dispatched read, human decisions first.

            Every refusal below this point is about a read that already went
            out, so a pause or cancel taken while it was in flight outranks
            whatever the response itself turned out to be: classifying it as
            ``SOURCE_UNAVAILABLE``/``GATEWAY_TIMEOUT``/``MALFORMED_RESULT``
            would drop the operator's decision out of the outcome and the
            audit path (bot review finding -- first fixed for the transport
            exception only, which left every normally returned response on the
            old path). ``settlement_denied`` likewise applies to all of them.

            The deadline is deliberately not consulted here: a lapsed
            authorization does not make a transport error a gateway timeout,
            and claiming otherwise would upgrade an uncertain contact into a
            false ``confirmed``.
            """
            decision = self._control_decision()
            if decision:
                # C3 第 4 节（:112）："暂停使相关在途结果失效，仅保留历史"。The
                # read reached the source and its bytes exist, so a human
                # decision invalidates the observation without discarding it:
                # it is committed with ``adopted=False`` and the outcome
                # carries it as history (bot review finding). A body that never
                # parsed has no observation to keep, and ``_history`` returns
                # ``None`` for it.
                return self._refuse(
                    operation,
                    "denied",
                    decision,
                    contact,
                    evidence=self._history(dispatch_id, operation, plan, response),
                )
            if settlement_denied:
                return self._refuse(operation, "denied", "CONTROL_UNAVAILABLE", contact)
            return self._refuse(
                operation,
                status,
                reason,
                contact,
                detail=detail if detail is not None else _refusal_detail(reason, plan),
            )

        try:
            charged = self._charge(operation.operation_id, elapsed, dispatch_id)
        except ToolBudgetExhausted:
            return refuse_after_fetch("denied", "OPERATION_BUDGET_EXHAUSTED")
        except ToolTimeBudgetExhausted:
            # Settling an already-counted dispatch is not gated by either cap,
            # so a real ledger cannot raise this here; handled for the same
            # defensive reason as its sibling.
            return refuse_after_fetch("denied", "TIME_BUDGET_EXHAUSTED")
        except ToolControlDenied:
            # The store refused on control grounds, which is a human decision,
            # not a storage outage. Do not return here: fall through to this
            # method's own control re-read so the outcome carries the
            # authoritative reason and the observation that really reached the
            # source is still registered as history (bot review finding).
            charged, settlement_denied = False, True
        if not charged and not settlement_denied:
            # A generic storage failure at settlement is still a post-fetch
            # refusal: route it through the same precedence check, or a pause
            # taken while the read was in flight stays out of the outcome and
            # the audit path (bot review finding -- this return was the one
            # post-fetch exit the previous pass did not enumerate).
            return refuse_after_fetch("denied", "CONTROL_UNAVAILABLE")

        if failure is not None:
            return refuse_after_fetch(failure[0], failure[1])
        if elapsed > timeout:
            # A result that arrives after its deadline must not update state.
            return refuse_after_fetch("timeout", "GATEWAY_TIMEOUT")
        problem, rows, payload = self._inspect(plan, operation, response)
        if problem is not None:
            return refuse_after_fetch(*problem)
        # Narrowing for the type checker: _inspect() returns no problem only
        # after it has established this.
        assert isinstance(response, TransportResponse)
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
        invalid = self._control_invalid(operation.finished_at)
        if invalid:
            pass
        elif settlement_denied:
            # The store denied the settlement on control grounds while this
            # re-read sees nothing wrong (the decision may have been taken and
            # reverted, or this snapshot may be stale). The authoritative
            # writer said no, and a result whose cost could not be recorded is
            # never adopted, so refuse -- but as history, not silence.
            invalid = "CONTROL_UNAVAILABLE"
        else:
            invalid = ""
        try:
            record = self._record(
                dispatch_id,
                operation,
                plan,
                response,
                payload,
                rows,
                status if not invalid else "denied",
                adopted=not invalid,
            )
        except _ViewTooLarge as too_large:
            # A read that completed and would have been adopted, but whose
            # whole view does not fit: refused like every other post-fetch
            # refusal (a human decision landed meanwhile still outranks it).
            # Nothing is registered -- like the byte-limit refusal, the raw
            # body is not kept as evidence -- and the read stays charged.
            return refuse_after_fetch(
                "error",
                "RESULT_TOO_LARGE",
                detail=_view_refusal_detail(
                    too_large.tokens,
                    too_large.limit,
                    too_large.query,
                    too_large.window,
                ),
            )
        if invalid:
            try:
                registered = self._register(
                    record
                )  # history only; adoption already refused
            except ToolControlDenied:
                # Already being refused for a control reason; a sink that also
                # rejects on control grounds leaves no evidence, which the
                # outcome reports by carrying none.
                registered = False
            return self._refuse(
                operation,
                "denied",
                invalid,
                "confirmed",
                evidence=record if registered else None,
            )
        try:
            committed = self._register(record)
        except ToolControlDenied:
            # Decided between the re-check above and the commit: report the
            # authoritative reason, not a generic evidence error.
            #
            # The fallback is deliberately neutral. ``ToolControlDenied`` covers
            # an expired lease and a changed owner as well as a newer
            # generation, so naming ``CONTROL_GENERATION_CHANGED`` when this
            # snapshot sees no change would assert a specific decision nobody
            # verified -- the same fabricated-audit-fact class as the contact
            # classification fixed earlier (bot review finding). It matches the
            # settlement-denied fallback in ``refuse_after_fetch`` for the same
            # reason. The precise cause stays where it was decided: the store
            # records the diverted result in its own audit trail.
            return self._refuse(
                operation,
                "denied",
                self._control_decision() or "CONTROL_UNAVAILABLE",
                "confirmed",
            )
        if not committed:
            # Evidence must be committed before it may be consumed. A generic
            # sink failure is an evidence error -- unless a human decision has
            # landed since the re-check above, which outranks it and keeps
            # the observation as history like every other post-fetch refusal.
            decision = self._control_decision()
            if decision:
                return self._refuse(
                    operation,
                    "denied",
                    decision,
                    "confirmed",
                    evidence=self._history(dispatch_id, operation, plan, response),
                )
            return self._refuse(
                operation, "error", "EVIDENCE_NOT_COMMITTED", "confirmed"
            )
        return ToolOutcome(
            operation=operation,
            status=status,
            reason=reason,
            source_contact="confirmed",
            # ``record.view`` is itself a detached copy (``EvidenceRecord.view``
            # deep-copies on every access), so this is already a separate
            # object from whatever is committed under ``view_sha256``; a
            # caller mutating it can never reach the stored evidence.
            model_view=record.view,
            evidence=record,
        )

    def _release(self, operation_id: str, dispatch_id: UUID) -> None:
        """Settle a reservation for a read that never went out, at zero cost.

        Best effort: the caller is already refusing, and a ledger that cannot
        record the release must not change the reason the caller reports. The
        reservation then stays held, which is exactly the behaviour this method
        exists to avoid -- but it is the pre-existing, fail-closed outcome, not
        a new failure mode.
        """
        try:
            self._charge(operation_id, 0.0, dispatch_id)
        except Exception:
            return

    def _charge(self, operation_id: str, seconds: float, dispatch_id: UUID) -> bool:
        try:
            self._ledger.charge(operation_id, seconds, dispatch_id=dispatch_id)
        except (ToolBudgetExhausted, ToolTimeBudgetExhausted, ToolControlDenied):
            # Fixed-code signals, not vendor text: let the caller report the
            # authoritative denial instead of a generic ledger failure (bot
            # review finding). ``ToolControlDenied`` must reach the caller for
            # the same reason -- swallowing it here would put the observation
            # back on the generic path that drops its history.
            raise
        except Exception:
            # Any other ledger error text may carry storage detail; never
            # re-raise it.
            return False
        return True

    def _inspect(
        self, plan: _Plan, operation: ToolOperation, response: object
    ) -> tuple[tuple[ToolStatus, str] | None, list[object], object]:
        """Validate one returned response; the only place these rules live.

        Returns ``(problem, rows, payload)``. ``problem`` is ``None`` when the
        response may be turned into a record -- adopted or history-only. Both
        callers go through it, because ``_history()`` originally repeated only
        the parse step and thereby committed an oversized body and crashed
        ``execute()`` with an ``AttributeError`` from a malformed
        ``source_start_at`` (bot review finding).
        """

        def _problem(
            status: ToolStatus, reason: str
        ) -> tuple[tuple[ToolStatus, str], list[object], object]:
            return (status, reason), [], None

        if not isinstance(response, TransportResponse) or not isinstance(
            response.body, bytes
        ):
            return _problem("error", "MALFORMED_RESULT")
        if len(response.body) > plan.registration.max_result_bytes:
            return _problem("error", "RESULT_TOO_LARGE")
        if response.source_status is not None:
            # A source error is not an observation of the target's state, so it
            # is classified onto a fixed reason and the operation record keeps
            # the audit trail; the error body itself is not registered as
            # evidence and its vendor text never reaches model context.
            source_reason = plan.registration.classify(str(response.source_status))
            return _problem("error", source_reason)
        rows, payload = _result_rows(plan.registration, response.body)
        if rows is None:
            return _problem("error", "MALFORMED_RESULT")
        if response.data_as_of is not None and (
            not isinstance(response.data_as_of, datetime)
            or response.data_as_of.tzinfo is None
            or response.data_as_of.utcoffset() is None
        ):
            return _problem("error", "MALFORMED_RESULT")
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
            return _problem("error", "MALFORMED_RESULT")
        query_window = response.query_window
        if query_window is not None and (
            not isinstance(query_window, Window)
            or not plan.window.contains(query_window)
        ):
            # B2 review disposition P1: the adapter may report a narrower
            # window it actually queried (otel_demo's start/end params), but
            # never one wider than or outside what it was authorized for --
            # the same fail-closed treatment as an over-long lookback below.
            return _problem("error", "MALFORMED_RESULT")
        effective_window = query_window if query_window is not None else plan.window
        lookback = response.lookback_seconds
        if lookback is not None and (
            type(lookback) is not int
            or lookback < 0
            or lookback > effective_window.seconds
        ):
            # The adapter may not claim a per-point read longer than the
            # (query) window it was asked for (the PromQL guard refuses
            # longer selectors before any request, and a longer one could
            # not start inside that window).
            return _problem("error", "MALFORMED_RESULT")
        if _unit_fields_problem(response, len(rows)):
            return _problem("error", "MALFORMED_RESULT")
        assert operation.finished_at is not None
        if any(
            moment is not None and moment > operation.finished_at
            for moment in (response.data_as_of, source_start_at, source_end_at)
        ):
            # No observation may claim source timestamps later than the moment
            # its own read completed. A future ``data_as_of`` makes
            # ``freshness_seconds`` negative, i.e. makes impossible metadata
            # look unusually fresh, and a future source interval claims rows
            # that cannot exist yet -- both were adopted and shown to the model
            # (bot review finding; the interval half is the same defect one
            # field over, found by checking the class rather than the one
            # reported field).
            #
            # Compared strictly against the trusted clock, with no skew
            # tolerance: any tolerance would be an arbitrary constant, and the
            # fail-closed direction surfaces a source whose clock is wrong as a
            # refusal the operator sees, instead of silently recording
            # corrupt freshness. ``data_as_of == finished_at`` stays valid
            # (freshness 0).
            return _problem("error", "MALFORMED_RESULT")
        if source_start_at is not None:
            # The adapter's own trusted metadata says which interval these
            # rows cover. When it lies outside what this Run was authorized to
            # read, the rows are out-of-scope data and the violation is
            # detectable here, so fail closed rather than hand them to the
            # model (bot review finding). The bound is the *scope* window, not
            # the narrower requested one: a source may legitimately cover a
            # slightly different interval inside the authorization (bucket
            # alignment), but never outside it.
            # Compared as bare bounds, not through ``Window``: a source may
            # report a single instant (start == end), which ``Window`` refuses.
            assert source_end_at is not None
            try:
                covered_start = source_start_at.astimezone(timezone.utc)
                covered_end = source_end_at.astimezone(timezone.utc)
            except (ValueError, OverflowError, OSError):
                return _problem("error", "MALFORMED_RESULT")
            scope_window = self._scope.window
            if covered_start < scope_window.start or covered_end > scope_window.end:
                return _problem("denied", "WINDOW_OUT_OF_SCOPE")
        return None, list(rows), payload

    def _history(
        self,
        dispatch_id: UUID,
        operation: ToolOperation,
        plan: _Plan,
        response: object,
    ) -> EvidenceRecord | None:
        """Commit an invalidated but well-formed observation as history.

        Returns ``None`` when there is nothing to keep -- the transport raised,
        the body never parsed, or the sink refused the commit. Never adopts:
        the caller is refusing, and the record says so.
        """
        if not isinstance(response, TransportResponse) or not isinstance(
            response.body, bytes
        ):
            return None
        if response.source_status is not None:
            # A source error is not an observation of the target's state: this
            # module already refuses to register its body as evidence or let
            # its vendor text near model context, and a human decision landing
            # on top does not turn it into one.
            return None
        problem, rows, payload = self._inspect(plan, operation, response)
        if problem is not None:
            # Whatever would have been refused on the adopt path is not
            # retained on the history path either: an over-limit body must not
            # slip past ``max_result_bytes`` because a human paused the Run.
            return None
        record = self._record(
            dispatch_id,
            operation,
            plan,
            response,
            payload,
            rows,
            "denied",
            adopted=False,
        )
        try:
            return record if self._register(record) else None
        except ToolControlDenied:
            # The sink refused this commit on control grounds too; there is
            # then no history to carry, which the outcome reports by carrying
            # none.
            return None

    def _register(self, record: EvidenceRecord) -> bool:
        try:
            reference = self._evidence.register(record)
        except ToolControlDenied:
            # The sink is required to validate the control generation inside
            # the commit; when it rejects on those grounds that is a human
            # decision, not a storage outage, and collapsing it into ``False``
            # reported ``EVIDENCE_NOT_COMMITTED`` while the operator's pause
            # stayed out of the outcome entirely (bot review finding). Same
            # typed signal, same treatment as the ledger's.
            raise
        except Exception:
            return False
        return reference == record.evidence_id

    # -- evidence ------------------------------------------------------

    def _view_tokens(self, view: Mapping[str, object]) -> int:
        """Tokens of the exact string a view becomes as model message content."""

        count = self._count_tokens(canonical(view))
        if type(count) is not int or count < 0:
            raise ToolContractError("INVALID_TOKEN_COUNTER")
        return count

    def _record(
        self,
        dispatch_id: UUID,
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
        # Every row of an adopted observation goes to the model, or the whole
        # view is refused below (``_ViewTooLarge``): a view is never partly
        # delivered. An invalidated observation survives as history; none of
        # its rows are handed to the model, and the view says so.
        kept: Sequence[object] = rows if adopted else ()
        data_as_of = response.data_as_of
        source_start_at = response.source_start_at
        source_end_at = response.source_end_at
        freshness = (
            None if data_as_of is None else (observed_at - data_as_of).total_seconds()
        )
        lookback = response.lookback_seconds
        # B2 review disposition P1: the view's window (and, below,
        # lookback_start_at) is the window actually queried, not the wider
        # authorized frame ``plan.window`` always carries -- validated
        # in ``_inspect()`` to be no wider than and contained in it.
        query_window = response.query_window
        effective_window = query_window if query_window is not None else plan.window
        view: dict[str, object] = {
            # One identity per dispatched observation, not per stable
            # operation: the per-dispatch charging protocol permits the same
            # operation to be dispatched twice (a retry, a duplicate
            # delivery), and those responses can carry different bytes and
            # observation times. Sharing an id would let a sink keyed by it
            # drop or overwrite one of them and return the surviving
            # reference, which ``_register()`` accepts as proof of commit --
            # the model view would then describe bytes the evidence link does
            # not resolve to (bot review finding). ``operation_id`` stays
            # beside it for correlation.
            "evidence_id": f"{operation.operation_id}:{dispatch_id}",
            "operation_id": operation.operation_id,
            "trust": "untrusted-evidence",
            "status": status,
            "adopted": adopted,
            # Stated on the view itself because the L2 contract's citation
            # rule ("fact-like claims cite ok views only") was invisible to
            # the model while ``adopted: true`` on a ``no_data`` view looked
            # like an invitation to cite it (v4 packet, normal-2 / fault-1).
            "citable_as_fact": adopted and status == "ok",
            "tool": registration.name,
            "tool_version": registration.version,
            "source": registration.source,
            "target_id": plan.target.target_id,
            "registry_revision": operation.registry_revision,
            "tool_registry_revision": operation.tool_registry_revision,
            "projection_revision": PROJECTION_REVISION,
            "query": dict(plan.params),
            "window": effective_window.as_json(),
            "observed_at": observed_at.isoformat(),
            "dispatch_started_at": None
            if operation.started_at is None
            else operation.started_at.isoformat(),
            "data_as_of": None if data_as_of is None else data_as_of.isoformat(),
            "source_start_at": None
            if source_start_at is None
            else source_start_at.isoformat(),
            "source_end_at": None
            if source_end_at is None
            else source_end_at.isoformat(),
            "freshness_seconds": freshness,
            "lookback_seconds": lookback,
            # The earliest instant any returned point reads: the adapter
            # starts evaluating ``lookback`` after the query window's start,
            # so the first point's ``[t - lookback, t]`` begins exactly there.
            "lookback_start_at": None
            if lookback is None
            else effective_window.start.isoformat(),
            "result_count": len(rows),
            "incomplete": incomplete,
        }
        if response.view_fields and adopted:
            view.update(response.view_fields)
        view.update(
            _row_dependent_fields(
                kept,
                rows=rows,
                adopted=adopted,
                row_unit=response.row_unit,
                backend_rows_returned=response.backend_rows_returned,
            )
        )
        if adopted:
            # The one size check (M1-01, A): the whole view, all fixed fields
            # and every row, in the counter's tokens -- what would reach the
            # model as this tool message's content. Over the registered limit
            # it is refused whole, never truncated into a partial view (the
            # partial views of one trace batch were the source of the
            # overlapping-evidence errors in the third-batch review; see
            # docs/tasks/2026-09-29-m1-01-view-bytes-timeout.md).
            tokens = self._view_tokens(view)
            if tokens > registration.max_view_tokens:
                raise _ViewTooLarge(
                    tokens,
                    registration.max_view_tokens,
                    cast(Mapping[str, object], view["query"]),
                    cast(Mapping[str, object], view["window"]),
                )
        # Read back from ``view`` so the record's own audit fields match
        # exactly what the committed view says.
        omitted_rows = cast(int, view["omitted_rows"])
        omitted_bytes = cast(int, view["omitted_bytes"])
        return EvidenceRecord(
            evidence_id=f"{operation.operation_id}:{dispatch_id}",
            operation=operation,
            status=status,
            raw=response.body,
            raw_sha256=sha256(response.body).hexdigest(),
            _view=view,
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
        detail: Mapping[str, object] | None = None,
    ) -> ToolOutcome:
        """Build a refusal outcome whose model view carries no source content.

        ``detail`` (B1 review disposition P2) adds fixed, reason-specific,
        content-free extra fields -- never a source's own bytes -- so a
        refusal the model can act on (retry with a smaller ``limit`` or a
        narrower window) is not just a bare code.
        """

        view: dict[str, object] = {
            "operation_id": operation.operation_id,
            "trust": "gateway",
            "status": status,
            "citable_as_fact": False,
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
        if detail:
            view.update(detail)
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


def _refusal_detail(reason: str, plan: _Plan) -> Mapping[str, object] | None:
    """B1 review disposition P2: an actionable ``RESULT_TOO_LARGE`` refusal.

    Upstream's own oversized-result tools return a hint the model can act on
    rather than a bare error (the Prometheus toolset returns a summary
    instead of raw data when a result is too large). This project keeps
    ``RESULT_TOO_LARGE`` a refusal -- adopting no content either way -- but
    states the byte ceiling that was crossed and suggests a smaller
    ``limit`` or a narrower window, generically (not otel_demo-specific: any
    registration whose ``max_result_bytes`` a call exceeds gets the same
    actionable text). Every other reason gets no detail, unchanged.
    """
    if reason != "RESULT_TOO_LARGE":
        return None
    ceiling = plan.registration.max_result_bytes
    return {
        "max_result_bytes": ceiling,
        "message": (
            f"The result exceeded the {ceiling}-byte limit for this tool. "
            "Retry with a smaller limit or a narrower window."
        ),
    }


def _view_refusal_detail(
    tokens: int,
    limit: int,
    query: Mapping[str, object],
    window: Mapping[str, object],
) -> Mapping[str, object]:
    """The over-limit-view sibling of ``_refusal_detail``: same reason, same
    ``message`` field, but the view's own limit and measured size. Worded like
    upstream's oversized-result error (``tool_context_window_limiter.py:69,
    131-139``): fixed text, never source content.

    ``window`` and ``query`` are what an ok view would have carried: the
    window actually queried (which overrides the authorized frame the generic
    refusal view shows) and the accepted parameters. Without them the model
    could not tell which of its own calls was refused and read the frame as
    the window it had asked for (real-Run reviews, view-tokens-effect)."""
    return {
        "window": window,
        "query": dict(query),
        "max_view_tokens": limit,
        "view_tokens": tokens,
        "message": (
            f"The tool call result is too large to return: {tokens}/{limit} "
            "tokens.\n"
            "Try to repeat the query but proactively narrow down the result "
            "(a narrower time window, a filter, or a smaller limit) so that "
            "the tool answer fits within the allowed number of tokens."
        ),
    }


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
        if isinstance(value, float) and not math.isfinite(value):
            # NaN/Infinity are valid Python floats -- spec.accepts() only
            # isinstance-checks a "number" parameter -- but canonical()
            # serializes them as the bare, non-standard JSON tokens NaN/
            # Infinity (Python's json.dumps default), which a strict
            # transport-side deserializer may reject or a source may
            # interpret inconsistently, letting a model-supplied query value
            # escape the declared JSON contract (bot review finding).
            return None, "INVALID_PARAMS"
        accepted[key] = value
    if any(
        spec.required and key not in accepted
        for key, spec in registration.parameters.items()
    ):
        return None, "INVALID_PARAMS"
    return accepted, ""


def _reject_constant(name: str) -> object:
    """Refuse JSON's non-standard constants anywhere in a source body.

    ``json.loads`` accepts ``NaN``/``Infinity``/``-Infinity`` by default, and
    the earlier row-level check only looked at values under ``result_path``.
    A body such as ``{"meta": NaN, "data": {"result": [...]}}`` was therefore
    adopted, and the *raw bytes retained as evidence* were not valid JSON --
    a strict evidence reader fails on them, and non-finite values in fields
    like the incomplete marker can steer projection semantics (bot review
    finding). Rejecting at decode time covers the whole payload, not one
    subtree.
    """
    raise ValueError(f"NON_FINITE_CONSTANT:{name}")


def _result_rows(
    registration: ToolRegistration, body: bytes
) -> tuple[list[object] | None, object]:
    """Navigate the declared result path; ``None`` rows means malformed."""

    try:
        payload = json.loads(body.decode("utf-8"), parse_constant=_reject_constant)
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
            json.dumps(row, allow_nan=False)
    except (ValueError, RecursionError):
        # A `\uD800`-style escape decodes into a Python str holding a lone
        # surrogate codepoint -- json.loads accepts it without error -- but
        # re-encoding it to UTF-8 for canonicalization (here, and later in
        # _record()) raises UnicodeEncodeError, a ValueError subclass. This
        # is a fresh failure mode past decoding succeeding, not a duplicate
        # of the decoder-limit check above (bot review finding).
        #
        # A bare `NaN`/`Infinity`/`-Infinity` token is likewise accepted by
        # json.loads()'s default parse_constant (a Python json extension,
        # not standard JSON) and decodes into a non-finite float with no
        # error either -- canonical() later re-emits the same non-standard
        # token rather than raising, so it would otherwise pass straight
        # through into the committed view. json.dumps(row, allow_nan=False)
        # raises ValueError on a non-finite float anywhere in the row,
        # mirroring the finite-parameter check already applied to
        # model-supplied params (bot review finding).
        return None, payload
    return cursor, payload


# Every key ``_record`` writes on a view; an adapter's ``view_fields`` may not
# shadow one of them.
_GENERIC_VIEW_KEYS = frozenset(
    {
        "evidence_id",
        "operation_id",
        "trust",
        "status",
        "adopted",
        "citable_as_fact",
        "tool",
        "tool_version",
        "source",
        "target_id",
        "registry_revision",
        "tool_registry_revision",
        "projection_revision",
        "query",
        "window",
        "observed_at",
        "dispatch_started_at",
        "data_as_of",
        "source_start_at",
        "source_end_at",
        "freshness_seconds",
        "lookback_seconds",
        "lookback_start_at",
        "result_count",
        "returned_count",
        "incomplete",
        "truncated",
        "omitted_rows",
        "omitted_bytes",
        "content",
    }
)
_ROW_UNIT = re.compile(r"[a-z][a-z_]*")


def _unit_fields_problem(response: TransportResponse, row_count: int) -> bool:
    """``True`` when the adapter's unit counts or extra view fields are not
    well formed: a unit without a count (or the reverse), a backend count
    below the rows it handed over, a key that shadows a generic field or one
    of the unit-count fields, or a value that is not plain finite JSON."""
    unit = response.row_unit
    backend = response.backend_rows_returned
    if (unit is None) != (backend is None):
        return True
    reserved: set[str] = set()
    if unit is not None:
        if not isinstance(unit, str) or not _ROW_UNIT.fullmatch(unit):
            return True
        if type(backend) is not int or backend < row_count:
            return True
        reserved = {f"backend_{unit}_returned", f"{unit}_shown", f"{unit}_omitted"}
    fields = response.view_fields
    if fields is None:
        return False
    if not isinstance(fields, Mapping):
        return True
    for key, value in fields.items():
        if not isinstance(key, str) or not key or key in _GENERIC_VIEW_KEYS:
            return True
        if key in reserved:
            return True
        try:
            json.dumps(value, allow_nan=False)
            canonical(value).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            return True
    return False


def _incomplete(registration: ToolRegistration, payload: object) -> bool:
    marker = registration.incomplete_marker
    if marker is None or not isinstance(payload, Mapping):
        return False
    return bool(payload.get(marker))


def _row_dependent_fields(
    kept: Sequence[object],
    *,
    rows: Sequence[object],
    adopted: bool,
    row_unit: str | None,
    backend_rows_returned: int | None,
) -> dict[str, object]:
    """The view fields that depend on which rows are shown.

    ``kept`` is every row of an adopted observation and none of an
    invalidated one (history), so ``omitted_rows``/``omitted_bytes`` are zero
    on a view the model receives and describe the withheld rows otherwise.
    """
    omitted_rows = len(rows) - len(kept)
    omitted_bytes = sum(
        len(canonical(row).encode("utf-8")) for row in rows[len(kept) :]
    )
    fields: dict[str, object] = {
        "returned_count": len(kept),
        "truncated": bool(omitted_rows),
        "omitted_rows": omitted_rows,
        "omitted_bytes": omitted_bytes,
        "content": None if not adopted else list(kept),
    }
    if row_unit is not None and backend_rows_returned is not None:
        # Counts with their unit (round 2 rule B): what the source returned,
        # what this view shows, and the gap between them -- rows the adapter
        # sampled out, which the model does not see (the view limit never
        # drops rows: an over-limit view is refused whole).
        fields[f"backend_{row_unit}_returned"] = backend_rows_returned
        fields[f"{row_unit}_shown"] = len(kept)
        fields[f"{row_unit}_omitted"] = backend_rows_returned - len(kept)
    return fields
