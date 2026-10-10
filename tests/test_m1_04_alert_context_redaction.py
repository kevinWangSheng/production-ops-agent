"""Credentials nested in JSON or spanning lines never reach the model
(M1-04 step 4 independent review P1-1).

Two shapes leaked through ``redact_credentials`` into ``alert_context`` and
the model message: a credential name whose value is a JSON object, array or
bare scalar (``{"PASSWORD":{"value":"..."}}``), and a quoted credential value
whose closing quote is on a later line. Both now fail closed. All values are
synthetic.
"""

from __future__ import annotations

from opspilot.alertmanager import (
    _redacted,
    alert_context,
    alert_context_of_record,
    parse_alert,
    record_of,
)
from opspilot.tools.registry import REDACTED_CREDENTIAL, canonical, redact_credentials

R = REDACTED_CREDENTIAL
NESTED = '{"PASSWORD":{"value":"NESTED-FAKE-SECRET"}}'
MULTILINE = 'password="first-FAKE\nSECOND-FAKE-SECRET"'
SECRETS = ("NESTED-FAKE-SECRET", "first-FAKE", "SECOND-FAKE-SECRET", "4711")


def _assert_clean(text):
    for secret in SECRETS:
        assert secret not in text, (secret, text)


def test_a_credential_name_with_a_container_value_redacts_the_whole_value():
    cases = {
        NESTED: f'{{"PASSWORD":"{R}"}}',
        '{"api_key": ["a-FAKE", "b-FAKE"], "user": "ops"}': (
            f'{{"api_key": "{R}", "user": "ops"}}'
        ),
        '{"token": {"v": "x}y-FAKE", "w": [1, {"z": 2}]}, "n": 1}': (
            f'{{"token": "{R}", "n": 1}}'
        ),
        # Unbalanced: nothing after the opening brace is shown.
        'note {"secret": {"a": "open-FAKE': f'note {{"secret": "{R}"',
    }
    for text, expected in cases.items():
        assert redact_credentials(text) == expected, text
        assert redact_credentials(expected) == expected  # idempotent


def test_a_quoted_credential_name_with_a_bare_scalar_is_redacted():
    cases = {
        '{"password": 4711, "user": "ops"}': f'{{"password": {R}, "user": "ops"}}',
        "{'token': true}": f"{{'token': {R}}}",
    }
    for text, expected in cases.items():
        assert redact_credentials(text) == expected, text
        assert redact_credentials(expected) == expected


def test_a_container_under_an_ordinary_name_is_still_examined():
    text = '{"config": {"api_key": "inner-FAKE", "region": "eu"}}'
    assert redact_credentials(text) == (
        f'{{"config": {{"api_key": "{R}", "region": "eu"}}}}'
    )


def test_a_quoted_credential_that_does_not_close_on_its_line_fails_closed():
    cases = {
        MULTILINE: f'password="{R}',
        'password: "\nSECOND-FAKE-SECRET"': f'password: "{R}',
        "token='abc-FAKE\nnext line": f"token='{R}",
    }
    for text, expected in cases.items():
        assert redact_credentials(text) == expected, text
        assert redact_credentials(expected) == expected


def test_both_repros_are_clean_in_the_alert_context():
    for value in (NESTED, MULTILINE):
        fact = alert_context({"alertname": "A"}, {"description": value})
        _assert_clean(canonical(fact))


def test_json_values_get_the_recursive_credential_key_rule():
    # A key spelled with a JSON escape is invisible to the text rules; the
    # parsed object is redacted by key like the audit record.
    escaped = '{"PASS\\u0057ORD": "escaped-FAKE", "region": "eu"}'
    fact = alert_context({}, {"description": escaped})
    value = fact["annotations"]["description"]
    assert "escaped-FAKE" not in value
    # The shared text rule decodes the escaped name itself (recheck P1-1),
    # so the text keeps its form; the parsed check finds nothing left.
    assert value == '{"PASS\\u0057ORD": "' + R + '", "region": "eu"}'
    long_name = "x" * 130 + "\\u0070assword"
    long_escaped = '{"' + long_name + '": "escaped-FAKE"}'
    # Any length: the shared text rule redacts the value and keeps the key.
    expected = '{"' + long_name + '": "' + R + '"}'
    assert alert_context({}, {"d": long_escaped})["annotations"]["d"] == expected
    cut = alert_context({}, {"d": long_escaped[:-1]})["annotations"]["d"]
    assert cut == expected[:-1]
    # Ordinary backslash text that is not JSON is kept as sent.
    for text in ('{"re\\u0067ion": "eu", "p": "C:\\\\tmp"', "[a\\.b] {x"):
        assert alert_context({}, {"d": text})["annotations"]["d"] == text
    # A JSON value with nothing to redact keeps its text as sent.
    plain = '{"region": "eu", "zone": ["a", "b"]}'
    assert alert_context({}, {"d": plain})["annotations"]["d"] == plain


def test_the_audit_record_redacts_a_credential_keys_container():
    assert _redacted({"password": {"value": "x-FAKE"}, "n": {"token": 4711}}) == {
        "password": R,
        "n": {"token": R},
    }


def test_the_fallback_rebuild_matches_intake_for_the_repros():
    for value in (NESTED, MULTILINE, '{"PASS\\u0057ORD": "escaped-FAKE"}'):
        alert = parse_alert(
            {
                "status": "firing",
                "labels": {"alertname": "A"},
                "annotations": {"description": value},
                "startsAt": "2026-10-10T11:41:54Z",
                "fingerprint": "f1",
            }
        )
        record = record_of(alert)
        _assert_clean(record.alert_json)
        assert alert_context_of_record(record.alert_json) == alert_context(
            alert.labels, alert.annotations
        )


def test_container_and_multiline_scans_stay_linear_on_large_input():
    import time

    for chunk in ('{"token":{', '"a":{', '"password":"x\n', '{"k":[', '"token": 1,'):
        text = chunk * (512 * 1024 // len(chunk))
        started = time.monotonic()
        redact_credentials(text)
        assert time.monotonic() - started < 5, chunk


# -- recheck P1-1: escaped key names in malformed or too-deep JSON -------------

# Built at runtime so the secret scanner does not read literals. Synthetic.
_AUDIT_FAKE = "-".join(("AUDIT", "FAKE"))
_DEEP_FAKE = "-".join(("DEEP", "FAKE"))
_ESCAPED = "PASS" + "\\u0057" + "ORD"  # JSON text for the name PASSWORD
_ESCAPED_REPROS = (
    # (a) malformed / truncated JSON, escaped, plain and lowercase names
    "{" + f'"{_ESCAPED}":"{_AUDIT_FAKE}",',
    "{" + f'"PASSWORD":"{_AUDIT_FAKE}",',
    "{" + f'"pass\\u0077ord":"{_AUDIT_FAKE}",',
    "{" + f'"password":"{_AUDIT_FAKE}",',
    # (b) escaped name next to JSON nested too deep to parse
    "{" + f'"{_ESCAPED}":"{_DEEP_FAKE}","x":' + "{" * 1100 + "0" + "}" * 1100 + "}",
    "{" + f'"pass\\u0077ord":"{_DEEP_FAKE}","x":' + "{" * 1100 + "0" + "}" * 1100,
    # other escape forms and quote styles
    "{" + f'"P\\u0041SS\\/WORD": "{_AUDIT_FAKE}"' + "}",
    f"{{'{_ESCAPED}': '{_AUDIT_FAKE}'}}",
    "{" + f'"{_ESCAPED}": {{"v": "{_AUDIT_FAKE}"}}' + "}",
    "{" + f'"{_ESCAPED}": 4711{_AUDIT_FAKE}' + "}",
    "{" + f'"{_ESCAPED}":"{_AUDIT_FAKE}\nmore"',
)


def _clean(text):
    for secret in (_AUDIT_FAKE, _DEEP_FAKE):
        assert secret not in text, text


def test_escaped_credential_names_are_redacted_by_the_shared_rule():
    for text in _ESCAPED_REPROS:
        once = redact_credentials(text)
        _clean(once)
        assert redact_credentials(once) == once  # idempotent
    assert redact_credentials("{" + f'"{_ESCAPED}":"{_AUDIT_FAKE}"' + "}") == (
        "{" + f'"{_ESCAPED}":"{R}"' + "}"
    )


def test_escaped_ordinary_names_are_left_alone():
    text = '{"re\\u0067ion": "eu", "note": "a \\"quoted\\" word"}'
    assert redact_credentials(text) == text


def test_escaped_repros_are_clean_in_the_alert_context_and_audit_record():
    for text in _ESCAPED_REPROS:
        _clean(canonical(alert_context({"alertname": "A"}, {"description": text})))
        alert = parse_alert(
            {
                "status": "firing",
                "labels": {"alertname": "A"},
                "annotations": {"description": text},
                "startsAt": "2026-10-10T11:41:54Z",
                "fingerprint": "f1",
            }
        )
        record = record_of(alert)
        _clean(record.alert_json)
        if not record.truncated:
            assert alert_context_of_record(record.alert_json) == alert_context(
                alert.labels, alert.annotations
            )


def test_escaped_repros_never_reach_the_model_through_the_question():
    from dataclasses import replace

    from opspilot.investigation.context import initial_messages, project_input_content
    from tests.m1_investigation_support import assemble

    _, request, _, _, _, _ = assemble(replies=[])
    for text in _ESCAPED_REPROS:
        messages, _ = initial_messages(
            replace(request.as_input(), question=f"config was {text}"),
            evidence_context=None,
        )
        _clean(messages[1]["content"])
        _clean(str(project_input_content({"text": text})))


def test_escaped_name_scans_stay_linear_on_large_input():
    import time

    for chunk in ('"\\u0041":', '"a\\', '"PASS\\u0057ORD":{', "'\\x'"):
        text = chunk * (512 * 1024 // len(chunk))
        started = time.monotonic()
        redact_credentials(text)
        assert time.monotonic() - started < 5, chunk


# -- recheck 2: escaped credential keys of any length ------------------------

_LONGKEY_FAKE = "-".join(("LONGKEY", "FAKE"))


def _long_repros():
    for padding in ("x" * 130, "x" * 10_000, "\\u0078" * 10_000):
        for name in (padding + "pass\\u0077ord", padding + "\\u0070assword"):
            yield "prefix {" + f'"{name}":"{_LONGKEY_FAKE}"' + "}"
            yield f'prefix "{name}":"{_LONGKEY_FAKE}"'
            yield f'prefix "{name}": {{"v": "{_LONGKEY_FAKE}"}}'
            yield f'prefix "{name}": 4711{_LONGKEY_FAKE}'


def test_long_escaped_credential_keys_are_redacted_everywhere():
    from dataclasses import replace

    from opspilot.investigation.context import initial_messages, project_input_content
    from tests.m1_investigation_support import assemble

    _, request, _, _, _, _ = assemble(replies=[])
    for text in _long_repros():
        once = redact_credentials(text)
        assert _LONGKEY_FAKE not in once, text[:40]
        assert redact_credentials(once) == once
        assert _LONGKEY_FAKE not in canonical(alert_context({}, {"d": text}))
        alert = parse_alert(
            {
                "status": "firing",
                "labels": {"alertname": "A"},
                "annotations": {"d": text},
                "startsAt": "2026-10-10T11:41:54Z",
                "fingerprint": "f1",
            }
        )
        assert _LONGKEY_FAKE not in record_of(alert).alert_json
        messages, _ = initial_messages(
            replace(request.as_input(), question=text), evidence_context=None
        )
        assert _LONGKEY_FAKE not in messages[1]["content"]
        assert _LONGKEY_FAKE not in str(project_input_content({"text": text}))


def test_a_key_opened_by_the_previous_candidate_quote_is_still_checked():
    # The quote before ``:PASS...`` is both followed by a separator and the
    # opening quote of the escaped key: it must not hide the key.
    text = f'x ":PASS\\u0057ORD": "{_LONGKEY_FAKE}"'
    assert _LONGKEY_FAKE not in redact_credentials(text)


def test_escaped_key_search_stays_linear_on_large_input():
    import time

    for chunk in ('"a\\', '"\\u0041', '"a\\":', "'\\x':", '"\\":"'):
        text = chunk * (512 * 1024 // len(chunk))
        started = time.monotonic()
        redact_credentials(text)
        assert time.monotonic() - started < 5, chunk
