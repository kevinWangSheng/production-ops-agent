"""Lab protocol of the live scenario driver (eval PR-C): engineer script only.

Interface: docs/tasks/2026-10-18-eval-pr-c-interface.md (sections 3-6, 11).
Contract: docs/tasks/2026-10-18-eval-pr-c-contract.md (U1, U2, A2-A4).

Everything the driver does to or reads from the shared kind lab goes through
the Protocols below; ``real_deps`` builds the only implementations that touch
kubectl, flagd, Prometheus, Jaeger, PostgreSQL or the DeepSeek balance, so
every rule here is testable offline with fakes. The shared-occupation files
are F11's (``scripts/f11_u2_harness.py``): the same names, formats and
flocks, never a second lock, and ``fault-active.json`` keeps the keys F11's
``restore_fault`` reads so either recovery path can restore it.

The driver mutates the *lab* only through the fault whitelist; PRODUCT-
CONSTRAINTS reserves fault injection for engineers and isolated harnesses,
and nothing here is reachable from the product, the worker or the model.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from opspilot.evaluation import SignatureRule, Window
from opspilot.observation.release_policy import canonical_json, sha256
from scripts import kind_lab

ROOT = Path(__file__).resolve().parents[1]
OWNER = "eval-live-1"
STATE_FILE = "fault-active.json"
PROPAGATION_SECONDS = 600
WASH_SECONDS = 600
POLL_SECONDS = 5.0
PROBE_SECONDS = 5.0
WINDOW_SECONDS = 300
FENCE_ANNOTATION = "opspilot.dev/eval-live-restore-fence"
# Jaeger search page size for the independent observation; a page this full
# may have dropped traces, so the result is marked incomplete. No server-side
# tag filter: ``span_error`` decides locally, since an error shown only by
# status code, gRPC or HTTP 5xx carries no ``error=true`` tag (r11). Larger
# pages than that limit or bodies past the byte cap are incomplete, never
# "no errors".
TRACE_LIMIT = 1500
MAX_TELEMETRY_BYTES = 64 * 1024 * 1024
# Adaptive split of a full page (r12) and its caps per ``spans()`` call.
SPLIT_FACTOR = 4
MAX_SUBREQUESTS = 64
MAX_SEARCH_BYTES = 1024 * 1024 * 1024
MIN_SUBWINDOW_US = 1_000


class LabError(Exception):
    """A fixed, credential-free failure code (``.code`` and ``str()``)."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class Interrupted(BaseException):  # noqa: N818 - a signal, not an error
    """SIGINT/SIGTERM turned into a catchable exit (U2 recovery path)."""


# -- cooperative interrupts (interface r16) -----------------------------------
#
# The signal handler only records a stop request; ``checkpoint()`` turns it
# into ``Interrupted`` outside protected sections. ``pending`` is the stop
# request: permanent for one CLI call (R3), read and never cleared. A second
# signal (SIGINT and SIGTERM counted together) raises at once, but only
# outside protected sections and outside external commands (R2).
_STOP: dict[str, Any] = {
    "depth": 0,  # protected sections (nestable)
    "pending": False,
    "signals": 0,
    "first": None,  # the first signal number, None for a caught Interrupted
    "at": None,  # monotonic time of the first stop request
    "external": 0,  # inside an external command (no forced raise)
    "scopes": 0,
}
SLEEP_SLICE_SECONDS = 0.5
GUARD_RETRY_SECONDS = 0.05


def _reset_stop() -> None:
    _STOP.update(depth=0, pending=False, signals=0, first=None, at=None, external=0)


@contextmanager
def interrupt_scope() -> Iterator[None]:
    """One CLI call (or one mode called directly): the stop state starts
    clear and is cleared again when the outermost scope ends."""
    outermost = _STOP["scopes"] == 0
    if outermost:
        _reset_stop()
    _STOP["scopes"] += 1
    try:
        yield
    finally:
        _STOP["scopes"] -= 1
        if outermost:
            _reset_stop()


def request_stop(signum: int | None = None) -> None:
    """Record a stop request; a caught ``Interrupted`` records one too (R3)."""
    if not _STOP["pending"]:
        _STOP.update(pending=True, first=signum, at=time.monotonic())


def stop_requested() -> bool:
    return bool(_STOP["pending"])


def handle_signal(signum: int, frame: Any) -> None:
    """The SIGINT/SIGTERM handler: record, raise only on a forced stop."""
    _STOP["signals"] += 1
    request_stop(signum)
    if _STOP["signals"] >= 2 and not _STOP["depth"] and not _STOP["external"]:
        raise Interrupted()


def checkpoint() -> None:
    """Where a stop request becomes ``Interrupted``; a no-op when protected."""
    if _STOP["pending"] and not _STOP["depth"]:
        raise Interrupted()


@contextmanager
def protected() -> Iterator[None]:
    """A section that is never interrupted: no checkpoint fires and no forced
    stop raises inside it, and leaving it raises nothing either (R2)."""
    _STOP["depth"] += 1
    try:
        yield
    finally:
        _STOP["depth"] -= 1


@contextmanager
def external_command() -> Iterator[None]:
    """kubectl/pg_ctl/initdb/git: a checkpoint before, no forced stop during;
    the command's own timeout bounds it. Every such command also runs in its
    own session (``start_new_session=True``), so a terminal Ctrl-C reaches
    the driver only, never the command itself."""
    checkpoint()
    _STOP["external"] += 1
    try:
        yield
    finally:
        _STOP["external"] -= 1


def interruptible_sleep(seconds: float) -> None:
    """Sleep in slices of at most ``SLEEP_SLICE_SECONDS`` and return early
    once a stop is requested (R5); a protected section sleeps in full."""
    if seconds <= 0:
        return
    deadline = time.monotonic() + seconds
    while (left := deadline - time.monotonic()) > 0:
        if _STOP["pending"] and not _STOP["depth"]:
            return
        time.sleep(min(left, SLEEP_SLICE_SECONDS))


@dataclass(frozen=True)
class FaultFlag:
    flag: str
    fault_variant: str
    normal_variant: str = kind_lab.NORMAL_VARIANT


FAULT_FLAGS: Mapping[str, FaultFlag] = {
    name: FaultFlag(name, variant) for name, variant in kind_lab.FAULT_FLAGS.items()
}


# -- seams --------------------------------------------------------------------


class LiveClock(Protocol):
    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


@dataclass(frozen=True)
class FlagConfig:
    raw: bytes
    uid: str
    resource_version: str
    # The restore-fence annotation as read (r6); None when absent/unknown.
    fence: str | None = None


class FlagStore(Protocol):
    def read(self) -> FlagConfig: ...
    def replace(
        self, current: FlagConfig, new_raw: bytes, *, fence: str | None
    ) -> FlagConfig: ...


class FlagResolver(Protocol):
    def resolve(self, flag: str, method: str, timeout: float) -> str | None: ...


@dataclass(frozen=True)
class ObservedSpan:
    trace_id: str
    span_id: str
    service: str
    operation: str
    start: datetime
    tags: Mapping[str, Any]


@dataclass(frozen=True)
class SpanQuery:
    spans: tuple[ObservedSpan, ...]
    complete: bool


@dataclass(frozen=True)
class CallCount:
    value: float | None
    complete: bool


class TelemetryProbe(Protocol):
    def spans(
        self, service: str, start: datetime, end: datetime, timeout: float
    ) -> SpanQuery: ...
    def checkout_calls(
        self, start: datetime, end: datetime, timeout: float
    ) -> CallCount: ...


class LabIdentity(Protocol):
    def verify(self) -> tuple[str, ...]: ...


class PortProbe(Protocol):
    def in_use(self, port: int) -> bool: ...


@dataclass(frozen=True)
class PgInstance:
    directory: Path
    port: int
    owner_dsn: str
    runtime_dsn: str  # the worker role: the Run itself
    web_dsn: str = ""  # the web role: incident intake; empty means runtime_dsn


class PgLauncher(Protocol):
    def start(self, directory: Path, port: int) -> PgInstance: ...
    def stop(self, instance: PgInstance) -> None: ...


class BalanceReader(Protocol):
    def balance(self) -> Decimal | None: ...


class Investigator(Protocol):
    def investigate(self, request: Any) -> Any: ...


class EnvChecker(Protocol):
    def check(self) -> str | None: ...


@dataclass(frozen=True)
class Provenance:
    commit: str | None
    clean: bool


class ProvenanceReader(Protocol):
    def read(self) -> Provenance: ...


@dataclass(frozen=True)
class LiveDeps:
    clock: LiveClock
    flags: FlagStore
    flagd: FlagResolver
    telemetry: TelemetryProbe
    identity: LabIdentity
    ports: PortProbe
    pg: PgLauncher
    balance: BalanceReader
    investigator: Investigator
    ledger_root: Path
    evidence_root: Path
    recording_root: Path
    pg_root: Path
    scenarios_root: Path
    flagd_url: str
    # None = the environment preconditions hold, else "ENV_UNCONFIRMED".
    env: EnvChecker
    # The code version (r5); the driver core itself starts no process. None
    # leaves the manifest's ``code`` dimension unknown.
    provenance: ProvenanceReader | None = None


# -- private ledger -----------------------------------------------------------


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)


def write_private(path: Path, data: bytes, *, exclusive: bool = True) -> None:
    _private_dir(path.parent)
    if exclusive:
        with path.open("xb") as stream:
            os.chmod(path, 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temporary.open("xb") as stream:
        os.chmod(temporary, 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _json(value: Any) -> bytes:
    return (
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n"
    ).encode()


@dataclass
class AttemptLedger:
    """Append-only lifecycle log of one attempt (``lifecycle.jsonl``).

    ``freeze`` closes it for the summary's ``ledger_ref``; later events (an
    explicit recovery) go to ``recovery.jsonl`` so the frozen bytes stay put.
    """

    directory: Path
    clock: LiveClock
    seq: int = 0
    frozen: bool = False
    # An append that failed (disk full, read-only, ...). Logging never stops
    # a restore or a PG stop halfway; the caller turns this into exit 5.
    write_failed: bool = False

    def __post_init__(self) -> None:
        try:
            _private_dir(self.directory)
        except OSError:
            self.write_failed = True

    def event(self, stage: str, event: str, **detail: Any) -> None:
        name = "recovery.jsonl" if self.frozen else "lifecycle.jsonl"
        row = {
            "seq": self.seq,
            "at": self.clock.now().isoformat(),
            "mono": self.clock.monotonic(),
            "stage": stage,
            "event": event,
            "detail": detail,
        }
        self.seq += 1
        path = self.directory / name
        try:
            with path.open("a") as stream:
                os.chmod(path, 0o600)
                stream.write(canonical_json(row) + "\n")
        except OSError:
            self.write_failed = True

    def write(self, name: str, value: Any) -> None:
        try:
            write_private(self.directory / name, _json(value))
        except OSError:
            # Raised (the caller stops before touching the lab), and kept:
            # the attempt's evidence is incomplete, so it ends with exit 5.
            self.write_failed = True
            raise

    def freeze(self) -> bytes:
        self.frozen = True
        path = self.directory / "lifecycle.jsonl"
        return path.read_bytes() if path.exists() else b""


# -- shared occupation (F11 files and flocks) ---------------------------------


@contextmanager
def _guard(root: Path, name: str, *, blocking: bool) -> Iterator[None]:
    _private_dir(root)
    with (root / f".{name}.guard").open("a") as stream:
        # A blocking guard is a non-blocking retry with a checkpoint before
        # every retry (R1): a stop request is honoured while waiting.
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not blocking:
                    raise LabError("LAB_OPERATION_IN_PROGRESS") from None
            checkpoint()
            time.sleep(GUARD_RETRY_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _reservation(root: Path) -> dict[str, Any] | None:
    path = root / "occupation.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_bytes())
    except ValueError:
        raise LabError("LAB_OCCUPIED") from None
    if not isinstance(value, dict):
        raise LabError("LAB_OCCUPIED")
    return value


def _check_reservation(
    record: dict[str, Any] | None, holder: str, batch: str, now: datetime
) -> None:
    if not record:
        raise LabError("LAB_RESERVATION_REQUIRED")
    if (record.get("holder"), record.get("batch")) != (holder, batch):
        raise LabError("LAB_OCCUPIED")
    try:
        until = datetime.fromisoformat(str(record.get("expected_end")))
    except ValueError:
        raise LabError("LAB_RESERVATION_EXPIRED") from None
    if until.tzinfo is None or until <= now:
        # Expired reservations are never extended or reused (no stealing).
        raise LabError("LAB_RESERVATION_EXPIRED")


def check_occupation(root: Path, holder: str, batch: str, now: datetime) -> None:
    """A live reservation held by exactly this holder/batch; never claims."""
    if not holder.strip() or not batch.strip():
        raise LabError("LAB_RESERVATION_REQUIRED")
    with _guard(root, "occupation", blocking=True):
        _check_reservation(_reservation(root), holder, batch, now)


@contextmanager
def active_marker(
    root: Path, holder: str, batch: str, round_id: str, now: datetime
) -> Iterator[Path]:
    """F11's ``.active-*.json``: blocks ``occupation release`` while running."""
    marker = root / f".active-{uuid4().hex}.json"
    # Interruptible until the lock is held; the creation and the removal are
    # protected, the body (``yield``) is not (R4).
    with _guard(root, "occupation", blocking=True), protected():
        _check_reservation(_reservation(root), holder, batch, now)
        write_private(
            marker,
            _json(dict(holder=holder, batch=batch, pid=os.getpid(), round=round_id)),
        )
    try:
        yield marker
    finally:
        with protected():
            marker.unlink(missing_ok=True)


@contextmanager
def fault_guard(root: Path) -> Iterator[None]:
    """F11's ``.fault.guard``, non-blocking: one lab mutation/observation."""
    with _guard(root, "fault", blocking=False):
        yield


def _run_start_head(path: Path, round_id: str) -> str:
    """The chain head of ``run-starts.jsonl``; the round must not be in it."""
    raw = path.read_bytes() if path.exists() else b""
    previous = "0" * 64
    for line in raw.splitlines():
        try:
            row = json.loads(line)
            body = {k: v for k, v in row.items() if k != "record_sha256"}
            if row["previous_sha256"] != previous or row["record_sha256"] != sha256(
                canonical_json(body).encode()
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise LabError("BATCH_LOG_INVALID") from None
        if row.get("round") == round_id:
            raise LabError("ROUND_ALREADY_STARTED")
        previous = row["record_sha256"]
    return str(previous)


def check_run_start(root: Path, round_id: str) -> None:
    """``append_run_start``'s checks without the append: the guard opens, the
    chain is valid, the round is new and the log (or its directory) is
    writable. Raises LabError, or OSError for a guard that cannot open."""
    with _guard(root, "batch-journal", blocking=True):
        path = root / "run-starts.jsonl"
        _run_start_head(path, round_id)
        if not os.access(path if path.exists() else root, os.W_OK):
            raise LabError("LAB_LEDGER_UNAVAILABLE")


def append_run_start(
    root: Path, round_id: str, holder: str, batch: str, now: datetime
) -> dict[str, Any]:
    """One ``run_started`` row in F11's hash-chained ``run-starts.jsonl``."""
    with _guard(root, "batch-journal", blocking=True), protected():
        path = root / "run-starts.jsonl"
        previous = _run_start_head(path, round_id)
        entry: dict[str, Any] = dict(
            format_version=1,
            kind="run_started",
            batch=batch,
            round=round_id,
            holder=holder,
            started_at=now.isoformat(),
            start_id=str(uuid4()),
            previous_sha256=previous,
        )
        entry["record_sha256"] = sha256(canonical_json(entry).encode())
        with path.open("a") as stream:
            os.chmod(path, 0o600)
            stream.write(canonical_json(entry) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return entry


# -- flags --------------------------------------------------------------------


def _flags(raw: bytes) -> Mapping[str, Any]:
    try:
        data = json.loads(raw)
        flags = data["flags"]
    except (ValueError, KeyError, TypeError):
        raise LabError("FLAG_OR_VARIANT_UNCONFIRMED") from None
    if not isinstance(flags, dict):
        raise LabError("FLAG_OR_VARIANT_UNCONFIRMED")
    return flags


def flagd_method(raw: bytes, flag: str) -> str:
    """The flagd Resolve method for ``flag``, from its variants' value type."""
    entry = _flags(raw).get(flag)
    variants = entry.get("variants") if isinstance(entry, dict) else None
    if not isinstance(variants, dict) or not variants:
        raise LabError("FLAGD_METHOD_UNCONFIRMED")
    kinds = set()
    for value in variants.values():
        if isinstance(value, bool):
            kinds.add("Boolean")
        elif isinstance(value, (int, float)):
            kinds.add("Float")
        elif isinstance(value, str):
            kinds.add("String")
        else:
            kinds.add("Object")
    if len(kinds) != 1 or kinds == {"Object"}:
        raise LabError("FLAGD_METHOD_UNCONFIRMED")
    return kinds.pop()


def fault_for(flag: str, variant: str) -> FaultFlag:
    fault = FAULT_FLAGS.get(flag)
    if fault is None or fault.fault_variant != variant:
        raise LabError("FAULT_NOT_WHITELISTED")
    return fault


def mutate(raw: bytes, fault: FaultFlag) -> bytes:
    try:
        return bytes(
            kind_lab.mutate_flags(
                raw, fault.fault_variant, fault.normal_variant, flag=fault.flag
            )
        )
    except SystemExit:
        raise LabError("FLAG_OR_VARIANT_UNCONFIRMED") from None


def _remaining(clock: LiveClock, started: float, budget: float) -> float:
    return budget - (clock.monotonic() - started)


def await_variant(
    deps: LiveDeps,
    flag: str,
    method: str,
    variant: str,
    *,
    budget_seconds: float = PROPAGATION_SECONDS,
    poll_seconds: float = POLL_SECONDS,
    probe_timeout: float = PROBE_SECONDS,
) -> datetime:
    """The first time flagd resolves ``flag`` to ``variant`` (daemon only)."""
    clock = deps.clock
    started = clock.monotonic()
    while (left := _remaining(clock, started, budget_seconds)) > 0:
        checkpoint()
        resolved = deps.flagd.resolve(flag, method, min(probe_timeout, left))
        checkpoint()
        if resolved == variant:
            return clock.now()
        left = _remaining(clock, started, budget_seconds)
        if left <= 0:
            break
        clock.sleep(min(poll_seconds, left))
    raise LabError("FLAG_PROPAGATION_UNCONFIRMED")


def confirm_all_normal(
    deps: LiveDeps, config: FlagConfig, *, budget_seconds: float = PROPAGATION_SECONDS
) -> datetime:
    """Every whitelisted flag off in the ConfigMap *and* on the daemon."""
    flags = _flags(config.raw)
    for fault in FAULT_FLAGS.values():
        entry = flags.get(fault.flag)
        if not isinstance(entry, dict) or (
            entry.get("defaultVariant") != fault.normal_variant
        ):
            raise LabError("FLAGS_NOT_NORMAL")
    confirmed = deps.clock.now()
    for fault in FAULT_FLAGS.values():
        method = flagd_method(config.raw, fault.flag)
        try:
            confirmed = await_variant(
                deps,
                fault.flag,
                method,
                fault.normal_variant,
                budget_seconds=budget_seconds,
            )
        except LabError:
            raise LabError("FLAGS_NOT_NORMAL") from None
    return confirmed


# -- fault intent and restore (U2) --------------------------------------------


def read_state(root: Path) -> dict[str, Any] | None:
    path = root / STATE_FILE
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_bytes())
    except ValueError:
        raise LabError("FAULT_STATE_INVALID") from None
    if not isinstance(value, dict):
        raise LabError("FAULT_STATE_INVALID")
    return value


def _write_state(root: Path, state: Mapping[str, Any], *, exclusive: bool) -> None:
    try:
        write_private(root / STATE_FILE, _json(dict(state)), exclusive=exclusive)
    except FileExistsError:
        raise LabError("FAULT_RECOVERY_REQUIRED") from None


@dataclass(frozen=True)
class FaultIntent:
    round_id: str
    attempt_id: str
    holder: str
    batch: str
    fault: FaultFlag


@dataclass(frozen=True)
class Injected:
    original_sha256: str
    injected_sha256: str
    effective_at: datetime
    requested_at: datetime


@dataclass(frozen=True)
class Committed:
    """An injection written and verified in the ConfigMap, not yet effective."""

    original_sha256: str
    injected_sha256: str
    requested_at: datetime
    flag: str
    method: str
    variant: str


def inject_fault(
    intent: FaultIntent, *, deps: LiveDeps, ledger: AttemptLedger
) -> Injected:
    """``commit_fault`` then ``await_fault``, for callers that keep nothing
    in between."""
    committed = commit_fault(intent, deps=deps, ledger=ledger)
    effective = await_fault(committed, deps=deps, ledger=ledger)
    return Injected(
        committed.original_sha256,
        committed.injected_sha256,
        effective,
        committed.requested_at,
    )


def commit_fault(
    intent: FaultIntent,
    *,
    deps: LiveDeps,
    ledger: AttemptLedger,
    record: Callable[[Committed], None] | None = None,
) -> Committed:
    """Persist the restore intent, CAS-inject and verify the bytes (R6).

    The caller holds ``fault_guard``. Any failure after the intent is written
    leaves the state file for ``restore_fault`` (the caller always runs it).
    Everything from the first evidence write to ``record`` -- the caller's
    copy of the hashes and request time -- is one protected section, so the
    fault metadata exists before the propagation wait starts.
    """
    root = deps.ledger_root
    if read_state(root) is not None:
        # Someone else's intent: never written over, never restored by us.
        raise LabError("LAB_FAULT_FOREIGN")
    current = deps.flags.read()
    confirm_all_normal(deps, current)
    fault = intent.fault
    method = flagd_method(current.raw, fault.flag)
    injected = mutate(current.raw, fault)
    original_sha, injected_sha = sha256(current.raw), sha256(injected)
    checkpoint()
    with protected():
        requested = _commit(intent, current, injected, deps=deps, ledger=ledger)
        committed = Committed(
            original_sha,
            injected_sha,
            requested,
            fault.flag,
            method,
            fault.fault_variant,
        )
        if record is not None:
            record(committed)
    return committed


def _commit(
    intent: FaultIntent,
    current: FlagConfig,
    injected: bytes,
    *,
    deps: LiveDeps,
    ledger: AttemptLedger,
) -> datetime:
    root = deps.ledger_root
    fault = intent.fault
    original_sha, injected_sha = sha256(current.raw), sha256(injected)
    ledger.write("flags-original.json", {"raw": current.raw.decode()})
    ledger.write("flags-injected.json", {"raw": injected.decode()})
    requested = deps.clock.now()
    state: dict[str, Any] = dict(
        ledger=str(ledger.directory.resolve()),
        flag=fault.flag,
        normal=fault.normal_variant,
        variant=fault.fault_variant,
        original=current.raw.decode(),
        injected=injected.decode(),
        planned_at=requested.isoformat(),
        planned_restore_at=None,
        owner=OWNER,
        round=intent.round_id,
        attempt_id=intent.attempt_id,
        holder=intent.holder,
        batch=intent.batch,
        configmap_uid=current.uid,
        resource_version_before=current.resource_version,
        original_sha256=original_sha,
        injected_sha256=injected_sha,
        phase="intent",
    )
    _write_state(root, state, exclusive=True)
    ledger.event("inject", "intent_written", flag=fault.flag, sha256=injected_sha)
    # Every ConfigMap write carries a fence of its own (r7).
    deps.flags.replace(current, injected, fence=uuid4().hex)
    live = deps.flags.read()
    if sha256(live.raw) != injected_sha or live.uid != current.uid:
        raise LabError("FLAG_CONFIG_PATCH_UNVERIFIED")
    state["phase"] = "injected"
    _write_state(root, state, exclusive=False)
    ledger.event("inject", "config_verified", sha256=injected_sha)
    return requested


def await_fault(
    committed: Committed, *, deps: LiveDeps, ledger: AttemptLedger
) -> datetime:
    """The flagd propagation of a committed injection (interruptible)."""
    effective = await_variant(deps, committed.flag, committed.method, committed.variant)
    ledger.event("propagation", "flagd_effective", at=effective.isoformat())
    return effective


@dataclass(frozen=True)
class Restored:
    restored_sha256: str
    normal_confirmed_at: datetime
    wash: PreObservation


def restore_fault(
    round_id: str,
    attempt_id: str,
    signatures: Sequence[SignatureRule],
    *,
    deps: LiveDeps,
    ledger: AttemptLedger,
) -> Restored | None:
    """Idempotent restore of this attempt's retained fault, then the wash.

    ``None`` when no state exists. Never overwrites a change it did not make:
    only the recorded injection (version-conditioned) or, when the original
    is already live, a fence write that excludes a late injection commit.
    The state file is removed only after the wash passes. The whole restore,
    from reading the intent to the last ledger event, is protected (r16).
    """
    with protected():
        return _restore(round_id, attempt_id, signatures, deps=deps, ledger=ledger)


def _restore(
    round_id: str,
    attempt_id: str,
    signatures: Sequence[SignatureRule],
    *,
    deps: LiveDeps,
    ledger: AttemptLedger,
) -> Restored | None:
    root = deps.ledger_root
    state = read_state(root)
    if state is None:
        return None
    if (
        state.get("owner") != OWNER
        or state.get("round") != round_id
        or state.get("attempt_id") != attempt_id
    ):
        raise LabError("FAULT_BELONGS_TO_ANOTHER_RUN")
    original = str(state["original"]).encode()
    injected = str(state["injected"]).encode()
    live = deps.flags.read()
    if live.uid != state.get("configmap_uid"):
        raise LabError("FLAG_OBJECT_REPLACED")
    if live.raw not in (original, injected):
        ledger.event("restore", "refused", reason="FLAGS_CHANGED_REFUSE_RESTORE")
        raise LabError("FLAGS_CHANGED_REFUSE_RESTORE")
    # A fresh fence on this write too, injected -> original included (r7):
    # only our own fence proves an unknown result was our write.
    fence = uuid4().hex
    unknown = False
    try:
        deps.flags.replace(live, original, fence=fence)
    except LabError as exc:
        # Only an indeterminate result is decided by the re-read below; a
        # definitive refusal (FLAG_CAS_CONFLICT) means someone else wrote
        # first: stop, keep the intent, never overwrite (r6).
        if exc.code != "FLAG_WRITE_UNKNOWN":
            raise
        unknown = True
        ledger.event("restore", "write_unknown")
    after = deps.flags.read()
    if (
        sha256(after.raw) != state.get("original_sha256")
        or after.uid != live.uid
        or after.resource_version == live.resource_version
        # An advanced version alone may be another writer's; an unknown
        # write counts only when our own fence value is visible.
        or (unknown and after.fence != fence)
    ):
        raise LabError("FLAG_RESTORE_UNVERIFIED")
    ledger.event("restore", "config_verified", sha256=sha256(after.raw))
    normal_at = await_variant(
        deps,
        str(state["flag"]),
        flagd_method(original, str(state["flag"])),
        str(state["normal"]),
    )
    state["phase"] = "restored_unwashed"
    _write_state(root, state, exclusive=False)
    ledger.event("restore", "flagd_normal", at=normal_at.isoformat())
    wash = normal_window(signatures, not_before=normal_at, deps=deps)
    ledger.write(f"wash-{ledger.seq}.json", _raw(wash))
    if not wash.passed:
        ledger.event("wash", "failed", reason=wash.reason)
        # The wash is the section 6 normal window; its own code is the cause.
        raise LabError(wash.reason or "NORMAL_WINDOW_UNCONFIRMED")
    (root / STATE_FILE).unlink()
    ledger.event("wash", "passed", window=_window_json(wash.window))
    return Restored(sha256(after.raw), normal_at, wash)


# -- pre-observation (U1) -----------------------------------------------------


@dataclass(frozen=True)
class PreObservation:
    kind: Literal["fault", "normal"]
    passed: bool
    reason: str | None
    flag_confirmed_at: datetime | None
    window: Window | None
    matched: tuple[ObservedSpan, ...]
    checkout_calls: float | None
    probes: int
    raw: tuple[Mapping[str, Any], ...] = field(default=(), compare=False)


def span_error(tags: Mapping[str, Any]) -> bool:
    """Shown status tags only; the gate's ``_error_by_tags`` rule."""
    if tags.get("error") is True or tags.get("otel.status_code") == "ERROR":
        return True
    grpc = tags.get("rpc.grpc.status_code")
    if grpc is not None and str(grpc) not in {"0", "None"}:
        return True
    return str(tags.get("http.status_code", "")).startswith("5")


def _hits(
    query: SpanQuery, rule: SignatureRule, start: datetime, end: datetime
) -> list[ObservedSpan]:
    wanted = {(m.service, m.operation) for m in rule.any_of}
    return [
        span
        for span in query.spans
        if (span.service, span.operation) in wanted
        and span_error(span.tags)
        and start <= span.start <= end
    ]


def _unique(spans: Sequence[ObservedSpan]) -> list[ObservedSpan]:
    """First occurrence of each (trace_id, span_id), order kept."""
    seen: set[tuple[str, str]] = set()
    unique: list[ObservedSpan] = []
    for span in spans:
        key = (span.trace_id, span.span_id)
        if key not in seen:
            seen.add(key)
            unique.append(span)
    return unique


def _window_json(window: Window | None) -> dict[str, str] | None:
    if window is None:
        return None
    return {"start": window.start.isoformat(), "end": window.end.isoformat()}


def observation_json(result: PreObservation) -> dict[str, Any]:
    return {
        "kind": result.kind,
        "passed": result.passed,
        "reason": result.reason,
        "flag_confirmed_at": None
        if result.flag_confirmed_at is None
        else result.flag_confirmed_at.isoformat(),
        "window": _window_json(result.window),
        "matched_span_ids": [span.span_id for span in result.matched],
        "trace_ids": sorted({span.trace_id for span in result.matched}),
        "checkout_calls": result.checkout_calls,
        "probes": result.probes,
    }


def _raw(result: PreObservation) -> dict[str, Any]:
    return {**observation_json(result), "raw": [dict(item) for item in result.raw]}


def _span_row(span: ObservedSpan) -> dict[str, Any]:
    return {
        "trace_id": span.trace_id,
        "span_id": span.span_id,
        "service": span.service,
        "operation": span.operation,
        "start": span.start.isoformat(),
        "tags": dict(span.tags),
    }


def _query_spans(
    deps: LiveDeps,
    service: str,
    start: datetime,
    end: datetime,
    probe_timeout: float,
    deadline: float,
) -> SpanQuery:
    """One service's spans, bounded by the stage deadline (r13), with a
    checkpoint before and after the request (R1)."""
    left = deadline - deps.clock.monotonic()
    if left < 0:
        return SpanQuery((), False)
    checkpoint()
    within = getattr(deps.telemetry, "spans_within", None)
    if within is not None:
        result: SpanQuery = within(
            service,
            start,
            end,
            request_timeout=min(probe_timeout, max(left, 0.01)),
            deadline=deadline,
        )
    else:
        result = deps.telemetry.spans(
            service, start, end, min(probe_timeout, max(left, 0.01))
        )
    checkpoint()
    return result


def _checkout_calls(
    deps: LiveDeps, start: datetime, end: datetime, timeout: float
) -> CallCount:
    checkpoint()
    count = deps.telemetry.checkout_calls(start, end, timeout)
    checkpoint()
    return count


def fault_gate(
    signature: SignatureRule,
    *,
    confirmed_at: datetime,
    deps: LiveDeps,
    budget_seconds: float = PROPAGATION_SECONDS,
    poll_seconds: float = POLL_SECONDS,
    probe_timeout: float = PROBE_SECONDS,
) -> PreObservation:
    """>=1 signature error span after the flag was confirmed, with traffic."""
    clock = deps.clock
    started = clock.monotonic()
    services = sorted({m.service for m in signature.any_of})
    probes = 0
    raw: list[Mapping[str, Any]] = []
    calls: float | None = None
    while _remaining(clock, started, budget_seconds) > 0:
        checkpoint()
        end = clock.now()
        complete = end >= confirmed_at
        matched: list[ObservedSpan] = []
        deadline = started + budget_seconds
        for service in services:
            query = _query_spans(
                deps, service, confirmed_at, end, probe_timeout, deadline
            )
            probes += 1
            complete = complete and query.complete
            matched += _hits(query, signature, confirmed_at, end)
            raw.append(
                {
                    "service": service,
                    "start": confirmed_at.isoformat(),
                    "end": end.isoformat(),
                    "complete": query.complete,
                    "spans": [_span_row(span) for span in query.spans],
                }
            )
        count = _checkout_calls(
            deps,
            confirmed_at,
            end,
            min(probe_timeout, max(_remaining(clock, started, budget_seconds), 0.01)),
        )
        probes += 1
        raw.append({"checkout_calls": count.value, "complete": count.complete})
        calls = count.value
        # Jaeger returns whole traces, so a span seen through two signature
        # services' searches arrives twice; count and report it once.
        matched = _unique(matched)
        if _remaining(clock, started, budget_seconds) < 0:
            # Results that arrived after the stage deadline never pass (r13).
            break
        if (
            complete
            and count.complete
            and count.value is not None
            and count.value > 0
            and len(matched) >= signature.min_matches
        ):
            return PreObservation(
                "fault",
                True,
                None,
                confirmed_at,
                Window(confirmed_at, end),
                tuple(sorted(matched, key=lambda s: (s.start, s.span_id))),
                count.value,
                probes,
                tuple(raw),
            )
        left = _remaining(clock, started, budget_seconds)
        if left <= 0:
            break
        clock.sleep(min(poll_seconds, left))
    return PreObservation(
        "fault",
        False,
        "FAULT_NOT_OBSERVED",
        confirmed_at,
        None,
        (),
        calls,
        probes,
        tuple(raw),
    )


def normal_window(
    signatures: Sequence[SignatureRule],
    *,
    not_before: datetime,
    deps: LiveDeps,
    window_seconds: float = WINDOW_SECONDS,
    budget_seconds: float = WASH_SECONDS,
    probe_timeout: float = PROBE_SECONDS,
) -> PreObservation:
    """A complete fresh window with checkout traffic and zero signature hits."""
    clock = deps.clock
    started = clock.monotonic()
    services = sorted({m.service for rule in signatures for m in rule.any_of})
    start = not_before
    probes = 0
    raw: list[Mapping[str, Any]] = []
    calls: float | None = None
    while True:
        checkpoint()
        end = start + timedelta(seconds=window_seconds)
        wait = (end - clock.now()).total_seconds()
        if wait > 0:
            if wait > _remaining(clock, started, budget_seconds):
                break
            clock.sleep(wait)
            checkpoint()  # an interruptible sleep returns early on a stop
        # The budget is inclusive (U1 "at most 600 s"): a window that closes
        # exactly at it is still read.
        if _remaining(clock, started, budget_seconds) < 0:
            break
        complete = True
        hits: list[ObservedSpan] = []
        deadline = started + budget_seconds
        for service in services:
            query = _query_spans(deps, service, start, end, probe_timeout, deadline)
            probes += 1
            complete = complete and query.complete
            for rule in signatures:
                hits += _hits(query, rule, start, end)
            raw.append(
                {
                    "service": service,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "complete": query.complete,
                    "spans": [_span_row(span) for span in query.spans],
                }
            )
        count = _checkout_calls(
            deps,
            start,
            end,
            min(probe_timeout, max(_remaining(clock, started, budget_seconds), 0.01)),
        )
        probes += 1
        raw.append({"checkout_calls": count.value, "complete": count.complete})
        calls = count.value
        if _remaining(clock, started, budget_seconds) < 0:
            # A window judged after the stage deadline is never clean (r13).
            break
        if (
            complete
            and count.complete
            and count.value is not None
            and count.value > 0
            and not hits
        ):
            return PreObservation(
                "normal",
                True,
                None,
                not_before,
                Window(start, end),
                (),
                count.value,
                probes,
                tuple(raw),
            )
        # A dirty or incomplete window: start a fresh one from now.
        start = max(clock.now(), end)
    return PreObservation(
        "normal",
        False,
        "NORMAL_WINDOW_UNCONFIRMED",
        not_before,
        None,
        (),
        calls,
        probes,
        tuple(raw),
    )


# -- real adapters (live only; never used by offline tests) -------------------


class ProbeEnvChecker:
    """The probe needs only the lab Prometheus account (no model, no trace)."""

    def check(self) -> str | None:
        return None if kind_lab.lab_authorization() is not None else "ENV_UNCONFIRMED"


class Unavailable:
    """A seat a probe must never use (investigator, PG, balance)."""

    def __getattr__(self, name: str) -> Any:
        raise LabError("PROBE_SEAM_UNAVAILABLE")


class GitProvenance:
    """HEAD and whether tracked files are clean, from ``git`` (live only)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _git(self, *args: str) -> str | None:
        try:
            with external_command():
                result = subprocess.run(
                    ["git", "-C", str(self.root), *args],
                    capture_output=True,
                    timeout=10,
                    check=False,
                    start_new_session=True,
                )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.decode().strip() if result.returncode == 0 else None

    def read(self) -> Provenance:
        commit = self._git("rev-parse", "HEAD")
        status = self._git("status", "--porcelain", "--untracked-files=no")
        return Provenance(commit or None, status == "")


class LabEnvChecker:
    """The lab trace target and the otel-demo profile, from ``os.environ``
    (credentials were already read by ``prepare_round``)."""

    def check(self) -> str | None:
        from opspilot import tracing

        env = os.environ
        if env.get("OPSPILOT_TOOL_PROFILE") != "otel-demo":
            return "ENV_UNCONFIRMED"
        return "ENV_UNCONFIRMED" if tracing.check_lab_target(env) else None


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        interruptible_sleep(seconds)


def _external(command: list[str], *, timeout: float, data: str | None = None) -> bytes:
    """No shell and no stderr echo: kubectl output may hold deployment data."""
    try:
        with external_command():
            result = subprocess.run(
                command,
                input=None if data is None else data.encode(),
                capture_output=True,
                timeout=timeout,
                check=False,
                env=kind_lab.lab_env(),
                start_new_session=True,
            )
    except (subprocess.TimeoutExpired, OSError):
        raise LabError("EXTERNAL_CALL_TIMEOUT") from None
    if result.returncode:
        raise LabError("EXTERNAL_CALL_FAILED")
    if len(result.stdout) > 10 * 1024 * 1024:
        raise LabError("EXTERNAL_RESPONSE_TOO_LARGE")
    return result.stdout


def _configmap(document: Mapping[str, Any]) -> FlagConfig:
    try:
        annotations = document["metadata"].get("annotations") or {}
        fence = annotations.get(FENCE_ANNOTATION)
        return FlagConfig(
            raw=str(document["data"][kind_lab.FLAG_FILE]).encode(),
            uid=str(document["metadata"]["uid"]),
            resource_version=str(document["metadata"]["resourceVersion"]),
            fence=fence if isinstance(fence, str) else None,
        )
    except (KeyError, TypeError, AttributeError):
        raise LabError("FLAG_READ_FAILED") from None


# kubectl's stderr when the API server definitively refused a JSON patch: a
# failed ``test`` op, a 409 Conflict or a resourceVersion precondition. Such
# a write did not happen; any other non-zero exit is indeterminate (r6).
DEFINITIVE_REFUSALS = (
    "testing value",  # jsonpatch: "testing value /data/x failed: test failed"
    "test failed",
    "(conflict)",  # 409: "Error from server (Conflict): ..."
    "the object has been modified",
    "precondition failed",  # uid/resourceVersion preconditions
)


def _patch(command: list[str], *, timeout: float) -> bytes:
    """``kubectl patch`` stdout; ``FLAG_CAS_CONFLICT`` for a definitive
    refusal, ``FLAG_WRITE_UNKNOWN`` only when the outcome cannot be known."""
    try:
        with external_command():
            result = subprocess.run(
                command,
                capture_output=True,
                timeout=timeout,
                check=False,
                env=kind_lab.lab_env(),
                start_new_session=True,
            )
    except (subprocess.TimeoutExpired, OSError):
        raise LabError("FLAG_WRITE_UNKNOWN") from None
    if result.returncode:
        stderr = result.stderr.decode("utf-8", "replace").lower()
        if any(marker in stderr for marker in DEFINITIVE_REFUSALS):
            raise LabError("FLAG_CAS_CONFLICT")
        raise LabError("FLAG_WRITE_UNKNOWN")
    if len(result.stdout) > 10 * 1024 * 1024:
        raise LabError("FLAG_WRITE_UNKNOWN")
    return result.stdout


class KubectlFlagStore:
    """``flagd-config`` through the lab kubeconfig/context only."""

    def __init__(self) -> None:
        # Annotations of the last read, by resourceVersion: the fence write
        # replaces the whole map, and the CAS test guarantees it is current.
        self._annotations: dict[str, dict[str, Any]] = {}

    def read(self) -> FlagConfig:
        try:
            document = json.loads(
                _external(
                    kind_lab.kubectl(
                        "-n",
                        kind_lab.NAMESPACE,
                        "get",
                        "configmap",
                        "flagd-config",
                        "-o",
                        "json",
                    ),
                    timeout=20,
                )
            )
        except (LabError, ValueError):
            raise LabError("FLAG_READ_FAILED") from None
        config = _configmap(document)
        annotations = document.get("metadata", {}).get("annotations") or {}
        self._annotations = {config.resource_version: dict(annotations)}
        return config

    def replace(
        self, current: FlagConfig, new_raw: bytes, *, fence: str | None
    ) -> FlagConfig:
        escaped = kind_lab.FLAG_FILE.replace("~", "~0").replace("/", "~1")
        patch: list[dict[str, Any]] = [
            {"op": "test", "path": "/metadata/uid", "value": current.uid},
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": current.resource_version,
            },
            {"op": "test", "path": f"/data/{escaped}", "value": current.raw.decode()},
            {"op": "replace", "path": f"/data/{escaped}", "value": new_raw.decode()},
        ]
        if fence is not None:
            annotations = self._annotations.get(current.resource_version)
            if annotations is None:
                raise LabError("FLAG_READ_FAILED")
            patch.append(
                {
                    "op": "add",
                    "path": "/metadata/annotations",
                    "value": {**annotations, FENCE_ANNOTATION: fence},
                }
            )
        raw = _patch(
            kind_lab.kubectl(
                "-n",
                kind_lab.NAMESPACE,
                "patch",
                "configmap",
                "flagd-config",
                "--type=json",
                "-p",
                canonical_json(patch),
                "-o",
                "json",
            ),
            timeout=60,
        )
        try:
            document = json.loads(raw)
        except ValueError:
            # The server accepted the patch but its answer is unreadable.
            raise LabError("FLAG_WRITE_UNKNOWN") from None
        written = _configmap(document)
        if fence is not None and (
            written.resource_version == current.resource_version
            or (document.get("metadata", {}).get("annotations") or {}).get(
                FENCE_ANNOTATION
            )
            != fence
        ):
            raise LabError("FLAG_RESTORE_FENCE_UNVERIFIED")
        return written


class HttpFlagResolver:
    """flagd's HTTP Connect evaluation API on an already forwarded port."""

    def __init__(self, url: str) -> None:
        self.url = check_flagd_url(url).rstrip("/")

    def resolve(self, flag: str, method: str, timeout: float) -> str | None:
        request = urllib.request.Request(
            f"{self.url}/flagd.evaluation.v1.Service/Resolve{method}",
            data=json.dumps({"flagKey": flag, "context": {}}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with kind_lab.OPENER.open(request, timeout=max(timeout, 0.01)) as response:
                body = json.loads(response.read(30720))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            return None
        variant = body.get("variant") if isinstance(body, dict) else None
        return variant if isinstance(variant, str) else None


def check_flagd_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = None
    if (
        parts.scheme != "http"
        or parts.hostname not in {"127.0.0.1", "localhost", "::1"}
        or port is None
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
    ):
        raise LabError("LOCAL_FLAGD_URL_REQUIRED")
    return url


class LabTelemetry:
    """Independent reads: Jaeger search and the lab account on Prometheus."""

    def __init__(self, clock: LiveClock | None = None) -> None:
        # The same monotonic scale as the gates' stage deadline (r13).
        self.clock = clock or SystemClock()

    def spans(
        self, service: str, start: datetime, end: datetime, timeout: float
    ) -> SpanQuery:
        return self.spans_within(
            service, start, end, request_timeout=timeout, deadline=None
        )

    def spans_within(
        self,
        service: str,
        start: datetime,
        end: datetime,
        *,
        request_timeout: float,
        deadline: float | None,
    ) -> SpanQuery:
        """This service's error spans in the window, complete or flagged not.

        A full page may hide traces, so it is discarded and its sub-window
        split in ``SPLIT_FACTOR`` until every page stays under the limit
        (r12). ``request_timeout`` bounds each HTTP request and, with a
        ``deadline`` (this probe's monotonic clock), never outlasts the stage
        (r13); the request count, the bytes read and the smallest sub-window
        are capped too, and reaching any limit is ``complete=False`` -- never
        a clean answer.
        """
        found: dict[tuple[str, str], ObservedSpan] = {}
        requests = read = 0
        pending = [
            (int(start.timestamp() * 1_000_000), int(end.timestamp() * 1_000_000))
        ]
        while pending:
            low, high = pending.pop()
            if requests >= MAX_SUBREQUESTS:
                return SpanQuery(tuple(found.values()), False)
            timeout = request_timeout
            if deadline is not None:
                left = deadline - self.clock.monotonic()
                if left <= 0:
                    # The stage is out of time: what is missing stays missing.
                    return SpanQuery(tuple(found.values()), False)
                timeout = min(request_timeout, left)
            requests += 1
            query = urllib.parse.urlencode(
                {"service": service, "start": low, "end": high, "limit": TRACE_LIMIT}
            )
            checkpoint()  # before and after every sub-request (R1)
            payload, size = _get_json_sized(
                f"{kind_lab.JAEGER}/api/traces?{query}", timeout
            )
            checkpoint()
            read += size
            if read > MAX_SEARCH_BYTES or not isinstance(payload, dict):
                return SpanQuery(tuple(found.values()), False)
            traces = payload.get("data")
            if not isinstance(traces, list):
                return SpanQuery(tuple(found.values()), False)
            if len(traces) >= TRACE_LIMIT:
                width = high - low
                if width < MIN_SUBWINDOW_US * SPLIT_FACTOR:
                    return SpanQuery(tuple(found.values()), False)
                step = width // SPLIT_FACTOR
                bounds = [low + step * i for i in range(SPLIT_FACTOR)] + [high]
                for index in range(SPLIT_FACTOR):
                    first = bounds[index] if index == 0 else bounds[index] + 1
                    pending.append((first, bounds[index + 1]))
                continue
            if not _collect_errors(traces, service, found):
                return SpanQuery(tuple(found.values()), False)
        return SpanQuery(tuple(found.values()), True)

    def checkout_calls(
        self, start: datetime, end: datetime, timeout: float
    ) -> CallCount:
        seconds = max(int((end - start).total_seconds()), 1)
        expr = (
            "sum(increase(traces_span_metrics_calls_total"
            f'{{service_name="checkout"}}[{seconds}s]))'
        )
        query = urllib.parse.urlencode(
            {"query": expr, "time": f"{end.timestamp():.3f}"}
        )
        status, payload = kind_lab.get_json(
            f"{kind_lab.PROMETHEUS}/api/v1/query?{query}",
            timeout=max(timeout, 0.01),
            authorization=kind_lab.lab_authorization(),
        )
        if (
            status != 200
            or not isinstance(payload, dict)
            or payload.get("status") != "success"
        ):
            return CallCount(None, False)
        try:
            rows = payload["data"]["result"]
            value = float(rows[0]["value"][1]) if rows else 0.0
        except (KeyError, IndexError, TypeError, ValueError):
            return CallCount(None, False)
        return CallCount(value, True)


def _get_json_sized(url: str, timeout: float) -> tuple[Any, int]:
    """A bounded anonymous GET (Jaeger): (payload or None, bytes read)."""
    request = urllib.request.Request(url, method="GET")
    try:
        with kind_lab.OPENER.open(request, timeout=max(timeout, 0.01)) as response:
            if response.status != 200:
                return None, 0
            raw = response.read(MAX_TELEMETRY_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None, 0
    if len(raw) > MAX_TELEMETRY_BYTES:
        return None, len(raw)
    try:
        return json.loads(raw), len(raw)
    except ValueError:
        return None, len(raw)


def _collect_errors(
    traces: list[Any], service: str, found: dict[tuple[str, str], ObservedSpan]
) -> bool:
    """Add ``service``'s error spans of a page to ``found``; False if malformed."""
    try:
        for trace in traces:
            processes = {
                key: value["serviceName"] for key, value in trace["processes"].items()
            }
            for span in trace["spans"]:
                if processes.get(span["processID"]) != service:
                    continue
                tags = {tag["key"]: tag["value"] for tag in span.get("tags") or []}
                if not span_error(tags):
                    continue
                key = (str(trace["traceID"]), str(span["spanID"]))
                found.setdefault(
                    key,
                    ObservedSpan(
                        trace_id=key[0],
                        span_id=key[1],
                        service=service,
                        operation=str(span["operationName"]),
                        start=datetime.fromtimestamp(
                            span["startTime"] / 1_000_000, tz=timezone.utc
                        ),
                        tags=tags,
                    ),
                )
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return False
    return True


class KindLabIdentity:
    """The expected lab answers on its ports: context, auth and services."""

    def verify(self) -> tuple[str, ...]:
        failures: list[str] = []
        try:
            # ``kubectl()`` pins the lab kubeconfig and context; the demo
            # namespace answering through it is the cluster identity.
            name = (
                _external(
                    kind_lab.kubectl(
                        "get", "namespace", kind_lab.NAMESPACE, "-o", "name"
                    ),
                    timeout=10,
                )
                .decode()
                .strip()
            )
        except LabError:
            name = ""
        if name != f"namespace/{kind_lab.NAMESPACE}":
            failures.append("KUBE_CONTEXT_UNCONFIRMED")
        anonymous, _ = kind_lab.get_json(
            f"{kind_lab.PROMETHEUS}/api/v1/status/buildinfo"
        )
        authorization = kind_lab.lab_authorization()
        authorized, _ = (
            (0, None)
            if authorization is None
            else kind_lab.get_json(
                f"{kind_lab.PROMETHEUS}/api/v1/status/buildinfo",
                authorization=authorization,
            )
        )
        if anonymous != 401 or authorized != 200:
            failures.append("PROMETHEUS_IDENTITY_UNCONFIRMED")
        status, services = kind_lab.get_json(f"{kind_lab.JAEGER}/api/services")
        names = services.get("data") if isinstance(services, dict) else None
        if status != 200 or not isinstance(names, list) or "checkout" not in names:
            failures.append("JAEGER_IDENTITY_UNCONFIRMED")
        return tuple(failures)


class SocketPortProbe:
    def in_use(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            return probe.connect_ex(("127.0.0.1", port)) == 0


def _pg_binary(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    path = Path("/opt/homebrew/opt/postgresql@17/bin") / name
    if not path.is_file():
        raise LabError("POSTGRES_BINARY_MISSING")
    return str(path)


# The only variables the PostgreSQL tools and the postmaster inherit (r11):
# nothing that could carry a model, trace or lab credential.
PG_ENV_KEYS = ("PATH", "HOME", "TMPDIR", "LANG", "TZ")


def pg_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key in PG_ENV_KEYS or key.startswith("LC_")
    }


def _write_pwfile(path: Path, password: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(password + "\n")


class LocalPgLauncher:
    """A throwaway PostgreSQL per attempt: own directory, TCP only on
    127.0.0.1, SCRAM passwords random per attempt (no trust, r11), started
    with a sanitized environment, migrated by the owner, runtime logins for
    the web and worker roles."""

    owner = "eval_owner"
    database = "opspilot_eval"
    logins = {"eval_web": "opspilot_web", "eval_worker": "opspilot_worker"}

    def start(self, directory: Path, port: int) -> PgInstance:
        import secrets

        import psycopg
        from psycopg import sql
        from psycopg.conninfo import make_conninfo

        from opspilot import schema

        if directory.exists() or directory.is_symlink():
            raise LabError("PG_DIRECTORY_EXISTS")
        _private_dir(directory.parent)
        data = directory
        started = False
        passwords = {
            name: secrets.token_urlsafe(32) for name in (self.owner, *self.logins)
        }

        def dsn(user: str, database: str = self.database) -> str:
            return make_conninfo(
                host="127.0.0.1",
                port=str(port),
                dbname=database,
                user=user,
                password=passwords[user],
            )

        pwfile = directory.parent / f".pwfile-{uuid4().hex}"
        try:
            # The owner password reaches initdb through a 0600 file in the
            # attempt's private directory, removed right after (never argv).
            _write_pwfile(pwfile, passwords[self.owner])
            try:
                with external_command():
                    subprocess.run(
                        [
                            _pg_binary("initdb"),
                            "-D",
                            str(data),
                            "-U",
                            self.owner,
                            "--auth=scram-sha-256",
                            f"--pwfile={pwfile}",
                            "--no-locale",
                            "--encoding=UTF8",
                        ],
                        check=True,
                        timeout=120,
                        capture_output=True,
                        env=pg_env(),
                        start_new_session=True,
                    )
            finally:
                pwfile.unlink(missing_ok=True)
            checkpoint()
            with (data / "postgresql.conf").open("a") as config:
                # No Unix socket: a worktree path can exceed the 103-byte limit.
                config.write(
                    f"\nlisten_addresses='127.0.0.1'\nport={port}\n"
                    "unix_socket_directories=''\nshared_buffers=32MB\n"
                    "max_connections=20\nautovacuum=off\n"
                )
            # Set before the start: a timed-out ``pg_ctl -w start`` may still
            # leave a running server; ``stop`` no-ops when there is no pid file.
            checkpoint()
            started = True
            with external_command():
                subprocess.run(
                    [
                        _pg_binary("pg_ctl"),
                        "-D",
                        str(data),
                        "-l",
                        str(data.parent / "postgres.log"),
                        "-w",
                        "start",
                    ],
                    check=True,
                    timeout=120,
                    capture_output=True,
                    # The postmaster inherits this environment, not ours.
                    env=pg_env(),
                    start_new_session=True,
                )
            # A checkpoint before and after every DB operation (R1).
            checkpoint()
            admin = dsn(self.owner, "postgres")
            with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
                conn.execute(f"CREATE DATABASE {self.database}")
            checkpoint()
            owner_dsn = dsn(self.owner)
            schema.migrate(owner_dsn, pg_dump=_pg_binary("pg_dump"))
            checkpoint()
            with psycopg.connect(owner_dsn, autocommit=True, connect_timeout=5) as conn:
                # A fresh cluster: the logins cannot pre-exist, so plain
                # members of the migration's group roles are created here,
                # each with its own random SCRAM password.
                for login, group in self.logins.items():
                    conn.execute(
                        sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE {}").format(
                            sql.Identifier(login),
                            sql.Literal(passwords[login]),
                            sql.Identifier(group),
                        )
                    )
            checkpoint()
        except BaseException as exc:
            if not isinstance(exc, Exception):
                request_stop()  # a caught interrupt is a stop request (R3)
            # The caller never receives an instance it could stop, so the
            # server this call launched is stopped here (its own directory and
            # port only, through ``stop``'s pid check) before failing (r6).
            # The cleanup is protected: the stop finishes and its result is
            # checked before any interrupt is handled (r16, #273 item 1).
            stop_failed = False
            with protected():
                if started:
                    try:
                        self.stop(PgInstance(data, port, "", ""))
                    except Exception:  # noqa: BLE001 - reported as PG_STOP_FAILED
                        stop_failed = True
            if stop_failed:
                # A server that may still listen outranks the start failure
                # and an interrupt: the attempt stops the round (never
                # swallowed, never exit 0); the stop request stays set.
                raise LabError("PG_STOP_FAILED") from None
            if not isinstance(exc, Exception):
                raise  # Interrupted/KeyboardInterrupt keep their own path
            code = exc.code if isinstance(exc, LabError) else "PG_START_FAILED"
            raise LabError(code) from None
        return PgInstance(
            directory=data,
            port=port,
            owner_dsn=owner_dsn,
            runtime_dsn=dsn("eval_worker"),
            web_dsn=dsn("eval_web"),
        )

    def stop(self, instance: PgInstance) -> None:
        pid = instance.directory / "postmaster.pid"
        if not pid.is_file():
            return
        lines = pid.read_text().splitlines()
        # Only the instance this attempt started: its own data directory/port.
        if (
            len(lines) < 4
            or Path(lines[1]) != instance.directory
            or lines[3] != str(instance.port)
        ):
            raise LabError("PG_STOP_FAILED")
        try:
            with external_command():
                subprocess.run(
                    [
                        _pg_binary("pg_ctl"),
                        "-D",
                        str(instance.directory),
                        "-m",
                        "fast",
                        "-w",
                        "stop",
                    ],
                    check=True,
                    timeout=120,
                    capture_output=True,
                    env=pg_env(),
                    start_new_session=True,
                )
        except (subprocess.SubprocessError, OSError):
            raise LabError("PG_STOP_FAILED") from None


class DeepSeekBalance:
    """The account balance before/after an attempt; ``None`` when unknown."""

    def __init__(self, key: str) -> None:
        self._key = key

    def balance(self) -> Decimal | None:
        from scripts.m1_context_latency import balance

        try:
            infos = balance(self._key)["balance_infos"]
            cny = next(item for item in infos if item.get("currency") == "CNY")
            return Decimal(str(cny["total_balance"]))
        except (
            urllib.error.URLError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            StopIteration,
            InvalidOperation,
        ):
            return None
