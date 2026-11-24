"""Safety: the shared credential-name rule redacts credentials, not counts or
ordinary session settings (#366; rulings B1-U3 option a and B1-D8 in
docs/tasks/2026-11-22-next-batches-b1-prereview.md).

* A name ending in ``tokens`` whose value is a number is a count and stays
  visible; with a non-numeric value it is still a credential.
* ``session`` names a credential only as the name's last word or when
  followed by ``id``/``key``/``cookie``; ``sessionAffinity`` and
  ``SESSION_TIMEOUT`` are settings. ``sessionid`` (one word) is redacted.
* ``auth``, ``token``, ``password`` stay credential names.
* ``imagePullSecrets[].name`` is a Secret reference name, visible like the
  #775 references; every other field in such an entry is still replaced.

The same name definition backs tool-parameter refusal and alert labels, so
the session cases are pinned there too. Values are synthetic, low entropy.
"""

from __future__ import annotations

import json

import pytest

from opspilot.alertmanager import alert_context
from opspilot.redaction import REDACTED_CREDENTIAL, redact_credentials, redact_payload
from opspilot.tools.registry import ParameterSpec, ToolContractError
from tests.m1_tool_support import registration

#: Synthetic credential value, built at runtime (no scanner-visible literal).
FAKE = "-".join(("fake", "cred", "val"))

# -- prose forms: ``k: v``, ``k=v``, JSON ``"k": v`` -------------------------

VISIBLE_TEXT = [
    "prompt_tokens: 12",
    "total_tokens=5",
    '"max_tokens": 4000',
    "max_view_tokens=25000",
    '{"prompt_tokens":12,"view_tokens":31000}',
    "sessionAffinity: None",
    "SESSION_TIMEOUT=30",
    "session_timeout_ms=30000",
    '"session_timeout_ms": 30000',
    "ssoSessionIdleTimeout: 1800",
    "ssoSessionMaxLifespan=36000",
]


@pytest.mark.parametrize("text", VISIBLE_TEXT)
def test_counts_and_session_settings_stay_visible(text):
    assert redact_credentials(text) == text


REDACTED_TEXT = [
    # ``*tokens`` with a non-numeric value is still a credential.
    "access_tokens: {v}",
    "access_tokens={v}",
    '"access_tokens": "{v}"',
    # ``session`` as the last word, or followed by id/key/cookie.
    "session={v}",
    "session: {v}",
    '"session": "{v}"',
    "session_id={v}",
    '"session_id": "{v}"',
    "SESSION_ID={v}",
    "session_key={v}",
    "sessionKey: {v}",
    "session_cookie={v}",
    "user_session={v}",
    "sessionid={v}",
    '"sessionid": "{v}"',
    "SESSIONID={v}",
    # Unchanged credential names.
    "auth: {v}",
    "token={v}",
    '"token": "{v}"',
    "password={v}",
    '"password": "{v}"',
]


@pytest.mark.parametrize("template", REDACTED_TEXT)
def test_credential_names_stay_redacted(template):
    redacted = redact_credentials(template.format(v=FAKE))
    assert FAKE not in redacted
    assert REDACTED_CREDENTIAL in redacted
    assert redact_credentials(redacted) == redacted


def test_auth_ok_stays_redacted():
    # Listed in #366 but kept redacted by B1-U3 a.
    redacted = redact_credentials("auth: ok")
    assert redacted == f"auth: {REDACTED_CREDENTIAL}"


# -- structured payloads -----------------------------------------------------


def test_payload_counts_and_session_settings_visible_credentials_redacted():
    payload = {
        "usage": {"prompt_tokens": 12, "total_tokens": 5, "max_tokens": 4000},
        "limits": {"max_view_tokens": 25000, "view_tokens": 31000},
        "spec": {"sessionAffinity": "None", "type": "ClusterIP"},
        "consumer": {"session_timeout_ms": 30000, "SESSION_TIMEOUT": 30},
        "realm": {"ssoSessionIdleTimeout": 1800},
        "creds": {
            "access_tokens": FAKE,
            "session": FAKE,
            "session_id": FAKE,
            "session_key": FAKE,
            "sessionid": FAKE,
            "auth": FAKE,
            "token": FAKE,
            "password": FAKE,
        },
    }
    out = redact_payload(payload)
    for section in ("usage", "limits", "spec", "consumer", "realm"):
        assert out[section] == payload[section], section
    assert out["creds"] == {key: REDACTED_CREDENTIAL for key in payload["creds"]}
    assert FAKE not in json.dumps(out)


# -- B1-D8: imagePullSecrets[].name ------------------------------------------


def _pod() -> dict:
    return {
        "kind": "Pod",
        "metadata": {
            "name": "orders-0",
            "annotations": {"token": FAKE},
        },
        "spec": {
            "imagePullSecrets": [
                {"name": "regcred"},
                {"name": "mirror-pull", "note": FAKE},
            ],
            "containers": [
                {
                    "name": "orders",
                    "image": "ghcr.io/example/orders:1.2.3",
                    "args": [f"--password={FAKE}"],
                    "env": [
                        {
                            "name": "DB_PASS",
                            "valueFrom": {
                                "secretKeyRef": {"name": "orders-db", "key": "pw"}
                            },
                        }
                    ],
                }
            ],
            "sessionAffinity": "None",
        },
    }


def test_image_pull_secret_names_visible_other_fields_redacted():
    out = redact_payload(_pod())
    pulls = out["spec"]["imagePullSecrets"]
    assert isinstance(pulls, list), pulls
    assert [entry["name"] for entry in pulls] == ["regcred", "mirror-pull"]
    # Only ``name`` is readable inside an entry; any sibling is replaced.
    assert pulls[1]["note"] == REDACTED_CREDENTIAL
    # Credential-valued fields elsewhere stay redacted.
    assert out["metadata"]["annotations"]["token"] == REDACTED_CREDENTIAL
    # Including the ``--password=`` container arg (#802).
    assert FAKE not in json.dumps(out)
    # Existing #775 reference names and the session setting stay visible.
    ref = out["spec"]["containers"][0]["env"][0]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": "orders-db", "key": "pw"}
    assert out["spec"]["sessionAffinity"] == "None"


def test_command_line_flag_credential_is_redacted():
    # #802; the full flag matrix is in test_safety_flag_credentials.py.
    out = redact_payload(_pod())
    args = out["spec"]["containers"][0]["args"]
    assert FAKE not in json.dumps(args)
    assert args[0].startswith("--password")


# -- same definition: tool-parameter refusal and alert labels ----------------


@pytest.mark.parametrize("name", ["session_timeout_seconds", "sessionAffinity"])
def test_session_setting_parameter_names_are_accepted(name):
    params = {
        "expr": ParameterSpec("string", required=True, description="PromQL."),
        name: ParameterSpec("string", description="A session setting."),
    }
    registration(parameters=params)


@pytest.mark.parametrize("name", ["session", "session_id", "sessionid", "auth"])
def test_session_credential_parameter_names_are_refused(name):
    params = {
        "expr": ParameterSpec("string", required=True, description="PromQL."),
        name: ParameterSpec("string", description="Something."),
    }
    with pytest.raises(ToolContractError, match="RESERVED_PARAMETER"):
        registration(parameters=params)


def test_alert_labels_follow_the_same_session_rule():
    labels = {
        "alertname": "HighLatency",
        "sessionAffinity": "None",
        "session_timeout_ms": "30000",
        "session": FAKE,
        "session_id": FAKE,
        "sessionid": FAKE,
    }
    fact = alert_context(labels, {})
    seen = fact["labels"]
    assert seen["sessionAffinity"] == "None"
    assert seen["session_timeout_ms"] == "30000"
    for key in ("session", "session_id", "sessionid"):
        assert seen[key] == REDACTED_CREDENTIAL, key
    assert FAKE not in json.dumps(fact)
