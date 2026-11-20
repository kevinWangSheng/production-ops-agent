"""Implementer's unit checks for ``opspilot.tools.argocd_apps`` against the
CI seed objects (``tests/fixtures/c4_argocd/apps.yaml``), with a fake A2
client: every canary position is dropped, and the two kinds of 404."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from opspilot.tools.argocd_apps import ArgoCDTransport
from opspilot.tools.executor import TransportRequest
from opspilot.tools.kubernetes_core import _Fetched
from opspilot.tools.outcomes import Window

FIXTURES = Path(__file__).parent / "fixtures" / "c4_argocd"
END = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def _apps() -> list[dict[str, Any]]:
    text = (
        (FIXTURES / "apps.yaml")
        .read_text()
        .replace("__RECENT__", "2026-11-20T11:00:00Z")
    )
    return [i for i in json.loads(text)["items"] if i["kind"] == "Application"]


class _Client:
    def __init__(self, status: int | None = None, body: bytes = b"") -> None:
        self.urls: list[str] = []
        self.status, self.body = status, body

    def token(self) -> str:
        return "t"

    def get(self, url: str, **kwargs: Any) -> _Fetched:
        self.urls.append(url)
        if self.status is not None:
            return _Fetched(self.status, self.body)
        path = url.split("?", 1)[0]
        apps = _apps()
        if "/applications/" in path:
            name = path.rsplit("/", 1)[1]
            (app,) = [a for a in apps if a["metadata"]["name"] == name]
            return _Fetched(200, json.dumps(app).encode())
        return _Fetched(
            200, json.dumps({"kind": "ApplicationList", "items": apps}).encode()
        )


def _request(tool: str, params: dict[str, Any]) -> TransportRequest:
    return TransportRequest(
        operation_id="op",
        source="otel-demo",
        verb="get",
        endpoint="http://127.0.0.1:1",
        selector={"kubernetes_endpoint": "https://127.0.0.1:6443"},
        params=params,
        window=Window(END - timedelta(hours=1), END),
        timeout_seconds=5.0,
        max_result_bytes=2 * 1024 * 1024,
        credential_ref="otel-demo",
        tool=tool,
    )


def test_no_canary_position_reaches_the_body_or_the_view():
    canaries = (FIXTURES / "canaries.txt").read_text().split()
    stored = json.dumps(_apps())
    assert all(c in stored for c in canaries)  # positive control
    for tool, params in (
        ("argocd_app_list", {}),
        ("argocd_app_get", {"app_name": "c4-canary"}),
        ("argocd_app_history", {"app_name": "c4-canary"}),
        ("argocd_app_resources", {"app_name": "c4-canary"}),
    ):
        client = _Client()
        response = ArgoCDTransport(client=client).fetch(_request(tool, params))  # type: ignore[arg-type]
        seen = response.body.decode() + json.dumps(response.view_fields)
        leaked = [c for c in canaries if c in seen]
        assert not leaked, (tool, leaked)
        assert all(
            "/applications" in u and "applicationsets" not in u for u in client.urls
        )
    client = _Client()
    response = ArgoCDTransport(client=client).fetch(  # type: ignore[arg-type]
        _request("argocd_app_get", {"app_name": "c4-canary"})
    )
    (row,) = json.loads(response.body)["results"]
    assert row["sources"][0]["repo_url"] == "https://git.example.test/c4/canary.git"


def test_history_is_ascending_and_not_cut_to_the_window():
    response = ArgoCDTransport(client=_Client()).fetch(  # type: ignore[arg-type]
        _request("argocd_app_history", {"app_name": "c4-history"})
    )
    rows = json.loads(response.body)["results"]
    assert [r["id"] for r in rows] == list(range(1, 13))
    assert response.source_start_at is None and response.data_as_of is None


def test_named_404_with_the_name_is_no_data_and_without_it_crd_missing():
    status = {"kind": "Status", "reason": "NotFound", "details": {"name": "ghost"}}
    response = ArgoCDTransport(
        client=_Client(404, json.dumps(status).encode())  # type: ignore[arg-type]
    ).fetch(_request("argocd_app_get", {"app_name": "ghost"}))
    assert response.source_status is None and json.loads(response.body) == {
        "results": []
    }
    assert "APP_NOT_FOUND" in json.dumps(response.view_fields)
    bare = {"kind": "Status", "reason": "NotFound", "details": {}}
    response = ArgoCDTransport(
        client=_Client(404, json.dumps(bare).encode())  # type: ignore[arg-type]
    ).fetch(_request("argocd_app_get", {"app_name": "ghost"}))
    assert response.source_status == "CRD_MISSING"
