"""Independent contract tests for #696 (view half): K8s object views keep a
compact ``managedFields`` summary instead of dropping it whole.

Written from the B4 pre-review (docs/tasks/2026-11-22-next-batches-b4-prereview.md
rows B4-6, B4-7, B4-8 and the 必答类别 rows naming #696) without reading an
implementation. Exercised through the public path: the ``otel-demo-ext``
worker's executor against the loopback HTTPS fake API server of the B4
contract tests, serving ConfigMaps.

Expectations chosen where the contract leaves a shape open (each also marked
``# CHOSEN:``):

* Sorted by ``time`` ascending (oldest first), so "the newest 20" are the
  last 20 entries.
* Over 20 entries the sibling ``metadata.managedFieldsSummaryDropped`` holds
  the number dropped; it is absent when nothing was dropped.
* An object without ``managedFields`` (or with an empty list) carries no
  ``managedFieldsSummary`` key at all, as the API server omits absent fields.
* ``time`` is the API server's string unchanged.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from opspilot.tools.kubernetes_core import COUNT_TOOL, JQ_QUERY_TOOL
from opspilot.tools.profiles import PROFILE_ENV
from tests.test_tool_expansion_b4_contract import (  # noqa: F401 -- fixtures
    EXT_PROFILE,
    SWITCH,
    FakeApiServer,
    Worker,
    _clean_environment,
    _env,
    _face,
    _json,
    _view,
    tls_pair,
)

TABULAR_TOOL = "kubernetes_tabular_query"
NS = "shop"
_CONFIGMAPS = re.compile(r"/api/v1/(?:namespaces/([^/]+)/)?configmaps")
FIELD_CANARY = "CANARY696FIELDVALUE"
SUMMARY = "managedFieldsSummary"
DROPPED = "managedFieldsSummaryDropped"  # CHOSEN: sibling count field


class ConfigMapApi(FakeApiServer):
    def __init__(self) -> None:
        super().__init__()
        self.configmaps: list[dict[str, Any]] = []

    def answer(self, index: int, record: dict[str, Any]) -> tuple[int, bytes]:
        match = _CONFIGMAPS.fullmatch(record["path"])
        if record["method"] == "GET" and match:
            items = [
                c
                for c in self.configmaps
                if match[1] is None or c["metadata"]["namespace"] == match[1]
            ]
            body = {
                "kind": "ConfigMapList",
                "apiVersion": "v1",
                "metadata": {"resourceVersion": "696"},
                "items": json.loads(json.dumps(items)),
            }
            return 200, _json(body)
        return super().answer(index, record)


@pytest.fixture
def api(tls_pair) -> Iterator[ConfigMapApi]:  # noqa: F811
    fake = ConfigMapApi()
    fake.start(tls_pair)
    yield fake
    fake.stop()


def _entry(manager: str, operation: str, time: str, **extra: Any) -> dict[str, Any]:
    return {
        "manager": manager,
        "operation": operation,
        "apiVersion": "v1",
        "time": time,
        "fieldsType": "FieldsV1",
        "fieldsV1": {
            "f:data": {
                "f:FEATURE_FLAG": {},
                f'k:{{"name":"TOKEN","value":"{FIELD_CANARY}"}}': {},
            }
        },
        **extra,
    }


def configmap(name: str, managed: list[dict[str, Any]] | None) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "name": name,
        "namespace": NS,
        "uid": f"uid-{name}",
        "resourceVersion": "1",
        "creationTimestamp": "2026-11-01T00:00:00Z",
    }
    if managed is not None:
        metadata["managedFields"] = managed
    return {"metadata": metadata, "data": {"FEATURE_FLAG": "on"}}


# Deliberately out of time order on the wire.
THREE = [
    _entry("helm", "Apply", "2026-11-22T07:30:00Z"),
    _entry("kubectl-client-side-apply", "Update", "2026-11-20T10:00:00Z"),
    _entry(
        "kube-controller-manager",
        "Update",
        "2026-11-21T12:00:00Z",
        subresource="status",
    ),
]
EXPECTED_THREE = [
    {
        "manager": "kubectl-client-side-apply",
        "operation": "Update",
        "time": "2026-11-20T10:00:00Z",
    },
    {
        "manager": "kube-controller-manager",
        "operation": "Update",
        "time": "2026-11-21T12:00:00Z",
        "subresource": "status",
    },
    {"manager": "helm", "operation": "Apply", "time": "2026-11-22T07:30:00Z"},
]


def _worker(api: ConfigMapApi) -> Worker:
    return Worker(_env(api, **{SWITCH: None}))


def _jq(worker: Worker, expr: str, tool: str = JQ_QUERY_TOOL):
    outcome = worker.call(
        tool, {"kind": "configmaps", "namespace": NS, "jq_expr": expr}
    )
    assert outcome.status == "ok", _view(outcome)
    return outcome


def _results(outcome) -> list[Any]:
    """The jq outputs, read back from the evidence body (``results``)."""
    return json.loads(outcome.evidence.raw)["results"]


def _keys_anywhere(value: object) -> set[str]:
    if isinstance(value, Mapping):
        found = set(map(str, value))
        for child in value.values():
            found |= _keys_anywhere(child)
        return found
    if isinstance(value, list):
        found: set[str] = set()
        for child in value:
            found |= _keys_anywhere(child)
        return found
    return set()


def test_three_writers_are_summarised_sorted_without_the_field_tree(api):
    api.configmaps = [configmap("flags", THREE)]
    outcome = _jq(_worker(api), ".items[0].metadata")
    [metadata] = _results(outcome)
    # CHOSEN: ascending by time; subresource only on the entry that has it.
    assert metadata[SUMMARY] == EXPECTED_THREE
    assert DROPPED not in metadata
    whole = _jq(_worker(api), ".")
    keys = _keys_anywhere(_results(whole))
    assert "managedFields" not in keys
    assert "fieldsV1" not in keys and "fieldsType" not in keys
    outputs = {
        "view": json.dumps(_view(whole), default=str),
        "raw": whole.evidence.raw.decode("utf-8", "replace"),
        "evidence-view": json.dumps(dict(whole.evidence.view), default=str),
    }
    for where, text in outputs.items():
        assert FIELD_CANARY not in text, where
        assert "fieldsV1" not in text, where
        assert '"managedFields"' not in text, where


def test_over_twenty_writers_keep_the_newest_twenty_and_count_the_rest(api):
    managed = [
        _entry(f"writer-{n:02d}", "Update", f"2026-11-21T{n:02d}:00:00Z")
        for n in range(24, -1, -1)  # 25 entries, newest first on the wire
    ]
    managed[0]["time"] = "2026-11-22T00:00:00Z"  # writer-24 is the newest
    api.configmaps = [configmap("busy", managed)]
    [metadata] = _results(_jq(_worker(api), ".items[0].metadata"))
    summary = metadata[SUMMARY]
    assert len(summary) == 20
    assert [e["manager"] for e in summary] == [f"writer-{n:02d}" for n in range(5, 25)]
    assert [e["time"] for e in summary] == sorted(e["time"] for e in summary)
    assert metadata[DROPPED] == 5


def test_jq_count_and_columns_see_the_same_summary(api):
    api.configmaps = [configmap("flags", THREE)]
    worker = _worker(api)
    jq = _jq(worker, ".items[0].metadata.managedFieldsSummary[0].manager")
    assert _results(jq) == ["kubectl-client-side-apply"]
    count = _jq(worker, ".items[].metadata.managedFieldsSummary[]", tool=COUNT_TOOL)
    body = json.loads(count.evidence.raw)
    assert body["count"] == 3
    assert [json.loads(p)["manager"] for p in body["preview"]] == [
        e["manager"] for e in EXPECTED_THREE
    ]
    table = worker.call(
        TABULAR_TOOL,
        {
            "kind": "configmaps",
            "namespace": NS,
            "columns": (
                "NAME:.metadata.name,"
                "FIRST:.metadata.managedFieldsSummary[0].manager,"
                "WHO:.metadata.managedFieldsSummary[*].manager,"
                "WHEN:.metadata.managedFieldsSummary[*].time,"
                "SUB:.metadata.managedFieldsSummary[1].subresource,"
                "TREE:.metadata.managedFields[0].fieldsV1"
            ),
        },
    )
    assert table.status == "ok", _view(table)
    [row] = json.loads(table.evidence.raw)["rows"]
    assert row == [
        "flags",
        "kubectl-client-side-apply",
        ",".join(e["manager"] for e in EXPECTED_THREE),
        ",".join(e["time"] for e in EXPECTED_THREE),
        "status",
        "<none>",
    ]


def test_an_object_without_managed_fields_gets_no_summary(api):
    api.configmaps = [configmap("bare", None), configmap("empty", [])]
    results = _results(_jq(_worker(api), ".items[].metadata"))
    # CHOSEN: absent key, not an empty array.
    assert [SUMMARY in m or DROPPED in m for m in results] == [False, False]
    assert all("managedFields" not in m for m in results)


def test_modification_times_are_current_state_with_no_source_time(api):
    api.configmaps = [configmap("flags", THREE)]
    outcome = _jq(_worker(api), ".items[0].metadata.managedFieldsSummary")
    view = _view(outcome)
    assert view["source_start_at"] is None and view["source_end_at"] is None
    assert outcome.evidence.source_start_at is None
    assert outcome.evidence.source_end_at is None


def _schemas() -> dict[str, Mapping[str, Any]]:
    return {
        s["function"]["name"]: s["function"]
        for s in _face({PROFILE_ENV: EXT_PROFILE}).tool_schemas
    }


def test_descriptions_and_cannot_prove_describe_the_summary(api):
    schemas = _schemas()
    for name in (JQ_QUERY_TOOL, COUNT_TOOL):
        text = schemas[name]["description"]
        assert "without metadata.managedFields" not in text, name
        assert SUMMARY in text, name
    api.configmaps = [configmap("flags", THREE)]
    worker = _worker(api)
    for tool in (JQ_QUERY_TOOL, COUNT_TOOL):
        cannot = str(_view(_jq(worker, ".items[]", tool=tool))["cannot_prove"]).lower()
        assert "managedfieldssummary" in cannot or "managedfields" in cannot, cannot
        assert "last" in cannot, cannot  # each manager's last write
        assert "history" in cannot, cannot  # not a full change history
        assert re.search(r"\b(which|what) fields?\b", cannot), cannot
