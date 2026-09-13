"""Projector authenticity; hostile code writes only a harmless temporary marker."""

import hashlib
from pathlib import Path

import pytest

from scripts.m0.holmes_bridge import (
    canonical_hash,
    load_dependencies,
    replay_projection,
)
from scripts.m0.outcomes_v3 import ProjectionContext

ROOT = Path(__file__).resolve().parents[1]
OLD = "22a96b87cf6575c8246abeb7edbed5101e1c6951fcc62b5e8243b6e589c6efd8"
DEP = "507d4205fb5e65cdd831003ae45096703050534873018cbb0016c4f4678e4caf"


def historical():
    registry = {"integration_id": "lab", "containers": []}
    return ProjectionContext(
        registry=registry,
        registry_hash=canonical_hash(registry),
        source_path=str(
            ROOT / f"docs/evidence/m0-real-environment/runtime-sources/{OLD}.py.txt"
        ),
        source_sha256=OLD,
    )


@pytest.mark.parametrize(
    "kind",
    ["body", "default", "decorator", "annotation", "dependency", "unknown_dependency"],
)
def test_self_supplied_hash_cannot_authorize_projector_execution(tmp_path, kind):
    marker = tmp_path / "executed"
    expression = f"__import__('pathlib').Path({str(marker)!r}).write_text('executed')"
    context = historical()
    source = tmp_path / "untrusted.py"
    if kind in {"dependency", "unknown_dependency"}:
        source.write_text(
            f"def _bind_identity_v2(record={expression}): return record\ndef log_projection_v2(record, registry=None): return record\n"
        )
        context = context.model_copy(
            update={
                "dependencies": [
                    {
                        "module": "legacy_projections"
                        if kind == "dependency"
                        else "unregistered",
                        "source_path": str(source),
                        "source_sha256": hashlib.sha256(
                            source.read_bytes()
                        ).hexdigest(),
                    }
                ]
            }
        )
        from scripts.m0.outcomes_v3 import ProjectionDependency

        context = context.model_copy(
            update={
                "dependencies": [
                    ProjectionDependency.model_construct(**d)
                    for d in context.dependencies
                ]
            }
        )
    else:
        code = "def bind_identity(*args): return {}\ndef log_projection(record, registry=None): return record\n"
        if kind == "body":
            code += f"def trace_projection(record, registry=None):\n    {expression}\n    return record\n"
        elif kind == "default":
            code += (
                f"def trace_projection(record, registry={expression}): return record\n"
            )
        else:
            code += f"@((lambda f: f) if {expression} else (lambda f: f))\ndef trace_projection(record, registry=None): return record\n"
        source.write_text(code)
        context = context.model_copy(
            update={
                "source_path": str(source),
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        )
    with pytest.raises(ValueError, match="PROJECTION_SOURCE_UNTRUSTED"):
        if kind in {"dependency", "unknown_dependency"}:
            load_dependencies(context, {})
        else:
            replay_projection({"tool": "otel_traces", "http_status": 200}, context)
    assert not marker.exists()


def test_report_only_import_rejects_unknown_source_before_copy_or_execution(tmp_path):
    import json

    from test_m0_initial_evidence import bundle

    from scripts.m0_environment.initial_evidence import import_initial_evidence

    manifest, scope, _, _ = bundle(tmp_path)
    document = json.loads(manifest.read_bytes())
    original = Path(document["projection_context"]["source_path"]).read_text()
    marker = tmp_path / "import-executed"
    expression = f"__import__('pathlib').Path({str(marker)!r}).write_text('executed')"
    malicious = tmp_path / "self-pinned.py"
    malicious.write_text(
        original.replace(
            "def bind_identity(",
            f"@((lambda f: f) if {expression} else (lambda f: f))\ndef bind_identity(",
            1,
        )
    )
    document["projection_context"].update(
        source_path=str(malicious),
        source_sha256=hashlib.sha256(malicious.read_bytes()).hexdigest(),
    )
    manifest.write_text(json.dumps(document))
    out = tmp_path / "new-run"
    out.mkdir()
    result = import_initial_evidence(
        '{"request":"review"}',
        manifest,
        out,
        scope,
        run_id="new-run",
        report_only=True,
        runtime_source_sha256="0" * 64,
    )
    assert result["unverified"] and not result["verified_views"]
    assert not marker.exists() and not (out / "initial-evidence").exists()
    assert (
        out / "initial-manifest-original.json"
    ).exists()  # Unexecuted JSON audit only.


@pytest.mark.parametrize("target", ["source", "dependency"])
def test_claiming_a_known_hash_does_not_authorize_different_bytes(tmp_path, target):
    from scripts.m0.outcomes_v3 import ProjectionDependency

    context = historical()
    marker = tmp_path / "known-hash-executed"
    file = tmp_path / "different.py"
    file.write_text(
        f"def bind_identity(value=__import__('pathlib').Path({str(marker)!r}).write_text('executed')): return value\n"
    )
    if target == "source":
        context = context.model_copy(update={"source_path": str(file)})
    else:
        context = context.model_copy(
            update={
                "dependencies": [
                    ProjectionDependency(
                        module="legacy_projections",
                        source_path=str(file),
                        source_sha256=DEP,
                    )
                ]
            }
        )
    with pytest.raises(ValueError, match="PROJECTION_AUTHENTICITY_HASH_MISMATCH"):
        load_dependencies(context, {})
    assert not marker.exists()


def test_known_hashes_do_not_authorize_an_unreviewed_combination():
    from scripts.m0.outcomes_v3 import ProjectionDependency

    context = historical()
    hash_value = "0ed7d37aeca6057f2fd59ca2da692cab73b1247bd0e209555200ef74f32e27fc"
    context = context.model_copy(
        update={
            "dependencies": [
                ProjectionDependency(
                    module="legacy_projections",
                    source_path=str(
                        ROOT
                        / f"docs/evidence/m0-real-environment/runtime-sources/{hash_value}.py.txt"
                    ),
                    source_sha256=hash_value,
                )
            ]
        }
    )
    with pytest.raises(ValueError, match="PROJECTION_SOURCE_UNTRUSTED"):
        replay_projection({"tool": "otel_logs", "error": "synthetic"}, context)


def test_all_fixed_historical_bundles_allow_relocated_identical_bytes(tmp_path):
    from scripts.m0 import projector_trust
    from scripts.m0.outcomes_v3 import ProjectionDependency

    for index, (source_hash, dep_pins) in enumerate(projector_trust.HISTORICAL_BUNDLES):
        folder = tmp_path / str(index)
        folder.mkdir()
        directory = (
            "immutable" if source_hash.startswith("7fd732") else "runtime-sources"
        )
        original = (
            ROOT / f"docs/evidence/m0-real-environment/{directory}/{source_hash}.py.txt"
        )
        relocated = folder / "projector.py"
        relocated.write_bytes(original.read_bytes())
        deps = []
        for module, dep_hash in dep_pins:
            path = folder / f"{module}.py"
            path.write_bytes(
                (
                    ROOT
                    / f"docs/evidence/m0-real-environment/runtime-sources/{dep_hash}.py.txt"
                ).read_bytes()
            )
            deps.append(
                ProjectionDependency(
                    module=module, source_path=str(path), source_sha256=dep_hash
                )
            )
        context = historical().model_copy(
            update={
                "source_path": str(relocated),
                "source_sha256": source_hash,
                "dependencies": deps,
            }
        )
        assert (
            replay_projection({"tool": "otel_logs", "error": "synthetic"}, context)[
                "error"
            ]
            == "synthetic"
        )


def test_fixed_current_repository_bundle_is_trusted_independently_of_manifest():
    from scripts.m0 import projector_trust
    from scripts.m0.outcomes_v3 import ProjectionDependency

    directory = ROOT / "scripts/m0_environment"
    source = directory / "holmes_baseline.py"
    deps = [
        ProjectionDependency(
            module=name,
            source_path=str(directory / f"{name}.py"),
            source_sha256=hashlib.sha256(
                (directory / f"{name}.py").read_bytes()
            ).hexdigest(),
        )
        for name in projector_trust.MODULES
    ]
    context = historical().model_copy(
        update={
            "source_path": str(source),
            "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "dependencies": deps,
        }
    )
    assert (
        replay_projection({"tool": "otel_logs", "error": "synthetic"}, context)["error"]
        == "synthetic"
    )


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_known_source_pin_still_rejects_nonregular_or_symlink_paths(tmp_path, kind):
    context = historical()
    path = tmp_path / "projector.py"
    if kind == "symlink":
        path.symlink_to(context.source_path)
    else:
        path.mkdir()
    context = context.model_copy(update={"source_path": str(path)})
    with pytest.raises(ValueError, match="PROJECTION_SOURCE_INVALID"):
        load_dependencies(context, {})


def test_direct_dependency_loading_requires_the_wrapper_to_be_authenticated():
    from scripts.m0.outcomes_v3 import ProjectionDependency

    context = historical().model_copy(
        update={
            "source_sha256": "0" * 64,
            "dependencies": [
                ProjectionDependency(
                    module="legacy_projections",
                    source_sha256=DEP,
                    source_path=str(
                        ROOT
                        / f"docs/evidence/m0-real-environment/runtime-sources/{DEP}.py.txt"
                    ),
                )
            ],
        }
    )
    with pytest.raises(ValueError, match="PROJECTION_SOURCE_UNTRUSTED"):
        load_dependencies(context, {})
