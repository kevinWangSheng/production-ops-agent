"""Read-only tool and target registration contracts for the Tool Gateway.

Contract sources:

- Technical plan section 8 (tool registration contract, read-only surface,
  target isolation, simultaneous timeout and output-size bounds).
- Technical plan section 3 (the Tool Gateway holds target-scoped read-only
  credentials and independently validates authorization and budgets).
- ``PRODUCT-CONSTRAINTS.md``, "Evidence and context requirements" and
  "Runtime and human control requirements".
- The frozen M1-01 per-Run resource ceilings in
  ``docs/testing/first-investigation-v4-2026-09-10.md`` (user approved
  2026-09-13).

Everything in this module is a *controller-owned* contract: registrations and
targets are built from reviewed configuration, never from model output. A
violation here is an operator or programmer error and therefore raises.
Problems caused by model-proposed input are never raised; they are returned as
a typed outcome by :mod:`opspilot.tools.executor`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

__all__ = [
    "FORBIDDEN_VERBS",
    "MAX_REQUEST_TIMEOUT_SECONDS",
    "MAX_RESULT_BYTES",
    "READ_ONLY_VERBS",
    "RESERVED_PARAMETERS",
    "SOURCE_ERROR_REASONS",
    "ParameterSpec",
    "RegisteredTarget",
    "TargetRegistry",
    "ToolContractError",
    "ToolRegistration",
    "ToolRegistry",
    "canonical",
    "canonical_hash",
]


class ToolContractError(Exception):
    """Fixed-code failure of a registration or target contract.

    Only fixed codes may cross this boundary: registration inputs may quote
    vendor text, and vendor text must not be re-raised into logs, reports or
    model context.
    """


# Frozen M1-01 ceilings. Raising them needs a new user-approved freeze in the
# acceptance packet, not a code change alone.
MAX_REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RESULT_BYTES = 2 * 1024 * 1024

# Technical plan section 8: Kubernetes exposes only get/list/watch/logs, and no
# arbitrary shell, SQL or code execution is offered at all.
READ_ONLY_VERBS = frozenset({"get", "list", "watch", "logs", "query", "describe"})
FORBIDDEN_VERBS = frozenset(
    {
        "apply",
        "attach",
        "cordon",
        "create",
        "delete",
        "drain",
        "evict",
        "exec",
        "patch",
        "port-forward",
        "restart",
        "rollout",
        "run",
        "scale",
        "shell",
        "sql",
        "update",
        "write",
    }
)

# Parameter names the gateway resolves itself. A registration may not declare
# them, so a model-proposed call can never choose an endpoint, a target or an
# authentication header.
RESERVED_PARAMETERS = frozenset(
    {
        "api_key",
        "authorization",
        "base_url",
        "credential",
        "credential_ref",
        "endpoint",
        "headers",
        "password",
        "secret",
        "target",
        "target_id",
        "token",
        "url",
    }
)

# Outcome reasons a registration may map a source-reported status onto. The
# vocabulary is fixed so a data source cannot invent its own outcome class.
SOURCE_ERROR_REASONS = frozenset(
    {"INVALID_PARAMS", "SOURCE_ERROR", "SOURCE_UNAVAILABLE"}
)

_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{2,63}")
_HANDLE = re.compile(r"[a-z0-9][a-z0-9_-]{2,63}")
_VERSION = re.compile(r"[a-z0-9][a-z0-9.+-]{0,31}")
_ENDPOINT = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=-]{1,512}")

ParameterKind = Literal["string", "integer", "number", "boolean"]

_PYTHON_KINDS: Mapping[str, tuple[type, ...]] = MappingProxyType(
    {
        "string": (str,),
        "integer": (int,),
        "number": (int, float),
        "boolean": (bool,),
    }
)


def canonical(value: object) -> str:
    """Stable JSON text used for every hash and every stored query string."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ParameterSpec:
    """One declared query parameter of a registered tool."""

    kind: ParameterKind
    required: bool = False

    def __post_init__(self) -> None:
        if self.kind not in _PYTHON_KINDS or type(self.required) is not bool:
            raise ToolContractError("INVALID_PARAMETER_SPEC")

    def accepts(self, value: object) -> bool:
        if self.kind != "boolean" and type(value) is bool:
            return False  # bool is an int subclass; keep the kinds disjoint.
        return isinstance(value, _PYTHON_KINDS[self.kind])


@dataclass(frozen=True)
class ToolRegistration:
    """The section 8 registration contract for one read-only tool.

    Name, version, parameter schema, data source, absolute query window bound,
    request deadline, result-size ceiling, error classification and the
    incomplete-result marker are all declared here, so the executor never has
    to infer them from a response.
    """

    name: str
    version: str
    source: str
    verb: str
    parameters: Mapping[str, ParameterSpec]
    result_path: tuple[str, ...]
    request_timeout_seconds: float
    max_result_bytes: int
    max_view_bytes: int
    max_window_seconds: int
    error_classes: Mapping[str, str] = field(default_factory=dict)
    incomplete_marker: str | None = None
    read_only: bool = True

    def __post_init__(self) -> None:
        if not (
            isinstance(self.name, str)
            and _NAME.fullmatch(self.name)
            and isinstance(self.version, str)
            and _VERSION.fullmatch(self.version)
            and isinstance(self.source, str)
            and _NAME.fullmatch(self.source)
        ):
            raise ToolContractError("INVALID_TOOL_IDENTITY")
        if self.read_only is not True or self.verb in FORBIDDEN_VERBS:
            raise ToolContractError("WRITE_CAPABILITY_FORBIDDEN")
        if self.verb not in READ_ONLY_VERBS:
            raise ToolContractError("VERB_NOT_ALLOWED")
        if not isinstance(self.parameters, Mapping) or any(
            not isinstance(key, str) or not isinstance(spec, ParameterSpec)
            for key, spec in self.parameters.items()
        ):
            raise ToolContractError("INVALID_PARAMETER_SPEC")
        if RESERVED_PARAMETERS & set(self.parameters):
            raise ToolContractError("RESERVED_PARAMETER")
        if not isinstance(self.result_path, tuple) or any(
            not isinstance(step, str) or not step for step in self.result_path
        ):
            raise ToolContractError("INVALID_RESULT_PATH")
        if (
            type(self.request_timeout_seconds) not in (int, float)
            or not 0 < self.request_timeout_seconds <= MAX_REQUEST_TIMEOUT_SECONDS
        ):
            raise ToolContractError("REQUEST_TIMEOUT_OUT_OF_RANGE")
        if (
            type(self.max_result_bytes) is not int
            or not 0 < self.max_result_bytes <= MAX_RESULT_BYTES
        ):
            raise ToolContractError("RESULT_LIMIT_OUT_OF_RANGE")
        if (
            type(self.max_view_bytes) is not int
            or not 0 < self.max_view_bytes <= self.max_result_bytes
        ):
            raise ToolContractError("VIEW_LIMIT_OUT_OF_RANGE")
        if type(self.max_window_seconds) is not int or self.max_window_seconds <= 0:
            raise ToolContractError("WINDOW_LIMIT_OUT_OF_RANGE")
        if not isinstance(self.error_classes, Mapping) or any(
            not isinstance(key, str) or not key or reason not in SOURCE_ERROR_REASONS
            for key, reason in self.error_classes.items()
        ):
            raise ToolContractError("INVALID_ERROR_CLASSES")
        if self.incomplete_marker is not None and (
            not isinstance(self.incomplete_marker, str) or not self.incomplete_marker
        ):
            raise ToolContractError("INVALID_INCOMPLETE_MARKER")
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))
        object.__setattr__(
            self, "error_classes", MappingProxyType(dict(self.error_classes))
        )

    def classify(self, source_status: str) -> str:
        """Map a source-reported status onto a fixed outcome reason."""

        return self.error_classes.get(source_status, "SOURCE_ERROR")


@dataclass(frozen=True)
class RegisteredTarget:
    """An immutable target identity. Resolution is by ``target_id`` only.

    ``display_name`` exists for humans. Nothing in this package resolves a
    target from a display name, a service label or a model-proposed endpoint.
    """

    target_id: str
    source: str
    endpoint: str
    credential_ref: str
    selector: Mapping[str, str] = field(default_factory=dict)
    display_name: str = ""

    def __post_init__(self) -> None:
        if not (
            isinstance(self.target_id, str)
            and _NAME.fullmatch(self.target_id)
            and isinstance(self.source, str)
            and _NAME.fullmatch(self.source)
        ):
            raise ToolContractError("INVALID_TARGET_IDENTITY")
        if not isinstance(self.endpoint, str) or not _ENDPOINT.fullmatch(self.endpoint):
            raise ToolContractError("INVALID_ENDPOINT")
        if "@" in self.endpoint.split("//", 1)[1].split("/", 1)[0]:
            # Userinfo in the authority would carry credential material.
            raise ToolContractError("CREDENTIAL_MATERIAL_FORBIDDEN")
        if not isinstance(self.credential_ref, str) or not _HANDLE.fullmatch(
            self.credential_ref
        ):
            # Only an opaque handle may be stored here. Secret material stays
            # with the transport's credential provider, outside this package.
            raise ToolContractError("CREDENTIAL_MATERIAL_FORBIDDEN")
        if not isinstance(self.selector, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.selector.items()
        ):
            raise ToolContractError("INVALID_SELECTOR")
        if not isinstance(self.display_name, str):
            raise ToolContractError("INVALID_TARGET_IDENTITY")
        object.__setattr__(self, "selector", MappingProxyType(dict(self.selector)))


class _FrozenIndex:
    """Shared read-only index behaviour for the two registries."""

    __slots__ = ("_entries", "_revision")

    def __init__(self, entries: Mapping[str, object], fingerprint: object) -> None:
        self._entries = MappingProxyType(dict(entries))
        self._revision = canonical_hash(fingerprint)

    @property
    def revision(self) -> str:
        """Content hash of the whole registry; recorded with every operation."""

        return self._revision


class ToolRegistry(_FrozenIndex):
    """The set of tools this gateway may execute at all."""

    __slots__ = ()

    def __init__(self, registrations: Iterable[ToolRegistration]) -> None:
        entries: dict[str, ToolRegistration] = {}
        for registration in registrations:
            if not isinstance(registration, ToolRegistration):
                raise ToolContractError("INVALID_REGISTRATION")
            if registration.name in entries:
                raise ToolContractError("DUPLICATE_TOOL")
            entries[registration.name] = registration
        super().__init__(
            entries,
            [
                [name, entries[name].version, entries[name].source, entries[name].verb]
                for name in sorted(entries)
            ],
        )

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(self._entries)

    def lookup(self, name: object) -> ToolRegistration | None:
        """Return the registration for ``name`` or ``None``; never raises.

        ``name`` arrives from model output, so an unknown or ill-typed name is
        a normal gateway outcome, not an exception.
        """

        if not isinstance(name, str):
            return None
        return self._entries.get(name)


class TargetRegistry(_FrozenIndex):
    """Registered immutable targets, resolvable only by their own identity."""

    __slots__ = ()

    def __init__(self, targets: Iterable[RegisteredTarget]) -> None:
        entries: dict[str, RegisteredTarget] = {}
        for target in targets:
            if not isinstance(target, RegisteredTarget):
                raise ToolContractError("INVALID_TARGET")
            if target.target_id in entries:
                raise ToolContractError("DUPLICATE_TARGET")
            entries[target.target_id] = target
        super().__init__(
            entries,
            [
                [
                    target_id,
                    entries[target_id].source,
                    entries[target_id].endpoint,
                    sorted(entries[target_id].selector.items()),
                ]
                for target_id in sorted(entries)
            ],
        )

    @property
    def target_ids(self) -> frozenset[str]:
        return frozenset(self._entries)

    def resolve(self, target_id: object) -> RegisteredTarget | None:
        """Resolve a registered target by identity; never by any other field."""

        if not isinstance(target_id, str):
            return None
        return self._entries.get(target_id)
