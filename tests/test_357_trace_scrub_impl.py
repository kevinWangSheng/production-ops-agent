"""#357: the trace export fallback runs the shared ``redact_credentials``.

Seam: a lab tracer with the SDK's in-memory exporter; text enters through a
model call's prompt and reply and is read back from the exported span
attributes. Secrets are synthetic markers; no test opens a socket.
"""

import json

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from opspilot import redaction, tracing
from opspilot.investigation.client import DeepSeekClient
from tests.test_tracing import _Opener, lab_env, model_call

FAKE_DEEPSEEK_KEY = "fake-deepseek-key-not-real-1111"
BS = chr(92)  # written via chr() so no tool decodes the JSON escapes below
SLASH_ESCAPED = BS + "/"
SLASH_UNICODE = BS + "u002f"


@pytest.fixture(autouse=True)
def _reset_tracer():
    yield
    tracing.reset()


def _reply(content):
    return json.dumps(
        {
            "id": "resp-357",
            "model": "deepseek-flash",
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    ).encode()


def _export(text):
    """Send ``text`` as both a user prompt and the model's reply; return the
    exported string attribute values."""
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(lab_env(), exporter=exporter)
    call = model_call(
        messages=(
            {"role": "system", "content": "You are OpsPilot."},
            {"role": "user", "content": text},
        )
    )
    with tracer.run(incident_id="i", run_id="r", versions={}):
        DeepSeekClient(FAKE_DEEPSEEK_KEY, opener=_Opener(_reply(text))).complete(call)
    tracer.shutdown()
    values = []
    for span in exporter.get_finished_spans():
        for value in dict(span.attributes).values():
            if isinstance(value, str):
                values.append(value)
            elif isinstance(value, (tuple, list)):
                values.extend(item for item in value if isinstance(item, str))
    return values


def _model_texts(values):
    """The exported attributes that carry the test text."""
    return [value for value in values if "checkout" in value]


@pytest.mark.parametrize(
    "secret_text, secret",
    [
        ("dsn https://ops:syn-literal-357a@db.lab/x", "syn-literal-357a"),
        (
            "dsn https:" + SLASH_ESCAPED * 2 + "ops:syn-escaped-357b@db.lab",
            "syn-escaped-357b",
        ),
        (
            "dsn https:" + SLASH_UNICODE * 2 + "ops:syn-unicode-357c@db.lab",
            "syn-unicode-357c",
        ),
        ("note access_token=syn-assign-357d next", "syn-assign-357d"),
        ('payload {"api_key": "syn-json-357e"}', "syn-json-357e"),
        ("header Bearer syn-bearer-token-357f", "syn-bearer-token-357f"),
    ],
)
def test_shared_policy_forms_never_reach_exported_spans(secret_text, secret):
    values = _export(f"checkout errors; {secret_text}")
    assert _model_texts(values), "the text was not exported at all"
    for value in values:
        assert secret not in value, value


def test_userinfo_redaction_keeps_the_host_visible():
    values = _export("checkout dsn https://ops:syn-literal-357g@db.lab/x")
    texts = _model_texts(values)
    assert texts
    for value in texts:
        assert "syn-literal-357g" not in value
        assert "@db.lab/x" in value


def test_a_form_added_to_the_shared_policy_is_covered_without_tracing_changes(
    monkeypatch,
):
    novel = "zq357-novel-shape-secret"
    shared = redaction.redact_credentials

    def extended(text):
        return shared(text).replace(novel, redaction.REDACTED_CREDENTIAL)

    monkeypatch.setattr(redaction, "redact_credentials", extended)
    values = _export(f"checkout errors carry {novel} in the body")
    texts = _model_texts(values)
    assert texts
    for value in values:
        assert novel not in value, value
    assert any(redaction.REDACTED_CREDENTIAL in value for value in texts)


def test_text_the_shared_policy_does_not_match_is_exported_byte_for_byte():
    text = (
        "checkout p95 rose to 1.8s after deploy 2026-10-26T08:00Z; "
        'see https://grafana.lab/d/abc?var=svc&from=now-1h {"rows": [1, 2]} '
        "path " + SLASH_ESCAPED + "api" + SLASH_ESCAPED + "v1 no userinfo here"
    )
    assert redaction.redact_credentials(text) == text  # premise of the test
    values = _export(text)
    assert text in values  # prompt content attribute, unchanged
    assert values.count(text) >= 2  # and the completion content attribute


def test_existing_key_shape_and_env_value_cleaning_is_kept(monkeypatch):
    env_secret = "syn-env-secret-value-357h"
    monkeypatch.setenv("SYN357_API_KEY", env_secret)
    sk_key = "sk-" + "a1b2c3d4" * 3
    ls_key = "lsv2_" + "pt357synthetic" * 2
    values = _export(f"checkout leaked {env_secret} {sk_key} {ls_key} plainly")
    for value in values:
        for secret in (env_secret, sk_key, ls_key):
            assert secret not in value, value
