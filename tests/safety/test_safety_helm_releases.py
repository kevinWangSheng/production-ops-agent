"""Safety: the Helm release history tools (B4, #563) keep the identity's
Secret access narrowed to Helm records, and nothing but the twelve row fields
of a record leaves the tool (R8-18, r11). Guarded by mutations M39 and M40.
"""

from __future__ import annotations

import base64
import gzip
import json
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import pytest

from opspilot.tools.executor import TransportRequest
from opspilot.tools.helm_releases import HISTORY_TOOL, HelmReleasesTransport
from opspilot.tools.kubernetes_core import (
    CREDENTIAL_REF,
    ENDPOINT_SELECTOR,
    KubernetesClient,
    KubernetesRequestRefused,
)
from opspilot.tools.outcomes import Window

API = "https://127.0.0.1:6443"
HELM_QUERY = urlencode(
    [
        ("fieldSelector", "type=helm.sh/release.v1"),
        ("labelSelector", "owner=helm"),
        ("limit", "5"),
    ]
)
CANARIES = ("CANARY-SAFE-VALUES", "CANARY-SAFE-MANIFEST", "CANARY-SAFE-NOTES")


class _Response:
    status = 200

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]


class _Opener:
    def __init__(self, body: bytes = b'{"kind":"SecretList","items":[]}') -> None:
        self.body = body
        self.calls: list[str] = []

    def open(self, request, timeout=None):
        self.calls.append(request.full_url)
        return _Response(self.body)


def _client(opener: _Opener) -> KubernetesClient:
    return KubernetesClient(
        credentials={CREDENTIAL_REF: "k8s-ro-safety"},
        opener=opener,  # type: ignore[arg-type]
        helm_releases=True,
    )


@pytest.mark.parametrize(
    "url",
    [
        f"{API}/api/v1/secrets",
        f"{API}/api/v1/namespaces/b4-a/secrets?limit=500",
        f"{API}/api/v1/namespaces/b4-a/secrets/plain-canary",
        f"{API}/api/v1/secrets?{HELM_QUERY}&fieldSelector=type%3DOpaque",
        f"{API}/api/v1/secrets?fieldSelector=type%3DOpaque&{HELM_QUERY}",
    ],
)
def test_with_helm_on_only_the_exact_helm_page_reaches_the_cluster(url):
    opener = _Opener()
    with pytest.raises(KubernetesRequestRefused):
        _client(opener).get(
            url, deadline=time.monotonic() + 5, max_bytes=1024, helm_page=True
        )
    assert opener.calls == []


def test_the_exact_helm_page_is_sent_only_for_the_helm_transport():
    opener = _Opener()
    url = f"{API}/api/v1/secrets?{HELM_QUERY}"
    with pytest.raises(KubernetesRequestRefused):
        _client(opener).get(url, deadline=time.monotonic() + 5, max_bytes=1024)
    assert opener.calls == []
    _client(opener).get(
        url, deadline=time.monotonic() + 5, max_bytes=1024, helm_page=True
    )
    assert opener.calls == [url]


def _secret() -> dict[str, object]:
    record = {
        "name": "shop",
        "namespace": "b4-a",
        "version": 1,
        "info": {
            "status": "deployed",
            "description": "Install complete",
            "last_deployed": "2026-11-20T09:00:00Z",
            "notes": CANARIES[2],
        },
        "chart": {"metadata": {"name": "c", "version": "0.1.0"}, "values": {}},
        "config": {"secret": CANARIES[0]},
        "manifest": CANARIES[1],
    }
    helm_layer = base64.b64encode(gzip.compress(json.dumps(record).encode()))
    return {
        "metadata": {
            "name": "sh.helm.release.v1.shop.v1",
            "namespace": "b4-a",
            "labels": {"owner": "helm", "name": "shop", "version": "1"},
        },
        "type": "helm.sh/release.v1",
        "data": {"release": base64.b64encode(helm_layer).decode()},
    }


def test_values_manifest_and_notes_never_leave_the_tool():
    page = json.dumps({"kind": "SecretList", "items": [_secret()]}).encode()
    transport = HelmReleasesTransport(client=_client(_Opener(page)))
    now = datetime(2026, 11, 20, 10, tzinfo=timezone.utc)
    response = transport.fetch(
        TransportRequest(
            operation_id="safety-b4",
            source="otel-demo",
            verb="list",
            endpoint="http://127.0.0.1:9090",
            selector={ENDPOINT_SELECTOR: API},
            params={"release_name": "shop", "namespace": "b4-a"},
            window=Window(now - timedelta(hours=1), now),
            timeout_seconds=5,
            max_result_bytes=2 * 1024 * 1024,
            credential_ref="otel-demo-ro",
            tool=HISTORY_TOOL,
        )
    )
    (row,) = json.loads(response.body)["results"]
    assert (row["name"], row["revision"], row["status"]) == ("shop", 1, "deployed")
    text = response.body.decode() + json.dumps(response.view_fields)
    for canary in CANARIES:
        assert canary not in text
