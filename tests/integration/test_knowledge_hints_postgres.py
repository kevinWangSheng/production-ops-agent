"""Real store retrieval; knowledge is published only through review primitives."""

import os
from datetime import datetime
from uuid import UUID

import psycopg
import pytest

from opspilot.knowledge import Actor, ProposalDraft, canonical_json
from opspilot.knowledge.hints import valid_knowledge
from tests.integration.test_m1_03_knowledge_contract_postgres import (
    _draft,
)
from tests.integration.test_m1_03_knowledge_contract_postgres import (
    incident as incident,
)
from tests.integration.test_m1_03_knowledge_contract_postgres import (
    scratch_dsn as scratch_dsn,
)
from tests.integration.test_m1_03_knowledge_contract_postgres import (
    store as store,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)
REVIEWER = Actor("alice", "basic_auth")


def uid(store, incident):
    with psycopg.connect(store.dsn) as conn:
        return conn.execute(
            "SELECT t.resource_uid FROM opspilot_targets t JOIN opspilot_incidents i USING(target_id) WHERE i.incident_id=%s",
            (incident[0],),
        ).fetchone()[0]


def approve(store, incident, proposals, *, expected=0, entry_generations=None):
    draft = _draft(
        store,
        incident,
        key=f"draft-{incident[0]}-{expected}",
        proposals=proposals,
        expected=expected,
    )
    return store.approve(
        draft.object_id,
        draft.version,
        expected_generation=draft.generation,
        idempotency_key=f"approve-{incident[0]}-{expected}",
        actor=REVIEWER,
        entry_generations=entry_generations or {},
    )


def test_target_rank_provenance_age_and_active_only(store, incident):
    published = approve(
        store,
        incident,
        [
            ProposalDraft("low", "unrelated", [], {"checks": ["inspect"]}),
            ProposalDraft(
                "high",
                "needle",
                ["needle"],
                {"checks": ["needle"], "evidence_refs": ["old-id"]},
            ),
        ],
    )
    target = uid(store, incident)
    fact = store.knowledge_for_target(target, "needle", max_age_seconds=2592000)
    assert valid_knowledge(fact)
    assert fact["entries"][0]["name"] == "needle"
    assert len(fact["entries"]) == 2  # non-matches are retained
    entry = fact["entries"][0]
    assert entry["source_incident_id"] == str(incident[0])
    assert entry["source_postmortem_id"] == str(published.object_id)
    assert entry["approved_by"] == "alice"
    assert entry["age_seconds"] == int(
        (
            datetime.fromisoformat(fact["retrieved_at"])
            - datetime.fromisoformat(entry["observation_ended_at"])
        ).total_seconds()
    )
    assert entry["freshness"] == "fresh" and "old-id" not in entry["content"]
    assert (
        store.knowledge_for_target("different-target", "needle", max_age_seconds=0)[
            "entries"
        ]
        == []
    )
    assert (
        store.knowledge_for_target(target, "needle", limit=1, max_age_seconds=0)[
            "entries"
        ][0]["name"]
        == "needle"
    )
    assert (
        store.knowledge_for_target(target, "needle", limit=0, max_age_seconds=0)[
            "entries"
        ]
        == []
    )
    entry_id = UUID(entry["entry_id"])
    store.revoke(
        entry_id,
        1,
        reason="outdated",
        expected_generation=1,
        idempotency_key="revoke-hint",
        actor=REVIEWER,
    )
    after = store.knowledge_for_target(target, "needle", max_age_seconds=2592000)
    assert [e["name"] for e in after["entries"]] == ["unrelated"]


def test_superseded_revision_is_replaced(store, incident):
    first = approve(store, incident, [ProposalDraft("p1", "old", [], {"checks": []})])
    entry_id = first.published["p1"]["entry_id"]
    approve(
        store,
        incident,
        [ProposalDraft("p2", "new", [], {"checks": []}, entry_id)],
        expected=first.generation,
        entry_generations={entry_id: 1},
    )
    entries = store.knowledge_for_target(
        uid(store, incident), "old", max_age_seconds=1000
    )["entries"]
    assert [(e["entry_id"], e["revision"], e["name"]) for e in entries] == [
        (str(entry_id), 2, "new")
    ]


def test_truncation_and_total_size_drop_lowest_rank(store, incident):
    approve(
        store,
        incident,
        [
            ProposalDraft(
                str(i),
                f"hint-{i}",
                [],
                {"checks": ["大" * 4000], "evidence_refs": ["old-id"]},
            )
            for i in range(3)
        ],
    )
    fact = store.knowledge_for_target(uid(store, incident), "", max_age_seconds=1000)
    assert valid_knowledge(fact)
    assert fact["omitted_for_size"] == 1 and len(fact["entries"]) == 2
    assert len(canonical_json(fact["entries"]).encode()) <= 12288
    for entry in fact["entries"]:
        assert entry["truncated"] and len(entry["content"].encode()) <= 4096
        assert "old-id" not in entry["content"]


def test_exact_freshness_boundary_with_snapshot_clock(store, incident, monkeypatch):
    from contextlib import contextmanager
    from datetime import timedelta

    approve(store, incident, [ProposalDraft("p1", "boundary", [], {"checks": []})])
    target = uid(store, incident)
    initial = store.knowledge_for_target(target, "", max_age_seconds=100)
    observed = datetime.fromisoformat(initial["entries"][0]["observation_ended_at"])
    transaction = store.transaction
    clock = observed + timedelta(seconds=100)

    class Snapshot:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, statement, params=None):
            if statement == "SELECT clock_timestamp() AS retrieved_at":
                return self.conn.execute(
                    "SELECT %s::timestamptz AS retrieved_at", (clock,)
                )
            return self.conn.execute(statement, params)

    @contextmanager
    def injected_transaction(*, snapshot=False):
        assert snapshot
        with transaction(snapshot=snapshot) as conn:
            assert (
                conn.execute("SHOW transaction_read_only").fetchone()[
                    "transaction_read_only"
                ]
                == "on"
            )
            yield Snapshot(conn)

    monkeypatch.setattr(store, "transaction", injected_transaction)
    at_boundary = store.knowledge_for_target(target, "", max_age_seconds=100)
    assert at_boundary["entries"][0]["age_seconds"] == 100
    assert at_boundary["entries"][0]["freshness"] == "fresh"
    clock += timedelta(seconds=1)
    after = store.knowledge_for_target(target, "", max_age_seconds=100)
    assert after["entries"][0]["age_seconds"] == 101
    assert after["entries"][0]["freshness"] == "stale"
