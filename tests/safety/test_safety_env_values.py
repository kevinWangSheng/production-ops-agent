"""Safety: Pod env values and ConfigMap entries of credential-named
variables never reach the model, whichever Kubernetes tool reads them (#574,
user decisions B1-U1 a, B1-U2 a, B1-D4..D6).

Through the shipped ``otel-demo-ext`` gateway:

* the whole object (``kubernetes_jq_query`` with ``.items[]``): ``env[].value``
  of ``DB_PASSWORD``/``DB_PASS``/``SMTP_PWD``/``MAX_TOKENS`` is replaced, also
  inside the ``last-applied-configuration`` annotation string, while
  ``DB_HOST``/``LOG_LEVEL``/``SESSION_TIMEOUT`` and the ``secretKeyRef``
  reference names stay visible (M56, M57);
* a jq expression that extracts the bare value (``[.items[].data.DB_PASSWORD]``,
  ``.items[].spec.containers[].env[].value``) gets the marker, because the
  objects are redacted before the expression runs (M54);
* a tabular column on the same field (``PW:.data.DB_PASSWORD``) likewise (M55).
"""

from __future__ import annotations

import json

from tests.safety.gateway_support import everything_seen, gateway

#: Synthetic values (low entropy) that must never be visible.
HIDDEN = {
    "DB_PASSWORD": "canary-env-81",
    "DB_PASS": "canary-env-82",
    "SMTP_PWD": "canary-env-83",
    "MAX_TOKENS": "4096",
}
ANNOTATED = "canary-env-84"
#: Values that must stay visible.
SHOWN = {"DB_HOST": "postgres-orders", "LOG_LEVEL": "debug", "SESSION_TIMEOUT": "30"}
CONFIG_HIDDEN = "canary-cm-85"


def _env() -> list[dict[str, object]]:
    env: list[dict[str, object]] = [
        {"name": k, "value": v} for k, v in {**HIDDEN, **SHOWN}.items()
    ]
    env.append(
        {
            "name": "API_TOKEN",
            "valueFrom": {"secretKeyRef": {"name": "orders-ref", "key": "orders-key"}},
        }
    )
    return env


def _pods() -> bytes:
    applied = {
        "spec": {
            "containers": [
                {
                    "name": "orders",
                    "env": [
                        {"name": "DB_PASSWORD", "value": ANNOTATED},
                        {"name": "LOG_LEVEL", "value": "debug"},
                    ],
                }
            ]
        }
    }
    pod = {
        "metadata": {
            "name": "orders-0",
            "namespace": "otel-demo",
            "annotations": {
                "kubectl.kubernetes.io/last-applied-configuration": json.dumps(
                    applied, separators=(",", ":")
                )
            },
        },
        "spec": {"containers": [{"name": "orders", "env": _env()}]},
    }
    return json.dumps({"kind": "PodList", "items": [pod]}).encode()


def _config_maps() -> bytes:
    cm = {
        "metadata": {"name": "orders-config", "namespace": "otel-demo"},
        "data": {"DB_PASSWORD": CONFIG_HIDDEN, "LOG_LEVEL": "debug"},
    }
    return json.dumps({"kind": "ConfigMapList", "items": [cm]}).encode()


def _seen(monkeypatch, tmp_path, body: bytes, tool: str, params: dict) -> str:
    gw = gateway(monkeypatch, tmp_path, body=body)
    outcome = gw.call(tool, params)
    assert gw.source.requests, "the call must reach the source"
    assert outcome.status == "ok", outcome.model_view
    return everything_seen(outcome, gw.evidence)


def test_env_values_are_hidden_by_variable_name(monkeypatch, tmp_path):
    """M56 (structured ``env[].value``), M57 (the annotation's JSON text)."""
    seen = _seen(
        monkeypatch,
        tmp_path,
        _pods(),
        "kubernetes_jq_query",
        {"kind": "pods", "jq_expr": ".items[]"},
    )
    for value in (*HIDDEN.values(), ANNOTATED):
        assert value not in seen, value
    for value in (*SHOWN.values(), "orders-ref", "orders-key"):
        assert value in seen, value


def test_jq_extracting_a_bare_value_gets_the_marker(monkeypatch, tmp_path):
    """M54: the objects are redacted before the model's expression runs."""
    seen = _seen(
        monkeypatch,
        tmp_path,
        _config_maps(),
        "kubernetes_jq_query",
        {"kind": "configmaps", "jq_expr": "[.items[].data.DB_PASSWORD]"},
    )
    assert CONFIG_HIDDEN not in seen
    seen = _seen(
        monkeypatch,
        tmp_path,
        _pods(),
        "kubernetes_jq_query",
        {"kind": "pods", "jq_expr": ".items[].spec.containers[].env[].value"},
    )
    for value in HIDDEN.values():
        assert value not in seen, value
    assert "postgres-orders" in seen  # positive control


def test_a_tabular_column_on_a_credential_field_gets_the_marker(monkeypatch, tmp_path):
    """M55: the objects are redacted before the columns are read."""
    seen = _seen(
        monkeypatch,
        tmp_path,
        _config_maps(),
        "kubernetes_tabular_query",
        {"kind": "configmaps", "columns": "NAME:.metadata.name,PW:.data.DB_PASSWORD"},
    )
    assert CONFIG_HIDDEN not in seen
    assert "orders-config" in seen  # positive control
    seen = _seen(
        monkeypatch,
        tmp_path,
        _pods(),
        "kubernetes_tabular_query",
        {"kind": "pods", "columns": "ENV:.spec.containers[*].env[*].value"},
    )
    for value in HIDDEN.values():
        assert value not in seen, value
    assert "postgres-orders" in seen
