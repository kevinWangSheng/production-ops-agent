"""Tool and target registration contract (technical plan section 8).

The registration contract is what lets the executor refuse before it queries:
name, version, parameter schema, data source, absolute window bound, request
deadline, result-size ceiling, error classification and incomplete marker are
all declared up front by the operator, never inferred from a response.
"""

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from opspilot.tools import (
    FORBIDDEN_VERBS,
    MAX_REQUEST_TIMEOUT_SECONDS,
    MAX_RESULT_BYTES,
    RESERVED_PARAMETERS,
    ParameterSpec,
    QueryScope,
    TargetRegistry,
    ToolContractError,
    ToolDescription,
    ToolRegistry,
    Window,
)
from tests.m1_tool_support import (
    WINDOW_END,
    WINDOW_START,
    description,
    registration,
    target,
)


def test_every_forbidden_verb_is_refused_at_registration():
    for verb in sorted(FORBIDDEN_VERBS):
        with pytest.raises(ToolContractError, match="WRITE_CAPABILITY_FORBIDDEN"):
            registration(verb=verb)


def test_read_only_flag_cannot_be_turned_off():
    with pytest.raises(ToolContractError, match="WRITE_CAPABILITY_FORBIDDEN"):
        registration(read_only=False)


def test_sources_that_may_return_secrets_are_refused_at_registration():
    with pytest.raises(ToolContractError, match="SECRET_BEARING_SOURCE_FORBIDDEN"):
        registration(may_contain_secrets=True)


def test_secret_bearing_declaration_is_boolean():
    with pytest.raises(ToolContractError, match="INVALID_SECRET_DECLARATION"):
        registration(may_contain_secrets="unknown")


@pytest.mark.parametrize("verb", ["", "GET", "mutate", "anything"])
def test_verb_must_come_from_the_read_only_allowlist(verb):
    with pytest.raises(ToolContractError, match="VERB_NOT_ALLOWED"):
        registration(verb=verb)


@pytest.mark.parametrize("name", sorted(RESERVED_PARAMETERS))
def test_gateway_owned_parameters_cannot_be_declared(name):
    with pytest.raises(ToolContractError, match="RESERVED_PARAMETER"):
        registration(parameters={name: ParameterSpec("string")})


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"request_timeout_seconds": MAX_REQUEST_TIMEOUT_SECONDS + 0.5}, "REQUEST_TIM"),
        ({"request_timeout_seconds": 0}, "REQUEST_TIM"),
        ({"max_result_bytes": MAX_RESULT_BYTES + 1}, "RESULT_LIMIT_OUT_OF_RANGE"),
        ({"max_result_bytes": 0}, "RESULT_LIMIT_OUT_OF_RANGE"),
        ({"max_view_bytes": 8192}, "VIEW_LIMIT_OUT_OF_RANGE"),
        ({"max_view_bytes": 0}, "VIEW_LIMIT_OUT_OF_RANGE"),
        ({"max_view_bytes": 1}, "VIEW_LIMIT_OUT_OF_RANGE"),
        ({"max_window_seconds": 0}, "WINDOW_LIMIT_OUT_OF_RANGE"),
    ],
)
def test_frozen_m1_01_ceilings_are_enforced_by_construction(overrides, code):
    """``max_view_bytes`` in (0, 1) is a bot review finding: ``_fit_rows()``
    always counts the two enclosing bytes of an empty JSON array `[]`, so a
    budget below that floor is a ceiling no result -- not even an empty one
    -- could ever satisfy.
    """

    with pytest.raises(ToolContractError, match=code):
        registration(**overrides)


def test_error_classes_may_only_map_onto_fixed_reasons():
    with pytest.raises(ToolContractError, match="INVALID_ERROR_CLASSES"):
        registration(error_classes={"503": "HEALTHY"})
    assert registration().classify("503") == "SOURCE_UNAVAILABLE"
    assert registration().classify("418") == "SOURCE_ERROR"


def test_result_path_and_incomplete_marker_must_be_usable():
    with pytest.raises(ToolContractError, match="INVALID_RESULT_PATH"):
        registration(result_path=["data"])
    with pytest.raises(ToolContractError, match="INVALID_INCOMPLETE_MARKER"):
        registration(incomplete_marker="")


# --- C3 section 7 check 3: ToolDescription five-field structural completeness
# ---------------------------------------------------------------------------
# Only structure is asserted here (a value is present / a placeholder is
# present), never prose quality — see ToolDescription's docstring and the
# independent review disposition F10 it cites.


@pytest.mark.parametrize("field_name", ["returns", "limits", "cannot_prove"])
@pytest.mark.parametrize("blank", ["", "   "])
def test_tool_description_required_fields_reject_blank_text(field_name, blank):
    with pytest.raises(ToolContractError, match="EMPTY_TOOL_DESCRIPTION_FIELD"):
        description(**{field_name: blank})


@pytest.mark.parametrize("field_name", ["window_format", "values_format"])
def test_tool_description_format_fields_require_their_placeholder(field_name):
    with pytest.raises(ToolContractError, match="MISSING_DESCRIPTION_PLACEHOLDER"):
        description(**{field_name: "no placeholder token in this sentence"})


def test_tool_description_placeholder_check_requires_the_exact_token():
    # A near-miss (wrong bracket, wrong/singular name) must not satisfy the
    # check: it is a literal substring match on the exact token, not "looks
    # like some placeholder".
    with pytest.raises(ToolContractError, match="MISSING_DESCRIPTION_PLACEHOLDER"):
        description(window_format="the window goes at (window)")
    with pytest.raises(ToolContractError, match="MISSING_DESCRIPTION_PLACEHOLDER"):
        description(values_format="the values go at {value}")


def test_tool_description_accepts_well_formed_fields():
    described = description()
    assert described.returns and described.limits and described.cannot_prove


@pytest.mark.parametrize(
    "field_name",
    ["returns", "window_format", "values_format", "limits", "cannot_prove"],
)
def test_tool_description_may_not_embed_a_concrete_endpoint(field_name):
    """Bot review finding: section 8 explicitly forbids a concrete
    endpoint/base_url (among other credential material) in the model-visible
    face, but the structural checks only asserted non-emptiness and
    placeholder presence -- a registration with a real URL baked into any of
    the five fields was accepted and would flow into the tool registry
    contract a future renderer sends to the model.
    """

    tainted = "See https://metrics.internal:9090/api/v1/query for details"
    if field_name == "window_format":
        tainted += " {window}"
    elif field_name == "values_format":
        tainted += " {values}"

    with pytest.raises(ToolContractError, match="CREDENTIAL_MATERIAL_FORBIDDEN"):
        description(**{field_name: tainted})


def test_tool_description_may_still_name_endpoints_and_credentials_by_word():
    # Section 8 explicitly does NOT transplant RESERVED_PARAMETERS-style
    # words onto description text: "endpoint"/"token"/"credential" as plain
    # words remain legitimate, required prose (e.g. naming which endpoint a
    # tool reads). Only a concrete scheme://... URL is rejected.
    described = description(
        returns=(
            "The value returned by the metrics endpoint; no token or "
            "credential is ever included in the response."
        )
    )
    assert "endpoint" in described.returns and "credential" in described.returns
    assert "{window}" in described.window_format
    assert "{values}" in described.values_format


def test_tool_description_field_names_and_order_match_c3_table():
    # Field names and declaration order must match the C3 section 8 table
    # verbatim (returns / window_format / values_format / limits /
    # cannot_prove), so a future renderer can rely on it without a separate
    # ordering rule.
    described = ToolDescription(
        returns="r",
        window_format="w {window}",
        values_format="v {values}",
        limits="l",
        cannot_prove="c",
    )
    assert [f.name for f in dataclasses.fields(described)] == [
        "returns",
        "window_format",
        "values_format",
        "limits",
        "cannot_prove",
    ]


def test_tool_description_is_immutable():
    described = description()
    with pytest.raises(dataclasses.FrozenInstanceError):
        described.returns = "different"


def test_registration_requires_a_structurally_complete_tool_description():
    # A blank required field never reaches ToolRegistration at all: building
    # `description(returns="")` itself raises inside ToolDescription's own
    # __post_init__ (see test_tool_description_required_fields_reject_blank_text),
    # before `registration(...)` is ever called. What ToolRegistration must
    # guard on its own is a caller skipping ToolDescription entirely.
    with pytest.raises(ToolContractError, match="INVALID_TOOL_DESCRIPTION"):
        registration(description=None)
    with pytest.raises(ToolContractError, match="INVALID_TOOL_DESCRIPTION"):
        registration(description={"returns": "not a ToolDescription instance"})


# C3 section 7 check 2 (fingerprint covers the tool-layer and parameter-layer
# description) is exercised end to end in
# tests/test_m1_tool_registry_binding.py::CONTRACT_CHANGES — both that the
# revision changes and that an old scope built on the pre-change registry is
# denied before any transport call.


def test_registration_and_its_parameter_map_are_immutable():
    declared = registration()
    with pytest.raises(dataclasses.FrozenInstanceError):
        declared.verb = "exec"
    with pytest.raises(TypeError):
        declared.parameters["injected"] = ParameterSpec("string")


def test_parameter_kinds_stay_disjoint():
    assert ParameterSpec("integer").accepts(5)
    assert not ParameterSpec("integer").accepts(True)
    assert not ParameterSpec("integer").accepts(1.5)
    assert ParameterSpec("number").accepts(1.5)
    assert not ParameterSpec("string").accepts(b"bytes")
    with pytest.raises(ToolContractError, match="INVALID_PARAMETER_SPEC"):
        ParameterSpec("anything")


def test_duplicate_registrations_are_refused():
    with pytest.raises(ToolContractError, match="DUPLICATE_TOOL"):
        ToolRegistry([registration(), registration()])
    with pytest.raises(ToolContractError, match="DUPLICATE_TARGET"):
        TargetRegistry([target(), target()])


def test_registry_revision_is_content_addressed():
    first = TargetRegistry([target()])
    same = TargetRegistry([target()])
    other = TargetRegistry([target(endpoint="https://metrics.internal:9091")])
    assert first.revision == same.revision
    assert first.revision != other.revision


@pytest.mark.parametrize(
    "credential_ref",
    [
        "https://reader:inline-material@metrics.internal",  # userinfo
        "Bearer inline-material",  # a header value
        "x" * 65,  # an opaque blob
        "",  # nothing at all
    ],
)
def test_targets_may_only_hold_an_opaque_credential_handle(credential_ref):
    with pytest.raises(ToolContractError, match="CREDENTIAL_MATERIAL_FORBIDDEN"):
        target(credential_ref=credential_ref)


def test_endpoint_may_not_carry_userinfo():
    with pytest.raises(ToolContractError, match="CREDENTIAL_MATERIAL_FORBIDDEN"):
        target(endpoint="https://reader:inline-material@metrics.internal:9090")


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://metrics.internal/api?token=secret",
        "https://metrics.internal/api?sig=presigned-signature&expires=123",
        "https://metrics.internal/api#fragment-token",
    ],
)
def test_endpoint_may_not_carry_a_query_string_or_fragment(endpoint):
    """Bot review finding: only the userinfo component was rejected. A
    query-auth or presigned-URL style endpoint (``?token=...``, ``?sig=...``)
    passed the regex and the userinfo check untouched, storing a secret
    directly in ``RegisteredTarget.endpoint`` -- and from there in every
    ``TransportRequest.endpoint`` -- bypassing the opaque ``credential_ref``
    indirection this dataclass otherwise enforces entirely. Request-time
    query values belong in ``TransportRequest.params``, never baked into the
    registered endpoint.
    """
    with pytest.raises(ToolContractError, match="CREDENTIAL_MATERIAL_FORBIDDEN"):
        target(endpoint=endpoint)


def test_targets_resolve_only_by_registered_identity():
    registry = TargetRegistry(
        [
            target(target_id="checkout-prod", display_name="checkout"),
            target(target_id="checkout-staging", display_name="checkout"),
        ]
    )
    assert registry.resolve("checkout-prod").target_id == "checkout-prod"
    assert registry.resolve("checkout-staging").target_id == "checkout-staging"
    # A display name, an endpoint or anything else resolves to nothing.
    assert registry.resolve("checkout") is None
    assert registry.resolve("https://metrics.internal:9090") is None
    assert registry.resolve(None) is None
    assert not [name for name in dir(registry) if "name" in name.lower()]


def test_target_credential_binding_is_part_of_registry_revision():
    first = TargetRegistry([target(credential_ref="prom-ro-checkout")])
    second = TargetRegistry([target(credential_ref="prom-ro-other")])

    assert first.revision != second.revision


def test_registries_expose_no_mutation_surface():
    targets = TargetRegistry([target()])
    tools = ToolRegistry([registration()])
    public = {name for name in dir(targets) if not name.startswith("_")}
    assert public == {"resolve", "revision", "target_ids"}
    assert {name for name in dir(tools) if not name.startswith("_")} == {
        "lookup",
        "revision",
        "tool_names",
    }
    with pytest.raises(AttributeError):
        targets.entries = {}
    resolved = targets.resolve("checkout-prod")
    with pytest.raises(dataclasses.FrozenInstanceError):
        resolved.endpoint = "https://attacker.example"
    with pytest.raises(TypeError):
        resolved.selector["namespace"] = "other"


def _scope(**overrides):
    fields = {
        "scope_id": "scope-1",
        "subject_kind": "incident",
        "subject_id": "incident-42",
        "run_id": "run-9",
        "control_generation": 7,
        "registry_revision": "abc",
        "tool_registry_revision": "def",
        "target_ids": frozenset({"checkout-prod"}),
        "tool_names": frozenset({"metrics.range_query"}),
        "window": Window(WINDOW_START, WINDOW_END),
        "deadline": WINDOW_END + timedelta(hours=1),
    }
    fields.update(overrides)
    return QueryScope(**fields)


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"max_operations": 21}, "OPERATION_BUDGET_OUT_OF_RANGE"),
        ({"max_operations": 0}, "OPERATION_BUDGET_OUT_OF_RANGE"),
        ({"max_tool_seconds": 241}, "TIME_BUDGET_OUT_OF_RANGE"),
        ({"subject_kind": "release"}, "INVALID_SUBJECT_KIND"),
        ({"control_generation": -1}, "INVALID_CONTROL_GENERATION"),
        ({"deadline": datetime(2026, 9, 14, 2)}, "INVALID_DEADLINE"),
        ({"registry_revision": ""}, "INVALID_SCOPE"),
        ({"tool_registry_revision": ""}, "INVALID_SCOPE"),
        ({"tool_registry_revision": None}, "INVALID_SCOPE"),
        ({"target_ids": {"checkout-prod"}}, "INVALID_SCOPE_NAMES"),
    ],
)
def test_scope_refuses_authorizations_outside_the_frozen_contract(overrides, code):
    with pytest.raises(ToolContractError, match=code):
        _scope(**overrides)


def test_scope_normalizes_its_deadline_to_utc():
    offset = timezone(timedelta(hours=8))
    scope = _scope(deadline=datetime(2026, 9, 14, 10, tzinfo=offset))
    assert scope.deadline == datetime(2026, 9, 14, 2, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "value",
    [
        None,
        {"last": "5m"},
        {"start": "2026-09-14T00:00:00", "end": "2026-09-14T01:00:00"},
        {"start": "2026-09-14", "end": "2026-09-15"},
        {"start": "2026-09-14T01:00:00Z", "end": "2026-09-14T00:00:00Z"},
        {"start": "2026-09-14T00:00:00Z", "end": "2026-09-14T00:00:00Z"},
        {"start": "not-a-time", "end": "2026-09-14T00:00:00Z"},
        {"start": 1, "end": 2},
    ],
)
def test_only_an_absolute_aware_window_parses(value):
    assert Window.parse(value) is None


def test_absolute_window_parses_and_normalizes():
    window = Window.parse(
        {"start": "2026-09-14T08:00:00+08:00", "end": "2026-09-14T01:30:00Z"}
    )
    assert window == Window(
        WINDOW_START, datetime(2026, 9, 14, 1, 30, tzinfo=timezone.utc)
    )
    assert window.seconds == 5400
    assert Window(WINDOW_START, WINDOW_END).contains(
        Window(WINDOW_START, datetime(2026, 9, 14, 0, 30, tzinfo=timezone.utc))
    )
    assert not Window(WINDOW_START, WINDOW_END).contains(
        Window(WINDOW_START, datetime(2026, 9, 14, 1, 30, tzinfo=timezone.utc))
    )


def test_registered_target_identity_is_validated():
    with pytest.raises(ToolContractError, match="INVALID_TARGET_IDENTITY"):
        target(target_id="UPPER")
    with pytest.raises(ToolContractError, match="INVALID_ENDPOINT"):
        target(endpoint="ftp://metrics.internal")
    with pytest.raises(ToolContractError, match="INVALID_SELECTOR"):
        target(selector={"namespace": 1})


def test_missing_secret_declaration_cannot_register_a_source():
    from opspilot.tools import ToolRegistration

    fields = {
        f.name: getattr(registration(), f.name)
        for f in dataclasses.fields(registration())
    }
    fields.pop("may_contain_secrets")
    with pytest.raises(TypeError, match="may_contain_secrets"):
        ToolRegistration(**fields)


@pytest.mark.parametrize(
    "text",
    [
        "the cluster read from https://metrics.internal:9090/api",
        "pass the handle issued by https://vault.internal/creds/reader",
    ],
)
def test_a_parameter_description_may_not_carry_a_concrete_endpoint(text):
    """Bot review finding: `ToolDescription`'s five fields reject a concrete
    `scheme://` URL, but `ParameterSpec.description` is the *same* section 8
    model-visible face (its own docstring says so) and was checked only for
    being a string. A registration could therefore put an endpoint, a
    credential handle URL or a URL-embedded token into a parameter
    description and have `ToolRegistry` accept and fingerprint it, and a
    renderer would send it to the model -- exactly what section 8 forbids.

    This is the same deterministic URL rule, not a keyword scanner: the
    reasoning in `ToolDescription`'s docstring about why secret-*value*
    detection stays a human-review question applies here unchanged.
    """
    with pytest.raises(ToolContractError, match="CREDENTIAL_MATERIAL_FORBIDDEN"):
        ParameterSpec(kind="string", description=text)


def test_a_parameter_description_may_still_name_a_reserved_word():
    """The URL rule must not become a keyword scanner: naming which endpoint
    or token a parameter refers to is legitimate required prose (see
    `ToolDescription`'s docstring), so only concrete `scheme://` material is
    refused.
    """
    spec = ParameterSpec(
        kind="string", description="which endpoint alias to read; not a token"
    )
    assert spec.description.startswith("which endpoint")


@pytest.mark.parametrize(
    "selector",
    [
        {"authorization": "Bearer inline-material"},
        {"token": "inline-material"},
        {"api_key": "inline-material"},
        {"password": "inline-material"},
        {"credential": "inline-material"},
        {"headers": "authorization: Bearer inline-material"},
    ],
)
def test_target_selectors_may_not_carry_authentication_material(selector):
    """Bot review finding: `selector` was validated only as a str->str
    mapping, so reviewed target configuration could put `authorization` or
    `token` in it. `RegisteredTarget` otherwise enforces that secret material
    stays behind the opaque `credential_ref` -- it rejects endpoint userinfo,
    query strings and fragments for exactly this reason -- and the selector
    flows straight into `TransportRequest.selector`, whose docstring claims
    it holds "nothing secret". The reserved authentication names are the
    deterministic part of that rule, the same list the gateway already
    refuses to let a registration declare as a parameter.
    """
    with pytest.raises(ToolContractError, match="CREDENTIAL_MATERIAL_FORBIDDEN"):
        target(selector=selector)


def test_target_selectors_still_accept_plain_targeting_metadata():
    entry = target(selector={"cluster": "prod-1", "table": "orders"})
    assert dict(entry.selector) == {"cluster": "prod-1", "table": "orders"}


@pytest.mark.parametrize(
    "text",
    [
        "read from HTTPS://metrics.internal:9090/api",
        "read from Http://metrics.internal/api",
    ],
)
def test_a_model_visible_url_is_detected_whatever_the_scheme_case(text):
    """Bot review finding: URI schemes are case-insensitive (RFC 3986) but the
    detector was compiled case-sensitively, so `HTTPS://metrics.internal/api`
    walked straight through the endpoint-leak check that the earlier
    description fix was supposed to close.
    """
    with pytest.raises(ToolContractError, match="CREDENTIAL_MATERIAL_FORBIDDEN"):
        ParameterSpec(kind="string", description=text)
    with pytest.raises(ToolContractError, match="CREDENTIAL_MATERIAL_FORBIDDEN"):
        description(returns=text)


@pytest.mark.parametrize("name", ["Authorization", "TOKEN", "Api_Key", "Headers"])
def test_reserved_parameter_names_are_matched_case_insensitively(name):
    """Bot review finding: `RESERVED_PARAMETERS` holds lowercase names and the
    check was an exact set intersection, so reviewed configuration using the
    conventional capitalisation declared the field successfully -- after which
    the model could supply it and the gateway forwarded it in
    `TransportRequest.params`, defeating the invariant the set exists for.
    """
    with pytest.raises(ToolContractError, match="RESERVED_PARAMETER"):
        registration(parameters={name: ParameterSpec(kind="string")})


@pytest.mark.parametrize(
    "text",
    [
        "rows come from postgresql://reader:inline-material@db.internal/metrics",
        "the collector at grpc://metrics.internal:4317",
        "streamed over wss://logs.internal/stream",
        "see file:///etc/opspilot/targets.yaml",
    ],
)
def test_model_visible_prose_rejects_every_concrete_uri_scheme(text):
    """Bot review finding: the detector recognised HTTP(S) only, so a
    `postgresql://user:pass@host/db` in the model-visible face carried
    credentials straight into the text intended for the model, and
    `grpc://`/`wss://` endpoints passed untouched. Section 8's prohibition is
    about concrete endpoints and credential material, not about one scheme.
    """
    with pytest.raises(ToolContractError, match="CREDENTIAL_MATERIAL_FORBIDDEN"):
        ParameterSpec(kind="string", description=text)
    with pytest.raises(ToolContractError, match="CREDENTIAL_MATERIAL_FORBIDDEN"):
        description(returns=text)


def test_model_visible_prose_still_accepts_ordinary_text():
    """The generic detector must key on a real `scheme://`, not on punctuation:
    ordinary prose with colons and slashes stays legitimate.
    """
    spec = ParameterSpec(
        kind="string",
        description="ratio of 5xx:2xx over the window; see runbook section 3/4",
    )
    assert spec.description.startswith("ratio")


@pytest.mark.parametrize(
    "name",
    ["X-Api-Key", "access_token", "proxy_authorization", "refreshToken", "Auth"],
)
def test_authentication_parameter_aliases_are_refused(name):
    """Bot review finding: normalising case cannot reject names that are simply
    absent from `RESERVED_PARAMETERS`. `X-Api-Key`, `access_token` and
    `proxy_authorization` are conventional authentication fields; declaring one
    let the model supply the credential-bearing value, which `_run()` then
    forwarded in `TransportRequest.params`.
    """
    with pytest.raises(ToolContractError, match="RESERVED_PARAMETER"):
        registration(parameters={name: ParameterSpec(kind="string")})


@pytest.mark.parametrize("name", ["expr", "step_seconds", "author_filter", "cluster"])
def test_ordinary_parameter_names_are_still_accepted(name):
    """The stem rule must not swallow legitimate query parameters -- including
    `author_filter`, which contains "auth" only as a substring of a word.
    """
    entry = registration(parameters={name: ParameterSpec(kind="string")})
    assert name in entry.parameters


@pytest.mark.parametrize("name", ["AUTH", "AUTH_HEADER", "COOKIE", "SESSION", "SIG"])
def test_uppercase_authentication_names_are_refused_like_their_lowercase_forms(name):
    """Bot review finding: `_WORDS` continues a token only through lowercase
    characters, so tokenizing the original spelling split `AUTH` into four
    single letters and let the all-caps spellings through while `auth`,
    `cookie`, `session` and `sig` were refused.
    """
    with pytest.raises(ToolContractError, match="RESERVED_PARAMETER"):
        registration(parameters={name: ParameterSpec(kind="string")})
    with pytest.raises(ToolContractError, match="CREDENTIAL_MATERIAL_FORBIDDEN"):
        target(selector={name: "x"})


def test_camel_case_authentication_names_are_still_refused():
    """The case-folded pass must not lose the camelCase one."""
    with pytest.raises(ToolContractError, match="RESERVED_PARAMETER"):
        registration(parameters={"refreshAuth": ParameterSpec(kind="string")})


def test_non_encodable_description_text_is_refused_at_registration():
    """Bot review finding: a lone surrogate is a valid `str` but not encodable,
    so it passed every registration check and then raised a raw
    `UnicodeEncodeError` out of `canonical_hash(...).encode("utf-8")`,
    bypassing this module's fixed-code contract and able to abort registry
    construction.
    """
    with pytest.raises(ToolContractError, match="INVALID_DESCRIPTION_TEXT"):
        description(returns="x\ud800")
    with pytest.raises(ToolContractError, match="INVALID_PARAMETER_SPEC"):
        ParameterSpec(kind="string", description="x\ud800")
    # Same class one field over: selector text is fingerprinted too.
    with pytest.raises(ToolContractError, match="INVALID_SELECTOR"):
        target(selector={"cluster": "x\ud800"})
    with pytest.raises(ToolContractError, match="INVALID_SELECTOR"):
        target(selector={"x\ud800": "prod-1"})


def test_registries_build_from_every_accepted_registration():
    """The property behind the rule: whatever registration is accepted can be
    fingerprinted without raising outside the ToolContractError contract.
    """
    entry = registration(
        parameters={"expr": ParameterSpec(kind="string", description="PromQL 表达式")}
    )
    assert ToolRegistry([entry]).revision
    assert TargetRegistry([target(selector={"cluster": "生产-1"})]).revision
