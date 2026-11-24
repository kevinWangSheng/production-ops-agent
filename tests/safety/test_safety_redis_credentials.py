"""Safety: Redis/Valkey password directives are credentials (#818).

``redis-server --requirepass <v>`` / ``--masterauth <v>`` on a command line
and ``requirepass <v>`` / ``masterauth <v>`` lines in a redis.conf (a
ConfigMap) hide the value before storage and model input; ``primaryauth`` is
Valkey's spelling of ``masterauth``. The directive word stays readable and
ordinary config lines stay visible. Values are synthetic, low entropy.
"""

from __future__ import annotations

import json

import pytest

from opspilot.redaction import REDACTED_CREDENTIAL, redact_credentials, redact_payload
from tests.safety.gateway_support import everything_seen, gateway

FAKE = "-".join(("canary", "redis", "81"))
FAKE2 = "-".join(("canary", "redis", "82"))

HIDDEN_TEXT = [
    # flag forms (the names join the shared credential-name rule)
    "redis-server --requirepass {v} --port 6379",
    "redis-server /etc/redis.conf --masterauth {v}",
    "redis-server --requirepass={v}",
    "valkey-server --primaryauth {v}",
    # config-line forms (redis.conf, CONFIG SET text)
    "port 6379\nrequirepass {v}\nappendonly yes",
    "masterauth {v}",
    "  requirepass\t{v}",
    'requirepass "{v} {v2}"',
    "primaryauth {v}",
    "CONFIG SET requirepass {v}",
    "REQUIREPASS {v}",
    # redis.conf as JSON text (a ConfigMap read as text)
    '{{"redis.conf":"port 6379\\nrequirepass {v}\\n"}}',
    # assignment forms
    "requirepass={v}",
    '{{"masterauth": "{v}"}}',
]


@pytest.mark.parametrize("template", HIDDEN_TEXT)
def test_redis_password_directives_are_hidden_in_text(template):
    text = template.format(v=FAKE, v2=FAKE2)
    redacted = redact_credentials(text)
    assert FAKE not in redacted and FAKE2 not in redacted, redacted
    assert REDACTED_CREDENTIAL in redacted
    assert redact_credentials(redacted) == redacted


VISIBLE_TEXT = [
    "port 6379\nappendonly yes\nmaxmemory 256mb",
    "redis-server --port 6379 --appendonly yes",
    "masteruser replica",
]


@pytest.mark.parametrize("text", VISIBLE_TEXT)
def test_ordinary_redis_config_stays_visible(text):
    assert redact_credentials(text) == text


def test_redis_password_flags_never_reach_the_model(monkeypatch, tmp_path):
    pod = {
        "metadata": {"name": "cache-0", "namespace": "otel-demo"},
        "spec": {
            "containers": [
                {
                    "name": "redis",
                    "command": [
                        "redis-server",
                        "--requirepass",
                        FAKE,
                        "--port",
                        "6379",
                    ],
                    "args": [f"--masterauth {FAKE2} --appendonly yes"],
                }
            ]
        },
    }
    body = json.dumps({"kind": "PodList", "items": [pod]}).encode()
    gw = gateway(monkeypatch, tmp_path, body=body)
    outcome = gw.call("kubernetes_jq_query", {"kind": "pods", "jq_expr": ".items[]"})
    assert gw.source.requests, "the call must reach the source"
    assert outcome.status == "ok", outcome.model_view
    seen = everything_seen(outcome, gw.evidence)
    assert FAKE not in seen and FAKE2 not in seen
    for visible in ("--requirepass", "--masterauth", "6379", "--appendonly yes"):
        assert visible in seen, visible


def test_redis_conf_configmap_lines_are_hidden():
    configmap = {
        "kind": "ConfigMap",
        "data": {"redis.conf": f"port 6379\nrequirepass {FAKE}\nmasterauth {FAKE2}\n"},
    }
    out = redact_payload(configmap)
    conf = out["data"]["redis.conf"]
    assert FAKE not in conf and FAKE2 not in conf
    assert conf.startswith("port 6379\nrequirepass ")
