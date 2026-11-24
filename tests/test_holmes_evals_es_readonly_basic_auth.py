"""#666: ``--es external`` without ``--es-readonly-env`` creates the product's
read-only key with the admin's basic credentials.

Real Elasticsearch 8 refuses an API-key-authenticated request that derives a
key with privileges ("creating derived api keys requires an explicit role
descriptor that is empty"), so the engineer's API key cannot mint the B1-5
key. The stand-in below answers exactly that way to ``ApiKey`` and accepts
``Basic``; the real ``scripts/es_readonly_identity.py`` subprocess runs
against it.
"""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from scripts.holmes_evals import driver

ENGINEER = "engineer-fake-value-1"
ADMIN_USER = "elastic"
ADMIN_PASSWORD = "admin-fake-password-2"
MINTED_ID = "ro-id"
MINTED_RAW = "ro-raw-3"
MINTED = base64.b64encode(f"{MINTED_ID}:{MINTED_RAW}".encode()).decode()
#: B1-5's role, written out here so a change to the script's constant fails.
B1_5_ROLE = {
    "opspilot_readonly": {
        "cluster": ["monitor"],
        "indices": [
            {"names": ["*"], "privileges": ["read", "view_index_metadata", "monitor"]}
        ],
    }
}
DERIVED_KEY_ERROR = {
    "error": {
        "type": "illegal_argument_exception",
        "reason": "creating derived api keys requires an explicit role "
        "descriptor that is empty (has no privileges)",
    },
    "status": 400,
}


class _StubEs:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                auth = self.headers.get("Authorization", "")
                stub.requests.append({"path": self.path, "auth": auth, "body": body})
                basic = (
                    "Basic "
                    + base64.b64encode(
                        f"{ADMIN_USER}:{ADMIN_PASSWORD}".encode()
                    ).decode()
                )
                if self.path != "/_security/api_key":
                    status, answer = 404, {"error": {"type": "not_found"}}
                elif auth == f"ApiKey {ENGINEER}" and body.get("role_descriptors"):
                    status, answer = 400, DERIVED_KEY_ERROR
                elif auth == basic:
                    status, answer = (
                        200,
                        {
                            "id": MINTED_ID,
                            "name": body.get("name"),
                            "api_key": MINTED_RAW,
                            "encoded": MINTED,
                        },
                    )
                else:
                    status, answer = 401, {"error": {"type": "security_exception"}}
                data = json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def stub() -> Iterator[_StubEs]:
    es = _StubEs()
    yield es
    es.server.shutdown()
    es.server.server_close()


def _cfg(tmp_path: Path, es_url: str) -> driver.Config:
    return driver.Config(
        round="t1",
        upstream=tmp_path / "upstream",
        es_mode="external",
        es_url=es_url,
        tmp_root=tmp_path / "tmp",
        evidence_root=tmp_path / "evidence",
    )


def _all_text(root: Path) -> str:
    return "".join(
        p.read_text(errors="replace") for p in root.rglob("*") if p.is_file()
    )


def test_external_mints_read_only_key_with_basic_auth(
    tmp_path: Path, stub: _StubEs, capfd: pytest.CaptureFixture[str]
) -> None:
    cfg = _cfg(tmp_path, stub.url)
    cfg.round_dir.mkdir(parents=True)
    private = {
        "ELASTICSEARCH_API_KEY": ENGINEER,
        "ELASTICSEARCH_USERNAME": ADMIN_USER,
        "ELASTICSEARCH_PASSWORD": ADMIN_PASSWORD,
    }

    driver._prepare_es(cfg, driver.Backends(), driver.Round(), private)

    assert cfg.product_es == {
        "OPSPILOT_ES_URL": stub.url,
        "OPSPILOT_ES_API_KEY": MINTED,
    }
    assert cfg.engineer_key == ENGINEER  # still the fixtures' credential
    assert len(stub.requests) == 1
    request = stub.requests[0]
    assert request["auth"].startswith("Basic ")
    assert request["body"]["role_descriptors"] == B1_5_ROLE
    # The admin credential stays out of output, files and the config.
    out = capfd.readouterr()
    seen = out.out + out.err + _all_text(tmp_path) + repr(cfg)
    assert ADMIN_PASSWORD not in seen
    assert ENGINEER not in _all_text(tmp_path)


def test_external_without_admin_or_readonly_env_fails_closed(
    tmp_path: Path, stub: _StubEs
) -> None:
    cfg = _cfg(tmp_path, stub.url)
    cfg.round_dir.mkdir(parents=True)

    with pytest.raises(driver.RoundAbort) as caught:
        driver._prepare_es(
            cfg, driver.Backends(), driver.Round(), {"ELASTICSEARCH_API_KEY": ENGINEER}
        )

    assert caught.value.code == "ES_ADMIN_CREDENTIALS_MISSING"
    assert stub.requests == []
