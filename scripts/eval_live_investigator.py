"""The real Investigator of the live scenario driver (eval PR-C, A5/A6/A10).

Live only: it reads the DeepSeek/LangSmith credentials, calls the model and
reads the lab. Offline tests use a fake ``Investigator`` instead; nothing in
``scripts/eval_live.py`` depends on this module except ``real_deps``.

One ``investigate`` call is one product Run on the attempt's own throwaway
PostgreSQL: intake through the web role, then ``InvestigationRunner`` under
the worker role with the ``otel-demo`` profile. The runner's
``attempt_wrapper`` (A5) receives the executor built for the real lease and
input, freezes ``ToolSnapshot`` from it and the public registrations, binds
the recording context, and only then enters ``record()``; inside it the B2
hooks replace the lazy model, executor, HTTP and delivery delegates and the
rest of the attempt runs through ``proceed()``. No epoch is predicted.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from opspilot import tracing
from opspilot.acceptance import IncidentOutcome, outcome_from_durable
from opspilot.evaluation import (
    ReportCapture,
    ToolBinding,
    ToolSnapshot,
    TraceEntry,
    TraceRecord,
    bind_tool_snapshots,
    investigator_input,
    record,
)
from opspilot.evaluation.codec import canonical_bytes
from opspilot.evaluation.recording import RecordingContext
from opspilot.evaluation.storage import ArtifactRef
from opspilot.investigation.client import DeepSeekClient
from opspilot.investigation.limits import M1_FROZEN_LIMITS, RUN_WALL_SECONDS
from opspilot.investigation.runner import InvestigationRunner, RunnerOutcome
from opspilot.persistence import DurableStore
from opspilot.tools import otel_demo
from opspilot.tools.registry import canonical_hash
from opspilot.web import DurableEventLog, DurableEvidenceStore, DurableIncidentStore
from opspilot.web.service import LEASE_SECONDS
from opspilot.worker import Worker
from scripts.eval_live_lab import (
    LabError,
    LiveClock,
    checkpoint,
    protected,
    write_private,
)
from scripts.lab_evidence import (
    configure_lab_round,
    langsmith_client,
    prepare_lab_project,
    trace_evidence,
)

RUN_WALL = timedelta(seconds=RUN_WALL_SECONDS)


@dataclass(frozen=True)
class RoundSession:
    key: str
    project: Mapping[str, Any] | None
    config: otel_demo.OtelDemoConfig


def prepare_round(round_id: str) -> RoundSession:
    """Credentials and the round's project name, from local files only.

    No network: the LangSmith project is created by
    ``OtelDemoInvestigator.begin_round`` once the preflight passed, so a
    refused round makes no external call (interface r4). Never prints values.
    """
    from scripts.m1_live_flash_loop import load_langsmith_env, read_key

    explicit = os.environ.get("M0_ENV_FILE")
    if not explicit or not os.path.isfile(os.path.expanduser(explicit)):
        raise LabError("ENV_UNCONFIRMED")
    path = Path(explicit).expanduser().resolve()
    key = read_key(path)
    if not key:
        raise LabError("ENV_UNCONFIRMED")
    load_langsmith_env(path)
    env = os.environ
    if env.get(tracing.TRACE_ENV) != "lab" or env.get("OPSPILOT_TOOL_PROFILE") != (
        "otel-demo"
    ):
        raise LabError("ENV_UNCONFIRMED")
    try:
        # Only names the round's project in the environment (no request).
        configure_lab_round(env, round_id)
    except ValueError:
        raise LabError("ENV_UNCONFIRMED") from None
    if tracing.check_lab_target(env):
        raise LabError("ENV_UNCONFIRMED")
    return RoundSession(key, None, otel_demo.OtelDemoConfig.from_env(env))


class _Delegate:
    """A forwarding seat filled inside ``record()`` (model/executor/delivery)."""

    def __init__(self, target: Any) -> None:
        self.target = target

    def __getattr__(self, name: str) -> Any:
        return getattr(self.target, name)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.target(*args, **kwargs)


class _Checkpointed:
    """Checkpoints around every call of a recording proxy (r16 R8): outside
    the proxy, so a completed physical call is recorded first; the resulting
    ``Interrupted`` is a BaseException and passes the Run's error mapping."""

    def __init__(self, target: Any) -> None:
        self.target = target

    def __getattr__(self, name: str) -> Any:
        value = getattr(self.target, name)
        if not callable(value):
            return value

        def call(*args: Any, **kwargs: Any) -> Any:
            checkpoint()
            result = value(*args, **kwargs)
            checkpoint()
            return result

        return call

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        checkpoint()
        result = self.target(*args, **kwargs)
        checkpoint()
        return result


def _sealed_record(
    drive: Callable[[Any], IncidentOutcome],
    *,
    destination: Path,
    context: RecordingContext,
) -> ArtifactRef:
    """``record(drive)`` with its sealing protected (r16 R4): from the moment
    ``drive`` returns until ``record`` returns or raises, no checkpoint fires
    and no forced stop raises, so ``recording.json`` is written and
    ``INCOMPLETE`` removed whatever arrives. The Run itself stays
    interruptible, and a ``drive`` that raises never enters the section."""
    sealing = ExitStack()

    def sealed(hooks: Any) -> IncidentOutcome:
        outcome = drive(hooks)
        sealing.enter_context(protected())
        return outcome

    with sealing:
        return record(sealed, destination=destination, context=context)


class _BusinessClock:
    """The Run's clock; inside ``record()`` every reading also advances the
    recording's business clock (B2 ``CaptureHooks.clock``), so each event is
    stamped no earlier than the operation times it contains."""

    def __init__(self, base: LiveClock) -> None:
        self.base = base
        self.hooks: Any = None
        self.last: Any = None

    def now(self) -> Any:
        at = self.base.now()
        # Never let a wall-clock step backwards reach the recording.
        if self.last is not None and at < self.last:
            at = self.last
        self.last = at
        if self.hooks is not None:
            self.hooks.clock(at)
        return at

    def monotonic(self) -> float:
        return self.base.monotonic()

    def sleep(self, seconds: float) -> None:
        self.base.sleep(seconds)


def _window_json(window: Any) -> Mapping[str, Any] | None:
    return None if window is None else window.as_json()


def _versions(manifest: Any) -> dict[str, str]:
    return {
        name: str(value.value)
        for name, value in manifest.dimensions.items()
        if value.kind in ("hash", "version")
    }


def _finish_reason(destination: Any, ref: ArtifactRef | None) -> str:
    """The final model reply's own finish reason, read from the recording."""
    from opspilot.evaluation import load_recording
    from opspilot.evaluation.recording import json_blob

    if ref is None:
        return "unknown"
    recording = load_recording(destination / ref.path, expected_hash=ref.sha256)
    for event in reversed(recording.events):
        if event.kind == "model" and event.response_ref is not None:
            return str(json_blob(recording, event.response_ref).get("finish_reason"))
    return "unknown"


class OtelDemoInvestigator:
    def __init__(
        self,
        session: RoundSession,
        clock: LiveClock,
        *,
        client_factory: Callable[[str, LiveClock], DeepSeekClient] | None = None,
    ) -> None:
        self.session = session
        self.clock = clock
        # Tests hand in a client over a scripted opener; live builds the real one.
        self.client_factory = client_factory or (
            lambda key, clock: DeepSeekClient(key, clock=clock)
        )
        # The manifest's registry/permissions are built from the same config.
        self.config = session.config

    def begin_round(self, round_id: str) -> None:
        """After the preflight: the round's LangSmith project (longest
        retention, set before any trace) and the lab tracer."""
        try:
            project = prepare_lab_project(
                os.environ, round_id, client_factory=langsmith_client
            )
        except SystemExit:
            raise LabError("ENV_UNCONFIRMED") from None
        except Exception:  # noqa: BLE001 - a LangSmith outage refuses the round
            raise LabError("TRACE_PROJECT_UNAVAILABLE") from None
        self.session = replace(self.session, project=project)
        tracing.configure(os.environ)

    def investigate(self, request: Any) -> Any:
        from scripts.eval_live import InvestigationResult

        clock = _BusinessClock(self.clock)
        attempt, spec, manifest = request.attempt, request.spec, request.manifest
        if (
            request.submit_not_after is not None
            and clock.now() > request.submit_not_after
        ):
            return InvestigationResult(
                None,
                None,
                None,
                None,
                None,
                (),
                None,
                None,
                None,
                {},
                "PRE_OBSERVATION_STALE",
            )
        web = DurableStore(request.pg.web_dsn or request.pg.runtime_dsn)
        web.install()
        store = DurableStore(request.pg.runtime_dsn)
        store.install()
        log = DurableEventLog(store)
        log.install()
        evidence = DurableEvidenceStore(store)
        evidence.install()
        incident, run_id = uuid4(), uuid4()
        submitted = clock.now()
        deadline = submitted + RUN_WALL
        face = otel_demo.otel_demo_face(clock)
        run_input = face.input_for(
            run_id=str(run_id),
            question=spec.stimulus.question,
            target_id=spec.stimulus.target_id,
            deadline=deadline,
            model_requests=M1_FROZEN_LIMITS.model_requests,
        )
        versions = otel_demo.otel_demo_versions()
        web.accept(
            incident,
            run_id,
            f"eval-live-{request.round_id}-{attempt.attempt_id}",
            deadline=deadline,
            budget_limit=M1_FROZEN_LIMITS.model_requests,
            versions=versions,
            input=run_input.as_json(),
        )
        client = self.client_factory(self.session.key, clock)
        model = _Delegate(client)
        delivery = _Delegate(None)
        facade = _Delegate(None)
        build = otel_demo.otel_demo_executor_factory(
            store, evidence=evidence, clock=clock, config=self.session.config
        )
        registrations = otel_demo.otel_demo_registrations(self.session.config)
        seen: dict[str, Any] = {}

        def factory(lease: Any, input: Any) -> Any:
            executor = build(lease, input)
            seen["executor"] = executor
            facade.target = executor
            return facade

        def wrapper(
            lease: Any, input: Any, executor: Any, proceed: Callable[[], RunnerOutcome]
        ) -> RunnerOutcome:
            real = seen["executor"]
            scope = real.scope
            context = RecordingContext(
                recording_id=f"eval-live-{request.round_id}-{attempt.attempt_id}",
                source_run_id=str(run_id),
                source_manifest_hash="0" * 64,
                scenario_id=spec.scenario_id,
                scenario_version=spec.version,
                synthetic=False,
                versions=_versions(manifest),
                stimulus=dict(investigator_input(spec)),
                initial_state={},
                input_watermark=f"input:{run_id}",
                control_watermark=f"control:{lease.control_generation}",
                started_at=clock.now(),
                ended_at=clock.now(),
                bindings=tuple(
                    ToolBinding(
                        tool=tool.name,
                        version=tool.version,
                        target=target.target_id,
                        source=tool.source,
                        authorized_window=scope.window.as_json(),
                        default_window=_window_json(scope.default_query_window),
                        permissions_hash="0" * 64,
                        registry_hash="0" * 64,
                        profile_hash=canonical_hash(versions),
                        projection_hash=str(manifest.dimensions["projection"].value),
                    )
                    for tool in registrations.tools
                    for target in registrations.targets
                ),
            )
            context = bind_tool_snapshots(
                context,
                (ToolSnapshot(registrations.tools, registrations.targets, scope),),
                manifest,
            )
            seen["epoch"] = lease.epoch

            def drive(hooks: Any) -> IncidentOutcome:
                clock.hooks = hooks
                hooks.input(canonical_bytes(input.as_json()))
                model.target = _Checkpointed(hooks.model(client))
                client.capture_http = hooks.http
                facade.target = _Checkpointed(hooks.tools(real))
                delivery.target = hooks.delivery
                seen["runner"] = proceed()
                clock.hooks = None
                hooks.finish(clock.now())
                return _outcome(spec, store, log, incident)

            request.recording_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                seen["recording_ref"] = _sealed_record(
                    drive, destination=request.recording_dir, context=context
                )
            except Exception as exc:  # noqa: BLE001 - fixed code kept, Run result kept
                seen["recording_error"] = getattr(exc, "code", "RECORDING_INCOMPLETE")
            runner_outcome = seen.get("runner")
            if not isinstance(runner_outcome, RunnerOutcome):
                raise LabError(seen.get("recording_error", "RECORDING_INCOMPLETE"))
            return runner_outcome

        runner = InvestigationRunner(
            store=store,
            worker=Worker.create(store, dict(versions)),
            model=model,
            executor_factory=factory,
            clock=clock,
            lease_seconds=LEASE_SECONDS,
            events=log,
            evidence=evidence,
            capture_delivery=delivery,
            attempt_wrapper=wrapper,
        )
        started = clock.now()
        error: str | None = None
        try:
            runner.resume(incident)
        except LabError as exc:
            error = exc.code
        except Exception as exc:  # noqa: BLE001 - the Run's own failure, fixed code
            error = "RUNNER_FAILED"
            # Type name only (no message: it may carry provider text).
            seen["runner_exception"] = type(exc).__name__
        ended = clock.now()
        try:
            outcome: IncidentOutcome | None = _outcome(spec, store, log, incident)
        except ValueError:
            # A Run left mid-attempt has no projectable final state.
            outcome = None
        with protected():
            write_private(
                request.ledger_dir / "investigator.json",
                json.dumps(
                    {
                        "runner_exception": seen.get("runner_exception"),
                        "recording_error": seen.get("recording_error"),
                        "error": error,
                    },
                    sort_keys=True,
                ).encode(),
            )
        report = _report(store, incident)
        ref = seen.get("recording_ref")
        finish = _finish_reason(request.recording_dir, ref) if ref is not None else None
        tracer = tracing.tracer()
        trace_id = tracer.last_trace_id
        traces = _traces(self.session, attempt.attempt_id, str(run_id), trace_id, clock)
        usage = store.run_usage(run_id)
        runner_outcome = seen.get("runner")
        loop = None if runner_outcome is None else runner_outcome.loop
        return InvestigationResult(
            product_run_id=str(run_id),
            outcome=outcome,
            report=None
            if report is None
            else ReportCapture(raw=report, finish_reason=finish or "unknown"),
            recording_ref=ref,
            recording_error=seen.get("recording_error"),
            traces=traces,
            submitted_at=submitted,
            started_at=started,
            ended_at=ended,
            usage={
                "epoch": seen.get("epoch"),
                "runner_status": None
                if runner_outcome is None
                else runner_outcome.status,
                "model_requests_used": usage.get("model_requests_used"),
                "model_seconds_used": usage.get("model_seconds_used"),
                "tool_operations_used": usage.get("tool_operations_used"),
                "tool_seconds_used": usage.get("tool_seconds_used"),
                "steps_committed": None if loop is None else loop.steps_committed,
            },
            error=error,
        )


def _outcome(
    spec: Any, store: DurableStore, log: Any, incident: Any
) -> IncidentOutcome:
    snapshot = store.rebuild(incident)
    controls = tuple(
        asdict(row) for row in DurableIncidentStore(store).control_audit(incident)
    )
    handoffs = tuple(
        dict(event.payload)
        for event in log.read_after(incident, 0, limit=1000)
        if event.kind == "run_handoff"
    )
    return outcome_from_durable(
        spec.incident, snapshot, controls=controls, handoff_events=handoffs
    )


def _report(store: DurableStore, incident: Any) -> str | None:
    for step in reversed(store.rebuild(incident)["steps"]):
        response = step["response"] or {}
        if response.get("kind") == "conclusion":
            content = response["conclusion"].get("report_content")
            return None if content is None else str(content)
    return None


def _traces(
    session: RoundSession,
    attempt_id: str,
    run_id: str,
    trace_id: str | None,
    clock: LiveClock,
) -> tuple[TraceRecord, ...]:
    # Flush this attempt's spans before the bounded read-back, then reopen
    # the exporter for the next attempt; checkpoints before the flush and
    # after the read-back (R1).
    checkpoint()
    tracing.shutdown()
    evidence = trace_evidence(
        {"mode": "lab", "trace_id": trace_id},
        run_id=run_id,
        project=session.project,
        client_factory=langsmith_client,
    )
    tracing.configure(os.environ)
    checkpoint()
    raw = str(evidence.get("read_back"))
    read_back = (
        raw
        if raw in ("found", "root_run_not_found", "no_trace_id", "not_exported")
        else "error"
    )
    found = evidence.get("langsmith_run_id")
    return (
        TraceRecord(
            entry=TraceEntry(
                role="investigation",
                required=True,
                read_back=read_back,  # type: ignore[arg-type]
                run_id=found,
                # The root was looked up by this Run's own OTel trace id.
                expected_run_id=found if trace_id else None,
                product_run_id=run_id,
            ),
            attempt_id=attempt_id,
            evaluation_id=attempt_id,
            project=evidence.get("project"),
            url=evidence.get("url"),
            raw_read_back=json.dumps(
                {"read_back": raw, "otel_trace_id": trace_id}, sort_keys=True
            ),
            checked_at=clock.now(),
        ),
    )
