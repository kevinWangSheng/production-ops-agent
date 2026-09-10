"""One trusted initial-evidence bundle, imported without relabeling raw or clocks."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

from scripts.m0.outcomes_v3 import ProjectionContext
from scripts.m0.outcomes_v4 import Timing, build_context
from scripts.m0_environment.round02 import canonical_hash, save


class InitialEvidenceError(ValueError):
    pass


def validate_timing_echo(supplied, view_hashes, verified_timings):
    """The legacy sidecar may confirm imported timing, never provide new clocks."""
    if not isinstance(supplied, dict):
        raise InitialEvidenceError("INITIAL_TIMING_ECHO_MISMATCH")
    for evidence_id, record in supplied.items():
        if (
            evidence_id not in verified_timings
            or evidence_id not in view_hashes
            or not isinstance(record, dict)
            or record.get("view_hash") != view_hashes[evidence_id]
        ):
            raise InitialEvidenceError("INITIAL_TIMING_ECHO_MISMATCH")
        proposed = Timing.model_validate_json(json.dumps(record.get("timing")))
        verified = Timing.model_validate_json(json.dumps(verified_timings[evidence_id]))
        if proposed != verified:
            raise InitialEvidenceError("INITIAL_TIMING_ECHO_MISMATCH")


def _bytes(path, expected=None):
    path = Path(path)
    resolved = path.resolve()
    if (
        path.is_symlink()
        or ".env" in resolved.parts
        or "private-protocol" in resolved.parts
    ):
        raise InitialEvidenceError("INITIAL_SOURCE_PATH_DENIED")
    if not path.exists():
        raise InitialEvidenceError("INITIAL_SOURCE_MISSING")
    if not path.is_file():
        raise InitialEvidenceError("INITIAL_SOURCE_PATH_DENIED")
    data = path.read_bytes()
    if expected is not None and (
        not re.fullmatch(r"[a-f0-9]{64}", str(expected))
        or hashlib.sha256(data).hexdigest() != expected
    ):
        raise InitialEvidenceError("INITIAL_SOURCE_HASH_MISMATCH")
    return data


def _copy(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise InitialEvidenceError("INITIAL_DESTINATION_EXISTS")
    with path.open("xb") as handle:
        path.chmod(0o600)
        handle.write(data)


def context_signature(context):
    if canonical_hash(context.registry) != context.registry_hash or len(
        {d.module for d in context.dependencies}
    ) != len(context.dependencies):
        raise InitialEvidenceError("INITIAL_CONTEXT_INCOMPATIBLE")
    return {
        "registry_hash": context.registry_hash,
        "source_sha256": context.source_sha256,
        "dependencies": sorted(
            (d.module, d.source_sha256) for d in context.dependencies
        ),
    }


TOOL_INTERFACES = frozenset(
    {"otel_services", "otel_metrics", "otel_logs", "otel_traces"}
)


def allowed_interfaces(scope):
    """Absent field means exactly the existing fixed read-only tool surface."""
    values = scope.get("interfaces", sorted(TOOL_INTERFACES))
    if not isinstance(values, list) or any(
        not isinstance(value, str) or value not in TOOL_INTERFACES for value in values
    ):
        raise InitialEvidenceError("INITIAL_INTERFACE_SCOPE_UNKNOWN")
    return frozenset(values)


def _window(value):
    if not isinstance(value, dict):
        raise InitialEvidenceError("INITIAL_QUERY_TIME_UNKNOWN")
    start, end = value.get("start"), value.get("end")
    if (
        any(
            type(item) not in (int, float) or not math.isfinite(item)
            for item in (start, end)
        )
        or not start < end
    ):
        raise InitialEvidenceError("INITIAL_QUERY_TIME_UNKNOWN")
    return {"start": start, "end": end}


def authorized_query_window(raw, origin, scope):
    """Disclosure gate, separate from whether a fact later meets a time policy."""
    tool = raw.get("tool")
    original_scope = origin["scope"]
    if (
        not isinstance(tool, str)
        or tool not in allowed_interfaces(scope)
        or tool not in allowed_interfaces(original_scope)
    ):
        raise InitialEvidenceError("INITIAL_INTERFACE_DENIED")
    query = raw.get("query")
    if tool == "otel_services":
        # This fixed inventory endpoint accepts no time filter. A scope window
        # cannot be relabeled as a query or event-time guarantee.
        raise InitialEvidenceError("INITIAL_QUERY_TIME_UNKNOWN")
    required = (
        {"start", "end", "query"}
        if tool == "otel_metrics"
        else {"start", "end", "service"}
    )
    if not isinstance(query, dict) or set(query) != required:
        raise InitialEvidenceError("INITIAL_QUERY_SHAPE_UNKNOWN")
    actual, prior, current = (
        _window(query),
        _window(original_scope.get("window")),
        _window(scope.get("window")),
    )
    if actual["start"] < prior["start"] or actual["end"] > prior["end"]:
        raise InitialEvidenceError("INITIAL_ORIGINAL_QUERY_SCOPE_MISMATCH")
    if actual["start"] < current["start"] or actual["end"] > current["end"]:
        raise InitialEvidenceError("INITIAL_QUERY_WINDOW_DENIED")
    if tool in {"otel_logs", "otel_traces"}:
        if query["service"] not in scope.get("services", []) or query[
            "service"
        ] not in original_scope.get("services", []):
            raise InitialEvidenceError("INITIAL_QUERY_SERVICE_DENIED")
    else:
        if (
            scope.get("metrics_scope") != "integration"
            or original_scope.get("metrics_scope") != "integration"
        ):
            raise InitialEvidenceError("INITIAL_INTERFACE_SCOPE_UNKNOWN")
        expression = query["query"]
        width = actual["end"] - actual["start"]
        if (
            not isinstance(expression, str)
            or len(expression) > 2000
            or width < 300
            or "@" in expression
            or re.search(r"\boffset\b", expression)
        ):
            raise InitialEvidenceError("INITIAL_METRIC_QUERY_TIME_UNKNOWN")
        units = {"s": 1, "m": 60, "h": 3600}
        for value in re.findall(r"\[([^]]+)\]", expression):
            if (
                not re.fullmatch(r"[1-9][0-9]*[smh]", value)
                or int(value[:-1]) * units[value[-1]] > width
            ):
                raise InitialEvidenceError("INITIAL_METRIC_QUERY_TIME_UNKNOWN")
    return actual


def verify_initial_entry(entry, context, scope, *, base_dir=None):
    """Verify one operator-selected original entry; never copy or invent provenance."""
    from scripts.m0.holmes_bridge import replay_projection
    from scripts.m0.projector_trust import authenticate_projection

    authenticate_projection(context)
    from scripts.m0_environment.report_contract import source_timing

    if canonical_hash(
        context.registry
    ) != context.registry_hash or context.registry_hash != scope.get(
        "deployment_registry_sha256"
    ):
        raise InitialEvidenceError("INITIAL_CONTEXT_INCOMPATIBLE")
    base = Path(base_dir or ".")

    def resolved(name):
        path = Path(name)
        return path if path.is_absolute() else base / path

    evidence_id = entry.get("evidence_id")
    if not isinstance(evidence_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]*", evidence_id
    ):
        raise InitialEvidenceError("INITIAL_EVIDENCE_ID_INVALID_OR_DUPLICATE")
    raw_bytes = _bytes(resolved(entry["raw_path"]), entry["raw_file_sha256"])
    view_bytes = _bytes(resolved(entry["view_path"]), entry["view_file_sha256"])
    origin_bytes = _bytes(
        resolved(entry["manifest_path"]), entry["manifest_file_sha256"]
    )
    raw, view, origin = (
        json.loads(value) for value in (raw_bytes, view_bytes, origin_bytes)
    )
    if not all(
        isinstance(value, dict) for value in (raw, view, origin)
    ) or not isinstance(origin.get("scope"), dict):
        raise InitialEvidenceError("INITIAL_RECORD_SHAPE_INVALID")
    revision = origin["projection_version"]
    if any(
        value.get("evidence_id") != evidence_id for value in (raw, view, origin)
    ) or raw.get("tool") != view.get("tool"):
        raise InitialEvidenceError("INITIAL_EVIDENCE_BINDING_MISMATCH")
    if (
        origin["raw_sha256"] != canonical_hash(raw)
        or origin["view_sha256"] != canonical_hash(view)
        or origin["raw_file_sha256"] != entry["raw_file_sha256"]
        or origin["view_file_sha256"] != entry["view_file_sha256"]
        or origin.get("query") != raw.get("query")
        or origin.get("scope") != raw.get("trusted_access_scope")
        or origin["scope"].get("deployment_registry_sha256") != context.registry_hash
        or entry.get("projection_revision", revision) != revision
    ):
        raise InitialEvidenceError("INITIAL_MANIFEST_BINDING_MISMATCH")
    query_window = authorized_query_window(raw, origin, scope)
    try:
        projected = replay_projection(raw, context, revision=revision)
    except (ValueError, KeyError, TypeError, AttributeError, OSError):
        raise InitialEvidenceError("INITIAL_PROJECTION_INVALID") from None
    if projected != view:
        raise InitialEvidenceError("INITIAL_PROJECTION_MISMATCH")
    # Scope binding is checked on the actual projected view, never raw targets.
    build_context(
        "initial-verification",
        {evidence_id: view},
        context.registry,
        [],
        {},
        allowed_scope=scope,
    )
    timing = Timing.model_validate_json(
        json.dumps(entry.get("timing", source_timing(raw, view)))
    ).model_dump(mode="json")
    from scripts.m0_environment.report_contract import source_timing

    supplied = Timing.model_validate_json(json.dumps(timing))
    visible = Timing.model_validate_json(json.dumps(source_timing({}, view)))
    if supplied.source_time_basis not in {"unknown", "event_time"}:
        raise InitialEvidenceError("INITIAL_SOURCE_TIME_PROOF_UNSUPPORTED")
    if supplied.source_time_basis == "event_time" and (
        visible.source_time_basis != "event_time"
        or supplied.source_start_at != visible.source_start_at
        or supplied.source_end_at != visible.source_end_at
    ):
        raise InitialEvidenceError("INITIAL_SOURCE_TIME_MISMATCH")
    if supplied.source_time_basis == "unknown" and (
        supplied.source_start_at is not None or supplied.source_end_at is not None
    ):
        raise InitialEvidenceError("INITIAL_SOURCE_TIME_MISMATCH")
    for field in ("operation_started_at", "collection_completed_at"):
        recorded = Timing.model_validate_json(json.dumps({field: raw.get(field)}))
        if getattr(recorded, field) != getattr(supplied, field):
            raise InitialEvidenceError("INITIAL_COLLECTION_TIME_MISMATCH")
    return {
        "raw_bytes": raw_bytes,
        "view_bytes": view_bytes,
        "manifest_bytes": origin_bytes,
        "raw": raw,
        "view": view,
        "projection_revision": revision,
        "query_window": query_window,
        "timing": timing,
    }


def import_initial_evidence(
    question_content,
    manifest_path,
    out,
    scope,
    *,
    run_id,
    report_only,
    runtime_source_sha256,
    runtime_dependencies=None,
):
    """Unknown inputs stay auditable; verified imports are never tool operations."""
    out = Path(out)
    original = question_content.encode("utf-8")
    _copy(out / "question-original.txt", original)
    unverified = []
    result = {
        "actual_question_content": question_content,
        "verified_views": {},
        "timings": {},
        "projection_context": None,
        "unverified": unverified,
    }

    def issue(location, value, reason):
        content = (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        )
        unverified.append(
            {
                "location": location,
                "content": content,
                "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                "reason": reason,
            }
        )

    document = None
    supplied = []
    try:
        document = json.loads(question_content)
        if not isinstance(document, dict):
            raise InitialEvidenceError("INITIAL_QUESTION_OBJECT_REQUIRED")
        supplied = document.get("business_tool_views", [])
        if not isinstance(supplied, list):
            raise InitialEvidenceError("INITIAL_VIEW_LIST_INVALID")
    except (ValueError, TypeError):
        issue("question", question_content, "INITIAL_QUESTION_OBJECT_REQUIRED")

    entries = []
    context = None
    loaded = []
    if manifest_path and not unverified:
        location, manifest_content = "manifest", str(manifest_path)
        try:
            manifest_path = Path(manifest_path)
            bundle_bytes = _bytes(manifest_path)
            _copy(out / "initial-manifest-original.json", bundle_bytes)
            manifest_content = bundle_bytes.decode("utf-8", errors="replace")
            bundle = json.loads(bundle_bytes)
            if not isinstance(bundle, dict):
                raise InitialEvidenceError("INITIAL_MANIFEST_INVALID")
            if bundle.get(
                "schema_version"
            ) != "m0-initial-evidence-v1" or not isinstance(
                bundle.get("entries"), list
            ):
                raise InitialEvidenceError("INITIAL_MANIFEST_INVALID")
            context = ProjectionContext.model_validate_json(
                json.dumps(bundle["projection_context"])
            )
            context_signature(context)
            if canonical_hash(
                context.registry
            ) != context.registry_hash or context.registry_hash != scope.get(
                "deployment_registry_sha256"
            ):
                raise InitialEvidenceError("INITIAL_CONTEXT_INCOMPATIBLE")
            if not report_only and (
                context.source_sha256 != runtime_source_sha256
                or runtime_dependencies is None
                or {d.module: d.source_sha256 for d in context.dependencies}
                != runtime_dependencies
            ):
                raise InitialEvidenceError("INITIAL_MIXED_CONTEXT_UNSUPPORTED")
            if not context.source_path:
                raise InitialEvidenceError("INITIAL_SOURCE_PATH_REQUIRED")
            base = manifest_path.parent

            def resolved(name):
                path = Path(name)
                return path if path.is_absolute() else base / path

            context = context.model_copy(
                update={"source_path": str(resolved(context.source_path))}
            )
            from scripts.m0.projector_trust import authenticate_projection

            context = context.model_copy(
                update={
                    "dependencies": [
                        dependency.model_copy(
                            update={
                                "source_path": str(resolved(dependency.source_path))
                            }
                        )
                        for dependency in context.dependencies
                    ]
                }
            )
            authenticate_projection(context)
            source_bytes = _bytes(context.source_path, context.source_sha256)
            dependency_bytes = []
            for dependency in context.dependencies:
                source_path = resolved(dependency.source_path)
                dependency_bytes.append(
                    (
                        dependency.module,
                        source_path,
                        _bytes(source_path, dependency.source_sha256),
                        dependency.source_sha256,
                    )
                )
            context = context.model_copy(
                update={
                    "dependencies": [
                        d.model_copy(update={"source_path": str(path)})
                        for d, (_, path, _, _) in zip(
                            context.dependencies, dependency_bytes
                        )
                    ]
                }
            )
            seen = set()
            for ordinal, entry in enumerate(bundle["entries"]):
                location, manifest_content = f"entries[{ordinal}]", entry
                if not isinstance(entry, dict):
                    raise InitialEvidenceError("INITIAL_MANIFEST_ENTRY_INVALID")
                evidence_id = entry.get("evidence_id")
                if (
                    not isinstance(evidence_id, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", evidence_id)
                    or evidence_id in seen
                    or evidence_id.startswith(run_id + "-e")
                ):
                    raise InitialEvidenceError(
                        "INITIAL_EVIDENCE_ID_INVALID_OR_DUPLICATE"
                    )
                seen.add(evidence_id)
                if "projection_context" in entry and context_signature(
                    ProjectionContext.model_validate_json(
                        json.dumps(entry["projection_context"])
                    )
                ) != context_signature(context):
                    raise InitialEvidenceError("INITIAL_MIXED_CONTEXT_UNSUPPORTED")
                verified = verify_initial_entry(entry, context, scope, base_dir=base)
                raw_bytes, view_bytes, origin_bytes, raw, view, revision, timing = (
                    verified[key]
                    for key in (
                        "raw_bytes",
                        "view_bytes",
                        "manifest_bytes",
                        "raw",
                        "view",
                        "projection_revision",
                        "timing",
                    )
                )
                loaded.append(
                    (
                        evidence_id,
                        raw_bytes,
                        view_bytes,
                        origin_bytes,
                        raw,
                        view,
                        revision,
                        timing,
                    )
                )
            if supplied:
                if len(supplied) != len(loaded) or any(
                    not isinstance(view, dict) for view in supplied
                ):
                    raise InitialEvidenceError("INITIAL_USER_VIEW_MISMATCH")
                by_id = {view.get("evidence_id"): view for view in supplied}
                if len(by_id) != len(supplied) or any(
                    by_id.get(evidence_id) != view
                    for evidence_id, _, _, _, _, view, _, _ in loaded
                ):
                    raise InitialEvidenceError("INITIAL_USER_VIEW_MISMATCH")
            copy_dir = out / "initial-evidence"
            copied_source = copy_dir / (context.source_sha256 + ".py.txt")
            _copy(copied_source, source_bytes)
            copied_dependencies = []
            for module, _, data, sha in dependency_bytes:
                path = copy_dir / (sha + ".py.txt")
                if path.exists():
                    _bytes(path, sha)
                else:
                    _copy(path, data)
                copied_dependencies.append(
                    {
                        "module": module,
                        "source_path": str(path.resolve()),
                        "source_sha256": sha,
                    }
                )
            copied_context = context.model_dump(mode="json")
            copied_context.update(
                source_path=str(copied_source.resolve()),
                dependencies=copied_dependencies,
            )
            for (
                evidence_id,
                raw_bytes,
                view_bytes,
                origin_bytes,
                raw,
                view,
                revision,
                timing,
            ) in loaded:
                paths = {
                    "raw_path": copy_dir / (evidence_id + "-raw.json"),
                    "view_path": copy_dir / (evidence_id + "-view.json"),
                    "manifest_path": copy_dir / (evidence_id + "-manifest.json"),
                }
                for key, data in zip(paths, (raw_bytes, view_bytes, origin_bytes)):
                    _copy(paths[key], data)
                entries.append(
                    {
                        "evidence_id": evidence_id,
                        **{key: str(path.resolve()) for key, path in paths.items()},
                        "raw_file_sha256": hashlib.sha256(raw_bytes).hexdigest(),
                        "view_file_sha256": hashlib.sha256(view_bytes).hexdigest(),
                        "manifest_file_sha256": hashlib.sha256(
                            origin_bytes
                        ).hexdigest(),
                        "projection_revision": revision,
                        "timing": timing,
                    }
                )
                result["verified_views"][evidence_id] = view
                result["timings"][evidence_id] = timing
            result["projection_context"] = copied_context
            save(
                out / "initial-evidence.json",
                {
                    "schema_version": "m0-initial-evidence-v1",
                    "source_manifest_sha256": hashlib.sha256(bundle_bytes).hexdigest(),
                    "original_projection_context": bundle["projection_context"],
                    "projection_context": copied_context,
                    "entries": entries,
                },
            )
            if not supplied:
                document["business_tool_views"] = list(
                    result["verified_views"].values()
                )
                result["actual_question_content"] = json.dumps(
                    document, ensure_ascii=False
                )
        except (ValueError, KeyError, TypeError, OSError) as exc:
            reason = (
                str(exc)
                if isinstance(exc, InitialEvidenceError)
                or re.fullmatch(r"[A-Z][A-Z0-9_]+", str(exc))
                else "INITIAL_PROVENANCE_INVALID_OR_MISSING"
            )
            issue(location, manifest_content, reason)
            result.update(verified_views={}, timings={}, projection_context=None)
    elif supplied:
        for index, value in enumerate(supplied):
            issue(f"business_tool_views[{index}]", value, "INITIAL_PROVENANCE_MISSING")
    save(
        out / "initial-import-audit.json",
        {"status": "unknown" if unverified else "verified", "unverified": unverified},
    )
    save(
        out / "input-provenance.json",
        {
            "original_user_content_sha256": hashlib.sha256(original).hexdigest(),
            "actual_user_content_sha256": hashlib.sha256(
                result["actual_question_content"].encode()
            ).hexdigest(),
            "original_user_content_path": "question-original.txt",
            "actual_user_content_path": "input-business.json",
            "initial_evidence_status": "unknown" if unverified else "verified",
        },
    )
    return result
