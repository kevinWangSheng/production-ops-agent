"""Versioned ``HealthProfile`` and the pure per-sample verdict (C3 section 10).

A profile says what *recovered* means for one target: the required signals
and their PromQL, how many points a reading needs and how much traffic makes
an error ratio meaningful, the healthy bounds, how fresh a reading must be,
how long health has to hold, and how long and how often the Observer may
sample. The file is data, not code: the Observer (step 4) runs the queries
with its own read-only credentials, the session store (step 2) keeps the
readings, and this module only decides what one set of readings says.

Three rules this module exists to hold:

1. A required signal that is missing, stale, timed out, failed or too thin
   never confirms health; the sample is adoptable as an *unknown* outcome
   (``no_data`` / ``stale`` / ``timeout`` / ``failed``), not a healthy one.
2. Traffic below the profile's effective-traffic gate makes every ratio
   meaningless, so the sample is ``no_data`` even when the error ratio
   looks fine (F6 step 2: removing traffic does not confirm recovery).
3. The revision is a function of the file's content. Any change to a query,
   a threshold or a window produces a new revision, so a session bound to the
   old revision stops adopting samples (``evaluate_sample`` compares it).
4. The queries select the ``subject`` and nothing else. ``subject`` is the
   value the authorization compares with the registered target, so a file
   whose subject says ``payment`` while its PromQL still selects
   ``checkout`` would let checkout's health resolve a payment incident
   (issue #122). Every vector selector in every query must carry the
   subject's namespace and workload under the label names the signal's
   ``scope`` declares; a selector that cannot be parsed, has no matcher on
   those labels, or matches them any other way is refused at load.

The reading DTO and the evaluation output follow the M1-02 step 1/2
interface contract recorded in
``docs/tasks/2026-10-03-m1-02-recovery-observation.md``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from opspilot.domain.base import (
    DTO,
    Count,
    DomainError,
    Positive,
    Text,
    sanitized_errors,
)
from opspilot.domain.observation import SampleOutcome

__all__ = [
    "PROFILE_DIRECTORY",
    "PROFILE_FORMAT_VERSION",
    "WINDOW_TOLERANCE_SHARE",
    "canonical_content",
    "HealthProfile",
    "HealthProfileError",
    "HealthSignal",
    "ProfileSubject",
    "ReadingStatus",
    "SampleEvaluation",
    "SessionParameters",
    "SignalBound",
    "SignalReading",
    "SignalScope",
    "SignalVerdict",
    "TrafficGate",
    "evaluate_readings",
    "load_health_profile",
    "profile_revision",
]

PROFILE_FORMAT_VERSION = 1
#: Profiles shipped with the product live next to this module. A deployment
#: may point the Observer at another directory; the loader takes a path.
PROFILE_DIRECTORY = Path(__file__).resolve().parent / "profiles"

Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
#: A Kubernetes DNS-1123 label: the shape of a namespace, a Deployment name
#: and the OTel ``service.name`` values the lab emits. The scope check
#: derives regular expressions from these values (``<service>-.*``,
#: ``a|b|c``), so they may not contain regex metacharacters.
DnsLabel = Annotated[str, Field(pattern=r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")]
#: A Prometheus label name.
LabelName = Annotated[str, Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]{0,127}$")]
ReadingStatus = Literal["ok", "no_data", "stale", "timeout", "failed"]
#: Per-signal verdicts. ``healthy``, ``degraded``, ``below_traffic_gate`` and
#: ``not_judged`` are the usable readings; the rest are unusable and make the
#: sample unknown when the signal is required.
SignalVerdictKind = Literal[
    "healthy",
    "degraded",
    "below_traffic_gate",
    "not_judged",
    "missing",
    "no_data",
    "stale",
    "timeout",
    "failed",
    "insufficient_samples",
    "query_mismatch",
    "not_in_profile",
]
#: Sample outcomes for an unusable required reading, worst first. ``failed``
#: and ``timeout`` describe the query, ``stale`` the data age, and
#: ``no_data`` covers everything that returned nothing usable.
_UNKNOWN_SEVERITY: tuple[SampleOutcome, ...] = ("failed", "timeout", "stale", "no_data")
_UNKNOWN_FOR_VERDICT: dict[str, SampleOutcome] = {
    "failed": "failed",
    "query_mismatch": "failed",
    "timeout": "timeout",
    "stale": "stale",
    "no_data": "no_data",
    "missing": "no_data",
    "insufficient_samples": "no_data",
}


#: A reading's window may differ from ``evaluation_window_seconds`` by this
#: share before it is judged stale. The Observer aligns window edges to the
#: Prometheus step (15 s in the lab), so a 300 s window legitimately spans
#: 285–315 s; a window that is minutes off is not the profile's window.
WINDOW_TOLERANCE_SHARE = 0.05


class HealthProfileError(Exception):
    """A profile file that cannot be read or does not satisfy the format."""

    def __init__(
        self,
        code: Literal["PROFILE_UNREADABLE", "PROFILE_INVALID"],
        detail: str,
        errors: Sequence[dict[str, object]] = (),
    ) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.errors = tuple(errors)


class SignalBound(DTO):
    """The closed interval a signal value must fall in to be healthy."""

    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def at_least_one_finite_edge(self) -> SignalBound:
        if self.min is None and self.max is None:
            raise ValueError("BOUND_EMPTY")
        for edge in (self.min, self.max):
            if edge is not None and not math.isfinite(edge):
                raise ValueError("BOUND_NOT_FINITE")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("BOUND_INVERTED")
        return self

    def contains(self, value: float) -> bool:
        if self.min is not None and value < self.min:
            return False
        if self.max is not None and value > self.max:
            return False
        return True


class SignalScope(DTO):
    """How one signal's selectors name the profile's ``subject`` (rule 4).

    Metric families spell the same workload differently: kube-state-metrics
    carries ``namespace`` and ``deployment`` (or a ``pod`` name prefixed by
    the Deployment), the span metrics carry ``service_name`` and no
    namespace at all. The signal therefore declares the label names; the
    values are never declared, they are the subject's. The validator then
    requires, in every vector selector of ``query``, ``coverage_query`` and
    ``freshness_query``:

    - ``<namespace_label>="<subject.kubernetes_namespace>"`` unless
      ``namespace_label`` is ``null``, the explicit, reviewable statement
      that the series have no namespace dimension;
    - ``<workload_label>="<subject.service>"`` (``exact``),
      ``<workload_label>=~"<subject.service>-.*"`` (``prefix``, pod names),
      or ``<workload_label>=~"<dep1>|<dep2>|..."`` (``dependencies``, the
      named objects a dependency signal watches).

    Any other matcher on those labels (``!=``, a regex, another value) is a
    mismatch; a selector without them is unbound. Both are refused.
    """

    namespace_label: LabelName | None
    workload_label: LabelName
    workload_match: Literal["exact", "prefix", "dependencies"] = "exact"
    dependencies: tuple[DnsLabel, ...] = ()

    @field_validator("dependencies", mode="before")
    @classmethod
    def dependencies_from_json_array(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def labels_and_dependencies_are_consistent(self) -> SignalScope:
        if self.namespace_label == self.workload_label:
            raise ValueError("SCOPE_LABELS_IDENTICAL")
        if (self.workload_match == "dependencies") != bool(self.dependencies):
            raise ValueError("SCOPE_DEPENDENCIES_INCONSISTENT")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("SCOPE_DEPENDENCY_DUPLICATE")
        return self


class HealthSignal(DTO):
    """One observable the Observer queries every sample.

    ``sample_count`` on a reading is the number of *raw* samples the signal's
    underlying series carry inside the evaluation window, obtained by the
    Observer from ``coverage_query`` (a ``count_over_time`` over the same
    selector and range as ``query``). A range query over ``query`` would not
    do: Prometheus evaluates each step with a lookback delta, so a series
    whose scrapes stopped minutes ago still yields points. ``minimum_samples``
    is the floor on that count; fewer raw samples make the reading
    ``insufficient_samples`` rather than a healthy one. The Observer stores
    the reading's ``query`` only; ``coverage_query`` is recovered from the
    profile revision on replay.

    ``freshness_query`` gives the time of the newest *raw* sample behind the
    signal, taken over every series the selector matches as the *oldest* of
    their newest samples (``min(timestamp(<selector>))``): a signal over
    eight dependencies is only as fresh as its stalest dependency, ``max``
    would let one fresh series hide seven stale ones (PR #119 review P2-1).
    Freshness is judged on that timestamp, not on the query window: after
    scrapes stop, ``query`` keeps answering through the lookback delta and
    ``coverage_query`` keeps counting the points already inside the range
    for minutes, while this timestamp stops moving at once. The Observer
    stores it on the reading (``latest_sample_at``); a reading without it is
    stale.

    ``traffic_dependent`` marks ratios and quantiles whose value means nothing
    without traffic (an error ratio over two requests, a p95 over one span).
    They are not judged while traffic is below the gate or unknown. State
    signals such as replica counts are judged regardless of traffic: zero
    replicas is a fact whether or not requests arrive.

    ``scope`` binds the three queries to the profile's subject
    (:class:`SignalScope`); the profile validator checks every selector.
    """

    name: Identifier
    description: Text
    required: bool = True
    query: Text
    coverage_query: Text
    freshness_query: Text
    minimum_samples: Positive = 1
    traffic_dependent: bool
    healthy: SignalBound
    scope: SignalScope


class TrafficGate(DTO):
    """The signal and floor below which ratio signals carry no information."""

    signal: Identifier
    minimum: Annotated[float, Field(gt=0)]


class SessionParameters(DTO):
    """The four values an observation session fixes at authorization time.

    Copied onto the session row by the caller (interface contract item 5);
    the session store never reads the profile file.
    """

    deadline_seconds: Positive
    max_samples: Positive
    sample_interval_seconds: Positive
    sustained_window_seconds: Positive

    @model_validator(mode="after")
    def health_can_be_sustained_within_the_budget(self) -> SessionParameters:
        """A window the budget can never cover would make recovery unprovable."""
        if self.sustained_window_seconds > self.deadline_seconds:
            raise ValueError("SUSTAINED_WINDOW_EXCEEDS_DEADLINE")
        coverage = (self.max_samples - 1) * self.sample_interval_seconds
        if coverage < self.sustained_window_seconds:
            raise ValueError("SAMPLE_BUDGET_CANNOT_COVER_SUSTAINED_WINDOW")
        return self


class ProfileSubject(DTO):
    """Which workload the profile describes.

    Compared with the registered target at authorization (namespace and
    workload, ``ObservationStore.authorize_session_in``) and bound to every
    query by the scope check (rule 4), so it cannot drift from the PromQL.
    """

    service: DnsLabel
    kubernetes_namespace: DnsLabel


class HealthProfile(DTO):
    """A versioned recovery definition for one target."""

    format_version: Literal[1]
    profile_id: Identifier
    description: Text
    subject: ProfileSubject
    source: Identifier
    #: Where the numbers came from (C3 section 13: calibrated against the lab
    #: baseline, frozen before candidate evaluation). Part of the revision.
    calibration_source: Text
    #: Range every query covers, and the age past which a reading is stale.
    evaluation_window_seconds: Positive
    freshness_seconds: Positive
    query_timeout_seconds: Positive
    session: SessionParameters
    effective_traffic: TrafficGate
    signals: tuple[HealthSignal, ...] = Field(min_length=1)

    @field_validator("signals", mode="before")
    @classmethod
    def signals_from_json_array(cls, value: object) -> object:
        """The file boundary is the one place a JSON array becomes a tuple."""
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def signals_are_consistent(self) -> HealthProfile:
        names = [signal.name for signal in self.signals]
        if len(set(names)) != len(names):
            raise ValueError("DUPLICATE_SIGNAL_NAME")
        if not any(signal.required for signal in self.signals):
            raise ValueError("NO_REQUIRED_SIGNAL")
        gate = next(
            (s for s in self.signals if s.name == self.effective_traffic.signal), None
        )
        if gate is None:
            raise ValueError("TRAFFIC_SIGNAL_UNKNOWN")
        if not gate.required:
            raise ValueError("TRAFFIC_SIGNAL_NOT_REQUIRED")
        if self.query_timeout_seconds > self.session.sample_interval_seconds:
            raise ValueError("QUERY_TIMEOUT_EXCEEDS_SAMPLE_INTERVAL")
        # Every range selector written into a query must be the evaluation
        # window: ``evaluate_readings`` judges the reading's window against
        # ``evaluation_window_seconds`` and would otherwise accept a profile
        # whose PromQL aggregates over another span (PR #113 review).
        for signal in self.signals:
            for query in (signal.query, signal.coverage_query, signal.freshness_query):
                for span in _range_selector_seconds(query):
                    if span != self.evaluation_window_seconds:
                        raise ValueError("RANGE_SELECTOR_NOT_EVALUATION_WINDOW")
        # Rule 4: every selector in every query names the subject the way the
        # signal's scope declares (issue #122). The error carries the signal
        # index and query field, never a value from the file.
        for index, signal in enumerate(self.signals):
            expected = _expected_matchers(self.subject, signal.scope)
            for field in ("query", "coverage_query", "freshness_query"):
                try:
                    _check_selectors_bound(getattr(signal, field), expected)
                except ValueError as exc:
                    raise ValueError(f"{exc} signals/{index}/{field}") from None
        return self

    @property
    def revision(self) -> str:
        return profile_revision(self)

    def signal(self, name: str) -> HealthSignal | None:
        return next((s for s in self.signals if s.name == name), None)


class SignalReading(DTO):
    """One signal's result for one sample (interface contract item 3)."""

    signal_name: Identifier
    status: ReadingStatus
    value: float | None = None
    sample_count: Count | None = None
    query: Text
    window_start: AwareDatetime
    window_end: AwareDatetime
    source: Identifier
    raw_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    #: Time of the newest raw sample behind the signal (``freshness_query``);
    #: ``None`` when the source returned none. Stale without it (issue #86).
    latest_sample_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def value_matches_status(self) -> SignalReading:
        """A non-``ok`` reading carries no value; an ``ok`` one must."""
        if self.window_end <= self.window_start:
            raise ValueError("INVALID_WINDOW")
        if self.status == "ok":
            if self.value is None or not math.isfinite(self.value):
                raise ValueError("OK_READING_NEEDS_FINITE_VALUE")
            if self.sample_count is None:
                raise ValueError("OK_READING_NEEDS_SAMPLE_COUNT")
        elif self.value is not None:
            raise ValueError("NON_OK_READING_HAS_VALUE")
        return self


class SignalVerdict(DTO):
    """Why one signal did or did not count towards health."""

    signal_name: Identifier
    required: bool
    verdict: SignalVerdictKind
    value: float | None = None
    reason: Text


class SampleEvaluation(DTO):
    """What one set of readings says (interface contract item 4)."""

    health_profile_revision: Text
    outcome: SampleOutcome
    required_signals_present: bool
    reason: Text
    verdicts: tuple[SignalVerdict, ...]


def profile_revision(profile: HealthProfile) -> str:
    """``<profile_id>@<sha256 of the canonical content>[:12]``.

    The session store only stores and compares this string. Canonical means
    the validated model rendered as JSON with sorted keys and no whitespace,
    so formatting and key order in the file do not change the revision while
    every value does.
    """
    canonical = canonical_content(profile)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{profile.profile_id}@{digest[:12]}"


def canonical_content(profile: HealthProfile) -> str:
    """The text the revision hashes: the validated model as JSON with sorted
    keys and no whitespace. The session store keeps this text behind the
    revision (``opspilot_health_profiles``) so a replay reads exactly what
    was hashed."""
    if not isinstance(profile, HealthProfile):
        raise DomainError("INVALID_INPUT", "health profile is required")
    return json.dumps(
        profile.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def load_health_profile(path: Path) -> HealthProfile:
    """Read and validate one profile file.

    Unknown fields, missing fields and out-of-range values are rejected; the
    raised error carries the sanitized field locations, never the file's
    values, so a mis-pasted credential cannot leak through the failure.
    """
    if not isinstance(path, Path):
        raise DomainError("INVALID_INPUT", "profile path is required")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HealthProfileError(
            "PROFILE_UNREADABLE", f"{path.name}: {exc.__class__.__name__}"
        ) from None
    try:
        payload = json.loads(text)
    except ValueError:
        raise HealthProfileError("PROFILE_INVALID", f"{path.name}: not JSON") from None
    if not isinstance(payload, dict):
        raise HealthProfileError(
            "PROFILE_INVALID", f"{path.name}: top level is not an object"
        )
    # The declared value is a file value and stays out of the error, like
    # every other rejected input. Exact int: ``True == 1`` in Python, a
    # boolean is not a format version (PR #113 review P2).
    declared = payload.get("format_version")
    if type(declared) is not int or declared != PROFILE_FORMAT_VERSION:
        raise HealthProfileError(
            "PROFILE_INVALID",
            f"{path.name}: format_version is not {PROFILE_FORMAT_VERSION}",
            [
                {
                    "type": "format_version",
                    "loc": ("format_version",),
                    "msg": "unsupported",
                }
            ],
        )
    try:
        return HealthProfile.model_validate(payload)
    except ValidationError as exc:
        errors = sanitized_errors(exc)
        where = ", ".join(_render_error(error) for error in errors)
        raise HealthProfileError(
            "PROFILE_INVALID", f"{path.name}: {where}", errors
        ) from None


def evaluate_readings(
    profile: HealthProfile,
    readings: Sequence[SignalReading],
    *,
    sample_time: datetime,
) -> SampleEvaluation:
    """Decide what one sample's readings say about the target.

    Deterministic and side-effect free; the same profile, readings and
    ``sample_time`` always give the same answer, which is what F6 step 5
    replays from the stored sample row.

    ``sample_time`` is when the Observer took the sample (database clock,
    stored with the sample). A reading is judged ``stale`` whatever its
    status says when its newest raw sample (``latest_sample_at``, from the
    signal's ``freshness_query``) is missing or older than
    ``freshness_seconds`` before ``sample_time``, when its window ended more
    than ``freshness_seconds`` before ``sample_time`` or after it, or when
    its window does not span ``evaluation_window_seconds`` (within
    ``WINDOW_TOLERANCE_SHARE``): old data never confirms recovery (C3
    section 10), and a query that still answers after scrapes stopped is
    old data.

    Outcome rules, in order:

    1. Any *judged* required signal outside its healthy bound is
       ``degraded``. A reading is judged when it is usable (``ok``, enough
       points, the profile's query and source, fresh) and either does not
       depend on traffic or traffic is confirmed above the gate. Zero
       replicas is degraded even with no traffic at all.
    2. Otherwise any required signal without a usable reading makes the
       sample *unknown*: the worst of ``failed`` > ``timeout`` > ``stale`` >
       ``no_data`` among them, with ``required_signals_present`` false.
    3. Otherwise traffic below the effective-traffic gate is ``no_data``:
       the ratios are present but meaningless (F6 step 2).
    4. Otherwise the sample is ``healthy``.

    Optional signals are reported in the verdicts but never move the outcome.
    """
    if not isinstance(profile, HealthProfile):
        raise DomainError("INVALID_INPUT", "health profile is required")
    if not isinstance(readings, Sequence) or any(
        not isinstance(item, SignalReading) for item in readings
    ):
        raise DomainError("INVALID_INPUT", "readings must be SignalReading values")
    if not isinstance(sample_time, datetime) or sample_time.tzinfo is None:
        raise DomainError("INVALID_INPUT", "sample_time must be timezone-aware")
    by_name: dict[str, SignalReading] = {}
    for reading in readings:
        if reading.signal_name in by_name:
            raise DomainError(
                "INVALID_INPUT", f"duplicate reading for {reading.signal_name}"
            )
        by_name[reading.signal_name] = reading

    judged: dict[str, _Judged] = {
        signal.name: _judge(profile, signal, by_name.get(signal.name), sample_time)
        for signal in profile.signals
    }
    gate = profile.effective_traffic
    traffic = judged[gate.signal]
    traffic_confirmed = traffic.usable and traffic.value is not None
    traffic_confirmed = traffic_confirmed and traffic.value >= gate.minimum  # type: ignore[operator]
    traffic_low = traffic.usable and not traffic_confirmed

    verdicts: list[SignalVerdict] = []
    for signal in profile.signals:
        item = judged[signal.name]
        verdict: SignalVerdictKind
        if not item.usable:
            verdict, reason = item.unusable, item.reason
        elif signal.name == gate.signal and traffic_low:
            verdict = "below_traffic_gate"
            reason = f"{item.value} < effective-traffic minimum {gate.minimum}"
        elif signal.traffic_dependent and not traffic_confirmed:
            verdict = "not_judged"
            reason = f"{item.value} not judged: traffic " + (
                "below the gate" if traffic_low else "not confirmed"
            )
        elif item.value is not None and not signal.healthy.contains(item.value):
            verdict = "degraded"
            reason = (
                f"{item.value} outside healthy bound {_render_bound(signal.healthy)}"
            )
        else:
            verdict = "healthy"
            reason = (
                f"{item.value} within healthy bound {_render_bound(signal.healthy)}"
            )
        verdicts.append(
            SignalVerdict(
                signal_name=signal.name,
                required=signal.required,
                verdict=verdict,
                value=item.value,
                reason=reason,
            )
        )
    known = {signal.name for signal in profile.signals}
    for name, reading in by_name.items():
        if name not in known:
            verdicts.append(
                SignalVerdict(
                    signal_name=name,
                    required=False,
                    verdict="not_in_profile",
                    value=reading.value,
                    reason="reading has no signal in the profile; ignored",
                )
            )

    required = [verdict for verdict in verdicts if verdict.required]
    unusable = [
        _UNKNOWN_FOR_VERDICT[verdict.verdict]
        for verdict in required
        if verdict.verdict in _UNKNOWN_FOR_VERDICT
    ]
    present = not unusable
    degraded = sorted(v.signal_name for v in required if v.verdict == "degraded")
    if degraded:
        outcome: SampleOutcome = "degraded"
        reason = f"required signals outside their healthy bound: {', '.join(degraded)}"
    elif unusable:
        outcome = next(level for level in _UNKNOWN_SEVERITY if level in unusable)
        names = sorted(
            verdict.signal_name
            for verdict in required
            if verdict.verdict in _UNKNOWN_FOR_VERDICT
        )
        reason = f"required signals without a usable reading: {', '.join(names)}"
    elif traffic_low:
        outcome = "no_data"
        reason = (
            f"traffic below the effective-traffic gate ({gate.signal} < {gate.minimum})"
        )
    else:
        outcome = "healthy"
        reason = "every required signal is present, fresh and within bounds"
    return SampleEvaluation(
        health_profile_revision=profile_revision(profile),
        outcome=outcome,
        required_signals_present=present,
        reason=reason,
        verdicts=tuple(verdicts),
    )


class _Judged(DTO):
    """Whether one reading can be judged at all, before bounds and traffic."""

    usable: bool
    value: float | None = None
    unusable: SignalVerdictKind = "missing"
    reason: Text = "-"


def _judge(
    profile: HealthProfile,
    signal: HealthSignal,
    reading: SignalReading | None,
    sample_time: datetime,
) -> _Judged:
    if reading is None:
        return _Judged(
            usable=False, unusable="missing", reason="no reading for this signal"
        )
    if reading.query != signal.query or reading.source != profile.source:
        return _Judged(
            usable=False,
            unusable="query_mismatch",
            reason="reading was taken with a query or source the profile does not define",
        )
    if reading.status != "ok":
        return _Judged(
            usable=False,
            unusable=reading.status,
            reason=f"reading status is {reading.status}",
        )
    age = (sample_time - reading.window_end).total_seconds()
    if age < 0 or age > profile.freshness_seconds:
        return _Judged(
            usable=False,
            unusable="stale",
            reason=(
                f"window ended {age:.0f}s before the sample; "
                f"profile allows {profile.freshness_seconds}s"
            ),
        )
    span = (reading.window_end - reading.window_start).total_seconds()
    expected = profile.evaluation_window_seconds
    if abs(span - expected) > expected * WINDOW_TOLERANCE_SHARE:
        return _Judged(
            usable=False,
            unusable="stale",
            reason=f"window spans {span:.0f}s; profile evaluates {expected}s",
        )
    if reading.latest_sample_at is None:
        return _Judged(
            usable=False,
            unusable="stale",
            reason="no raw sample timestamp behind the reading",
        )
    sample_age = (sample_time - reading.latest_sample_at).total_seconds()
    if sample_age < 0 or sample_age > profile.freshness_seconds:
        return _Judged(
            usable=False,
            unusable="stale",
            reason=(
                f"newest raw sample is {sample_age:.0f}s old at the sample; "
                f"profile allows {profile.freshness_seconds}s"
            ),
        )
    # ``value`` and ``sample_count`` are guaranteed by the reading validator.
    value = reading.value
    count = reading.sample_count
    assert value is not None and count is not None
    if count < signal.minimum_samples:
        return _Judged(
            usable=False,
            value=value,
            unusable="insufficient_samples",
            reason=f"{count} points, profile needs {signal.minimum_samples}",
        )
    return _Judged(usable=True, value=value)


_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'')
_BRACKET = re.compile(r"\[([^\]]*)\]")
_DURATION_PART = re.compile(r"(\d+)(ms|[smhdwy])")
_DURATION = re.compile(r"^(?:\d+(?:ms|[smhdwy]))+$")
_UNIT_SECONDS = {
    "ms": 0.001,
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "y": 31536000,
}


def _duration_seconds(text: str) -> float:
    """A full PromQL duration (``5m``, ``1m30s``, ``1h5m10s500ms``)."""
    if not _DURATION.match(text):
        raise ValueError("RANGE_SELECTOR_UNPARSABLE")
    return sum(
        int(amount) * _UNIT_SECONDS[unit]
        for amount, unit in _DURATION_PART.findall(text)
    )


def _range_selector_seconds(query: str) -> list[float]:
    """Spans of every range or subquery selector in ``query``.

    Quoted label values are skipped (a regex matcher may contain brackets);
    every remaining ``[...]`` must be a complete PromQL duration, optionally
    followed by ``:`` and a resolution, or the profile is rejected rather
    than silently left unchecked (codex round 3, P2: ``[1m30s]`` used to
    slip past a single-unit pattern).
    """
    spans: list[float] = []
    for content in _BRACKET.findall(_QUOTED.sub('""', query)):
        duration, separator, resolution = content.partition(":")
        spans.append(_duration_seconds(duration))
        if separator and resolution:
            _duration_seconds(resolution)
    return spans


class _Matcher(DTO):
    label: str
    op: str
    value: str


#: What the scanner distinguishes. Strings are consumed whole so a brace or
#: bracket inside a label value is never structure; numbers take their unit
#: suffix (``5m``) so the ``m`` is not an identifier.
_TOKEN = re.compile(
    r"(?P<space>\s+)"
    r"|(?P<string>\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')"
    r"|(?P<number>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?[a-zA-Z]*)"
    r"|(?P<ident>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"|(?P<brace>\{)"
    r"|(?P<punct>[()\[\],+\-*/%^=!~<>@.])"
)
_MATCHER = re.compile(
    r"\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*"
    r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')\s*"
)
_LABEL_LIST = re.compile(
    r"\s*(?:[a-zA-Z_][a-zA-Z0-9_]*(?:\s*,\s*[a-zA-Z_][a-zA-Z0-9_]*)*\s*,?)?\s*"
)
#: Keywords followed by a parenthesised list of label names, not selectors.
_LABEL_LIST_KEYWORDS = frozenset(
    {"by", "without", "on", "ignoring", "group_left", "group_right"}
)
_KEYWORDS = frozenset({"and", "or", "unless", "bool", "offset"})
_ESCAPES = {"\\": "\\", '"': '"', "'": "'", "n": "\n", "t": "\t", "r": "\r"}


def _vector_selectors(query: str) -> list[tuple[_Matcher, ...]]:
    """The matcher sets of every vector selector in ``query``, fail closed.

    A bare metric name (``up``, ``x[5m]``) selects every namespace, so it is
    ``SCOPE_SELECTOR_UNBOUND``; anything the scanner does not recognise (a
    stray brace, a backtick string, an escape it cannot decode) is
    ``SCOPE_SELECTOR_UNPARSABLE`` rather than skipped. An identifier is a
    function or aggregation when ``(`` follows it, a selector when ``{``
    does, a keyword when it is one; everything else is a bare metric.
    """
    selectors: list[tuple[_Matcher, ...]] = []
    position = 0
    pending: str | None = None  # an identifier awaiting the token after it
    while position < len(query):
        token = _TOKEN.match(query, position)
        if token is None:
            raise ValueError("SCOPE_SELECTOR_UNPARSABLE")
        kind, text, position = token.lastgroup, token.group(), token.end()
        if kind == "space":
            continue
        if kind == "brace":
            matchers, position = _matcher_body(query, position)
            selectors.append(matchers)
            pending = None
            continue
        if pending is not None:
            if pending in _LABEL_LIST_KEYWORDS:
                if text != "(":
                    raise ValueError("SCOPE_SELECTOR_UNPARSABLE")
                position = _skip_label_list(query, position)
                pending = None
                continue
            if text == "(":
                pending = None
                continue
            if kind == "ident" and text in _LABEL_LIST_KEYWORDS:
                pending = text  # ``sum by (le) (...)``
                continue
            raise ValueError("SCOPE_SELECTOR_UNBOUND")
        if kind == "ident" and text not in _KEYWORDS:
            pending = text
    if pending is not None:
        raise ValueError("SCOPE_SELECTOR_UNBOUND")
    return selectors


def _matcher_body(query: str, position: int) -> tuple[tuple[_Matcher, ...], int]:
    """Parse ``label op "value", ...`` from after ``{`` to the closing brace;
    returns the matchers and the position after ``}``."""
    start = position
    while position < len(query) and query[position] != "}":
        if query[position] in "\"'":
            string = _QUOTED.match(query, position)
            if string is None:
                raise ValueError("SCOPE_SELECTOR_UNPARSABLE")
            position = string.end()
        else:
            position += 1
    if position >= len(query):
        raise ValueError("SCOPE_SELECTOR_UNPARSABLE")
    body = query[start:position]
    matchers: list[_Matcher] = []
    cursor = 0
    while cursor < len(body):
        item = _MATCHER.match(body, cursor)
        if item is None:
            raise ValueError("SCOPE_SELECTOR_UNPARSABLE")
        matchers.append(
            _Matcher(
                label=item.group(1), op=item.group(2), value=_unquote(item.group(3))
            )
        )
        cursor = item.end()
        if cursor < len(body):
            if body[cursor] != ",":
                raise ValueError("SCOPE_SELECTOR_UNPARSABLE")
            cursor += 1
            if cursor >= len(body):
                raise ValueError("SCOPE_SELECTOR_UNPARSABLE")  # trailing comma
    return tuple(matchers), position + 1


def _skip_label_list(query: str, position: int) -> int:
    """``position`` is after the ``(`` of ``by (a, b)``; returns after ``)``."""
    end = query.find(")", position)
    if end < 0 or _LABEL_LIST.fullmatch(query, position, end) is None:
        raise ValueError("SCOPE_SELECTOR_UNPARSABLE")
    return end + 1


def _unquote(literal: str) -> str:
    """The value of a double- or single-quoted PromQL string. Only the
    escapes a label value needs are decoded; any other is refused."""
    out: list[str] = []
    inner = literal[1:-1]
    index = 0
    while index < len(inner):
        char = inner[index]
        if char == "\\":
            index += 1
            if index >= len(inner) or inner[index] not in _ESCAPES:
                raise ValueError("SCOPE_SELECTOR_UNPARSABLE")
            out.append(_ESCAPES[inner[index]])
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _expected_matchers(
    subject: ProfileSubject, scope: SignalScope
) -> dict[str, tuple[str, str]]:
    """``{label: (op, value)}`` every selector of the signal must carry."""
    expected: dict[str, tuple[str, str]] = {}
    if scope.namespace_label is not None:
        expected[scope.namespace_label] = ("=", subject.kubernetes_namespace)
    if scope.workload_match == "exact":
        expected[scope.workload_label] = ("=", subject.service)
    elif scope.workload_match == "prefix":
        expected[scope.workload_label] = ("=~", f"{subject.service}-.*")
    else:
        expected[scope.workload_label] = ("=~", "|".join(scope.dependencies))
    return expected


def _check_selectors_bound(query: str, expected: dict[str, tuple[str, str]]) -> None:
    """Every selector carries every expected matcher exactly; a query with
    no selector at all (``vector(0)``) observes nothing and is unbound."""
    selectors = _vector_selectors(query)
    if not selectors:
        raise ValueError("SCOPE_SELECTOR_UNBOUND")
    for matchers in selectors:
        for label, (op, value) in expected.items():
            found = [m for m in matchers if m.label == label]
            if not found:
                raise ValueError("SCOPE_SELECTOR_UNBOUND")
            if any((m.op, m.value) != (op, value) for m in found):
                raise ValueError("SCOPE_SELECTOR_MISMATCH")


def _render_location(location: object) -> str:
    if isinstance(location, tuple) and location:
        return "/".join(str(part) for part in location)
    return "<root>"


def _render_error(error: dict[str, object]) -> str:
    """The location, plus the code when the error is one of this module's
    validators (a ``ValueError`` whose message is a code and, for the scope
    check, the signal index and field). Other pydantic messages are not
    appended: they describe the expected shape and are not needed to act."""
    rendered = _render_location(error["loc"])
    if error["type"] == "value_error":
        code = str(error["msg"]).removeprefix("Value error, ")
        rendered = f"{rendered} {code}" if rendered != "<root>" else code
    return rendered


def _render_bound(bound: SignalBound) -> str:
    low = "-inf" if bound.min is None else str(bound.min)
    high = "+inf" if bound.max is None else str(bound.max)
    return f"[{low}, {high}]"
