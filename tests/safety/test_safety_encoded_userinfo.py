"""Safety: URL userinfo stays hidden when the authority's ``//`` is encoded
(#359, user decision B1-U4 a, B1-D7).

Through the shipped ``otel-demo-ext`` gateway, a Pod whose annotations hold
connection strings with the ``//`` percent-encoded, escaped two levels deep
and written as HTML entities: the password never reaches the model and the
host still does, in the #319 shape (prefix as written, marker, ``@``, host).
Ordinary encoded text -- a ``%40`` in a query string, a redirect URL, an
``&#x2F;`` path -- passes unchanged. Guarded by mutations M51, M52 and M53.
"""

from __future__ import annotations

import json

import pytest

from opspilot.redaction import REDACTED_CREDENTIAL, redact_credentials
from tests.safety.gateway_support import everything_seen, gateway

JQ_CALL = {"kind": "pods", "jq_expr": ".items[]"}
#: Synthetic passwords (low entropy), one per encoding.
PERCENT = "canary-pct-71"
DEEP = "canary-deep-72"
ENTITY = "canary-ent-73"
ENCODED = {
    "percent": f"https%3A%2F%2Forders%3A{PERCENT}%40db-pct.example%2Forders",
    "deep": f"https:\\\\\\/\\\\\\/orders:{DEEP}@db-deep.example",
    "entity": f"https:&#x2F;&#x2F;orders:{ENTITY}&#64;db-ent.example",
}
#: Must pass unchanged.
ORDINARY = (
    "/search?q=a%40b.com&page=2",
    "redirect=https%3A%2F%2Fexample.com%3Fnext%3Dbob%40x.com",
    "next=%2F%2Fcdn.example.com%2Fa%40b.png",
    '<a href="&#x2F;docs&#x2F;x">docs</a>',
    "&#x2F;&#x2F;cdn.example.com&#x2F;img.png",
    "git@github.com:org/repo.git",
)


def _pod() -> bytes:
    annotations = {f"conn/{k}": v for k, v in ENCODED.items()}
    annotations.update({f"plain/{i}": text for i, text in enumerate(ORDINARY)})
    pod = {
        "metadata": {
            "name": "orders-0",
            "namespace": "otel-demo",
            "annotations": annotations,
        },
        "spec": {"containers": [{"name": "orders"}]},
    }
    return json.dumps({"kind": "PodList", "items": [pod]}).encode()


def test_encoded_userinfo_never_reaches_the_model(monkeypatch, tmp_path):
    gw = gateway(monkeypatch, tmp_path, body=_pod())
    outcome = gw.call("kubernetes_jq_query", JQ_CALL)
    assert gw.source.requests, "the call must reach the source"
    assert outcome.status == "ok", outcome.model_view
    seen = everything_seen(outcome, gw.evidence)
    for password in (PERCENT, DEEP, ENTITY):
        assert password not in seen, password
    for host in ("db-pct.example", "db-deep.example", "db-ent.example"):
        assert host in seen, host
    for text in ORDINARY[2:]:  # JSON keeps these bytes as written
        assert json.dumps(text)[1:-1] in seen, text


@pytest.mark.parametrize("kind", sorted(ENCODED))
def test_each_encoding_keeps_the_319_shape(kind):
    """M51-M53: one rule per encoding, prefix as written, a literal ``@``."""
    prefix = {"percent": "https%3A%2F%2F", "deep": "https:\\\\\\/\\\\\\/"}.get(
        kind, "https:&#x2F;&#x2F;"
    )
    host = {"percent": "db-pct.example", "deep": "db-deep.example"}.get(
        kind, "db-ent.example"
    )
    assert redact_credentials(ENCODED[kind]).startswith(
        f"{prefix}{REDACTED_CREDENTIAL}@{host}"
    )


def test_ordinary_encoded_text_is_left_alone():
    for text in ORDINARY:
        assert redact_credentials(text) == text, text
