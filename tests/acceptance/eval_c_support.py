"""Offline doubles for the PR-C r5 public protocols; no live adapter imports."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import asdict, fields, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from opspilot.evaluation import ToolSnapshot, bind_tool_snapshots, record
from opspilot.investigation.loop import ModelCall, serialized_request
from opspilot.tools.otel_demo import (
    CREDENTIAL_REF,
    METRICS_TOOL,
    TARGET_ID,
    OtelDemoTransport,
    otel_demo_registrations,
)
from scripts import eval_live_lab as lab
from scripts.eval_live import InvestigationRequest, InvestigationResult, template_scope
from tests.acceptance import eval_b2_support as b2
from tests.m1_tool_support import FakeClock, FixedControl, build

ROUND = "canary-round"
HOLDER = "CANARY-HOLDER"
BATCH = "CANARY-BATCH"
SECRET = "CANARY-CREDENTIAL-83c6"
RAW_HTTP = "CANARY-RAW-HTTP-74c1"
PRIVATE_ANSWER = "CANARY-PRIVATE-ANSWER-0a91"
FAULT = "eval-payment-failure-1"
NORMAL = "eval-normal-1"
VARIANTS = {
    "paymentFailure": "100%",
    "productCatalogFailure": "on",
    "cartFailure": "on",
}


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def encode_outcome(outcome):
    return encode(asdict(outcome))


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def flags_raw():
    return encode(
        {
            "flags": {
                name: {
                    "defaultVariant": "off",
                    "variants": {
                        "off": False,
                        variant: True,
                    },
                }
                for name, variant in VARIANTS.items()
            }
        }
    )


class Clock:
    def __init__(self):
        self.utc = datetime(2026, 10, 18, tzinfo=UTC)
        self.mono = 0.0

    def now(self):
        return self.utc

    def monotonic(self):
        return self.mono

    def sleep(self, seconds):
        assert 0 <= seconds <= 600
        self.mono += seconds
        self.utc += timedelta(seconds=seconds)


class Flags:
    def __init__(self, harness):
        self.h = harness
        self.current = lab.FlagConfig(flags_raw(), "canary-cm", "1")
        self.writes = []
        self.on_replace = None
        self.after_write = None

    def read(self):
        return self.current

    def replace(self, current, new_raw, *, fence):
        # Persistence and exclusivity are observable safety boundaries, not
        # private call-order assertions. Every write must already have intent.
        path = self.h.deps.ledger_root / "fault-active.json"
        assert path.is_file(), "ConfigMap write happened without durable intent"
        assert path.stat().st_mode & 0o777 == 0o600
        intent = json.loads(path.read_bytes())
        assert intent["owner"] == "eval-live-1"
        assert intent["round"] == ROUND
        assert intent["original_sha256"] == sha(intent["original"].encode())
        assert intent["injected_sha256"] == sha(intent["injected"].encode())
        if self.on_replace:
            self.on_replace(intent, current, new_raw, fence)
        if current != self.current:
            raise lab.LabError("FLAG_CAS_CONFLICT")
        self.writes.append((current, new_raw, fence))
        self.current = replace(
            current,
            raw=new_raw,
            resource_version=str(int(current.resource_version) + 1),
        )
        if self.after_write:
            self.after_write()
        return self.current


class Resolver:
    def __init__(self, flags):
        self.flags = flags
        self.calls = []
        self.override = None

    def resolve(self, flag, method, timeout):
        assert 0 < timeout <= 5
        self.calls.append((flag, method, timeout))
        variant = json.loads(self.flags.current.raw)["flags"][flag]["defaultVariant"]
        return self.override(flag, variant) if self.override else variant


class Telemetry:
    def __init__(self, harness):
        self.h = harness
        self.complete = True
        self.traffic = 1.0
        self.custom_spans = None
        self.custom_calls = None
        self.queries = []
        self.bad_wash = False

    def spans(self, service, start, end, timeout):
        assert 0 < timeout <= 5
        self.queries.append((service, start, end))
        if self.custom_spans:
            return self.custom_spans(service, start, end)
        spans = []
        live = json.loads(self.h.flags.current.raw)["flags"]
        for _, answer in self.h.scenarios:
            if answer.injected is None:
                continue
            if (
                live[answer.injected.flag]["defaultVariant"] == "off"
                and not self.bad_wash
            ):
                continue
            for matcher in answer.signature.any_of:
                if matcher.service == service:
                    spans.append(
                        lab.ObservedSpan(
                            "canary-trace",
                            "canary-span",
                            service,
                            matcher.operation,
                            end,
                            {"error": True, "private": PRIVATE_ANSWER},
                        )
                    )
        return lab.SpanQuery(tuple(spans), self.complete)

    def checkout_calls(self, start, end, timeout):
        assert 0 < timeout <= 5
        if self.custom_calls:
            return self.custom_calls(start, end)
        return lab.CallCount(self.traffic, self.complete)


class Identity:
    def __init__(self):
        self.result = ()
        self.callback = None

    def verify(self):
        if self.callback:
            return self.callback()
        return self.result


class Ports:
    def __init__(self):
        self.busy = False
        self.queried = []

    def in_use(self, port):
        self.queried.append(port)
        assert port == 55497, "driver switched ports or probed lab forwarding ports"
        return self.busy


class Pg:
    def __init__(self):
        self.started = []
        self.stopped = []
        self.callback = None

    def start(self, directory, port):
        if self.callback:
            self.callback(directory, port)
        assert not directory.exists(), "foreign PG directory reached launcher"
        directory.mkdir(parents=True)
        pg = lab.PgInstance(
            directory,
            port,
            f"postgresql://owner:{SECRET}@localhost:{port}/canary",
            f"postgresql://worker:{SECRET}@localhost:{port}/canary",
        )
        self.started.append(pg)
        return pg

    def stop(self, instance):
        assert instance in self.started, "stopped an instance it did not start"
        self.stopped.append(instance)


class Balance:
    def __init__(self):
        self.calls = 0

    def balance(self):
        self.calls += 1
        return Decimal("100.00")


class ProvenanceReader:
    """Deterministic provenance; never shells out to git or reads real HEAD."""

    def __init__(self):
        self.result = lab.Provenance(commit="a" * 40, clean=True)
        self.calls = 0
        self.callback = None

    def read(self):
        self.calls += 1
        if self.callback:
            self.callback()
        return self.result


class Env:
    def __init__(self):
        self.result = None
        self.calls = 0

    def check(self):
        self.calls += 1
        return self.result


def recording_context(attempt, manifest, round_id=ROUND, epoch=7):
    registrations = otel_demo_registrations()
    template = template_scope()
    duration = template.window.end - template.window.start
    scope = replace(
        template,
        scope_id=f"otel-demo:{attempt.product_run_id}:{epoch}",
        subject_id="CANARY-INCIDENT",
        run_id=attempt.product_run_id,
        control_generation=0,
        global_suspension_generation=0,
        target_suspension_generation=0,
        window=replace(template.window, start=b2.START - duration, end=b2.START),
        deadline=b2.END + timedelta(minutes=10),
    )
    ctx = b2.context(
        recording_id=f"eval-live-{round_id}-{attempt.attempt_id}",
        source_run_id=attempt.product_run_id,
        scenario_id=attempt.scenario_id,
        scenario_version=attempt.scenario_version,
        bindings=(),
        stimulus={**b2.context().stimulus, "target_id": TARGET_ID},
    )
    return bind_tool_snapshots(
        ctx,
        (ToolSnapshot(registrations.tools, registrations.targets, scope),),
        manifest,
    )


class Investigator:
    def __init__(self, harness):
        self.h = harness
        self.requests = []
        self.callback = None
        self.error = None
        self.recording_error = None
        self.submission_delay = 0
        self.before_creation_stale = False
        self.synthetic = False
        self.omit_recording = False
        self.recording_transform = None
        self.result_transform = None
        self.outcome_transform = None
        self.results = []
        self.recordings = []
        self.created_runs = []
        self.with_evidence = False
        self.tool_outcomes = []

    def investigate(self, request):
        assert isinstance(request, InvestigationRequest)
        self.requests.append(request)
        assert list(self.h.deps.ledger_root.glob(".active-*.json"))
        assert request.pg in self.h.pg.started
        if self.callback:
            self.callback(request)
        self.h.clock.sleep(self.submission_delay)
        observation = request.pre_observation
        expected_deadline = (
            max(span.start for span in observation.matched)
            if observation.kind == "fault"
            else observation.window.end
        ) + timedelta(seconds=300)
        assert request.submit_not_after == expected_deadline
        if self.before_creation_stale:
            assert self.h.clock.now() > request.submit_not_after
            result = InvestigationResult(
                product_run_id=None,
                outcome=None,
                report=None,
                recording_ref=None,
                recording_error=None,
                traces=(),
                submitted_at=None,
                started_at=self.h.clock.now(),
                ended_at=self.h.clock.now(),
                usage={},
                error="PRE_OBSERVATION_STALE",
            )
            self.results.append(result)
            return result
        run_id = f"canary-run-{len(self.requests)}"
        self.created_runs.append(run_id)
        attempt = replace(request.attempt, product_run_id=run_id)
        ctx = recording_context(attempt, request.manifest)
        scope = ctx.tool_snapshots[0].scope
        clock = FakeClock(start=ctx.started_at)
        if self.with_evidence:
            from tests.test_m1_otel_demo_contract import GOOD_EXPR, FakeOpener

            sample_at = scope.window.end - timedelta(minutes=1)
            response_body = encode(
                {
                    "status": "success",
                    "data": {
                        "resultType": "matrix",
                        "result": [
                            {
                                "metric": {"service_name": "checkout"},
                                "values": [[sample_at.timestamp(), "3"]],
                            }
                        ],
                    },
                }
            )
            opener = FakeOpener(by_query={GOOD_EXPR: response_body})
            transport = OtelDemoTransport(
                credentials={CREDENTIAL_REF: None}, opener=opener
            )
            registrations = otel_demo_registrations()
            executor, _, _, clock = build(
                registrations=registrations.tools,
                targets=registrations.targets,
                transport=transport,
                clock=clock,
                control=FixedControl(
                    generation=scope.control_generation,
                    global_generation=scope.global_suspension_generation,
                    target_generation=scope.target_suspension_generation,
                ),
                scope_overrides={
                    field.name: getattr(scope, field.name) for field in fields(scope)
                },
            )
            metric = next(
                tool for tool in registrations.tools if tool.name == METRICS_TOOL
            )
            ctx = replace(
                ctx,
                bindings=(
                    b2.binding(
                        tool=metric.name,
                        target=TARGET_ID,
                        source=metric.source,
                        authorized_window=scope.window.as_json(),
                        default_window=scope.default_query_window.as_json()
                        if scope.default_query_window
                        else None,
                    ),
                ),
            )
            ctx = bind_tool_snapshots(ctx, ctx.tool_snapshots, request.manifest)
        ctx = replace(
            ctx,
            stimulus={
                "question": request.spec.stimulus.question,
                "subject_id": request.spec.stimulus.subject_id,
                "target_id": TARGET_ID,
            },
        )
        result = b2.outcome(
            (),
            scenario_id=request.spec.scenario_id,
            final_state="failed",
            decision="handoff",
            report_available=False,
            handoff_reasons=("MODEL_UNAVAILABLE",),
            actions=(),
        )

        if self.outcome_transform:
            result = self.outcome_transform(result)

        def driver(hooks):
            nonlocal result
            from opspilot.tools import ToolRequest
            from opspilot.tools.registry import canonical
            from tests.m1_investigation_support import reply, tool_call

            class ModelWithHttp(b2.FakeModel):
                def complete(self, call):
                    response = super().complete(call)
                    response_id = f"{RAW_HTTP}-{len(self.calls)}"
                    usage = dict(response.usage)
                    payload = {
                        "id": response_id,
                        "object": "chat.completion",
                        "model": response.response_model,
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": response.content,
                                    "reasoning_content": response.reasoning_content,
                                    "tool_calls": [
                                        dict(item) for item in response.tool_calls
                                    ],
                                },
                                "finish_reason": response.finish_reason,
                            }
                        ],
                        "usage": usage,
                    }
                    hooks.http(
                        serialized_request(call), b2.canonical_bytes(payload), 200
                    )
                    return replace(response, raw={"id": response_id, "usage": usage})

            scripted = (
                [
                    reply(
                        tool_calls=[
                            tool_call(
                                name=METRICS_TOOL,
                                arguments=json.dumps({"expr": GOOD_EXPR}),
                            )
                        ],
                        finish="tool_calls",
                    ),
                    reply(content="CANARY-DONE"),
                ]
                if self.with_evidence
                else [reply(content="CANARY-DONE")]
            )
            model = hooks.model(ModelWithHttp(scripted))
            hooks.input(
                encode(
                    {
                        "question": request.spec.stimulus.question,
                        "subject_id": request.spec.stimulus.subject_id,
                        "target_id": TARGET_ID,
                        "evidence_context": {
                            "run_id": run_id,
                            "time_policies": [
                                {
                                    "id": "CANARY-SCOPE",
                                    "mode": "historical_window",
                                    "all_authorized_targets": True,
                                    "window": scope.window.as_json(),
                                }
                            ],
                        },
                    }
                )
            )
            hooks.clock(ctx.started_at)
            call = ModelCall(
                messages=({"role": "user", "content": request.spec.stimulus.question},),
                tools=None,
                json_mode=False,
                max_tokens=100,
                timeout_seconds=30,
            )
            first = model.complete(call)
            if self.with_evidence:
                observed = hooks.tools(executor).execute(
                    ToolRequest(
                        step_id="CANARY-STEP",
                        tool_index=0,
                        tool_name=METRICS_TOOL,
                        target_ref=TARGET_ID,
                        params={"expr": GOOD_EXPR},
                        window=scope.window.as_json(),
                    )
                )
                self.tool_outcomes.append(observed)
                assert observed.status == "ok", (observed.status, observed.reason)
                assert observed.evidence is not None, (observed.status, observed.reason)
                message = {
                    "role": "tool",
                    "tool_call_id": first.tool_calls[0]["id"],
                    "content": canonical(dict(observed.model_view)),
                }
                hooks.delivery(observed.model_view, message)
                model.complete(
                    replace(
                        call,
                        messages=call.messages
                        + (
                            {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": list(first.tool_calls),
                            },
                            message,
                        ),
                    )
                )
                result = replace(result, evidence_ids=(observed.evidence.evidence_id,))
            hooks.finish(clock.now() if self.with_evidence else b2.END)
            return result

        ref = None
        if not self.omit_recording:
            # Produce deterministic offline fixture bytes using B2's writer.
            # Only the private fixture metadata flag is changed afterward so
            # the driver must genuinely reload and validate a non-synthetic
            # package. No real adapter, PG, or live Run is used or claimed.
            ref = record(driver, destination=request.recording_dir, context=ctx)
            doc = b2.package_json(request.recording_dir)
            doc["context"]["synthetic"] = self.synthetic
            if self.recording_transform:
                self.recording_transform(doc)
            digest = b2.write_recording(request.recording_dir, doc)
            path = request.recording_dir / ref.path
            path.chmod(0o600)
            ref = replace(ref, sha256=digest, bytes=path.stat().st_size)
            self.recordings.append((path, ref))
        returned = InvestigationResult(
            product_run_id=run_id,
            outcome=result,
            report=None,
            recording_ref=ref,
            recording_error=self.recording_error,
            traces=(),
            submitted_at=self.h.clock.now(),
            started_at=self.h.clock.now(),
            ended_at=self.h.clock.now(),
            usage={"epoch": 7, "model_requests": 2 if self.with_evidence else 1},
            error=self.error,
        )
        if self.result_transform:
            returned = self.result_transform(returned)
        self.results.append(returned)
        return returned


class Harness:
    def __init__(self, root):
        from opspilot.evaluation import load_scenarios

        self.root = root
        scenarios_root = root / "scenarios"
        shutil.copytree(
            Path(__file__).resolve().parents[2] / "evals/scenarios", scenarios_root
        )
        self.scenarios = load_scenarios(scenarios_root)
        self.clock = Clock()
        self.flags = Flags(self)
        self.flagd = Resolver(self.flags)
        self.telemetry = Telemetry(self)
        self.identity = Identity()
        self.ports = Ports()
        self.pg = Pg()
        self.investigator = Investigator(self)
        self.env = Env()
        self.provenance = ProvenanceReader()
        self.deps = lab.LiveDeps(
            clock=self.clock,
            flags=self.flags,
            flagd=self.flagd,
            telemetry=self.telemetry,
            identity=self.identity,
            ports=self.ports,
            pg=self.pg,
            balance=Balance(),
            investigator=self.investigator,
            ledger_root=root / "tmp/lab-ledgers",
            evidence_root=root / "public",
            recording_root=root / "tmp/eval",
            pg_root=root / "tmp/eval-live",
            scenarios_root=scenarios_root,
            flagd_url="http://127.0.0.1:8013",
            env=self.env,
            provenance=self.provenance,
        )
        self.deps.ledger_root.mkdir(parents=True)
        self.reservation = self.deps.ledger_root / "occupation.json"
        self.reservation.write_bytes(
            encode(
                {
                    "holder": HOLDER,
                    "batch": BATCH,
                    "expected_end": (self.clock.now() + timedelta(hours=6)).isoformat(),
                    "claimed_at": self.clock.now().isoformat(),
                }
            )
        )

    def argv(self, scenario=FAULT, n=1):
        return [
            "--mode",
            "live",
            "--round",
            ROUND,
            "--scenario",
            scenario,
            "--n",
            str(n),
            "--parallel",
            "1",
            "--holder",
            HOLDER,
            "--batch",
            BATCH,
            "--flagd-url",
            self.deps.flagd_url,
            "--pg-port",
            "55497",
        ]

    def final(self, capsys, *, argv=None):
        from scripts.eval_live import main

        try:
            code = main(self.argv() if argv is None else argv, deps=self.deps)
        except SystemExit as exc:
            code = exc.code
        output = capsys.readouterr().out
        final = json.loads(output.strip().splitlines()[-1])
        assert final["round"] == ROUND
        assert final["exit_code"] == code
        assert {"status", "reason", "slots"} <= final.keys()
        assert all(slot["status"] != "unknown" for slot in final["slots"])
        for summary in self.summaries():
            assert summary["capture"]["attempt"]["status"] != "unknown"
        lifecycle = self.public() / "lifecycle.json"
        if lifecycle.exists():
            assert all(
                slot["status"] != "unknown"
                for slot in json.loads(lifecycle.read_bytes())["slots"]
            )
        return code, final

    def public(self):
        return self.deps.evidence_root / ROUND

    def summaries(self):
        return [
            json.loads(p.read_bytes())
            for p in sorted(self.public().glob("runs/*/summary.json"))
        ]

    def signature(self):
        return next(
            answer.signature
            for spec, answer in self.scenarios
            if spec.scenario_id == FAULT
        )
