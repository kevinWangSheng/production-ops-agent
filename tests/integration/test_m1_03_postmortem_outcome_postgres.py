"""F13 acceptance projection over a real ``KnowledgeStore`` (M1-03 step 4).

Implementer checks of ``postmortem_records`` / ``postmortem_outcome`` on
records the store really committed: provenance, reviewer and hash of a
published revision, supersede and revoke with the knowledge read, and
another incident's records refused as ``unknown`` (D11). The independent
F13 acceptance scenarios (worker generation, HTTP review, page consistency)
are ``tests/acceptance/test_f13_postmortem.py``.
"""

import os
from collections.abc import Iterator
from types import SimpleNamespace
from uuid import uuid4

import pytest
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.acceptance import postmortem_outcome, postmortem_records
from opspilot.knowledge import KnowledgeStore
from scripts.m0.postgres_lab import DSN
from tests.integration.test_m1_03_knowledge_store_postgres import (
    PG_DUMP,
    POOL,
    REVIEWER,
    _approved,
    _draft,
    _drop_database,
    _key,
    _new_database,
    _proposal,
    _seed_incident,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)


@pytest.fixture(scope="module")
def dsn() -> Iterator[str]:
    name = _new_database()
    scratch = make_conninfo(DSN, dbname=name)
    schema.migrate(scratch, pg_dump=PG_DUMP)
    try:
        yield scratch
    finally:
        _drop_database(name)


@pytest.fixture(scope="module")
def store(dsn: str) -> Iterator[KnowledgeStore]:
    knowledge = KnowledgeStore(dsn, pool=POOL)
    knowledge.install()
    yield knowledge
    knowledge.close()


def _scenario(incident_id) -> SimpleNamespace:
    return SimpleNamespace(scenario_id="f13-pg", subject_id=str(incident_id))


def _outcome(store, incident_id):
    return postmortem_outcome(
        _scenario(incident_id), postmortem_records(store, incident_id)
    )


def test_an_incident_without_a_postmortem_is_not_generated(store, dsn) -> None:
    incident_id, _ = _seed_incident(dsn)
    outcome = _outcome(store, incident_id)
    assert (outcome.generation_status, outcome.postmortem_id) == ("not_generated", None)
    assert outcome.versions == () and outcome.knowledge == ()


def test_a_draft_under_review_publishes_nothing(store, dsn) -> None:
    incident_id, watermark = _seed_incident(dsn)
    draft = _draft(store, incident_id, watermark, proposals=[_proposal()])
    outcome = _outcome(store, incident_id)
    assert outcome.generation_status == "under_review"
    (version,) = outcome.versions
    assert (version.version, version.state) == (draft.version, "under_review")
    assert version.watermark["observation_ending_id"] == watermark.observation_ending_id
    assert [c.conclusion_key for c in version.conclusions] == ["timeline", "finding-1"]
    assert version.conclusions[1].evidence_refs[0]["citable_as_fact"] is True
    assert [a.action for a in outcome.review_actions] == ["generate", "submit"]
    assert outcome.knowledge == ()


def test_publish_supersede_and_revoke_through_the_projection(store, dsn) -> None:
    first = _approved(store, dsn)
    entry_id = first.published["kb-1"]["entry_id"]
    first_incident = store.postmortem(first.object_id)["incident_id"]
    (published,) = _outcome(store, first_incident).knowledge
    assert (published.revision, published.state, published.retrievable) == (
        1,
        "active",
        True,
    )
    assert (published.approved_by, published.approved_by_kind) == (
        REVIEWER.actor_id,
        "basic_auth",
    )
    assert (published.source_postmortem_id, published.source_version) == (
        first.object_id,
        1,
    )
    assert len(published.content_sha256) == 64
    assert published.freshness is not None

    second = _approved(
        store,
        dsn,
        proposals=[_proposal(supersedes=entry_id)],
        entry_generations={entry_id: 1},
    )
    second_incident = store.postmortem(second.object_id)["incident_id"]
    store.revoke(
        entry_id,
        2,
        reason="root cause was the cache",
        expected_generation=2,
        idempotency_key=_key(),
        actor=REVIEWER,
    )
    outcome = _outcome(store, second_incident)
    # only the revision this postmortem published; the entry's whole audit
    # trail still shows how it got there
    assert [(k.revision, k.state, k.retrievable) for k in outcome.knowledge] == [
        (2, "revoked", False),
    ]
    revoked = outcome.knowledge[0]
    assert (revoked.revoked_reason, revoked.revoked_by) == (
        "root cause was the cache",
        REVIEWER.actor_id,
    )
    assert revoked.supersedes_revision == 1
    assert [a.action for a in outcome.knowledge_actions] == [
        "publish",
        "supersede",
        "revoke",
    ]
    # the first postmortem's projection keeps only its own revision, superseded
    assert [
        (k.revision, k.state, k.source_postmortem_id)
        for k in _outcome(store, first_incident).knowledge
    ] == [(1, "superseded", first.object_id)]


def test_records_of_another_incident_are_unknown(store, dsn) -> None:
    approved = _approved(store, dsn)
    incident_id = store.postmortem(approved.object_id)["incident_id"]
    other = uuid4()
    outcome = postmortem_outcome(
        _scenario(other), postmortem_records(store, incident_id)
    )
    assert (outcome.generation_status, outcome.unknown_reasons) == (
        "unknown",
        ("SUBJECT_MISMATCH",),
    )
    assert outcome.versions == () and outcome.knowledge == ()
