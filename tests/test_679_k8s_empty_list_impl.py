"""Implementer test for #679 review hardening: an empty 200 page that is not a
genuine List response (``kind`` ending in ``List``, ``items`` a JSON list)
never becomes an ``ok`` "no objects" answer, on the first or a later page."""

from __future__ import annotations

import pytest

from tests.test_679_k8s_empty_list_contract import (  # noqa: F401 -- fixtures
    LIST_TOOLS,
    NS,
    _call,
    _params,
    _view,
    api,
    np_page,
)
from tests.test_tool_expansion_b4_contract import (  # noqa: F401 -- fixtures
    _clean_environment,
    _json,
    tls_pair,
)

NOT_A_LIST = {
    "empty-object": b"{}",
    "items-null": b'{"kind":"NetworkPolicyList","items":null}',
    "no-kind": b'{"items":[]}',
    "status": _json({"kind": "Status", "status": "Failure", "code": 200}),
}


@pytest.mark.parametrize("later", [False, True], ids=["first-page", "later-page"])
@pytest.mark.parametrize("body", list(NOT_A_LIST.values()), ids=list(NOT_A_LIST))
@pytest.mark.parametrize("tool", LIST_TOOLS)
def test_an_empty_page_that_is_not_a_list_is_never_ok(api, tool, body, later):  # noqa: F811
    pages = [(200, body)]
    if later:
        pages.insert(0, (200, np_page([], token="np+1/=")))
    api.np_pages = pages
    outcome = _call(api, tool, _params(tool, "networkpolicies", NS))
    view = _view(outcome)
    assert outcome.status != "ok", view
    assert view.get("citable_as_fact") is not True
