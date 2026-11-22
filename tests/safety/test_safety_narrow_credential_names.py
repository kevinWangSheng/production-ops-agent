"""Safety: #366 narrows which names count as credentials (user decision
B1-U3 a, B1-D8) without opening real credentials to the model.

Through the shipped ``otel-demo-ext`` gateway, a Pod whose spec and
annotations carry token counts, session settings, session credentials and
``imagePullSecrets``: counts (a bare number under a name ending in
``tokens``), session settings and the pull-Secret *name* reach the model;
a non-numeric ``access_tokens`` value, session identifiers and every other
field of an ``imagePullSecrets`` entry never do. Guarded by mutations
M47-M50.
"""

from __future__ import annotations

import json

from opspilot.redaction import REDACTED_CREDENTIAL, redact_credentials, redact_payload
from tests.safety.gateway_support import everything_seen, gateway

JQ_CALL = {"kind": "pods", "jq_expr": ".items[]"}
#: Synthetic values that must never be visible (low entropy).
HIDDEN = {
    "access_tokens": "canary-access-61",
    "session_id": "canary-session-62",
    "sessionid": "canary-session-63",
    "session": "canary-session-64",
}
PULL_HIDDEN = "canary-pull-65"


def _pod() -> bytes:
    pod = {
        "metadata": {
            "name": "orders-0",
            "namespace": "otel-demo",
            "annotations": {
                # prose forms, as a config dump or a log line would carry them
                "llm/usage": "prompt_tokens=1234 max_tokens: 4096",
                "llm/creds": " ".join(f"{k}={v}" for k, v in HIDDEN.items()),
            },
        },
        "spec": {
            "sessionAffinity": "ClientIP",
            "imagePullSecrets": [
                {"name": "orders-registry-ref", "token": PULL_HIDDEN},
            ],
            "containers": [{"name": "orders"}],
        },
        "status": {"usage": {"total_tokens": 31000}},
    }
    return json.dumps({"kind": "PodList", "items": [pod]}).encode()


def test_counts_settings_and_pull_secret_names_reach_the_model_values_do_not(
    monkeypatch, tmp_path
):
    gw = gateway(monkeypatch, tmp_path, body=_pod())
    outcome = gw.call("kubernetes_jq_query", JQ_CALL)
    assert gw.source.requests, "the call must reach the source"
    assert outcome.status == "ok", outcome.model_view
    seen = everything_seen(outcome, gw.evidence)
    for visible in ("1234", "4096", "31000", "ClientIP", "orders-registry-ref"):
        assert visible in seen, visible
    for value in (*HIDDEN.values(), PULL_HIDDEN):
        assert value not in seen, value


def test_a_non_numeric_access_tokens_value_is_still_redacted():
    """M47: the count exemption needs the whole value to be a number."""
    for text in (
        f"access_tokens={HIDDEN['access_tokens']}",
        f'"access_tokens": "{HIDDEN["access_tokens"]}"',
        "access_tokens: 12abc",
    ):
        assert redact_credentials(text).endswith(
            (REDACTED_CREDENTIAL, f'"{REDACTED_CREDENTIAL}"')
        ), text
    payload = {"access_tokens": HIDDEN["access_tokens"], "prompt_tokens": "12"}
    assert redact_payload(payload) == {
        "access_tokens": REDACTED_CREDENTIAL,
        "prompt_tokens": REDACTED_CREDENTIAL,
    }


def test_redis_session_store_values_stay_redacted(monkeypatch, tmp_path):
    """A stored web session is not a session *setting*: a string key or a
    hash field named for a session keeps its value hidden after #366."""
    from tests.redis_support import GET_VALUE_TOOL, make_tools

    monkeypatch.setenv("C6C_TEST_K8S_CA_FILE", str(tmp_path / "ca.pem"))
    (tmp_path / "ca.pem").write_text("synthetic ca placeholder\n")
    tools = make_tools(monkeypatch)
    tools.server.data.update(
        {
            "session:4f1c": ("string", b"canary-session-66"),
            "spring:session:sessions:4f1c": (
                "hash",
                {"sessionAttr:ctx": "canary-session-67", "creationTime": "1763812800"},
            ),
        }
    )
    seen = ""
    for key in ("session:4f1c", "spring:session:sessions:4f1c"):
        outcome = tools.run(GET_VALUE_TOOL, {"key": key})
        assert outcome.status == "ok", outcome.model_view
        seen += everything_seen(outcome, tools.sinks[-1])
    assert "canary-session-66" not in seen and "canary-session-67" not in seen
    assert "1763812800" in seen  # positive control: other fields shown


def test_a_count_name_whose_prefix_is_a_credential_name_stays_redacted():
    """M48: ``auth_tokens``, ``session_tokens``, ``cookie_tokens`` are
    credential names first; a number under them is not a count."""
    for name in ("auth_tokens", "authTokens", "session_tokens", "cookie_tokens"):
        for text in (f"{name}: 5", f"{name}=5", f'"{name}": 5', f'{{\\"{name}\\": 5}}'):
            assert "5" not in redact_credentials(text), text
        assert redact_payload({name: 123456}) == {name: REDACTED_CREDENTIAL}, name
    assert redact_credentials("prompt_tokens: 5") == "prompt_tokens: 5"  # control


def test_session_credential_words_and_code_tokens_stay_redacted():
    """M49/M50: ``session`` before a word naming the session itself
    (``session_jwt``, ``sessionStore``) is a credential, and a one-time or
    refresh code count (``otp_tokens``, ``refreshTokens``) is not a count;
    session status and ordinary counts stay visible."""
    hidden = (
        *("session_jwt", "session_ticket", "session_sid", "session_uuid"),
        *("session_nonce", "session_hash", "session_value", "session_data"),
        *("session_payload", "session_blob", "session_string", "session_bytes"),
        *("sessionHandle", "sessionStore", "session_values"),
        *("otp_tokens", "pin_tokens", "mfa_tokens", "passcode_tokens"),
        *("refresh_tokens", "refreshTokens"),
    )
    for name in hidden:
        for text in (f"{name}: 5", f'"{name}": 5'):
            assert "5" not in redact_credentials(text), text
        assert redact_payload({name: 5}) == {name: REDACTED_CREDENTIAL}, name
    visible = (
        *("stored_session_state", "replayed_session_state", "session_consistent"),
        *("session_sustained", "session.max_samples", "session_purpose"),
        *("prompt_tokens", "max_tokens", "view_tokens", "total_tokens"),
        *("access_tokens", "api_tokens", "reasoning_tokens"),
    )
    for name in visible:
        assert redact_credentials(f"{name}: 5") == f"{name}: 5", name
