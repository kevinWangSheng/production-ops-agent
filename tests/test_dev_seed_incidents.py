"""The dev seed's fixture shape (web UX spec 2026-11-16, section 7) and its
refusal of non-dev databases. Seeding and ``--reset`` need Postgres."""

from __future__ import annotations

import pytest

from opspilot.web.store import MappingTargetRegistry
from scripts.dev_seed_incidents import (
    MARKER_TITLE,
    NEEDS_HUMAN_KINDS,
    SERVICES,
    TITLE_PREFIX,
    TOTAL,
    alert_for,
    check_dsn,
    plan,
    registry_entries,
    service_for,
    title_for,
)


def test_plan_has_exactly_30_waiting_with_the_marker_oldest_at_position_1500():
    kinds = plan()
    assert len(kinds) == TOTAL == 2003
    waiting = [i for i, k in enumerate(kinds) if k in NEEDS_HUMAN_KINDS]
    assert len(waiting) == 30
    marker = kinds.index("marker")
    assert marker == min(waiting) and TOTAL - marker == 1500
    assert MARKER_TITLE in title_for(marker, "marker")
    assert kinds.count("queued") == 12
    assert kinds.count("resolved") == 3
    assert kinds.count("waiting") >= 1
    assert kinds.count("handoff") + kinds.count("ambiguous") >= 1
    assert kinds.count("closed") == TOTAL - 42


def test_titles_are_tagged_long_enough_and_services_are_mixed():
    kinds = plan()
    titles = [title_for(i, k) for i, k in enumerate(kinds)]
    assert all(t.startswith(TITLE_PREFIX) for t in titles)
    assert sum(len(t) > 200 for t in titles) >= 20
    assert len(SERVICES) >= 8
    assert {service_for(i, k) for i, k in enumerate(kinds)} == set(SERVICES)
    assert alert_for(0, "closed")["annotations"]["summary"] == titles[0]


def test_registry_is_valid_and_ambiguous_entries_share_one_match():
    registry = MappingTargetRegistry(registry_entries())
    assert len(registry.match_alert({"service": "ambiguous"})) == 2
    for name in SERVICES:
        assert len(registry.match_alert({"service": name})) == 1
    assert registry.match_alert({"service": "unmapped-1"}) == ()


@pytest.mark.parametrize("name", ["opspilot", "production", "m0_budget"])
def test_refuses_databases_without_dev_or_test_in_the_name(name):
    with pytest.raises(SystemExit):
        check_dsn(f"host=127.0.0.1 dbname={name}")


@pytest.mark.parametrize("name", ["opspilot_dev", "ux_test"])
def test_accepts_dev_and_test_databases(name):
    assert check_dsn(f"host=127.0.0.1 dbname={name}") == name
