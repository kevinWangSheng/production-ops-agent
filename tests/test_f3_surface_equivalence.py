"""Pure checks for the F3 v11 surface-equivalence script (no LangSmith)."""

import json
from hashlib import sha256

import pytest

from opspilot import redaction
from scripts.f3_surface_equivalence import (
    check,
    escaped_matches,
    extract_texts,
    rules_of,
    tool_messages,
)

S, U = "\\/", "\\u002f"
VIEW = json.dumps({"status": "ok", "content": [{"metric": {"job": "checkout"}}]})
NOW = rules_of(redaction)


def _identity(text: str) -> tuple[str, bytes]:
    """A rule set that rewrites nothing (stands in for v10 on new forms)."""
    return text, text.encode("utf-8")


def _call(*tools: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": "s"}] + [
        {"role": "tool", "content": t} for t in tools
    ]


def test_tool_messages_are_the_union_of_a_growing_transcript():
    calls = [_call(), _call("a"), _call("a", "b"), _call("a", "b", "a")]
    assert tool_messages(calls) == ["a", "b", "a"]


def test_a_transcript_that_does_not_grow_by_prefix_is_refused():
    with pytest.raises(ValueError):
        tool_messages([_call("a", "b"), _call("b")])


def test_clean_views_are_counted_without_contents():
    cut = "x" * 10 + "...[truncated]"
    result = check([VIEW, cut], now=NOW, before=NOW)
    assert result == {
        "tool_messages": 2,
        "tool_message_bytes": len(VIEW) + len(cut),
        "tool_messages_sha256": sha256(f"{VIEW}\n{cut}".encode()).hexdigest(),
        "changed_by_current_redaction": 0,
        "escaped_authority_matches": 0,
        "idempotence_changes_pre_existing": 0,
        "trace_truncated_messages": 1,
    }
    assert "checkout" not in json.dumps(result)


@pytest.mark.parametrize(
    "text",
    [
        # escaped form in the message text itself
        f"dial https:{S}{S}u:pw@db failed",
        # JSON nested in a view's string value: visible only after decoding
        json.dumps({"log": f'{{"dsn":"postgres:{S}{S}u:pw@db"}}'}),
        json.dumps({"label": f"https:{U}{U}u:pw@prom"}),
    ],
)
def test_an_escaped_userinfo_counts_as_a_v10_to_current_change(text):
    assert escaped_matches(text) >= 1
    result = check([text], now=NOW, before=_identity)
    assert result["escaped_authority_matches"] >= 1
    assert result["changed_by_current_redaction"] == 1


def test_a_rewrite_both_rule_sets_make_is_pre_existing_not_a_change():
    # the gateway refusal shape: a *_tokens key re-redacts under either rules
    view = json.dumps({"max_view_tokens": 25000, "view_tokens": 169932})
    result = check([view], now=NOW, before=NOW)
    assert result["idempotence_changes_pre_existing"] == 1
    assert result["changed_by_current_redaction"] == 0


def test_extract_texts_cover_evidence_views_and_step_results():
    extract = {
        "raw": {
            "evidence": [{"view": {"a": 1}}],
            "steps": [{"tool_results": [{"r": 2}, {"r": 3}]}, {"tool_results": None}],
        }
    }
    assert extract_texts(extract) == ['{"a": 1}', '{"r": 2}', '{"r": 3}']
