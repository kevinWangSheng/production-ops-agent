"""F13 acceptance projection without a database (M1-03 step 4, r6).

``postmortem_outcome`` copies committed records and refuses another
subject's (D11); ``postmortem_records`` reads inside a stable-generation
boundary (D34); the seam has no model, telemetry or SQL of its own (R12).
The PostgreSQL side is ``tests/integration/test_m1_03_postmortem_outcome
_postgres.py``.
"""

import ast
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

import opspilot.acceptance_postmortem as seam
from opspilot.acceptance import (
    PostmortemOutcome,
    PostmortemRecords,
    postmortem_outcome,
    postmortem_records,
)

AT = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
INCIDENT = uuid4()
POSTMORTEM = uuid4()
ENTRY = uuid4()
SCENARIO = SimpleNamespace(scenario_id="f13-unit", subject_id=str(INCIDENT))


def _audit(kind: str, object_id: UUID, action: str, generation: int, **extra):
    return {
        "event_id": uuid4(),
        "idempotency_key": f"k-{generation}-{action}",
        "object_kind": kind,
        "object_id": object_id,
        "action": action,
        "version": extra.get("version"),
        "revision": extra.get("revision"),
        "actor_id": extra.get("actor_id", "alice"),
        "principal_kind": extra.get("principal_kind", "basic_auth"),
        "reason": extra.get("reason"),
        "expected_generation": generation - 1,
        "resulting_generation": generation,
        "recorded_at": AT,
    }


def _records() -> PostmortemRecords:
    document = {
        "incident": {"incident_id": str(INCIDENT)},
        "timeline": [{"at": "2026-10-09T12:00:00+00:00", "event": "incident"}],
        "runs": [],
        "human_actions": [{"action": "takeover"}],
        "recovery": {"ended_reason": "recovery_confirmed"},
        "evidence_catalog": [{"evidence_id": "s:requests", "kind": "recovery"}],
        "generation": {"model_requests": 1},
        "validation": {"citations_valid": True, "errors": {}},
    }
    dispute = {
        "dispute_id": uuid4(),
        "postmortem_id": POSTMORTEM,
        "version": 1,
        "conclusion_key": "f1",
        "reason": "contradicted",
        "raised_by": "worker-1",
        "raised_by_kind": "worker",
        "event_id": uuid4(),
        "raised_at": AT,
    }
    version = {
        "postmortem_id": POSTMORTEM,
        "version": 1,
        "revises_version": None,
        "state": "approved",
        "stale_reason": None,
        "content": json.dumps(document),
        "content_sha256": "a" * 64,
        "incident_control_generation": 2,
        "observation_generation": 1,
        "observation_session_id": uuid4(),
        "observation_ending_id": uuid4(),
        "run_count": 1,
        "last_run_id": uuid4(),
        "input_watermark": 0,
        "evidence_snapshot_sha256": "b" * 64,
        "generated_by": "worker-1",
        "generated_by_kind": "worker",
        "created_at": AT,
        "conclusions": [
            {
                "postmortem_id": POSTMORTEM,
                "version": 1,
                "conclusion_key": "f1",
                "ordinal": 0,
                "section": "findings",
                "body": "errors rose",
                "author": "model",
                "certainty": "supported",
                "citations_valid": True,
                "evidence_refs": [{"evidence_id": "s:requests"}],
                "dispute_state": "disputed",
                "disputes": [dispute],
            }
        ],
        "proposals": [
            {
                "postmortem_id": POSTMORTEM,
                "version": 1,
                "proposal_key": "kb-1",
                "name": "n",
                "tags": ["t"],
                "content": "{}",
                "content_sha256": "c" * 64,
                "supersedes_entry_id": None,
            }
        ],
    }
    revision = {
        "entry_id": ENTRY,
        "revision": 1,
        "state": "active",
        "name": "n",
        "tags": ["t"],
        "content": "{}",
        "content_sha256": "c" * 64,
        "source_postmortem_id": POSTMORTEM,
        "source_version": 1,
        "source_proposal_key": "kb-1",
        "supersedes_revision": None,
        "approved_by": "alice",
        "approved_by_kind": "basic_auth",
        "event_id": uuid4(),
        "approved_at": AT,
        "revoked_reason": None,
        "revoked_by": None,
        "revoked_by_kind": None,
        "revoked_at": None,
    }
    return PostmortemRecords(
        incident_id=INCIDENT,
        view={
            "incident_id": INCIDENT,
            "status": "approved",
            "postmortem": {
                "postmortem_id": POSTMORTEM,
                "incident_id": INCIDENT,
                "generation": 3,
                "latest_version": 1,
                "versions": [version],
            },
            "schedule": {"consecutive_failures": 0},
            "attempts": [{"incident_id": INCIDENT, "status": "succeeded"}],
        },
        published=(
            {
                "entry_id": ENTRY,
                "revision": 1,
                "name": "n",
                "source_version": 1,
                "source_proposal_key": "kb-1",
                "supersedes_revision": None,
                "state": "active",
            },
        ),
        entries={ENTRY: {"entry_id": ENTRY, "generation": 1, "revisions": [revision]}},
        active={
            ENTRY: {**revision, "freshness": {"approved_at": AT}},
        },
        postmortem_audit=(
            _audit(
                "postmortem",
                POSTMORTEM,
                "generate",
                1,
                principal_kind="worker",
                actor_id="worker-1",
                version=1,
            ),
            _audit("postmortem", POSTMORTEM, "approve", 3, version=1),
        ),
        entry_audit={
            ENTRY: (_audit("knowledge_entry", ENTRY, "publish", 1, revision=1),)
        },
    )


def test_committed_records_are_copied() -> None:
    outcome = postmortem_outcome(SCENARIO, _records())
    assert isinstance(outcome, PostmortemOutcome)
    assert (outcome.generation_status, outcome.unknown_reasons) == ("approved", ())
    assert (outcome.postmortem_id, outcome.postmortem_generation) == (POSTMORTEM, 3)
    (version,) = outcome.versions
    assert version.sections["human_actions"] == [{"action": "takeover"}]
    assert version.sections["recovery"] == {"ended_reason": "recovery_confirmed"}
    assert version.watermark["incident_control_generation"] == 2
    assert version.validation == {"citations_valid": True, "errors": {}}
    (conclusion,) = version.conclusions
    assert conclusion.dispute_state == "disputed"
    assert conclusion.disputes[0]["reason"] == "contradicted"
    assert [a.action for a in version.review_actions] == ["generate", "approve"]
    (revision,) = outcome.knowledge
    assert (revision.state, revision.retrievable) == ("active", True)
    assert revision.freshness == {"approved_at": AT}
    assert (revision.approved_by, revision.approved_by_kind) == ("alice", "basic_auth")
    assert [a.action for a in outcome.knowledge_actions] == ["publish"]
    assert outcome.model_requests == ()


def test_a_revision_the_knowledge_read_does_not_return_is_not_retrievable() -> None:
    records = _records()
    records.active[ENTRY] = None  # type: ignore[index]
    (revision,) = postmortem_outcome(SCENARIO, records).knowledge
    assert (revision.retrievable, revision.freshness) == (False, None)


def test_nothing_written_is_not_generated() -> None:
    view = {
        "incident_id": INCIDENT,
        "status": "not_generated",
        "postmortem": None,
        "schedule": {"consecutive_failures": 0},
        "attempts": [],
    }
    outcome = postmortem_outcome(
        SCENARIO, PostmortemRecords(INCIDENT, view, (), {}, {}, (), {})
    )
    assert (outcome.generation_status, outcome.versions, outcome.knowledge) == (
        "not_generated",
        (),
        (),
    )


def _corrupt(path: str):
    """A records copy with one field moved to another subject."""
    records = _records()
    view = deepcopy(dict(records.view))
    other = uuid4()
    version = view["postmortem"]["versions"][0]
    if path == "records":
        return PostmortemRecords(
            other,
            *(
                getattr(records, f)
                for f in (
                    "view",
                    "published",
                    "entries",
                    "active",
                    "postmortem_audit",
                    "entry_audit",
                )
            ),
        )
    if path == "view":
        view["incident_id"] = other
    elif path == "snapshot":
        view["postmortem"]["incident_id"] = other
    elif path == "attempt":
        view["attempts"][0]["incident_id"] = other
    elif path == "version":
        version["postmortem_id"] = other
    elif path == "conclusion":
        version["conclusions"][0]["version"] = 2
    elif path == "dispute":
        version["conclusions"][0]["disputes"][0]["conclusion_key"] = "f2"
    elif path == "proposal":
        version["proposals"][0]["postmortem_id"] = other
    elif path == "published":
        entries = {ENTRY: deepcopy(dict(records.entries[ENTRY]))}
        entries[ENTRY]["revisions"][0]["source_postmortem_id"] = other
        return PostmortemRecords(
            INCIDENT,
            view,
            records.published,
            entries,
            records.active,
            records.postmortem_audit,
            records.entry_audit,
        )
    elif path == "postmortem_audit":
        audit = (_audit("postmortem", other, "approve", 3, version=1),)
        return PostmortemRecords(
            INCIDENT,
            view,
            records.published,
            records.entries,
            records.active,
            audit,
            records.entry_audit,
        )
    elif path == "entry_audit":
        audit = {ENTRY: (_audit("knowledge_entry", other, "publish", 1, revision=1),)}
        return PostmortemRecords(
            INCIDENT,
            view,
            records.published,
            records.entries,
            records.active,
            records.postmortem_audit,
            audit,
        )
    elif path == "active":
        active = {ENTRY: {"entry_id": other, "revision": 1, "freshness": {}}}
        return PostmortemRecords(
            INCIDENT,
            view,
            records.published,
            records.entries,
            active,
            records.postmortem_audit,
            records.entry_audit,
        )
    elif path == "extra_entry":
        entries = {
            **records.entries,
            other: {"entry_id": other, "generation": 1, "revisions": []},
        }
        return PostmortemRecords(
            INCIDENT,
            view,
            records.published,
            entries,
            records.active,
            records.postmortem_audit,
            records.entry_audit,
        )
    return PostmortemRecords(
        INCIDENT,
        view,
        records.published,
        records.entries,
        records.active,
        records.postmortem_audit,
        records.entry_audit,
    )


@pytest.mark.parametrize(
    "path",
    [
        "records",
        "view",
        "snapshot",
        "attempt",
        "version",
        "conclusion",
        "dispute",
        "proposal",
        "published",
        "postmortem_audit",
        "entry_audit",
        "active",
        "extra_entry",
    ],
)
def test_records_of_another_subject_project_unknown(path: str) -> None:
    outcome = postmortem_outcome(SCENARIO, _corrupt(path))
    assert (outcome.generation_status, outcome.unknown_reasons) == (
        "unknown",
        ("SUBJECT_MISMATCH",),
    )
    assert (outcome.postmortem_id, outcome.versions, outcome.knowledge) == (
        None,
        (),
        (),
    )
    assert (outcome.review_actions, outcome.knowledge_actions) == ((), ())


class _MovingStore:
    """Read surface whose postmortem generation moves ``moves`` times."""

    def __init__(self, moves: int) -> None:
        self.records = _records()
        self.moves = moves
        self.generation = 3

    def incident_postmortem(self, incident_id):
        view = deepcopy(dict(self.records.view))
        view["postmortem"]["generation"] = self.generation
        if self.moves:
            self.moves -= 1
            self.generation += 1
        return view

    def knowledge_from_postmortem(self, postmortem_id):
        return list(self.records.published)

    def knowledge_history(self, entry_id):
        return self.records.entries[entry_id]

    def active_revision(self, entry_id):
        return self.records.active[entry_id]

    def audit_trail(self, kind, object_id):
        if kind == "postmortem":
            return list(self.records.postmortem_audit)
        return list(self.records.entry_audit[object_id])


def test_a_moved_generation_is_read_again() -> None:
    store = _MovingStore(moves=1)
    records = postmortem_records(store, INCIDENT)
    assert records.view["postmortem"]["generation"] == 4
    assert postmortem_outcome(SCENARIO, records).postmortem_generation == 4


def test_a_generation_that_never_settles_is_refused() -> None:
    with pytest.raises(ValueError, match="^RECORDS_UNSTABLE$"):
        postmortem_records(_MovingStore(moves=100), INCIDENT, attempts=3)


def test_the_seam_imports_no_model_telemetry_or_sql() -> None:
    tree = ast.parse(Path(seam.__file__).read_text())
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imported <= {
        "__future__",
        "json",
        "collections.abc",
        "dataclasses",
        "datetime",
        "typing",
        "uuid",
        "opspilot.knowledge.contract",
    }
