"""Offline bridge over trusted Holmes tool records and safe business captures.

Never opens private-protocol or provider responses. No network or model calls.
"""

import argparse
import ast
import copy
import hashlib
import inspect
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .outcomes_v3 import (
    AccessScope,
    Action,
    AgentInput,
    Artifact,
    ComposeTarget,
    Delivery,
    IncidentOutcome,
    IncidentScenario,
    IntegrationTarget,
    ModelReport,
    ProjectionContext,
    ProjectionDependency,
    Subject,
    TrustedFacts,
    Window,
    check_outcome,
    project,
)

SOURCE = Path(
    "/Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment/scripts/m0_environment/holmes_baseline.py"
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def canonical_hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


DEPENDENCY_FUNCTIONS = {
    "legacy_projections": {
        "_bind_identity_v2",
        "log_projection_v2",
        "trace_projection_v2",
        "metric_projection_v1",
    },
    "trace_view": {"details", "clipped_detail", "trace_projection_v3"},
}


def load_dependencies(context, namespace):
    """Only explicit trusted dependency snapshots; no imports or paths from raw."""
    deps = {d.module: d for d in context.dependencies}
    if len(deps) != len(context.dependencies):
        raise ValueError("PROJECTION_DEPENDENCY_DUPLICATE")
    for name in ("legacy_projections", "trace_view"):
        if name not in deps:
            continue
        dependency = deps[name]
        path = Path(dependency.source_path)
        if path.is_symlink() or path.suffix not in {".py", ".txt"}:
            raise ValueError("PROJECTION_DEPENDENCY_INVALID")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != dependency.source_sha256:
            raise ValueError("PROJECTION_DEPENDENCY_HASH_MISMATCH")
        tree = ast.parse(raw)
        functions = [
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in DEPENDENCY_FUNCTIONS[name]
        ]
        required = (
            {"_bind_identity_v2", "log_projection_v2"}
            if name == "legacy_projections"
            else DEPENDENCY_FUNCTIONS[name]
        )
        if not required <= {n.name for n in functions} or len(
            {n.name for n in functions}
        ) != len(functions):
            raise ValueError("PROJECTION_DEPENDENCY_INVALID")
        if name == "trace_view":
            constants = {
                n.targets[0].id: ast.literal_eval(n.value)
                for n in tree.body
                if isinstance(n, ast.Assign)
                and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id in {"VERSION", "DETAIL_KEYS"}
            }
            if set(constants) != {"VERSION", "DETAIL_KEYS"}:
                raise ValueError("PROJECTION_DEPENDENCY_INVALID")
            namespace.update(constants)
        exec(
            compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"),
            namespace,
        )


def dependencies_from_manifest(path):
    """The operator selects a trusted manifest; this is never a telemetry field."""
    manifest = json.loads(Path(path).read_bytes())
    root = Path(__file__).resolve().parents[2]
    result = []
    for entry in manifest["sources"]:
        for module in DEPENDENCY_FUNCTIONS:
            if entry["source"] == f"scripts/m0_environment/{module}.py":
                result.append(
                    ProjectionDependency(
                        module=module,
                        source_path=str(root / entry["snapshot"]),
                        source_sha256=entry["sha256"],
                    )
                )
    return result


def replay_projection(record, context, *, revision=None):
    """Execute only the exact frozen pure projection functions, not harness main."""
    source_path = Path(context.source_path) if context.source_path else SOURCE
    if source_path.is_symlink() or source_path.suffix not in {".py", ".txt"}:
        raise ValueError("PROJECTION_SOURCE_INVALID")
    source = source_path.read_bytes()
    if (
        hashlib.sha256(source).hexdigest() != context.source_sha256
        or canonical_hash(context.registry) != context.registry_hash
    ):
        raise ValueError("PROJECTION_CONTEXT_MISMATCH")
    required = {"bind_identity", "trace_projection", "log_projection"}
    names = required | {"metric_projection"}
    nodes = [
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    if not required <= {n.name for n in nodes}:
        raise ValueError("PROJECTION_SOURCE_INVALID")
    namespace = {
        "hashlib": hashlib,
        "json": json,
        "copy": copy,
        "canonical_hash": canonical_hash,
    }
    load_dependencies(context, namespace)
    exec(
        compile(ast.Module(body=nodes, type_ignores=[]), str(source_path), "exec"),
        namespace,
    )
    if "error" in record:
        view = {
            k: record[k]
            for k in (
                "evidence_id",
                "tool",
                "query",
                "observed_at",
                "http_status",
                "error",
            )
            if k in record
        }
        view["data_withheld"] = "data" in record or "raw_response_base64" in record
        return view
    tool = record["tool"]

    def invoke(name, *args):
        function = namespace[name]
        kwargs = (
            {"version": revision}
            if revision is not None
            and "version" in inspect.signature(function).parameters
            else {}
        )
        try:
            return function(*args, **kwargs)
        except NameError:
            raise ValueError("PROJECTION_DEPENDENCY_MISSING") from None

    if record.get("http_status") == 200 and tool in {"otel_traces", "otel_logs"}:
        view = invoke(
            "trace_projection" if tool == "otel_traces" else "log_projection",
            record,
            context.registry,
        )
        actual_revision = view.get("projection_version") or view.get("data", {}).get(
            "projection_version", "m0-02-v2"
        )
        if revision is not None and actual_revision != revision:
            raise ValueError("PROJECTION_VERSION_MISMATCH")
        return view
    if tool == "otel_metrics" and revision in {"m0-02-metrics-v1", "m0-02-metrics-v2"}:
        if "metric_projection" not in namespace:
            raise ValueError("PROJECTION_VERSION_UNSUPPORTED")
        view = invoke("metric_projection", record)
        if view.get("projection_version") != revision:
            raise ValueError("PROJECTION_VERSION_MISMATCH")
        return view
    if tool == "otel_services" and record.get("http_status") == 200:
        return {
            **record,
            "data": {
                **record["data"],
                "services": record["actual_sources"]["services"],
            },
        }
    return record


def extract_registered_views(messages, registered, *, initial_view_ids=()):
    """Parse exactly the fixed tool envelope; telemetry is never searched for IDs."""
    if not isinstance(messages, list):
        raise ValueError("BUSINESS_PROJECTION_INVALID")
    decoder = json.JSONDecoder()
    found, call_ids = {}, set()
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"user", "tool"}:
            raise ValueError("BUSINESS_PROJECTION_INVALID")
        if message["role"] == "user":
            if set(message) != {"role", "content"}:
                raise ValueError("BUSINESS_PROJECTION_INVALID")
            if initial_view_ids:
                try:
                    document = json.loads(message["content"])
                except (ValueError, TypeError):
                    document = None
                candidates = (
                    document.get("business_tool_views", [])
                    if isinstance(document, dict)
                    else []
                )
                if isinstance(candidates, list):
                    for view in candidates:
                        evidence_id = (
                            view.get("evidence_id") if isinstance(view, dict) else None
                        )
                        if (
                            isinstance(evidence_id, str)
                            and evidence_id in initial_view_ids
                        ):
                            if (
                                evidence_id in found
                                or registered.get(evidence_id) != view
                            ):
                                raise ValueError("INITIAL_USER_VIEW_MISMATCH")
                            found[evidence_id] = view
            continue
        if (
            set(message) != {"role", "content", "tool_call_id"}
            or message["tool_call_id"] in call_ids
        ):
            raise ValueError("AMBIGUOUS_TOOL_MESSAGE")
        call_ids.add(message["tool_call_id"])
        content = message["content"]
        prefix = "tool_call_metadata="
        if not isinstance(content, str) or not content.startswith(prefix):
            raise ValueError("TOOL_MESSAGE_FORMAT")
        metadata, end = decoder.raw_decode(content, len(prefix))
        rest = content[end:].lstrip()
        # Pinned Holmes format_tool_result_data uses this exact ERROR prefix.
        # Do not scan for a later JSON object or consume arbitrary error prose.
        error_prefix = "Tool execution failed:\n\n"
        failed_message = rest.startswith(error_prefix)
        if failed_message:
            rest = rest[len(error_prefix) :]
        view, consumed = decoder.raw_decode(rest)
        if rest[consumed:].strip() or set(metadata) != {"tool_name", "tool_call_id"}:
            raise ValueError("TOOL_MESSAGE_FORMAT")
        if (
            not isinstance(view, dict)
            or metadata["tool_call_id"] != message["tool_call_id"]
            or metadata["tool_name"] != view.get("tool")
        ):
            raise ValueError("TOOL_MESSAGE_BINDING")
        evidence_id = view.get("evidence_id")
        if (
            evidence_id not in registered
            or registered[evidence_id] != view
            or evidence_id in found
        ):
            raise ValueError("UNREGISTERED_OR_AMBIGUOUS_VIEW")
        registered_failure = bool(view.get("error")) or (
            type(view.get("http_status")) is int and view["http_status"] != 200
        )
        if failed_message != registered_failure:
            raise ValueError("TOOL_MESSAGE_STATUS_MISMATCH")
        found[evidence_id] = view
    return found


def _initial_records(run_dir, supplied, context, scope, report_only):
    """Only verify trusted imported copies; absent provenance remains an audit."""
    from scripts.m0_environment.initial_evidence import (
        context_signature,
        verify_initial_entry,
    )

    def unknown(reason):
        values = supplied if isinstance(supplied, list) else [supplied]
        if not values:
            values = [{"initial_manifest": "unusable"}]
        return [
            {
                "location": f"business_tool_views[{index}]",
                "content": canonical(value),
                "content_sha256": hashlib.sha256(canonical(value).encode()).hexdigest(),
                "reason": reason,
            }
            for index, value in enumerate(values)
        ]

    path = run_dir / "initial-evidence.json"
    if not path.exists():
        return [], context, unknown("INITIAL_PROVENANCE_MISSING") if supplied else []
    try:
        bundle = json.loads(path.read_bytes())
        if bundle.get("schema_version") != "m0-initial-evidence-v1":
            raise ValueError("INITIAL_PROVENANCE_INVALID")
        producer = ProjectionContext.model_validate_json(
            json.dumps(bundle["projection_context"])
        )
        original = ProjectionContext.model_validate_json(
            json.dumps(bundle["original_projection_context"])
        )
        if (
            context_signature(producer) != context_signature(original)
            or producer.registry_hash != context.registry_hash
        ):
            return [], context, unknown("INITIAL_CONTEXT_INCOMPATIBLE")
        if not report_only and context_signature(producer) != context_signature(
            context
        ):
            return [], context, unknown("INITIAL_MIXED_CONTEXT_UNSUPPORTED")
        if not isinstance(supplied, list) or any(
            not isinstance(v, dict) for v in supplied
        ):
            return [], context, unknown("INITIAL_VIEW_LIST_INVALID")
        ids = [v.get("evidence_id") for v in supplied]
        if any(
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", key)
            for key in ids
        ) or len(set(ids)) != len(ids):
            return [], context, unknown("INITIAL_EVIDENCE_ID_INVALID_OR_DUPLICATE")
        loaded = []
        for entry in bundle["entries"]:
            value = verify_initial_entry(entry, producer, scope, base_dir=run_dir)
            value["evidence_id"] = entry["evidence_id"]
            loaded.append(value)
        if (
            len({x["evidence_id"] for x in loaded}) != len(loaded)
            or len(loaded) != len(supplied)
            or any(x["view"] != supplied[ids.index(x["evidence_id"])] for x in loaded)
        ):
            return [], context, unknown("INITIAL_USER_VIEW_MISMATCH")
        return loaded, producer if report_only else context, []
    except (ValueError, KeyError, TypeError, OSError):
        return [], context, unknown("INITIAL_PROVENANCE_INVALID")


def compose_target(row, registry, registry_hash):
    labels = row["labels"]
    return ComposeTarget(
        kind="compose",
        integration_id=registry["integration_id"],
        deployment_instance=registry_hash,
        service=labels["com.docker.compose.service"],
        container_id=row["container_id"],
        image_digest=row["image_id"],
        telemetry_instance=row["hostname"],
        mapping_revision=registry_hash,
        config_revision=labels["com.docker.compose.config-hash"],
    )


def integration_target(registry, registry_hash, services):
    return IntegrationTarget(
        kind="integration",
        integration_id=registry["integration_id"],
        deployment_instance=registry_hash,
        mapping_revision=registry_hash,
        service_identity="unknown",
        observed_services=sorted(set(services)),
    )


def observed_targets(view, registry, registry_hash):
    services = view.get("actual_sources", {}).get("services", [])
    data = view.get("data", {})
    identities = data.get("source_identity_table", []) if isinstance(data, dict) else []
    if view.get("tool") == "otel_logs":
        identities = [i.get("container_binding", {}) for i in identities]
    targets = []
    unknown_services = set(services)
    for binding in identities:
        if binding.get("container_mapping") != "matched":
            continue
        rows = [
            r
            for r in registry["containers"]
            if r["container_id"] == binding.get("container_id")
            and r["image_id"] == binding.get("image_id")
            and r.get("labels", {}).get("com.docker.compose.config-hash")
            == binding.get("config_hash")
        ]
        if len(rows) != 1:
            raise ValueError("IDENTITY_BINDING_MISMATCH")
        target = compose_target(rows[0], registry, registry_hash)
        if target not in targets:
            targets.append(target)
        unknown_services.discard(target.service)
    if unknown_services or not targets:
        targets.append(integration_target(registry, registry_hash, unknown_services))
    return targets


def _input_scope(run_dir, scope, registry, registry_hash):
    run_id = run_dir.name
    initial = json.loads((run_dir / "input-business.json").read_bytes())
    users = [json.loads(m["content"]) for m in initial if m.get("role") == "user"]
    if len(users) != 1 or users[0]["run_id"] != run_id:
        raise ValueError("INITIAL_REQUEST_BINDING")
    focus = users[0]["investigation_subject"]
    rows = [
        r for r in registry["containers"] if r["container_id"] == focus["container_id"]
    ]
    if len(rows) != 1:
        raise ValueError("SUBJECT_IDENTITY_UNKNOWN")
    target = compose_target(rows[0], registry, registry_hash)
    if (target.service, target.image_digest, target.config_revision) != (
        focus["service"],
        focus["image_id"],
        focus["config_revision"],
    ):
        raise ValueError("SUBJECT_IDENTITY_MISMATCH")
    subject = Subject(id=run_id, kind="incident", target=target)
    window = Window(
        start=datetime.fromtimestamp(scope["window"]["start"], timezone.utc),
        end=datetime.fromtimestamp(scope["window"]["end"], timezone.utc),
    )
    allowed_targets = [
        compose_target(r, registry, registry_hash)
        for r in registry["containers"]
        if r.get("labels", {}).get("com.docker.compose.service") in scope["services"]
    ]
    allowed_targets.append(integration_target(registry, registry_hash, []))
    access = AccessScope(
        revision=scope["policy_revision"],
        targets=allowed_targets,
        interfaces=scope.get(
            "interfaces", ["otel_services", "otel_metrics", "otel_logs", "otel_traces"]
        ),
        window=window,
        services=scope["services"],
    )
    return initial, users, subject, access


def _load_common(
    run_dir,
    *,
    projection_source_sha256,
    projection_source_path=None,
    projection_dependencies=(),
    final_generation=0,
    controls=(),
    report_type=ModelReport,
):
    """Build from one actual final closed report; quality remains independent."""
    run_dir = Path(run_dir)
    run_id = run_dir.name
    if not re.fullmatch(r"[A-Za-z0-9-]+", run_id):
        raise ValueError("RUN_ID_INVALID")

    def read(name):
        return json.loads((run_dir / name).read_bytes())

    config = read("configuration.json")
    scope = config["trusted_access_scope"]
    registry = read("deployment-registry.json")
    registry_hash = canonical_hash(registry)
    if (
        scope["deployment_registry_sha256"] != registry_hash
        or registry["integration_id"] != scope["integration_id"]
    ):
        raise ValueError("REGISTRY_MISMATCH")
    context = ProjectionContext(
        registry=registry,
        registry_hash=registry_hash,
        source_sha256=projection_source_sha256,
        source_path=str(projection_source_path) if projection_source_path else None,
        dependencies=list(projection_dependencies),
    )
    strict_mode = report_type is not ModelReport
    deliveries = (
        read("delivered-business.json")
        if (run_dir / "delivered-business.json").exists()
        else []
    )
    results = read("result-business.json")
    if results["run_id"] != run_id:
        raise ValueError("REPORT_REQUEST_BINDING")
    report = None
    report_error = None
    candidate_content = results.get("final_business_content")
    candidate_from_response = False
    try:
        if results.get("finish_reason") != "stop":
            raise ValueError("FINAL_RESPONSE_REQUIRED")
        if strict_mode:
            from scripts.m0_environment.report_contract import parse_report

            report = report_type.model_validate(
                parse_report(
                    results.get("final_business_content"), version="m0-report-v2"
                )
            )
        else:
            report = report_type.model_validate_json(results["final_business_content"])
    except (ValueError, TypeError):
        if not strict_mode:
            raise ValueError("FINAL_REPORT_SCHEMA_INVALID") from None
        report_error = (
            "FINAL_RESPONSE_INCOMPLETE"
            if results.get("finish_reason") != "stop"
            else "FINAL_REPORT_SCHEMA_INVALID"
        )
    if len({d["request_ordinal"] for d in deliveries}) != len(deliveries):
        raise ValueError("DUPLICATE_PHYSICAL_REQUEST")
    deliveries = sorted(deliveries, key=lambda d: d["request_ordinal"])
    delivered = deliveries[-1] if deliveries else None
    final_ordinal = delivered["request_ordinal"] if delivered else None
    response_records = {}
    delivery_states = {}
    for item in deliveries:
        ordinal = item["request_ordinal"]
        path = run_dir / f"response-{ordinal}-business.json"
        observed = read(path.name) if path.exists() else None
        response_records[ordinal] = observed
        if observed is not None and (
            observed.get("run_id"),
            observed.get("request_ordinal"),
        ) != (run_id, ordinal):
            raise ValueError("REPORT_REQUEST_BINDING")
        accepted = (
            item.get("state") == "response_received"
            and item.get("http_status") == 200
            and observed is not None
            and observed.get("response_complete") is True
            and observed.get("identity_accepted") is True
        )
        delivery_states[ordinal] = (
            "response_committed"
            if accepted
            else "dispatched"
            if item.get("dispatch_started_at")
            else "prepared"
        )
    response = response_records.get(final_ordinal)
    if delivered and delivery_states[final_ordinal] == "response_committed":
        if len(response.get("choices", [])) != 1:
            raise ValueError("REPORT_REQUEST_BINDING")
        choice = response["choices"][0]
        if (
            strict_mode
            and candidate_content is None
            and results.get("status") in {"failed", "incomplete"}
            and isinstance(choice.get("content"), str)
        ):
            # Runner failed before accepting an answer. Preserve the safe response
            # as an unaccepted candidate; never invent its missing ReportCapture.
            candidate_from_response = True
            candidate_content = choice["content"]
            from scripts.m0_environment.report_contract import parse_report

            try:
                if choice.get("finish_reason") != "stop":
                    raise ValueError("FINAL_RESPONSE_INCOMPLETE")
                report = report_type.model_validate(
                    parse_report(candidate_content, version="m0-report-v2")
                )
                report_error = None
            except (ValueError, TypeError):
                report = None
                report_error = "FINAL_REPORT_SCHEMA_INVALID"
        elif (
            choice.get("finish_reason") != results.get("finish_reason")
            or choice.get("content") != candidate_content
        ):
            raise ValueError("REPORT_REQUEST_BINDING")
    else:
        if not strict_mode:
            raise ValueError("FINAL_RESPONSE_REQUIRED")
        report = None
        report_error = "FINAL_RESPONSE_INCOMPLETE"
    initial, users, subject, access = _input_scope(
        run_dir, scope, registry, registry_hash
    )
    report_only = (
        config.get("phase") == "report"
        and type(config.get("max_steps")) is int
        and config["max_steps"] == 1
    )
    observations_path = run_dir / "observations.json"
    if observations_path.exists():
        observations = read("observations.json")
    elif report_only:
        observations = []
    else:
        raise ValueError("DYNAMIC_OBSERVATIONS_UNKNOWN")
    if report_only and observations:
        raise ValueError("REPORT_ONLY_DYNAMIC_OPERATIONS")
    supplied = users[0].get("business_tool_views", [])
    imported, context, unverified = _initial_records(
        run_dir, supplied, context, scope, report_only
    )
    artifacts, views, registered, actions = [], {}, {}, []
    initial_ids = set()
    for item in imported:
        raw, view = item["raw"], item["view"]
        evidence_id = item["evidence_id"]
        try:
            bounds = item["query_window"]
            original_window = Window(
                start=datetime.fromtimestamp(bounds["start"], timezone.utc),
                end=datetime.fromtimestamp(bounds["end"], timezone.utc),
            )
            artifact = Artifact(
                id=evidence_id,
                interface=raw["tool"],
                targets=observed_targets(view, context.registry, context.registry_hash),
                query=canonical(raw["query"]),
                window=original_window,
                captured_at=datetime.fromisoformat(raw["observed_at"]),
                status="ok"
                if raw.get("http_status") == 200 and not raw.get("error")
                else "failed",
                raw=item["raw_bytes"].decode(),
                raw_hash=hashlib.sha256(item["raw_bytes"]).hexdigest(),
                projection_revision=item["projection_revision"],
            )
        except (ValueError, KeyError, TypeError, OverflowError):
            content = canonical(view)
            unverified.append(
                {
                    "location": f"initial-evidence[{evidence_id}]",
                    "content": content,
                    "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                    "reason": "INITIAL_METADATA_UNKNOWN",
                }
            )
            continue
        artifacts.append(artifact)
        views[evidence_id] = project(artifact, [], evidence_id, context=context)
        registered[evidence_id] = view
        initial_ids.add(evidence_id)
    for operation in observations:
        evidence_id = operation["evidence_id"]
        if (
            not re.fullmatch(re.escape(run_id) + r"-e[1-9][0-9]*", evidence_id)
            or evidence_id in registered
        ):
            raise ValueError("AMBIGUOUS_OPERATION")
        raw_bytes = (run_dir / f"{evidence_id}-raw.json").read_bytes()
        view_bytes = (run_dir / f"{evidence_id}-tool-model-view.json").read_bytes()
        raw, view = json.loads(raw_bytes), json.loads(view_bytes)
        manifest = read(f"{evidence_id}-manifest.json")
        if (
            raw != operation
            or manifest["evidence_id"] != evidence_id
            or manifest["scope"] != scope
            or manifest["raw_sha256"] != canonical_hash(raw)
            or manifest["view_sha256"] != canonical_hash(view)
            or manifest["raw_file_sha256"] != hashlib.sha256(raw_bytes).hexdigest()
            or manifest["view_file_sha256"] != hashlib.sha256(view_bytes).hexdigest()
        ):
            raise ValueError("ARTIFACT_HASH_OR_OPERATION_MISMATCH")
        revision = manifest.get("projection_version", "m0-02-v2")
        if replay_projection(raw, context, revision=revision) != view:
            raise ValueError("PROJECTION_MISMATCH")
        targets = observed_targets(view, registry, registry_hash)
        ok = raw.get("http_status") == 200 and not raw.get("error")
        artifact = Artifact(
            id=evidence_id,
            interface=raw["tool"],
            targets=targets,
            query=canonical(raw["query"]),
            window=access.window,
            captured_at=datetime.fromisoformat(raw["observed_at"]),
            status="ok" if ok else "failed",
            raw=raw_bytes.decode(),
            raw_hash=hashlib.sha256(raw_bytes).hexdigest(),
            projection_revision=revision,
        )
        artifacts.append(artifact)
        views[evidence_id] = project(artifact, [], evidence_id, context=context)
        registered[evidence_id] = view
        query_service = raw.get("query", {}).get("service")
        actions.append(
            Action(
                kind="query",
                target=integration_target(
                    registry,
                    registry_hash,
                    [query_service] if isinstance(query_service, str) else [],
                ),
                interface=raw["tool"],
                attempted=True if type(raw.get("http_status")) is int else None,
                authorized=None,
                executed=None,
                evidence_id=evidence_id,
                audit_basis="Proxy response observed; backend execution and permission decision are not independently captured."
                if type(raw.get("http_status")) is int
                else "Tool operation recorded; actual dispatch, backend execution and permission decision remain unknown.",
            )
        )
    base_deliveries = []
    actual_views = {}
    request_id, step_id = "not_started", "not_started"
    for record in deliveries if strict_mode else [delivered]:
        if (
            record["trusted_access_scope"] != scope
            or canonical_hash(record["messages"]) != record["business_messages_sha256"]
        ):
            raise ValueError("BUSINESS_PROJECTION_MISMATCH")
        found = extract_registered_views(
            record["messages"], registered, initial_view_ids=initial_ids
        )
        if record is delivered:
            actual_views = found
        ordinal = record["request_ordinal"]
        request_id = f"{run_id}:http:{ordinal}"
        step_id = f"{run_id}:report-step:{ordinal}"
        base_deliveries.append(
            Delivery(
                run_id=run_id,
                step_id=step_id,
                request_id=request_id,
                control_generation=final_generation,
                business_projection="holmes-user-tool-v1",
                business_projection_hash=record["business_messages_sha256"],
                business_projection_content=canonical(record["messages"]),
                full_wire_hash=record["actual_request_sha256"],
                views=[views[e] for e in found],
                state=delivery_states[ordinal],
            )
        )
    if (
        not strict_mode
        and not unverified
        and any(
            ref not in actual_views
            for claim in report.claims
            for ref in claim.evidence_ids
        )
    ):
        raise ValueError("REPORT_EVIDENCE_NOT_DELIVERED")
    execution = "completed" if report is not None else "blocked"
    runtime_reasons = []
    if strict_mode:
        # Successful parsing is only a candidate. The runner owns execution status.
        status = results.get("status")
        if status == "failed":
            execution = "failed"
            runtime_reasons.append("RUNTIME_EXECUTION_FAILED")
        elif status == "incomplete":
            execution = "blocked"
            runtime_reasons.append("RUNTIME_EXECUTION_INCOMPLETE")
        elif status != "investigation_returned":
            execution = "blocked"
            runtime_reasons.append("RUNTIME_STATUS_UNKNOWN")
        for present, code in (
            ("report_validation_error" in results, "RUNTIME_REPORT_VALIDATION_FAILED"),
            ("error_type" in results, "RUNTIME_EXCEPTION_RECORDED"),
            (
                bool(
                    results.get("boundary_errors")
                    or results.get("boundary_error_codes")
                ),
                "RUNTIME_BOUNDARY_FAILURE",
            ),
        ):
            if present:
                if execution == "completed":
                    execution = "blocked"
                runtime_reasons.append(code)
    if candidate_from_response:
        runtime_reasons.extend(
            [
                "UNACCEPTED_CANDIDATE_FROM_RESPONSE",
                "RUNNER_FINAL_CONTENT_NULL"
                if "final_business_content" in results
                else "RUNNER_FINAL_CONTENT_MISSING",
            ]
        )
    versions = {
        "adapter": "holmes-v3-bridge-1",
        "projection_code": context.source_sha256,
        "registry": registry_hash,
        "model": config["model"],
        "prompt": config["prompt_sha256"],
        "projection_dependencies": canonical_hash(
            [
                {"module": d.module, "source_sha256": d.source_sha256}
                for d in sorted(context.dependencies, key=lambda d: d.module)
            ]
        ),
    }
    if isinstance(config.get("upstream_commit"), str):
        versions["upstream_commit"] = config["upstream_commit"]
    if isinstance(config.get("tool_schema"), list):
        versions["tool_schema_sha256"] = canonical_hash(config["tool_schema"])
    scenario = IncidentScenario(
        schema_version="m0-public-v3",
        scenario_id=run_id,
        versions=versions,
        agent_input=AgentInput(
            subject=subject,
            request=users[0]["request"],
            initial_views=[views[key] for key in views if key in initial_ids],
        ),
        trusted=TrustedFacts(
            scope=access,
            artifacts=artifacts,
            deliveries=base_deliveries,
            controls=list(controls),
            final_generation=final_generation,
            current_run=run_id,
            execution=execution,
            observed_actions=actions,
            projection_context=context,
        ),
    )
    outcome = IncidentOutcome(
        schema_version="m0-public-v3",
        scenario_id=run_id,
        versions=versions,
        subject=subject,
        run_id=run_id,
        report_step_id=step_id,
        report_request_id=request_id,
        control_generation=final_generation,
        execution=execution,
        assessment_status=report.assessment_status if report else "incomplete",
        conclusion=report.conclusion if report else "inconclusive",
        claims=[
            {"kind": claim.kind, "text": claim.text, "evidence_ids": claim.evidence_ids}
            for claim in (report.claims if report else [])
        ],
        evidence_ids=list(actual_views),
        gaps=(report.gaps if report else [report_error]) + runtime_reasons,
        handoff=execution != "completed"
        or report is None
        or report.assessment_status == "incomplete",
        health="unknown",
    )
    input_details = {
        "actual_user_content": next(
            m["content"] for m in initial if m["role"] == "user"
        ),
        "unverified_initial_views": unverified,
        "first_context": deliveries[0].get("evidence_context") if deliveries else None,
        "delivery_records": deliveries,
        "report_error": report_error,
        "candidate_content": candidate_content,
        "candidate_from_response": candidate_from_response,
    }
    return (
        scenario,
        outcome,
        report,
        config,
        delivered,
        response,
        results,
        input_details,
    )


def load_legacy_packet(run_dir, **kwargs):
    """Explicit historical v3 structure only; never the current strict contract."""
    return _load_common(run_dir, **kwargs)[:2]


def legacy_report_audit(run_dir):
    """Preserve the whole safe historical report without inventing v2 claims."""
    result = json.loads((Path(run_dir) / "result-business.json").read_bytes())
    content = result.get("final_business_content")
    parsed = None
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except ValueError:
            pass
    return {
        "original_report_content": content,
        "original_report_sha256": hashlib.sha256(content.encode()).hexdigest()
        if isinstance(content, str)
        else None,
        "original_report": parsed,
        "strict_scope": "unknown",
        "strict_freshness": "unknown",
        "current_acceptance_pass": False,
    }


def load_packet(run_dir, **kwargs):
    """Current strict v4 entry. Old payloads require explicit legacy replay."""
    from . import outcomes_v4 as strict

    run_dir = Path(run_dir)

    def read(name):
        return json.loads((run_dir / name).read_bytes())

    config = read("configuration.json")
    if config.get("report_schema_version") != "m0-report-v2":
        raise ValueError("LEGACY_REPORT_REQUIRES_EXPLICIT_REPLAY")
    generation = config["trusted_access_scope"].get("control_generation")
    if type(generation) is not int or generation < 0:
        raise ValueError("STRICT_CONTROL_UNKNOWN")
    kwargs["final_generation"] = generation
    base, old_outcome, report, config, record, response, result, input_details = (
        _load_common(run_dir, report_type=strict.ModelReportV2, **kwargs)
    )
    policies_raw = read("time-policies.json")
    if policies_raw != config.get("time_policies") or canonical_hash(
        policies_raw
    ) != config.get("time_policies_sha256"):
        raise ValueError("TIME_POLICY_FILE_MISMATCH")
    policies = [
        strict.TimePolicy.model_validate_json(json.dumps(p)) for p in policies_raw
    ]
    strict_deliveries = []
    for base_delivery, item in zip(
        base.trusted.deliveries, input_details["delivery_records"], strict=True
    ):
        context_raw = item.get("evidence_context")
        if canonical_hash(context_raw) != item.get("evidence_context_sha256"):
            raise ValueError("CONTEXT_CAPTURE_MISMATCH")
        context = strict.EvidenceContext.model_validate_json(json.dumps(context_raw))
        if item.get("control_generation") != generation:
            raise ValueError("CONTROL_CAPTURE_MISMATCH")
        ordinal = item["request_ordinal"]
        strict_deliveries.append(
            strict.Delivery.model_validate_json(
                json.dumps(
                    base_delivery.model_dump(mode="json")
                    | {
                        "request_id": item["request_id"],
                        "step_id": f"{run_dir.name}:report-step:{ordinal}",
                        "context": context.model_dump(mode="json"),
                        "final_phase": item.get("final_phase")
                        if type(item.get("final_phase")) is bool
                        else None,
                        "dispatch_started_at": item.get("dispatch_started_at"),
                        "response_received_at": item.get("response_received_at"),
                    }
                )
            )
        )
    expected_step = (
        f"{run_dir.name}:report-step:{record['request_ordinal']}" if record else None
    )
    capture_path = run_dir / "report-capture-business.json"
    capture = (
        strict.ReportCapture.model_validate_json(capture_path.read_bytes())
        if capture_path.exists()
        else None
    )
    if capture is not None:
        if record is None or (
            capture.run_id,
            capture.step_id,
            capture.request_id,
            capture.control_generation,
        ) != (run_dir.name, expected_step, record.get("request_id"), generation):
            raise ValueError("REPORT_OUTPUT_BINDING_MISMATCH")
        if (
            response is None
            or len(response.get("choices", [])) != 1
            or capture.content != result.get("final_business_content")
            or capture.content != response["choices"][0].get("content")
        ):
            raise ValueError("REPORT_OUTPUT_BINDING_MISMATCH")
    elif report is not None and not input_details["candidate_from_response"]:
        raise ValueError("REPORT_OUTPUT_BINDING_MISMATCH")
    if (
        report is not None
        and "final_report" in result
        and result["final_report"] != report.model_dump(mode="json")
    ):
        raise ValueError("REPORT_PARSED_OBJECT_MISMATCH")
    timing_records = {
        key: strict.TimingRecord.model_validate_json(json.dumps(value))
        for key, value in (
            read("evidence-timings.json")
            if (run_dir / "evidence-timings.json").exists()
            else {}
        ).items()
    }
    facts = strict.TrustedFacts.model_validate(
        base.trusted.model_dump(exclude={"deliveries"})
        | {
            "deliveries": strict_deliveries,
            "time_policies": policies,
            "report_capture": capture,
            "evaluation_at": capture.response_received_at if capture else None,
            "timing_records": timing_records,
        }
    )
    versions = base.versions | {
        "adapter": "holmes-v4-bridge-1",
        "report": "m0-report-v2",
        "temporal_policies": canonical_hash(policies_raw),
    }
    if isinstance(config.get("report_instruction_sha256"), str):
        versions["report_instruction_sha256"] = config["report_instruction_sha256"]
    actual_content = input_details["actual_user_content"]
    original_path = run_dir / "question-original.txt"
    try:
        original_content = original_path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError):
        original_content = None
    try:
        provenance = read("input-provenance.json")
    except (OSError, ValueError):
        provenance = None
    unverified = list(input_details["unverified_initial_views"])
    if (run_dir / "initial-import-audit.json").exists():
        for entry in read("initial-import-audit.json").get("unverified", []):
            if entry not in unverified:
                unverified.append(entry)
    if (
        not isinstance(provenance, dict)
        or provenance.get("actual_user_content_sha256")
        != strict.content_hash(actual_content)
        or original_content is None
        or provenance.get("original_user_content_sha256")
        != strict.content_hash(original_content)
    ):
        unverified.append(
            {
                "location": "input-provenance",
                "content": actual_content,
                "content_sha256": strict.content_hash(actual_content),
                "reason": "INITIAL_INPUT_PROVENANCE_MISMATCH",
            }
        )
    initial_context = (
        input_details["first_context"] if base.agent_input.initial_views else None
    )
    agent_input = strict.AgentInput.model_validate_json(
        json.dumps(
            base.agent_input.model_dump(mode="json")
            | {
                "evidence_context": initial_context,
                "actual_user_content": actual_content,
                "actual_user_content_sha256": strict.content_hash(actual_content),
                "original_user_content": original_content,
                "original_user_content_sha256": strict.content_hash(original_content)
                if original_content is not None
                else None,
                "unverified_initial_views": unverified,
            }
        )
    )
    scenario = strict.IncidentScenario(
        schema_version="m0-public-v4",
        scenario_id=base.scenario_id,
        versions=versions,
        agent_input=agent_input,
        trusted=facts,
    )
    outcome = strict.IncidentOutcome(
        schema_version="m0-public-v4",
        scenario_id=old_outcome.scenario_id,
        versions=versions,
        subject=old_outcome.subject,
        run_id=old_outcome.run_id,
        control_generation=generation,
        execution=old_outcome.execution,
        report_step_id=expected_step,
        report_request_id=record["request_id"] if record else None,
        report=report,
        report_content=input_details["candidate_content"],
        report_content_sha256=strict.content_hash(input_details["candidate_content"])
        if isinstance(input_details["candidate_content"], str)
        else None,
        evidence_ids=old_outcome.evidence_ids,
        handoff=old_outcome.handoff,
        handoff_reasons=old_outcome.gaps if old_outcome.handoff else [],
        health="unknown",
    )
    return scenario, outcome


def main():
    parser = argparse.ArgumentParser(
        description="Offline safe-business Holmes -> current strict v4 contract check"
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--projection-source-sha256", required=True)
    parser.add_argument("--projection-source-file", type=Path)
    parser.add_argument("--projection-dependencies-manifest", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--legacy-v3",
        action="store_true",
        help="Explicit historical replay; never current strict acceptance",
    )
    args = parser.parse_args()
    try:
        loader = load_legacy_packet if args.legacy_v3 else load_packet
        scenario, outcome = loader(
            args.run_dir,
            projection_source_sha256=args.projection_source_sha256,
            projection_source_path=args.projection_source_file,
            projection_dependencies=dependencies_from_manifest(
                args.projection_dependencies_manifest
            )
            if args.projection_dependencies_manifest
            else (),
        )
        if args.legacy_v3:
            errors = check_outcome(scenario, outcome)
        else:
            from .outcomes_v4 import check_outcome as strict_check

            errors = strict_check(scenario, outcome)
        result = {
            "run_id": scenario.scenario_id,
            "contract_consistent": not errors,
            "violations": errors,
            "report_request_id": outcome.report_request_id,
            "initial_views": len(scenario.agent_input.initial_views),
            "captured_artifacts": len(scenario.trusted.artifacts),
            "delivered_views": len(outcome.evidence_ids),
            "assessment_status": outcome.assessment_status
            if args.legacy_v3
            else outcome.report.assessment_status
            if outcome.report is not None
            else None,
            "conclusion": outcome.conclusion
            if args.legacy_v3
            else outcome.report.conclusion
            if outcome.report is not None
            else None,
            "assurance_mode": "explicit-legacy-v3" if args.legacy_v3 else "strict-v4",
            "boundary": "Contract consistency only; report quality/causality require independent review. No private protocol read.",
        }
        if args.legacy_v3:
            result.update(legacy_report_audit(args.run_dir))
        elif errors or outcome.report is None or outcome.handoff:
            result.update(
                execution=outcome.execution,
                handoff=outcome.handoff,
                handoff_reasons=outcome.handoff_reasons,
                report=outcome.report.model_dump(mode="json")
                if outcome.report
                else None,
                report_content=outcome.report_content,
                report_content_sha256=outcome.report_content_sha256,
                agent_input=scenario.agent_input.model_dump(mode="json"),
                scenario=scenario.model_dump(mode="json"),
                outcome=outcome.model_dump(mode="json"),
            )
    except (ValueError, KeyError, OSError, TypeError) as exc:
        code = str(exc)
        if len(code) > 80 or not re.fullmatch(r"[A-Z][A-Z_]+", code):
            code = "INVALID_ARTIFACT"
        result = {
            "run_id": args.run_dir.name,
            "contract_consistent": False,
            "violations": [code],
        }
        if code == "LEGACY_REPORT_REQUIRES_EXPLICIT_REPLAY":
            result.update(legacy_report_audit(args.run_dir))
    if args.output:
        fd = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", closefd=False) as handle:
                json.dump(result, handle, indent=2)
                handle.write("\n")
        finally:
            os.close(fd)
    print(
        json.dumps(
            {
                key: value
                for key, value in result.items()
                if key
                not in {
                    "original_report_content",
                    "original_report",
                    "report_content",
                    "report",
                    "agent_input",
                    "scenario",
                    "outcome",
                }
            }
        )
    )
    return 0 if result["contract_consistent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
