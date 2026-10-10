"""``redact_credentials`` covers quoted credential values (PR #189 bot P1).

JSON ``"name":"v"``, YAML ``name: "v"`` and ``name='v'`` reached the model
unchanged because the unquoted value pattern stops at a quote. The same
authentication-name rules decide what is a credential; ordinary quoted text
is left alone. All values are synthetic.
"""

from __future__ import annotations

import time
from dataclasses import replace

from opspilot.investigation.context import initial_messages, project_input_content
from opspilot.tools.registry import REDACTED_CREDENTIAL, redact_credentials
from tests.m1_investigation_support import assemble

R = REDACTED_CREDENTIAL


def test_quoted_credential_values_are_redacted_and_the_quotes_kept():
    cases = {
        '{"api_key":"AKIAFAKEFAKE1234"}': f'{{"api_key":"{R}"}}',
        '{"password": "hunter2fake", "user": "ops"}': (
            f'{{"password": "{R}", "user": "ops"}}'
        ),
        'password: "hunter2fake"': f'password: "{R}"',
        "token='abc123fake'": f"token='{R}'",
        "'client_secret': 'two words fake'": f"'client_secret': '{R}'",
        '{"Authorization": "Bearer abc.def.ghi12345"}': f'{{"Authorization": "{R}"}}',
        'X-Api-Key = "k-fake-1" then retry': f'X-Api-Key = "{R}" then retry',
        'password: "esc\\"aped-fake" tail': f'password: "{R}" tail',
        "note: \"use password='p1fake' now\"": f"note: \"use password='{R}' now\"",
    }
    for text, expected in cases.items():
        assert redact_credentials(text) == expected, text
        assert redact_credentials(expected) == expected  # idempotent


def test_ordinary_quoted_text_is_unchanged():
    for text in (
        '{"label_key": "env", "author_filter": "alice", "group_by_key": "pod"}',
        "msg: \"hello world\" and title: 'Token rotation plan'",
        'he said: "it\'s fine"',
        'password: ""',
        'error: "connection refused" at 10:30',
    ):
        assert redact_credentials(text) == text, text


def test_unquoted_forms_keep_their_existing_output():
    cases = {
        "api_key=AKIAFAKE": f"api_key={R}",
        "Authorization: Bearer abc.def.ghi": f"Authorization: {R}",
        "https://user:pw@host/p?token=t1&q=2": f"https://{R}@host/p?token={R}&q=2",
        "the author_filter=alice option": "the author_filter=alice option",
    }
    for text, expected in cases.items():
        assert redact_credentials(text) == expected, text


def test_unclosed_quotes_stay_linear_on_large_input():
    for chunk in ('a:"', "k='", 'password: "', 'password:"x '):
        text = chunk * (512 * 1024 // len(chunk))
        started = time.monotonic()
        redact_credentials(text)
        assert time.monotonic() - started < 5, chunk


def test_a_quoted_credential_in_the_question_or_a_note_never_reaches_the_model():
    _, request, _, _, _, _ = assemble(replies=[])
    question = 'checkout fails; config was {"api_key":"AKIAFAKEFAKE1234"}'
    messages, _ = initial_messages(
        replace(request.as_input(), question=question), evidence_context=None
    )
    assert "AKIAFAKEFAKE1234" not in messages[1]["content"]
    assert messages[1]["content"] == f'checkout fails; config was {{"api_key":"{R}"}}'
    projected = project_input_content({"text": "retry with password: 'hunter2fake'"})
    assert projected == {"text": f"retry with password: '{R}'"}
