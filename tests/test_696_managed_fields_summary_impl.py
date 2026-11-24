"""Implementer tests for #696 beyond the independent contract file: entries
with nothing to say are skipped, and a credential in a manager name is
redacted like any other object text."""

from __future__ import annotations

import json

from tests.test_696_managed_fields_summary_contract import (  # noqa: F401 -- fixtures
    SUMMARY,
    _jq,
    _results,
    _view,
    _worker,
    api,
    configmap,
    tls_pair,
)
from tests.test_tool_expansion_b4_contract import _clean_environment  # noqa: F401

TOKEN = "aaaaaaaa696bbbbbbbb696"


def test_an_entry_without_manager_operation_or_time_is_skipped(api):  # noqa: F811
    api.configmaps = [
        configmap(
            "flags",
            [
                {"fieldsType": "FieldsV1", "fieldsV1": {"f:data": {}}},
                {"subresource": "status"},
                {
                    "manager": "helm",
                    "operation": "Apply",
                    "time": "2026-11-22T07:30:00Z",
                },
            ],
        ),
        configmap("hollow", [{"fieldsV1": {}}]),
    ]
    results = _results(_jq(_worker(api), ".items[].metadata"))
    assert results[0][SUMMARY] == [
        {"manager": "helm", "operation": "Apply", "time": "2026-11-22T07:30:00Z"}
    ]
    assert SUMMARY not in results[1]


def test_a_credential_in_a_manager_name_is_redacted(api):  # noqa: F811
    api.configmaps = [
        configmap(
            "flags",
            [
                {
                    "manager": f"Bearer {TOKEN}",
                    "operation": "Update",
                    "time": "2026-11-22T07:30:00Z",
                }
            ],
        )
    ]
    outcome = _jq(_worker(api), ".items[0].metadata.managedFieldsSummary")
    texts = {
        "view": json.dumps(_view(outcome), default=str),
        "raw": outcome.evidence.raw.decode("utf-8", "replace"),
        "evidence-view": json.dumps(dict(outcome.evidence.view), default=str),
    }
    for where, text in texts.items():
        assert TOKEN not in text, where
