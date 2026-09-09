"""Check the loaded experiment environment against the approved Python and lock."""

import importlib.metadata
import platform
import tomllib
from pathlib import Path

from packaging.markers import Marker
from packaging.specifiers import SpecifierSet

from .config import ConfigError

ROOT = Path(__file__).resolve().parents[2]


def check_runtime(expected):
    try:
        actual = {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
        }
        if (
            type(expected) is not dict
            or expected != actual
            or actual["implementation"] != "CPython"
        ):
            raise ValueError
        lock = tomllib.loads((ROOT / "uv.lock").read_text())
        if actual["python"] not in SpecifierSet(lock["requires-python"]):
            raise ValueError
        packages = {package["name"]: package for package in lock["package"]}
        if len(packages) != len(lock["package"]):
            raise ValueError  # Multiple-version resolution needs a separately reviewed resolver.
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())
        groups = project["tool"]["uv"]["default-groups"]
        pending = list(packages["opspilot"].get("dependencies", []))
        for group in groups:
            pending += packages["opspilot"]["dev-dependencies"][group]
        seen = set()
        while pending:
            dependency = pending.pop()
            if "marker" in dependency and not Marker(dependency["marker"]).evaluate():
                continue
            name = dependency["name"]
            extras = tuple(sorted(dependency.get("extra", [])))
            identity = (name, extras)
            if identity in seen:
                continue
            seen.add(identity)
            package = packages[name]
            if importlib.metadata.version(name) != package["version"]:
                raise ValueError
            pending += package.get("dependencies", [])
            for extra in extras:
                pending += package["optional-dependencies"][extra]
    except Exception:
        # Metadata/marker exceptions may contain arbitrary environment text.
        raise ConfigError("LIVE_RUNTIME_MISMATCH") from None
