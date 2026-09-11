"""Executable projector authenticity, independent of evidence-bundle assertions."""

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULES = ("legacy_projections", "trace_view")
# Reviewed exact bundles; a source pin never authorizes arbitrary dependencies.
# Evidence: semantics-preflight-review:19-21, bridge-dependency-review:18-32.
HISTORICAL_BUNDLES = frozenset(
    {
        ("7fd7326644b26fe00d0a8bad25aab4b865dfbd453bb5a7b4fa491eda5539a676", ()),
        ("22a96b87cf6575c8246abeb7edbed5101e1c6951fcc62b5e8243b6e589c6efd8", ()),
        (
            "22a96b87cf6575c8246abeb7edbed5101e1c6951fcc62b5e8243b6e589c6efd8",
            (
                (
                    "legacy_projections",
                    "507d4205fb5e65cdd831003ae45096703050534873018cbb0016c4f4678e4caf",
                ),
            ),
        ),
        (
            "e15d10cc9fc5ea523f8fd73af5e71e08596bc87481d6f78f1f0c8828ec783b63",
            (
                (
                    "legacy_projections",
                    "0ed7d37aeca6057f2fd59ca2da692cab73b1247bd0e209555200ef74f32e27fc",
                ),
                (
                    "trace_view",
                    "0fdac927db8e633bdcae63d2dbacc988e3b3c5d66977ddddea22dbafb8b88f4b",
                ),
            ),
        ),
    }
)


def _source_bytes(value):
    path = Path(value)
    if path.is_symlink() or path.suffix not in {".py", ".txt"} or not path.is_file():
        raise ValueError("PROJECTION_SOURCE_INVALID")
    return path.read_bytes()


def trusted_bundles():
    """Only fixed repository files define the current trusted runtime bundle."""
    directory = ROOT / "scripts/m0_environment"
    current = (
        hashlib.sha256(_source_bytes(directory / "holmes_baseline.py")).hexdigest(),
        tuple(
            (name, hashlib.sha256(_source_bytes(directory / f"{name}.py")).hexdigest())
            for name in MODULES
        ),
    )
    return HISTORICAL_BUNDLES | {current}


def authenticate_projection(context, *, fallback_source=None):
    """Return the verified bytes which callers must compile, not re-read paths."""
    dependencies = tuple(
        sorted((d.module, d.source_sha256) for d in context.dependencies)
    )
    if (
        len({d.module for d in context.dependencies}) != len(dependencies)
        or any(d.module not in MODULES for d in context.dependencies)
        or (context.source_sha256, dependencies) not in trusted_bundles()
    ):
        raise ValueError("PROJECTION_SOURCE_UNTRUSTED")

    def verified_bytes(value, expected):
        data = _source_bytes(value)
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("PROJECTION_AUTHENTICITY_HASH_MISMATCH")
        return data

    source = verified_bytes(
        context.source_path
        or fallback_source
        or ROOT / "scripts/m0_environment/holmes_baseline.py",
        context.source_sha256,
    )
    deps = {
        d.module: verified_bytes(d.source_path, d.source_sha256)
        for d in context.dependencies
    }
    # Whole bundle verified before any selected function/default/decorator executes.
    return source, deps
