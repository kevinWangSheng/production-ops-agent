"""One logical sample, end to end: queries, readings, verdict, submission.

The deterministic task the Observer process runs for each leased sampling
job (M1-02 step 4, issue #86; C3 section 10 "采样与提交"):

1. the window ends at the database clock *now* and spans the profile's
   evaluation window;
2. for every signal, three instant queries at the window end (``query``,
   ``coverage_query``, ``freshness_query``) -- and **before each request**
   the lease's control scope is re-checked (C3 section 4); once it has moved
   no further request is issued and the partial sample is submitted, which
   the store files as ``suspended`` and ends the session;
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
    "InstantSource",
    "SampleTaken",
    "replay_readings",
    "submit_without_readings",
    "take_sample",
]

_log = logging.getLogger("opspilot.observer")


class InstantSource(Protocol):
    def instant(
        self, expr: str, *, at: datetime, timeout_seconds: int
    ) -> InstantResult: ...


@dataclass(frozen=True)
class SampleTaken:
    receipt: SampleReceipt
    evaluation: SampleEvaluation | None
    # how many instant queries were issued, and whether the scope check
    # stopped the sample before every signal was queried
    requests_issued: int
    scope_interrupted: bool


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
    gathered: list[tuple[HealthSignal, dict[str, InstantResult]]] = []
    issued = 0
    interrupted = False
    for signal in profile.signals:
        results: dict[str, InstantResult] = {}
        for kind, expr in (
            ("query", signal.query),
            ("coverage", signal.coverage_query),
            ("freshness", signal.freshness_query),
        ):
            if not check(lease):
                interrupted = True
                break
            results[kind] = source.instant(expr, at=window_end, timeout_seconds=timeout)
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
        reading, raw = _reading(
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
        "sample session=%s sequence=%s outcome=%s present=%s requests=%s interrupted=%s disposition=%s reason=%s state=%s",
        lease.session_id,
        lease.sequence,
        evaluation.outcome,
        evaluation.required_signals_present,
        issued,
        interrupted,
        receipt.disposition,
        receipt.reason,
        receipt.session_state,
    )
    return SampleTaken(
        receipt=receipt,
        evaluation=evaluation,
        requests_issued=issued,
        scope_interrupted=interrupted,
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
    if freshness.status == "ok" and freshness.value is not None:
        latest = epoch_to_datetime(freshness.value)
    if status == "ok" and count is None:
        # the value came back but the point count did not: the coverage
        # query failed or timed out, the reading cannot count
        status = coverage.status if coverage.status != "no_data" else "failed"
    if status == "ok" and count == 0:
        status = "no_data"
    if status == "ok" and not all(item.body_complete for item in results.values()):
        # A truncated body is not the actual return: fail closed, the
        # bundle records which body is incomplete (PR #119 review P2-4).
        status = "failed"
    raw = _bundle(window_start, window_end, sample_time, results)
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
) -> bytes:
    """The three responses as one JSON document: exact body bytes (base64)
    with their own sha256 each, whether each body is complete, the
    expression, HTTP status, the instant the queries were evaluated at and
    the ``sample_time`` the verdict used. Deterministic for the same inputs.

    Always within ``READING_RAW_LIMIT``: each body is bounded by
    ``RESPONSE_LIMIT_BYTES`` (three of them base64-encoded fit with room for
    the fields), so the stored hash is always the hash of the actual
    returns, never of a substitute.
    """
    document: dict[str, Any] = {
        "format": "opspilot.observer.reading/2",
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "evaluated_at": window_end.isoformat(),
        "sample_time": sample_time.isoformat(),
    }
    for kind, item in results.items():
        document[kind] = {
            "expr": item.expr,
            "status": item.status,
            "detail": item.detail,
            "http_status": item.http_status,
            "body_complete": item.body_complete,
            "body_sha256": hashlib.sha256(item.body).hexdigest(),
            "body_b64": base64.b64encode(item.body).decode("ascii"),
        }
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    assert len(raw) <= READING_RAW_LIMIT, len(raw)
    return raw


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
        reading, _ = _reading(
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
    return evaluate_readings(profile, readings, sample_time=sample_time)


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
    status, value, detail = (
        _single_value(body) if complete else ("failed", None, "INCOMPLETE")
    )
    if str(part.get("status")) in ("timeout", "failed") and status == "ok":
        # the Observer recorded a transport failure for this body
        status, value = cast(InstantStatus, str(part["status"])), None
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
