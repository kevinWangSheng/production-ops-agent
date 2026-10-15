"""Bounded engineer-only U2 lab harness (r1/L3); run with python -m scripts.f11_u2_harness.

See docs/development.md for commands and input contracts. No product authorization,
calibration catalog writes, chart rebuilds or model calls. Raw data stays in tmp/.
Candidate contexts contain explicitly UNAPPROVED synthetic policy metadata solely
because the product pure evaluator requires a typed policy; never import these
contexts into the Controller or the production calibration catalog.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import subprocess
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from opspilot.domain.evidence import QueryWindow
from opspilot.observation.release_policy import (
    ReleasePolicy,
    canonical_json,
    sha256,
)
from opspilot.observation.release_types import ReleaseQueryResult, ReleaseSampleEvidence
from opspilot.observation.releases import ReleaseEvent, ReleaseRecord, ReleaseStore
from opspilot.observation.store import OBSERVER_LEASE_SECONDS
from opspilot.observer.health_profile import (
    HealthProfile,
    canonical_content,
    load_health_profile,
)
from opspilot.observer.prometheus import PrometheusReadOnlySource, VectorResult
from opspilot.observer.release_evaluation import (
    ReleaseEvaluationContext,
    advance_release_window,
    build_release_query_plan,
    evaluate_release_sample,
    query_evidence_consistent,
)
from opspilot.observer.sampler import SUBMIT_MARGIN_SECONDS
from opspilot.persistence import DurableStore, PersistenceError, PoolConfig
from opspilot.schema import load_target_identities
from scripts import kind_lab

ROOT = Path(__file__).resolve().parents[1]
LEDGERS = ROOT / "tmp/lab-ledgers"
SUMMARY = ROOT / "docs/evidence/f11-u2-calibration/summary.json"
TARGET_KEYS = ("integration_id", "cluster_uid", "namespace", "resource_uid", "workload")
AD_FLAG = "adFailure"  # Lead verified in live flagd-config/demo.flagd.json.
CONTRACT = "docs/tasks/2026-10-13-f11-pr2-contract.md"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("UTC_OFFSET_REQUIRED")
    return parsed.astimezone(timezone.utc)


def write_json(path: Path, value: Any, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode()
    if exclusive:
        with path.open("xb") as stream:
            os.chmod(path, 0o600)
            stream.write(data)
    else:
        temporary = path.with_name(path.name + f".{uuid4().hex}.tmp")
        with temporary.open("xb") as stream:
            os.chmod(temporary, 0o600)
            stream.write(data)
        temporary.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_bytes())


def append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        os.chmod(path, 0o600)
        stream.write(canonical_json(event) + "\n")


@contextmanager
def local_guard(name: str, *, blocking: bool = True) -> Iterator[None]:
    LEDGERS.mkdir(parents=True, exist_ok=True)
    with (LEDGERS / f".{name}.guard").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise ValueError("LAB_OPERATION_IN_PROGRESS") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def occupation(action: str, holder: str, batch: str, until: datetime | None = None):
    """Expired reservations remain visible and require owner release; no stealing."""
    with local_guard("occupation"):
        path = LEDGERS / "occupation.json"
        record = read_json(path) if path.exists() else None
        if action == "show":
            return record
        if not holder or not batch:
            raise ValueError("HOLDER_AND_BATCH_REQUIRED")
        if record and (record["holder"], record["batch"]) != (holder, batch):
            raise ValueError("LAB_OCCUPIED")
        if action == "claim":
            if record:
                raise ValueError("LAB_ALREADY_RESERVED")
            if until is None or until <= utc_now():
                raise ValueError("FUTURE_END_REQUIRED")
            record = dict(
                holder=holder,
                started_at=utc_now().isoformat(),
                expected_end=until.isoformat(),
                batch=batch,
            )
            write_json(path, record, exclusive=True)
        elif action == "release":
            if not record:
                raise ValueError("LAB_NOT_RESERVED")
            # No release while a live operation (possibly scheduled) is in flight.
            if (
                any(LEDGERS.glob(".active-*.json"))
                or (LEDGERS / "fault-active.json").exists()
            ):
                raise ValueError("LAB_OPERATION_IN_PROGRESS")
            path.unlink()
        elif action == "check":
            if not record:
                raise ValueError("LAB_RESERVATION_REQUIRED")
        else:
            raise ValueError("INVALID_OCCUPATION_ACTION")
        return record


@contextmanager
def reserved(holder: str, batch: str) -> Iterator[None]:
    marker = LEDGERS / f".active-{uuid4().hex}.json"
    with local_guard("occupation"):
        record = (
            read_json(LEDGERS / "occupation.json")
            if (LEDGERS / "occupation.json").exists()
            else None
        )
        if not record or (record["holder"], record["batch"]) != (holder, batch):
            raise ValueError("LAB_OCCUPIED_OR_NOT_RESERVED")
        write_json(
            marker, dict(holder=holder, batch=batch, pid=os.getpid()), exclusive=True
        )
    try:
        yield
    finally:
        marker.unlink()


@dataclass(frozen=True)
class CollectedSample:
    evidence: ReleaseSampleEvidence
    claimed_at: datetime
    decision_at: datetime
    elapsed_seconds: float
    query_timings: tuple[dict[str, Any], ...]


def collect_sample(
    context: ReleaseEvaluationContext,
    source: Any,
    ledger: Path,
    *,
    clock: Callable[[], datetime] = utc_now,
    monotonic: Callable[[], float] = time.monotonic,
) -> CollectedSample:
    """Public plan/evaluator only; identical serial phases to release_sampling."""
    started = monotonic()
    claimed = clock()
    end = clock()
    deadline = started + OBSERVER_LEASE_SECONDS - SUBMIT_MARGIN_SECONDS
    sample_id = uuid4()
    folder = ledger / "raw" / str(sample_id)
    folder.mkdir(parents=True, exist_ok=False)
    gathered: list[ReleaseQueryResult] = []
    timings: list[dict[str, Any]] = []
    window = QueryWindow(
        start=end - timedelta(seconds=context.health_profile.evaluation_window_seconds),
        end=end,
    )

    def evidence() -> ReleaseSampleEvidence:
        return ReleaseSampleEvidence(
            format_version=1,
            sample_id=sample_id,
            window=window,
            queries=tuple(gathered),
        )

    def collect(plan: dict[str, str]) -> None:
        for key, expr in plan.items():
            before = monotonic()
            left = deadline - before
            issued = left >= 1
            timeout = (
                min(context.health_profile.query_timeout_seconds, int(left))
                if issued
                else 0
            )
            if issued:
                result = source.instant_vector(expr, at=end, timeout_seconds=timeout)
            else:
                result = VectorResult(
                    expr=expr,
                    status="timeout",
                    series=(),
                    body=b"",
                    http_status=None,
                    detail="LEASE_BUDGET",
                    body_complete=False,
                    sent_at=None,
                )
            query = ReleaseQueryResult(
                query_key=key,
                query=expr,
                source="prometheus",
                evaluated_at=end,
                sent_at=result.sent_at,
                status=result.status,
                http_status=result.http_status,
                detail=result.detail,
                body_complete=result.body_complete,
                raw=result.body,
                raw_sha256=sha256(result.body),
            )
            raw_file = folder / f"{len(gathered):02d}.bin"
            raw_file.write_bytes(query.raw)
            os.chmod(raw_file, 0o600)
            timings.append(
                dict(
                    query_key=key,
                    query=expr,
                    evaluated_at=end.isoformat(),
                    issued=issued,
                    timeout_seconds=timeout,
                    elapsed_seconds=monotonic() - before,
                    raw_path=str(raw_file.relative_to(ledger)),
                    raw_sha256=query.raw_sha256,
                    raw_bytes=len(query.raw),
                    status=query.status,
                    sent_at=query.sent_at.isoformat() if query.sent_at else None,
                    http_status=query.http_status,
                    body_complete=query.body_complete,
                )
            )
            gathered.append(query)
            # Persist each response before proceeding, including failures/partial bodies.
            write_json(
                folder / f"{len(gathered) - 1:02d}.json",
                query.model_dump(mode="json", exclude={"raw"}),
            )

    base = build_release_query_plan(context, window_end=end, instance_uids=())
    collect(base)
    mapped = evaluate_release_sample(context, evidence(), decision_at=end)
    full = build_release_query_plan(
        context,
        window_end=end,
        instance_uids=tuple(i.pod_uid for i in mapped.instances),
    )
    collect({key: expr for key, expr in full.items() if key not in base})
    return CollectedSample(
        evidence(), claimed, clock(), monotonic() - started, tuple(timings)
    )


def save_sample(sample: CollectedSample, ledger: Path, sequence: int) -> None:
    write_json(
        ledger / "samples" / f"{sequence:03d}.json",
        dict(
            sequence=sequence,
            sample_id=str(sample.evidence.sample_id),
            window=sample.evidence.window.model_dump(mode="json"),
            claimed_at=sample.claimed_at.isoformat(),
            decision_at=sample.decision_at.isoformat(),
            elapsed_seconds=sample.elapsed_seconds,
            queries=list(sample.query_timings),
        ),
        exclusive=True,
    )


def load_samples(ledger: Path) -> list[CollectedSample]:
    samples = []
    for path in sorted((ledger / "samples").glob("*.json")):
        row = read_json(path)
        queries = []
        for timing in row["queries"]:
            raw_path = (ledger / timing["raw_path"]).resolve()
            if not raw_path.is_relative_to(ledger.resolve()):
                raise ValueError("RAW_PATH_OUTSIDE_LEDGER")
            raw = raw_path.read_bytes()
            if sha256(raw) != timing["raw_sha256"]:
                raise ValueError("RAW_SHA256_MISMATCH")
            meta = read_json(raw_path.with_suffix(".json"))
            queries.append(
                ReleaseQueryResult.model_validate(
                    {
                        **meta,
                        "raw": raw,
                        "evaluated_at": utc(meta["evaluated_at"]),
                        "sent_at": utc(meta["sent_at"]) if meta["sent_at"] else None,
                    }
                )
            )
        evidence = ReleaseSampleEvidence(
            format_version=1,
            sample_id=UUID(row["sample_id"]),
            window=QueryWindow(
                start=utc(row["window"]["start"]), end=utc(row["window"]["end"])
            ),
            queries=tuple(queries),
        )
        samples.append(
            CollectedSample(
                evidence,
                utc(row["claimed_at"]),
                utc(row["decision_at"]),
                row["elapsed_seconds"],
                tuple(row["queries"]),
            )
        )
    return samples


def fold_samples(
    context: ReleaseEvaluationContext,
    samples: list[CollectedSample],
    candidates: list[int],
    *,
    capture_end: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Reuse the product fold; only schedule/adoption/deadline envelope lives here.

    Every envelope rule below cites PR-2 contract line numbers. No simulated
    fast-forward, shifted sample time or average-cost projection is accepted.
    """
    results = {}
    if context.release.used_sample_count:
        raise ValueError("HARNESS_REQUIRES_NEW_BOUNDED_SESSION")
    for minimum in candidates:
        if (
            minimum <= 0
            or max(minimum, context.release.sustained_window_seconds)
            >= context.release.deadline_seconds
        ):
            raise ValueError("INVALID_CANDIDATE_M")  # §4:451
        content = context.policy.content.model_copy(
            update={"min_tracking_seconds": minimum}
        )
        digest = sha256(canonical_json(content.model_dump(mode="json")).encode())
        policy = ReleasePolicy(
            policy_id=context.policy.policy_id,
            revision=f"{context.policy.policy_id}@{digest[:12]}",
            content_sha256=digest,
            content=content,
        )
        candidate = context.model_copy(
            update={
                "release": context.release.model_copy(
                    update={
                        "min_tracking_seconds": minimum,
                        "calibrated_policy_revision": policy.revision,
                        "calibrated_policy_content": canonical_json(
                            content.model_dump(mode="json")
                        ),
                        "calibrated_policy_sha256": digest,
                    }
                ),
                "policy": policy,
            }
        )
        marks: dict[str, Any] = {}
        used = 0  # §4:458-459: one bounded session, no budget restart
        due = context.authorized_at + timedelta(
            seconds=context.release.sample_interval_seconds
        )  # §4:439,461; U2 measurement 3
        status = reason = terminal_at = None
        rows = []
        for sample in samples:
            evidence, decision = sample.evidence, sample.decision_at
            if status:
                break
            if (
                sample.claimed_at < due
                or evidence.window.end < sample.claimed_at
                or decision < evidence.window.end
            ):
                raise ValueError("INVALID_SAMPLE_SCHEDULE")  # §4:445,448,461
            if (
                sample.claimed_at >= candidate.release.deadline_at
                or decision >= candidate.release.deadline_at
            ):  # §4:450
                status, reason, terminal_at = (
                    "unknown",
                    "deadline_expired",
                    candidate.release.deadline_at,
                )
                break
            evaluation = evaluate_release_sample(
                candidate, evidence, decision_at=decision
            )
            adopted = True
            adoption_reason = "adopted"
            if decision >= sample.claimed_at + timedelta(
                seconds=OBSERVER_LEASE_SECONDS
            ):  # §8:625-632
                adopted, adoption_reason = False, "lease_revoked"
            elif not query_evidence_consistent(
                candidate, evidence, decision
            ):  # §7:619; r7:39
                adopted, adoption_reason = False, "readings_inconsistent"
            gap = (
                marks.get("tracking_until") is not None
                and evidence.window.start > marks["tracking_until"]
            )
            if adopted:
                used += 1  # §4:458: includes legitimate unknown/timeout/failed
                marks, status, reason = advance_release_window(
                    candidate,
                    marks,
                    evaluation,
                    window_start=evidence.window.start,
                    window_end=evidence.window.end,
                    decision_at=decision,
                    used_sample_count=used,
                )
                if status:
                    terminal_at = decision
            if decision < sample.claimed_at + timedelta(seconds=OBSERVER_LEASE_SECONDS):
                due = decision + timedelta(
                    seconds=candidate.release.sample_interval_seconds
                )
                # §4:461; §8:632-634; release_samples.py:512-517:
                # valid history_only requeues the same job at decision+I too.
            rows.append(
                dict(
                    sample_id=str(evidence.sample_id),
                    window=evidence.window.model_dump(mode="json"),
                    decision_at=decision.isoformat(),
                    adopted=adopted,
                    adoption_reason=adoption_reason,
                    coverage_gap=gap,
                    evaluation=evaluation.model_dump(mode="json"),
                    used_sample_count=used,
                    marks={k: v.isoformat() if v else None for k, v in marks.items()},
                )
            )
        # A finite truncated ledger cannot certify a product terminal; expose the missing tail.
        if (
            status is None
            and capture_end
            and capture_end.get("reason") == "deadline_expired"
            and utc(capture_end["at"]) >= candidate.release.deadline_at
        ):
            status, reason, terminal_at = (
                "unknown",
                "deadline_expired",
                candidate.release.deadline_at,
            )  # §4:450
        if status is None:
            status, reason = "unknown", "CAPTURE_INCOMPLETE"
        remaining = candidate.release.max_samples - used
        seconds_left = (
            (candidate.release.deadline_at - terminal_at).total_seconds()
            if terminal_at
            else None
        )
        unknowns = [r for r in rows if r["evaluation"]["verdict"] == "unknown"]
        post_warmup = [
            r for r in rows if utc(r["window"]["start"]) >= candidate.authorized_at
        ]
        results[str(minimum)] = dict(
            status=status,
            reason=reason,
            terminal_at=terminal_at.isoformat() if terminal_at else None,
            samples=rows,
            used_sample_count=used,
            remaining_samples=remaining,
            remaining_seconds=seconds_left,
            healthy_reserve_ok=(
                remaining >= 5 and seconds_left is not None and seconds_left >= 300
            )
            if status == "healthy"
            else None,
            unknown_reasons=dict(
                Counter(
                    code for r in unknowns for code in r["evaluation"]["reason_codes"]
                )
            ),
            unknown_ratio=len(unknowns) / len(rows) if rows else None,
            post_warmup_unknown_ratio=sum(
                r["evaluation"]["verdict"] == "unknown" for r in post_warmup
            )
            / len(post_warmup)
            if post_warmup
            else None,
            coverage_gaps=[r["sample_id"] for r in rows if r["coverage_gap"]],
        )
    return results


def run_summary(
    context: ReleaseEvaluationContext,
    samples: list[CollectedSample],
    *,
    scenario: str,
    truth: dict[str, Any],
    timeline: list[Any],
    candidates: list[int],
    code_revision: str,
    capture_end: dict[str, Any] | None = None,
    capture_code: dict[str, Any] | None = None,
    fold_code: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return dict(
        format_version=1,
        evidence_kind="U2_SIDECAR_UNAPPROVED",
        run_id=str(context.release.observation_id),
        scenario=scenario,
        truth=truth,
        # Historical capture identity must never come from the folding checkout.
        # Older ledgers without a start snapshot remain explicitly unverified.
        code_revision=capture_code["revision"] if capture_code else None,
        harness_sha256=capture_code["source_sha256"]["scripts/f11_u2_harness.py"]
        if capture_code
        else None,
        capture_code=capture_code,
        capture_code_status="frozen" if capture_code else "unavailable",
        fold_code=fold_code
        or dict(
            revision=code_revision,
            source_sha256={
                "scripts/f11_u2_harness.py": sha256(Path(__file__).read_bytes())
            },
        ),
        release=context.release.model_dump(mode="json"),
        target=context.release.target_snapshot,
        profile_revision=context.health_profile.revision,
        profile_sha256=sha256(canonical_content(context.health_profile).encode()),
        profile=context.health_profile.model_dump(mode="json"),
        released_at=context.release.released_at.isoformat(),
        authorized_at=context.authorized_at.isoformat(),
        timeline=timeline,
        candidates=fold_samples(context, samples, candidates, capture_end=capture_end),
        capture_end=capture_end,
        capture=[
            dict(
                sample_id=str(s.evidence.sample_id),
                claimed_at=s.claimed_at.isoformat(),
                decision_at=s.decision_at.isoformat(),
                elapsed_seconds=s.elapsed_seconds,
                evaluation=evaluate_release_sample(
                    context, s.evidence, decision_at=s.decision_at
                ).model_dump(mode="json"),
                lease_remaining_seconds=OBSERVER_LEASE_SECONDS - s.elapsed_seconds,
                queries=list(s.query_timings),
            )
            for s in samples
        ],
        raw_response_hashes=[
            dict(sample_id=str(s.evidence.sample_id), **q)
            for s in samples
            for q in s.query_timings
        ],
    )


def aggregate_summaries(paths: list[Path], output: Path = SUMMARY) -> dict[str, Any]:
    """Freeze all supplied successes AND failures; never declare calibration approved."""
    if not paths:
        raise ValueError("RUN_SUMMARIES_REQUIRED")
    runs = [
        dict(summary=read_json(p), sha256=sha256(p.read_bytes()), path=str(p))
        for p in paths
    ]
    if len({r["sha256"] for r in runs}) != len(runs):
        raise ValueError("DUPLICATE_RUN_SUMMARY")
    for identity in (
        lambda r: r["summary"]["run_id"],
        lambda r: (
            r["summary"]["release"]["source"],
            r["summary"]["release"]["source_release_id"],
        ),
    ):
        if len({identity(r) for r in runs}) != len(runs):
            raise ValueError("DUPLICATE_RELEASE_RUN")
    value = dict(
        format_version=1,
        evidence_kind="U2_CALIBRATION_UNAPPROVED",
        approval=None,
        runs=runs,
        scenario_counts=dict(Counter(r["summary"]["scenario"] for r in runs)),
    )
    write_json(output, value, exclusive=True)
    return value


def external(
    command: list[str], *, timeout: float = 20, data: str | None = None
) -> bytes:
    """No shell or stderr echo: commands may return sensitive deployment content."""
    try:
        result = subprocess.run(
            command,
            input=data.encode() if data is not None else None,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=kind_lab.lab_env(),
        )
    except subprocess.TimeoutExpired:
        raise ValueError("EXTERNAL_CALL_TIMEOUT") from None
    if result.returncode:
        raise ValueError("EXTERNAL_CALL_FAILED")
    if len(result.stdout) > 10 * 1024 * 1024:
        raise ValueError("EXTERNAL_RESPONSE_TOO_LARGE")
    return result.stdout


def kube(*args: str, timeout: float = 20) -> Any:
    return json.loads(external(kind_lab.kubectl(*args), timeout=timeout))


def freeze_code(ledger: Path, phase: str) -> dict[str, Any]:
    """Retain source bytes for uncommitted code; never include env or identity data."""
    root = ROOT.resolve()
    sources = [
        root / "scripts/f11_u2_harness.py",
        root / "scripts/kind_lab.py",
        root / "pyproject.toml",
        root / "uv.lock",
        *sorted((root / "opspilot").rglob("*.py")),
    ]
    relative = [str(path.relative_to(root)) for path in sources]
    revision = external(["git", "-C", str(root), "rev-parse", "HEAD"]).decode().strip()
    status = external(
        [
            "git",
            "-C",
            str(root),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--",
            *relative,
        ]
    )
    diff = external(
        ["git", "-C", str(root), "diff", "--binary", revision, "--", *relative]
    )
    destination = ledger / "code-snapshots" / phase
    if phase == "fold":
        # Preserve a failed attempt without blocking a corrected offline retry.
        # The completed summary still uses its existing exclusive-write guard.
        destination /= uuid4().hex
    destination.mkdir(parents=True, exist_ok=False)

    def retain(path: Path, raw: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            os.chmod(path, 0o600)
            stream.write(raw)

    hashes = {}
    for source, name in zip(sources, relative, strict=True):
        if not source.resolve().is_relative_to(root):
            raise ValueError("CODE_SOURCE_OUTSIDE_REPOSITORY")
        raw = source.read_bytes()
        hashes[name] = sha256(raw)
        retain(destination / name, raw)
    retain(destination / "worktree.diff", diff)
    retain(destination / "worktree-status.bin", status)
    return dict(
        revision=revision,
        recorded_at=utc_now().isoformat(),
        source_sha256=hashes,
        worktree_diff_sha256=sha256(diff),
        worktree_status_sha256=sha256(status),
        snapshot_path=str(destination.relative_to(ledger)),
    )


def snapshot(*, timeout: float = 20) -> dict[str, Any]:
    start = utc_now()
    deployment = kube(
        "-n",
        "otel-demo",
        "get",
        "deployment",
        "checkout",
        "-o",
        "json",
        timeout=timeout,
    )
    objects_requested = utc_now()
    objects = kube(
        "-n",
        "otel-demo",
        "get",
        "replicasets,pods,events",
        "-o",
        "json",
        timeout=timeout,
    )
    objects_observed = utc_now()
    rs = [
        o
        for o in objects["items"]
        if o["kind"] == "ReplicaSet"
        and any(
            owner.get("uid") == deployment["metadata"]["uid"]
            and owner.get("controller")
            for owner in o["metadata"].get("ownerReferences", [])
        )
    ]
    rs_uids = {r["metadata"]["uid"] for r in rs}
    pods = [
        o
        for o in objects["items"]
        if o["kind"] == "Pod"
        and any(
            owner.get("uid") in rs_uids and owner.get("controller")
            for owner in o["metadata"].get("ownerReferences", [])
        )
    ]
    related_uids = (
        rs_uids | {p["metadata"]["uid"] for p in pods} | {deployment["metadata"]["uid"]}
    )
    return dict(
        requested_at=start.isoformat(),
        observed_at=utc_now().isoformat(),
        objects_requested_at=objects_requested.isoformat(),
        objects_observed_at=objects_observed.isoformat(),
        deployment=deployment,
        replicasets=rs,
        pods=pods,
        events=[
            o
            for o in objects["items"]
            if o["kind"] == "Event"
            and o.get("involvedObject", {}).get("uid") in related_uids
        ],
    )


def revision_from_snapshot(snap: dict[str, Any], rs: dict[str, Any]) -> dict[str, Any]:
    pods = [
        p
        for p in snap["pods"]
        if any(
            o.get("uid") == rs["metadata"]["uid"]
            for o in p["metadata"].get("ownerReferences", [])
        )
    ]
    if not pods:
        raise ValueError("POD_DIGESTS_UNAVAILABLE")
    maps = []
    for pod in pods:
        containers = pod["spec"]["containers"] + pod["spec"].get("initContainers", [])
        statuses = pod.get("status", {}).get("containerStatuses", []) + pod.get(
            "status", {}
        ).get("initContainerStatuses", [])
        images = {}
        for container in containers:
            state = next((s for s in statuses if s["name"] == container["name"]), None)
            match = re.search(
                r"(?:^|@)(sha256:[0-9a-f]{64})$",
                state.get("imageID", "") if state else "",
            )
            if not match:
                raise ValueError("CONTAINER_DIGEST_UNAVAILABLE")
            images[container["name"]] = match[1]
        maps.append(images)
    if any(m != maps[0] for m in maps):
        raise ValueError("CONFLICTING_CONTAINER_DIGESTS")
    return dict(
        images=maps[0],
        pod_template_digest="sha256:"
        + sha256(canonical_json(snap["deployment"]["spec"]["template"]).encode()),
    )


def current_rs(snap: dict[str, Any]) -> dict[str, Any]:
    rev = (
        snap["deployment"]["metadata"]
        .get("annotations", {})
        .get("deployment.kubernetes.io/revision")
    )
    candidates = [
        r
        for r in snap["replicasets"]
        if rev
        and r["metadata"]
        .get("annotations", {})
        .get("deployment.kubernetes.io/revision")
        == rev
    ]
    if len(candidates) != 1:
        raise ValueError("DEPLOYMENT_REVISION_UNCONFIRMED")
    return candidates[0]


def publish(ledger: Path, release_id: str, *, dry_run: bool = False) -> dict[str, Any]:
    if not release_id or len(release_id) > 128:
        raise ValueError("INVALID_RELEASE_ID")
    patch = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "checkout",
                            "env": [
                                {"name": "OPSPILOT_U2_RELEASE_ID", "value": release_id}
                            ],
                        }
                    ]
                }
            }
        }
    }
    command = kind_lab.kubectl(
        "-n",
        "otel-demo",
        "patch",
        "deployment",
        "checkout",
        "--type=strategic",
        "-p",
        canonical_json(patch),
        "-o",
        "json",
    )
    if dry_run:
        print(shlex.join(command))
        return {"dry_run": True}
    with local_guard("publish", blocking=False), local_guard("fault", blocking=False):
        if (LEDGERS / "fault-active.json").exists():
            raise ValueError("FAULT_RECOVERY_REQUIRED")
        if (ledger / "release.json").exists():
            raise ValueError("RELEASE_ALREADY_RECORDED")
        before = snapshot()
        if not any(
            c["name"] == "checkout"
            for c in before["deployment"]["spec"]["template"]["spec"]["containers"]
        ):
            raise ValueError("CHECKOUT_CONTAINER_MISSING")
        rs = current_rs(before)
        revision = revision_from_snapshot(before, rs)
        # A JSON Patch resourceVersion test prevents overwriting a concurrent deployment edit.
        patch["metadata"] = {
            "resourceVersion": before["deployment"]["metadata"]["resourceVersion"]
        }
        command = kind_lab.kubectl(
            "-n",
            "otel-demo",
            "patch",
            "deployment",
            "checkout",
            "--type=strategic",
            "-p",
            canonical_json(patch),
            "-o",
            "json",
        )
        write_json(ledger / "before.json", before, exclusive=True)
        requested = utc_now()
        append_event(
            ledger / "timeline.jsonl",
            dict(
                kind="release_patch_requested",
                at=requested.isoformat(),
                release_id=release_id,
            ),
        )
        response = external(command)
        returned = utc_now()  # R is receipt of successful patch, never Pod Ready.
        patched = json.loads(response)
        if patched["spec"]["template"] == before["deployment"]["spec"]["template"]:
            raise ValueError("POD_TEMPLATE_UNCHANGED")
        value = dict(
            release_id=release_id,
            released_at=returned.isoformat(),
            request_before=requested.isoformat(),
            request_after=returned.isoformat(),
            before_revision=revision,
            before_pod_template_hash=rs["metadata"]["labels"]["pod-template-hash"],
            before_replicaset_uid=rs["metadata"]["uid"],
            before_template=before["deployment"]["spec"]["template"],
            after_template=patched["spec"]["template"],
            deployment_uid=patched["metadata"]["uid"],
            deployment_generation=patched["metadata"]["generation"],
        )
        write_json(ledger / "release.json", value, exclusive=True)
        append_event(ledger / "timeline.jsonl", dict(kind="release_patch", **value))
        return value


def watch_timeline(ledger: Path, seconds: int, poll: float = 2) -> dict[str, Any]:
    release = read_json(ledger / "release.json")
    previous = read_json(ledger / "before.json")
    old_uids = {p["metadata"]["uid"] for p in previous["pods"]}
    seen_ready: set[str] = set()
    seen_gone: set[str] = set()
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        left = until - time.monotonic()
        # Two reads, each bounded to half the remaining watch budget.
        snap = snapshot(timeout=min(20, max(0.01, left / 2)))
        write_json(ledger / "watch" / f"{uuid4().hex}.json", snap, exclusive=True)
        if (
            snap["deployment"]["spec"]["template"] != release["after_template"]
            or snap["deployment"]["metadata"]["uid"] != release["deployment_uid"]
        ):
            raise ValueError("CONCURRENT_DEPLOYMENT_CHANGE")
        pods = {p["metadata"]["uid"]: p for p in snap["pods"]}
        for uid, pod in pods.items():
            ready = next(
                (
                    c
                    for c in pod.get("status", {}).get("conditions", [])
                    if c["type"] == "Ready" and c["status"] == "True"
                ),
                None,
            )
            if uid not in old_uids and uid not in seen_ready and ready:
                seen_ready.add(uid)
                append_event(
                    ledger / "timeline.jsonl",
                    dict(
                        kind="new_pod_ready",
                        pod_uid=uid,
                        pod=pod["metadata"]["name"],
                        event_at=utc(ready["lastTransitionTime"]).isoformat(),
                        observed_at=snap["observed_at"],
                        owners=pod["metadata"].get("ownerReferences", []),
                        containers=pod["status"].get("containerStatuses", []),
                    ),
                )
        for uid in old_uids - pods.keys() - seen_gone:
            seen_gone.add(uid)
            append_event(
                ledger / "timeline.jsonl",
                dict(
                    kind="old_pod_gone",
                    pod_uid=uid,
                    event_at=None,
                    # Source may have read a Pod that vanished while the GET
                    # response was still in flight. Bound the combined RS/Pod/
                    # Event read by its request start, never its response end.
                    lower_bound=previous.get(
                        "objects_requested_at", previous["requested_at"]
                    ),
                    upper_bound=snap.get("objects_observed_at", snap["observed_at"]),
                ),
            )
        previous = snap
        try:
            rs = current_rs(snap)
            images = revision_from_snapshot(snap, rs)
            current_pods = [
                p
                for p in pods.values()
                if any(
                    o.get("uid") == rs["metadata"]["uid"]
                    for o in p["metadata"].get("ownerReferences", [])
                )
            ]
            all_ready = len(current_pods) >= snap["deployment"]["spec"].get(
                "replicas", 1
            ) and all(
                any(
                    c["type"] == "Ready" and c["status"] == "True"
                    for c in p.get("status", {}).get("conditions", [])
                )
                for p in current_pods
            )
        except ValueError:
            images = None
            all_ready = False
        if (
            images
            and old_uids <= seen_gone
            and seen_ready
            and all_ready
            and snap["deployment"].get("status", {}).get("observedGeneration", 0)
            >= release["deployment_generation"]
        ):
            after_hash = rs["metadata"]["labels"]["pod-template-hash"]
            if after_hash == release["before_pod_template_hash"]:
                raise ValueError("POD_TEMPLATE_HASH_UNCHANGED")
            release.update(
                after_revision=images,
                after_pod_template_hash=after_hash,
                after_replicaset_uid=rs["metadata"]["uid"],
                identity_available_at=snap["observed_at"],
            )
            write_json(ledger / "after.json", snap, exclusive=True)
            write_json(ledger / "release.json", release)
            return release
        time.sleep(min(poll, max(0, until - time.monotonic())))
    raise ValueError("ROLLOUT_WATCH_TIMEOUT")


def validate_identity(
    registry: dict[str, Any],
    resource_uid: str,
    db_target: dict[str, Any],
    cluster_namespace: dict[str, Any],
    namespace: dict[str, Any],
    deployment: dict[str, Any],
) -> dict[str, str]:
    entry = registry.get(resource_uid)
    if entry is None:
        raise ValueError("TARGET_NOT_REGISTERED")
    target = {
        k: resource_uid if k == "resource_uid" else entry.get(k) for k in TARGET_KEYS
    }
    if (
        any(not isinstance(v, str) or not v for v in target.values())
        or {k: db_target.get(k) for k in TARGET_KEYS} != target
    ):
        raise ValueError("REGISTRY_DATABASE_IDENTITY_MISMATCH")
    if target["namespace"] != "otel-demo" or target["workload"] != "checkout":
        raise ValueError("U2_TARGET_OUT_OF_SCOPE")
    if (
        cluster_namespace["metadata"]["name"] != "kube-system"
        or cluster_namespace["metadata"]["uid"] != target["cluster_uid"]
        or namespace["metadata"]["name"] != target["namespace"]
        or deployment["metadata"]["name"] != target["workload"]
        or deployment["metadata"]["namespace"] != target["namespace"]
    ):
        raise ValueError("CLUSTER_IDENTITY_MISMATCH")
    labels = deployment["metadata"].get("labels", {})
    integrations = {
        labels[k]
        for k in ("opspilot.integration.id", "opspilot_integration_id")
        if k in labels
    }
    # The lab currently stamps the integration through OTEL_RESOURCE_ATTRIBUTES,
    # promoted to opspilot_integration_id by the collector (values.yaml:18-23).
    for container in deployment["spec"]["template"]["spec"]["containers"]:
        for env in container.get("env", []):
            if env["name"] == "OTEL_RESOURCE_ATTRIBUTES":
                integrations.update(
                    part.split("=", 1)[1]
                    for part in env.get("value", "").split(",")
                    if part.startswith("opspilot.integration.id=")
                )
    if integrations != {target["integration_id"]}:
        raise ValueError("INTEGRATION_IDENTITY_UNCONFIRMED")
    return target


def db_snapshot(
    dsn: str, resource_uid: str, profile: HealthProfile
) -> tuple[dict[str, Any], datetime]:
    """Only SELECTs; no migration/install/profile writes; all server calls bounded."""
    with psycopg.connect(
        dsn,
        connect_timeout=5,
        options="-c statement_timeout=5000 -c default_transaction_read_only=on",
        row_factory=dict_row,
    ) as conn:
        rows = conn.execute(
            "SELECT target_id,integration_id,cluster_uid,namespace,resource_uid,workload FROM opspilot_targets WHERE resource_uid=%s",
            (resource_uid,),
        ).fetchall()
        frozen = conn.execute(
            "SELECT content,content_sha256 FROM opspilot_health_profiles WHERE health_profile_revision=%s",
            (profile.revision,),
        ).fetchone()
        at = conn.execute("SELECT clock_timestamp() AS at").fetchone()["at"]
    if len(rows) != 1 or frozen is None:
        raise ValueError("TARGET_OR_PROFILE_UNAVAILABLE")
    if frozen["content"] != canonical_content(profile) or frozen[
        "content_sha256"
    ] != sha256(canonical_content(profile).encode()):
        raise ValueError("DATABASE_PROFILE_MISMATCH")
    return rows[0], at.astimezone(timezone.utc)


def register_release(
    ledger: Path,
    registry_path: Path,
    resource_uid: str,
    profile_path: Path,
    minimum: int,
    dsn: str,
) -> dict[str, Any]:
    """Register a saved rollout through public stores; never authorize or migrate."""
    saved = read_json(ledger / "release.json")
    try:
        for side in ("before", "after"):
            expected = "sha256:" + sha256(
                canonical_json(saved[f"{side}_template"]).encode()
            )
            if saved[f"{side}_revision"]["pod_template_digest"] != expected:
                raise ValueError("RELEASE_TEMPLATE_DIGEST_MISMATCH")
        event = ReleaseEvent.model_validate_json(
            canonical_json(
                dict(
                    format_version=1,
                    source="u2-lab-configuration-patch",
                    source_release_id=saved["release_id"],
                    released_at=utc(saved["released_at"]).isoformat(),
                    before=saved["before_revision"],
                    after=saved["after_revision"],
                    auxiliary={
                        key: saved[key]
                        for key in (
                            "deployment_uid",
                            "deployment_generation",
                            "before_replicaset_uid",
                            "after_replicaset_uid",
                            "before_pod_template_hash",
                            "after_pod_template_hash",
                        )
                        if key in saved
                    },
                )
            )
        )
    except KeyError:
        raise ValueError("RELEASE_EVENT_IDENTITY_UNCONFIRMED") from None
    registry = load_target_identities(registry_path)
    entry = registry.get(resource_uid)
    if entry is None:
        raise ValueError("TARGET_NOT_REGISTERED")
    identity = {
        key: resource_uid if key == "resource_uid" else entry[key]
        for key in TARGET_KEYS
    }
    raw_profile = profile_path.read_bytes()
    profile = HealthProfile.model_validate_json(raw_profile)
    if (identity["namespace"], identity["workload"], entry["health_profile_id"]) != (
        profile.subject.kubernetes_namespace,
        profile.subject.service,
        profile.profile_id,
    ):
        raise ValueError("REGISTRY_PROFILE_MISMATCH")
    if identity["namespace"] != "otel-demo" or identity["workload"] != "checkout":
        raise ValueError("U2_TARGET_OUT_OF_SCOPE")
    # Lead decision A: match the web registration path; retain original bytes
    # separately while storing the canonical content covered by profile.revision.
    profile_content = canonical_content(profile)
    raw_body = canonical_json(event.model_dump(mode="json")).encode()
    attempt = ledger / "registration-attempts" / uuid4().hex
    attempt.mkdir(parents=True, exist_ok=False)
    for name, raw in (("event.json", raw_body), ("profile-original.json", raw_profile)):
        with (attempt / name).open("xb") as stream:
            os.chmod(attempt / name, 0o600)
            stream.write(raw)
    bounded_dsn = make_conninfo(dsn, connect_timeout=5)
    pool = PoolConfig(min_size=0, max_size=1, timeout=5.0)
    targets = DurableStore(bounded_dsn, pool=pool)
    releases = ReleaseStore(bounded_dsn, pool=pool)
    try:
        target_id = targets.register_target(resource_uid)
        receipt = releases.record_release(
            event=event,
            target_id=target_id,
            identity=identity,
            health_profile_revision=profile.revision,
            health_profile=profile_content,
            min_tracking_seconds=minimum,
            actor="u2-harness",
            raw_body=raw_body,
        )
        record = (
            releases.get_release(receipt.observation_id)
            if receipt.observation_id
            else None
        )
        value = dict(
            **receipt.model_dump(mode="json"),
            requested_target_id=str(target_id),
            released_at=record.released_at.astimezone(timezone.utc).isoformat()
            if record
            else None,
            deadline_at=record.deadline_at.astimezone(timezone.utc).isoformat()
            if record
            else None,
            source=event.source,
            source_release_id=event.source_release_id,
            raw_body_sha256=sha256(raw_body),
            requested_profile_revision=profile.revision,
            profile_original_sha256=sha256(raw_profile),
        )
        write_json(attempt / "receipt.json", value, exclusive=True)
        write_json(ledger / "registration.json", value)
        return value
    finally:
        releases.close()
        targets.close()


def candidate_context(
    ledger: Path,
    registry_path: Path,
    resource_uid: str,
    profile_path: Path,
    minimum: int,
    dsn: str,
) -> ReleaseEvaluationContext:
    release = read_json(ledger / "release.json")
    # Cannot form A until the after RS, hashes and full container digests exist.
    if "after_revision" not in release:
        raise ValueError("RELEASE_IDENTITY_INCOMPLETE_RUN_WATCH_FIRST")
    profile = load_health_profile(profile_path)
    registry = load_target_identities(registry_path)
    snap = snapshot()
    cluster = kube("get", "namespace", "kube-system", "-o", "json")
    namespace = kube("get", "namespace", "otel-demo", "-o", "json")
    before_db = utc_now()
    row, authorized = db_snapshot(dsn, resource_uid, profile)
    after_db = utc_now()
    target = validate_identity(
        registry, resource_uid, row, cluster, namespace, snap["deployment"]
    )
    if registry[resource_uid]["health_profile_id"] != profile.profile_id:
        raise ValueError("REGISTRY_PROFILE_MISMATCH")
    if (
        snap["deployment"]["metadata"]["uid"] != release["deployment_uid"]
        or snap["deployment"]["spec"]["template"] != release["after_template"]
    ):
        raise ValueError("RELEASE_TARGET_CHANGED")
    released = utc(release["released_at"])
    if (
        released > authorized
        or max(minimum, profile.session.sustained_window_seconds)
        >= profile.session.deadline_seconds
        or minimum <= 0
    ):
        raise ValueError("INVALID_RELEASE_TIME_OR_M")
    content = dict(
        format_version=1,
        target=target,
        health_profile_revision=profile.revision,
        health_profile_sha256=sha256(canonical_content(profile).encode()),
        min_tracking_seconds=minimum,
        evaluator="release-after-v1",
        mapping="pod-uid-replicaset-image-v1",
        evidence_path="tmp/UNAPPROVED_U2_CANDIDATE",
        evidence_sha256="0" * 64,
        approval_ref="UNAPPROVED_U2_CANDIDATE_DO_NOT_AUTHORIZE",
        approved_at=authorized.isoformat(),
    )
    digest = sha256(canonical_json(content).encode())
    policy = ReleasePolicy.model_validate_json(
        canonical_json(
            dict(
                policy_id="unapproved-u2",
                revision=f"unapproved-u2@{digest[:12]}",
                content_sha256=digest,
                content=content,
            )
        )
    )
    record = ReleaseRecord.model_validate_json(
        canonical_json(
            dict(
                observation_id=str(uuid4()),
                format_version=1,
                source="u2-lab-configuration-patch",
                source_release_id=release["release_id"],
                target_id=str(row["target_id"]),
                target_snapshot=target,
                before_revision=release["before_revision"],
                after_revision=release["after_revision"],
                auxiliary={
                    k: release[k]
                    for k in (
                        "deployment_uid",
                        "deployment_generation",
                        "before_replicaset_uid",
                        "after_replicaset_uid",
                        "before_pod_template_hash",
                        "after_pod_template_hash",
                    )
                },
                semantic_sha256=sha256(canonical_json(release).encode()),
                health_profile_revision=profile.revision,
                timing_policy_version=1,
                released_at=released.isoformat(),
                received_at=authorized.isoformat(),
                deadline_at=(
                    released + timedelta(seconds=profile.session.deadline_seconds)
                ).isoformat(),
                deadline_seconds=profile.session.deadline_seconds,
                min_tracking_seconds=minimum,
                **{
                    k: getattr(profile.session, k)
                    for k in (
                        "sample_interval_seconds",
                        "sustained_window_seconds",
                        "max_samples",
                    )
                },
                status="observing",
                control_generation=0,
                observation_generation=1,
                paused=False,
                ended_reason=None,
                ended_at=None,
                incident_id=None,
                supersedes_observation_id=None,
                created_at=authorized.isoformat(),
                updated_at=authorized.isoformat(),
                used_sample_count=0,
                calibrated_policy_revision=policy.revision,
                calibrated_policy_content=canonical_json(content),
                calibrated_policy_sha256=digest,
            )
        )
    )
    context = ReleaseEvaluationContext(
        release=record,
        session_id=uuid4(),
        authorized_at=authorized,
        policy=policy,
        health_profile=profile,
    )
    write_json(ledger / "context.json", context.model_dump(mode="json"), exclusive=True)
    write_json(
        ledger / "identity.json",
        dict(
            target=target,
            registry_path=str(registry_path),
            registry_sha256=sha256(registry_path.read_bytes()),
            database_time=authorized.isoformat(),
            host_before=before_db.isoformat(),
            host_after=after_db.isoformat(),
            db_minus_host_seconds_bounds=[
                (authorized - after_db).total_seconds(),
                (authorized - before_db).total_seconds(),
            ],
            cluster=cluster,
            namespace=namespace,
            deployment=snap["deployment"],
            approval=None,
        ),
        exclusive=True,
    )
    return context


def db_clock(dsn: str) -> datetime:
    with psycopg.connect(
        dsn,
        connect_timeout=5,
        options="-c statement_timeout=5000 -c default_transaction_read_only=on",
    ) as conn:
        return (
            conn.execute("SELECT clock_timestamp()")
            .fetchone()[0]
            .astimezone(timezone.utc)
        )


def collect_run(
    ledger: Path, context: ReleaseEvaluationContext, source: Any, dsn: str
) -> None:
    if list((ledger / "samples").glob("*.json")):
        raise ValueError("CAPTURE_ALREADY_EXISTS_NO_BUDGET_RESTART")
    write_json(
        ledger / "capture-start.json",
        dict(
            authorized_at=context.authorized_at.isoformat(),
            code=freeze_code(ledger, "capture"),
        ),
        exclusive=True,
    )
    due = context.authorized_at + timedelta(
        seconds=context.release.sample_interval_seconds
    )
    for sequence in range(1, context.release.max_samples + 1):
        while True:
            now = db_clock(dsn)
            if now >= context.release.deadline_at:
                write_json(
                    ledger / "capture-end.json",
                    dict(reason="deadline_expired", at=now.isoformat()),
                    exclusive=True,
                )
                return
            if now >= due:
                break
            time.sleep(min(1, (due - now).total_seconds()))
        sample = collect_sample(context, source, ledger, clock=lambda: db_clock(dsn))
        save_sample(sample, ledger, sequence)
        if sample.decision_at >= sample.claimed_at + timedelta(
            seconds=OBSERVER_LEASE_SECONDS
        ) or not query_evidence_consistent(
            context, sample.evidence, sample.decision_at
        ):
            write_json(
                ledger / "capture-end.json",
                dict(
                    reason="history_only_capture_stopped",
                    at=sample.decision_at.isoformat(),
                ),
                exclusive=True,
            )
            return
        due = sample.decision_at + timedelta(
            seconds=context.release.sample_interval_seconds
        )
    write_json(
        ledger / "capture-end.json",
        dict(reason="capture_limit", at=db_clock(dsn).isoformat()),
        exclusive=True,
    )


def flags() -> tuple[dict[str, Any], bytes]:
    cm = kube("-n", "otel-demo", "get", "configmap", "flagd-config", "-o", "json")
    return cm, cm["data"][kind_lab.FLAG_FILE].encode()


def change_flag(raw: bytes, flag: str, variant: str, expected: str) -> bytes:
    if flag == kind_lab.FLAG:
        return kind_lab.mutate_flags(raw, variant, expected)
    data = json.loads(raw)
    selected = data["flags"].get(flag)
    if (
        not selected
        or selected["defaultVariant"] != expected
        or variant not in selected["variants"]
    ):
        raise ValueError("FLAG_OR_VARIANT_UNCONFIRMED")
    selected["defaultVariant"] = variant
    return (json.dumps(data, indent=2) + "\n").encode()


def patch_flags(
    cm: dict[str, Any], before: bytes, after: bytes, *, restore_fence: bool = False
) -> None:
    escaped = kind_lab.FLAG_FILE.replace("~", "~0").replace("/", "~1")
    patch = [
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": cm["metadata"]["resourceVersion"],
        },
        {"op": "test", "path": f"/data/{escaped}", "value": before.decode()},
        {"op": "replace", "path": f"/data/{escaped}", "value": after.decode()},
    ]
    fence = uuid4().hex if restore_fence else None
    if fence:
        # Replacing unchanged flag bytes can be a Kubernetes no-op. A unique
        # annotation makes this CAS advance resourceVersion, fencing an earlier
        # injection whose PATCH was sent but whose outcome remains unknown.
        patch.append(
            dict(
                op="add",
                path="/metadata/annotations",
                value={
                    **cm["metadata"].get("annotations", {}),
                    "opspilot.dev/u2-restore-fence": fence,
                },
            )
        )
    patched = kube(
        "-n",
        "otel-demo",
        "patch",
        "configmap",
        "flagd-config",
        "--type=json",
        "-p",
        canonical_json(patch),
        "-o",
        "json",
    )
    if fence and (
        patched["metadata"]["resourceVersion"] == cm["metadata"]["resourceVersion"]
        or patched["metadata"]
        .get("annotations", {})
        .get("opspilot.dev/u2-restore-fence")
        != fence
    ):
        raise ValueError("FLAG_RESTORE_FENCE_UNVERIFIED")
    if flags()[1] != after:
        raise ValueError("FLAG_CONFIG_PATCH_UNVERIFIED")


def flagd_resolution(
    url: str, flag: str, method: str, timeout: float
) -> dict[str, Any]:
    """Probe daemon evaluation, NOT application onset. Protocol reference:
    https://github.com/open-feature/flagd/blob/main/README.md (HTTP Connect v1).
    Caller supplies a separately prepared lab port-forward; this never starts one.
    """
    parts = urlsplit(url)
    if (
        parts.scheme != "http"
        or parts.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
    ):
        raise ValueError("LOCAL_FLAGD_URL_REQUIRED")
    if method not in {"Boolean", "String", "Int", "Float", "Object"}:
        raise ValueError("FLAGD_METHOD_UNCONFIRMED")
    body = external(
        [
            "curl",
            "--silent",
            "--show-error",
            "--fail",
            "--noproxy",
            "*",
            "--max-time",
            str(timeout),
            "--max-filesize",
            "30720",
            "-H",
            "Content-Type: application/json",
            "--data-binary",
            "@-",
            url.rstrip("/") + f"/flagd.evaluation.v1.Service/Resolve{method}",
        ],
        timeout=timeout + 1,
        data=canonical_json(dict(flagKey=flag, context={})),
    )
    return json.loads(body)


def await_flag(
    ledger: Path,
    url: str,
    flag: str,
    method: str,
    variant: str,
    seconds: int,
    *,
    not_before: datetime | None = None,
) -> datetime:
    until = time.monotonic() + seconds
    last_not_matching = not_before
    while time.monotonic() < until:
        requested = utc_now()
        try:
            result = flagd_resolution(
                url, flag, method, min(5, max(0.01, until - time.monotonic()))
            )
        except ValueError:
            result = {}
        observed = utc_now()
        append_event(
            ledger / "fault.jsonl",
            dict(
                kind="flagd_probe",
                flag=flag,
                expected_variant=variant,
                request_before=requested.isoformat(),
                request_after=observed.isoformat(),
                response=result,
            ),
        )
        if result.get("variant") == variant:
            append_event(
                ledger / "fault.jsonl",
                dict(
                    kind="flagd_variant_effective",
                    flag=flag,
                    variant=variant,
                    effective_at=observed.isoformat(),
                    lower_bound=last_not_matching.isoformat()
                    if last_not_matching
                    else None,
                    upper_bound=observed.isoformat(),
                    application_onset_at=None,
                    limitation="daemon resolution only; align request/span evidence separately",
                ),
            )
            return observed
        if "variant" in result:
            last_not_matching = requested
        time.sleep(min(1, max(0, until - time.monotonic())))
    raise ValueError("FLAG_PROPAGATION_UNCONFIRMED")


def restore_fault(
    ledger: Path, url: str, method: str, propagation_seconds: int
) -> None:
    state_path = LEDGERS / "fault-active.json"
    state = read_json(state_path)
    if state["ledger"] != str(ledger.resolve()):
        raise ValueError("FAULT_BELONGS_TO_ANOTHER_RUN")
    cm, live = flags()
    original, injected = state["original"].encode(), state["injected"].encode()
    if live == injected:
        patch_flags(cm, live, original)
    elif live != original:
        raise ValueError("FLAGS_CHANGED_REFUSE_RESTORE")
    else:
        # A normal read alone cannot exclude a late injection commit. Establish
        # an actual CAS write before confirming recovery and removing its inputs.
        patch_flags(cm, live, original, restore_fence=True)
    append_event(
        ledger / "fault.jsonl",
        dict(
            kind="restore_config_verified",
            at=utc_now().isoformat(),
            planned_at=state.get("planned_restore_at"),
        ),
    )
    await_flag(ledger, url, state["flag"], method, state["normal"], propagation_seconds)
    state_path.unlink()


def scheduled_fault(
    ledger: Path,
    *,
    offset: int,
    duration: int,
    flag: str,
    variant: str,
    normal: str,
    url: str,
    method: str,
    propagation_seconds: int = 180,
    dry_run: bool = False,
) -> None:
    if (
        offset < 0
        or duration <= 0
        or offset + duration > 7200
        or not flag
        or variant == normal
        or propagation_seconds <= 0
        or propagation_seconds > 600
    ):
        raise ValueError("INVALID_FAULT_SCHEDULE")
    if dry_run:
        print(
            f"R+{offset}s kubectl patch configmap flagd-config ({flag}: {normal} -> {variant}); "
            f"flagd Resolve{method}; restore after {duration}s of confirmed daemon effect"
        )
        return
    released = utc(read_json(ledger / "release.json")["released_at"])
    planned = released + timedelta(seconds=offset)
    with local_guard("fault", blocking=False):
        state_path = LEDGERS / "fault-active.json"
        if state_path.exists():
            raise ValueError("FAULT_RECOVERY_REQUIRED")
        append_event(
            ledger / "fault.jsonl",
            dict(
                kind="injection_planned",
                planned_at=planned.isoformat(),
                flag=flag,
                variant=variant,
                duration_seconds=duration,
                duration_origin="first_confirmed_flagd_variant",
                unrelated_flag_confirmed=(flag == AD_FLAG)
                if flag != kind_lab.FLAG
                else None,
                unrelated_scenario_verified=False if flag != kind_lab.FLAG else None,
            ),
        )
        while utc_now() < planned:
            time.sleep(min(1, max(0, (planned - utc_now()).total_seconds())))
        try:
            cm, original = flags()
            injected = change_flag(original, flag, variant, normal)
            # Verify normal state on the same daemon before changing config.
            if flagd_resolution(url, flag, method, 5).get("variant") != normal:
                raise ValueError("FLAGD_BASELINE_NOT_NORMAL")
        except BaseException as exc:
            append_event(
                ledger / "fault.jsonl",
                dict(
                    kind="injection_failed",
                    stage="preflight",
                    at=utc_now().isoformat(),
                    error_type=type(exc).__name__,
                ),
            )
            raise
        state = dict(
            ledger=str(ledger.resolve()),
            flag=flag,
            normal=normal,
            variant=variant,
            original=original.decode(),
            injected=injected.decode(),
            planned_at=planned.isoformat(),
            planned_restore_at=None,
        )
        write_json(
            state_path, state, exclusive=True
        )  # recovery survives timeout/kill after PATCH
        try:
            requested = utc_now()
            patch_flags(cm, original, injected)
            append_event(
                ledger / "fault.jsonl",
                dict(
                    kind="injection_config_verified",
                    request_before=requested.isoformat(),
                    request_after=utc_now().isoformat(),
                    planned_at=planned.isoformat(),
                    sha256=sha256(injected),
                ),
            )
            effective = await_flag(
                ledger,
                url,
                flag,
                method,
                variant,
                propagation_seconds,
                not_before=requested,
            )
            planned_restore = effective + timedelta(seconds=duration)
            state["planned_restore_at"] = planned_restore.isoformat()
            write_json(state_path, state)
            while utc_now() < planned_restore:
                time.sleep(
                    min(1, max(0, (planned_restore - utc_now()).total_seconds()))
                )
        except BaseException as exc:
            append_event(
                ledger / "fault.jsonl",
                dict(
                    kind="injection_failed",
                    at=utc_now().isoformat(),
                    error_type=type(exc).__name__,
                ),
            )
            raise
        finally:
            try:
                restore_fault(ledger, url, method, propagation_seconds)
            except BaseException as exc:
                append_event(
                    ledger / "fault.jsonl",
                    dict(
                        kind="restore_failed",
                        at=utc_now().isoformat(),
                        error_type=type(exc).__name__,
                    ),
                )
                raise


def positive(value: str) -> int:
    number = int(value)
    if not 0 < number <= 7200:
        raise argparse.ArgumentTypeError("expected 1..7200")
    return number


def ledger_path(value: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", value):
        raise ValueError("INVALID_RUN_ID")
    path = LEDGERS / value
    if not path.resolve().is_relative_to(LEDGERS.resolve()):
        raise ValueError("LEDGER_PATH_OUTSIDE_TMP")
    return path


def read_events(ledger: Path, filename: str) -> list[Any]:
    path = ledger / filename
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def artifact_manifest(ledger: Path) -> list[dict[str, Any]]:
    """Bind raw Kubernetes/fault/context artifacts as well as HTTP response bytes."""
    manifest = []
    for path in sorted(ledger.rglob("*")):
        if not path.is_file() or path.name == "summary.json" or path.suffix == ".tmp":
            continue
        if not path.resolve().is_relative_to(ledger.resolve()):
            raise ValueError("ARTIFACT_PATH_OUTSIDE_LEDGER")
        raw = path.read_bytes()
        manifest.append(
            dict(path=str(path.relative_to(ledger)), bytes=len(raw), sha256=sha256(raw))
        )
    return manifest


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--holder", default="")
    p.add_argument("--batch", default="")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print actions, no external calls or writes",
    )
    sub = p.add_subparsers(dest="command", required=True)
    lock = sub.add_parser("occupation")
    lock.add_argument("action", choices=["claim", "show", "release"])
    lock.add_argument("--until", type=utc)
    for name in (
        "release",
        "watch",
        "register",
        "context",
        "fault",
        "restore",
        "collect",
        "fold",
    ):
        command = sub.add_parser(name)
        command.add_argument(
            "--run",
            required=True,
            help="unique run directory name under tmp/lab-ledgers",
        )
        if name == "release":
            command.add_argument("--release-id", required=True)
        if name == "watch":
            command.add_argument("--seconds", type=positive, default=600)
        if name in ("register", "context"):
            command.add_argument(
                "--registry",
                type=Path,
                default=os.environ.get("OPSPILOT_TARGET_IDENTITIES"),
            )
            command.add_argument("--resource-uid", required=True)
            command.add_argument(
                "--profile",
                type=Path,
                default=ROOT / "opspilot/observer/profiles/otel-demo-checkout.json",
            )
            command.add_argument("--minimum", type=positive, required=True)
        if name in ("fault", "restore"):
            command.add_argument(
                "--flagd-url",
                required=True,
                help="already forwarded local flagd evaluation endpoint",
            )
            command.add_argument(
                "--method",
                required=True,
                choices=["Boolean", "String", "Int", "Float", "Object"],
            )
            command.add_argument("--propagation-seconds", type=positive, default=180)
        if name == "fault":
            command.add_argument("--offset", type=int, required=True)
            command.add_argument("--duration", type=positive, required=True)
            command.add_argument(
                "--flag",
                default=kind_lab.FLAG,
                help="ad flag confirmed: adFailure; normal/variant still checked at runtime",
            )
            command.add_argument("--variant", default=kind_lab.FAULT_VARIANT)
            command.add_argument("--normal", default=kind_lab.NORMAL_VARIANT)
        if name == "fold":
            command.add_argument("--minimum", nargs="+", type=positive, required=True)
            command.add_argument(
                "--scenario",
                required=True,
                choices=[
                    "healthy",
                    "immediate",
                    "delayed-5m",
                    "delayed-15m",
                    "unrelated",
                    "boundary",
                    "probe",
                ],
            )
            command.add_argument(
                "--truth",
                type=Path,
                required=True,
                help="independently recorded truth JSON, failures included",
            )
    aggregate = sub.add_parser("aggregate")
    aggregate.add_argument("summaries", nargs="+", type=Path)
    aggregate.add_argument("--output", type=Path, default=SUMMARY)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "occupation":
            if args.dry_run:
                print(f"occupation {args.action} (dry-run)")
            else:
                print(
                    json.dumps(
                        occupation(args.action, args.holder, args.batch, args.until)
                    )
                )
            return 0
        if args.command == "aggregate":
            if args.dry_run:
                print("aggregate supplied run summaries (dry-run)")
            else:
                aggregate_summaries(args.summaries, args.output)
            return 0
        ledger = ledger_path(args.run)
        if args.dry_run:
            if args.command == "release":
                publish(ledger, args.release_id, dry_run=True)
            elif args.command == "fault":
                scheduled_fault(
                    ledger,
                    offset=args.offset,
                    duration=args.duration,
                    flag=args.flag,
                    variant=args.variant,
                    normal=args.normal,
                    url=args.flagd_url,
                    method=args.method,
                    dry_run=True,
                )
            else:
                print(
                    f"{args.command}: kubectl/flagd/Prometheus/DB calls and local writes skipped (dry-run)"
                )
            return 0
        if args.command == "fold":
            context = ReleaseEvaluationContext.model_validate_json(
                (ledger / "context.json").read_bytes()
            )
            started = (
                read_json(ledger / "capture-start.json")
                if (ledger / "capture-start.json").exists()
                else {}
            )
            fold_code = freeze_code(ledger, "fold")
            summary = run_summary(
                context,
                load_samples(ledger),
                scenario=args.scenario,
                truth=read_json(args.truth),
                timeline=read_events(ledger, "timeline.jsonl")
                + read_events(ledger, "fault.jsonl"),
                candidates=args.minimum,
                code_revision=fold_code["revision"],
                capture_code=started.get("code"),
                fold_code=fold_code,
                capture_end=read_json(ledger / "capture-end.json")
                if (ledger / "capture-end.json").exists()
                else None,
            )
            summary["identity"] = (
                read_json(ledger / "identity.json")
                if (ledger / "identity.json").exists()
                else None
            )
            summary["release_snapshot"] = (
                read_json(ledger / "release.json")
                if (ledger / "release.json").exists()
                else None
            )
            summary["artifact_hashes"] = artifact_manifest(ledger)
            summary["platform_links"] = summary["truth"].get("platform_links", [])
            write_json(ledger / "summary.json", summary, exclusive=True)
            print(json.dumps(summary, indent=2))
            return 0
        with reserved(args.holder, args.batch):
            if args.command == "release":
                publish(ledger, args.release_id)
            elif args.command == "watch":
                watch_timeline(ledger, args.seconds)
            elif args.command == "context":
                if not args.registry or not os.environ.get("OPSPILOT_LAB_DSN"):
                    raise ValueError("REGISTRY_AND_LAB_DSN_REQUIRED")
                candidate_context(
                    ledger,
                    args.registry,
                    args.resource_uid,
                    args.profile,
                    args.minimum,
                    os.environ["OPSPILOT_LAB_DSN"],
                )
            elif args.command == "register":
                if not args.registry or not os.environ.get("OPSPILOT_LAB_DSN"):
                    raise ValueError("REGISTRY_AND_LAB_DSN_REQUIRED")
                with local_guard("register", blocking=False):
                    receipt = register_release(
                        ledger,
                        args.registry,
                        args.resource_uid,
                        args.profile,
                        args.minimum,
                        os.environ["OPSPILOT_LAB_DSN"],
                    )
                print(json.dumps(receipt, indent=2))
                if receipt["outcome"] == "rejected":
                    return 2
            elif args.command == "fault":
                scheduled_fault(
                    ledger,
                    offset=args.offset,
                    duration=args.duration,
                    flag=args.flag,
                    variant=args.variant,
                    normal=args.normal,
                    url=args.flagd_url,
                    method=args.method,
                    propagation_seconds=args.propagation_seconds,
                )
            elif args.command == "restore":
                with local_guard("fault", blocking=False):
                    restore_fault(
                        ledger, args.flagd_url, args.method, args.propagation_seconds
                    )
            elif args.command == "collect":
                context = ReleaseEvaluationContext.model_validate_json(
                    (ledger / "context.json").read_bytes()
                )
                username = os.environ.get("OPSPILOT_LAB_PROMETHEUS_USERNAME")
                password = os.environ.get("OPSPILOT_LAB_PROMETHEUS_PASSWORD")
                dsn = os.environ.get("OPSPILOT_LAB_DSN")
                if username != "lab" or not password or not dsn:
                    raise ValueError("SOURCE_LAB_ENV_AND_SET_LAB_DSN")
                source = PrometheusReadOnlySource(
                    kind_lab.PROMETHEUS, basic_auth=(username, password)
                )
                with local_guard("collect", blocking=False):
                    collect_run(ledger, context, source, dsn)
        return 0
    except (
        ValueError,
        OSError,
        KeyError,
        psycopg.Error,
        PersistenceError,
        SystemExit,
    ) as exc:
        # Do not print ValidationError/DB errors (their text may embed credentials).
        code = (
            str(exc)
            if type(exc) is ValueError and re.fullmatch(r"[A-Z_]+", str(exc))
            else type(exc).__name__
        )
        print(f"U2_HARNESS_ERROR: {code}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
