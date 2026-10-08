"""Offline replay of a stored observation session (M1-02 step 5, #87; F6 step 5).

Recomputes the recovery verdict from what the store kept -- the session row,
the profile content frozen behind its revision, every sample row with the
conditions its decision was taken under, every reading row with its raw
bundle -- and from nothing else: no telemetry is queried, no model is
called, the wall clock is never consulted. Three layers are recomputed and
each compared with what is stored:

1. **readings**: every bundle is re-hashed against ``raw_sha256`` and the
   reading rebuilt from the exact response bytes (``sampler.replay_sample``);
2. **the sample's outcome**: the rebuilt readings are judged against the
   profile's thresholds, freshness and traffic gate at the ``sample_time``
   the bundle recorded (``evaluate_readings``);
3. **the session**: the recomputed outcomes are folded through the store's
   own decision procedure (``fold_history``): adoption, healthy streak,
   sustained window, budget, ending, lifecycle.

Any disagreement -- a bundle that no longer hashes, a reading, an outcome or
a decision that recomputes differently, a profile that does not reproduce
its revision -- is reported as ``STORED_OBSERVATION_INTEGRITY_MISMATCH``
with the verdict ``unknown``: the stored verdict is never repeated as the
replay's own. What the bundles actually say is still reported alongside
(``recomputed_verdict``) so a human can see which side moved.

``python -m opspilot.observer.replay --session <id>`` prints the result as
JSON and exits 0 when consistent, 1 when not, 2 on a usage error. It reads
the Observer's own DSN variable (the role has SELECT on every table the
replay needs and nothing else) or an explicit ``--dsn``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TextIO
from uuid import UUID

from pydantic import ValidationError

from opspilot.observation.store import (
    ObservationStore,
    ReplayedSample,
    ReplayReport,
    fold_history,
    profile_revision,
)
from opspilot.observer.health_profile import (
    HealthProfile,
    SampleEvaluation,
    canonical_content,
)
from opspilot.observer.sampler import (
    PROFILE_INVALID,
    PROFILE_SENTINEL,
    PROFILE_SENTINEL_SOURCE,
    PROFILE_UNAVAILABLE,
    replay_sample,
)
from opspilot.persistence.base import PersistenceError

__all__ = [
    "INTEGRITY_MISMATCH",
    "PROFILE_SENTINEL",
    "PROFILE_UNREADABLE",
    "ReplayedBasis",
    "ReplayedReading",
    "SessionReplay",
    "main",
    "replay",
    "replay_history",
    "replay_stored_session",
    "summary",
]

_log = logging.getLogger("opspilot.observer")

#: The one code the acceptance harness freezes for a replay that cannot
#: vouch for the stored basis (docs/testing/f6-acceptance-tests.md).
INTEGRITY_MISMATCH = "STORED_OBSERVATION_INTEGRITY_MISMATCH"
#: The stored profile content does not validate or does not reproduce the
#: session's revision: nothing can be recomputed against it.
PROFILE_UNREADABLE = "HEALTH_PROFILE_UNREADABLE"
DSN_VAR = "OPSPILOT_OBSERVER_DSN"

Verdict = str  # "healthy" | "degraded" | "unknown"


@dataclass(frozen=True)
class ReplayedReading:
    """One stored reading row next to its rebuild from the raw bundle."""

    signal_name: str
    query: str
    source: str
    window_start: datetime
    window_end: datetime
    raw_sha256: str | None
    # the bundle is present and hashes to ``raw_sha256`` (None: no bundle)
    raw_verified: bool | None
    # (status, value, sample_count) as stored vs. as rebuilt from the bundle
    stored: tuple[str, float | None, int | None]
    replayed: tuple[str, float | None, int | None] | None
    # the per-signal verdict and reason of the recomputed evaluation
    verdict: str | None = None
    reason: str | None = None

    @property
    def matches(self) -> bool:
        return self.replayed is not None and self.stored == self.replayed


@dataclass(frozen=True)
class ReplayedBasis:
    """One stored sample: its basis, its recomputation and its decision."""

    sample_id: UUID
    sequence: int
    window_start: datetime
    window_end: datetime
    submitted_at: datetime | None
    stored_outcome: str
    stored_required_signals_present: bool
    # recomputed from the bundles; None when nothing could be recomputed
    # (``recompute_skipped`` says why)
    replayed_outcome: str | None
    replayed_required_signals_present: bool | None
    replayed_reason: str | None
    recompute_skipped: str | None
    readings: tuple[ReplayedReading, ...]
    # the store's decision (disposition, reason, confirms_health, basis,
    # transition) as stored vs. as refolded on the recomputed outcome
    decision: ReplayedSample
    # codes naming every disagreement; empty when the sample reproduces
    integrity: tuple[str, ...] = ()

    @property
    def consistent(self) -> bool:
        return not self.integrity


@dataclass(frozen=True)
class SessionReplay:
    session_id: UUID
    incident_id: UUID
    health_profile_revision: str | None
    # the human handling the observation started from (``authorized_at``)
    handled_at: datetime
    deadline_at: datetime | None
    max_samples: int
    sustained_window_seconds: int
    samples: tuple[ReplayedBasis, ...]
    report: ReplayReport
    stored_session_state: str
    replayed_session_state: str
    # what the recomputed fold says on its own, before integrity is weighed
    recomputed_verdict: Verdict
    latest_sample_verdict: str | None
    # the replay's claim: ``unknown`` whenever the stored basis does not
    # reproduce, otherwise the recomputed verdict
    recovery_verdict: Verdict
    recovery_confirmed: bool
    healthy_window_seconds: int
    # lifecycle the replayed verdicts imply (None: no claim) vs. recorded
    expected_lifecycle: str | None
    recorded_lifecycle: str | None
    integrity: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    # The product's recovery reason codes (F6, #140), computed here once for
    # the online verdict and the replay alike -- the acceptance projection
    # only copies them: the integrity codes, then the per-signal verdicts
    # of the latest adopted sample (``INSUFFICIENT_TRAFFIC``,
    # ``REQUIRED_TELEMETRY_MISSING`` + ``MISSING_SIGNAL:<name>``,
    # ``STALE_TELEMETRY:<name>``, ``DEGRADED_SIGNAL:<name>``, the sentinel's
    # ``HEALTH_PROFILE_UNAVAILABLE`` / ``HEALTH_PROFILE_INVALID``),
    # ``CONTINUED_DEGRADATION`` when the verdict is degraded,
    # ``OBSERVATION_UNCONFIRMED`` when the session ended without confirming
    # and ``NO_HEALTH_PROFILE`` for a session without a revision.
    recovery_reasons: tuple[str, ...] = ()
    # the session's end as committed: state not ``authorized``, its
    # ``ended_reason``, and whether that end is a handoff to a human
    # (deadline or budget: C3 section 10 bounded continuation, then handoff)
    observation_ended: bool = False
    ended_reason: str | None = None
    handoff: bool = False
    # the ending code (upper case) followed by the recovery reasons; empty
    # when the session did not hand off
    handoff_reasons: tuple[str, ...] = ()
    # audits the harness reads: this replay issues neither
    external_queries: tuple[Any, ...] = field(default=())
    model_requests: tuple[Any, ...] = field(default=())

    @property
    def consistent(self) -> bool:
        return not self.integrity


_HANDOFF_ENDINGS = frozenset({"deadline_expired", "max_samples_exhausted"})
_UNUSABLE_VERDICTS = frozenset(
    {
        "missing",
        "no_data",
        "timeout",
        "failed",
        "query_mismatch",
        "insufficient_samples",
    }
)


def _signal_reasons(
    evaluation: SampleEvaluation | None, skipped: str | None
) -> list[str]:
    """Reason codes from one sample's recomputed per-signal verdicts (the
    required flag is the evaluation's own, from the frozen profile)."""
    if skipped in (PROFILE_UNAVAILABLE, PROFILE_INVALID):
        return ["REQUIRED_TELEMETRY_MISSING", str(skipped)]
    if evaluation is None:
        return []
    reasons: list[str] = []
    missing: list[str] = []
    for verdict in evaluation.verdicts:
        if not verdict.required:
            continue
        if verdict.verdict == "below_traffic_gate":
            reasons.append("INSUFFICIENT_TRAFFIC")
        elif verdict.verdict == "degraded":
            reasons.append(f"DEGRADED_SIGNAL:{verdict.signal_name}")
        elif verdict.verdict == "stale":
            missing.append(verdict.signal_name)
            reasons.append(f"STALE_TELEMETRY:{verdict.signal_name}")
        elif verdict.verdict in _UNUSABLE_VERDICTS:
            missing.append(verdict.signal_name)
    if missing:
        reasons.append("REQUIRED_TELEMETRY_MISSING")
        reasons.extend(f"MISSING_SIGNAL:{name}" for name in dict.fromkeys(missing))
    return reasons


def _load_profile(
    row: Mapping[str, Any] | None,
) -> tuple[HealthProfile | None, str | None]:
    """The validated profile behind a stored profile row, or why not.

    Fail closed on integrity as well as on shape: the content must hash to
    ``content_sha256`` (when the row carries it), that hash must name the
    revision, and the validated model must reproduce the revision from its
    canonical content.
    """
    if row is None:
        return None, None
    content = row.get("content")
    revision = row.get("health_profile_revision")
    if not isinstance(content, str) or not isinstance(revision, str):
        return None, PROFILE_UNREADABLE
    digest = hashlib.sha256(content.encode()).hexdigest()
    expected = row.get("content_sha256")
    if expected is not None and expected != digest:
        return None, PROFILE_UNREADABLE
    profile_id, _, _ = revision.rpartition("@")
    if profile_revision(profile_id, content) != revision:
        return None, PROFILE_UNREADABLE
    try:
        parsed = json.loads(content)
        profile = HealthProfile.model_validate(parsed)
    except (ValueError, ValidationError):
        return None, PROFILE_UNREADABLE
    if profile.revision != revision:
        return None, PROFILE_UNREADABLE
    return profile, None


def _triple(row: Mapping[str, Any]) -> tuple[str, float | None, int | None]:
    value = row.get("value")
    count = row.get("sample_count")
    return (
        str(row["status"]),
        None if value is None else float(value),
        None if count is None else int(count),
    )


def _basis_codes(
    profile: HealthProfile,
    row: Mapping[str, Any],
    *,
    verified: bool | None,
    sample_window: tuple[datetime, datetime],
) -> list[str]:
    """Why a reading row is not the basis it claims to be: its signal is not
    in the frozen profile, or its query, source or window are not the
    profile's and the sample's (and, when the bundle verifies, not the
    bundle's). A row is trusted for what it says only when every one of
    these is the frozen value (codex review of PR #139, P2-3)."""
    name = str(row["signal_name"])
    signal = profile.signal(name)
    if signal is None:
        return [f"UNKNOWN_SIGNAL:{name}"]
    codes: list[str] = []
    if row.get("query") != signal.query:
        codes.append(f"READING_BASIS_MISMATCH:{name}:query")
    if row.get("source") != profile.source:
        codes.append(f"READING_BASIS_MISMATCH:{name}:source")
    if (row.get("window_start"), row.get("window_end")) != sample_window:
        codes.append(f"READING_BASIS_MISMATCH:{name}:window")
    if verified:
        try:
            bundle = json.loads(bytes(row["raw"]))
            exprs = {
                kind: bundle[kind]["expr"]
                for kind in ("query", "coverage", "freshness")
            }
            window = (
                datetime.fromisoformat(str(bundle["window_start"])),
                datetime.fromisoformat(str(bundle["window_end"])),
            )
        except (ValueError, KeyError, TypeError):
            # a bundle without these fields was filed as failed by the
            # Observer (construction failure); the rebuild says so
            return codes
        # all three queries carry the verdict: the value, the point count
        # (minimum samples) and the newest raw sample (freshness) -- each
        # must be the frozen profile's (codex recheck of PR #139, P2-1)
        for kind, frozen in (
            ("query", signal.query),
            ("coverage", signal.coverage_query),
            ("freshness", signal.freshness_query),
        ):
            if exprs[kind] != frozen:
                codes.append(f"READING_BASIS_MISMATCH:{name}:bundle_{kind}")
        if window != sample_window:
            codes.append(f"READING_BASIS_MISMATCH:{name}:bundle_window")
    return codes


def _profile_unavailable_record(
    rows: Sequence[Mapping[str, Any]], revision: str
) -> str | None:
    """The reason recorded by the one sentinel reading of a sample the
    Observer filed without a usable profile (``sampler.submit_without_readings``),
    verified against its hash and bound to this session's revision; ``None``
    when the rows are anything else."""
    if len(rows) != 1:
        return None
    row = rows[0]
    raw = row.get("raw")
    if (
        str(row.get("signal_name")) != PROFILE_SENTINEL
        or str(row.get("source")) != PROFILE_SENTINEL_SOURCE
        or str(row.get("status")) != "failed"
        or raw is None
        or hashlib.sha256(bytes(raw)).hexdigest() != row.get("raw_sha256")
    ):
        return None
    try:
        bundle = json.loads(bytes(raw))
    except ValueError:
        return None
    if not isinstance(bundle, dict):
        return None
    reason = bundle.get("reading_error")
    if reason not in (PROFILE_UNAVAILABLE, PROFILE_INVALID):
        return None
    if (
        bundle.get("health_profile_revision") != revision
        or row.get("query") != revision
    ):
        return None
    return str(reason)


def _basis_only(
    rows: Sequence[Mapping[str, Any]], verified: Mapping[str, bool | None]
) -> tuple[ReplayedReading, ...]:
    return tuple(
        ReplayedReading(
            signal_name=str(row["signal_name"]),
            query=str(row.get("query")),
            source=str(row.get("source")),
            window_start=row["window_start"],
            window_end=row["window_end"],
            raw_sha256=row.get("raw_sha256"),
            raw_verified=verified[str(row["signal_name"])],
            stored=_triple(row),
            replayed=None,
        )
        for row in rows
    )


def _readings(
    profile: HealthProfile | None,
    rows: Sequence[Mapping[str, Any]],
    *,
    sample_window: tuple[datetime, datetime],
) -> tuple[tuple[ReplayedReading, ...], SampleEvaluation | None, list[str]]:
    """Rebuild one sample's readings and judge them; the codes name every
    reading whose bundle, basis or rebuild disagrees with its row. A row
    the domain cannot even parse (a value the column accepts but the DTO
    does not) is reported as ``BASIS_UNPARSABLE`` instead of raising
    (codex review of PR #139, P2-4)."""
    codes: list[str] = []
    verified: dict[str, bool | None] = {}
    for row in rows:
        name = str(row["signal_name"])
        raw = row.get("raw")
        if raw is None:
            verified[name] = None
            codes.append(f"RAW_MISSING:{name}")
            continue
        ok = hashlib.sha256(bytes(raw)).hexdigest() == row.get("raw_sha256")
        verified[name] = ok
        if not ok:
            codes.append(f"RAW_HASH_MISMATCH:{name}")
    if profile is None:
        return _basis_only(rows, verified), None, codes
    for row in rows:
        codes.extend(
            _basis_codes(
                profile,
                row,
                verified=verified[str(row["signal_name"])],
                sample_window=sample_window,
            )
        )
    try:
        rebuilt, evaluation = replay_sample(profile, rows)
    except Exception as exc:  # noqa: BLE001 - a bad row is a finding, not a crash
        codes.append(f"BASIS_UNPARSABLE:{type(exc).__name__}")
        return _basis_only(rows, verified), None, codes
    by_name = {reading.signal_name: reading for reading in rebuilt}
    verdicts = {verdict.signal_name: verdict for verdict in evaluation.verdicts}
    compared: list[ReplayedReading] = []
    for row in rows:
        name = str(row["signal_name"])
        reading = by_name.get(name)
        replayed = (
            None
            if reading is None
            else (reading.status, reading.value, reading.sample_count)
        )
        verdict = verdicts.get(name)
        item = ReplayedReading(
            signal_name=name,
            query=str(row["query"]),
            source=str(row["source"]),
            window_start=row["window_start"],
            window_end=row["window_end"],
            raw_sha256=row.get("raw_sha256"),
            raw_verified=verified[name],
            stored=_triple(row),
            replayed=replayed,
            verdict=None if verdict is None else str(verdict.verdict),
            reason=None if verdict is None else verdict.reason,
        )
        if not item.matches:
            codes.append(f"READING_MISMATCH:{name}")
        compared.append(item)
    return tuple(compared), evaluation, codes


def _sentinel_window_codes(
    row: Mapping[str, Any], *, sample_window: tuple[datetime, datetime]
) -> list[str]:
    codes: list[str] = []
    if (row.get("window_start"), row.get("window_end")) != sample_window:
        codes.append("SENTINEL_MISMATCH:window")
    try:
        bundle = json.loads(bytes(row["raw"]))
        window = (
            datetime.fromisoformat(str(bundle["window_start"])),
            datetime.fromisoformat(str(bundle["window_end"])),
        )
    except (ValueError, KeyError, TypeError):
        return codes + ["SENTINEL_MISMATCH:bundle_window"]
    if window != sample_window:
        codes.append("SENTINEL_MISMATCH:bundle_window")
    return codes


def _session_parameter_codes(
    row: Mapping[str, Any], profile: HealthProfile
) -> list[str]:
    """The session row's parameters must be the frozen profile's session
    values (interface contract item 5): the row cannot vouch for itself.
    The deadline is an absolute instant chosen at authorization; it is
    checked as the span from the row's creation, with a minute of skew
    between the authorizing clock and the database's."""
    frozen = profile.session
    codes: list[str] = []
    for name, expected in (
        ("max_samples", frozen.max_samples),
        ("sustained_window_seconds", frozen.sustained_window_seconds),
        ("sample_interval_seconds", frozen.sample_interval_seconds),
    ):
        if name in row and int(row[name]) != expected:
            codes.append(f"SESSION_PARAMETER_MISMATCH:{name}")
    deadline = row.get("deadline_at")
    created = row.get("created_at") or row.get("authorized_at")
    if isinstance(deadline, datetime) and isinstance(created, datetime):
        span = (deadline - created).total_seconds()
        if span <= 0 or span > frozen.deadline_seconds + 60:
            codes.append("SESSION_PARAMETER_MISMATCH:deadline_at")
    return codes


def replay_history(history: Mapping[str, Any]) -> SessionReplay:
    """Recompute one session from its stored history alone.

    ``history`` is what ``ObservationStore.session_history`` returns (or the
    same shape built from the frozen artifacts by ``replay``). Pure: no
    I/O, no clock.
    """
    row = history["session"]
    revision = row.get("health_profile_revision")
    profile, profile_error = _load_profile(
        history.get("health_profile") if revision is not None else None
    )
    stored_samples: Sequence[Mapping[str, Any]] = history["samples"]
    partial: list[dict[str, Any]] = []
    outcomes: dict[UUID, tuple[str, bool]] = {}
    for stored in stored_samples:
        rows = list(stored.get("readings") or ())
        stored_outcome = str(stored["outcome"])
        window = (stored["window_start"], stored["window_end"])
        skipped: str | None = None
        evaluation: SampleEvaluation | None = None
        codes: list[str] = []
        if revision is None:
            skipped = "NO_HEALTH_PROFILE"
            readings, _, _ = _readings(None, rows, sample_window=window)
        elif profile is None:
            skipped = PROFILE_UNREADABLE
            readings, _, _ = _readings(None, rows, sample_window=window)
        else:
            # An adopted sample under a readable profile carries a reading
            # for every profile signal: the Observer queries them all, and
            # the only paths that file a sample without readings are a
            # session without a revision or a profile that did not validate
            # -- neither holds here, and no sample column says otherwise.
            # Missing rows are therefore a damaged basis whatever the
            # stored verdict says (codex review of PR #139, P2-1). A sample
            # filed as history only (e.g. interrupted by a scope change) is
            # legitimately partial and is not held to this.
            adopted = str(stored.get("disposition")) == "adopted"
            present = {str(r["signal_name"]) for r in rows}
            # The one exemption: the Observer could not use the profile row
            # at sampling time and said so in a hashed sentinel reading
            # (codex recheck of PR #139, P2-4).
            recorded = _profile_unavailable_record(rows, str(revision))
            if recorded is not None:
                skipped = recorded
                readings, _, more = _readings(None, rows, sample_window=window)
                codes.extend(more)
                # the sentinel path writes exactly this verdict and binds
                # its reading to the sample window like any other (#141)
                if stored_outcome != "failed":
                    codes.append("SENTINEL_MISMATCH:outcome")
                if bool(stored["required_signals_present"]):
                    codes.append("SENTINEL_MISMATCH:required_signals_present")
                codes.extend(_sentinel_window_codes(rows[0], sample_window=window))
            elif not rows:
                skipped = "NO_READINGS"
                readings = ()
                if adopted:
                    codes.append("NO_READINGS")
            else:
                if adopted:
                    codes.extend(
                        f"READING_MISSING:{signal.name}"
                        for signal in profile.signals
                        if signal.name not in present
                    )
                readings, evaluation, more = _readings(
                    profile, rows, sample_window=window
                )
                codes.extend(more)
                if evaluation is not None:
                    outcomes[stored["sample_id"]] = (
                        evaluation.outcome,
                        evaluation.required_signals_present,
                    )
                    if (evaluation.outcome, evaluation.required_signals_present) != (
                        stored_outcome,
                        bool(stored["required_signals_present"]),
                    ):
                        codes.append("OUTCOME_MISMATCH")
                else:
                    skipped = "BASIS_UNPARSABLE"
        partial.append(
            {
                "readings": readings,
                "evaluation": evaluation,
                "skipped": skipped,
                "codes": codes,
            }
        )
    report = fold_history(history, outcomes=outcomes or None)
    samples: list[ReplayedBasis] = []
    for stored, item, decision in zip(
        stored_samples, partial, report.samples, strict=True
    ):
        codes = list(item["codes"])
        if decision.deadline_mismatch:
            codes.append("DEADLINE_MISMATCH")
        if decision.scope_mismatch:
            codes.append("SCOPE_MISMATCH")
        if decision.stored != decision.replayed:
            codes.append("DECISION_MISMATCH")
        if decision.signal_mismatches and decision.stored[1] != "readings_inconsistent":
            codes.append("READINGS_INCONSISTENT")
        evaluation = item["evaluation"]
        samples.append(
            ReplayedBasis(
                sample_id=stored["sample_id"],
                sequence=int(stored["sequence"]),
                window_start=stored["window_start"],
                window_end=stored["window_end"],
                submitted_at=stored.get("submitted_at"),
                stored_outcome=str(stored["outcome"]),
                stored_required_signals_present=bool(
                    stored["required_signals_present"]
                ),
                replayed_outcome=None if evaluation is None else evaluation.outcome,
                replayed_required_signals_present=(
                    None if evaluation is None else evaluation.required_signals_present
                ),
                replayed_reason=None if evaluation is None else evaluation.reason,
                recompute_skipped=item["skipped"],
                readings=item["readings"],
                decision=decision,
                integrity=tuple(dict.fromkeys(codes)),
            )
        )

    integrity: list[str] = []
    if profile_error is not None:
        integrity.append(profile_error)
    if profile is not None:
        parameter_codes = _session_parameter_codes(row, profile)
        if parameter_codes:
            integrity.append(INTEGRITY_MISMATCH)
            integrity.extend(parameter_codes)
    if any(sample.integrity for sample in samples) or not report.consistent:
        integrity.append(INTEGRITY_MISMATCH)
    if not report.ending_consistent:
        integrity.append("ENDING_MISMATCH")
    if not report.session_consistent:
        integrity.append("SESSION_STATE_MISMATCH")
    if not report.lifecycle_consistent:
        integrity.append("LIFECYCLE_MISMATCH")
    if any(item.deadline_mismatch for item in report.samples):
        integrity.append("DEADLINE_MISMATCH")
    # The session row's watermarks are the fold's own output: a deleted
    # sample or a rewritten count/sequence/window end/streak start shows up
    # here (codex review of PR #139, P2-2).
    stored_marks = (
        row.get("adopted_sequence"),
        row.get("adopted_window_end"),
        row.get("adopted_count"),
        row.get("healthy_since"),
    )
    replayed_marks = (
        report.adopted_sequence,
        report.adopted_window_end,
        report.adopted_count,
        report.healthy_since,
    )
    if (
        any(
            stored is not None and stored != replayed
            for stored, replayed in zip(stored_marks, replayed_marks, strict=True)
        )
        or (
            row.get("adopted_window_end") is None
            and "adopted_window_end" in row
            and report.adopted_window_end is not None
        )
        or (
            row.get("healthy_since") is None
            and "healthy_since" in row
            and report.healthy_since is not None
        )
    ):
        integrity.append("WATERMARK_MISMATCH")
        if INTEGRITY_MISMATCH not in integrity:
            integrity.insert(0, INTEGRITY_MISMATCH)

    # What the recomputed fold says: confirmed when a replayed verdict
    # confirmed recovery; else the latest adopted sample's outcome.
    confirmed = any(item.replayed[4] == "recovery_confirmed" for item in report.samples)
    latest: str | None = None
    latest_reason: str | None = None
    latest_signal_reasons: list[str] = []
    for sample, item, decision in zip(samples, partial, report.samples, strict=True):
        if decision.replayed[0] == "adopted":
            latest = sample.replayed_outcome or sample.stored_outcome
            latest_reason = sample.replayed_reason
            latest_signal_reasons = _signal_reasons(item["evaluation"], item["skipped"])
    if revision is None:
        # no HealthProfile: recovery can never be judged, in either
        # direction (PRODUCT-CONSTRAINTS "Recovery observations")
        recomputed: Verdict = "unknown"
    elif confirmed:
        recomputed = "healthy"
    elif latest == "degraded":
        recomputed = "degraded"
    else:
        recomputed = "unknown"
    verdict: Verdict = "unknown" if integrity else recomputed

    reasons: list[str] = list(integrity)
    if revision is None:
        reasons.append("NO_HEALTH_PROFILE")
    if not integrity:
        if row.get("ended_reason"):
            reasons.append(str(row["ended_reason"]))
        if latest_reason:
            reasons.append(latest_reason)
    ended = str(row.get("state")) != "authorized"
    ended_reason = None if row.get("ended_reason") is None else str(row["ended_reason"])
    recovery_reasons: list[str] = list(integrity)
    recovery_reasons.extend(latest_signal_reasons)
    if verdict == "degraded":
        recovery_reasons.append("CONTINUED_DEGRADATION")
    if ended and verdict != "healthy":
        recovery_reasons.append("OBSERVATION_UNCONFIRMED")
    if revision is None:
        recovery_reasons.append("NO_HEALTH_PROFILE")
    handoff = ended and ended_reason in _HANDOFF_ENDINGS
    return SessionReplay(
        session_id=row["session_id"],
        incident_id=row["incident_id"],
        health_profile_revision=revision,
        handled_at=row["authorized_at"],
        deadline_at=row.get("deadline_at"),
        max_samples=int(row["max_samples"]),
        sustained_window_seconds=int(row["sustained_window_seconds"]),
        samples=tuple(samples),
        report=report,
        stored_session_state=report.stored_session_state,
        replayed_session_state=report.replayed_session_state,
        recomputed_verdict=recomputed,
        latest_sample_verdict=latest,
        recovery_verdict=verdict,
        recovery_confirmed=verdict == "healthy",
        healthy_window_seconds=0 if integrity else report.healthy_window_seconds,
        expected_lifecycle=None if integrity else report.expected_lifecycle,
        recorded_lifecycle=report.recorded_lifecycle,
        integrity=tuple(integrity),
        reasons=tuple(dict.fromkeys(reasons)),
        recovery_reasons=tuple(dict.fromkeys(recovery_reasons)),
        observation_ended=ended,
        ended_reason=ended_reason,
        handoff=handoff,
        handoff_reasons=(
            tuple(dict.fromkeys((str(ended_reason).upper(), *recovery_reasons)))
            if handoff
            else ()
        ),
    )


def replay(
    profile: HealthProfile | str | Mapping[str, Any],
    handled_at: datetime,
    samples: Sequence[Mapping[str, Any]],
    *,
    session: Mapping[str, Any],
    endings: Sequence[Mapping[str, Any]] | None = None,
    allow_telemetry: bool = False,
    allow_model: bool = False,
) -> SessionReplay:
    """The F6 seam: replay from the frozen profile, the handling time and
    the persisted samples (``docs/testing/f6-acceptance-tests.md``).

    ``profile`` is the frozen content (canonical JSON text, its parsed
    mapping, or the validated model); it must be the revision the session
    was authorized under, otherwise the replay reports
    ``HEALTH_PROFILE_UNREADABLE``. ``session`` is the stored session row
    (its parameters bound the fold); ``handled_at`` replaces its
    ``authorized_at``. The flags exist for the harness only: this replay is
    offline by construction and refuses to be told otherwise.
    """
    if allow_telemetry or allow_model:
        raise ValueError("REPLAY_IS_OFFLINE")
    if isinstance(profile, HealthProfile):
        content = canonical_content(profile)
    elif isinstance(profile, str):
        content = profile
    elif isinstance(profile, Mapping):
        content = json.dumps(
            profile, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
    else:
        raise ValueError("INVALID_PROFILE")
    revision = session.get("health_profile_revision")
    try:
        profile_id = str(json.loads(content).get("profile_id", ""))
    except (ValueError, AttributeError):
        profile_id = ""
    history = {
        "session": {**session, "authorized_at": handled_at},
        "incident_lifecycle": None,
        "health_profile": None
        if revision is None
        else {
            "health_profile_revision": revision,
            "profile_id": profile_id,
            "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "content": content,
        },
        "samples": list(samples),
        "endings": list(endings or ()),
    }
    return replay_history(history)


def replay_stored_session(store: ObservationStore, session_id: UUID) -> SessionReplay:
    """Replay one session from the database (one snapshot read, nothing else)."""
    return replay_history(store.session_history(session_id))


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return value


def summary(result: SessionReplay) -> dict[str, Any]:
    """The replay as a JSON document (no raw bytes: hashes only)."""
    document: dict[str, Any] = _jsonable(
        {
            "session_id": result.session_id,
            "incident_id": result.incident_id,
            "health_profile_revision": result.health_profile_revision,
            "handled_at": result.handled_at,
            "deadline_at": result.deadline_at,
            "max_samples": result.max_samples,
            "sustained_window_seconds": result.sustained_window_seconds,
            "consistent": result.consistent,
            "integrity": result.integrity,
            "reasons": result.reasons,
            "recovery_verdict": result.recovery_verdict,
            "recovery_confirmed": result.recovery_confirmed,
            "recomputed_verdict": result.recomputed_verdict,
            "latest_sample_verdict": result.latest_sample_verdict,
            "healthy_window_seconds": result.healthy_window_seconds,
            "expected_lifecycle": result.expected_lifecycle,
            "recorded_lifecycle": result.recorded_lifecycle,
            "stored_session_state": result.stored_session_state,
            "replayed_session_state": result.replayed_session_state,
            "external_queries": result.external_queries,
            "model_requests": result.model_requests,
            "samples": [
                {
                    "sample_id": sample.sample_id,
                    "sequence": sample.sequence,
                    "window_start": sample.window_start,
                    "window_end": sample.window_end,
                    "submitted_at": sample.submitted_at,
                    "stored_outcome": sample.stored_outcome,
                    "stored_required_signals_present": sample.stored_required_signals_present,
                    "replayed_outcome": sample.replayed_outcome,
                    "replayed_required_signals_present": sample.replayed_required_signals_present,
                    "replayed_reason": sample.replayed_reason,
                    "recompute_skipped": sample.recompute_skipped,
                    "consistent": sample.consistent,
                    "integrity": sample.integrity,
                    "decision": {
                        "stored": sample.decision.stored,
                        "replayed": sample.decision.replayed,
                        "raw_mismatches": sample.decision.raw_mismatches,
                        "signal_mismatches": sample.decision.signal_mismatches,
                    },
                    "readings": [
                        {
                            "signal_name": reading.signal_name,
                            "query": reading.query,
                            "source": reading.source,
                            "window_start": reading.window_start,
                            "window_end": reading.window_end,
                            "raw_sha256": reading.raw_sha256,
                            "raw_verified": reading.raw_verified,
                            "stored": reading.stored,
                            "replayed": reading.replayed,
                            "verdict": reading.verdict,
                            "reason": reading.reason,
                        }
                        for reading in sample.readings
                    ],
                }
                for sample in result.samples
            ],
        }
    )
    return document


def main(
    argv: Sequence[str] | None = None,
    *,
    store_factory: Callable[[str], Any] | None = None,
    stdout: TextIO | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """``--session <id>`` (repeatable) or ``--incident <id>``; JSON list on
    stdout; exit 0 when every replayed session is consistent, 1 when any
    is not, 2 on a usage or storage error."""
    env = os.environ if env is None else env
    out = stdout or sys.stdout
    parser = argparse.ArgumentParser(
        prog="python -m opspilot.observer.replay",
        description="Replay stored observation sessions offline.",
    )
    parser.add_argument("--dsn", default=env.get(DSN_VAR) or None)
    parser.add_argument("--session", action="append", default=[])
    parser.add_argument("--incident", default=None)
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return 2 if exc.code else 0
    if not args.dsn:
        print(f"--dsn or {DSN_VAR} is required", file=sys.stderr)
        return 2
    if not args.session and not args.incident:
        print("--session or --incident is required", file=sys.stderr)
        return 2
    try:
        session_ids = [UUID(item) for item in args.session]
        incident_id = None if args.incident is None else UUID(args.incident)
    except ValueError:
        print("ids must be UUIDs", file=sys.stderr)
        return 2
    store = (store_factory or ObservationStore)(args.dsn)
    try:
        if incident_id is not None:
            session_ids.extend(
                row["session_id"] for row in store.incident_sessions(incident_id)
            )
        documents: list[dict[str, Any]] = []
        for sid in session_ids:
            try:
                documents.append(summary(replay_stored_session(store, sid)))
            except PersistenceError:
                raise
            except Exception as exc:  # noqa: BLE001 - report, never crash
                # stored rows the replay cannot even fold (a value the
                # column accepts but the domain does not): an explicit
                # failure is the result, not a traceback (codex P2-4)
                _log.warning(
                    "replay failed session=%s error=%s", sid, type(exc).__name__
                )
                documents.append(_failure_document(sid, exc))
    except PersistenceError as exc:
        print(f"storage: {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()
    print(json.dumps(documents, indent=2), file=out)
    return 0 if all(item["consistent"] for item in documents) else 1


def _failure_document(session_id: UUID, exc: BaseException) -> dict[str, Any]:
    code = f"REPLAY_FAILED:{type(exc).__name__}"
    return {
        "session_id": str(session_id),
        "consistent": False,
        "integrity": [INTEGRITY_MISMATCH, code],
        "reasons": [INTEGRITY_MISMATCH, code],
        "recovery_verdict": "unknown",
        "recovery_confirmed": False,
        "recomputed_verdict": "unknown",
        "samples": [],
        "external_queries": [],
        "model_requests": [],
    }


if __name__ == "__main__":
    sys.exit(main())
