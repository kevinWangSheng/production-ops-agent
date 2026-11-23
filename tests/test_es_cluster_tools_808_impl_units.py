"""Implementer unit checks for #808 beyond the independent contract file."""

from __future__ import annotations

import pytest

from opspilot.tools.elasticsearch_cluster import NODES_STATS_TOOL, _plan


@pytest.mark.parametrize("node_id", [".", "..", "...", "a..b"])
def test_dot_only_node_ids_are_refused_before_sending(node_id):
    refused = _plan(NODES_STATS_TOOL, {"node_id": node_id})
    assert not isinstance(refused, tuple)
    assert refused.sent is False
    assert refused.source_status == "INVALID_NODE_ID"


def test_a_dotted_node_name_is_still_accepted():
    path, *_ = _plan(NODES_STATS_TOOL, {"node_id": "es.node-1"})
    assert path == "/_nodes/es.node-1/stats"
