"""F13 #179 PostgreSQL contract tests (r8, D39-D44/R21-R27)."""

import json
import os
from copy import deepcopy
from uuid import uuid4

import pytest

from opspilot.knowledge.generation import build_input
from opspilot.knowledge.store import Actor
from tests.integration import test_m1_03_draft_generation_postgres as _generation_tests
from tests.integration.test_m1_03_draft_generation_postgres import (
    ScriptedModel,
    _evidence,
    _reply,
    _seed,
    _worker,
)

dsn = _generation_tests.dsn
stores = _generation_tests.stores
_isolated = _generation_tests._isolated

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

REVIEWER = Actor("alice", "basic_auth")


def _call_payload(model):
    return json.loads(model.calls[0].messages[1]["content"])


def _reply_r29(incident, *, revision, key="checkout-errors"):
    reply = _reply(incident, proposals=[])
    reply["proposals"] = [
        {
            "key": key,
            "name": "token=supersecret",
            "tags": [],
            "symptoms": ["5xx"],
            "checks": ["error ratio"],
            "evidence_ids": [_evidence(incident)],
            "supersedes_revision": revision,
        }
    ]
    return reply


def _publish_one(stores, dsn):
    knowledge, _, durable = stores
    incident = _seed(dsn)
    proposal = {
        "key": "checkout-errors",
        "name": "token=supersecret",
        "tags": [],
        "symptoms": ["5xx"],
        "checks": ["error ratio"],
        "evidence_ids": [_evidence(incident)],
    }
    [first] = _worker(
        stores, ScriptedModel(_reply(incident, proposals=[proposal]))
    ).poll_once()
    snapshot = knowledge.incident_postmortem(incident)["postmortem"]
    approved = knowledge.approve(
        snapshot["postmortem_id"],
        first.version,
        expected_generation=snapshot["generation"],
        idempotency_key=str(uuid4()),
        actor=REVIEWER,
    )
    durable.append_input(incident, uuid4(), {"text": "new input"})
    return incident, approved.published["checkout-errors"]["entry_id"], proposal


def test_d39_r21_r22_r23_model_receives_sorted_redacted_projection(stores, dsn):
    incident, entry_id, proposal = _publish_one(stores, dsn)
    knowledge, jobs, _ = stores
    raw = jobs.read_input(incident)
    built = build_input(raw, secrets=["supersecret"])
    payload = built.payload
    entries = payload["published_knowledge_entries"]
    assert [e["key"] for e in entries] == sorted(e["key"] for e in entries)
    assert all(set(e) == {"key", "name", "revision"} for e in entries)
    assert all(str(entry_id) not in json.dumps(e) for e in entries)
    assert "supersecret" not in built.payload_text
    assert (
        built.payload_text
        == build_input(jobs.read_input(incident), secrets=["supersecret"]).payload_text
    )
    proposal = deepcopy(proposal)
    proposal["supersedes_revision"] = entries[0]["revision"]
    model = ScriptedModel(_reply(incident, proposals=[proposal]))
    _worker(stores, model).generate(incident)
    sent = _call_payload(model)
    assert sent["published_knowledge_entries"] == entries


def test_r24_no_active_entries_is_empty(stores, dsn):
    incident = _seed(dsn)
    built = build_input(stores[1].read_input(incident), secrets=[])
    assert built.payload["published_knowledge_entries"] == []


def test_d40_revoked_entry_is_not_input_or_replaceable(stores, dsn):
    knowledge, _, _ = stores
    incident, entry_id, proposal = _publish_one(stores, dsn)
    history = knowledge.knowledge_history(entry_id)
    knowledge.revoke(
        entry_id,
        1,
        reason="retired",
        expected_generation=history["generation"],
        idempotency_key=str(uuid4()),
        actor=REVIEWER,
    )
    built = build_input(stores[1].read_input(incident), secrets=[])
    assert built.payload["published_knowledge_entries"] == []
    from opspilot.knowledge.generation import assemble, parse_model_output

    out = parse_model_output(json.dumps(_reply(incident, proposals=[proposal])))
    assert assemble(built, out, {}, record={}).proposals[0].supersedes_entry_id is None


def test_d44_r26_new_version_changes_generation_record_old_remains_readable(
    stores, dsn
):
    knowledge, _, _ = stores
    incident, entry_id, proposal = _publish_one(stores, dsn)
    proposal = deepcopy(proposal)
    proposal["supersedes_revision"] = knowledge.active_revision(entry_id)["revision"]
    model = ScriptedModel(_reply(incident, proposals=[proposal]))
    [outcome] = _worker(stores, model).poll_once()
    versions = knowledge.incident_postmortem(incident)["postmortem"]["versions"]
    assert versions[-1]["version"] == outcome.version
    old_record = json.loads(versions[0]["content"])["generation"]
    new_record = json.loads(versions[-1]["content"])["generation"]
    assert new_record["input_sha256"] != old_record["input_sha256"]
    # Simulate the legacy persisted generation record this r8 migration must
    # remain compatible with.  The actual old version is retained unchanged.
    legacy_record = {
        **old_record,
        "prompt_version": "f13-postmortem-prompt-v2",
        "output_schema_version": "f13-postmortem-output-v1",
        "input_policy_version": "f13-postmortem-input-v1",
        "contract_revision": "r7",
    }
    assert new_record["prompt_version"] == "f13-postmortem-prompt-v3"
    assert new_record["output_schema_version"] == "f13-postmortem-output-v2"
    assert new_record["input_policy_version"] == "f13-postmortem-input-v2"
    assert new_record["contract_revision"] == "r8"
    assert new_record["prompt_version"] != legacy_record["prompt_version"]
    assert new_record["output_schema_version"] != legacy_record["output_schema_version"]
    assert new_record["input_policy_version"] != legacy_record["input_policy_version"]
    assert versions[0]["content_sha256"] != versions[-1]["content_sha256"]
    assert versions[0]["state"] == "approved"


def test_d42_entry_generation_conflict_rejects_approval(stores, dsn):
    knowledge, _, _ = stores
    incident, entry_id, proposal = _publish_one(stores, dsn)
    proposal = deepcopy(proposal)
    proposal["supersedes_revision"] = knowledge.active_revision(entry_id)["revision"]
    model = ScriptedModel(_reply(incident, proposals=[proposal]))
    [outcome] = _worker(stores, model).poll_once()
    pm = knowledge.incident_postmortem(incident)["postmortem"]
    current = knowledge.knowledge_history(entry_id)["generation"]
    with pytest.raises(Exception):
        knowledge.approve(
            pm["postmortem_id"],
            outcome.version,
            expected_generation=pm["generation"],
            idempotency_key=str(uuid4()),
            actor=REVIEWER,
            entry_generations={entry_id: current - 1},
        )


def test_r29_matching_key_and_revision_binds_supersede_target(stores, dsn):
    knowledge, _, _ = stores
    incident, entry_id, _ = _publish_one(stores, dsn)
    revision = knowledge.active_revision(entry_id)["revision"]
    model = ScriptedModel(_reply_r29(incident, revision=revision))
    outcomes = _worker(stores, model).poll_once()
    assert model.calls
    assert "supersedes_revision" in model.calls[0].messages[0]["content"]
    [outcome] = outcomes
    assert outcome.state == "under_review"
    version = knowledge.incident_postmortem(incident)["postmortem"]["versions"][-1]
    assert version["proposals"][0]["supersedes_entry_id"] == entry_id


@pytest.mark.parametrize(
    "revision,key",
    [(99, "checkout-errors"), (None, "checkout-errors"), (2, "unknown-key")],
)
def test_r29_bad_target_revision_retries_once_then_output_invalid(
    stores, dsn, revision, key
):
    incident, _, _ = _publish_one(stores, dsn)
    bad = _reply_r29(incident, revision=revision, key=key)
    model = ScriptedModel(bad, deepcopy(bad))
    [outcome] = _worker(stores, model).poll_once()
    assert (outcome.status, outcome.error_code, outcome.model_requests) == (
        "failed",
        "OUTPUT_INVALID",
        2,
    )


def test_r29_repair_with_correct_revision_binds_target(stores, dsn):
    knowledge, _, _ = stores
    incident, entry_id, _ = _publish_one(stores, dsn)
    revision = knowledge.active_revision(entry_id)["revision"]
    first = _reply_r29(incident, revision=99)
    repaired = _reply_r29(incident, revision=revision)
    model = ScriptedModel(first, repaired)
    [outcome] = _worker(stores, model).poll_once()
    assert (outcome.status, outcome.state, outcome.model_requests) == (
        "succeeded",
        "under_review",
        2,
    )
    version = knowledge.incident_postmortem(incident)["postmortem"]["versions"][-1]
    assert version["proposals"][0]["supersedes_entry_id"] == entry_id
