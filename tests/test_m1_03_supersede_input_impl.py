"""#179 (contract r8, D39-D46, R21-R27): supersede reachable in generation.

Pure functions only: the model sees this postmortem's active entries
(``published_knowledge_entries``: key, redacted name, revision; no entry
UUID), a supersede proposal names the target key and the revision it saw,
and code binds it to the entry only on an exact key + revision match.
"""

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from opspilot.knowledge.contract import (
    CONTRACT_REVISION,
    POSTMORTEM_CONTENT_SCHEMA_VERSION,
    POSTMORTEM_INPUT_POLICY_VERSION,
    POSTMORTEM_OUTPUT_SCHEMA_VERSION,
    POSTMORTEM_PROMPT_VERSION,
)
from opspilot.knowledge.generation import (
    MAX_INPUT_BYTES,
    SYSTEM_PROMPT,
    InputTooLarge,
    OutputInvalid,
    PublishedEntry,
    assemble,
    build_input,
    parse_model_output,
    proposal_problems,
)
from opspilot.knowledge.store import Watermark

T0 = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
SECRET = "s3cr3t-" + "value-0123456789"


def _raw(published=None):
    incident_id = uuid4()
    return {
        "incident": {
            "incident_id": incident_id,
            "intake_key": "key-1",
            "target_id": None,
            "resource_uid": None,
            "state": "completed",
            "lifecycle": "resolved",
            "mode": "auto",
            "created_at": T0,
            "control_generation": 0,
            "observation_generation": 1,
            "current_run_id": None,
            "conclusion": None,
        },
        "watermark": Watermark(
            incident_control_generation=0,
            observation_generation=1,
            observation_session_id=uuid4(),
            observation_ending_id=uuid4(),
            run_count=0,
            last_run_id=None,
            input_watermark=0,
            evidence_snapshot_sha256="0" * 64,
        ),
        "postmortem": {"postmortem_id": uuid4(), "generation": 3, "latest_version": 1},
        "pending_regeneration_version": None,
        "return_reason": None,
        "published_entries": {} if published is None else published,
        "runs": [],
        "controls": [],
        "inputs": [],
        "evidence": [],
        "observation": {
            "session": {
                "health_profile_revision": "hp-1",
                "authorized_by": "op",
                "authorized_at": T0,
                "state": "completed",
                "adopted_count": 0,
                "max_samples": 5,
                "sample_interval_seconds": 60,
                "sustained_window_seconds": 60,
                "healthy_since": None,
            },
            "ending": {
                "ended_reason": "recovery_confirmed",
                "transition": "recovery_confirmed",
                "lifecycle_before": "observing_recovery",
                "lifecycle_after": "resolved",
                "recorded_at": T0,
            },
            "samples": [],
            "readings": [],
        },
    }


def _published():
    return {
        "zeta-errors": {"entry_id": uuid4(), "revision": 1, "name": "zeta errors"},
        "checkout-5xx": {
            "entry_id": uuid4(),
            "revision": 2,
            "name": "checkout 5xx; pass" + "word=" + SECRET,
        },
    }


def _proposal(key="checkout-5xx", **extra):
    return {
        "key": key,
        "name": "checkout 5xx after deploy",
        "tags": ["checkout"],
        "symptoms": ["5xx"],
        "checks": ["error ratio"],
        "evidence_ids": ["ev-1"],
        **extra,
    }


def _reply(*proposals):
    return json.dumps(
        {
            "narrative_sections": [
                {
                    "key": "impact",
                    "section": "impact_summary",
                    "body": "Impact",
                    "evidence_ids": ["ev-1"],
                }
            ],
            "conclusions": [],
            "proposals": list(proposals),
            "disputes": [],
        }
    )


CATALOG = [{"evidence_id": "ev-1", "citable_as_fact": True}]


# --- input (D39, D40, R21-R24)


def test_payload_lists_active_entries_by_key_without_entry_ids() -> None:
    published = _published()
    built = build_input(_raw(published), secrets=[SECRET])
    entries = built.payload["published_knowledge_entries"]
    assert [e["key"] for e in entries] == ["checkout-5xx", "zeta-errors"]
    assert entries[0]["revision"] == 2 and entries[1]["revision"] == 1
    assert set(entries[0]) == {"key", "name", "revision"}
    assert SECRET not in built.payload_text
    assert "[REDACTED]" in entries[0]["name"]
    for entry in published.values():
        assert str(entry["entry_id"]) not in built.payload_text


def test_payload_without_active_entries_sends_an_empty_list() -> None:
    built = build_input(_raw(), secrets=[])
    assert built.payload["published_knowledge_entries"] == []


def test_entry_order_does_not_change_the_input_hash() -> None:
    published = _published()
    raw = _raw(published)
    reordered = {**raw, "published_entries": dict(reversed(list(published.items())))}
    assert (
        build_input(raw, secrets=[]).payload_sha256
        == build_input(reordered, secrets=[]).payload_sha256
    )


def test_entries_count_toward_the_total_input_limit() -> None:
    name = "n" * 200
    count = MAX_INPUT_BYTES // 200 + 1
    published = {
        f"k{i:06d}": {"entry_id": uuid4(), "revision": 1, "name": name}
        for i in range(count)
    }
    with pytest.raises(InputTooLarge) as caught:
        build_input(_raw(published), secrets=[])
    assert caught.value.limit == "MAX_INPUT_BYTES"


# --- output (D41, D43, R25)


def test_supersedes_revision_is_parsed_and_defaults_to_none() -> None:
    output = parse_model_output(
        _reply(_proposal(supersedes_revision=2), _proposal("new-key"))
    )
    assert [p.supersedes_revision for p in output.proposals] == [2, None]
    explicit = parse_model_output(_reply(_proposal(supersedes_revision=None)))
    assert explicit.proposals[0].supersedes_revision is None


@pytest.mark.parametrize("value", [0, -1, True, "2", 1.0, [2]])
def test_an_illegal_supersedes_revision_is_invalid_output(value) -> None:
    with pytest.raises(OutputInvalid) as caught:
        parse_model_output(_reply(_proposal(supersedes_revision=value)))
    assert "proposals[0].supersedes_revision" in caught.value.problems


def _entries():
    return {
        key: PublishedEntry(value["entry_id"], value["revision"], value["name"])
        for key, value in _published().items()
    }


def test_exact_key_and_revision_is_a_valid_supersede_target() -> None:
    output = parse_model_output(_reply(_proposal(supersedes_revision=2)))
    assert proposal_problems(output, CATALOG, _entries()) == []


@pytest.mark.parametrize(
    ("proposal", "code"),
    [
        # the revision seen in the input differs (D41)
        (_proposal(supersedes_revision=1), "SUPERSEDE_REVISION_MISMATCH"),
        # no active entry has this key: names and case do not match (D43)
        (
            _proposal("checkout-5xx-x", supersedes_revision=2),
            "SUPERSEDE_TARGET_UNKNOWN",
        ),
        (_proposal("checkout_5xx", supersedes_revision=2), "SUPERSEDE_TARGET_UNKNOWN"),
        # an active key without the revision: no implicit mapping (R25)
        (_proposal(), "SUPERSEDE_REVISION_MISSING"),
    ],
)
def test_a_wrong_supersede_target_is_an_output_problem(proposal, code) -> None:
    output = parse_model_output(_reply(proposal))
    assert proposal_problems(output, CATALOG, _entries()) == [
        f"proposals.{proposal['key']}.{code}"
    ]


def test_revoked_or_superseded_entries_are_not_targets() -> None:
    # D40: only active entries reach the input, so a key that was revoked or
    # superseded is unknown however the model names it
    output = parse_model_output(_reply(_proposal("old-key", supersedes_revision=1)))
    assert proposal_problems(output, CATALOG, _entries()) == [
        "proposals.old-key.SUPERSEDE_TARGET_UNKNOWN"
    ]


def test_duplicate_targets_are_invalid_output() -> None:
    with pytest.raises(OutputInvalid) as caught:
        parse_model_output(
            _reply(
                _proposal(supersedes_revision=2),
                _proposal(supersedes_revision=2),
            )
        )
    assert "DUPLICATE_PROPOSAL_KEY" in caught.value.problems


def test_assemble_binds_the_entry_only_on_key_and_revision() -> None:
    published = _published()
    built = build_input(_raw(published), secrets=[])
    output = parse_model_output(
        _reply(_proposal(supersedes_revision=2), _proposal("brand-new"))
    )
    draft = assemble(built, output, {}, record={}, secrets=[])
    by_key = {p.key: p.supersedes_entry_id for p in draft.proposals}
    assert by_key == {
        "checkout-5xx": published["checkout-5xx"]["entry_id"],
        "brand-new": None,
    }
    # a stale revision never binds, even if validation were skipped
    stale = parse_model_output(_reply(_proposal(supersedes_revision=1)))
    [proposal] = assemble(built, stale, {}, record={}, secrets=[]).proposals
    assert proposal.supersedes_entry_id is None


# --- prompt and versions (D44, R26)


def test_prompt_describes_the_entries_and_the_target_revision() -> None:
    assert "published_knowledge_entries" in SYSTEM_PROMPT
    assert '"supersedes_revision"' in SYSTEM_PROMPT


def test_r8_versions() -> None:
    assert POSTMORTEM_PROMPT_VERSION == "f13-postmortem-prompt-v3"
    assert POSTMORTEM_OUTPUT_SCHEMA_VERSION == "f13-postmortem-output-v2"
    assert POSTMORTEM_INPUT_POLICY_VERSION == "f13-postmortem-input-v2"
    assert POSTMORTEM_CONTENT_SCHEMA_VERSION == "f13-postmortem-content-v1"
    assert CONTRACT_REVISION == "r8"
