"""Safety: a Pod spec's Secret *reference names* reach the model, Secret
values never do (2026-11-22 user decision after SREGym problem 13).

Through the shipped ``otel-demo-ext`` gateway: ``env[].valueFrom.secretKeyRef``,
``envFrom[].secretRef`` and ``volumes[].secret`` show which Secret and key a
Pod reads; any other field inside them is replaced, a credential-shaped
string in a readable field is still redacted, and Secret objects stay
unreadable (A2-6). Guarded by mutations M41 and M42.
"""

from __future__ import annotations

import json

import pytest

from tests.safety.gateway_support import everything_seen, gateway

JQ_CALL = {"kind": "pods", "jq_expr": ".items[]"}
#: Reference names that must be visible (low entropy, synthetic).
NAMES = ("orders-db-ref", "orders-db-key", "orders-env-ref", "orders-tls-ref")
#: Values that must never be visible (low entropy, synthetic).
HIDDEN = (
    "canary-ref-sibling-41",
    "canary-ref-assign-42",
    "canary-ref-data-43",
    "canary-ref-nested-44",
)


def _pod(secret_key_ref: dict, secret_ref: dict, volume_secret: dict) -> bytes:
    pod = {
        "metadata": {"name": "orders-0", "namespace": "otel-demo"},
        "spec": {
            "containers": [
                {
                    "name": "orders",
                    "env": [
                        {
                            "name": "DB_PASS",
                            "valueFrom": {"secretKeyRef": secret_key_ref},
                        }
                    ],
                    "envFrom": [{"secretRef": secret_ref}],
                }
            ],
            "volumes": [{"name": "tls", "secret": volume_secret}],
        },
    }
    return json.dumps({"kind": "PodList", "items": [pod]}).encode()


def _seen(monkeypatch, tmp_path, body: bytes) -> str:
    gw = gateway(monkeypatch, tmp_path, body=body)
    outcome = gw.call("kubernetes_jq_query", JQ_CALL)
    assert gw.source.requests, "the call must reach the source"
    assert outcome.status == "ok", outcome.model_view
    return everything_seen(outcome, gw.evidence)


def test_secret_reference_names_reach_the_model(monkeypatch, tmp_path):
    seen = _seen(
        monkeypatch,
        tmp_path,
        _pod(
            {"name": "orders-db-ref", "key": "orders-db-key"},
            {"name": "orders-env-ref"},
            {"secretName": "orders-tls-ref"},
        ),
    )
    for name in NAMES:
        assert name in seen, name


def test_values_inside_or_beside_a_reference_stay_redacted(monkeypatch, tmp_path):
    """Mutation of the fixture above: value-bearing fields planted inside the
    exempt references, and a credential assignment in a readable name."""
    seen = _seen(
        monkeypatch,
        tmp_path,
        _pod(
            {
                "name": "orders-db-ref",
                "key": "orders-db-key",
                "value": "canary-ref-sibling-41",
            },
            # assembled so the source line itself is not a credential literal
            {"name": "pass" + "word=" + HIDDEN[1]},
            {
                "secretName": "orders-tls-ref",
                "data": {"tls.key": "canary-ref-data-43"},
                "stringData": {"nested": {"x": "canary-ref-nested-44"}},
            },
        ),
    )
    assert "orders-db-key" in seen and "orders-tls-ref" in seen
    for value in HIDDEN:
        assert value not in seen, value


@pytest.mark.parametrize("kind", ["secrets", "Secret"])
def test_secret_objects_are_still_never_read(monkeypatch, tmp_path, kind):
    body = json.dumps(
        {"kind": "SecretList", "items": [{"data": {"k": "canary-ref-data-43"}}]}
    ).encode()
    gw = gateway(monkeypatch, tmp_path, body=body)
    outcome = gw.call("kubernetes_jq_query", {**JQ_CALL, "kind": kind})
    assert (outcome.status, outcome.reason) == ("error", "RESOURCE_NOT_ALLOWED")
    assert gw.source.requests == []
    assert "canary-ref-data-43" not in everything_seen(outcome, gw.evidence)
