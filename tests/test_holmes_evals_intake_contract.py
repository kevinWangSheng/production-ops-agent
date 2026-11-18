"""#666 PR-B contract: the web intake client and the evaluation's target
identity (H-10, R10-1, H-U1).

Independent contract tests written from the task record's PR-B public
interface (``intake.py``) without reading the implementation. A capturing
HTTP server on 127.0.0.1 stands in for the OpsPilot web process.

Rules under test:

* ``POST /intake/ui``, form encoded, Basic auth with the round's UI account,
  ``Origin`` = the web root, ``Accept: application/json``;
* the form's ``question`` is exactly the text given (the driver passes
  ``user_prompt`` verbatim apart from the R10-1 time rewrite; no cluster-name
  prefix, H-U1), ``target_id`` defaults to ``m0-otel-20260909``;
* the idempotency key is ``holmes-<round>-<qid>-t<trial>-c<chance>`` and is
  a valid product identifier;
* loopback only, redirects are not followed;
* the UI account's password never shows in ``repr``; ``users_env`` is a
  ``name=<pbkdf2>`` entry the product verifies;
* ``write_identities`` writes a file the product's loader accepts.

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* "<题号>" in the key: the interface defines 题号 as the directory name's
  part before the first ``_`` (``index_pattern``) but passes the whole
  directory name as ``qid``; both readings are accepted here (reported as a
  spec gap).
* A 4xx answer comes back as a ``SubmitResult`` carrying the product's
  ``code`` or as an exception with ``.code``; either is accepted.
"""

from __future__ import annotations

import base64
import json
import threading
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from opspilot.intake import IntakeRequest
from opspilot.schema import load_target_identities
from opspilot.web.auth import verify_password
from tests.holmes_evals_b_support import no_proxy_env  # noqa: F401 - fixture
from tests.holmes_evals_support import mod

pytestmark = pytest.mark.usefixtures("no_proxy_env")

QUESTION = (
    "Why did checkout-275 fail between 2026-11-18T09:58:01Z and now?\n"
    "Use index app-275-logs & check 'severity=ERROR' + \"level\" 100% — ünïcode."
)


class _Capture:
    def __init__(self, status: int, body: Any, headers: dict[str, str] | None = None):
        self.status = status
        self.body = body
        self.headers = headers or {}
        self.requests: list[dict[str, Any]] = []


@contextmanager
def _web(
    status: int = 201, body: Any = None, headers=None
) -> Iterator[tuple[str, _Capture]]:
    capture = _Capture(
        status,
        body
        if body is not None
        else {
            "incident_id": "11111111-1111-4111-8111-111111111111",
            "run_id": "22222222-2222-4222-8222-222222222222",
            "replayed": False,
            "sequence": 1,
        },
        headers,
    )

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            capture.requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": self.rfile.read(length),
                }
            )
            raw = json.dumps(capture.body).encode()
            self.send_response(capture.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for k, v in capture.headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        do_POST = _serve
        do_GET = _serve

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", capture
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _name(account: Any) -> str:
    return account.users_env().split("=", 1)[0]


def test_constants() -> None:
    intake = mod("intake")
    assert intake.TARGET_ID == "m0-otel-20260909"
    assert intake.CLUSTER_UID == "holmes-evals"


def test_ui_account_random_hidden_and_verifiable() -> None:
    intake = mod("intake")
    a, b = intake.new_account(), intake.new_account()
    assert a.password and b.password and a.password != b.password
    assert a.password not in repr(a)
    entry = a.users_env()
    name, sep, encoded = entry.partition("=")
    assert sep and name and "," not in entry
    assert a.password not in entry
    assert verify_password(a.password, encoded)
    assert not verify_password(b.password, encoded)


@pytest.mark.parametrize(
    ("rnd", "qid", "trial", "chance"),
    [
        ("r1", "275_es_field_name_correction", 2, 1),
        ("20261118", "183b_elasticsearch_index_discovery", 1, 3),
    ],
)
def test_idempotency_key_format(rnd: str, qid: str, trial: int, chance: int) -> None:
    key = mod("intake").idempotency_key(rnd, qid, trial, chance)
    number = qid.split("_", 1)[0]
    # ASSUMPTION: either reading of <题号> (see module docstring).
    assert key in {
        f"holmes-{rnd}-{qid}-t{trial}-c{chance}",
        f"holmes-{rnd}-{number}-t{trial}-c{chance}",
    }
    IntakeRequest(target_id="m0-otel-20260909", question="q", idempotency_key=key)


def test_idempotency_keys_distinct_per_trial_and_chance() -> None:
    f = mod("intake").idempotency_key
    keys = {
        f("r1", q, t, c)
        for q in ("275_es_field_name_correction", "276_es_numeric_severity_field")
        for t in (1, 2, 3)
        for c in (1, 2, 3)
    }
    assert len(keys) == 18


def test_write_identities_loads_in_product(tmp_path: Path) -> None:
    intake = mod("intake")
    path = tmp_path / "identities.json"
    intake.write_identities(path)
    loaded = load_target_identities(path)
    assert set(loaded) == {intake.TARGET_ID}
    entry = loaded[intake.TARGET_ID]
    assert entry["integration_id"] == intake.TARGET_ID
    assert entry["cluster_uid"] == intake.CLUSTER_UID
    assert entry["health_profile_id"] == "otel-demo-checkout"


def test_submit_question_request_shape() -> None:
    intake = mod("intake")
    account = intake.new_account()
    key = intake.idempotency_key("r1", "275_es_field_name_correction", 1, 1)
    with _web() as (url, capture):
        result = intake.submit_question(url, account, question=QUESTION, key=key)
    assert result.status == 201
    assert str(result.incident_id) == "11111111-1111-4111-8111-111111111111"
    assert str(result.run_id) == "22222222-2222-4222-8222-222222222222"
    assert result.replayed is False
    assert result.code is None
    (req,) = capture.requests
    assert req["method"] == "POST"
    assert req["path"] == "/intake/ui"
    h = req["headers"]
    assert h["content-type"].startswith("application/x-www-form-urlencoded")
    assert h["accept"] == "application/json"
    assert h["origin"] == url
    scheme, _, token = h["authorization"].partition(" ")
    assert scheme == "Basic"
    user, _, password = base64.b64decode(token).decode().partition(":")
    assert (user, password) == (_name(account), account.password)
    form = urllib.parse.parse_qs(req["body"].decode("ascii"), keep_blank_values=True)
    assert form == {
        "target_id": ["m0-otel-20260909"],
        "question": [QUESTION],
        "idempotency_key": [key],
    }


def test_submit_question_replay_and_target_override() -> None:
    intake = mod("intake")
    account = intake.new_account()
    body = {
        "incident_id": "11111111-1111-4111-8111-111111111111",
        "run_id": "22222222-2222-4222-8222-222222222222",
        "replayed": True,
        "sequence": 1,
    }
    with _web(200, body) as (url, capture):
        result = intake.submit_question(
            url + "/", account, question="q", key="holmes-r1-x-t1-c1", target_id="t-2"
        )
    assert (result.status, result.replayed) == (200, True)
    form = urllib.parse.parse_qs(capture.requests[0]["body"].decode("ascii"))
    assert form["target_id"] == ["t-2"]
    assert capture.requests[0]["headers"]["origin"] == url


def test_submit_question_refusal_carries_code_not_secrets() -> None:
    intake = mod("intake")
    account = intake.new_account()
    with _web(400, {"code": "INVALID_INPUT"}) as (url, _):
        try:
            result = intake.submit_question(url, account, question="q", key="k")
        except Exception as error:  # noqa: BLE001 - either shape is allowed
            assert getattr(error, "code", None)
            assert account.password not in str(error)
        else:
            assert result.status == 400
            assert result.code == "INVALID_INPUT"
            assert account.password not in repr(result)


def test_submit_question_does_not_follow_redirects() -> None:
    intake = mod("intake")
    account = intake.new_account()
    with _web(303, {}, {"Location": "/incidents/abc"}) as (url, capture):
        try:
            result = intake.submit_question(url, account, question="q", key="k")
        except Exception:  # noqa: BLE001 - a refusal is fine
            result = None
        assert [r["path"] for r in capture.requests] == ["/intake/ui"]
    if result is not None:
        assert result.status == 303
        assert result.incident_id is None or not str(result.incident_id)


class _RecordingOpener:
    """Would answer 201; records whether the client tried to open anything."""

    def __init__(self) -> None:
        self.opened: list[Any] = []

    def open(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        self.opened.append(request)
        raise AssertionError("a non-loopback address must be refused before I/O")


@pytest.mark.parametrize(
    "url",
    ["http://192.0.2.10:29280", "http://example.invalid:29280", "https://10.1.2.3"],
)
def test_submit_question_refuses_non_loopback(url: str) -> None:
    intake = mod("intake")
    opener = _RecordingOpener()
    with pytest.raises(Exception) as caught:  # noqa: B017 - any refusal
        intake.submit_question(
            url, intake.new_account(), question="q", key="k", opener=opener
        )
    assert not isinstance(caught.value, AssertionError)
    assert opener.opened == []
