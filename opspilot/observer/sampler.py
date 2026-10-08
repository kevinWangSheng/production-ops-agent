"""One logical sample, end to end: queries, readings, verdict, submission.

The deterministic task the Observer process runs for each leased sampling
job (M1-02 step 4, issue #86; C3 section 10 "采样与提交"):

1. the window ends at the database clock *now* and spans the profile's
   evaluation window;
2. for every signal, three instant queries at the window end (``query``,
   ``coverage_query``, ``freshness_query``) -- and **before each request**
   the lease's control scope is re-checked (C3 section 4); once it has moved
   no further request is issued and the partial sample is submitted, which
   the store files as ``suspended`` and ends the session. Every request is
   bounded by what is left of the lease (``SUBMIT_MARGIN_SECONDS``), so the
   whole sample is submitted inside its lease even when the source hangs;
3. ``sample_time`` is taken *after* the last query returned (the verdict
   judges freshness against it; a window from the future is stale);
4. the readings are judged by ``evaluate_readings``; a reading the verdict
   finds stale or too thin is stored with that status, so the stored rows
   say what the verdict said and a replay from the rows agrees;
5. the raw bodies of the three responses travel with the reading (a bounded
   JSON bundle with its sha256 and the ``sample_time`` used, kept by the
   store; ``replay_readings`` re-judges from it alone) and the sample is
   submitted in one transaction.

No model is called anywhere on this path, and nothing here imports the
investigation worker, the tool gateway or their credentials.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, cast
from uuid import uuid4

from opspilot.domain.evidence import QueryWindow
from opspilot.domain.observation import HealthSample
from opspilot.observation.store import (
    READING_RAW_LIMIT,
    ObservationStore,
    SampleLease,
    SampleReceipt,
)
from opspilot.observation.store import SignalReading as StoredReading
from opspilot.observer.health_profile import (
    HealthProfile,
    HealthSignal,
    ReadingStatus,
    SampleEvaluation,
    SignalReading,
    evaluate_readings,
)
from opspilot.observer.prometheus import (
    InstantResult,
    InstantStatus,
    _single_value,
    epoch_to_datetime,
)

__all__ = [
    "SUBMIT_MARGIN_SECONDS",
    "InstantSource",
    "SampleTaken",
    "replay_readings",
    "replay_sample",
    "submit_without_readings",
    "take_sample",
]

_log = logging.getLogger("opspilot.observer")

#: Time kept free at the end of the lease for the two store round trips that
#: follow the last query (``current_time`` and ``submit_sample``) plus clock
#: skew between the Observer and PostgreSQL. The whole sample -- every
#: request -- must finish before ``lease_until - SUBMIT_MARGIN_SECONDS``: a
#: sample submitted after its lease is ``lease_revoked`` history, re-leased
#: with the same sequence and never adopted, so a hung Prometheus would
#: otherwise spin the session without consuming its budget (PR #119 codex
#: P1). Per request the timeout is min(profile timeout, time left in the
#: budget); once the budget is spent the remaining queries are recorded as
#: ``timeout`` without a request and the sample is submitted as unknown.
#: Chosen over lease renewal because it needs no extra store round trips and
#: its bound is provable from the lease alone.
SUBMIT_MARGIN_SECONDS = 5


class InstantSource(Protocol):
    def instant(
        self, expr: str, *, at: datetime, timeout_seconds: int
    ) -> InstantResult: ...


@dataclass(frozen=True)
class SampleTaken:
    receipt: SampleReceipt
    evaluation: SampleEvaluation | None
    # how many instant queries were issued, whether the scope check stopped
    # the sample before every signal was queried, and whether the lease
    # budget ran out (remaining queries recorded as timeouts, not sent)
    requests_issued: int
    scope_interrupted: bool
    budget_exhausted: bool = False


# Verdicts the Observer writes back into the reading status so the stored
# row carries the judgement its raw bundle supports (``_covers`` in the store
# then agrees with ``required_signals_present``).
_STATUS_FOR_VERDICT: dict[str, ReadingStatus] = {
    "stale": "stale",
    "insufficient_samples": "no_data",
}


def take_sample(
    lease: SampleLease,
    profile: HealthProfile,
    source: InstantSource,
    store: ObservationStore,
    *,
    scope_current: Callable[[SampleLease], bool] | None = None,
) -> SampleTaken:
    """Query, judge and submit one sample for ``lease``.

    ``scope_current`` defaults to ``store.lease_scope_current`` and is asked
    before every request. The lease's profile revision must be the
    ``profile`` given (the caller resolved it from the session's revision).
    """
    if lease.health_profile_revision != profile.revision:
        raise ValueError("PROFILE_REVISION_MISMATCH")
    check = scope_current or store.lease_scope_current
    window_end = store.current_time()
    window_start = window_end - timedelta(seconds=profile.evaluation_window_seconds)
    timeout = profile.query_timeout_seconds
    # The sample's budget: the lease's remaining lifetime (database clock,
    # read once as ``window_end``) minus the submission margin, tracked on
    # the monotonic clock from here on.
    budget_end = (
        time.monotonic()
        + (lease.lease_until - window_end).total_seconds()
        - SUBMIT_MARGIN_SECONDS
    )
    gathered: list[tuple[HealthSignal, dict[str, InstantResult]]] = []
    issued = 0
    interrupted = False
    exhausted = False
    for signal in profile.signals:
        results: dict[str, InstantResult] = {}
        for kind, expr in (
            ("query", signal.query),
            ("coverage", signal.coverage_query),
            ("freshness", signal.freshness_query),
        ):
            left = budget_end - time.monotonic()
            if not exhausted and left >= 1.0:
                if not check(lease):
                    interrupted = True
                    break
                # the scope check is a store round trip that can take
                # seconds: size the request from what is left *after* it
                # (issue #125, PR #119 P2)
                left = budget_end - time.monotonic()
            if exhausted or left < 1.0:
                # no time for another bounded request inside the lease: the
                # query is recorded as a timeout that was never sent, and no
                # scope check is made for a request that is not sent (codex
                # round 3, P2: the checks would eat the submission margin)
                exhausted = True
                results[kind] = InstantResult(
                    expr,
                    "timeout",
                    None,
                    b"",
                    None,
                    "LEASE_BUDGET",
                    body_complete=False,
                )
                continue
            results[kind] = source.instant(
                expr, at=window_end, timeout_seconds=min(timeout, int(left))
            )
            issued += 1
        if interrupted:
            # A signal not fully queried has no reading: the verdict reads it
            # as missing, the store files the sample as suspended.
            break
        gathered.append((signal, results))
    # The time the verdict judges freshness against: read after the last
    # query returned, and written into every reading's raw bundle so a replay
    # judges with the same instant (PR #119 review P2-3).
    sample_time = store.current_time()
    provisional: list[SignalReading] = []
    bundles: dict[str, bytes] = {}
    for signal, results in gathered:
        reading, raw = _reading_or_failed(
            signal, profile.source, results, window_start, window_end, sample_time
        )
        provisional.append(reading)
        bundles[signal.name] = raw
    first = evaluate_readings(profile, provisional, sample_time=sample_time)
    readings = [_finalized(reading, first) for reading in provisional]
    evaluation = evaluate_readings(profile, readings, sample_time=sample_time)
    assert evaluation.outcome == first.outcome
    assert evaluation.required_signals_present == first.required_signals_present
    sample = HealthSample(
        sample_id=str(uuid4()),
        session_id=str(lease.session_id),
        sequence=lease.sequence,
        window=QueryWindow(start=window_start, end=window_end),
        outcome=evaluation.outcome,
        subject_control_generation=lease.subject_control_generation,
        observation_generation=lease.observation_generation,
        health_profile_revision=lease.health_profile_revision,
        required_signals_present=evaluation.required_signals_present,
    )
    stored = [
        StoredReading(
            signal_name=reading.signal_name,
            status=reading.status,
            value=reading.value,
            sample_count=reading.sample_count,
            query=reading.query,
            window_start=reading.window_start,
            window_end=reading.window_end,
            source=reading.source,
            raw=bundles[reading.signal_name],
        )
        for reading in readings
    ]
    receipt = store.submit_sample(lease, sample, stored)
    _log.info(
        "sample session=%s sequence=%s outcome=%s present=%s requests=%s interrupted=%s budget_exhausted=%s disposition=%s reason=%s state=%s",
        lease.session_id,
        lease.sequence,
        evaluation.outcome,
        evaluation.required_signals_present,
        issued,
        interrupted,
        exhausted,
        receipt.disposition,
        receipt.reason,
        receipt.session_state,
    )
    return SampleTaken(
        receipt=receipt,
        evaluation=evaluation,
        requests_issued=issued,
        scope_interrupted=interrupted,
        budget_exhausted=exhausted,
    )


def submit_without_readings(
    lease: SampleLease,
    store: ObservationStore,
    *,
    outcome: str,
    window_seconds: int,
) -> SampleReceipt:
    """File a sample that could not be taken (no usable profile): an unknown
    observation that uses one of the session's samples, so a session that
    can never be judged still ends at its budget or deadline (C3 section 10:
    bounded continuation, then handoff)."""
    window_end = store.current_time()
    sample = HealthSample(
        sample_id=str(uuid4()),
        session_id=str(lease.session_id),
        sequence=lease.sequence,
        window=QueryWindow(
            start=window_end - timedelta(seconds=window_seconds), end=window_end
        ),
        outcome=outcome,  # type: ignore[arg-type]
        subject_control_generation=lease.subject_control_generation,
        observation_generation=lease.observation_generation,
        health_profile_revision=lease.health_profile_revision,
        required_signals_present=False,
    )
    return store.submit_sample(lease, sample, [])


def _reading(
    signal: HealthSignal,
    source_name: str,
    results: dict[str, InstantResult],
    window_start: datetime,
    window_end: datetime,
    sample_time: datetime,
) -> tuple[SignalReading, bytes]:
    value = results["query"]
    coverage = results["coverage"]
    freshness = results["freshness"]
    status: ReadingStatus
    if value.status == "ok":
        status = "ok"
    else:
        status = value.status
    count: int | None = None
    if coverage.status == "ok" and coverage.value is not None:
        count = max(0, int(coverage.value))
    elif coverage.status == "no_data":
        count = 0
    latest: datetime | None = None
    reading_error: str | None = None
    if freshness.status == "ok" and freshness.value is not None:
        try:
            latest = epoch_to_datetime(freshness.value)
        except (OverflowError, OSError, ValueError):
            # a finite number that is not a usable epoch (codex round 4):
            # the signal fails closed instead of crashing the sample
            latest = None
            reading_error = "FRESHNESS_EPOCH_INVALID"
            status = "failed"
    if status == "ok" and count is None:
        # the value came back but the point count did not: the coverage
        # query failed or timed out, the reading cannot count
        status = coverage.status if coverage.status != "no_data" else "failed"
    if status == "ok" and freshness.status in ("timeout", "failed"):
        # the freshness query's transport outcome is the reading's: a
        # timeout stays a timeout, a failure a failure (codex round 3, P2);
        # an empty freshness answer leaves the reading ok and the verdict
        # judges it stale for want of a raw sample timestamp
        status = freshness.status
    if status == "ok" and count == 0:
        status = "no_data"
    if status == "ok" and not all(item.body_complete for item in results.values()):
        # A truncated body is not the actual return: fail closed, the
        # bundle records which body is incomplete (PR #119 review P2-4).
        status = "failed"
    raw, truncated = _bundle(
        window_start, window_end, sample_time, results, reading_error=reading_error
    )
    if truncated:
        # the stored bundle could not hold the complete returns: fail closed
        status = "failed"
    reading = SignalReading(
        signal_name=signal.name,
        status=status,
        value=value.value if status == "ok" else None,
        sample_count=count if status == "ok" else None,
        query=signal.query,
        window_start=window_start,
        window_end=window_end,
        source=source_name,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        latest_sample_at=latest,
    )
    return reading, raw


def _finalized(reading: SignalReading, evaluation: SampleEvaluation) -> SignalReading:
    verdict = next(
        (v for v in evaluation.verdicts if v.signal_name == reading.signal_name), None
    )
    if verdict is None or reading.status != "ok":
        return reading
    status = _STATUS_FOR_VERDICT.get(verdict.verdict)
    if status is None:
        return reading
    return reading.model_copy(
        update={"status": status, "value": None, "sample_count": None}
    )


def _bundle(
    window_start: datetime,
    window_end: datetime,
    sample_time: datetime,
    results: dict[str, InstantResult],
    *,
    reading_error: str | None = None,
) -> tuple[bytes, bool]:
    """The three responses as one JSON document: exact body bytes (base64)
    with their own sha256 each, whether each body is complete, the
    expression, HTTP status, the instant the queries were evaluated at and
    the ``sample_time`` the verdict used. Deterministic for the same inputs.

    Bounded as a whole by ``READING_RAW_LIMIT`` (the store's CHECK): three
    complete bodies at ``RESPONSE_LIMIT_BYTES`` fit with room to spare, but
    the expressions count too, so when the rendered document is still too
    large the largest body is cut (``body_complete`` false, ``truncated``
    true) until it fits. Returns ``(raw, truncated)``; a truncated bundle
    never backs an ``ok`` reading (codex round 4: no assert, no crash before
    the sample is submitted).
    """
    bodies = {kind: item.body for kind, item in results.items()}
    complete = {kind: item.body_complete for kind, item in results.items()}
    truncated = False
    while True:
        document: dict[str, Any] = {
            "format": "opspilot.observer.reading/2",
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "evaluated_at": window_end.isoformat(),
            "sample_time": sample_time.isoformat(),
        }
        if reading_error is not None:
            document["reading_error"] = reading_error
        if truncated:
            document["truncated"] = True
        for kind, item in results.items():
            document[kind] = {
                "expr": item.expr,
                "status": item.status,
                "detail": item.detail,
                "http_status": item.http_status,
                "body_complete": complete[kind],
                "body_sha256": hashlib.sha256(bodies[kind]).hexdigest(),
                "body_b64": base64.b64encode(bodies[kind]).decode("ascii"),
            }
        raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        excess = len(raw) - READING_RAW_LIMIT
        if excess <= 0:
            return raw, truncated
        largest = max(bodies, key=lambda kind: len(bodies[kind]))
        if not bodies[largest]:
            # nothing left to cut (an absurd expression): keep the fields,
            # drop every body; the reading is failed either way
            for kind in bodies:
                bodies[kind] = b""
                complete[kind] = False
            truncated = True
            document_without = json.dumps(
                {
                    "format": "opspilot.observer.reading/2",
                    "sample_time": sample_time.isoformat(),
                    "truncated": True,
                    "reading_error": reading_error or "BUNDLE_TOO_LARGE",
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            return document_without, True
        # base64 inflates 4/3; cut a little more than the excess
        cut = min(len(bodies[largest]), excess * 3 // 4 + 64)
        bodies[largest] = bodies[largest][: len(bodies[largest]) - cut]
        complete[largest] = False
        truncated = True


def _failed_reading(
    signal: HealthSignal,
    source_name: str,
    window_start: datetime,
    window_end: datetime,
    sample_time: datetime,
    error: BaseException,
) -> tuple[SignalReading, bytes]:
    """The reading for a signal whose construction raised: ``failed`` with a
    fixed code and the exception *type* only (never its text, which could
    quote a response), so one bad signal cannot crash the sample before it
    is submitted (codex round 4). The sample is still adopted as unknown and
    consumes its budget."""
    raw = json.dumps(
        {
            "format": "opspilot.observer.reading/2",
            "sample_time": sample_time.isoformat(),
            "reading_error": "READING_CONSTRUCTION_FAILED",
            "error_type": type(error).__name__,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    reading = SignalReading(
        signal_name=signal.name,
        status="failed",
        query=signal.query,
        window_start=window_start,
        window_end=window_end,
        source=source_name,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
    )
    return reading, raw


def _reading_or_failed(
    signal: HealthSignal,
    source_name: str,
    results: dict[str, InstantResult],
    window_start: datetime,
    window_end: datetime,
    sample_time: datetime,
) -> tuple[SignalReading, bytes]:
    try:
        return _reading(
            signal, source_name, results, window_start, window_end, sample_time
        )
    except Exception as exc:  # noqa: BLE001 - one signal must not sink the sample
        _log.warning(
            "reading construction failed signal=%s error=%s",
            signal.name,
            type(exc).__name__,
        )
        return _failed_reading(
            signal, source_name, window_start, window_end, sample_time, exc
        )


def replay_readings(
    profile: HealthProfile, stored: Sequence[Mapping[str, Any]]
) -> SampleEvaluation:
    """Re-judge a stored sample from its reading rows' raw bundles alone.

    Rebuilds every reading from the exact response bytes in the bundle
    (value, point count, newest raw sample time) and judges with the
    ``sample_time`` the bundle recorded -- never the wall clock -- so the
    same bundles always give the same verdict (F6 step 5). A row without a
    bundle, or whose bundle no longer hashes to ``raw_sha256``, is rebuilt
    as ``failed`` and so never counts.
    """
    return replay_sample(profile, stored)[1]


def replay_sample(
    profile: HealthProfile, stored: Sequence[Mapping[str, Any]]
) -> tuple[list[SignalReading], SampleEvaluation]:
    """``replay_readings`` plus the rebuilt readings as the Observer would
    have stored them (status written back from the verdict, see
    ``_finalized``), so a replay can compare them with the stored rows."""
    readings: list[SignalReading] = []
    sample_time: datetime | None = None
    for row in stored:
        raw = row.get("raw")
        raw = None if raw is None else bytes(raw)
        bundle: dict[str, Any] | None = None
        if raw is not None and hashlib.sha256(raw).hexdigest() == row.get("raw_sha256"):
            try:
                parsed = json.loads(raw)
                bundle = parsed if isinstance(parsed, dict) else None
            except ValueError:
                bundle = None
        signal = profile.signal(str(row["signal_name"]))
        if bundle is not None and sample_time is None:
            sample_time = datetime.fromisoformat(str(bundle["sample_time"]))
        results = {
            kind: _result_from_bundle(bundle, kind)
            for kind in ("query", "coverage", "freshness")
        }
        if signal is None or bundle is None:
            readings.append(
                SignalReading(
                    signal_name=str(row["signal_name"]),
                    status="failed",
                    query=str(row["query"]),
                    window_start=row["window_start"],
                    window_end=row["window_end"],
                    source=str(row["source"]),
                )
            )
            continue
        reading, _ = _reading_or_failed(
            signal,
            str(row["source"]),
            results,
            row["window_start"],
            row["window_end"],
            sample_time or row["window_end"],
        )
        readings.append(reading)
    if sample_time is None:
        # no bundle carried the instant: judge at the latest window end,
        # which can only make readings staler, never fresher
        sample_time = max(
            (row["window_end"] for row in stored), default=None
        ) or datetime.fromtimestamp(0, tz=timezone.utc)
    first = evaluate_readings(profile, readings, sample_time=sample_time)
    finalized = [_finalized(reading, first) for reading in readings]
    return finalized, evaluate_readings(profile, finalized, sample_time=sample_time)


def _result_from_bundle(bundle: dict[str, Any] | None, kind: str) -> InstantResult:
    part = None if bundle is None else bundle.get(kind)
    if not isinstance(part, dict):
        return InstantResult(
            "", "failed", None, b"", None, "MISSING", body_complete=False
        )
    body = base64.b64decode(str(part.get("body_b64", "")))
    if hashlib.sha256(body).hexdigest() != part.get("body_sha256"):
        return InstantResult(
            "", "failed", None, body, None, "BODY_HASH", body_complete=False
        )
    complete = bool(part.get("body_complete", True))
    recorded = str(part.get("status", ""))
    if recorded in ("timeout", "failed"):
        # The Observer recorded a transport outcome for this query; it is
        # what the verdict saw, whatever the (possibly partial) body parses
        # to, so the replay keeps it (PR #119 codex P2: a timeout must
        # replay as a timeout, not as a failure).
        status: InstantStatus = cast(InstantStatus, recorded)
        value: float | None = None
        detail = str(part.get("detail", ""))
    elif not complete:
        status, value, detail = "failed", None, "INCOMPLETE"
    else:
        status, value, detail = _single_value(body)
    return InstantResult(
        str(part.get("expr", "")),
        status,
        value,
        body,
        part.get("http_status"),
        detail,
        body_complete=complete,
    )


def readings_of(sample: Sequence[StoredReading]) -> list[str]:
    """Signal names of stored readings (log/evidence helper)."""
    return [item.signal_name for item in sample]
