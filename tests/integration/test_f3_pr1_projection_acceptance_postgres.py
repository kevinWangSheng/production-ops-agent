"""Independent acceptance tests for F3 PR-1 on PostgreSQL: the read-only
investigation-evidence projection (steps 1, 2 and 4).

Written by a fresh-context author who did not implement PR-1, from the F3
contract pre-review (2026-10-17, ruled r1 by the user), section seven -- the
interface draft for ``opspilot/acceptance_investigation.py`` (R12) and
semantic points 1-5 -- plus R1 (an observation is a tool result with an
evidence record: ``ok`` and ``no_data``), R2 (explicitly unknown freshness
counts) and R3 ("exact query" = view ``query`` + ``window`` + ``lookback_*``
+ tool/version + the two registry revisions). ``feature_list.json`` F3:

1. every observation carries exact source, query, time window, freshness,
   target and resolvable evidence reference;
2. facts, hypotheses, recommendations, counter-evidence and rejected
   hypotheses remain distinguishable in the report;
4. input errors, connectivity failures, timeout and no-data results are
   distinguished.

One incident is submitted through the real web intake and investigated by the
real ``InvestigationRunner`` + ``WorkerLoop`` on a scratch database (same
wiring as ``test_f13_step4_acceptance_postgres.py``). The model is scripted;
the tool transport is a double that answers each expression with one class
(``tests/f3_pr1_support.py``), so one Run commits ok, input error,
connectivity failure, timeout and no data, and a report with one claim of
each kind. The projection is then read inside a READ ONLY transaction.

Interface assumptions beyond the pre-review's draft are marked
``ASSUMED`` -- the draft names the entry types and their fields but not the
container's attribute names or how a report's claims are grouped. They are
kept in the accessor helpers below so they are in one place.

NOT RUN by the author (PG; the task forbade starting PostgreSQL). Opt-in:
``M1_DURABLE_POSTGRES=1``.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator, Mapping
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from opspilot import schema
from opspilot.acceptance_audit import audit_timeline
from opspilot.investigation.runner import InvestigationRunner
from opspilot.persistence import DurableStore, PoolConfig
from opspilot.tools import (
    QueryScope,
    ReadOnlyToolExecutor,
    TargetRegistry,
    ToolRegistry,
)
from opspilot.tools.executor import MAX_OPERATIONS_PER_RUN, MAX_TOOL_SECONDS_PER_RUN
from opspilot.tools.fixture import (
    FIXTURE_TARGET,
    FIXTURE_TOOL,
    WINDOW_END,
    WINDOW_START,
    fixture_face,
    fixture_versions,
)
from opspilot.tools.ledger import DurableToolLedger
from opspilot.tools.outcomes import Window
from opspilot.web import (
    AuthConfig,
    Authenticator,
    DurableClock,
    DurableEventLog,
    DurableEvidenceStore,
    DurableIncidentStore,
    DurableWebLedger,
    Workbench,
    create_app,
    hash_password,
    token_digest,
)
from opspilot.worker import Worker
from opspilot.worker_main import WorkerLoop
from scripts.m0.postgres_lab import DSN
from tests.f3_pr1_support import (
    CLAIM_TEXT,
    EVIDENCE_KINDS,
    EXPECTED,
    INVALID_EXPR,
    NO_DATA_EXPR,
    OK_EXPR,
    TIMEOUT_EXPR,
    UNCITED_KINDS,
    UNREACHABLE_EXPR,
    ByExpressionTransport,
    five_class_tool_calls,
    five_kind_report,
    ok_evidence_id,
)
from tests.m1_investigation_support import ScriptedModel, reply
from tests.m1_tool_support import FixedControl, registration, target
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
    call,
    post_form,
    same_origin,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
POOL = PoolConfig(min_size=0, max_size=4, timeout=5.0)
UI = {**basic(), **same_origin()}
KINDS = (
    "fact",
    "hypothesis",
    "recommendation",
    "counter_evidence",
    "rejected_hypothesis",
)


# --- ASSUMED projection shape (pre-review section seven, R12) ---------------------
#
# Draft: ``investigation_evidence(conn, incident_id) -> InvestigationEvidence``,
# SELECT only, runnable in a READ ONLY transaction, ``LookupError`` for an
# unknown incident (as ``audit_timeline``). Entry types and their fields are
# the draft's; the container attributes and claim grouping are ASSUMED:
#
# * ``InvestigationEvidence.observations``: ``ObservationEntry`` per R1;
# * ``InvestigationEvidence.refused_operations``: ``RefusedOperation`` for
#   every error / timeout / denied tool result;
# * ``InvestigationEvidence.reports``: one ``ReportClaims`` per published or
#   handed-off report, with ``run_id`` and ``claims`` -- a mapping from each of
#   the five claim kinds to that kind's claims; each claim exposes ``text`` and
#   ``evidence`` -- one entry per cited evidence id with ``evidence_id``,
#   ``resolvable``, ``ok`` and ``citable_as_fact`` (draft: "每条 claim 的
#   evidence_ids 逐个标注能否解析、是否 ok 且 citable_as_fact").


def _observations(projection) -> tuple:
    return tuple(projection.observations)


def _refused(projection) -> tuple:
    return tuple(projection.refused_operations)


def _reports(projection) -> tuple:
    return tuple(projection.reports)


def _claims(report) -> Mapping[str, tuple]:
    return {kind: tuple(items) for kind, items in report.claims.items()}


def _instant(value: Any) -> datetime:
    """Draft does not fix datetime vs ISO text; accept both (ASSUMED)."""
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


# --- database and stack ---------------------------------------------------------------


@pytest.fixture(scope="module")
def dsn() -> Iterator[str]:
    name = f"opspilot_f3pr1_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    scratch = make_conninfo(DSN, dbname=name)
    schema.migrate(scratch, pg_dump=PG_DUMP)
    try:
        yield scratch
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


class _Scoped:
    """Queue and sweeper limited to the incidents a test names."""

    def __init__(self, store: DurableStore, incidents) -> None:
        self._store = store
        self._ids = tuple(incidents)

    def claimable_incidents(self, *, limit: int = 20):
        listed = self._store.claimable_incidents(limit=10_000)
        return tuple(i for i in listed if i in self._ids)[:limit]

    def sweep_expired_runs(self, *, incident_id=None, limit: int = 100):
        parked: list = []
        for subject in self._ids:
            if incident_id is None or incident_id == subject:
                parked.extend(
                    self._store.sweep_expired_runs(incident_id=subject, limit=limit)
                )
        return tuple(parked)

    def sweep_expired_runs_with_generations(
        self, *, incident_id=None, limit: int = 100
    ):
        parked: list = []
        for subject in self._ids:
            if incident_id is None or incident_id == subject:
                parked.extend(
                    self._store.sweep_expired_runs_with_generations(
                        incident_id=subject, limit=limit
                    )
                )
        return tuple(parked)


@pytest.fixture(scope="module")
def env(dsn: str) -> Iterator[dict[str, Any]]:
    durable = DurableStore(dsn, pool=POOL)
    durable.install()
    events = DurableEventLog(durable)
    events.install()
    evidence = DurableEvidenceStore(durable)
    evidence.install()
    ledger = DurableWebLedger(durable)
    ledger.install()
    workbench = Workbench(
        incidents=DurableIncidentStore(durable),
        events=events,
        evidence=evidence,
        ledger=ledger,
        run_versions=fixture_versions(),
        run_seconds=600,
        tool_face=fixture_face(),
    )
    config = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager-prod"},
        auth_revision="auth-rev-f3pr1",
        allowed_origins=frozenset({ORIGIN}),
    )
    app = create_app(
        workbench,
        Authenticator(config),
        DurableClock(durable),
        sse_poll_seconds=0.02,
        sse_idle_seconds=0.2,
    )
    yield {
        "app": app,
        "store": durable,
        "events": events,
        "evidence": evidence,
        "dsn": dsn,
        "workbench": workbench,
    }
    durable.close()


def _five_class_factory(store: DurableStore, *, evidence, clock):
    """The fixture profile's executor wiring (durable ledger, durable evidence
    sink, scope from the committed Run row) with the per-expression transport
    double in place of the canned one."""
    tools = ToolRegistry([registration(name=FIXTURE_TOOL, max_view_tokens=25_000)])
    targets = TargetRegistry([target(target_id=FIXTURE_TARGET)])

    def factory(lease, input):
        run = store.rebuild(lease.incident_id)["run"]
        scope = QueryScope(
            scope_id=f"f3pr1:{lease.run_id}:{lease.epoch}",
            subject_kind="incident",
            subject_id=str(lease.incident_id),
            run_id=str(lease.run_id),
            control_generation=lease.control_generation,
            global_suspension_generation=lease.global_suspension_generation,
            target_suspension_generation=lease.target_suspension_generation,
            registry_revision=targets.revision,
            tool_registry_revision=tools.revision,
            target_ids=frozenset({FIXTURE_TARGET}),
            tool_names=frozenset({FIXTURE_TOOL}),
            window=Window(WINDOW_START, WINDOW_END),
            deadline=run["deadline"],
        )
        return ReadOnlyToolExecutor(
            scope=scope,
            tools=tools,
            targets=targets,
            transport=ByExpressionTransport(),
            evidence=evidence,
            control=FixedControl(
                generation=lease.control_generation,
                global_generation=lease.global_suspension_generation,
                target_generation=lease.target_suspension_generation,
            ),
            clock=clock,
            ledger=DurableToolLedger(
                store,
                lease,
                max_operations=MAX_OPERATIONS_PER_RUN,
                max_tool_seconds=MAX_TOOL_SECONDS_PER_RUN,
            ),
        )

    return factory


def _report_round(request):
    return reply(
        content=five_kind_report(
            ok_evidence_id(request.messages), target=FIXTURE_TARGET
        )
    )


@pytest.fixture(scope="module")
def investigated(env) -> dict[str, Any]:
    response = post_form(
        env["app"],
        "/intake/ui",
        {
            "target_id": FIXTURE_TARGET,
            "question": "Why is checkout erroring?",
            "idempotency_key": f"f3pr1-{uuid4()}",
        },
        headers=UI,
    )
    assert response.status == 201, response.text
    incident_id = UUID(response.json()["incident_id"])
    run_id = UUID(response.json()["run_id"])

    store: DurableStore = env["store"]
    clock = DurableClock(store)
    model = ScriptedModel(
        [
            reply(tool_calls=five_class_tool_calls(FIXTURE_TOOL), finish="tool_calls"),
            _report_round,
        ]
    )
    runner = InvestigationRunner(
        store=store,
        worker=Worker.create(store, dict(fixture_versions())),
        model=model,
        executor_factory=_five_class_factory(
            store, evidence=env["evidence"], clock=clock
        ),
        clock=clock,
        lease_seconds=60,
        events=env["events"],
        evidence=env["evidence"],
    )
    scoped = _Scoped(store, (incident_id,))
    loop = WorkerLoop(
        queue=scoped,
        runner=runner,
        sweeper=scoped,
        events=env["events"],
        stop=threading.Event(),
        poll_seconds=0.05,
        batch=20,
    )
    results = loop.poll_once()
    # The scripted report must be published, or the claims below are not
    # about an accepted report.
    assert [(i, o.status) for i, o in results] == [(incident_id, "published")]

    from opspilot.acceptance_investigation import investigation_evidence

    with psycopg.connect(env["dsn"], row_factory=dict_row) as conn:
        conn.read_only = True
        projection = investigation_evidence(conn, incident_id)
        timeline = audit_timeline(conn, incident_id)
        conn.rollback()
    return {
        "incident_id": incident_id,
        "run_id": run_id,
        "projection": projection,
        "timeline": timeline,
    }


def _by_expr(entries, query_of) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for entry in entries:
        out[query_of(entry)] = entry
    return out


# --- step 1: every observation is fully attributed and resolvable -------------------


def test_observations_are_exactly_the_ok_and_no_data_results(investigated):
    # R1: refusals, errors and timeouts are not observations.
    observations = _observations(investigated["projection"])
    assert {o.status for o in observations} == {"ok", "no_data"}
    exprs = sorted(o.query["expr"] for o in observations)
    assert exprs == sorted([OK_EXPR, NO_DATA_EXPR])


def test_every_observation_carries_source_query_window_freshness_target_reference(
    investigated,
):
    run_id = str(investigated["run_id"])
    for o in _observations(investigated["projection"]):
        label = o.query.get("expr")
        # Point 1: any of these empty fails step 1.
        assert o.source, label
        assert o.target_id == FIXTURE_TARGET, label
        assert o.evidence_id, label
        assert o.query and o.query["expr"], label
        window = o.window
        assert window["start"] and window["end"], label
        assert _instant(window["start"]) < _instant(window["end"]), label
        assert o.tool == FIXTURE_TOOL and o.tool_version, label
        assert o.observed_at and o.projection_revision, label
        assert str(o.run_id) == run_id, label
        # Old projection revisions list what they lack; a fresh Run lacks
        # nothing (pre-review section seven, "旧数据").
        assert tuple(o.missing_fields) == (), (label, o.missing_fields)


def test_freshness_is_measured_for_ok_and_explicitly_unknown_for_no_data(
    investigated,
):
    by_expr = _by_expr(
        _observations(investigated["projection"]), lambda o: o.query["expr"]
    )
    ok = by_expr[OK_EXPR]
    assert ok.data_as_of is not None
    expected = (_instant(ok.observed_at) - _instant(ok.data_as_of)).total_seconds()
    assert ok.freshness_seconds == pytest.approx(expected)
    assert ok.freshness_seconds >= 0
    # R2 / point 1: no_data with no data_as_of is unknown freshness -- None,
    # not zero, and not a missing field.
    empty = by_expr[NO_DATA_EXPR]
    assert empty.data_as_of is None and empty.freshness_seconds is None
    assert "freshness_seconds" not in tuple(empty.missing_fields)


def test_every_observation_resolves_with_verified_hashes_and_matches_the_page(
    env, investigated
):
    # Point 2: resolvable = read back through the workbench's own path,
    # hashes verified, same run and subject.
    incident = str(investigated["incident_id"])
    for o in _observations(investigated["projection"]):
        assert o.resolvable is True, o.evidence_id
        record = call(
            env["app"],
            "GET",
            f"/incidents/{incident}/evidence/{o.evidence_id}",
            headers=basic(),
        )
        assert record.status == 200
        body = record.json()
        assert body["hashes_verified"] is True
        view = body["view"]
        # The projection is a reading of the stored view, not a second truth.
        assert view["query"] == dict(o.query)
        assert view["window"] == dict(o.window)
        assert view["source"] == o.source and view["target_id"] == o.target_id
        assert view["freshness_seconds"] == o.freshness_seconds


def test_tampered_raw_bytes_make_an_observation_unresolvable(env, investigated):
    # Point 2, negative: a reference whose bytes no longer hash to the
    # recorded digest is not "resolvable". Done in a transaction that is
    # rolled back, so the shared incident is untouched for the other tests.
    from opspilot.acceptance_investigation import investigation_evidence

    by_expr = _by_expr(
        _observations(investigated["projection"]), lambda o: o.query["expr"]
    )
    victim = by_expr[OK_EXPR].evidence_id
    with psycopg.connect(env["dsn"], row_factory=dict_row) as conn:
        conn.execute(
            "UPDATE opspilot_evidence SET raw=%s WHERE evidence_id=%s",
            (b'{"data":{"result":[{"metric":"forged","value":9}]}}', victim),
        )
        tampered = _by_expr(
            _observations(investigation_evidence(conn, investigated["incident_id"])),
            lambda o: o.query["expr"],
        )
        conn.rollback()
    assert tampered[OK_EXPR].resolvable is False
    assert tampered[NO_DATA_EXPR].resolvable is True


def test_unknown_incident_is_a_lookup_error(env):
    from opspilot.acceptance_investigation import investigation_evidence

    with psycopg.connect(env["dsn"], row_factory=dict_row) as conn:
        conn.read_only = True
        with pytest.raises(LookupError):
            investigation_evidence(conn, uuid4())
        conn.rollback()


# --- step 2: the five claim kinds stay apart ---------------------------------------


def test_the_published_report_keeps_the_five_claim_kinds_apart(investigated):
    reports = _reports(investigated["projection"])
    assert len(reports) == 1
    claims = _claims(reports[0])
    assert str(reports[0].run_id) == str(investigated["run_id"])
    assert set(claims) == set(KINDS)
    for kind in KINDS:
        texts = [c.text for c in claims[kind]]
        assert texts == [CLAIM_TEXT[kind]], (kind, texts)
    # Point 3: hypothesis / recommendation never appear in the fact group.
    fact_texts = {c.text for c in claims["fact"]}
    for kind in UNCITED_KINDS:
        assert CLAIM_TEXT[kind] not in fact_texts


def test_evidence_bearing_claims_cite_resolvable_ok_citable_observations(
    investigated,
):
    claims = _claims(_reports(investigated["projection"])[0])
    ok_id = _by_expr(
        _observations(investigated["projection"]), lambda o: o.query["expr"]
    )[OK_EXPR].evidence_id
    for kind in EVIDENCE_KINDS:
        (claim,) = claims[kind]
        refs = tuple(claim.evidence)
        assert [r.evidence_id for r in refs] == [ok_id], kind
        for ref in refs:
            assert ref.resolvable is True and ref.ok is True, kind
            assert ref.citable_as_fact is True, kind
    for kind in UNCITED_KINDS:
        (claim,) = claims[kind]
        assert tuple(claim.evidence) == (), kind


# --- step 4: the four classes in the acceptance projection and the audit -----------


def test_refused_operations_keep_input_connectivity_and_timeout_apart(investigated):
    refused = _refused(investigated["projection"])
    run_id = str(investigated["run_id"])
    assert len(refused) == 3, refused
    for r in refused:
        assert str(r.run_id) == run_id
        assert r.tool == FIXTURE_TOOL and r.target_id == FIXTURE_TARGET
        assert r.operation_id
    got = sorted((r.status, r.reason) for r in refused)
    assert got == sorted(
        EXPECTED[e] for e in (INVALID_EXPR, UNREACHABLE_EXPR, TIMEOUT_EXPR)
    )
    # Source contact is reported per class, never blank.
    by_reason = {r.reason: r for r in refused}
    assert by_reason["INVALID_PARAMS"].source_contact == "confirmed"
    assert by_reason["SOURCE_UNAVAILABLE"].source_contact == "possible"
    assert by_reason["TOOL_TIMEOUT"].source_contact == "possible"


def test_no_data_is_an_observation_not_a_refusal(investigated):
    projection = investigated["projection"]
    assert all(r.reason != "NO_DATA" for r in _refused(projection))
    statuses = {o.query["expr"]: o.status for o in _observations(projection)}
    assert statuses[NO_DATA_EXPR] == "no_data"


def test_the_audit_query_entries_distinguish_the_four_classes(investigated):
    # Point 5: the audit timeline's query entries are the third surface.
    run_id = investigated["run_id"]
    entries = [
        e
        for e in investigated["timeline"].entries
        if e.category == "query" and e.run_id == run_id
    ]
    codes = {(e.detail.get("result_code"), e.detail.get("reason")) for e in entries}
    dump = json.dumps([dict(e.detail) for e in entries], default=str)
    assert len(entries) == 5 and len(codes) == 5, dump
    assert {
        ("error", "INVALID_PARAMS"),
        ("error", "SOURCE_UNAVAILABLE"),
        ("timeout", "TOOL_TIMEOUT"),
    } <= codes, dump
    # 未确认: whether the audit carries NO_DATA as the reason of a no_data
    # result; its result code alone already sets it apart.
    assert {c for c in codes if c[0] == "no_data"} <= {
        ("no_data", "NO_DATA"),
        ("no_data", None),
    }, dump
    assert any(c[0] == "no_data" for c in codes) and ("ok", None) in codes, dump
