"""F7 step 5: reconstruct an incident from audit records; per-target limits.

``feature_list.json`` F7 step 5: "Reconstruct input, queries, evidence, state
changes, human corrections, recovery observations and postmortem review from
audit records; enforce per-target query and data-volume limits".

Contract:

* ``docs/tasks/2026-10-15-f7-contract.md`` S5a/S5b (raw records exist; one
  time-ordered reconstruction is missing), S5c + U4-a (the limits are the
  existing single-call ceilings -- window, 2 MiB, 30 s, view tokens -- bound
  per registration to the one registered target; no cumulative cap), R2
  (read-only projection, old data missing a source is reported, not an
  error), R6 (order by PostgreSQL time columns, never model or source time);
* ``docs/tasks/2026-10-16-f7-authz-audit.md`` A6 and "接口": ``AUDIT_CATEGORIES``,
  ``AuditEntry``, ``AuditTimeline``, ``audit_timeline(conn, incident_id)``
  and the source table of every category.

The PostgreSQL tests need ``M1_DURABLE_POSTGRES=1`` and ``OPSPILOT_LAB_DSN``
(a scratch database is created and dropped). The chain is built through the
product's routes and stores (``tests/acceptance/f7_audit_support.py``). The
projection's import is unconditional: a missing ``opspilot.acceptance_audit``
is an error, not a skip. The U4-a tests are offline.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql

from opspilot.acceptance_audit import (
    AUDIT_CATEGORIES,
    AuditEntry,
    AuditTimeline,
    audit_timeline,
)
from opspilot.tools import (
    MAX_REQUEST_TIMEOUT_SECONDS,
    MAX_RESULT_BYTES,
    TargetRegistry,
    ToolContractError,
    TransportResponse,
)
from opspilot.tools.otel_demo import TARGET_ID
from tests.acceptance.f7_audit_support import (
    CORRECTION_TEXT,
    QUESTION,
    AlertIncident,
    Chain,
    ShortChain,
    build_alert_incident,
    build_chain,
    build_short_chain,
    detail_text,
    free_text_markers,
)
from tests.acceptance.f7_pg_support import REVIEWER, WORKER, scratch_database
from tests.m1_tool_support import WINDOW_START, body, build, registration, request
from tests.m1_web_support import UI_USER
from tests.test_m1_otel_demo_contract import WINDOW_END

pg = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)

EXPECTED_CATEGORIES = (
    "input",
    "query",
    "evidence",
    "state_change",
    "human_correction",
    "recovery_observation",
    "postmortem_review",
)

#: Interface section: the tables each category may come from.
SOURCES = {
    "input": {"opspilot_inputs", "opspilot_input_rounds", "opspilot_alert_deliveries"},
    "query": {"opspilot_tool_charges"},
    "evidence": {"opspilot_evidence"},
    "state_change": {
        "opspilot_subject_events",
        "opspilot_runs",
        "opspilot_suspension_audit",
    },
    "human_correction": {"opspilot_controls"},
    "recovery_observation": {
        "opspilot_observation_sessions",
        "opspilot_observation_samples",
        "opspilot_observation_endings",
        "opspilot_self_heal_authorizations",
    },
    # User decision D1=a (2026-10-16): the 0008 postmortem tables (record,
    # versions, review audit) plus the knowledge entries.
    "postmortem_review": {
        "opspilot_postmortems",
        "opspilot_postmortem_versions",
        "opspilot_f13_audit",
        "opspilot_knowledge_entries",
    },
}


# -- fixtures -------------------------------------------------------------------


@pytest.fixture(scope="module")
def dsn() -> Iterator[str]:
    if os.environ.get("M1_DURABLE_POSTGRES") != "1":
        pytest.skip("explicit PG opt-in required: M1_DURABLE_POSTGRES=1")
    yield from scratch_database()


@pytest.fixture(scope="module")
def chain(dsn: str) -> Chain:
    return build_chain(dsn)


@pytest.fixture(scope="module")
def short_chain(dsn: str, chain: Chain) -> ShortChain:
    return build_short_chain(dsn)


@pytest.fixture(scope="module")
def alert_incident(dsn: str, chain: Chain) -> AlertIncident:
    return build_alert_incident(dsn)


def _timeline(dsn: str, incident: UUID) -> AuditTimeline:
    with psycopg.connect(dsn) as conn:
        return audit_timeline(conn, incident)


def _of(timeline: AuditTimeline, category: str) -> list[AuditEntry]:
    return [e for e in timeline.entries if e.category == category]


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict) or hasattr(value, "items"):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _strings(item)


def _row_counts(dsn: str) -> dict[str, int]:
    with psycopg.connect(dsn) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_type='BASE TABLE' "
                "ORDER BY 1"
            ).fetchall()
        ]
        return {
            table: conn.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            ).fetchone()[0]
            for table in tables
        }


def _incident_runs(dsn: str, incident: UUID) -> set[UUID]:
    with psycopg.connect(dsn) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT run_id FROM opspilot_runs WHERE incident_id=%s", (incident,)
            ).fetchall()
        }


# -- interface shape --------------------------------------------------------------


def test_the_seven_categories_are_the_step5_list():
    assert tuple(AUDIT_CATEGORIES) == EXPECTED_CATEGORIES


# -- S5b: the full chain ------------------------------------------------------------


@pg
def test_s5b_a_full_incident_reconstructs_all_seven_categories(dsn, chain):
    timeline = _timeline(dsn, chain.incident)

    assert timeline.incident_id == chain.incident
    assert set(timeline.counts) == set(EXPECTED_CATEGORIES)
    for category in EXPECTED_CATEGORIES:
        assert timeline.counts[category] >= 1, (category, timeline.counts)
        assert timeline.counts[category] == len(_of(timeline, category))
    assert tuple(timeline.missing) == ()
    assert sum(timeline.counts.values()) == len(timeline.entries)


@pg
def test_s5b_entries_are_ordered_by_pg_time_with_a_contiguous_sequence(dsn, chain):
    timeline = _timeline(dsn, chain.incident)
    entries = timeline.entries
    assert entries

    assert [e.sequence for e in entries] == list(range(len(entries)))
    for before, after in zip(entries, entries[1:]):
        assert before.recorded_at <= after.recorded_at, (before, after)
        if (before.recorded_at, before.source) == (after.recorded_at, after.source):
            assert before.record_id < after.record_id, (before, after)
    for entry in entries:
        assert isinstance(entry, AuditEntry)
        assert entry.category in EXPECTED_CATEGORIES
        assert entry.source in SOURCES[entry.category], entry
        assert entry.recorded_at.tzinfo is not None, entry
        assert isinstance(entry.record_id, str) and entry.record_id
    with pytest.raises(dataclasses.FrozenInstanceError):
        entries[0].sequence = 99  # type: ignore[misc]


@pg
def test_s5b_entries_belong_to_this_incident_only(dsn, chain, short_chain):
    """Built after the main chain on the same database: another incident's
    Run, queries and evidence must not appear in this incident's timeline."""
    timeline = _timeline(dsn, chain.incident)
    own = _incident_runs(dsn, chain.incident)
    assert chain.first_run in own
    assert short_chain.run not in own
    for entry in timeline.entries:
        if entry.run_id is not None:
            assert entry.run_id in own, entry
    for category in ("query", "evidence"):
        assert all(e.run_id is not None for e in _of(timeline, category)), category


@pg
def test_s5b_category_details_name_the_query_the_decision_and_the_records(dsn, chain):
    timeline = _timeline(dsn, chain.incident)

    queries = _of(timeline, "query")
    # Every executed read was charged; a refused call may be charged too.
    assert len(queries) >= chain.executed_reads
    for entry in queries:
        assert {"tool", "target_id"} <= set(entry.detail), entry.detail
        assert entry.detail["target_id"] == TARGET_ID, entry.detail
        assert entry.detail["tool"] in {"metrics_range_query", "traces_search"}

    actions = sorted(e.detail.get("action") for e in _of(timeline, "human_correction"))
    assert actions == ["correct", "register_remediation"]
    assert {e.actor for e in _of(timeline, "human_correction")} == {UI_USER}

    recovery = {e.source for e in _of(timeline, "recovery_observation")}
    assert {
        "opspilot_observation_sessions",
        "opspilot_observation_samples",
        "opspilot_observation_endings",
    } <= recovery
    assert str(chain.session_id) in {
        e.record_id for e in _of(timeline, "recovery_observation")
    }

    review = _of(timeline, "postmortem_review")
    assert "opspilot_knowledge_entries" in {e.source for e in review}
    assert {"opspilot_postmortems", "opspilot_postmortem_versions"} & {
        e.source for e in review
    }
    # The human review is attributable to its reviewer, and the draft to
    # the worker that wrote it.
    assert REVIEWER.actor_id in {e.actor for e in review}, review
    assert WORKER.actor_id in {e.actor for e in review}, review


@pg
def test_s5b_causal_order_holds(dsn, chain):
    timeline = _timeline(dsn, chain.incident)
    first = {c: min(e.recorded_at for e in _of(timeline, c)) for c in timeline.counts}

    # The input came before the first query.
    assert first["input"] < first["query"]

    # The correction came after the conclusion it corrects: after every
    # query and evidence record of the first Run (its report cited them).
    [correction] = [
        e
        for e in _of(timeline, "human_correction")
        if e.detail.get("action") == "correct"
    ]
    concluded = [
        e
        for e in timeline.entries
        if e.run_id == chain.first_run and e.category in ("query", "evidence")
    ]
    assert concluded
    assert correction.recorded_at > max(e.recorded_at for e in concluded)
    assert correction.sequence > max(e.sequence for e in concluded)

    # Recovery observation followed the registered remediation. User
    # decision D2=a (2026-10-16): rows written in one transaction have no
    # defined order, so the session row (written with the registration) is
    # held only to "no earlier than the preceding correction"; samples and
    # the ending stay strictly later than the registration.
    [registered] = [
        e
        for e in _of(timeline, "human_correction")
        if e.detail.get("action") == "register_remediation"
    ]
    observed = _of(timeline, "recovery_observation")
    for entry in observed:
        assert entry.recorded_at >= correction.recorded_at, entry
        if entry.source in (
            "opspilot_observation_samples",
            "opspilot_observation_endings",
        ):
            assert entry.recorded_at > registered.recorded_at, entry
    assert {e.source for e in observed} >= {
        "opspilot_observation_samples",
        "opspilot_observation_endings",
    }

    # The postmortem review came after the recovery session started.
    session_start = min(
        e.recorded_at
        for e in _of(timeline, "recovery_observation")
        if e.source == "opspilot_observation_sessions"
    )
    for entry in _of(timeline, "postmortem_review"):
        assert entry.recorded_at > session_start, entry


@pg
def test_s5b_detail_carries_ids_enums_and_counts_but_no_free_text(dsn, chain):
    timeline = _timeline(dsn, chain.incident)
    everything = detail_text([e.detail for e in timeline.entries])

    for marker in (*free_text_markers(), CORRECTION_TEXT, QUESTION):
        assert marker not in everything, marker
    for entry in timeline.entries:
        for text in _strings(entry.detail):
            assert not any(ch.isspace() for ch in text), (entry.source, text)
            assert len(text) <= 256, (entry.source, text[:80])


# -- R2: partial history ------------------------------------------------------------


@pg
def test_r2_an_incident_with_only_input_and_a_run_reports_the_rest_missing(
    dsn, short_chain
):
    timeline = _timeline(dsn, short_chain.incident)

    assert set(timeline.missing) == {
        "human_correction",
        "recovery_observation",
        "postmortem_review",
    }
    for category in ("input", "query", "evidence", "state_change"):
        assert timeline.counts[category] >= 1, (category, timeline.counts)
    for category in timeline.missing:
        assert timeline.counts[category] == 0
        assert _of(timeline, category) == []


@pg
def test_r2_an_alert_only_incident_has_its_delivery_as_input(dsn, alert_incident):
    timeline = _timeline(dsn, alert_incident.incident)

    inputs = _of(timeline, "input")
    assert [e.source for e in inputs].count("opspilot_alert_deliveries") == 1
    assert {"query", "evidence", "human_correction", "recovery_observation"} <= set(
        timeline.missing
    )
    assert "postmortem_review" in timeline.missing
    assert "input" not in timeline.missing


@pg
def test_r2_the_projection_runs_in_a_read_only_transaction_and_writes_nothing(
    dsn, chain, short_chain, alert_incident
):
    before = _row_counts(dsn)
    with psycopg.connect(dsn) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        timeline = audit_timeline(conn, chain.incident)
        assert conn.execute("SHOW transaction_read_only").fetchone()[0] == "on"
        conn.rollback()
    assert timeline.entries
    assert _row_counts(dsn) == before


@pg
def test_r2_an_unknown_incident_is_a_lookup_error(dsn, chain):
    with psycopg.connect(dsn) as conn:
        with pytest.raises(LookupError):
            audit_timeline(conn, uuid4())


# -- R6: PostgreSQL time, never model or source time --------------------------------


@pg
def test_r6_every_time_is_a_postgres_write_time_inside_the_build(dsn, chain):
    """The replayed backend bytes carry September 2026 timestamps (recorded
    window ending ``WINDOW_END``); PostgreSQL wrote every row between the two
    ``clock_timestamp()`` readings that bracket the build. A source or model
    time used for ordering would fall outside the bracket."""
    assert WINDOW_END < chain.started_at - timedelta(days=1)
    timeline = _timeline(dsn, chain.incident)
    for entry in timeline.entries:
        assert chain.started_at <= entry.recorded_at <= chain.finished_at, entry


@pg
def test_r6_an_alert_starts_at_does_not_place_the_input(dsn, alert_incident):
    """``startsAt`` is the source's claim (an hour earlier); the delivery is
    placed at its PostgreSQL receipt time."""
    timeline = _timeline(dsn, alert_incident.incident)
    [delivery] = [
        e for e in _of(timeline, "input") if e.source == "opspilot_alert_deliveries"
    ]
    assert delivery.recorded_at >= alert_incident.received_after
    assert delivery.recorded_at - alert_incident.starts_at > timedelta(minutes=30)
    assert timeline.entries[0].recorded_at >= alert_incident.received_after


# -- U4-a: single-call limits bound to the one registered target (offline) -----------
# Cited, not replaced: tests/test_m1_tool_boundaries.py:158 (window), :650 and
# :666 (bytes), :277 (timeout), tests/test_m1_tool_outcomes.py:451 (view
# tokens), :73 (unregistered target).


def test_u4a_the_frozen_ceilings_are_30_seconds_and_2_mib():
    assert MAX_REQUEST_TIMEOUT_SECONDS == 30.0
    assert MAX_RESULT_BYTES == 2 * 1024 * 1024


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"request_timeout_seconds": 30.5}, "REQUEST_TIMEOUT_OUT_OF_RANGE"),
        ({"max_result_bytes": 2 * 1024 * 1024 + 1}, "RESULT_LIMIT_OUT_OF_RANGE"),
        ({"max_view_tokens": 0}, "VIEW_LIMIT_OUT_OF_RANGE"),
        ({"max_window_seconds": 0}, "WINDOW_LIMIT_OUT_OF_RANGE"),
    ],
)
def test_u4a_a_registration_beyond_a_ceiling_is_refused(overrides, code):
    with pytest.raises(ToolContractError, match=code):
        registration(**overrides)


def test_u4a_the_shipped_otel_demo_limits_bind_to_its_single_registered_target():
    # No public enumeration exists; these are what the executor factory
    # registers (``otel_demo_executor_factory``).
    from opspilot.tools import otel_demo

    target = otel_demo._target(otel_demo.OtelDemoConfig())
    targets = TargetRegistry([target])
    assert target.target_id == TARGET_ID
    assert targets.revision
    registrations = otel_demo._registrations()
    assert {r.name for r in registrations} == {
        "metrics_range_query",
        "traces_search",
    }
    for item in registrations:
        assert item.source == target.source, item.name
        assert item.request_timeout_seconds <= MAX_REQUEST_TIMEOUT_SECONDS
        assert item.max_result_bytes <= MAX_RESULT_BYTES
        assert item.max_view_tokens == otel_demo.MAX_VIEW_TOKENS == 25_000
        assert item.max_window_seconds == otel_demo.OBSERVATION_SECONDS == 24 * 3600


def test_u4a_a_window_wider_than_the_registration_is_denied_before_dispatch():
    executor, transport, _, _ = build(
        registrations=[registration(max_window_seconds=600)]
    )
    outcome = executor.execute(
        request(
            window={
                "start": WINDOW_START.isoformat(),
                "end": (WINDOW_START + timedelta(minutes=30)).isoformat(),
            }
        )
    )
    assert (outcome.status, outcome.reason) == ("denied", "WINDOW_TOO_LARGE")
    assert not transport.called


def test_u4a_a_result_over_the_byte_ceiling_is_refused_and_not_registered():
    executor, transport, sink, _ = build(
        registrations=[registration(max_result_bytes=64, max_view_tokens=32)]
    )
    transport.response = TransportResponse(
        body=body([{"series": "x" * 200, "value": 1}])
    )
    outcome = executor.execute(request())
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert outcome.evidence is None and sink.records == []
    assert transport.requests[0].max_result_bytes == 64


def test_u4a_the_registered_timeout_is_handed_to_the_transport():
    executor, transport, _, _ = build(
        registrations=[registration(request_timeout_seconds=7.0)]
    )
    transport.response = TransportResponse(body=body([]))
    executor.execute(request())
    assert transport.requests[0].timeout_seconds <= 7.0


def test_u4a_a_view_over_the_token_ceiling_is_refused():
    executor, transport, sink, _ = build(
        registrations=[registration(max_view_tokens=16)],
        token_counter=lambda text: len(text.encode()),
    )
    transport.response = TransportResponse(
        body=body([{"series": i, "value": i} for i in range(10)])
    )
    outcome = executor.execute(request())
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE")
    assert outcome.model_view["content"] is None
    assert outcome.evidence is None and sink.records == []


def test_u4a_the_limits_cannot_be_spent_on_another_target():
    executor, transport, _, _ = build()
    outcome = executor.execute(request(target_ref="payments-prod"))
    assert (outcome.status, outcome.reason) == ("denied", "TARGET_NOT_REGISTERED")
    assert not transport.called
