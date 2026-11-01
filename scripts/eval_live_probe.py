"""Lab lifecycle probe of the live driver (eval PR-C, A11): no product Run.

Interface: docs/tasks/2026-10-18-eval-pr-c-interface.md section 2 (``--probe``,
r8). One scenario at a time on the shared lab, through the same seams and lab
protocol as a real round: preflight, restore intent, injection, flagd
propagation, the U1 fault gate (or the normal window), restore and wash. It
never creates an incident or Run, never calls a model, starts no PostgreSQL,
reads no balance and creates no LangSmith project, and writes no attempt
journal or summary, so a probe is never counted as an attempt.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from opspilot.evaluation import ScenarioError
from scripts import eval_live
from scripts.eval_live_lab import (
    UNREACHABLE_URLS,
    V2_FAULT_FLAGS,
    AttemptLedger,
    Committed,
    FaultIntent,
    Interrupted,
    LabError,
    LiveDeps,
    PreObservation,
    active_marker,
    append_run_start,
    await_fault,
    check_occupation,
    checkpoint,
    commit_fault,
    confirm_all_normal,
    fault_for,
    fault_gate,
    fault_guard,
    interrupt_scope,
    mark_injector,
    normal_window,
    observation_json,
    protected,
    read_state,
    request_stop,
    restore_fault,
    stop_requested,
)


def probe_preflight(config: eval_live.RoundConfig, deps: LiveDeps) -> str | None:
    """The round preflight minus PG and provenance; mutates nothing."""
    environment = deps.env.check()
    if environment is not None:
        return environment
    if eval_live.round_evidence(deps, config.round_id).exists():
        return "ROUND_EXISTS"
    try:
        check_occupation(
            deps.ledger_root, config.holder, config.batch, deps.clock.now()
        )
        if read_state(deps.ledger_root) is not None:
            return "FAULT_RECOVERY_REQUIRED"
        with fault_guard(deps.ledger_root):
            pass
        if deps.identity.verify():
            return "LAB_IDENTITY_UNCONFIRMED"
        v2 = any(eval_live.is_v2(slot) for slot in config.slots)
        confirm_all_normal(
            deps,
            deps.flags.read(),
            budget_seconds=eval_live.PREFLIGHT_FLAG_SECONDS,
            whitelist=V2_FAULT_FLAGS if v2 else None,
        )
        eval_live.check_slots(config.slots)
    except LabError as exc:
        return exc.code
    except OSError:
        return "LAB_LEDGER_UNAVAILABLE"
    return None


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _probe_one(
    config: eval_live.RoundConfig,
    deps: LiveDeps,
    slot: eval_live.Slot,
    signatures: Sequence[Any],
) -> tuple[dict[str, Any], bool]:
    """The probe.json entry, and whether its private ledger was complete."""
    if slot.plan is not None:
        return _probe_v2(config, deps, slot, signatures)
    spec, answer = slot.spec, slot.answer
    attempt_id = f"probe-{spec.scenario_id}"
    ledger = AttemptLedger(
        eval_live.attempt_ledger_dir(deps, config.round_id, attempt_id), deps.clock
    )
    entry: dict[str, Any] = {
        "scenario_id": spec.scenario_id,
        "kind": spec.kind,
        "flag": None if answer.injected is None else answer.injected.flag,
        "gate": None,
        "fault": None,
        "reason": None,
        "closure_reason": None,
        "interrupted": False,
    }
    gate: PreObservation | None = None
    injected = False
    evidence_failed = False
    try:
        check_occupation(
            deps.ledger_root, config.holder, config.batch, deps.clock.now()
        )
        with fault_guard(deps.ledger_root):
            try:
                checkpoint()
                if deps.identity.verify():
                    raise LabError("LAB_IDENTITY_UNCONFIRMED")
                if read_state(deps.ledger_root) is not None:
                    raise LabError("LAB_FAULT_FOREIGN")
                if spec.kind == "fault":
                    assert answer.injected is not None and answer.signature
                    intent = FaultIntent(
                        config.round_id,
                        attempt_id,
                        config.holder,
                        config.batch,
                        fault_for(answer.injected.flag, answer.injected.variant),
                    )
                    checkpoint()  # before the injection
                    injected = True

                    def committed(fault: Committed) -> None:
                        # Inside the commit's protected section, before the
                        # propagation wait (R6, #273 item 3).
                        entry["fault"] = {
                            "original_sha256": fault.original_sha256,
                            "injected_sha256": fault.injected_sha256,
                            "effective_at": None,
                            "restored": False,
                            "washed": False,
                        }

                    result = commit_fault(
                        intent, deps=deps, ledger=ledger, record=committed
                    )
                    effective_at = await_fault(result, deps=deps, ledger=ledger)
                    entry["fault"]["effective_at"] = _iso(effective_at)
                    gate = fault_gate(
                        answer.signature, confirmed_at=effective_at, deps=deps
                    )
                else:
                    confirmed = confirm_all_normal(deps, deps.flags.read())
                    gate = normal_window(signatures, not_before=confirmed, deps=deps)
                with protected():
                    ledger.write(
                        "pre-observation.json",
                        {**observation_json(gate), "raw": [dict(r) for r in gate.raw]},
                    )
                if not gate.passed:
                    entry["reason"] = gate.reason
            except (Interrupted, KeyboardInterrupt):
                request_stop()
                entry["interrupted"] = True
                entry["reason"] = entry["reason"] or "INTERRUPTED"
            except LabError as exc:
                entry["reason"] = exc.code
            except OSError:
                # A private evidence/state write: restored below, then the
                # probe stops with exit 5 (r11; a retained fault keeps 4).
                evidence_failed = True
                entry["reason"] = entry["reason"] or "EVIDENCE_WRITE_FAILED"
            except Exception:  # noqa: BLE001 - a driver bug still restores
                entry["reason"] = entry["reason"] or "DRIVER_ERROR"
            finally:
                if injected:
                    _restore(config, deps, attempt_id, signatures, ledger, entry)
    except LabError as exc:
        entry["reason"] = entry["reason"] or exc.code
    except OSError:
        # Occupation or guard file (any restore above already ran).
        entry["reason"] = entry["reason"] or "LAB_LEDGER_UNAVAILABLE"
    except (Interrupted, KeyboardInterrupt):
        # Before the guard (the occupation lock wait): nothing was touched.
        request_stop()
        entry["interrupted"] = True
        entry["reason"] = entry["reason"] or "INTERRUPTED"
    if stop_requested():
        # A stop requested during a protected section (the restore) or a
        # path without a checkpoint after it: the probe ends here (130).
        entry["interrupted"] = True
    if gate is not None:
        entry["gate"] = observation_json(gate)
    with protected():
        ledger.event("probe", "finished", reason=entry["reason"])
    return entry, not (ledger.write_failed or evidence_failed)


class _Tripwire:
    """The probe's model ``inner``: never the network; counts what reached it."""

    def __init__(self) -> None:
        self.calls = 0

    def open(self, request: Any, timeout: float) -> Any:
        self.calls += 1
        raise LabError("PROBE_TRIPWIRE")


def _model_check(mode: str, calls: int = 3) -> dict[str, Any]:
    """Drive the 503 front ``calls`` times over a tripwire (seams §7)."""
    import urllib.error
    import urllib.request

    from scripts.eval_live_inject import ModelTransportFault

    inner = _Tripwire()
    fault = ModelTransportFault(mode, inner)  # type: ignore[arg-type]
    statuses: list[int | str] = []
    for _ in range(calls):
        request = urllib.request.Request("https://model.invalid/", data=b"{}")
        try:
            fault.open(request, timeout=1.0)
        except urllib.error.HTTPError as exc:
            statuses.append(exc.code)
        except LabError as exc:
            statuses.append(exc.code)
    return {
        "mode": mode,
        "calls": calls,
        "statuses": statuses,
        "intercepted": fault.intercepted,
        "forwarded": fault.forwarded,
        "inner_calls": inner.calls,
        "model_requests": 0,
    }


def _alert_check(
    config: eval_live.RoundConfig,
    deps: LiveDeps,
    attempt_id: str,
    params: Any,
    starts_at: datetime,
) -> dict[str, Any]:
    """Deliver the sequence to a throwaway attempt PG; no runner (seams §7)."""
    from opspilot.persistence import DurableStore
    from opspilot.tools import otel_demo
    from scripts.eval_live_inject import alert_entry, intake_counts, intake_record

    if not config.pg_port:
        raise LabError("PG_PORT_REQUIRED")
    if deps.ports.in_use(config.pg_port):
        raise LabError("PG_PORT_IN_USE")
    directory = deps.pg_root / config.round_id / attempt_id / "pg"
    if directory.exists() or directory.is_symlink():
        raise LabError("PG_DIRECTORY_EXISTS")
    pg = deps.pg.start(directory, config.pg_port)
    try:
        web = DurableStore(pg.web_dsn or pg.runtime_dsn)
        web.install()
        run = alert_entry(
            params,
            web=web,
            face=otel_demo.otel_demo_face(deps.clock),
            versions=otel_demo.otel_demo_versions(),
            clock=deps.clock,
            round_id=config.round_id,
            first_starts_at=starts_at,
        )
        counts = intake_counts(pg.owner_dsn)
    finally:
        deps.pg.stop(pg)
    return {**intake_record("alertmanager", run, counts), "model_requests": 0}


def _probe_v2(
    config: eval_live.RoundConfig,
    deps: LiveDeps,
    slot: eval_live.Slot,
    signatures: Sequence[Any],
) -> tuple[dict[str, Any], bool]:
    """A v2 scenario's zero-model probe (#431 seams §7): intent, lab-side
    injectors and their effect, in-process self-checks, wash. No incident
    on the lab, no Run, no model call."""
    from scripts.eval_live_inject import OtlpSpanWriter, connection_refused, write_span

    plan, answer = slot.plan, slot.answer
    assert plan is not None
    attempt_id = f"probe-{slot.spec.scenario_id}"
    ledger = AttemptLedger(
        eval_live.attempt_ledger_dir(deps, config.round_id, attempt_id), deps.clock
    )
    entry: dict[str, Any] = {
        "scenario_id": slot.spec.scenario_id,
        "entry": plan.entry,
        "injectors": [dict(item) for item in plan.injectors],
        "effects": {},
        "gate": None,
        "fault": None,
        "reason": None,
        "closure_reason": None,
        "interrupted": False,
        "model_requests": 0,
    }
    effects: dict[str, Any] = entry["effects"]
    gate: PreObservation | None = None
    injected = False
    evidence_failed = False
    try:
        check_occupation(
            deps.ledger_root, config.holder, config.batch, deps.clock.now()
        )
        with fault_guard(deps.ledger_root):
            try:
                checkpoint()
                if deps.identity.verify():
                    raise LabError("LAB_IDENTITY_UNCONFIRMED")
                if read_state(deps.ledger_root) is not None:
                    raise LabError("LAB_FAULT_FOREIGN")
                intent = FaultIntent(
                    config.round_id,
                    attempt_id,
                    config.holder,
                    config.batch,
                    plan.flags[0] if plan.flags else None,
                    also=plan.flags[1:],
                    injectors=plan.injectors,
                    whitelist=V2_FAULT_FLAGS,
                )
                checkpoint()
                injected = True

                def committed(fault: Committed) -> None:
                    entry["fault"] = {
                        "flags": [f.flag for f in intent.faults],
                        "original_sha256": fault.original_sha256,
                        "injected_sha256": fault.injected_sha256,
                        "effective_at": None,
                        "restored": False,
                        "washed": False,
                    }

                result = commit_fault(
                    intent, deps=deps, ledger=ledger, record=committed
                )
                effective_at = await_fault(result, deps=deps, ledger=ledger)
                entry["fault"]["effective_at"] = _iso(effective_at)
                if plan.flags:
                    mark_injector(deps.ledger_root, "flagd", phase="effective")
                if answer.signature is not None:
                    gate = fault_gate(
                        answer.signature, confirmed_at=effective_at, deps=deps
                    )
                else:
                    gate = normal_window(signatures, not_before=effective_at, deps=deps)
                with protected():
                    ledger.write(
                        "pre-observation.json",
                        {**observation_json(gate), "raw": [dict(r) for r in gate.raw]},
                    )
                if not gate.passed:
                    entry["reason"] = gate.reason
                if plan.source is not None:
                    url = UNREACHABLE_URLS[plan.source]
                    effects["source_unreachable"] = {
                        "source": plan.source,
                        "url": url,
                        "connection_refused": connection_refused(url),
                    }
                    if not effects["source_unreachable"]["connection_refused"]:
                        entry["reason"] = entry["reason"] or "SOURCE_PORT_IN_USE"
                if plan.model_mode is not None:
                    effects["model_transport"] = _model_check(plan.model_mode)
                if plan.span is not None:
                    writer = deps.span_writer or OtlpSpanWriter()

                    def planned(span: Any) -> None:
                        mark_injector(
                            deps.ledger_root,
                            "otlp_span",
                            phase="intent",
                            trace_id=span.trace_id,
                            span_end=span.end.isoformat(),
                        )

                    span = write_span(
                        plan.span, writer=writer, clock=deps.clock, on_planned=planned
                    )
                    mark_injector(deps.ledger_root, "otlp_span", phase="effective")
                    effects["otlp_span"] = {
                        "trace_id": span.trace_id,
                        "span_start": _iso(span.start),
                        "span_end": _iso(span.end),
                        "visible": True,
                    }
                if plan.alert is not None:
                    effects["alert_delivery"] = _alert_check(
                        config, deps, attempt_id, plan.alert, effective_at
                    )
            except (Interrupted, KeyboardInterrupt):
                request_stop()
                entry["interrupted"] = True
                entry["reason"] = entry["reason"] or "INTERRUPTED"
            except LabError as exc:
                entry["reason"] = entry["reason"] or exc.code
            except OSError:
                evidence_failed = True
                entry["reason"] = entry["reason"] or "EVIDENCE_WRITE_FAILED"
            except Exception:  # noqa: BLE001 - a driver bug still restores
                entry["reason"] = entry["reason"] or "DRIVER_ERROR"
            finally:
                if injected:
                    _restore(config, deps, attempt_id, signatures, ledger, entry)
    except LabError as exc:
        entry["reason"] = entry["reason"] or exc.code
    except OSError:
        entry["reason"] = entry["reason"] or "LAB_LEDGER_UNAVAILABLE"
    except (Interrupted, KeyboardInterrupt):
        request_stop()
        entry["interrupted"] = True
        entry["reason"] = entry["reason"] or "INTERRUPTED"
    if stop_requested():
        entry["interrupted"] = True
    if gate is not None:
        entry["gate"] = observation_json(gate)
    with protected():
        ledger.event("probe", "finished", reason=entry["reason"])
    return entry, not (ledger.write_failed or evidence_failed)


def _restore(
    config: eval_live.RoundConfig,
    deps: LiveDeps,
    attempt_id: str,
    signatures: Sequence[Any],
    ledger: AttemptLedger,
    entry: dict[str, Any],
) -> None:
    try:
        # The restore and its result are one protected section (r16).
        with protected():
            state = read_state(deps.ledger_root)
            if state is None or state.get("attempt_id") != attempt_id:
                return
            restored = restore_fault(
                config.round_id, attempt_id, signatures, deps=deps, ledger=ledger
            )
            if restored is not None and entry["fault"] is not None:
                entry["fault"].update(restored=True, washed=True)
    except (Interrupted, KeyboardInterrupt):
        request_stop()
        entry["closure_reason"] = "RESTORE_FAILED:INTERRUPTED"
    except LabError as exc:
        entry["closure_reason"] = f"RESTORE_FAILED:{exc.code}"
    except Exception:  # noqa: BLE001 - a driver bug never hides a retained fault
        entry["closure_reason"] = "RESTORE_FAILED:DRIVER_ERROR"


def run_probe(config: eval_live.RoundConfig, deps: LiveDeps) -> eval_live.RoundResult:
    with interrupt_scope():
        return _run_probe(config, deps)


def _run_probe(config: eval_live.RoundConfig, deps: LiveDeps) -> eval_live.RoundResult:
    refusal = probe_preflight(config, deps)
    if refusal is not None:
        return eval_live._refuse(config, refusal)
    try:
        signatures = eval_live.fault_signatures(deps)
    except (OSError, ScenarioError):
        return eval_live._refuse(config, "SCENARIO_DIRECTORY_INVALID")
    try:
        append_run_start(
            deps.ledger_root,
            config.round_id,
            config.holder,
            config.batch,
            deps.clock.now(),
        )
    except LabError as exc:
        return eval_live._refuse(config, exc.code)
    except OSError:
        return eval_live._refuse(config, "LAB_LEDGER_UNAVAILABLE")
    results = [
        eval_live.SlotResult(s.spec.scenario_id, s.spec.version, s.repetition)
        for s in config.slots
    ]
    entries: list[dict[str, Any]] = []
    status: Literal["finished", "stopped", "interrupted"] = "finished"
    exit_code: int = 0
    reason: str | None = None
    try:
        with active_marker(
            deps.ledger_root,
            config.holder,
            config.batch,
            config.round_id,
            deps.clock.now(),
        ):
            for index, slot in enumerate(config.slots):
                checkpoint()  # before every slot
                entry, ledger_ok = _probe_one(config, deps, slot, signatures)
                entries.append(entry)
                out = results[index]
                out.attempt_id = f"probe-{slot.spec.scenario_id}"
                out.status = "ran" if entry["reason"] is None else "setup_failed"
                out.reason, out.closure_reason = (
                    entry["reason"],
                    entry["closure_reason"],
                )
                if entry["closure_reason"] is not None:
                    status, exit_code, reason = "stopped", 4, entry["closure_reason"]
                elif not ledger_ok:
                    # The private probe ledger is incomplete (r11, 5 > 130).
                    status, exit_code, reason = "stopped", 5, "EVIDENCE_WRITE_FAILED"
                elif entry["interrupted"]:
                    status, exit_code, reason = "interrupted", 130, "INTERRUPTED"
                elif entry["reason"] == "LAB_FAULT_FOREIGN":
                    # Section 5: another holder's fault is live; stop (r10).
                    status, exit_code, reason = "stopped", 1, "LAB_FAULT_FOREIGN"
                elif entry["reason"] is not None:
                    exit_code = 1
                if status != "finished":
                    for later in results[index + 1 :]:
                        later.reason = "ROUND_STOPPED"
                    break
    except LabError as exc:
        if eval_live._outranks(3, exit_code):
            status, exit_code, reason = "stopped", 3, exc.code
    except (Interrupted, KeyboardInterrupt):
        request_stop()
        if eval_live._outranks(130, exit_code):
            status, exit_code, reason = "interrupted", 130, "INTERRUPTED"
    except OSError:
        # The active marker could not be written or removed: never success.
        if eval_live._outranks(1, exit_code):
            status, exit_code, reason = "stopped", 1, "LAB_LEDGER_UNAVAILABLE"
    evidence = eval_live.round_evidence(deps, config.round_id)
    # Like run_round: the report is written in a protected section and a
    # stop requested meanwhile is read afterwards, so the final JSON always
    # comes out and the round then exits 130 (precedence 4 > 5 > 130).
    written = False
    with protected():
        retained, own = eval_live._retained(deps, config.round_id)
        if own and eval_live._outranks(4, exit_code):
            # This round's intent survived a path cut short: --recover (r13).
            status, exit_code, reason = "stopped", 4, "FAULT_RECOVERY_REQUIRED"
        document = {
            "schema": "opspilot-eval-live-probe-1",
            "round": config.round_id,
            "round_status": status,
            "exit_code": exit_code,
            "reason": reason,
            "retained_fault": retained,
            "scenarios": entries,
        }
        try:
            written = eval_live.write_public_exclusive(
                evidence, ["probe.json"], document
            )
        except (Interrupted, KeyboardInterrupt):
            request_stop()
            written = (evidence / "probe.json").is_file()
    if not written:
        # r11: an evidence write failure is exit 5 with the final JSON; a
        # retained fault (4) keeps its own exit code.
        if eval_live._outranks(5, exit_code):
            status, exit_code, reason = "stopped", 5, "EVIDENCE_WRITE_FAILED"
    if stop_requested() and eval_live._outranks(130, exit_code):
        status, exit_code, reason = "interrupted", 130, "INTERRUPTED"
    return eval_live.RoundResult(
        config.round_id, status, exit_code, reason, tuple(results)
    )
