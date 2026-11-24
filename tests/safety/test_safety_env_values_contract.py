"""Safety contract: Pod environment-variable values (#574).

Ruled 2026-11-22 (``docs/tasks/2026-11-22-next-batches-b1-prereview.md``
B1-U1 a, B1-U2 a, B1-D4 to B1-D6):

* In an object whose parent field is ``env`` (``containers[]``,
  ``initContainers[]``, a Deployment template), ``{name, value}`` with a
  credential-like ``name`` has its ``value`` replaced by the marker; the name
  stays visible. On env names only, ``PASS``/``PWD`` also count. Other
  variables (host, level, timeout, port) stay visible; so do
  ``valueFrom.secretKeyRef`` reference names (#775). Env values are strings:
  no count exemption (``MAX_TOKENS: "4096"`` is hidden).
* Text form: canonical JSON ``"name":"X","value":...`` hides the value; this
  also covers JSON written inside a string (``last-applied-configuration``).
* Kubernetes read tools redact the objects *before* the model's jq or column
  expression runs, so a value extracted without its name is still hidden.

Not pinned (undecided): ``value`` before ``name``, deeper-escaped JSON,
ordinary-named secrets.
"""

from __future__ import annotations

import json

import pytest

from opspilot.redaction import REDACTED_CREDENTIAL, redact_credentials, redact_payload
from tests.safety.gateway_support import everything_seen, gateway


def _canary(tag: str, n: int) -> str:
    """A low-entropy synthetic value built at runtime (no scanner-visible literal)."""
    return "-".join(("canary", "env", tag, f"{n:02d}"))


#: Credential-named env variables -> low-entropy synthetic values.
HIDDEN_ENV = {
    "DB_PASSWORD": _canary("pw", 1),
    "API_KEY": _canary("key", 2),
    "AUTH_TOKEN": _canary("tok", 3),
    "DB_PASS": _canary("pass", 4),
    "RABBITMQ_DEFAULT_PASS": _canary("rmq", 5),
    "SMTP_PWD": _canary("smtp", 6),
    "MAX_TOKENS": "4096",
}
#: Ordinary env variables -> values that must stay visible.
VISIBLE_ENV = {
    "DB_HOST": "orders-db-host:5432",
    "LOG_LEVEL": "debug-level-x",
    "SESSION_TIMEOUT": "37",
    "HTTP_PORT": "8089",
}


def _env(names: dict[str, str]) -> list[dict[str, str]]:
    return [{"name": k, "value": v} for k, v in names.items()]


def _pod(name: str = "orders-0") -> dict:
    env = _env(HIDDEN_ENV) + _env(VISIBLE_ENV)
    env.append(
        {
            "name": "REF_PASSWORD",
            "valueFrom": {
                "secretKeyRef": {"name": "orders-db-ref", "key": "orders-db-key"}
            },
        }
    )
    return {
        "metadata": {"name": name, "namespace": "otel-demo"},
        "spec": {
            "initContainers": [
                {"name": "migrate", "env": _env(HIDDEN_ENV) + _env(VISIBLE_ENV)}
            ],
            "containers": [{"name": "orders", "env": env}],
        },
    }


def _deployment() -> dict:
    return {
        "metadata": {"name": "orders"},
        "spec": {"template": {"spec": {"containers": [{"env": _env(HIDDEN_ENV)}]}}},
    }


def _hidden_values(env: dict[str, str] = HIDDEN_ENV) -> list[str]:
    return list(env.values())


def _env_values(text: str) -> set[str]:
    return {e["value"] for e in _walk_env(json.loads(text)) if "value" in e}


def _walk_env(node: object):
    if isinstance(node, dict):
        for key, item in node.items():
            if key == "env" and isinstance(item, list):
                yield from (e for e in item if isinstance(e, dict))
            else:
                yield from _walk_env(item)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_env(item)


# -- structured (redact_payload) -----------------------------------------------


@pytest.mark.parametrize("obj", [_pod(), _deployment()], ids=["pod", "deployment"])
def test_credential_env_values_are_hidden_names_stay(obj):
    out = redact_payload(obj)
    for env in _walk_env(out):
        if env["name"] in HIDDEN_ENV:
            assert env["value"] == REDACTED_CREDENTIAL, env
    text = json.dumps(out)
    for name, value in HIDDEN_ENV.items():
        assert f'"name": "{name}"' in text, name
        if value != "4096":
            assert value not in text, name
    assert "4096" not in _env_values(text)  # B1-D6: no count exemption in env


def test_ordinary_env_values_and_secret_references_stay_visible():
    out = redact_payload(_pod())
    seen = {e["name"]: e.get("value") for e in _walk_env(out)}
    for name, value in VISIBLE_ENV.items():
        assert seen[name] == value, name
    text = json.dumps(out)
    assert "orders-db-ref" in text and "orders-db-key" in text


def test_pass_is_not_a_general_credential_word_outside_env():
    obj = {"calibration": {"golden_pass": "3/3"}, "env": _env({"DB_PASS": "x1y"})}
    out = redact_payload(obj)
    assert out["calibration"] == {"golden_pass": "3/3"}
    assert out["env"] == [{"name": "DB_PASS", "value": REDACTED_CREDENTIAL}]


def test_last_applied_configuration_annotation_hides_env_values():
    applied = json.dumps(_deployment(), sort_keys=True, separators=(",", ":"))
    obj = {
        "metadata": {
            "name": "orders",
            "annotations": {
                "kubectl.kubernetes.io/last-applied-configuration": applied + "\n"
            },
        }
    }
    text = json.dumps(redact_payload(obj))
    for value in _hidden_values():
        if value != "4096":
            assert value not in text, value
    assert "DB_PASSWORD" in text and "RABBITMQ_DEFAULT_PASS" in text


# -- text (redact_credentials) -------------------------------------------------


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ('{"name":"DB_PASSWORD","value":"canary-txt-07"}', "canary-txt-07"),
        ('{"name": "AUTH_TOKEN", "value": "t0k"}', "t0k"),
    ],
)
def test_text_form_hides_credential_env_value(text, secret):
    out = redact_credentials(text)
    assert secret not in out
    assert REDACTED_CREDENTIAL in out
    assert json.loads(out)["name"] in text


@pytest.mark.parametrize(
    "text",
    ['{"name":"DB_HOST","value":"postgres:5432"}', '{"name":"metric","value":"3"}'],
)
def test_text_form_keeps_ordinary_values(text):
    assert redact_credentials(text) == text


# -- Kubernetes read tools: redact before the model's expression runs ----------

CONFIGMAPS = json.dumps(
    {
        "kind": "ConfigMapList",
        "items": [
            {
                "metadata": {"name": "orders-config", "namespace": "otel-demo"},
                "data": {
                    "DB_PASSWORD": "canary-cm-pw-08",
                    "API_KEY": "canary-cm-key-09",
                    "DB_HOST": "orders-db-host:5432",
                },
            }
        ],
    }
).encode()
PODS = json.dumps({"kind": "PodList", "items": [_pod()]}).encode()
CM_HIDDEN = ("canary-cm-pw-08", "canary-cm-key-09")
POD_HIDDEN = tuple(v for v in _hidden_values() if v != "4096")


def _seen(monkeypatch, tmp_path, body: bytes, tool: str, params: dict) -> str:
    gw = gateway(monkeypatch, tmp_path, body=body)
    outcome = gw.call(tool, params)
    assert gw.source.requests, "the call must reach the source"
    assert outcome.status == "ok", outcome.model_view
    return everything_seen(outcome, gw.evidence)


@pytest.mark.parametrize(
    ("body", "tool", "params", "hidden"),
    [
        pytest.param(
            CONFIGMAPS,
            "kubernetes_jq_query",
            {
                "kind": "configmaps",
                "jq_expr": "[.items[].data.DB_PASSWORD, .items[].data.API_KEY, .items[].data.DB_HOST]",
            },
            CM_HIDDEN,
            id="jq-configmap-data",
        ),
        pytest.param(
            PODS,
            "kubernetes_jq_query",
            {"kind": "pods", "jq_expr": "[.items[].spec.containers[].env[].value]"},
            POD_HIDDEN,
            id="jq-pod-env-values",
        ),
        pytest.param(
            PODS,
            "kubernetes_jq_query",
            {
                "kind": "pods",
                "jq_expr": "[.items[].spec.initContainers[].env[] | .value]",
            },
            POD_HIDDEN,
            id="jq-init-env-values",
        ),
        pytest.param(
            PODS,
            "kubernetes_tabular_query",
            {
                "kind": "pods",
                "columns": "NAME:.metadata.name,V:.spec.containers[*].env[*].value",
            },
            POD_HIDDEN,
            id="tabular-env-values",
        ),
        pytest.param(
            PODS,
            "kubernetes_tabular_query",
            {
                "kind": "pods",
                "columns": "NAME:.metadata.name,ENV:.spec.containers[*].env",
            },
            POD_HIDDEN,
            id="tabular-env-json-cell",
        ),
        pytest.param(
            CONFIGMAPS,
            "kubernetes_tabular_query",
            {
                "kind": "configmaps",
                "columns": "P:.data.DB_PASSWORD,K:.data.API_KEY,H:.data.DB_HOST",
            },
            CM_HIDDEN,
            id="tabular-configmap-data",
        ),
    ],
)
def test_model_expressions_cannot_extract_credential_values(
    monkeypatch, tmp_path, body, tool, params, hidden
):
    seen = _seen(monkeypatch, tmp_path, body, tool, params)
    for value in hidden:
        assert value not in seen, value
    assert "orders-db-host:5432" in seen
