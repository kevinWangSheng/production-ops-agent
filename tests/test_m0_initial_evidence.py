"""Initial evidence imports use real raw+view+frozen projector, never view-as-raw."""

import hashlib
import json
from pathlib import Path

from scripts.m0.holmes_bridge import canonical_hash, replay_projection
from scripts.m0.outcomes_v3 import ProjectionContext
from scripts.m0_environment.initial_evidence import import_initial_evidence
from scripts.m0_environment.round02 import save

ROOT = Path(__file__).resolve().parents[1]


def bundle(tmp_path):
    source_sha = "22a96b87cf6575c8246abeb7edbed5101e1c6951fcc62b5e8243b6e589c6efd8"
    dep_sha = "507d4205fb5e65cdd831003ae45096703050534873018cbb0016c4f4678e4caf"
    registry = {"integration_id": "m0-otel-20260909", "containers": []}
    scope = {
        "integration_id": registry["integration_id"],
        "services": ["checkout"],
        "policy_revision": "p",
        "deployment_registry_sha256": canonical_hash(registry),
        "window": {"start": 1789002000, "end": 1789002060},
    }
    context = ProjectionContext(
        registry=registry,
        registry_hash=canonical_hash(registry),
        source_sha256=source_sha,
        source_path=str(
            ROOT
            / "docs/evidence/m0-real-environment/runtime-sources"
            / (source_sha + ".py.txt")
        ),
        dependencies=[
            {
                "module": "legacy_projections",
                "source_path": str(
                    ROOT
                    / "docs/evidence/m0-real-environment/runtime-sources"
                    / (dep_sha + ".py.txt")
                ),
                "source_sha256": dep_sha,
            }
        ],
    )
    raw = {
        "evidence_id": "producer-e1",
        "tool": "otel_logs",
        "query": {"service": "checkout", **scope["window"]},
        "trusted_access_scope": scope,
        "http_status": 200,
        "observed_at": "2026-09-10T01:01:01Z",
        "operation_started_at": "2026-09-10T01:01:01Z",
        "collection_completed_at": "2026-09-10T01:01:02Z",
        "actual_sources": {"services": ["checkout"]},
        "data": {
            "integration_id": registry["integration_id"],
            "data": {
                "hits": {
                    "hits": [
                        {
                            "_id": "doc",
                            "_source": {
                                "@timestamp": "2026-09-10T01:00:30Z",
                                "resource": {"service.name": "checkout"},
                                "body": "observed",
                            },
                        }
                    ],
                    "total": {"value": 1},
                }
            },
        },
    }
    view = replay_projection(raw, context, revision="m0-02-logs-v3")
    paths = {name: tmp_path / (name + ".json") for name in ("raw", "view", "manifest")}
    save(paths["raw"], raw)
    save(paths["view"], view)

    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    save(
        paths["manifest"],
        {
            "evidence_id": raw["evidence_id"],
            "raw_sha256": canonical_hash(raw),
            "view_sha256": canonical_hash(view),
            "raw_file_sha256": sha(paths["raw"]),
            "view_file_sha256": sha(paths["view"]),
            "query": raw["query"],
            "scope": scope,
            "projection_version": "m0-02-logs-v3",
        },
    )
    entry = {
        "evidence_id": raw["evidence_id"],
        "raw_path": str(paths["raw"]),
        "raw_file_sha256": sha(paths["raw"]),
        "view_path": str(paths["view"]),
        "view_file_sha256": sha(paths["view"]),
        "manifest_path": str(paths["manifest"]),
        "manifest_file_sha256": sha(paths["manifest"]),
        "timing": {
            "operation_started_at": raw["operation_started_at"],
            "collection_completed_at": raw["collection_completed_at"],
            "source_start_at": "2026-09-10T01:00:30Z",
            "source_end_at": "2026-09-10T01:00:30Z",
            "source_time_basis": "event_time",
        },
    }
    manifest = tmp_path / "bundle.json"
    save(
        manifest,
        {
            "schema_version": "m0-initial-evidence-v1",
            "projection_context": context.model_dump(mode="json"),
            "entries": [entry],
        },
    )
    return manifest, scope, raw, view


def test_exact_import_auto_assembles_user_view_and_preserves_original(tmp_path):
    manifest, scope, raw, view = bundle(tmp_path)
    out = tmp_path / "run"
    out.mkdir()
    original = '{"request":"review"}\n'
    result = import_initial_evidence(
        original,
        manifest,
        out,
        scope,
        run_id="newrun",
        report_only=True,
        runtime_source_sha256="0" * 64,
    )
    assert result["unverified"] == []
    assert (out / "question-original.txt").read_bytes() == original.encode()
    assert json.loads(result["actual_question_content"])["business_tool_views"] == [
        view
    ]
    assert result["verified_views"] == {raw["evidence_id"]: view}
    imported = json.loads((out / "initial-evidence.json").read_text())
    assert (
        Path(imported["entries"][0]["raw_path"]).read_bytes()
        == (tmp_path / "raw.json").read_bytes()
    )
    assert (
        json.loads((out / "input-provenance.json").read_text())[
            "original_user_content_sha256"
        ]
        != hashlib.sha256(result["actual_question_content"].encode()).hexdigest()
    )


def test_missing_manifest_preserves_unverified_view_instead_of_inventing_raw(tmp_path):
    _, scope, _, view = bundle(tmp_path)
    out = tmp_path / "run"
    out.mkdir()
    content = json.dumps({"business_tool_views": [view]})
    result = import_initial_evidence(
        content,
        None,
        out,
        scope,
        run_id="newrun",
        report_only=True,
        runtime_source_sha256="0" * 64,
    )
    assert result["unverified"][0]["reason"] == "INITIAL_PROVENANCE_MISSING"
    assert (
        result["verified_views"] == {} and result["actual_question_content"] == content
    )
    assert not (out / "initial-evidence.json").exists()


def test_duplicate_or_mixed_keeps_original_input_and_no_qualified_views(tmp_path):
    manifest, scope, _, view = bundle(tmp_path)
    original_bundle = json.loads(manifest.read_text())
    for case in ("duplicate", "mixed"):
        value = json.loads(json.dumps(original_bundle))
        if case == "duplicate":
            value["entries"].append(value["entries"][0])
        out = tmp_path / case
        out.mkdir()
        save(manifest, value)
        content = json.dumps({"business_tool_views": [view]})
        result = import_initial_evidence(
            content,
            manifest,
            out,
            scope,
            run_id="newrun",
            report_only=case != "mixed",
            runtime_source_sha256="0" * 64,
        )
        assert result["unverified"] and not result["verified_views"]
        assert result["actual_question_content"] == content


def test_verified_source_outside_current_scope_is_not_imported(tmp_path):
    manifest, scope, _, _ = bundle(tmp_path)
    scope = {**scope, "services": ["different-service"]}
    out = tmp_path / "run"
    out.mkdir()
    result = import_initial_evidence(
        '{"request":"review"}',
        manifest,
        out,
        scope,
        run_id="newrun",
        report_only=True,
        runtime_source_sha256="0" * 64,
    )
    assert result["verified_views"] == {}
    assert result["unverified"][0]["reason"] == "TARGET_SCOPE_DENIED"


def test_bad_entry_shape_is_audited_with_original_manifest_bytes(tmp_path):
    manifest, scope, _, _ = bundle(tmp_path)
    value = json.loads(manifest.read_text())
    value["entries"] = [None]
    save(manifest, value)
    original = manifest.read_bytes()
    out = tmp_path / "run"
    out.mkdir()
    result = import_initial_evidence(
        '{"request":"review"}',
        manifest,
        out,
        scope,
        run_id="newrun",
        report_only=True,
        runtime_source_sha256="0" * 64,
    )
    assert result["unverified"][0]["location"] == "entries[0]"
    assert result["unverified"][0]["content"] == "null"
    assert (out / "initial-manifest-original.json").read_bytes() == original


def test_missing_raw_keeps_question_and_reports_missing_source(tmp_path):
    manifest, scope, _, view = bundle(tmp_path)
    value = json.loads(manifest.read_text())
    value["entries"][0]["raw_path"] = str(tmp_path / "missing-raw.json")
    save(manifest, value)
    out = tmp_path / "run"
    out.mkdir()
    question = json.dumps({"business_tool_views": [view]})
    result = import_initial_evidence(
        question,
        manifest,
        out,
        scope,
        run_id="newrun",
        report_only=True,
        runtime_source_sha256="0" * 64,
    )
    assert result["unverified"][0]["reason"] == "INITIAL_SOURCE_MISSING"
    assert result["actual_question_content"] == question
    assert (out / "question-original.txt").read_text() == question


def test_bad_id_is_preserved_as_audit_content_not_new_evidence_id(tmp_path):
    manifest, scope, _, _ = bundle(tmp_path)
    value = json.loads(manifest.read_text())
    value["entries"][0]["evidence_id"] = "../invalid"
    save(manifest, value)
    out = tmp_path / "run"
    out.mkdir()
    result = import_initial_evidence(
        '{"request":"review"}',
        manifest,
        out,
        scope,
        run_id="newrun",
        report_only=True,
        runtime_source_sha256="0" * 64,
    )
    audit = result["unverified"][0]
    assert audit["location"] == "entries[0]" and "../invalid" in audit["content"]
    assert "evidence_id" not in audit and result["verified_views"] == {}


def test_active_same_source_but_different_dependencies_is_mixed(tmp_path):
    manifest, scope, _, _ = bundle(tmp_path)
    source_sha = json.loads(manifest.read_text())["projection_context"]["source_sha256"]
    out = tmp_path / "run"
    out.mkdir()
    result = import_initial_evidence(
        '{"request":"review"}',
        manifest,
        out,
        scope,
        run_id="newrun",
        report_only=False,
        runtime_source_sha256=source_sha,
        runtime_dependencies={"legacy_projections": "0" * 64},
    )
    assert result["unverified"][0]["reason"] == "INITIAL_MIXED_CONTEXT_UNSUPPORTED"
    assert not result["verified_views"]
