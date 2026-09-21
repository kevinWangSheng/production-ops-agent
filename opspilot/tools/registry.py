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
from typing import Literal, TypeVar

__all__ = [
    "EMPTY_VIEW_BYTES",
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
    "ToolDescription",
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

# The smallest canonical view an executor can ever emit is the empty array
# `[]` (zero kept rows): `_fit_rows()` always counts these two enclosing
# bytes. A `max_view_bytes` below this is a ceiling no result, not even an
# empty one, could ever satisfy (bot review finding).
EMPTY_VIEW_BYTES = 2

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

# The authentication-bearing subset of RESERVED_PARAMETERS. These names may
# not appear as target selector keys either: unlike "target"/"target_id",
# which are targeting words a selector could plausibly need, every name here
# denotes credential material, which belongs behind `credential_ref`.
_AUTHENTICATION_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "credential",
        "credential_ref",
        "headers",
        "password",
        "secret",
        "token",
    }
)

# A flat denylist cannot hold the invariant on its own: `X-Api-Key`,
# `access_token` and `proxy_authorization` are conventional authentication
# fields that no normalization of the names above would ever reach (bot
# review finding). Two rules cover the alias families without swallowing
# legitimate query parameters:
#
# * unambiguous credential words are matched against the name folded to
#   alphanumerics, so separators and capitalisation cannot hide them;
# * short words that are also ordinary English fragments are matched only as
#   whole tokens, split on separators and camelCase. "auth" as a token is an
#   authentication field; "auth" inside `author_filter` is not, and that
#   distinction is what a plain substring rule got wrong (caught by
#   test_ordinary_parameter_names_are_still_accepted).
#
# This is a rule about parameter *names*, not about prose: it does not
# contradict the deliberate decision (see ToolDescription) to keep
# secret-*value* detection a human-review question. A legitimate read-only
# query parameter has no business being named after a credential, and the
# refusal is a fixed-code registration error the operator sees immediately.
_AUTHENTICATION_SUBSTRINGS = (
    "accesskey",
    "accesstoken",
    "apikey",
    "authorisation",
    "authorization",
    "bearer",
    "certificate",
    "credential",
    "keypair",
    "keystore",
    "passwd",
    "password",
    "privatekey",
    "publickey",
    "secret",
    "signature",
    "sshkey",
    "token",
    "truststore",
)
_AUTHENTICATION_TOKENS = frozenset(
    {"auth", "cert", "cookie", "p12", "pem", "pfx", "session", "sig"}
)
_WORDS = re.compile(r"[A-Za-z][a-z0-9]*|[0-9]+")


def _endpoint_text(value: str) -> bool:
    """Whether this model-visible text carries a concrete endpoint.

    The three detectors live behind one predicate so every model-visible
    surface -- the five description fields, a parameter description and a
    parameter name -- applies exactly the same rule. Adding a form here covers
    all of them at once; adding it at one call site is how the earlier rounds
    kept leaving one surface behind.
    """
    return bool(
        _MODEL_VISIBLE_URI.search(value)
        or _PROTOCOL_RELATIVE_URI.search(value)
        or _SCHEMELESS_ENDPOINT.search(value)
    )


def _utf8_text(value: str) -> bool:
    """Whether this text can be encoded, i.e. can survive fingerprinting.

    A lone surrogate (``"x\ud800"``) is a valid ``str`` but not encodable, so
    it passed every registration check and then raised a raw
    ``UnicodeEncodeError`` out of ``canonical_hash(...).encode("utf-8")`` --
    bypassing this module's fixed-code ``ToolContractError`` contract and able
    to abort registry construction (bot review finding).
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _authentication_name(name: str) -> bool:
    folded = "".join(char for char in name.lower() if char.isalnum())
    if any(stem in folded for stem in _AUTHENTICATION_SUBSTRINGS):
        return True
    # Bare ``key`` is deliberately absent from both collections: `label_key`,
    # `group_by_key` and `partition_key` are ordinary query parameters for a
    # read-only metrics tool, so matching it would refuse legitimate
    # registrations. The credential families that *use* the word are matched
    # as whole compounds instead (``privatekey``/``accesskey``/``sshkey``/
    # ``keypair``/``keystore``). A denylist cannot be complete; what actually
    # holds the boundary is that secrets live behind ``credential_ref`` and
    # never in a model-proposable parameter at all.
    #
    # Tokenized twice, and case-folded first: ``_WORDS`` continues a token
    # only through lowercase characters, so tokenizing the original spelling
    # split ``AUTH`` into four single letters and let ``AUTH``/``AUTH_HEADER``/
    # ``COOKIE``/``SESSION``/``SIG`` through while their lowercase forms were
    # refused (bot review finding). The camelCase pass is kept as well, so
    # ``refreshAuth`` still matches while ``author_filter`` still does not.
    words = {word.lower() for word in _WORDS.findall(name)}
    words.update(part for part in re.split(r"[^a-z0-9]+", name.lower()) if part)
    return bool(words & _AUTHENTICATION_TOKENS)


# Outcome reasons a registration may map a source-reported status onto. The
# vocabulary is fixed so a data source cannot invent its own outcome class.
SOURCE_ERROR_REASONS = frozenset(
    {"INVALID_PARAMS", "SOURCE_ERROR", "SOURCE_UNAVAILABLE"}
)

_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{2,63}")
_HANDLE = re.compile(r"[a-z0-9][a-z0-9_-]{2,63}")
_VERSION = re.compile(r"[a-z0-9][a-z0-9.+-]{0,31}")
# URI schemes are case-insensitive (RFC 3986), and this pattern is used both
# to validate an endpoint and to *detect* one hiding in model-visible prose.
# Compiled case-sensitively, ``HTTPS://metrics.internal/api`` walked straight
# through the detection path (bot review finding).
_ENDPOINT = re.compile(
    r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=-]{1,512}", re.IGNORECASE
)

# The model-visible face is checked with a *generic* scheme detector, not the
# HTTP(S) endpoint validator above: `postgresql://user:pass@db.internal/x`,
# `grpc://metrics.internal:4317` and `wss://logs.internal/stream` are concrete
# endpoints too, and the first carries credentials straight into the text
# intended for the model (bot review finding). Section 8 forbids 凭据、认证信息
# or a concrete endpoint / base_url / 凭据句柄 there, whatever the scheme.
# `_ENDPOINT` keeps its narrower job: what a registered transport endpoint may
# actually be.
# A concrete endpoint does not need a scheme to be one: `metrics.internal:9090`,
# `prometheus:9090` and `[fd00::1]:4317` locate an internal target just as
# precisely (bot review findings). Three unambiguous forms are detected:
# bracketed IPv6, a dotted quad, and a hostname label followed by a numeric
# port -- the label must contain a letter, which is what keeps `12:30`,
# `5xx:2xx` and `section 3/4` out.
#
# Accepted cost: prose of the shape `step:30` is now refused as well. It is a
# registration-time refusal with a fixed code that the operator sees
# immediately and can rewrite ("a step of 30 seconds"), which is the
# fail-closed direction for a rule whose job is to keep internal target
# locations out of the model-visible face.
#
# Still *not* detected, deliberately: a bare hostname with no port. Matching
# one in free text would reject `config.yaml` and `v1.2.3` too, which is the
# keyword scanner this module has repeatedly declined to build; that case
# stays a human-review question.
_SCHEMELESS_ENDPOINT = re.compile(
    r"(?:"
    r"\[[0-9A-Fa-f:]{2,45}\]"  # bracketed IPv6
    r"|(?:[0-9]{1,3}\.){3}[0-9]{1,3}"  # dotted quad
    r"|\b(?:[A-Za-z0-9-]*[A-Za-z][A-Za-z0-9-]*)(?:\.[A-Za-z0-9-]+)*"  # host label
    r"):[0-9]{1,5}\b"
)

# The ``scheme://`` form is unambiguous, so what follows it is not restricted
# to an alphabet: `https://监控.内部/指标` is as concrete an endpoint as its
# ASCII equivalent, and an ASCII-only class let it through (bot review
# finding). Prose does not accidentally contain ``://``.
#
# The scheme-less detector above deliberately stays ASCII: a non-ASCII label
# followed by ``:`` and digits is indistinguishable from ordinary prose in
# this repository's own languages ("步骤:30"), and that rule has no ``://``
# anchor to lean on.
_MODEL_VISIBLE_URI = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S{1,512}")

# The protocol-relative form `//authority/path` needs neither a scheme nor a
# port, so it slipped past both detectors above -- including
# `//reader:secret@metrics.internal/api`, which carries inline credentials as
# well as the endpoint (bot review finding). `//` plus a *recognisable
# authority* is the anchor here, the same kind of unambiguous marker that
# justifies not restricting the alphabet after `://`; an authority is
# recognised only when it has a dot, a port, brackets or userinfo, so a bare
# `// TODO` comment or a `//shared/config` path stays ordinary prose.
_PROTOCOL_RELATIVE_URI = re.compile(
    # Userinfo alone is an unambiguous authority marker: `//reader:secret@prometheus/api`
    # carries both an inline credential and an internal target, and no ordinary
    # prose writes `//x@y` (bot review finding -- the previous pass made
    # userinfo optional but still required the *host* to be dotted, ported or
    # bracketed, so a single-label host with credentials slipped through).
    r"//[^\s/@]+@[A-Za-z0-9-]+"
    r"|//(?:[^\s/@]+@)?(?:"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}"
    r"|(?:[0-9]{1,3}\.){3}[0-9]{1,3}"
    r"|\[[0-9A-Fa-f:]{2,45}\]"
    r"|[A-Za-z0-9-]*[A-Za-z][A-Za-z0-9-]*:[0-9]{1,5}"
    r")"
)

# C3 section 8 requires `window_format`/`values_format` to carry a placeholder
# for the absolute window / value enumeration a future renderer fills in per
# Run (the L3a template / L3b instance split of section 5). The placeholder
# token itself is this module's choice, not quoted verbatim in C3; it mirrors
# the existing `{steps}` convention in the instruction-layer prototype so a
# later renderer can reuse the same `str.format`-style substitution.
WINDOW_PLACEHOLDER = "{window}"
VALUES_PLACEHOLDER = "{values}"

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
    try:
        encoded = canonical(value).encode("utf-8")
    except ValueError as exc:
        # ``UnicodeEncodeError`` is a ``ValueError``; so is the JSON encoder's
        # refusal to serialise an integer past the interpreter's digit limit
        # (`max_window_seconds=10**4300`). The previous pass guarded only the
        # encoding half and left the serialising half raising raw -- the same
        # "fix the instance, miss the class" mistake this choke point exists to
        # end. Anything that cannot be canonicalised is refused here, in the
        # contract's own error type (bot review findings).
        # The single choke point for encodability. Text that is a valid ``str``
        # but not encodable (a lone surrogate) reached this line from any
        # fingerprinted field -- parameter *names*, ``result_path`` elements,
        # ``error_classes`` keys, the incomplete marker -- and raised a raw
        # ``UnicodeEncodeError`` that escaped this module's fixed-code
        # contract and could abort registry construction. Validating fields one
        # at a time kept missing the next one (three rounds of bot review found
        # three different fields), so the guarantee is stated here instead:
        # fingerprinting either succeeds or raises ``ToolContractError``.
        raise ToolContractError(
            "INVALID_REGISTRATION_TEXT"
            if isinstance(exc, UnicodeEncodeError)
            else "INVALID_REGISTRATION_VALUE"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ParameterSpec:
    """One declared query parameter of a registered tool.

    ``description`` is the model-visible prose for this parameter (technical
    plan section 8: "工具还有一个模型可见面，即送进模型的 description 与参数
    描述"). It defaults to ``""`` so existing callers that only assert on
    ``kind``/``required`` behaviour are unaffected; a real registration should
    supply real text, but structural completeness of parameter prose is not
    one of the two section 7 checks this module implements (only the tool
    level :class:`ToolDescription` has a registration-time completeness
    check — see its docstring).
    """

    kind: ParameterKind
    required: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        if self.kind not in _PYTHON_KINDS or type(self.required) is not bool:
            raise ToolContractError("INVALID_PARAMETER_SPEC")
        if not isinstance(self.description, str):
            raise ToolContractError("INVALID_PARAMETER_SPEC")
        if not _utf8_text(self.description):
            raise ToolContractError("INVALID_PARAMETER_SPEC")
        if _endpoint_text(self.description):
            # Parameter prose is part of the same section 8 model-visible
            # face as :class:`ToolDescription`, which refuses a concrete
            # ``scheme://`` URL for the same reason; leaving this field
            # unchecked let a registration route an endpoint, a credential
            # handle URL or a URL-embedded token to the model through the
            # other half of that face (bot review finding). Same
            # deterministic rule, same limits: see ToolDescription's
            # docstring for why secret-*value* detection stays a
            # human-review question rather than a keyword scanner.
            raise ToolContractError("CREDENTIAL_MATERIAL_FORBIDDEN")

    def accepts(self, value: object) -> bool:
        if self.kind != "boolean" and type(value) is bool:
            return False  # bool is an int subclass; keep the kinds disjoint.
        return isinstance(value, _PYTHON_KINDS[self.kind])


@dataclass(frozen=True)
class ToolDescription:
    """The section 8 model-visible face of one tool: five structured fields.

    C3 section 8 (2026-09-15 addition) requires the model-visible
    ``description`` to be a structure, not free text, "以便注册期确定性校验":

    ========================  =====================================  ========================
    field                     content                                 registration check
    ========================  =====================================  ========================
    ``returns``               what comes back, its source, its shape  non-empty
    ``window_format``         where/how the absolute window appears   placeholder present
    ``values_format``         where/how the value enumeration appears placeholder present
    ``limits``                the result cap and truncation semantics non-empty
    ``cannot_prove``          what this result must not be read to    non-empty
                              prove
    ========================  =====================================  ========================

    Field declaration order matches the C3 table order verbatim, so a future
    renderer that concatenates these into the text sent to the model can walk
    ``dataclasses.fields(description)`` in this order without a separate
    ordering rule.

    Only *structural* completeness is checked here — per the independent
    review disposition F10 in the PR #27 task record
    (``docs/tasks/2026-09-15-instruction-tool-contract.md``), free-text
    *quality* (does ``cannot_prove`` actually name a real misreading?) has no
    registration-time judge and is a human-review question, same as any other
    prose in this codebase. Raising here is therefore always an operator
    error, consistent with this module's fixed-code ``ToolContractError``
    convention.

    One narrow, deterministic exception (bot review finding): section 8
    explicitly forbids "凭据、认证信息或具体 endpoint / base_url / 凭据句柄" in
    this model-visible face. A concrete ``scheme://...`` URL is syntactically
    detectable with the same pattern already used to reject one in
    :class:`RegisteredTarget`'s endpoint, so it is checked here too. A
    general credential/secret-*value* scanner is not added: unlike a URL,
    "does this text contain a credential" is not decidable from syntax alone
    (RESERVED_PARAMETERS' words like "endpoint"/"token"/"credential" are
    explicitly *not* transplanted onto description text per section 8 --
    they are often required prose, e.g. naming which endpoint a tool reads),
    so any keyword-based attempt would either miss real secrets or reject
    legitimate descriptions. That content judgment, like the rest of this
    dataclass's prose quality, is a human-review question at registration
    time, not a registration-time judge this class can make deterministically.
    """

    returns: str
    window_format: str
    values_format: str
    limits: str
    cannot_prove: str

    def __post_init__(self) -> None:
        for name in ("returns", "limits", "cannot_prove"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ToolContractError("EMPTY_TOOL_DESCRIPTION_FIELD")
        for name, placeholder in (
            ("window_format", WINDOW_PLACEHOLDER),
            ("values_format", VALUES_PLACEHOLDER),
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or placeholder not in value:
                raise ToolContractError("MISSING_DESCRIPTION_PLACEHOLDER")
        for name in (
            "returns",
            "window_format",
            "values_format",
            "limits",
            "cannot_prove",
        ):
            value = getattr(self, name)
            if isinstance(value, str) and not _utf8_text(value):
                raise ToolContractError("INVALID_DESCRIPTION_TEXT")
            if isinstance(value, str) and _endpoint_text(value):
                raise ToolContractError("CREDENTIAL_MATERIAL_FORBIDDEN")


@dataclass(frozen=True)
class ToolRegistration:
    """The section 8 registration contract for one read-only tool.

    Name, version, parameter schema, data source, absolute query window bound,
    request deadline, result-size ceiling, error classification and the
    incomplete-result marker are all declared here, so the executor never has
    to infer them from a response. ``description`` is the section 8
    model-visible face (see :class:`ToolDescription`) — the execution-side
    contract above and the model-visible face are both part of "the
    registration contract" per section 8, and both are covered by
    :attr:`ToolRegistry.revision`.

    ``may_contain_secrets`` is a required operator declaration about the raw
    source payload, not just the selected rows. Only literal ``False`` is
    accepted; missing declarations fail construction and unknown/true values
    are refused. This is a reviewed configuration assertion, not a scanner
    or a runtime redaction guarantee.
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
    description: ToolDescription
    may_contain_secrets: bool
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
        if type(self.may_contain_secrets) is not bool:
            raise ToolContractError("INVALID_SECRET_DECLARATION")
        if self.may_contain_secrets:
            raise ToolContractError("SECRET_BEARING_SOURCE_FORBIDDEN")
        if self.verb not in READ_ONLY_VERBS:
            raise ToolContractError("VERB_NOT_ALLOWED")
        if not isinstance(self.parameters, Mapping) or any(
            not isinstance(key, str) or not isinstance(spec, ParameterSpec)
            for key, spec in self.parameters.items()
        ):
            raise ToolContractError("INVALID_PARAMETER_SPEC")
        if any(_endpoint_text(key) for key in self.parameters):
            # Parameter keys become property names in the model-visible
            # schema, so the endpoint prohibition applies to them exactly as it
            # does to the prose beside them; checking only the descriptions
            # left `{"prometheus:9090": ...}` as an open route (bot review
            # finding).
            raise ToolContractError("CREDENTIAL_MATERIAL_FORBIDDEN")
        if RESERVED_PARAMETERS & {key.lower() for key in self.parameters} or any(
            _authentication_name(key) for key in self.parameters
        ):
            # Compared case-folded: reviewed configuration writing the
            # conventional ``Authorization``/``Token`` capitalisation used to
            # slip past this exact-case intersection, after which the model
            # could supply that declared field and the gateway forwarded it in
            # ``TransportRequest.params`` -- defeating the invariant this set
            # exists to hold (bot review finding).
            raise ToolContractError("RESERVED_PARAMETER")
        if not isinstance(self.description, ToolDescription):
            # ToolDescription.__post_init__ already asserted the five-field
            # structural completeness (section 7 check 3); this only refuses
            # a caller that skipped constructing one at all.
            raise ToolContractError("INVALID_TOOL_DESCRIPTION")
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
            or not EMPTY_VIEW_BYTES <= self.max_view_bytes <= self.max_result_bytes
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
        if "?" in self.endpoint or "#" in self.endpoint:
            # A query string or fragment could carry a credential baked
            # directly into the endpoint -- a query-auth token, an API key, a
            # presigned-URL signature -- bypassing the opaque credential_ref
            # indirection this dataclass otherwise enforces (bot review
            # finding). Request-time query values belong in
            # TransportRequest.params, never in the registered endpoint.
            raise ToolContractError("CREDENTIAL_MATERIAL_FORBIDDEN")
        if not isinstance(self.credential_ref, str) or not _HANDLE.fullmatch(
            self.credential_ref
        ):
            # Only an opaque handle may be stored here. Secret material stays
            # with the transport's credential provider, outside this package.
            raise ToolContractError("CREDENTIAL_MATERIAL_FORBIDDEN")
        if not isinstance(self.selector, Mapping) or any(
            not isinstance(key, str)
            or not isinstance(value, str)
            # Selector keys and values are fingerprinted too, so the same
            # non-encodable text would abort TargetRegistry construction with
            # a raw UnicodeEncodeError (same class as the description fields).
            or not _utf8_text(key)
            or not _utf8_text(value)
            for key, value in self.selector.items()
        ):
            raise ToolContractError("INVALID_SELECTOR")
        if any(
            key.lower() in _AUTHENTICATION_KEYS or _authentication_name(key)
            for key in self.selector
        ):
            # The selector is targeting metadata that flows verbatim into
            # ``TransportRequest.selector`` ("nothing secret"). Naming an
            # authentication field here would carry credential material past
            # the opaque ``credential_ref`` indirection this dataclass
            # enforces everywhere else -- the same bypass already refused for
            # endpoint userinfo, query strings and fragments (bot review
            # finding). Only the reserved authentication *names* are
            # refused, deterministically; whether an arbitrary value is a
            # secret is not decidable here, same limit as the description
            # rule above.
            raise ToolContractError("CREDENTIAL_MATERIAL_FORBIDDEN")
        if not isinstance(self.display_name, str):
            raise ToolContractError("INVALID_TARGET_IDENTITY")
        object.__setattr__(self, "selector", MappingProxyType(dict(self.selector)))


_Entry = TypeVar("_Entry")


class _FrozenIndex:
    """Shared read-only index behaviour for the two registries."""

    __slots__ = ("_entries", "_revision")

    def __init__(self, entries: Mapping[str, _Entry], fingerprint: object) -> None:
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
                {
                    "name": name,
                    "version": entries[name].version,
                    "source": entries[name].source,
                    "verb": entries[name].verb,
                    # Section 7 check 2: both the tool-layer face and each
                    # parameter's own description must be in the fingerprint,
                    # or a description edit would silently leave
                    # `tool_schema_revision` unchanged for a resumed Run.
                    "description": {
                        "returns": entries[name].description.returns,
                        "window_format": entries[name].description.window_format,
                        "values_format": entries[name].description.values_format,
                        "limits": entries[name].description.limits,
                        "cannot_prove": entries[name].description.cannot_prove,
                    },
                    "parameters": {
                        key: {
                            "kind": spec.kind,
                            "required": spec.required,
                            "description": spec.description,
                        }
                        for key, spec in entries[name].parameters.items()
                    },
                    "result_path": entries[name].result_path,
                    "request_timeout_seconds": entries[name].request_timeout_seconds,
                    "max_result_bytes": entries[name].max_result_bytes,
                    "max_view_bytes": entries[name].max_view_bytes,
                    "max_window_seconds": entries[name].max_window_seconds,
                    "error_classes": dict(entries[name].error_classes),
                    "incomplete_marker": entries[name].incomplete_marker,
                    "read_only": entries[name].read_only,
                    "may_contain_secrets": entries[name].may_contain_secrets,
                }
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
        value = self._entries.get(name)
        return value if isinstance(value, ToolRegistration) else None


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
                    entries[target_id].credential_ref,
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
        value = self._entries.get(target_id)
        return value if isinstance(value, RegisteredTarget) else None
