"""Independent PR2 acceptance tests for #308 (over-limit refusal view + read_evidence).

Written from ``docs/tasks/2026-10-22-308-overlimit-evidence-contract.md`` (U1-U7,
R1-R13 and the "给独立测试作者的可观察行为" list) and the public tool-gateway,
evidence-store, report-validation and loop interfaces.  Nothing here reads the
PR2 implementation; contract names that are not spelled out are not guessed:
the new tool is reached only through ``otel_demo_executor_factory`` (the
product composition), the registrations and ``TOOL_SCHEMAS``.

The source is a ``FakeOpener`` serving prepared Prometheus matrices, so every
byte count is deterministic.  The token counter is the byte length, which
makes the 25,000-token view limit a 25,000-byte limit.
"""

from __future__ import annotations

import dataclasses
import json
import urllib.request
from collections.abc import Mapping
from datetime import timedelta

import pytest

from opspilot.investigation.context import context_policy_versions, delivered_view
from opspilot.investigation.loop import (
    InvestigationLoop,
    InvestigationRequest,
    investigation_versions,
)
from opspilot.investigation.reports import validated_report
from opspilot.investigation.store import MemoryStepStore
from opspilot.tools import (
    DENIED_REASONS,
    PROJECTION_REVISION,
    READ_ONLY_VERBS,
    ToolContractError,
    ToolRegistry,
    ToolRequest,
    TransportResponse,
)
from opspilot.tools import registry as registry_module
from opspilot.tools.otel_demo import (
    METRICS_TOOL,
    SOURCE,
    TARGET_ID,
    TOOL_NAMES,
    TOOL_SCHEMA_REVISION,
    TOOL_SCHEMAS,
    TRACE_PROJECTION,
    OtelDemoConfig,
    _evidence_context,
    otel_demo_executor_factory,
    otel_demo_registrations,
    otel_demo_versions,
)
from opspilot.tools.registry import canonical, canonical_hash
from opspilot.web.evidence import MemoryEvidenceStore
from scripts.eval_live import template_scope
from tests.m1_investigation_support import TOOL_SCHEMAS as FIXTURE_SCHEMAS
from tests.m1_investigation_support import (
    ScriptedModel,
    reply,
    report_json,
    tool_call,
)
from tests.m1_tool_support import (
    WINDOW_START,
    FakeClock,
    FakeTransport,
    build,
    historical_window_context,
    registration,
)
from tests.test_m1_investigation_compaction import (
    _limits_between_rounds,
    _rounds,
    _summary,
)
from tests.test_m1_otel_demo_contract import (
    CHECKOUT_FIRST,
    JAEGER_URL,
    NOW,
    PROMETHEUS_URL,
    WINDOW,
    FakeOpener,
    FakeStore,
    _input,
    _lease,
)

READ_TOOL = "read_evidence"

# Baseline values read from main@1f701af (PR1 merged, before any PR2 change).
OLD_TOOL_SCHEMA_REVISION = "otel-demo-5270fd8c543f"  # otel_demo.py @ 1f701af
OLD_PROJECTION_REVISION = "m1-01-tool-view-v8"  # outcomes.py @ 1f701af
OLD_PROMPT_REVISION = "prompt-replay-candidate-dd9018031487"  # @ 1f701af
OLD_CONTEXT_POLICY_REVISION = "ctx-ctx-policy-v1-a30e65ea52f4"  # @ 1f701af

MAX_VIEW = 25_000
SERIES = 200
STAMPS = [int(CHECKOUT_FIRST.timestamp()) + 30 * k for k in range(8)]


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


# -- source data ------------------------------------------------------------


def _series_rows(n: int = SERIES) -> list[dict[str, object]]:
    """``n`` series with 15 label keys of very different cardinality.

    ``pod`` has ``n`` values, ``k00``..``k11`` have 1..12, ``service_name`` 5,
    ``status_code`` 3: the ten largest counts are unambiguous (the 10th and
    11th differ), and every label value carries the ``zz-`` marker so a leak
    into a model view is detectable by substring.
    """
    rows = []
    for i in range(n):
        labels: dict[str, str] = {
            "service_name": f"zz-svc-{i % 5}",
            "pod": f"zz-pod-{i}",
            "status_code": f"zz-C{i % 3}",
        }
        for j in range(12):
            labels[f"k{j:02d}"] = f"zz-v{j}-{i % (j + 1)}"
        rows.append({"metric": labels, "values": [[s, "1.5"] for s in STAMPS]})
    return rows


def _matrix(rows: list[dict[str, object]]) -> bytes:
    return json.dumps(
        {"status": "success", "data": {"resultType": "matrix", "result": rows}}
    ).encode()


def _truth_cardinality(rows: list[dict[str, object]]) -> dict[str, int]:
    keys: dict[str, set[str]] = {}
    for row in rows:
        for key, value in row["metric"].items():  # type: ignore[union-attr]
            keys.setdefault(key, set()).add(value)
    return {key: len(values) for key, values in keys.items()}


class _CapturingStore(MemoryEvidenceStore):
    """The product's in-memory evidence store, remembering the records it saw."""

    def __init__(self) -> None:
        super().__init__()
        self.records = {}

    def register(self, record):
        self.records[record.evidence_id] = record
        return super().register(record)


class _Lab:
    """Product executor over a prepared source, one evidence store, many Runs."""

    def __init__(self, monkeypatch, *, token_counter=_bytes, sink=None) -> None:
        self.opener = FakeOpener()
        monkeypatch.setattr(urllib.request, "build_opener", lambda *h: self.opener)
        self.sink = sink if sink is not None else _CapturingStore()
        self.clock = FakeClock(start=NOW)
        self.token_counter = token_counter
        self.rows = _series_rows()
        self._steps = 0
        self.store, self.executor = self.new_run()

    def new_run(self):
        store = FakeStore(_lease(), deadline=NOW + timedelta(minutes=10))
        factory = otel_demo_executor_factory(
            store,
            evidence=self.sink,
            clock=self.clock,
            config=OtelDemoConfig(prometheus_url=PROMETHEUS_URL, jaeger_url=JAEGER_URL),
            token_counter=self.token_counter,
        )
        return store, factory(store.lease, _input(str(store.lease.run_id)))

    def serve(self, expr: str, rows: list[dict[str, object]] | None = None) -> str:
        self.opener.by_query[expr] = _matrix(self.rows if rows is None else rows)
        return expr

    def query(self, expr: str, *, executor=None):
        self._steps += 1
        return (executor or self.executor).execute(
            ToolRequest(
                step_id=f"q{self._steps}",
                tool_index=0,
                tool_name=METRICS_TOOL,
                target_ref=TARGET_ID,
                params={"expr": expr},
                window=WINDOW.as_json(),
            )
        )

    def read(self, evidence_id, offset=0, limit=10, *, executor=None):
        self._steps += 1
        return (executor or self.executor).execute(
            ToolRequest(
                step_id=f"r{self._steps}",
                tool_index=0,
                tool_name=READ_TOOL,
                target_ref=TARGET_ID,
                params={"evidence_id": evidence_id, "offset": offset, "limit": limit},
                window=WINDOW.as_json(),
            )
        )

    def spill(self, expr: str = "big_0", *, executor=None):
        outcome = self.query(self.serve(expr), executor=executor)
        assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
        return outcome

    @property
    def requests(self) -> int:
        return len(self.opener.requests)


@pytest.fixture
def lab(monkeypatch):
    return _Lab(monkeypatch)


def _parent_rows(lab: _Lab, evidence_id: str) -> list[object]:
    stored = lab.sink.get(evidence_id)
    assert stored is not None
    return json.loads(stored.raw)["data"]["result"]


def _pairs(value: object) -> list[tuple[str, int]]:
    """``label_cardinality`` as ordered (key, count) pairs, whatever the shape.

    The contract fixes the content (top-10 label keys with their value counts,
    no values) but not the JSON shape, so a mapping, a list of pairs and a list
    of single-key/count objects are all accepted.
    """
    if isinstance(value, Mapping):
        return [(str(k), v) for k, v in value.items()]
    assert isinstance(value, list), f"unrecognised label_cardinality: {value!r}"
    pairs = []
    for item in value:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            pairs.append((item[0], item[1]))
        elif isinstance(item, Mapping):
            ints = [v for v in item.values() if type(v) is int]
            strs = [v for v in item.values() if isinstance(v, str)]
            assert len(ints) == 1 and len(strs) == 1, f"unrecognised entry: {item!r}"
            pairs.append((strs[0], ints[0]))
        else:
            raise AssertionError(f"unrecognised entry: {item!r}")
    return pairs


# =============================================================================
# 1. The over-limit refusal view (U5, R2, U2)
# =============================================================================


def test_refusal_view_carries_evidence_id_and_fixed_u5_fields(lab):
    outcome = lab.spill()
    view = outcome.model_view

    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert isinstance(view["evidence_id"], str) and view["evidence_id"]
    assert view["adopted"] is False
    assert view["citable_as_fact"] is False
    assert view["max_view_tokens"] == MAX_VIEW
    assert type(view["view_tokens"]) is int and view["view_tokens"] > MAX_VIEW
    assert view["total_rows"] == SERIES
    assert view["row_unit"] == "series"
    # The usage text names the tool and the id to pass to it.
    text = canonical(view)
    assert READ_TOOL in text
    assert view["evidence_id"] in text.replace("\\", "")
    # R1/R2: the id resolves to the stored, un-adopted record carrying this view.
    stored = lab.sink.get(view["evidence_id"])
    assert stored is not None and stored.adopted is False
    assert stored.status == "error" and stored.hashes_verified is True
    assert canonical(stored.view) == canonical(view)


def test_refusal_view_has_no_content_preview_and_no_row_data(lab):
    view = lab.spill().model_view
    text = canonical(view)

    assert view.get("content") is None
    # No label value, series value or timestamp row of the source appears.
    assert "zz-" not in text
    assert '"values"' not in text
    assert "1.5" not in text


def test_refusal_view_metrics_summary_has_counts_only(lab):
    view = lab.spill().model_view
    truth = _truth_cardinality(lab.rows)

    assert view["series_count"] == SERIES
    assert view["total_points"] == SERIES * len(STAMPS)
    pairs = _pairs(view["label_cardinality"])
    counts = [count for _, count in pairs]
    assert len(pairs) == 10
    assert counts == sorted(counts, reverse=True)
    assert counts == sorted(truth.values(), reverse=True)[:10]
    for key, count in pairs:
        assert isinstance(key, str) and type(count) is int
        assert truth[key] == count
    assert len({key for key, _ in pairs}) == 10


def test_refusal_view_suggested_limit_is_a_first_chunk_that_fits(lab):
    outcome = lab.spill()
    view = outcome.model_view
    suggested = view["suggested_limit"]

    assert type(suggested) is int and 1 <= suggested < view["total_rows"]
    slice_outcome = lab.read(view["evidence_id"], 0, suggested)
    assert slice_outcome.status == "ok", slice_outcome.model_view
    assert slice_outcome.model_view["slice"]["returned"] == suggested


def test_refusal_view_keeps_the_gateway_provenance_of_the_refusal(lab):
    outcome = lab.spill()
    view = outcome.model_view

    assert view["status"] == "error" and view["reason"] == "RESULT_TOO_LARGE"
    assert view["tool"] == METRICS_TOOL
    assert view["target_id"] == TARGET_ID
    assert view["query"] == {"expr": "big_0"}
    assert view["window"] == WINDOW.as_json()


def test_seventeenth_overlimit_result_is_refused_with_stored_false(lab):
    views = [lab.spill(f"big_{i}").model_view for i in range(17)]

    assert all(view.get("stored", True) is True for view in views[:16])
    assert views[16]["stored"] is False
    assert views[16]["adopted"] is False and views[16]["citable_as_fact"] is False
    assert views[16].get("content") is None
    assert lab.sink.count_spilled(lab.executor.scope.run_id)[0] == 16
    # A refusal that was not stored cannot be read back.
    last_id = views[16].get("evidence_id")
    if last_id is not None:
        denied = lab.read(last_id)
        assert (denied.status, denied.reason) == ("denied", "EVIDENCE_NOT_READABLE")


def test_a_spilled_id_cited_as_a_fact_is_unknown_evidence_id(lab):
    outcome = lab.spill()
    view = outcome.model_view
    context = _evidence_context(lab.executor.scope.run_id, WINDOW)
    targets = frozenset({TARGET_ID})

    # The refusal is not adopted, so it never becomes a citable delivered view.
    assert (
        delivered_view(view, evidence_context=context, authorized_targets=targets)
        is None
    )

    content = json.dumps(
        {
            "schema_version": "m0-report-v2",
            "assessment_status": "completed",
            "conclusion": "supported",
            "summary": "Cites a refused result.",
            "claims": [
                {
                    "kind": "fact",
                    "text": "Many series exist.",
                    "evidence_ids": [view["evidence_id"]],
                    "target_refs": [TARGET_ID],
                    "time_scope_ref": "policy-window-1",
                }
            ],
            "gaps": [],
            "next_steps": [],
        }
    )
    report, reason, feedback = validated_report(
        content,
        finish_reason="stop",
        delivered=[],
        evidence_context=context,
        authorized_targets=targets,
    )
    assert report is None and reason == "REPORT_INVALID"
    assert "unknown_evidence_id" in feedback
    assert view["evidence_id"] in feedback


# =============================================================================
# 2. read_evidence slices (U4, U6, R4, R5, R7)
# =============================================================================


def test_read_evidence_returns_the_exact_parent_rows_as_adopted_evidence(lab):
    parent = lab.spill().model_view
    rows = _parent_rows(lab, parent["evidence_id"])

    outcome = lab.read(parent["evidence_id"], 30, 12)
    view = outcome.model_view

    assert (outcome.status, outcome.reason) == ("ok", None), view
    assert view["status"] == "ok"
    assert view["adopted"] is True and view["citable_as_fact"] is True
    assert view["tool"] == READ_TOOL
    assert view["parent_tool"] == METRICS_TOOL
    assert view["truncated"] is True
    assert view["content"] == rows[30:42]
    stored_parent = lab.sink.get(parent["evidence_id"])
    assert view["slice"] == {
        "parent_evidence_id": parent["evidence_id"],
        "parent_raw_sha256": stored_parent.raw_sha256,
        "offset": 30,
        "returned": 12,
        "total_rows": SERIES,
    }
    # A new evidence record, distinct from the parent.
    assert view["evidence_id"] != parent["evidence_id"]
    assert outcome.evidence is not None
    assert outcome.evidence.evidence_id == view["evidence_id"]
    assert outcome.evidence.view == view
    assert outcome.evidence.adopted is True and outcome.evidence.status == "ok"
    new = lab.sink.get(view["evidence_id"])
    assert new is not None and new.adopted is True and new.hashes_verified is True


def test_read_evidence_tail_slice_returns_the_remaining_rows(lab):
    parent = lab.spill().model_view
    rows = _parent_rows(lab, parent["evidence_id"])

    outcome = lab.read(parent["evidence_id"], SERIES - 5, 10)

    assert outcome.status == "ok", outcome.model_view
    assert outcome.model_view["content"] == rows[SERIES - 5 :]
    assert outcome.model_view["slice"]["returned"] == 5
    assert outcome.model_view["slice"]["total_rows"] == SERIES


def test_slice_raw_is_the_canonical_parent_reference_and_rows(lab):
    parent = lab.spill().model_view
    rows = _parent_rows(lab, parent["evidence_id"])
    stored_parent = lab.sink.get(parent["evidence_id"])

    outcome = lab.read(parent["evidence_id"], 7, 9)

    assert outcome.status == "ok"
    expected = canonical(
        {
            "parent_evidence_id": parent["evidence_id"],
            "parent_raw_sha256": stored_parent.raw_sha256,
            "offset": 7,
            "limit": 9,
            "rows": rows[7:16],
        }
    ).encode()
    assert outcome.evidence.raw == expected


def test_slice_inherits_the_parent_time_and_source_fields(lab):
    parent = lab.spill().model_view
    view = lab.read(parent["evidence_id"], 0, 5).model_view

    for key in (
        "observed_at",
        "data_as_of",
        "source_start_at",
        "source_end_at",
        "dispatch_started_at",
        "freshness_seconds",
        "window",
        "target_id",
    ):
        assert key in view, key
        assert view[key] == parent[key], key
    # The contract names the registration source ``opspilot-evidence`` (R3) and
    # also says the source is inherited (U4/R12): either the view's own
    # ``source`` or an explicit ``parent_source`` must be the parent's.
    assert parent["source"] in (view.get("source"), view.get("parent_source"))
    stored_parent = lab.sink.get(parent["evidence_id"])
    stored_slice = lab.sink.get(view["evidence_id"])
    assert stored_slice.observed_at == stored_parent.observed_at
    assert stored_slice.data_as_of == stored_parent.data_as_of
    assert view["source_start_at"] is not None  # real interval, not "unknown"


def test_slice_over_the_view_limit_is_refused_as_a_whole(lab):
    parent = lab.spill().model_view

    outcome = lab.read(parent["evidence_id"], 0, SERIES)

    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert outcome.model_view.get("content") is None
    assert outcome.model_view["adopted"] is False


def test_read_evidence_description_says_a_slice_proves_only_its_rows():
    registration = next(
        item for item in otel_demo_registrations().tools if item.name == READ_TOOL
    )
    cannot_prove = registration.description.cannot_prove.lower()
    assert "row" in cannot_prove
    assert registration.source == "opspilot-evidence"
    assert registration.verb == "get"
    assert registration.verb in READ_ONLY_VERBS
    assert registration.max_view_tokens == MAX_VIEW


def test_slice_binds_a_policy_window_fact_and_the_report_validates(lab):
    parent = lab.spill().model_view
    slice_view = lab.read(parent["evidence_id"], 0, 10).model_view
    context = _evidence_context(lab.executor.scope.run_id, WINDOW)
    targets = frozenset({TARGET_ID})

    delivered = delivered_view(
        slice_view, evidence_context=context, authorized_targets=targets
    )
    assert delivered is not None
    assert delivered.evidence_id == slice_view["evidence_id"]
    assert delivered.status == "ok" and delivered.citable_as_fact is True
    assert delivered.truncated is True
    assert TARGET_ID in delivered.target_ids
    assert "policy-window-1" in delivered.time_scope_refs

    report, reason, feedback = validated_report(
        _fact_report(slice_view["evidence_id"]),
        finish_reason="stop",
        delivered=[delivered],
        evidence_context=context,
        authorized_targets=targets,
    )
    assert report is not None, (reason, feedback)


@pytest.mark.parametrize("interface", [METRICS_TOOL, SOURCE])
def test_slice_time_binding_uses_the_parent_tool_and_source(lab, interface):
    """R4: a policy naming only the metrics tool (or its source) still binds."""
    parent = lab.spill().model_view
    slice_view = lab.read(parent["evidence_id"], 0, 10).model_view
    context = json.loads(
        json.dumps(_evidence_context(lab.executor.scope.run_id, WINDOW))
    )
    context["time_policies"][0]["interfaces"] = [interface]

    delivered = delivered_view(
        slice_view,
        evidence_context=context,
        authorized_targets=frozenset({TARGET_ID}),
    )

    assert delivered is not None
    assert "policy-window-1" in delivered.time_scope_refs


# =============================================================================
# 3. Access control (R3) and no source contact
# =============================================================================


def _assert_not_readable(outcome, lab, before):
    assert (outcome.status, outcome.reason) == ("denied", "EVIDENCE_NOT_READABLE")
    assert outcome.model_view.get("content") is None
    assert lab.requests == before


def test_read_evidence_never_contacts_the_source(lab):
    parent = lab.spill().model_view
    before = lab.requests

    outcome = lab.read(parent["evidence_id"], 0, 10)

    assert outcome.status == "ok"
    assert lab.requests == before


def test_evidence_not_readable_is_a_declared_denied_reason():
    assert "EVIDENCE_NOT_READABLE" in DENIED_REASONS


def test_another_runs_overlimit_record_is_not_readable(lab):
    parent = lab.spill().model_view
    _, other = lab.new_run()
    before = lab.requests

    _assert_not_readable(lab.read(parent["evidence_id"], executor=other), lab, before)


def test_an_adopted_record_is_not_readable(lab):
    small = lab.serve("small_0", lab.rows[:3])
    adopted = lab.query(small)
    assert adopted.status == "ok"
    before = lab.requests

    _assert_not_readable(lab.read(adopted.model_view["evidence_id"]), lab, before)


def test_an_unknown_evidence_id_is_not_readable(lab):
    before = lab.requests
    _assert_not_readable(
        lab.read("q1-t0:00000000-0000-0000-0000-000000000000"), lab, before
    )


def _forged(lab, parent_id: str, new_id: str, **view_changes):
    """A copy of the genuine spilled record with changed view fields."""
    record = lab.sink.records[parent_id]
    view = {**record.view, "evidence_id": new_id, **view_changes}
    forged = dataclasses.replace(
        record,
        evidence_id=new_id,
        _view=view,
        view_sha256=canonical_hash(view),
        status=view["status"],
    )
    assert lab.sink.register(forged) == new_id
    return new_id


def test_a_record_whose_target_is_no_longer_authorized_is_not_readable(lab):
    parent = lab.spill().model_view
    other = _forged(
        lab,
        parent["evidence_id"],
        "revoked-target-evidence",
        target_id="target-not-in-scope",
    )
    before = lab.requests

    _assert_not_readable(lab.read(other), lab, before)


def test_control_invalidated_history_is_not_readable(lab):
    parent = lab.spill().model_view
    history = _forged(
        lab,
        parent["evidence_id"],
        "control-invalidated-evidence",
        status="denied",
        reason="CONTROL_GENERATION_CHANGED",
    )
    before = lab.requests

    _assert_not_readable(lab.read(history), lab, before)


@pytest.mark.parametrize(
    "offset,limit",
    [(-1, 5), (0, 0), (0, -3), ("0", 5), (0, "5"), (1.5, 5), (True, 5)],
)
def test_invalid_offset_or_limit_is_refused_before_any_read(lab, offset, limit):
    parent = lab.spill().model_view
    before = lab.requests

    outcome = lab.read(parent["evidence_id"], offset, limit)

    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS")
    assert outcome.operation.sent is False
    assert lab.requests == before


def test_a_read_after_suspension_is_refused_by_control(lab):
    parent = lab.spill().model_view
    lab.store.control["global_suspended"] = True
    before = lab.requests

    outcome = lab.read(parent["evidence_id"], 0, 5)

    assert (outcome.status, outcome.reason) == ("denied", "SUSPENDED")
    assert outcome.operation.sent is False
    assert outcome.model_view.get("content") is None
    assert lab.requests == before


# =============================================================================
# 4. Revisions, tool names and the registry hard cap (R10, U7, acceptance 7)
# =============================================================================


def test_tool_schema_and_projection_revisions_changed_and_are_recomputable():
    registry_revision = ToolRegistry(otel_demo_registrations().tools).revision
    digest = canonical_hash(
        {
            "tool_schemas": [dict(item) for item in TOOL_SCHEMAS],
            "registry": registry_revision,
            "trace_projection": TRACE_PROJECTION,
            "projection_revision": PROJECTION_REVISION,
        }
    )
    assert TOOL_SCHEMA_REVISION == f"otel-demo-{digest[:12]}"
    assert TOOL_SCHEMA_REVISION != OLD_TOOL_SCHEMA_REVISION
    assert PROJECTION_REVISION != OLD_PROJECTION_REVISION
    # F3 PR-2 (#311 + #310) contract change: #311 bumps the projection to v10.
    assert PROJECTION_REVISION == "m1-01-tool-view-v10"
    assert otel_demo_versions()["tool_schema_revision"] == TOOL_SCHEMA_REVISION


def test_context_policy_is_unchanged_and_prompt_moves_only_with_310():
    # F3 PR-2 (#311 + #310) contract change: #308 PR2 left prompt_revision unchanged; #310 (same
    # PR-2) moves it, so it is compared for inequality.
    versions = investigation_versions()
    assert versions["prompt_revision"] != OLD_PROMPT_REVISION
    assert versions["context_policy_revision"] == OLD_CONTEXT_POLICY_REVISION
    assert context_policy_versions() == {
        "context_policy_revision": OLD_CONTEXT_POLICY_REVISION
    }


def test_tool_names_include_read_evidence_and_derive_from_registrations(lab):
    registered = tuple(item.name for item in otel_demo_registrations().tools)
    assert READ_TOOL in registered
    assert TOOL_NAMES == registered
    assert {schema["function"]["name"] for schema in TOOL_SCHEMAS} == set(registered)
    assert lab.executor.scope.tool_names == frozenset(registered)
    assert template_scope().tool_names == frozenset(registered)


def test_read_evidence_schema_declares_its_three_parameters():
    schema = next(s for s in TOOL_SCHEMAS if s["function"]["name"] == READ_TOOL)
    parameters = schema["function"]["parameters"]
    assert {"evidence_id", "offset", "limit"} <= set(parameters["properties"])
    assert "evidence_id" in parameters["required"]


def test_registry_has_a_hard_view_token_cap_of_at_most_25000():
    cap = registry_module.MAX_VIEW_TOKENS
    assert type(cap) is int and cap <= 25_000
    base = otel_demo_registrations().tools[0]
    dataclasses.replace(base, max_view_tokens=cap)
    with pytest.raises(ToolContractError):
        dataclasses.replace(base, max_view_tokens=cap + 1)
    assert all(item.max_view_tokens <= cap for item in otel_demo_registrations().tools)


# =============================================================================
# 5. Loop behaviour: dedupe (R2/R6), end-to-end read-back, compaction reset
# =============================================================================


def _fact_report(evidence_id: str) -> str:
    return json.dumps(
        {
            "schema_version": "m0-report-v2",
            "assessment_status": "completed",
            "conclusion": "supported",
            "summary": "The refused result was read back in slices.",
            "claims": [
                {
                    "kind": "fact",
                    "text": "The first series rows were read back.",
                    "evidence_ids": [evidence_id],
                    "target_refs": [TARGET_ID],
                    "time_scope_ref": "policy-window-1",
                }
            ],
            "gaps": ["Only the shown rows were read."],
            "next_steps": ["Narrow the query."],
        }
    )


def _incomplete_report() -> str:
    return json.dumps(
        {
            "schema_version": "m0-report-v2",
            "assessment_status": "incomplete",
            "conclusion": "inconclusive",
            "summary": "Nothing citable was collected.",
            "claims": [],
            "gaps": ["The result was too large."],
            "next_steps": [],
        }
    )


def _loop(lab: _Lab, replies, *, model_requests=4):
    executor = lab.executor
    store = MemoryStepStore(
        budget_limit=16,
        deadline=executor.scope.deadline,
        clock=lab.clock,
        run_id=executor.scope.run_id,
    )
    model = ScriptedModel(replies)
    loop = InvestigationLoop(
        model=model, executor=executor, store=store, clock=lab.clock
    )
    request = InvestigationRequest(
        run_id=executor.scope.run_id,
        question="Why is checkout erroring?",
        scope=executor.scope,
        tool_schemas=TOOL_SCHEMAS,
        model_requests=model_requests,
        evidence_context=_evidence_context(executor.scope.run_id, WINDOW),
    )
    return loop, request, model, store


def _tool_messages(call) -> list[dict]:
    return [json.loads(m["content"]) for m in call.messages if m["role"] == "tool"]


def _call_big(call_id="c1", expr="big_0"):
    return tool_call(call_id, name=METRICS_TOOL, arguments=json.dumps({"expr": expr}))


def _call_read(evidence_id, call_id="c2", offset=0, limit=5):
    return tool_call(
        call_id,
        name=READ_TOOL,
        arguments=json.dumps(
            {"evidence_id": evidence_id, "offset": offset, "limit": limit}
        ),
    )


def test_loop_reads_back_a_refused_result_and_cites_the_slice(lab):
    lab.serve("big_0")

    def read_back(call):
        refusal = _tool_messages(call)[-1]
        return reply(
            tool_calls=[_call_read(refusal["evidence_id"])], finish="tool_calls"
        )

    def cite_slice(call):
        slice_view = _tool_messages(call)[-1]
        return reply(content=_fact_report(slice_view["evidence_id"]))

    loop, request, model, _ = _loop(
        lab,
        [reply(tool_calls=[_call_big()], finish="tool_calls"), read_back, cite_slice],
        model_requests=3,
    )

    outcome = loop.run(request)

    assert outcome.execution == "completed", outcome.handoff_reasons
    assert not outcome.handoff
    refusal, slice_view = _tool_messages(model.calls[2])
    assert refusal["reason"] == "RESULT_TOO_LARGE" and refusal["adopted"] is False
    assert slice_view["status"] == "ok" and slice_view["adopted"] is True
    assert slice_view["evidence_id"] in outcome.evidence_ids
    assert outcome.report is not None
    assert outcome.report.claims[0].evidence_ids == [slice_view["evidence_id"]]


def test_loop_citing_the_refusal_id_as_a_fact_is_rejected_as_unknown(lab):
    lab.serve("big_0")

    def cite_refusal(call):
        refusal = _tool_messages(call)[-1]
        return reply(content=_fact_report(refusal["evidence_id"]))

    loop, request, model, _ = _loop(
        lab,
        [
            reply(tool_calls=[_call_big()], finish="tool_calls"),
            cite_refusal,
            cite_refusal,
        ],
        model_requests=3,
    )

    outcome = loop.run(request)

    assert outcome.report is None
    assert outcome.execution != "completed" or outcome.handoff
    retry = [
        m["content"]
        for call in model.calls[2:]
        for m in call.messages
        if m["role"] == "user" and "unknown_evidence_id" in m["content"]
    ]
    assert retry, "the forced-final feedback must name unknown_evidence_id"


def test_same_parameter_requery_of_an_overlimit_call_is_a_duplicate_view(lab):
    lab.serve("big_0")
    loop, request, model, _ = _loop(
        lab,
        [
            reply(tool_calls=[_call_big("c1"), _call_big("c2")], finish="tool_calls"),
            reply(content=_incomplete_report()),
        ],
        model_requests=2,
    )

    outcome = loop.run(request)

    assert outcome.execution == "completed", outcome.handoff_reasons
    first, second = _tool_messages(model.calls[1])
    assert first["reason"] == "RESULT_TOO_LARGE"
    assert second["reason"] == "DUPLICATE_TOOL_CALL"
    assert second["duplicate_of_evidence_id"] == first["evidence_id"]
    assert second["evidence_id"] == first["evidence_id"]
    assert lab.requests == 1  # the repeat never reached the source


def test_same_parameter_read_evidence_is_deduplicated(lab):
    lab.serve("big_0")

    def read_twice(call):
        refusal = _tool_messages(call)[-1]
        return reply(
            tool_calls=[
                _call_read(refusal["evidence_id"], "c2"),
                _call_read(refusal["evidence_id"], "c3"),
            ],
            finish="tool_calls",
        )

    loop, request, model, _ = _loop(
        lab,
        [
            reply(tool_calls=[_call_big()], finish="tool_calls"),
            read_twice,
            reply(content=_incomplete_report()),
        ],
        model_requests=3,
    )

    outcome = loop.run(request)

    assert outcome.execution == "completed", outcome.handoff_reasons
    _, first, second = _tool_messages(model.calls[2])
    assert first["status"] == "ok"
    assert second["reason"] == "DUPLICATE_TOOL_CALL"
    assert second["duplicate_of_evidence_id"] == first["evidence_id"]


def test_compaction_resets_dedupe_so_a_repeated_overlimit_call_is_requeried():
    """#309 reset keeps working once refusals carry evidence ids (R2).

    Uses the fixture registration (small, deterministic) with a source that
    serves an over-limit body for the ``[1m]`` expression only.
    """
    registrations = [
        dataclasses.replace(
            registration(), max_result_bytes=1_000_000, max_view_tokens=25_000
        )
    ]
    huge = _matrix(_series_rows(60))
    small = json.dumps(
        {"data": {"result": [{"metric": "checkout", "value": 3}]}}
    ).encode()

    class ByExpr(FakeTransport):
        def fetch(self, request):
            self.requests.append(request)
            big = "[1m]" in str(request.params.get("expr"))
            return TransportResponse(
                body=huge if big else small, data_as_of=WINDOW_START
            )

    clock = FakeClock()
    sink = _CapturingStore()
    executor, transport, _, clock = build(
        registrations=registrations,
        transport=ByExpr(),
        sink=sink,
        clock=clock,
        token_counter=_bytes,
    )
    limits = _limits_between_rounds(2)
    store = MemoryStepStore(
        budget_limit=16,
        deadline=executor.scope.deadline,
        clock=clock,
        run_id=executor.scope.run_id,
    )

    def repeat_first(_call):
        return reply(
            tool_calls=[
                tool_call("call-3", arguments='{"expr":"rate(http_errors[1m])"}')
            ],
            finish="tool_calls",
        )

    def cite_ok(_call):
        for results in store.tool_results.values():
            for item in results:
                if item["result"].get("status") == "ok":
                    return reply(
                        content=report_json(evidence_id=item["result"]["evidence_id"])
                    )
        return reply(content=report_json(evidence_id="missing"))

    # The second compaction after the repeated call is the threshold being
    # crossed again with one more tool group; it is not what is under test.
    model = ScriptedModel([*_rounds(2), _summary(), repeat_first, _summary(), cite_ok])
    loop = InvestigationLoop(model=model, executor=executor, store=store, clock=clock)
    request = InvestigationRequest(
        run_id=executor.scope.run_id,
        question="Why is checkout erroring?",
        scope=executor.scope,
        tool_schemas=FIXTURE_SCHEMAS,
        model_requests=8,
        evidence_context=historical_window_context(executor.scope.run_id),
        limits=limits,
    )

    outcome = loop.run(request)

    assert outcome.execution == "completed", outcome.handoff_reasons
    exprs = [r.params["expr"] for r in transport.requests]
    # round 1 ([1m], refused and kept), round 2 ([2m], ok), then after the
    # compaction the identical [1m] call is sent to the source again.
    assert exprs == [
        "rate(http_errors[1m])",
        "rate(http_errors[2m])",
        "rate(http_errors[1m])",
    ]
    refusals = [
        item["result"]
        for results in store.tool_results.values()
        for item in results
        if item["result"].get("reason") == "RESULT_TOO_LARGE"
    ]
    assert len(refusals) == 2
    assert refusals[0]["evidence_id"] != refusals[1]["evidence_id"]
    assert all(isinstance(r["evidence_id"], str) for r in refusals)
    assert all(r["reason"] != "DUPLICATE_TOOL_CALL" for r in refusals)
