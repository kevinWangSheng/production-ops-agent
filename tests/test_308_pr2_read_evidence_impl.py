"""#308 PR2 implementer tests: kept over-limit results and ``read_evidence``.

Contract: ``docs/tasks/2026-10-22-308-overlimit-evidence-contract.md`` (U1-U7,
R1-R13). Drives the product otel-demo executor factory (fake HTTP opener,
in-memory evidence store) so the routing transport, the summary hook and the
evidence-read transport are the shipped ones.
"""

from __future__ import annotations

import dataclasses
import json
import urllib.request
from datetime import timedelta

import pytest

from opspilot.investigation.context import delivered_view
from opspilot.redaction import redact_payload
from opspilot.tools import ReadOnlyToolExecutor, ToolContractError
from opspilot.tools.evidence_read import EVIDENCE_TOOL, EvidenceReadTransport
from opspilot.tools.executor import (
    EVIDENCE_SOURCE,
    MAX_SPILLED_RECORDS_PER_RUN,
    TransportRequest,
)
from opspilot.tools.otel_demo import (
    LABEL_VALUES_TOOL,
    METRIC_NAMES_TOOL,
    METRICS_TOOL,
    TARGET_ID,
    TOOL_NAMES,
    TOOL_SCHEMAS,
    TRACES_TOOL,
    OtelDemoConfig,
    otel_demo_executor_factory,
    otel_demo_registrations,
    overflow_summary,
)
from opspilot.tools.outcomes import PROJECTION_REVISION
from opspilot.tools.registry import MAX_VIEW_TOKENS, ToolRegistry, canonical
from opspilot.web.evidence import MemoryEvidenceStore
from tests.m1_tool_support import FakeClock, registration
from tests.test_m1_otel_demo_contract import (
    GOOD_EXPR,
    JAEGER_URL,
    NOW,
    PROMETHEUS_URL,
    WINDOW,
    FakeOpener,
    FakeStore,
    _call,
    _input,
    _lease,
)


def quarter_counter(text: str) -> int:
    return -(-len(text.encode("utf-8")) // 4)


def _series(count: int, *, pad: int = 600) -> list[dict]:
    stamp = int(WINDOW.start.timestamp()) + 60
    return [
        {
            "metric": {
                "__name__": "m",
                "service_name": f"svc-{index:05d}",
                "zone": f"z{index % 3}",
                "note": "login password=xyz",
                "pad": "x" * pad,
            },
            "values": [[stamp, "1"], [stamp + 30, "2"]],
        }
        for index in range(count)
    ]


def _matrix(rows: list[dict]) -> bytes:
    return json.dumps(
        {"status": "success", "data": {"resultType": "matrix", "result": rows}}
    ).encode()


ROWS = _series(240)


def _factory(monkeypatch, opener, evidence, *, store=None):
    store = store or FakeStore(_lease(), deadline=NOW + timedelta(minutes=10))
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: opener)
    factory = otel_demo_executor_factory(
        store,
        evidence=evidence,
        clock=FakeClock(start=NOW),
        config=OtelDemoConfig(
            prometheus_url=PROMETHEUS_URL, jaeger_url=JAEGER_URL, token=None
        ),
        token_counter=quarter_counter,
    )
    executor = factory(store.lease, _input(str(store.lease.run_id)))
    assert isinstance(executor, ReadOnlyToolExecutor)
    return executor, store


def _over_limit(monkeypatch, evidence=None):
    evidence = evidence if evidence is not None else MemoryEvidenceStore()
    opener = FakeOpener(by_query={GOOD_EXPR: _matrix(ROWS)})
    executor, store = _factory(monkeypatch, opener, evidence)
    outcome = executor.execute(_call(METRICS_TOOL, {"expr": GOOD_EXPR}))
    return executor, evidence, opener, store, outcome


def _read(executor, params, *, index=1):
    return executor.execute(_call(EVIDENCE_TOOL, params, index=index))


# -- the kept refusal (U5, R1, R2) -------------------------------------------


def test_over_limit_refusal_is_the_kept_records_view_with_a_count_summary(
    monkeypatch,
):
    _, evidence, _, _, outcome = _over_limit(monkeypatch)
    view = outcome.model_view
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert outcome.evidence is not None and outcome.adopted is False
    assert canonical(outcome.evidence.view) == canonical(view)  # R1
    stored = evidence.get(view["evidence_id"])
    assert stored is not None and stored.adopted is False
    assert canonical(stored.view) == canonical(view)
    assert stored.raw == canonical(redact_payload(json.loads(_matrix(ROWS)))).encode()
    assert view["adopted"] is False and view["citable_as_fact"] is False
    assert view["content"] is None and view["view_tokens"] > MAX_VIEW_TOKENS
    assert (view["total_rows"], view["row_unit"]) == (len(ROWS), "series")
    assert 0 < view["suggested_limit"] < len(ROWS)
    assert (view["series_count"], view["total_points"]) == (len(ROWS), 2 * len(ROWS))
    cardinality = view["label_cardinality"]
    assert cardinality[0] == {"label": "service_name", "value_count": len(ROWS)}
    counts = [item["value_count"] for item in cardinality]
    assert counts == sorted(counts, reverse=True) and len(cardinality) <= 10
    assert {"label": "zone", "value_count": 3} in cardinality
    assert "svc-00001" not in canonical(view) and "xyz" not in canonical(view)
    assert view["suggestion"].startswith(f"Consider using topk(5, {GOOD_EXPR})")
    assert "read_evidence" in view["usage"] and "stored" not in view
    # Never citable: the kept record yields no delivered citation.
    assert (
        delivered_view(
            view, evidence_context=None, authorized_targets=frozenset({TARGET_ID})
        )
        is None
    )


def test_suggested_limit_rows_fit_one_slice(monkeypatch):
    executor, _, _, _, outcome = _over_limit(monkeypatch)
    view = outcome.model_view
    fits = _read(
        executor, {"evidence_id": view["evidence_id"], "limit": view["suggested_limit"]}
    )
    assert fits.status == "ok"


# -- read_evidence (U4, U6, R3-R5) -------------------------------------------


def test_a_slice_is_adopted_citable_evidence_of_the_exact_rows(monkeypatch):
    executor, evidence, opener, _, outcome = _over_limit(monkeypatch)
    parent = outcome.model_view
    calls = len(opener.requests)
    slice_outcome = _read(
        executor, {"evidence_id": parent["evidence_id"], "offset": 10, "limit": 7}
    )
    view = slice_outcome.model_view
    assert len(opener.requests) == calls  # no data source contact
    assert slice_outcome.status == "ok" and slice_outcome.adopted
    expected_rows = json.loads(evidence.get(parent["evidence_id"]).raw)["data"][
        "result"
    ][10:17]
    assert view["content"] == expected_rows
    assert view["tool"] == EVIDENCE_TOOL and view["parent_tool"] == METRICS_TOOL
    assert view["truncated"] is True and view["omitted_rows"] == len(ROWS) - 7
    assert view["slice"] == {
        "parent_evidence_id": parent["evidence_id"],
        "parent_raw_sha256": evidence.get(parent["evidence_id"]).raw_sha256,
        "offset": 10,
        "returned": 7,
        "total_rows": len(ROWS),
    }
    for key in (
        "source",
        "window",
        "observed_at",
        "dispatch_started_at",
        "data_as_of",
        "source_start_at",
        "source_end_at",
        "freshness_seconds",
        "incomplete",
    ):
        assert view[key] == parent[key], key
    assert view["evidence_id"] != parent["evidence_id"]
    assert view["citable_as_fact"] is True
    # R5: the raw names the parent and its hash.
    raw = json.loads(slice_outcome.evidence.raw)
    assert raw["parent_raw_sha256"] == view["slice"]["parent_raw_sha256"]
    assert raw["rows"] == expected_rows and (raw["offset"], raw["limit"]) == (10, 7)


def test_a_slice_binds_to_a_time_policy_naming_only_the_parent_tool(monkeypatch):
    executor, _, _, _, outcome = _over_limit(monkeypatch)
    slice_view = _read(
        executor, {"evidence_id": outcome.model_view["evidence_id"], "limit": 3}
    ).model_view
    context = {
        "time_policies": [
            {
                "id": "metrics-only",
                "mode": "historical_window",
                "all_authorized_targets": True,
                "window": WINDOW.as_json(),
                "reference_rule": "response_received_at",
                "interfaces": [METRICS_TOOL],
            }
        ]
    }
    citation = delivered_view(
        slice_view, evidence_context=context, authorized_targets=frozenset({TARGET_ID})
    )
    assert citation is not None and citation.citable_as_fact
    assert "metrics-only" in citation.time_scope_refs


def test_an_offset_past_the_rows_is_no_data_and_a_slice_over_the_limit_is_refused(
    monkeypatch,
):
    executor, evidence, _, _, outcome = _over_limit(monkeypatch)
    evidence_id = outcome.model_view["evidence_id"]
    past = _read(
        executor, {"evidence_id": evidence_id, "offset": len(ROWS), "limit": 5}
    )
    assert (past.status, past.reason) == ("no_data", "NO_DATA")
    whole = _read(executor, {"evidence_id": evidence_id, "limit": len(ROWS)}, index=2)
    assert (whole.status, whole.reason) == ("error", "RESULT_TOO_LARGE")
    assert whole.evidence is None and "evidence_id" not in whole.model_view
    assert evidence.count_spilled(str(executor.scope.run_id))[0] == 1  # not re-kept


@pytest.mark.parametrize(
    ("params", "reason"),
    [
        ({"evidence_id": "nope:1", "limit": 1}, "EVIDENCE_NOT_READABLE"),
        ({"evidence_id": "", "limit": 1}, "EVIDENCE_NOT_READABLE"),
        ({"evidence_id": "{parent}", "offset": -1, "limit": 1}, "INVALID_PARAMS"),
        ({"evidence_id": "{parent}", "limit": 0}, "INVALID_PARAMS"),
    ],
)
def test_unreadable_requests_are_refused_before_any_read(monkeypatch, params, reason):
    executor, _, opener, _, outcome = _over_limit(monkeypatch)
    calls = len(opener.requests)
    params = {
        key: outcome.model_view["evidence_id"] if value == "{parent}" else value
        for key, value in params.items()
    }
    refused = _read(executor, params)
    assert refused.reason == reason
    assert refused.status == (
        "denied" if reason == "EVIDENCE_NOT_READABLE" else "error"
    )
    assert refused.source_contact == "none" and refused.operation.sent is False
    assert refused.evidence is None and len(opener.requests) == calls


def test_another_runs_record_and_an_adopted_record_are_not_readable(monkeypatch):
    evidence = MemoryEvidenceStore()
    _, _, _, _, outcome = _over_limit(monkeypatch, evidence)
    foreign = outcome.model_view["evidence_id"]
    opener = FakeOpener(by_query={GOOD_EXPR: _matrix(ROWS[:2])})
    executor, _ = _factory(monkeypatch, opener, evidence)  # a new Run
    assert _read(executor, {"evidence_id": foreign, "limit": 1}).reason == (
        "EVIDENCE_NOT_READABLE"
    )
    ok = executor.execute(_call(METRICS_TOOL, {"expr": GOOD_EXPR}))
    assert ok.status == "ok"
    adopted = _read(
        executor, {"evidence_id": ok.model_view["evidence_id"], "limit": 1}, index=2
    )
    assert adopted.reason == "EVIDENCE_NOT_READABLE"


def test_a_record_whose_target_is_no_longer_authorized_is_not_readable(monkeypatch):
    _, evidence, _, store, outcome = _over_limit(monkeypatch)
    transport = EvidenceReadTransport(
        store=evidence,
        run_id=str(store.lease.run_id),
        target_ids=frozenset({"another-target"}),
        result_paths={METRICS_TOOL: ("data", "result")},
    )
    response = transport.fetch(
        TransportRequest(
            operation_id="s-t0",
            source=EVIDENCE_SOURCE,
            verb="get",
            endpoint=PROMETHEUS_URL,
            selector={},
            params={"evidence_id": outcome.model_view["evidence_id"], "limit": 1},
            window=WINDOW,
            timeout_seconds=5.0,
            max_result_bytes=1 << 20,
            credential_ref="otel-demo-ro",
            tool=EVIDENCE_TOOL,
            target_id=TARGET_ID,
        )
    )
    assert (response.source_status, response.sent) == ("EVIDENCE_NOT_READABLE", False)


def test_a_paused_run_cannot_read(monkeypatch):
    executor, _, _, store, outcome = _over_limit(monkeypatch)
    store.control = {**store.control, "global_suspended": True}
    refused = _read(
        executor, {"evidence_id": outcome.model_view["evidence_id"], "limit": 1}
    )
    assert (refused.status, refused.reason) == ("denied", "SUSPENDED")


def test_non_metrics_tools_name_their_row_unit_only():
    assert overflow_summary(TRACES_TOOL, [], {}) == {"row_unit": "spans"}
    assert overflow_summary(METRIC_NAMES_TOOL, [], {}) == {"row_unit": "metric_names"}
    assert overflow_summary(LABEL_VALUES_TOOL, [], {}) == {"row_unit": "label_values"}


# -- per-Run cap (U2) ---------------------------------------------------------


class _FullStore(MemoryEvidenceStore):
    def register_spilled(self, record, *, max_records, max_bytes):
        assert max_records == MAX_SPILLED_RECORDS_PER_RUN
        return False


def test_past_the_cap_the_refusal_says_stored_false_and_carries_no_id(monkeypatch):
    _, _, _, _, outcome = _over_limit(monkeypatch, _FullStore())
    view = outcome.model_view
    assert outcome.evidence is None and "evidence_id" not in view
    assert view["stored"] is False and view["total_rows"] == len(ROWS)
    assert "read_evidence" in view["usage"] and "narrow" in view["usage"]


# -- registration and revisions (U7, contract item 7) -------------------------


def test_registry_caps_max_view_tokens_at_25k():
    assert MAX_VIEW_TOKENS == 25_000
    dataclasses.replace(registration(), max_view_tokens=MAX_VIEW_TOKENS)
    with pytest.raises(ToolContractError, match="VIEW_LIMIT_OUT_OF_RANGE"):
        dataclasses.replace(registration(), max_view_tokens=MAX_VIEW_TOKENS + 1)


def test_read_evidence_is_registered_and_derived_into_the_tool_names():
    tools = otel_demo_registrations().tools
    names = tuple(item.name for item in tools)
    assert names == TOOL_NAMES and EVIDENCE_TOOL in names
    assert [item["function"]["name"] for item in TOOL_SCHEMAS] == list(names)
    reg = ToolRegistry(tools).lookup(EVIDENCE_TOOL)
    assert (reg.source, reg.verb) == (EVIDENCE_SOURCE, "get")
    assert "only the rows it shows" in reg.description.cannot_prove  # R7
    assert PROJECTION_REVISION == "m1-01-tool-view-v9"


# -- eval provenance (R10) ----------------------------------------------------


def _eval_context(executor):
    from datetime import timedelta as _td

    from opspilot.evaluation.codec import encode
    from opspilot.evaluation.recording import RecordingContext, ToolBinding
    from opspilot.evaluation.rounds import (
        REQUIRED_DIMENSIONS,
        DimensionValue,
        Manifest,
    )
    from opspilot.evaluation.tool_provenance import ToolSnapshot, bind_tool_snapshots

    digest = "0" * 64
    scope = executor.scope
    bindings = tuple(
        ToolBinding(
            reg.name,
            reg.version,
            TARGET_ID,
            reg.source,
            encode(scope.window),
            None,
            digest,
            digest,
            digest,
            digest,
        )
        for reg in executor._tools._entries.values()
    )
    ctx = RecordingContext(
        "recording",
        scope.run_id,
        digest,
        "case",
        "v1",
        True,
        {},
        {"question": "q"},
        {},
        "inputs-0",
        "controls-0",
        NOW - _td(minutes=1),
        NOW + _td(hours=1),
        bindings,
    )
    manifest = Manifest(
        "round",
        {name: DimensionValue("hash", digest, None) for name in REQUIRED_DIMENSIONS},
    )
    snapshot = ToolSnapshot(
        tuple(executor._tools._entries.values()),
        tuple(executor._targets._entries.values()),
        scope,
    )
    return bind_tool_snapshots(ctx, (snapshot,), manifest)


def _check(executor, ctx, request, outcome):
    from opspilot.evaluation.tool_provenance import check_result

    binding = next(item for item in ctx.bindings if item.tool == request.tool_name)
    check_result(ctx, request, binding, outcome, NOW + timedelta(minutes=30))


def test_eval_provenance_accepts_kept_refusals_and_slices(monkeypatch):
    # Refusals a transport decides before sending (``sent: false``, here
    # EVIDENCE_NOT_READABLE) are outside the recording format for every
    # profile tool already; not exercised.
    from opspilot.evaluation.codec import StorageError

    executor, _, _, _, outcome = _over_limit(monkeypatch)
    ctx = _eval_context(executor)
    metrics_call = _call(METRICS_TOOL, {"expr": GOOD_EXPR})
    _check(executor, ctx, metrics_call, outcome)
    evidence_id = outcome.model_view["evidence_id"]
    for index, params in enumerate(
        (
            {"evidence_id": evidence_id, "offset": 2, "limit": 3},
            {"evidence_id": evidence_id, "offset": len(ROWS), "limit": 3},
            {"evidence_id": evidence_id, "limit": len(ROWS)},
        ),
        start=1,
    ):
        call = _call(EVIDENCE_TOOL, params, index=index)
        _check(executor, ctx, call, executor.execute(call))
    # A kept refusal whose summary is tampered with is not representable.
    tampered = dict(outcome.model_view, suggested_limit=len(ROWS) + 1)
    forged = dataclasses.replace(
        outcome,
        model_view=tampered,
        evidence=dataclasses.replace(outcome.evidence, _view=tampered),
    )
    with pytest.raises(StorageError):
        _check(executor, ctx, metrics_call, forged)


def test_eval_provenance_accepts_a_refusal_past_the_cap(monkeypatch):
    executor, _, _, _, outcome = _over_limit(monkeypatch, _FullStore())
    ctx = _eval_context(executor)
    _check(executor, ctx, _call(METRICS_TOOL, {"expr": GOOD_EXPR}), outcome)
