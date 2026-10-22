"""F3 step 5 competing-cause scenario driver (engineer-only lab script, PR-3a).

Contract: docs/tasks/2026-10-22-f3-pr3a-contract.md (D1-a, D2-a, D4-a, R-1,
R-6, R-7, R-8). Rubric and evaluator-side answer:
docs/testing/f3-competing-cause-rubric.md and evals/f3/competing-cause/.
This is not product code and grants the product nothing: the deployment and
the flag change use the F11 U2 harness's lab-admin path
(PRODUCT-CONSTRAINTS "isolated test harnesses"); the operator note goes
through the product's own human-control entry like a person would send it.

PUBLIC INTERFACE (frozen for independent tests; the body below may change)
==========================================================================

Invocation::

    .venv/bin/python -m scripts.f3_competing_scenario [GLOBAL] <command> [ARGS]

Global options (before the command):
    --dry-run                No external call of any kind (kubectl, flagd,
                             Prometheus, database, HTTP, sysctl, git) and no
                             file write; ``ports`` are never built or used.
    --holder H --batch B     Lab occupation owner; required by ``preflight``
                             and ``run`` unless --dry-run.
    --swap-min-free-mib N    Swap stop line for precondition 7 (default 2048).

Commands:
    plan [--flag-method M]   Print the plan (one JSON object). Never touches
                             ``ports``; --dry-run changes nothing.
    preflight                Evaluate the seven preconditions (one JSON
                             object). With --dry-run every check is
                             ``skipped``.
    run --run NAME --web URL --flagd-url URL --flag-method M
        [--origin O] [--alert-timeout S] [--poll-seconds S]
                             Live scenario, JSON Lines on stdout. With
                             --dry-run: plan, skipped preflight and the
                             action list only.

``--flag-method`` is one of Boolean, String, Int, Float, Object (flagd
Resolve<method>, checked again by the harness). ``--run`` matches
``[A-Za-z0-9_-]{1,64}``. ``--web`` must be http(s) on a loopback host
(127.0.0.1, localhost, ::1); ``--origin`` defaults to the web URL's
scheme://host[:port]. Defaults: --alert-timeout 1500, --poll-seconds 10.

Environment (read only through ``Ports.env``; never printed or stored):
    F3_LAB_UI_USER, F3_LAB_UI_PASSWORD   Basic Auth for the workbench (run).
    F3_LAB_DSN                           read-only DSN for the real store.
    The lab Prometheus account comes from scripts.kind_lab (unchanged).

Stdout. ``plan`` prints one object::

    {"command": "plan", "dry_run": bool, "scenario_id": "F3:competing-cause",
     "deployment": {"namespace": "otel-demo", "deployment": "checkout",
                    "container": "checkout", "change": str},
     "fault": {"flag": "paymentFailure", "normal_variant": "off",
               "variant": "100%", "offset_seconds": 420,
               "duration_seconds": 1800, "propagation_seconds": 180,
               "flag_method": str | null},
     "timing": {"rollout_budget_seconds": 120,
                "alert_latency_min_seconds": 120,
                "alert_latency_max_seconds": 600,
                "alert_lookback_seconds": 3600,
                "rollout_to_onset_min_seconds": int,
                "rollout_to_onset_ok": bool,          # >= 300
                "release_to_alert_max_seconds": int,
                "release_in_alert_window": bool,      # <= 3600
                "schedule_within_harness_limit": bool},  # offset+duration <= 7200
     "alert": {"alertname": "CheckoutPlaceOrderErrorRatioHigh",
               "namespace": "otel-demo", "workload": "checkout"},
     "follow_up": {"template": str, "example": str, "length": int,
                   "problems": [str], "expected_generation": 0,
                   "fields": ["action", "expected_generation",
                              "idempotency_key", "text"]},
     "frozen": {"rubric_sha256": hex, "stimulus_sha256": hex,
                "answer_sha256": hex, "answer_consistent": bool},
     "preconditions": [PRECONDITION_IDS...],
     "actions": [ACTIONS...]}

``preflight`` prints one object::

    {"command": "preflight", "dry_run": bool, "ok": bool | null,
     "reason": str | null,               # first failing check's reason
     "checks": [{"id": str, "status": "pass"|"fail"|"unverified"|"skipped",
                 "reason": str | null, "detail": str | null}, ...]}

``checks`` always lists all seven ids in PRECONDITION_IDS order; ``ok`` is
null under --dry-run. ``run`` prints JSON Lines, each object with
``"event"``: ``plan`` (the plan object), ``preflight`` (the preflight
object), ``action`` ({"name", "skipped": true}; dry-run only), ``released``
({"release_id", "released_at"}), ``incident`` ({"incident_id", "run_id"}),
``follow_up`` ({"status", "generation", "request_sha256"}),
``start_worker`` ({"incident_id"}: start the worker now), and last
``finished`` ({"ok": bool, "reason": str | null, "dry_run": bool, "ledger":
str | null, ...}). Every event is one flat JSON object: the fields listed
for an event are top-level keys next to ``"event"``, never nested under the
event name (e.g. ``{"event": "action", "name": "publish", "skipped": true}``;
the ``plan`` and ``preflight`` events carry the plan/preflight object's keys
at top level). Credentials never appear on stdout or in the ledger.

Configuration order: checks run in this fixed order and the first failure
is the one reported: (1) argparse; (2) frozen files and plan (exit 4);
(3) ``run`` only, also under --dry-run: ``--run`` name
(CONFIG_RUN_NAME_INVALID), then ``--web`` (CONFIG_WEB_URL_INVALID);
(4) without --dry-run: --holder/--batch (CONFIG_HOLDER_BATCH_REQUIRED);
(5) building ports (CONFIG_DSN_MISSING); (6) ``run`` only: UI credentials
(CONFIG_UI_CREDENTIALS_MISSING); then the preflight. Command-line-only
checks therefore precede environment and lab checks.

Exit codes and reason codes:
    0  success (dry-run included).
    2  usage or configuration: argparse errors, CONFIG_HOLDER_BATCH_REQUIRED,
       CONFIG_RUN_NAME_INVALID, CONFIG_UI_CREDENTIALS_MISSING,
       CONFIG_WEB_URL_INVALID, CONFIG_DSN_MISSING (real store only).
       ``plan`` exits 4 after printing the plan when the plan is
       inconsistent; ``preflight`` refusals (2, 4) print
       {"command", "ok": false, "reason"}; ``run`` reports every refusal in
       its ``finished`` line.
    3  a precondition failed or could not be verified; ``reason`` is one of
       PRECONDITION_LAB_NOT_RESERVED (1), PRECONDITION_FAULT_ACTIVE (2),
       PRECONDITION_FLAGS_NOT_OFF (3), PRECONDITION_ALERT_FIRING (4),
       PRECONDITION_OPEN_ALERT_INCIDENT (5), PRECONDITION_RECENT_ROLLOUT (6),
       PRECONDITION_SWAP_LOW (7); a check whose port raised is
       ``unverified`` with PRECONDITION_UNVERIFIED (its ``id`` names it).
    4  frozen files: FROZEN_FILE_UNREADABLE, FROZEN_PLAN_MISMATCH,
       STIMULUS_LEAKS_ANSWER.
    5  run step: RUN_LEDGER_EXISTS, PUBLISH_FAILED, ALERT_INCIDENT_NOT_FOUND,
       ALERT_INCIDENT_AMBIGUOUS, WORKER_ALREADY_CLAIMED, GENERATION_MOVED,
       FOLLOW_UP_TEXT_REJECTED, FOLLOW_UP_REFUSED, FOLLOW_UP_UNREACHABLE,
       FAULT_FAILED.

Precondition ids (R-6, in order): lab_reserved, no_active_fault, flags_off,
alert_not_firing, no_open_alert_incident, no_recent_rollout, swap_headroom.
The preflight runs before anything is published, also as the first step of
``run``: no_open_alert_incident fails on ANY incident from
``IncidentStore.alert_incidents`` whose lifecycle is not ``closed`` (and
whose alertname matches or is None), whatever its ``created_at``. A store
fake for a successful ``run`` must therefore return no open incident on that
first call and the new incident (created_at >= released_at) only on later
polls, as the real alert would.
Actions (run order): preflight, reserve, publish, schedule_fault,
await_alert_incident, follow_up, start_worker, await_fault_restore.
HARNESS_CALLS names every scripts.f11_u2_harness function this module
calls; FORBIDDEN_HARNESS_CALLS = ("register", "webhook") are never called.

Injectable interfaces (``main(argv, *, ports=None, stdout=None,
ports_factory=None) -> int``; when ``ports`` is None and the command needs
them, ``ports_factory(os.environ)`` builds them, default ``real_ports``)::

    @dataclass(frozen=True)
    class SwapUsage: total_mib: float; used_mib: float

    @dataclass(frozen=True)
    class AlertIncident:
        incident_id: str; run_id: str | None; lifecycle: str
        control_generation: int; run_state: str | None; step_count: int
        alertname: str | None   # None: delivery unreadable (counts as match)
        created_at: datetime    # aware UTC

    class LabPort(Protocol):
        def ledger_root(self) -> Path
        def check_reservation(self, holder: str, batch: str) -> None  # ValueError(code)
        def fault_active(self) -> bool
        def flag_defaults(self) -> Mapping[str, str]   # flag -> defaultVariant
        def firing_alert_count(self, alertname: str) -> int
        def recent_rollouts(self, since: datetime) -> Sequence[str]
        def swap_usage(self) -> SwapUsage
        def reserved(self, holder: str, batch: str) -> ContextManager[None]
        def publish(self, ledger: Path, release_id: str) -> Mapping[str, Any]
            # returns release.json content; must contain "released_at"
        def scheduled_fault(self, ledger: Path, *, offset: int, duration: int,
                            flag: str, variant: str, normal: str, url: str,
                            method: str) -> None   # blocks until restored
            # called as scheduled_fault(ledger, offset=..., ...): ledger is
            # positional, the rest keywords. It runs in a background thread;
            # any exception it raises ends the run with FAULT_FAILED, and if
            # that happens before the incident is found no follow-up is sent.

    class IncidentStore(Protocol):
        def alert_incidents(self, alertname: str, namespace: str,
                            workload: str) -> Sequence[AlertIncident]
        def release_observations_since(self, since: datetime) -> int

    class HttpPort(Protocol):
        def post_form(self, url: str, body: bytes, headers: Mapping[str, str],
                      timeout: float) -> tuple[int, bytes]
            # (status, body); unreachable -> ValueError

    class Clock(Protocol):
        def now(self) -> datetime   # aware UTC
        def sleep(self, seconds: float) -> None

    @dataclass
    class Ports: lab: LabPort; store: IncidentStore; http: HttpPort;
                 clock: Clock; env: Mapping[str, str]

The follow-up request is ``POST {web}/incidents/{incident_id}/control`` with
``application/x-www-form-urlencoded`` fields action=follow_up,
expected_generation=0, idempotency_key=note-<release_id>, text=<rendered
template>, and headers Authorization (Basic), Origin, Accept
application/json. Its body never contains a string from answer.json's
``injected`` (service, aliases, operation, aliases, flag, variant) nor the
words payment, charge, f3, unrelated, decoy (case-insensitive); the driver
refuses to send otherwise (FOLLOW_UP_TEXT_REJECTED). The ledger stores the
request hash, the form fields with the key renamed ``idempotency_label``,
and the response; never the Authorization header.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol, TextIO
from urllib.parse import urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opspilot.intake import _reject_ambiguous_text  # noqa: E402
from opspilot.investigation.context import INPUT_CONTENT_FIELD_MAX_CHARS  # noqa: E402
from opspilot.redaction import redact_credentials  # noqa: E402
from scripts import f11_u2_harness as harness  # noqa: E402
from scripts import kind_lab  # noqa: E402

SCENARIO_ID = "F3:competing-cause"
RUBRIC = ROOT / "docs/testing/f3-competing-cause-rubric.md"
STIMULUS = ROOT / "evals/f3/competing-cause/stimulus.json"
ANSWER = ROOT / "evals/f3/competing-cause/answer.json"

NAMESPACE = "otel-demo"
DEPLOYMENT = "checkout"
CONTAINER = "checkout"
ALERTNAME = "CheckoutPlaceOrderErrorRatioHigh"
FLAG = kind_lab.FLAG
NORMAL_VARIANT = kind_lab.NORMAL_VARIANT
FAULT_VARIANT = kind_lab.FAULT_VARIANT
OFFSET_SECONDS = 420  # R-1
DURATION_SECONDS = 1800  # R-1
PROPAGATION_SECONDS = 180  # harness scheduled_fault default
ROLLOUT_BUDGET_SECONDS = 120  # R-1: about 1-2 minutes of rollout
ALERT_LATENCY_MIN_SECONDS = 120  # rule ``for: 2m``
ALERT_LATENCY_MAX_SECONDS = 600  # rate 5m + for 2m + scrape/eval/group_wait
ALERT_LOOKBACK_SECONDS = 3600  # alert Run window starts 1 h before startsAt
HARNESS_SCHEDULE_LIMIT = 7200
MIN_ROLLOUT_TO_ONSET = 300
RECENT_ROLLOUT_SECONDS = 4200  # R-6 item 6: 70 minutes
DEFAULT_SWAP_MIN_FREE_MIB = 2048
DEFAULT_ALERT_TIMEOUT = 1500
DEFAULT_POLL_SECONDS = 10
EXPECTED_GENERATION = 0  # R-8: before the worker's first claim
HTTP_TIMEOUT = 15.0
LEDGER_DIR = "f3-competing-cause"

FLAG_METHODS = ("Boolean", "String", "Int", "Float", "Object")
FIXED_FORBIDDEN_WORDS = ("payment", "charge", "f3", "unrelated", "decoy")
FORM_FIELDS = ("action", "expected_generation", "idempotency_key", "text")
PRECONDITION_IDS = (
    "lab_reserved",
    "no_active_fault",
    "flags_off",
    "alert_not_firing",
    "no_open_alert_incident",
    "no_recent_rollout",
    "swap_headroom",
)
PRECONDITION_REASONS = {
    "lab_reserved": "PRECONDITION_LAB_NOT_RESERVED",
    "no_active_fault": "PRECONDITION_FAULT_ACTIVE",
    "flags_off": "PRECONDITION_FLAGS_NOT_OFF",
    "alert_not_firing": "PRECONDITION_ALERT_FIRING",
    "no_open_alert_incident": "PRECONDITION_OPEN_ALERT_INCIDENT",
    "no_recent_rollout": "PRECONDITION_RECENT_ROLLOUT",
    "swap_headroom": "PRECONDITION_SWAP_LOW",
}
ACTIONS = (
    "preflight",
    "reserve",
    "publish",
    "schedule_fault",
    "await_alert_incident",
    "follow_up",
    "start_worker",
    "await_fault_restore",
)
HARNESS_CALLS = (
    "lab_ledgers",
    "occupation",
    "reserved",
    "flags",
    "snapshot",
    "read_json",
    "write_json",
    "publish",
    "scheduled_fault",
)
FORBIDDEN_HARNESS_CALLS = ("register", "webhook")

EXIT_OK, EXIT_UNEXPECTED, EXIT_CONFIG = 0, 1, 2
EXIT_PRECONDITION, EXIT_FROZEN, EXIT_RUN = 3, 4, 5

_RUN_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_LOOPBACK_NAMES = {"localhost"}


class DriverError(Exception):
    """A fixed reason code plus the exit code it maps to; safe to print."""

    def __init__(self, code: str, exit_code: int, **detail: Any) -> None:
        super().__init__(code)
        self.code = code
        self.exit_code = exit_code
        self.detail = detail


# -- injectable interfaces ------------------------------------------------


@dataclass(frozen=True)
class SwapUsage:
    total_mib: float
    used_mib: float


@dataclass(frozen=True)
class AlertIncident:
    incident_id: str
    run_id: str | None
    lifecycle: str
    control_generation: int
    run_state: str | None
    step_count: int
    alertname: str | None
    created_at: datetime


class LabPort(Protocol):
    def ledger_root(self) -> Path: ...
    def check_reservation(self, holder: str, batch: str) -> None: ...
    def fault_active(self) -> bool: ...
    def flag_defaults(self) -> Mapping[str, str]: ...
    def firing_alert_count(self, alertname: str) -> int: ...
    def recent_rollouts(self, since: datetime) -> Sequence[str]: ...
    def swap_usage(self) -> SwapUsage: ...
    def reserved(self, holder: str, batch: str) -> AbstractContextManager[None]: ...
    def publish(self, ledger: Path, release_id: str) -> Mapping[str, Any]: ...
    def scheduled_fault(
        self,
        ledger: Path,
        *,
        offset: int,
        duration: int,
        flag: str,
        variant: str,
        normal: str,
        url: str,
        method: str,
    ) -> None: ...


class IncidentStore(Protocol):
    def alert_incidents(
        self, alertname: str, namespace: str, workload: str
    ) -> Sequence[AlertIncident]: ...
    def release_observations_since(self, since: datetime) -> int: ...


class HttpPort(Protocol):
    def post_form(
        self, url: str, body: bytes, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
    def sleep(self, seconds: float) -> None: ...


@dataclass
class Ports:
    lab: LabPort
    store: IncidentStore
    http: HttpPort
    clock: Clock
    env: Mapping[str, str] = field(default_factory=dict)


# -- frozen files and plan (pure) ----------------------------------------


@dataclass(frozen=True)
class Frozen:
    stimulus: Mapping[str, Any]
    answer: Mapping[str, Any]
    rubric_sha256: str
    stimulus_sha256: str
    answer_sha256: str


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_frozen(
    rubric: Path = RUBRIC, stimulus: Path = STIMULUS, answer: Path = ANSWER
) -> Frozen:
    try:
        return Frozen(
            stimulus=json.loads(stimulus.read_bytes()),
            answer=json.loads(answer.read_bytes()),
            rubric_sha256=sha256_file(rubric),
            stimulus_sha256=sha256_file(stimulus),
            answer_sha256=sha256_file(answer),
        )
    except (OSError, ValueError):
        raise DriverError("FROZEN_FILE_UNREADABLE", EXIT_FROZEN) from None


def answer_consistent(answer: Mapping[str, Any]) -> bool:
    """The frozen answer describes exactly the scenario this driver runs."""
    try:
        injected, deployment = answer["injected"], answer["deployment"]
        return (
            answer["scenario_id"] == SCENARIO_ID
            and injected["flag"] == FLAG
            and injected["variant"] == FAULT_VARIANT
            and injected["normal_variant"] == NORMAL_VARIANT
            and deployment["namespace"] == NAMESPACE
            and deployment["deployment"] == DEPLOYMENT
            and deployment["fault_offset_seconds"] == OFFSET_SECONDS
            and deployment["fault_duration_seconds"] == DURATION_SECONDS
            and tuple(item["id"] for item in answer["preconditions"])
            == PRECONDITION_IDS
        )
    except (KeyError, TypeError):
        return False


def forbidden_words(answer: Mapping[str, Any]) -> tuple[str, ...]:
    injected = answer.get("injected", {})
    words: list[str] = list(FIXED_FORBIDDEN_WORDS)
    for key in ("service", "operation", "flag", "variant"):
        if isinstance(injected.get(key), str):
            words.append(injected[key])
    for key in ("service_aliases", "operation_aliases"):
        words.extend(str(item) for item in injected.get(key, ()))
    return tuple(dict.fromkeys(word.lower() for word in words if word))


def follow_up_problems(text: str, answer: Mapping[str, Any]) -> tuple[str, ...]:
    """Why ``text`` may not go to the workbench; empty when it may."""
    problems = []
    if not text.strip() or len(text) > INPUT_CONTENT_FIELD_MAX_CHARS:
        problems.append("length")
    try:
        _reject_ambiguous_text(text)
    except ValueError:
        problems.append("control_character")
    if redact_credentials(text) != text:
        problems.append("redaction_changes_text")
    lowered = text.lower()
    problems.extend(
        f"leaks:{word}" for word in forbidden_words(answer) if word in lowered
    )
    return tuple(problems)


def utc_seconds(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def release_id_for(moment: datetime) -> str:
    """Neutral (D4-a): a fixed prefix and digits only, so no answer word."""
    return "rel-" + moment.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S")


def render_follow_up(template: str, released_at: str, release_id: str) -> str:
    return template.format(released_at=released_at, release_id=release_id)


def idempotency_value(release_id: str) -> str:
    return f"note-{release_id}"


def follow_up_body(text: str, release_id: str) -> bytes:
    fields = {
        "action": "follow_up",
        "expected_generation": str(EXPECTED_GENERATION),
        "idempotency_key": idempotency_value(release_id),
        "text": text,
    }
    return urlencode(fields).encode("utf-8")


def build_plan(frozen: Frozen, method: str | None, dry_run: bool) -> dict[str, Any]:
    template = str(frozen.stimulus.get("follow_up_template", ""))
    example_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    example = render_follow_up(
        template, utc_seconds(example_at), release_id_for(example_at)
    )
    onset_gap = OFFSET_SECONDS - ROLLOUT_BUDGET_SECONDS
    alert_max = OFFSET_SECONDS + PROPAGATION_SECONDS + ALERT_LATENCY_MAX_SECONDS
    return {
        "command": "plan",
        "dry_run": dry_run,
        "scenario_id": SCENARIO_ID,
        "deployment": {
            "namespace": NAMESPACE,
            "deployment": DEPLOYMENT,
            "container": CONTAINER,
            "change": "env OPSPILOT_U2_RELEASE_ID=<release_id> (harness publish)",
        },
        "fault": {
            "flag": FLAG,
            "normal_variant": NORMAL_VARIANT,
            "variant": FAULT_VARIANT,
            "offset_seconds": OFFSET_SECONDS,
            "duration_seconds": DURATION_SECONDS,
            "propagation_seconds": PROPAGATION_SECONDS,
            "flag_method": method,
        },
        "timing": {
            "rollout_budget_seconds": ROLLOUT_BUDGET_SECONDS,
            "alert_latency_min_seconds": ALERT_LATENCY_MIN_SECONDS,
            "alert_latency_max_seconds": ALERT_LATENCY_MAX_SECONDS,
            "alert_lookback_seconds": ALERT_LOOKBACK_SECONDS,
            "rollout_to_onset_min_seconds": onset_gap,
            "rollout_to_onset_ok": onset_gap >= MIN_ROLLOUT_TO_ONSET,
            "release_to_alert_max_seconds": alert_max,
            "release_in_alert_window": alert_max <= ALERT_LOOKBACK_SECONDS,
            "schedule_within_harness_limit": OFFSET_SECONDS + DURATION_SECONDS
            <= HARNESS_SCHEDULE_LIMIT,
        },
        "alert": {
            "alertname": ALERTNAME,
            "namespace": NAMESPACE,
            "workload": DEPLOYMENT,
        },
        "follow_up": {
            "template": template,
            "example": example,
            "length": len(example),
            "problems": list(follow_up_problems(example, frozen.answer)),
            "expected_generation": EXPECTED_GENERATION,
            "fields": list(FORM_FIELDS),
        },
        "frozen": {
            "rubric_sha256": frozen.rubric_sha256,
            "stimulus_sha256": frozen.stimulus_sha256,
            "answer_sha256": frozen.answer_sha256,
            "answer_consistent": answer_consistent(frozen.answer),
        },
        "preconditions": list(PRECONDITION_IDS),
        "actions": list(ACTIONS),
    }


def plan_problem(plan: Mapping[str, Any]) -> str | None:
    if not plan["frozen"]["answer_consistent"] or not all(
        plan["timing"][key]
        for key in (
            "rollout_to_onset_ok",
            "release_in_alert_window",
            "schedule_within_harness_limit",
        )
    ):
        return "FROZEN_PLAN_MISMATCH"
    if plan["follow_up"]["problems"] or "{" in plan["follow_up"]["example"]:
        return "STIMULUS_LEAKS_ANSWER"
    return None


# -- preconditions (R-6) --------------------------------------------------


def _check(check_id: str, evaluate: Callable[[], str | None]) -> dict[str, Any]:
    """``evaluate`` returns None (pass) or a failure detail."""
    try:
        detail = evaluate()
    except Exception as exc:  # any port failure is "could not verify"
        code = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        return dict(
            id=check_id,
            status="unverified",
            reason="PRECONDITION_UNVERIFIED",
            detail=code[:120],
        )
    if detail is None:
        return dict(id=check_id, status="pass", reason=None, detail=None)
    return dict(
        id=check_id,
        status="fail",
        reason=PRECONDITION_REASONS[check_id],
        detail=detail[:400],
    )


def preflight_checks(
    ports: Ports, holder: str, batch: str, swap_min_free_mib: float
) -> list[dict[str, Any]]:
    lab, store = ports.lab, ports.store
    since = ports.clock.now() - timedelta(seconds=RECENT_ROLLOUT_SECONDS)

    def reservation() -> str | None:
        try:
            lab.check_reservation(holder, batch)
        except ValueError as exc:
            return str(exc)
        return None

    def fault() -> str | None:
        return "fault-active.json present" if lab.fault_active() else None

    def flags() -> str | None:
        moved = sorted(
            name for name, variant in lab.flag_defaults().items() if variant != "off"
        )
        return ",".join(moved) if moved else None

    def alert() -> str | None:
        count = lab.firing_alert_count(ALERTNAME)
        return f"{count} ALERTS series" if count else None

    def incidents() -> str | None:
        open_ = [
            item.incident_id
            for item in store.alert_incidents(ALERTNAME, NAMESPACE, DEPLOYMENT)
            if item.lifecycle != "closed"
            and (item.alertname is None or item.alertname == ALERTNAME)
        ]
        return ",".join(open_) if open_ else None

    def rollouts() -> str | None:
        found = list(lab.recent_rollouts(since))
        observations = store.release_observations_since(since)
        if observations:
            found.append(f"{observations} release observation(s)")
        return "; ".join(found) if found else None

    def swap() -> str | None:
        usage = lab.swap_usage()
        free = usage.total_mib - usage.used_mib
        if usage.total_mib > 0 and free < swap_min_free_mib:
            return f"free {free:.0f} MiB < {swap_min_free_mib:.0f} MiB"
        return None

    evaluators: dict[str, Callable[[], str | None]] = {
        "lab_reserved": reservation,
        "no_active_fault": fault,
        "flags_off": flags,
        "alert_not_firing": alert,
        "no_open_alert_incident": incidents,
        "no_recent_rollout": rollouts,
        "swap_headroom": swap,
    }
    return [_check(check_id, evaluators[check_id]) for check_id in PRECONDITION_IDS]


def preflight_report(
    checks: list[dict[str, Any]] | None, dry_run: bool
) -> dict[str, Any]:
    if checks is None:
        checks = [
            dict(id=check_id, status="skipped", reason=None, detail=None)
            for check_id in PRECONDITION_IDS
        ]
        return dict(
            command="preflight", dry_run=dry_run, ok=None, reason=None, checks=checks
        )
    failed = [item for item in checks if item["status"] != "pass"]
    return dict(
        command="preflight",
        dry_run=dry_run,
        ok=not failed,
        reason=failed[0]["reason"] if failed else None,
        checks=checks,
    )


# -- run ------------------------------------------------------------------


def _emit(stdout: TextIO, value: Mapping[str, Any]) -> None:
    stdout.write(json.dumps(value, sort_keys=True, allow_nan=False) + "\n")
    stdout.flush()


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("UTC_OFFSET_REQUIRED")
    return parsed.astimezone(timezone.utc)


def web_origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def check_web_url(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host in _LOOPBACK_NAMES
    if (
        parts.scheme not in ("http", "https")
        or not loopback
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
    ):
        raise DriverError("CONFIG_WEB_URL_INVALID", EXIT_CONFIG)
    return url.rstrip("/")


def basic_authorization(env: Mapping[str, str]) -> str:
    user, password = env.get("F3_LAB_UI_USER"), env.get("F3_LAB_UI_PASSWORD")
    if not user or not password:
        raise DriverError("CONFIG_UI_CREDENTIALS_MISSING", EXIT_CONFIG)
    raw = f"{user}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _await_incident(
    ports: Ports,
    released_at: datetime,
    timeout: float,
    poll: float,
    fault_error: list[BaseException],
) -> AlertIncident:
    clock = ports.clock
    deadline = clock.now() + timedelta(seconds=timeout)
    while True:
        if fault_error:
            raise DriverError("FAULT_FAILED", EXIT_RUN)
        found = [
            item
            for item in ports.store.alert_incidents(ALERTNAME, NAMESPACE, DEPLOYMENT)
            if item.lifecycle != "closed"
            and item.created_at >= released_at
            and (item.alertname is None or item.alertname == ALERTNAME)
        ]
        if len(found) > 1:
            raise DriverError(
                "ALERT_INCIDENT_AMBIGUOUS",
                EXIT_RUN,
                incident_ids=sorted(item.incident_id for item in found),
            )
        if found:
            return found[0]
        if clock.now() >= deadline:
            raise DriverError("ALERT_INCIDENT_NOT_FOUND", EXIT_RUN)
        clock.sleep(poll)


def _send_follow_up(
    ports: Ports,
    web: str,
    origin: str,
    incident: AlertIncident,
    text: str,
    release_id: str,
    answer: Mapping[str, Any],
    ledger: Path,
) -> dict[str, Any]:
    if incident.control_generation != EXPECTED_GENERATION:
        raise DriverError(
            "GENERATION_MOVED", EXIT_RUN, generation=incident.control_generation
        )
    if incident.run_state != "queued" or incident.step_count:
        raise DriverError(
            "WORKER_ALREADY_CLAIMED",
            EXIT_RUN,
            run_state=incident.run_state,
            step_count=incident.step_count,
        )
    body = follow_up_body(text, release_id)
    if follow_up_problems(body.decode("utf-8"), answer) or follow_up_problems(
        text, answer
    ):
        raise DriverError("FOLLOW_UP_TEXT_REJECTED", EXIT_RUN)
    headers = {
        "Authorization": basic_authorization(ports.env),
        "Origin": origin,
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    path = f"/incidents/{incident.incident_id}/control"
    request_sha256 = hashlib.sha256(body).hexdigest()
    try:
        status, raw = ports.http.post_form(web + path, body, headers, HTTP_TIMEOUT)
    except ValueError:
        raise DriverError("FOLLOW_UP_UNREACHABLE", EXIT_RUN) from None
    try:
        response = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, ValueError):
        response = None
    record = {
        "path": path,
        "fields": {
            "action": "follow_up",
            "expected_generation": EXPECTED_GENERATION,
            # Named *_label so evidence copies never look like a secret.
            "idempotency_label": idempotency_value(release_id),
            "text": text,
        },
        "request_sha256": request_sha256,
        "status": status,
        "response": response,
    }
    harness.write_json(ledger / "follow-up.json", record, exclusive=True)
    if status != 200 or not isinstance(response, dict):
        code = response.get("code") if isinstance(response, dict) else None
        raise DriverError(
            "FOLLOW_UP_REFUSED", EXIT_RUN, http_status=status, refusal=code
        )
    return {
        "status": status,
        "generation": response.get("generation"),
        "request_sha256": request_sha256,
    }


def run_live(
    ports: Ports,
    args: argparse.Namespace,
    frozen: Frozen,
    plan: Mapping[str, Any],
    stdout: TextIO,
) -> dict[str, Any]:
    if not _RUN_NAME.fullmatch(args.run):
        raise DriverError("CONFIG_RUN_NAME_INVALID", EXIT_CONFIG)
    web = check_web_url(args.web)
    origin = args.origin or web_origin(web)
    basic_authorization(ports.env)  # fail before any lab call
    report = preflight_report(
        preflight_checks(ports, args.holder, args.batch, args.swap_min_free_mib),
        dry_run=False,
    )
    _emit(stdout, {"event": "preflight", **report})
    if not report["ok"]:
        raise DriverError(report["reason"], EXIT_PRECONDITION)
    ledger = ports.lab.ledger_root() / args.run
    if ledger.exists():
        raise DriverError("RUN_LEDGER_EXISTS", EXIT_RUN)
    harness.write_json(ledger / "plan.json", dict(plan), exclusive=True)
    release_id = release_id_for(ports.clock.now())
    result: dict[str, Any] = {"ledger": str(ledger), "release_id": release_id}
    fault_error: list[BaseException] = []
    with ports.lab.reserved(args.holder, args.batch):
        try:
            release = ports.lab.publish(ledger, release_id)
            released_at = _parse_utc(str(release["released_at"]))
        except DriverError:
            raise
        except Exception:
            raise DriverError("PUBLISH_FAILED", EXIT_RUN, **result) from None
        result["released_at"] = utc_seconds(released_at)
        _emit(stdout, {"event": "released", **result})

        def fault() -> None:
            try:
                ports.lab.scheduled_fault(
                    ledger,
                    offset=OFFSET_SECONDS,
                    duration=DURATION_SECONDS,
                    flag=FLAG,
                    variant=FAULT_VARIANT,
                    normal=NORMAL_VARIANT,
                    url=args.flagd_url,
                    method=args.flag_method,
                )
            except BaseException as exc:  # reported after join
                fault_error.append(exc)

        thread = threading.Thread(target=fault, name="f3-fault", daemon=False)
        thread.start()
        failure: DriverError | None = None
        try:
            incident = _await_incident(
                ports, released_at, args.alert_timeout, args.poll_seconds, fault_error
            )
            result.update(incident_id=incident.incident_id, run_id=incident.run_id)
            _emit(
                stdout,
                {
                    "event": "incident",
                    "incident_id": incident.incident_id,
                    "run_id": incident.run_id,
                },
            )
            text = render_follow_up(
                str(frozen.stimulus["follow_up_template"]),
                result["released_at"],
                release_id,
            )
            sent = _send_follow_up(
                ports, web, origin, incident, text, release_id, frozen.answer, ledger
            )
            result["follow_up"] = sent
            _emit(stdout, {"event": "follow_up", **sent})
            _emit(
                stdout, {"event": "start_worker", "incident_id": incident.incident_id}
            )
        except DriverError as exc:
            failure = exc
        finally:
            # The harness restores the flag itself; never leave it behind.
            thread.join()
    if fault_error and failure is None:
        failure = DriverError("FAULT_FAILED", EXIT_RUN)
    if failure is not None:
        failure.detail.update(result)
        raise failure
    harness.write_json(ledger / "summary.json", result, exclusive=True)
    return result


# -- real ports (lab only; never built under --dry-run) --------------------


class RealLab:
    def ledger_root(self) -> Path:
        return harness.lab_ledgers() / LEDGER_DIR

    def check_reservation(self, holder: str, batch: str) -> None:
        harness.occupation("check", holder, batch)

    def fault_active(self) -> bool:
        return (harness.lab_ledgers() / "fault-active.json").exists()

    def flag_defaults(self) -> Mapping[str, str]:
        _, raw = harness.flags()
        data = json.loads(raw)
        return {
            name: str(item["defaultVariant"]) for name, item in data["flags"].items()
        }

    def firing_alert_count(self, alertname: str) -> int:
        status, results = kind_lab.prom_instant(f'ALERTS{{alertname="{alertname}"}}')
        if status != 200:
            raise ValueError("PROMETHEUS_UNAVAILABLE")
        return len(results)

    def recent_rollouts(self, since: datetime) -> Sequence[str]:
        found = []
        snap = harness.snapshot()
        for rs in snap["replicasets"]:
            created = _parse_utc(rs["metadata"]["creationTimestamp"])
            if created >= since:
                found.append(
                    f"replicaset {rs['metadata']['name']} created {created.isoformat()}"
                )
        for condition in snap["deployment"].get("status", {}).get("conditions", []):
            if (
                condition.get("type") == "Progressing"
                and condition.get("reason") == "ReplicaSetUpdated"
            ):
                found.append("checkout rollout in progress")
        root = harness.lab_ledgers()
        for path in [
            *root.glob("*/release.json"),
            *root.glob(f"{LEDGER_DIR}/*/release.json"),
        ]:
            released = _parse_utc(harness.read_json(path)["released_at"])
            if released >= since:
                found.append(f"ledger release {path.parent.name}")
        return found

    def swap_usage(self) -> SwapUsage:
        output = subprocess.run(
            ["sysctl", "-n", "vm.swapusage"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
        values = dict(re.findall(r"(total|used) = ([0-9.]+)M", output))
        if set(values) != {"total", "used"}:
            raise ValueError("SWAP_USAGE_UNPARSED")
        return SwapUsage(float(values["total"]), float(values["used"]))

    def reserved(self, holder: str, batch: str) -> AbstractContextManager[None]:
        return harness.reserved(holder, batch)

    def publish(self, ledger: Path, release_id: str) -> Mapping[str, Any]:
        return harness.publish(ledger, release_id)

    def scheduled_fault(
        self,
        ledger: Path,
        *,
        offset: int,
        duration: int,
        flag: str,
        variant: str,
        normal: str,
        url: str,
        method: str,
    ) -> None:
        harness.scheduled_fault(
            ledger,
            offset=offset,
            duration=duration,
            flag=flag,
            variant=variant,
            normal=normal,
            url=url,
            method=method,
            propagation_seconds=PROPAGATION_SECONDS,
        )


_INCIDENT_SQL = """
SELECT i.incident_id::text AS incident_id, a.run_id::text AS run_id,
       i.lifecycle, i.control_generation, r.state AS run_state,
       (SELECT count(*) FROM opspilot_steps s WHERE s.run_id = a.run_id) AS step_count,
       (SELECT d.alert_json FROM opspilot_alert_deliveries d
         WHERE d.delivery_key = a.delivery_key ORDER BY d.delivery_id LIMIT 1) AS alert_json,
       a.created_at
FROM opspilot_alert_identities a
JOIN opspilot_incidents i ON i.incident_id = a.incident_id
LEFT JOIN opspilot_runs r ON r.run_id = a.run_id
WHERE a.namespace = %s AND a.workload = %s
"""


def _alertname(alert_json: str | None) -> str | None:
    try:
        value = json.loads(alert_json or "")["labels"]["alertname"]
    except (ValueError, KeyError, TypeError):
        return None
    return value if isinstance(value, str) else None


class RealStore:
    """SELECT-only, inside a READ ONLY transaction."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def _rows(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(
            self._dsn, connect_timeout=5, row_factory=dict_row
        ) as conn:
            conn.read_only = True
            with conn.transaction():
                return list(conn.execute(sql, params).fetchall())

    def alert_incidents(
        self, alertname: str, namespace: str, workload: str
    ) -> Sequence[AlertIncident]:
        rows = self._rows(_INCIDENT_SQL, (namespace, workload))
        result = []
        for row in rows:
            name = _alertname(row["alert_json"])
            if name is not None and name != alertname:
                continue
            result.append(
                AlertIncident(
                    incident_id=row["incident_id"],
                    run_id=row["run_id"],
                    lifecycle=row["lifecycle"],
                    control_generation=int(row["control_generation"]),
                    run_state=row["run_state"],
                    step_count=int(row["step_count"]),
                    alertname=name,
                    created_at=row["created_at"].astimezone(timezone.utc),
                )
            )
        return result

    def release_observations_since(self, since: datetime) -> int:
        rows = self._rows(
            "SELECT count(*) AS n FROM opspilot_release_observations "
            "WHERE released_at >= %s OR deadline_at > clock_timestamp()",
            (since,),
        )
        return int(rows[0]["n"])


class RealHttp:
    def post_form(
        self, url: str, body: bytes, headers: Mapping[str, str], timeout: float
    ) -> tuple[int, bytes]:
        request = urllib.request.Request(
            url, data=body, headers=dict(headers), method="POST"
        )
        try:
            with kind_lab.OPENER.open(request, timeout=timeout) as response:
                return int(response.status), response.read(64 * 1024)
        except urllib.error.HTTPError as error:
            return int(error.code), error.read(64 * 1024)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ValueError("FOLLOW_UP_UNREACHABLE") from None


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def real_ports(env: Mapping[str, str]) -> Ports:
    dsn = env.get("F3_LAB_DSN")
    if not dsn:
        raise DriverError("CONFIG_DSN_MISSING", EXIT_CONFIG)
    return Ports(
        lab=RealLab(),
        store=RealStore(dsn),
        http=RealHttp(),
        clock=SystemClock(),
        env=env,
    )


# -- command line ---------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python -m scripts.f3_competing_scenario")
    root.add_argument("--dry-run", action="store_true")
    root.add_argument("--holder", default="")
    root.add_argument("--batch", default="")
    root.add_argument(
        "--swap-min-free-mib", type=float, default=DEFAULT_SWAP_MIN_FREE_MIB
    )
    commands = root.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--flag-method", choices=FLAG_METHODS)
    commands.add_parser("preflight")
    run = commands.add_parser("run")
    run.add_argument("--run", required=True)
    run.add_argument("--web", required=True)
    run.add_argument("--origin")
    run.add_argument("--flagd-url", required=True)
    run.add_argument("--flag-method", required=True, choices=FLAG_METHODS)
    run.add_argument("--alert-timeout", type=float, default=DEFAULT_ALERT_TIMEOUT)
    run.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    return root


def main(
    argv: Sequence[str] | None = None,
    *,
    ports: Ports | None = None,
    stdout: TextIO | None = None,
    ports_factory: Callable[[Mapping[str, str]], Ports] | None = None,
) -> int:
    out = sys.stdout if stdout is None else stdout
    args = parser().parse_args(argv)
    final_event = args.command == "run"
    try:
        frozen = load_frozen()
        plan = build_plan(frozen, getattr(args, "flag_method", None), args.dry_run)
        if args.command == "plan":
            _emit(out, plan)
            problem = plan_problem(plan)
            return EXIT_FROZEN if problem else EXIT_OK
        problem = plan_problem(plan)
        if final_event:
            _emit(out, {"event": "plan", **plan})
        if problem:
            raise DriverError(problem, EXIT_FROZEN)
        if final_event:
            # Command-line-only checks first, dry-run included (fixed order).
            if not _RUN_NAME.fullmatch(args.run):
                raise DriverError("CONFIG_RUN_NAME_INVALID", EXIT_CONFIG)
            check_web_url(args.web)
        if args.dry_run:
            report = preflight_report(None, dry_run=True)
            if not final_event:
                _emit(out, report)
                return EXIT_OK
            _emit(out, {"event": "preflight", **report})
            for name in ACTIONS:
                _emit(out, {"event": "action", "name": name, "skipped": True})
            _emit(
                out,
                {
                    "event": "finished",
                    "ok": True,
                    "reason": None,
                    "dry_run": True,
                    "ledger": None,
                },
            )
            return EXIT_OK
        if not args.holder.strip() or not args.batch.strip():
            raise DriverError("CONFIG_HOLDER_BATCH_REQUIRED", EXIT_CONFIG)
        if ports is None:
            ports = (ports_factory or real_ports)(os.environ)
        if not final_event:
            report = preflight_report(
                preflight_checks(
                    ports, args.holder, args.batch, args.swap_min_free_mib
                ),
                dry_run=False,
            )
            _emit(out, report)
            return EXIT_OK if report["ok"] else EXIT_PRECONDITION
        result = run_live(ports, args, frozen, plan, out)
        _emit(
            out,
            {
                "event": "finished",
                "ok": True,
                "reason": None,
                "dry_run": False,
                **result,
            },
        )
        return EXIT_OK
    except DriverError as exc:
        if final_event:
            detail = {
                key: value for key, value in exc.detail.items() if key != "ledger"
            }
            _emit(
                out,
                {
                    "event": "finished",
                    "ok": False,
                    "reason": exc.code,
                    "dry_run": args.dry_run,
                    "ledger": exc.detail.get("ledger"),
                    **detail,
                },
            )
        else:
            _emit(out, {"command": args.command, "ok": False, "reason": exc.code})
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
