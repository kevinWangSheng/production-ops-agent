"""Independent r11/r9 request-boundary acceptance; fake transports only.

No implementation internals or private validators are used. PR-A ledger input
is restricted to evidence views; private ledger containers are never sent.
"""

from __future__ import annotations

import json
from dataclasses import is_dataclass, replace
from pathlib import Path

import pytest
from eval_b3_support import (
    MODELS,
    FakeTransport,
    NetworkGuard,
    build_world,
    env_text,
    http,
    judge_input,
    load,
    make_pack,
    transports,
    write_env,
)
from eval_contract_support import EID, SCOPE

from opspilot.evaluation.calibration_flow import (
    load_verified_calibration,
    run_calibration,
)
from opspilot.evaluation.judge import judge_once
from opspilot.tools.registry import canonical_hash


@pytest.fixture(scope="module", autouse=True)
def offline():
    guard = NetworkGuard()
    with pytest.MonkeyPatch.context() as mp:
        guard.install(mp)
        yield
    assert guard.attempts == []


@pytest.fixture(scope="module")
def credentials(offline, tmp_path_factory):
    path = write_env(tmp_path_factory.mktemp("r11-env"), env_text())
    return {provider: load(path, provider) for provider in MODELS}


@pytest.fixture(scope="module")
def verified(credentials, tmp_path_factory):
    world = build_world()
    pack = make_pack(world)
    destination = tmp_path_factory.mktemp("r11-proof") / "selected"
    selected = run_calibration(
        pack, configs=credentials, transports=transports(world), destination=destination
    )
    assert selected.selected_provider == "zai"
    return load_verified_calibration(destination, pack=pack, config=credentials["zai"])


@pytest.fixture(params=[False, True], ids=["unauthenticated", "authenticated"])
def boundary(request, credentials):
    proof = request.getfixturevalue("verified") if request.param else None

    def call(value):
        # Response validity is irrelevant: the contract under test is pre-send.
        transport = FakeTransport(lambda _: http(b"{}"))
        observation = judge_once(
            value, config=credentials["zai"], calibration=proof, transport=transport
        )
        return observation, transport

    return call


def changed(value, path, replacement):
    """Copy public DTOs/JSON without mutating shared fixtures."""
    if not path:
        return replacement
    key, *tail = path
    child = getattr(value, key) if is_dataclass(value) else value[key]
    new = changed(child, tail, replacement)
    if is_dataclass(value):
        return replace(value, **{key: new})
    if isinstance(value, dict):
        return {**value, key: new}
    items = list(value)
    items[key] = new
    return tuple(items) if isinstance(value, tuple) else items


def mutate(path, value):
    request = judge_input()
    if path[0] == "raw":
        doc = changed(json.loads(request.report.raw), path[1:], value)
        return replace(request, report=replace(request.report, raw=json.dumps(doc)))
    if path == ("delivered-key",):
        return replace(request, delivered={value: request.delivered[EID]})
    if path == ("authorized-key",):
        return replace(request, authorized={value: request.authorized[SCOPE]})
    if path[:3] == ("delivered", EID, "view"):
        record = request.delivered[EID]
        view = changed(record.view, path[3:], value)
        return replace(
            request,
            delivered={
                EID: replace(record, view=view, view_sha256=canonical_hash(view))
            },
        )
    return changed(request, path, value)


# Each category exercises all six malformed-input families independently.
FIELDS = [
    pytest.param(("spec", "version"), id="class2-spec-version"),
    pytest.param(("spec", "incident", "subject_id"), id="class2-incident-subject"),
    pytest.param(("answer", "injected", "service"), id="class2-injected-service"),
    pytest.param(
        ("answer", "injected", "service_aliases", 0), id="class2-service-alias"
    ),
    pytest.param(("answer", "signature", "rule_id"), id="class2-rule"),
    pytest.param(("delivered-key",), id="class3-delivery-key"),
    pytest.param(("authorized-key",), id="class3-window-key"),
    pytest.param(("delivered", EID, "target_id"), id="class3-target"),
    pytest.param(("delivered", EID, "view", "tool"), id="class3-projection-tool"),
    pytest.param(
        ("delivered", EID, "view", "content", 0, "service"), id="class3-span-service"
    ),
    pytest.param(("raw", "claims", 0, "evidence_ids", 0), id="class4-raw-evidence"),
    pytest.param(("raw", "claims", 0, "target_refs", 0), id="class4-raw-target"),
    pytest.param(("raw", "claims", 0, "time_scope_ref"), id="class4-raw-window"),
    pytest.param(("outcome", "actions", 0), id="class4-outcome-action"),
    pytest.param(("outcome", "permissions", 0), id="class4-outcome-permission"),
]
BAD = [
    pytest.param(" cart ", id="edge-whitespace"),
    pytest.param("ca rt", id="internal-whitespace"),
    pytest.param("cart\x00", id="control"),
    pytest.param("ｃａｒｔ", id="fullwidth"),
    pytest.param("x" * 257, id="overlong"),
    pytest.param("cart!", id="illegal-character"),
]


def assert_rejected(boundary, value):
    observation, transport = boundary(value)
    # One assertion reports every required observable, even on an early failure.
    assert (
        observation.result.status,
        observation.error_code,
        len(transport.calls),
        b"".join(call.body for call in transport.calls),
    ) == ("invalid", "SCHEMA_INVALID", 0, b"")


@pytest.mark.parametrize("path", FIELDS)
@pytest.mark.parametrize("value", BAD)
def test_noncanonical_identifier_never_sends(boundary, path, value):
    assert_rejected(boundary, mutate(path, value))


OPERATIONS = [
    ("answer", "injected", "operation"),
    ("answer", "injected", "operation_aliases", 0),
    ("answer", "signature", "any_of", 0, "operation"),
    ("delivered", EID, "view", "content", 0, "operation"),
]


@pytest.mark.parametrize("path", OPERATIONS)
@pytest.mark.parametrize(
    "value",
    [
        "HTTP POST",
        "GET /api/products/{productId}",
        "executing api route (pages) /api/products/[productId]",
        "{closure}",
    ],
)
def test_operation_spaces_and_route_brackets_pass(boundary, path, value):
    _, transport = boundary(mutate(path, value))
    assert len(transport.calls) == 1
    assert transport.calls[0].body


@pytest.mark.parametrize("path", OPERATIONS)
@pytest.mark.parametrize(
    "value",
    [
        "HTTP  POST",
        "HTTP\tPOST",
        " HTTP POST",
        "HTTP POST ",
        "HTTP\nPOST",
        "ＧＥＴ /api",
        "x" * 257,
        "GET /api?x",
    ],
)
def test_noncanonical_operations_never_send(boundary, path, value):
    assert_rejected(boundary, mutate(path, value))


@pytest.mark.parametrize(
    "field",
    [
        "evidence_id",
        "operation_id",
        "run_id",
        "subject_id",
        "target_id",
        "trace_id",
        "span_id",
        "parent_span_id",
        "service_version",
    ],
)
def test_optional_projection_reference_null_passes(boundary, field):
    request = judge_input()
    record = request.delivered[EID]
    view = {**record.view, "content": [{**record.view["content"][0], field: None}]}
    request = replace(
        request,
        delivered={EID: replace(record, view=view, view_sha256=canonical_hash(view))},
    )
    _, transport = boundary(request)
    assert len(transport.calls) == 1
    assert transport.calls[0].body


def test_report_time_scope_null_passes(boundary):
    request = judge_input()
    doc = json.loads(request.report.raw)
    # A hypothesis has no factual citation/window obligation. Keep the original
    # fact intact so this control isolates the nullable schema field.
    doc["claims"].append(
        {
            "kind": "hypothesis",
            "text": "A possible explanation.",
            "evidence_ids": [],
            "target_refs": [],
            "time_scope_ref": None,
        }
    )
    _, transport = boundary(
        replace(request, report=replace(request.report, raw=json.dumps(doc)))
    )
    assert len(transport.calls) == 1
    assert transport.calls[0].body


@pytest.mark.parametrize(
    ("pack", "count"),
    [
        ("normal-1", 17),
        ("normal-2", 23),
        ("fault-1", 11),
        ("fault-2", 9),
        ("pc-fault", 11),
        ("cart-fault", 5),
    ],
)
def test_all_pr_a_frozen_views_pass(boundary, pack, count):
    root = Path(__file__).resolve().parents[2]
    ledger = json.loads(
        (
            root / "docs/evidence/m1-01-alignment-c-effect" / pack / "ledger.json"
        ).read_bytes()
    )
    evidence = ledger["evidence"]
    assert len(evidence) == count  # All 76 views, including unadopted records.
    request = judge_input()
    base = request.delivered[EID]
    delivered = {EID: base}
    for index, item in enumerate(evidence):
        eid = f"frozen-{index}"
        view = item["view"]
        assert canonical_hash(view) == item["view_sha256"]
        delivered[eid] = replace(
            base, evidence_id=eid, view=view, view_sha256=item["view_sha256"]
        )
    _, transport = boundary(replace(request, delivered=delivered))
    assert len(transport.calls) == 1
    assert transport.calls[0].body
