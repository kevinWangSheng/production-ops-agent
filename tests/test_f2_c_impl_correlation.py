"""F2 PR-C implementation tests: the shared correlation rule and frozen
vocabularies (no database)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib import import_module
from uuid import UUID

from opspilot.persistence.alerts import ATTACH_NAMESPACE, OUTCOMES
from opspilot.persistence.correlation import (
    Correlation,
    CorrelationCandidate,
    choose_correlation,
)
from opspilot.web.service import _INTAKE_NAMESPACE

T0 = datetime(2026, 10, 21, tzinfo=UTC)
A = UUID(int=1)
B = UUID(int=2)
C = UUID(int=3)


def candidate(incident_id: UUID, lifecycle: str, minutes: int) -> CorrelationCandidate:
    return CorrelationCandidate(incident_id, lifecycle, T0 + timedelta(minutes=minutes))


def test_no_candidate_opens_an_unrelated_incident():
    assert choose_correlation([]) == Correlation(attach_to=None, related_to=None)


def test_every_non_closed_lifecycle_attaches():
    for lifecycle in ("open", "observing_recovery", "resolved"):
        assert choose_correlation([candidate(A, lifecycle, 0)]) == Correlation(A, None)


def test_the_latest_open_candidate_wins_over_older_and_closed_ones():
    rows = [
        candidate(A, "open", 0),
        candidate(B, "resolved", 5),
        candidate(C, "closed", 9),
    ]
    assert choose_correlation(rows) == Correlation(attach_to=B, related_to=None)


def test_all_closed_relates_to_the_latest_closed():
    rows = [candidate(B, "closed", 5), candidate(A, "closed", 0)]
    assert choose_correlation(rows) == Correlation(attach_to=None, related_to=B)


def test_created_at_ties_take_the_larger_incident_id():
    rows = [candidate(C, "open", 0), candidate(A, "open", 0)]
    assert choose_correlation(rows).attach_to == C
    closed = [candidate(A, "closed", 0), candidate(B, "closed", 0)]
    assert choose_correlation(closed).related_to == B


def test_outcome_vocabulary_matches_migration_0018():
    migration = import_module("opspilot.migrations.versions.0018_alert_correlation")
    assert migration.OUTCOMES == OUTCOMES
    assert set(OUTCOMES) - set(migration.OUTCOMES_0010) == {"attached"}
    assert migration.down_revision == "0017_web_suspension_grants"


def test_attach_namespace_is_the_intake_namespace():
    assert ATTACH_NAMESPACE == _INTAKE_NAMESPACE
