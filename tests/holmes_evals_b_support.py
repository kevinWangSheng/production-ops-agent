"""Shared helpers for the #666 PR-B contract tests (HolmesGPT public eval set).

Written from docs/tasks/2026-11-18-holmes-evals-666.md (section "PR-B 公开
接口") and the pre-review H-4, H-5, H-7, H-8, H-10, H-11, H-19..H-28, without
reading the PR-B implementation.

No real Elasticsearch, kind, model or network: the fake Elasticsearch runs in
process on 127.0.0.1, every HTTP client here bypasses proxies, and all
credentials are low-entropy, obviously fake values.
"""

from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

import pytest

from tests.holmes_evals_support import mod

# Obviously fake, low-entropy credentials (gitleaks: PR-A precedent).
ENGINEER = "engineer-fake-value-1"
OTHER = "not-a-known-value-2"

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PROXY_VARS = (
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "no_proxy",
    "NO_PROXY",
)


@pytest.fixture
def no_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host may route through a local proxy; loopback tests must not."""
    for name in PROXY_VARS:
        monkeypatch.delenv(name, raising=False)


def apikey(value: str) -> dict[str, str]:
    return {"Authorization": f"ApiKey {value}"}


def ndjson(lines: Sequence[Mapping[str, Any]]) -> bytes:
    return ("\n".join(json.dumps(line) for line in lines) + "\n").encode()


def jbody(doc: Any) -> bytes:
    return json.dumps(doc).encode()


class Es:
    """Calls ``FakeElasticsearch.handle`` with the ES REST shapes."""

    def __init__(self, store: Any, key: str = ENGINEER) -> None:
        self.store = store
        self.key = key

    def __call__(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        query: Mapping[str, Sequence[str] | str] | None = None,
        key: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, Any]:
        if body is None:
            raw = b""
        elif isinstance(body, bytes):
            raw = body
        else:
            raw = jbody(body)
        q = {
            k: [v] if isinstance(v, str) else list(v) for k, v in (query or {}).items()
        }
        h = dict(headers) if headers is not None else apikey(key or self.key)
        if raw and "Content-Type" not in h:
            h["Content-Type"] = (
                "application/x-ndjson" if path.endswith("_bulk") else "application/json"
            )
        return self.store.handle(method, path, q, h, raw)

    def ok(self, method: str, path: str, body: Any = None, **kw: Any) -> Any:
        status, payload = self(method, path, body, **kw)
        assert 200 <= status < 300, (method, path, status, payload)
        return payload

    def create(self, index: str, properties: Mapping[str, Any] | None = None) -> None:
        body: dict[str, Any] = {
            "settings": {"number_of_shards": 1, "number_of_replicas": 0}
        }
        if properties is not None:
            body["mappings"] = {"properties": dict(properties)}
        self.ok("PUT", f"/{index}", body, query={"wait_for_active_shards": "1"})

    def bulk(
        self, index: str, docs: Sequence[Mapping[str, Any]], ids: bool = True
    ) -> Any:
        lines: list[Mapping[str, Any]] = []
        for i, doc in enumerate(docs):
            lines.append({"index": {"_id": f"d{i}"}} if ids else {"index": {}})
            lines.append(doc)
        out = self.ok("POST", f"/{index}/_bulk", ndjson(lines))
        assert out.get("errors") is False, out
        self.ok("POST", f"/{index}/_refresh")
        return out

    def search(self, index: str, body: Mapping[str, Any]) -> Any:
        return self.ok("POST", f"/{index}/_search", dict(body))

    def sources(self, index: str) -> dict[str, dict[str, Any]]:
        out = self.search(index, {"query": {"match_all": {}}, "size": 10000})
        return {h["_id"]: h["_source"] for h in out["hits"]["hits"]}


def error_type(payload: Any) -> str | None:
    if isinstance(payload, Mapping) and isinstance(payload.get("error"), Mapping):
        value = payload["error"].get("type")
        return value if isinstance(value, str) else None
    return None


def new_store(key: str = ENGINEER) -> Any:
    return mod("fake_es").FakeElasticsearch(key)


@contextmanager
def served(store: Any) -> Iterator[str]:
    """``serve`` on 127.0.0.1 with an ephemeral port; yields the base URL."""
    server = mod("fake_es").serve(store, "127.0.0.1", 0)
    host, port = server.server_address[:2]
    thread: threading.Thread | None = None
    if not _answers(f"http://{host}:{port}/"):
        # ASSUMPTION: the interface does not say whether ``serve`` already
        # runs ``serve_forever``; start it here when nothing answers.
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(timeout=5)


def _answers(url: str) -> bool:
    try:
        with _OPENER.open(urllib.request.Request(url, method="GET"), timeout=1.5):
            return True
    except urllib.error.HTTPError:
        return True
    except (urllib.error.URLError, OSError):
        return False


def http(
    method: str,
    url: str,
    key: str | None = ENGINEER,
    body: Any = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, Any]:
    data = None if body is None else (body if isinstance(body, bytes) else jbody(body))
    h = {"Content-Type": "application/json"}
    if key is not None:
        h["Authorization"] = f"ApiKey {key}"
    h.update(headers or {})
    request = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with _OPENER.open(request, timeout=10) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, raw.decode("utf-8", "replace")


def encoded(key_id: str, api_key: str) -> str:
    return base64.b64encode(f"{key_id}:{api_key}".encode()).decode("ascii")
