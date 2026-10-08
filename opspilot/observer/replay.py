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
from opspilot.observer.sampler import replay_sample
from opspilot.persistence.base import PersistenceError

__all__ = [
    "INTEGRITY_MISMATCH",
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
    # audits the harness reads: this replay issues neither
    external_queries: tuple[Any, ...] = field(default=())
    model_requests: tuple[Any, ...] = field(default=())

    @property
    def consistent(self) -> bool:
        return not self.integrity


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


def _readings(
    profile: HealthProfile | None,
    rows: Sequence[Mapping[str, Any]],
) -> tuple[tuple[ReplayedReading, ...], SampleEvaluation | None, list[str]]:
    """Rebuild one sample's readings and judge them; the codes name every
    reading whose bundle or rebuild disagrees with its row."""
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
        readings = tuple(
            ReplayedReading(
                signal_name=str(row["signal_name"]),
                query=str(row["query"]),
                source=str(row["source"]),
                window_start=row["window_start"],
                window_end=row["window_end"],
                raw_sha256=row.get("raw_sha256"),
                raw_verified=verified[str(row["signal_name"])],
                stored=_triple(row),
                replayed=None,
            )
            for row in rows
        )
        return readings, None, codes
    rebuilt, evaluation = replay_sample(profile, rows)
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
        skipped: str | None = None
        evaluation: SampleEvaluation | None = None
        codes: list[str] = []
        if revision is None:
            skipped = "NO_HEALTH_PROFILE"
            readings, _, _ = _readings(None, rows)
        elif profile is None:
            skipped = PROFILE_UNREADABLE
            readings, _, _ = _readings(None, rows)
        elif not rows:
            # nothing to recompute from; a healthy claim without readings
            # is a tampered or truncated basis, anything else was filed
            # without readings on purpose (no usable profile at the time)
            skipped = "NO_READINGS"
            readings = ()
            if stored_outcome == "healthy":
                codes.append("NO_READINGS")
        else:
            readings, evaluation, codes = _readings(profile, rows)
            assert evaluation is not None
            outcomes[stored["sample_id"]] = (
                evaluation.outcome,
                evaluation.required_signals_present,
            )
            if (evaluation.outcome, evaluation.required_signals_present) != (
                stored_outcome,
                bool(stored["required_signals_present"]),
            ):
                codes.append("OUTCOME_MISMATCH")
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
    if any(sample.integrity for sample in samples) or not report.consistent:
        integrity.append(INTEGRITY_MISMATCH)
    if not report.ending_consistent:
        integrity.append("ENDING_MISMATCH")
    if not report.session_consistent:
        integrity.append("SESSION_STATE_MISMATCH")
    if not report.lifecycle_consistent:
        integrity.append("LIFECYCLE_MISMATCH")

    # What the recomputed fold says: confirmed when a replayed verdict
    # confirmed recovery; else the latest adopted sample's outcome.
    confirmed = any(item.replayed[4] == "recovery_confirmed" for item in report.samples)
    latest: str | None = None
    latest_reason: str | None = None
    for sample, decision in zip(samples, report.samples, strict=True):
        if decision.replayed[0] == "adopted":
            latest = sample.replayed_outcome or sample.stored_outcome
            latest_reason = sample.replayed_reason
    if confirmed:
        recomputed: Verdict = "healthy"
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
        results = [replay_stored_session(store, sid) for sid in session_ids]
    except PersistenceError as exc:
        print(f"storage: {exc}", file=sys.stderr)
        return 2
    finally:
        store.close()
    print(json.dumps([summary(item) for item in results], indent=2), file=out)
    return 0 if all(item.consistent for item in results) else 1


if __name__ == "__main__":
    sys.exit(main())
