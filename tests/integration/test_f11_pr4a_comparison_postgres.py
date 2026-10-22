"""Independent PostgreSQL acceptance tests for F11 PR-4a (release comparison
collection).

Written from ``docs/tasks/2026-10-21-f11-pr4-contract.md`` (P4-01=A, R12 split
4a/4b, 第 4 节, 第 6 节第 11 项, r2 补充 S1-S9) and the PR-4a probe evidence,
without reading the implementation. Prometheus is a fake answering by PromQL
shape (``tests.f11_comparison_support.ComparisonWorld``); everything else is
the real store under the real runtime roles. Guesses about table and column
names live in ``tests/f11_comparison_support.py``.

The Observer is driven through its public tick, ``ObserverLoop.poll_once``.
4a collects only: no incident, Run or verdict change (the consumer is 4b).
"""

# ruff: noqa: F811 - pytest fixtures imported from the PR-2a module
from __future__ import annotations

import functools
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from opspilot.acceptance_recovery import OBSERVER_GRANTS
from opspilot.observation import ObservationStore
from opspilot.observation.releases import (
    ReleaseAuxiliary,
    ReleaseEvent,
    ReleaseRevision,
    ReleaseStore,
)
from opspilot.observer.loop import ObserverLoop
from opspilot.observer.release_replay import replay_release_history
from opspilot.persistence import DurableStore
from opspilot.persistence.grants import RUNTIME_GRANTS, WEB_ROLE, WORKER_ROLE
from scripts.m0.postgres_lab import DSN
from tests.f11_comparison_support import (
    BEFORE_WINDOW,
    OLD_RS,
    OLD_SLICE_VALUES,
    QUERY_TIMEOUT,
    SERVICE_VALUES,
    SLICE_SIGNAL_NAMES,
    ComparisonWorld,
    all_reasons,
    bounds,
    by_scope,
    comparison_id,
    comparison_tables,
    evaluate,
    has_reason,
    to_result,
    valued,
)
from tests.f11_release_support import (
    AFTER_HASH,
    BEFORE_HASH,
    PROFILE_CONTENT,
    PROFILE_REVISION,
    EvidenceBuilder,
    digest,
    planned_texts,
    pods_for,
)
from tests.integration.test_f11_release_observation_postgres import (  # noqa: F401 - fixtures
    AFTER_IMAGES,
    BEFORE_IMAGES,
    MIN_TRACKING,
    OBSERVER_ROLE,
    QUERY_RESULTS,
    Catalog,
    Release,
    _authorize,
    _claim,
    _db_now,
    _ending_id,
    _endings,
    _entry,
    _exec,
    _make_due,
    _rows,
    _sample,
    _set_authorized_at,
    _state,
    _target,
    catalog,
    history,
    observer,
    observer_dsn,
    owner,
    reader,
    web,
    web_dsn,
    worker_dsn,
)
from tests.runtime_roles_support import catalog_grants

try:  # PR-4a public names (contract 第 4 节)
    from opspilot.observation.release_comparison import (
        build_release_comparison_plan,
        evaluate_release_comparison,
    )

    _PR4A_MISSING: str | None = None
except ImportError as exc:  # implementation not merged yet: fail, do not error
    _PR4A_MISSING = f"{type(exc).__name__}: {exc}"

pytestmark = [
    pytest.mark.skipif(
        __import__("os").environ.get("M1_DURABLE_POSTGRES") != "1",
        reason="explicit PG opt-in required",
    ),
]


# ===========================================================================
# helpers
# ===========================================================================


def _record(
    owner: DurableStore,
    web: ReleaseStore,
    *,
    before_hash: str | None = BEFORE_HASH,
    after_hash: str | None = AFTER_HASH,
    before_images: dict[str, str] = BEFORE_IMAGES,
    released_ago: float = 1000.0,
) -> Release:
    target_id, identity = _target(owner)
    event = ReleaseEvent(
        format_version=1,
        source="argo-rollouts",
        source_release_id=f"rel-{uuid4().hex}",
        released_at=_db_now() - timedelta(seconds=released_ago),
        before=ReleaseRevision(
            images=dict(before_images), pod_template_digest=digest("tmpl-before")
        ),
        after=ReleaseRevision(
            images=dict(AFTER_IMAGES), pod_template_digest=digest("tmpl-after")
        ),
        auxiliary=ReleaseAuxiliary(
            before_pod_template_hash=before_hash, after_pod_template_hash=after_hash
        ),
    )
    receipt = web.record_release(
        event=event,
        target_id=target_id,
        identity=identity,
        health_profile_revision=PROFILE_REVISION,
        health_profile=PROFILE_CONTENT,
        min_tracking_seconds=MIN_TRACKING,
        actor="release-source:test",
        raw_body=event.model_dump_json().encode(),
    )
    assert (receipt.outcome, receipt.status) == ("created", "pending"), receipt
    assert receipt.observation_id is not None
    return Release(web.get_release(receipt.observation_id), identity)


@dataclass
class Anomaly:
    oid: UUID
    session_id: UUID
    ending_id: UUID
    sample_id: UUID
    released_at: datetime
    window_end: datetime
    context: Any

    @property
    def cid(self) -> UUID:
        return comparison_id(self.oid, self.ending_id)


def _anomalous(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    **release: Any,
) -> Anomaly:
    """A release that ended ``anomalous`` through one degraded PR-2 sample."""
    recorded = _record(owner, web, **release)
    entry = _entry(recorded)
    catalog.add(entry)
    receipt = _authorize(web, recorded.oid, entry["revision"])
    assert receipt.outcome == "applied", receipt
    sid = receipt.session_id
    assert sid is not None
    released_at = recorded.record.released_at
    authorized_at = released_at + timedelta(seconds=100)
    _set_authorized_at(sid, authorized_at)
    window_end = authorized_at + BEFORE_WINDOW
    lease = _claim(observer, sid)
    context = observer.release_context(sid)
    pods = pods_for(AFTER_IMAGES)
    texts = planned_texts(context, window_end, [p.uid for p in pods])
    evidence = (
        EvidenceBuilder(window_end=window_end, texts=texts)
        .sufficient(pods, after={"error_ratio": 0.5})
        .evidence()
    )
    sample = observer.submit_release_sample(lease, evidence)
    assert sample.release_status == "anomalous", sample
    return Anomaly(
        oid=recorded.oid,
        session_id=sid,
        ending_id=_ending_id(recorded.oid, sid),
        sample_id=evidence.sample_id,
        released_at=released_at,
        window_end=window_end,
        context=context,
    )


def _tick(observer: ObservationStore, world: Any, batch: int = 50) -> None:
    ObserverLoop(store=observer, source=world, batch=batch).poll_once()


def _drain(observer: ObservationStore) -> None:
    """Consume whatever comparison another test left pending."""
    neutral = ComparisonWorld(old_pods=())
    for _ in range(12):
        if not observer.pending_comparisons(limit=50):
            return
        _tick(observer, neutral)
    pytest.fail("pending comparisons could not be drained", pytrace=False)


def _tables() -> dict[str, set[str]]:
    tables = comparison_tables(_rows)
    assert tables, "no comparison table found (name containing 'comparison')"
    return tables


def _stored(table: str, cid: UUID) -> list[dict[str, Any]]:
    return _rows(f"SELECT * FROM {table} WHERE comparison_id=%s", (cid,))


def _evidence(cid: UUID) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for table, columns in _tables().items():
        if {"raw", "raw_sha256", "comparison_id"} <= columns:
            out += _stored(table, cid)
    return out


def _comparison(a: Anomaly) -> Any:
    """The stored comparison evaluated the way replay does (S8): the stored
    comparison evidence plus the anomaly sample's own rows (after scopes are
    taken from the sample, not re-queried), through the public pure
    evaluator and the public plan builder."""
    results = {
        r["query_key"]: to_result(r)
        for r in _rows(
            f"SELECT * FROM {QUERY_RESULTS} WHERE sample_id=%s", (a.sample_id,)
        )
    }
    stored = _evidence(a.cid)
    assert stored, "no comparison evidence stored for this ending"
    results.update({r["query_key"]: to_result(r) for r in stored})
    plan = build_release_comparison_plan(
        a.context, released_at=a.released_at, after_window_end=a.window_end
    )
    return evaluate(evaluate_release_comparison, a.context, plan, results)


def _world(old_pods: Any = None, **kw: Any) -> ComparisonWorld:
    pods = (
        pods_for(BEFORE_IMAGES, 1, replicaset=OLD_RS) if old_pods is None else old_pods
    )
    return ComparisonWorld(old_pods=pods, **kw)


def _collect(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    world: ComparisonWorld | None = None,
    *,
    ticks: int = 1,
    **release: Any,
) -> tuple[Anomaly, ComparisonWorld]:
    _drain(observer)
    anomaly = _anomalous(owner, web, observer, catalog, **release)
    world = world or _world()
    for _ in range(ticks):
        _tick(observer, world)
    return anomaly, world


def _counts() -> dict[str, int]:
    return {
        table: _rows(f"SELECT count(*) AS n FROM {table}")[0]["n"]
        for table in (
            "opspilot_incidents",
            "opspilot_runs",
            "opspilot_observation_samples",
            "opspilot_observation_endings",
            "opspilot_observation_sessions",
        )
    }


# ===========================================================================
# 1. one bounded comparison collection after an anomaly ending
# ===========================================================================


def test_an_anomaly_ending_yields_exactly_one_pending_comparison_then_none(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    _drain(observer)
    before = len(observer.pending_comparisons(limit=50))
    _anomalous(owner, web, observer, catalog)
    assert len(observer.pending_comparisons(limit=50)) == before + 1

    _tick(observer, _world())

    assert len(observer.pending_comparisons(limit=50)) == before


def test_the_comparison_has_four_scopes_and_the_contract_windows(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    anomaly, world = _collect(owner, web, observer, catalog)

    comparison = _comparison(anomaly)
    assert comparison.status == "complete", all_reasons(comparison)
    scopes = by_scope(comparison)
    assert set(scopes) == {"before_service", "before_old", "after_service", "after_new"}
    # S1: before window = [released_at - profile window, released_at]
    start, end = bounds(comparison.window_before)
    assert (start, end) == (anomaly.released_at - BEFORE_WINDOW, anomaly.released_at)
    # the after window is the anomaly sample's own window
    assert bounds(comparison.window_after)[1] == anomaly.window_end
    # the slice is the old instances' reading, not the whole service's (S2)
    old_values = {s.value for s in valued(scopes["before_old"])}
    service_values = {s.value for s in valued(scopes["before_service"])}
    assert OLD_SLICE_VALUES["request_rate_per_second"] in old_values
    assert SERVICE_VALUES["request_rate_per_second"] in service_values
    assert OLD_SLICE_VALUES["request_rate_per_second"] not in service_values
    assert len(valued(scopes["before_old"])) == len(SLICE_SIGNAL_NAMES)
    # the new slice carries the anomaly sample's degraded reading
    assert valued(scopes["after_service"])
    assert 0.5 in {s.value for s in valued(scopes["after_new"])}
    assert world.unclassified == []


def test_s1_every_before_query_is_evaluated_at_released_at_and_stored_that_way(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    anomaly, world = _collect(owner, web, observer, catalog)

    assert world.calls, "the Observer issued no comparison query"
    # not the collection instant, and no fallback to an earlier time
    assert world.times() == {anomaly.released_at}
    # the after scopes come from the anomaly sample: not queried again
    assert world.new_world_queries == []
    stored = _evidence(anomaly.cid)
    assert stored
    assert {r["evaluated_at"] for r in stored} <= {
        anomaly.released_at,
        anomaly.window_end,
    }
    assert anomaly.released_at in {r["evaluated_at"] for r in stored}


def test_the_old_slice_uses_the_before_replicaset_and_the_old_instances(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    old = pods_for(BEFORE_IMAGES, 2, replicaset=OLD_RS)
    anomaly, world = _collect(owner, web, observer, catalog, _world(old))

    texts = [expr for expr, _, _ in world.calls]
    assert any(f'"{OLD_RS}"' in t for t in texts)  # RS = <workload>-<before hash>
    sliced = [t for t in texts if "service_instance_id" in t]
    assert sliced
    for pod in old:
        assert any(json.dumps(re.escape(str(pod.uid)))[1:-1] in t for t in sliced)
    comparison = _comparison(anomaly)
    assert comparison.status == "complete", all_reasons(comparison)


def test_a_second_tick_does_not_collect_again(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    anomaly, world = _collect(owner, web, observer, catalog)
    issued = len(world.calls)
    rows = len(_evidence(anomaly.cid))
    assert issued and rows

    _tick(observer, world)

    assert len(world.calls) == issued
    assert len(_evidence(anomaly.cid)) == rows


def test_collection_does_not_create_incidents_runs_or_change_the_release(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    reader: ReleaseStore,
    catalog: Catalog,
) -> None:
    _drain(observer)
    anomaly = _anomalous(owner, web, observer, catalog)
    before_counts = _counts()
    before_state = _state(reader.get_release(anomaly.oid))
    before_endings = _endings(anomaly.oid)

    _tick(observer, _world())

    assert _evidence(anomaly.cid)
    record = reader.get_release(anomaly.oid)
    assert (record.status, record.incident_id) == ("anomalous", None)
    assert _state(record) == before_state
    assert _endings(anomaly.oid) == before_endings
    assert _counts() == before_counts


# ===========================================================================
# 2. S2 / S3: the old slice is known or unknown, never guessed
# ===========================================================================


def test_s2_old_instances_must_run_exactly_the_before_images(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    wrong = pods_for(AFTER_IMAGES, 1, replicaset=OLD_RS)  # not before.images
    anomaly, _ = _collect(owner, web, observer, catalog, _world(wrong))

    comparison = _comparison(anomaly)
    scopes = by_scope(comparison)
    assert comparison.status == "partial"
    assert has_reason(comparison, "VERSION_IMAGE_MISMATCH")
    assert valued(scopes.get("before_old", [])) == []
    assert valued(scopes["before_service"])  # the whole-service reading stays


def test_s3_missing_before_hash_is_unknown_and_keeps_the_service_reading(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    anomaly, world = _collect(owner, web, observer, catalog, before_hash=None)

    comparison = _comparison(anomaly)
    scopes = by_scope(comparison)
    assert comparison.status == "partial"
    assert has_reason(comparison, "VERSION_IDENTITY_MISSING")
    assert valued(scopes.get("before_old", [])) == []
    assert valued(scopes["before_service"])
    # nothing is asked about a ReplicaSet that cannot be named
    assert not any("kube_replicaset_owner" in e for e, _, _ in world.calls)


def test_s3_equal_before_and_after_hash_is_not_distinguishable(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    anomaly, _ = _collect(owner, web, observer, catalog, before_hash=AFTER_HASH)

    comparison = _comparison(anomaly)
    scopes = by_scope(comparison)
    assert comparison.status == "partial"
    assert has_reason(comparison, "VERSION_SLICE_NOT_DISTINGUISHABLE")
    assert valued(scopes.get("before_old", [])) == []
    assert valued(scopes["before_service"])


def test_s3_equal_images_with_different_hashes_are_still_distinguishable(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    """A config rollout keeps the image digest; the ReplicaSet tells the
    versions apart, so identical images are not 'not distinguishable'."""
    old = pods_for(AFTER_IMAGES, 1, replicaset=OLD_RS)
    anomaly, _ = _collect(
        owner, web, observer, catalog, _world(old), before_images=AFTER_IMAGES
    )

    comparison = _comparison(anomaly)
    assert comparison.status == "complete", all_reasons(comparison)
    assert not has_reason(comparison, "VERSION_SLICE_NOT_DISTINGUISHABLE")
    assert valued(by_scope(comparison)["before_old"])


def test_s3_a_failed_mapping_leaves_no_slice_value_but_the_service_reading(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    anomaly, _ = _collect(owner, web, observer, catalog, _world(mapping_ok=False))

    comparison = _comparison(anomaly)
    scopes = by_scope(comparison)
    assert comparison.status == "partial"
    assert any(r.startswith("VERSION_") for r in all_reasons(comparison))
    assert valued(scopes.get("before_old", [])) == []
    assert valued(scopes["before_service"])


# ===========================================================================
# 3. S4: the instance set must equal the mapped set exactly
# ===========================================================================


@pytest.mark.parametrize("case", ["fewer", "more", "foreign"])
def test_s4_instances_must_equal_the_mapped_set(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    case: str,
) -> None:
    old = pods_for(BEFORE_IMAGES, 2, replicaset=OLD_RS)
    uids = {
        "fewer": [old[0].uid],
        "more": [old[0].uid, old[1].uid, uuid4()],
        "foreign": [old[0].uid, uuid4()],
    }[case]
    anomaly, _ = _collect(
        owner, web, observer, catalog, _world(old, instance_uids=uids)
    )

    comparison = _comparison(anomaly)
    scopes = by_scope(comparison)
    assert comparison.status == "partial"
    assert has_reason(comparison, "VERSION_INSTANCE_MISMATCH")
    assert valued(scopes.get("before_old", [])) == []
    assert valued(scopes["before_service"])


# ===========================================================================
# 4. S5: released_at later than the rollout is unknown, not retried earlier
# ===========================================================================


def test_s5_old_pods_already_gone_at_released_at_stay_unknown_without_fallback(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    _drain(observer)
    anomaly = _anomalous(owner, web, observer, catalog)
    # KSM shows the old pods only strictly before released_at
    world = _world(visible_before=anomaly.released_at)

    _tick(observer, world)

    comparison = _comparison(anomaly)
    scopes = by_scope(comparison)
    assert comparison.status == "partial"
    assert has_reason(comparison, "VERSION_MAPPING_MISSING")
    assert valued(scopes.get("before_old", [])) == []
    assert valued(scopes["before_service"])
    assert world.times() == {anomaly.released_at}, "no earlier-time fallback"


# ===========================================================================
# 5. S9: too few samples
# ===========================================================================


@pytest.mark.parametrize("scope", ["before_old", "before_service"])
def test_s9_too_few_samples_name_the_scope_and_signal(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    scope: str,
) -> None:
    world = _world(
        slice_coverage=1 if scope == "before_old" else 20,
        service_coverage=1 if scope == "before_service" else 20,
    )
    anomaly, _ = _collect(owner, web, observer, catalog, world)

    comparison = _comparison(anomaly)
    assert comparison.status == "partial"
    pattern = re.compile(rf"^INSUFFICIENT_SAMPLES:{scope}:[a-z0-9_]+$")
    assert any(pattern.match(r) for r in all_reasons(comparison)), all_reasons(
        comparison
    )


# ===========================================================================
# 6. S6: bounded attempts and queries
# ===========================================================================


def test_s6_failing_prometheus_is_bounded_and_ends_unavailable(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    _drain(observer)
    anomaly = _anomalous(owner, web, observer, catalog)
    world = _world(fail_all=True)

    for _ in range(10):
        _tick(observer, world)
        if not observer.pending_comparisons(limit=50):
            break
        time.sleep(0.2)

    # at most 3 attempts of one plan each (contract S6), each query within
    # the profile's per-query timeout
    assert 0 < len(world.calls) <= 3 * 49
    assert all(timeout <= QUERY_TIMEOUT for _, _, timeout in world.calls)
    # never permanently pending, and the outcome is recorded
    assert observer.pending_comparisons(limit=50) == ()
    statuses = {
        row["status"]
        for table, columns in _tables().items()
        if {"status", "comparison_id"} <= columns and "raw_sha256" not in columns
        for row in _stored(table, anomaly.cid)
    }
    assert statuses == {"unavailable"}, statuses


def test_s6_one_attempt_issues_no_more_queries_than_the_plan(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    anomaly, world = _collect(owner, web, observer, catalog)

    assert 0 < len(world.calls) <= 49  # contract S6: one attempt
    assert all(timeout <= QUERY_TIMEOUT for _, _, timeout in world.calls)


# ===========================================================================
# 7. permissions: Observer INSERT, web SELECT, worker nothing
# ===========================================================================


def _grants(role: str) -> dict[str, frozenset[str]]:
    with psycopg.connect(DSN) as conn:
        return catalog_grants(conn, role)


def test_comparison_tables_grant_observer_insert_web_select_worker_nothing() -> None:
    tables = _tables()
    observer_grants = _grants(OBSERVER_ROLE)
    web_grants = _grants(WEB_ROLE)
    worker_grants = _grants(WORKER_ROLE)
    for table in tables:
        mine = observer_grants[table]
        assert any(g == "INSERT" or g.startswith("INSERT(") for g in mine), (
            table,
            mine,
        )
        assert not any(
            g.split("(")[0] in {"UPDATE", "DELETE", "TRUNCATE"} for g in mine
        ), (table, mine)
        assert web_grants[table] == frozenset({"SELECT"}), (table, web_grants[table])
        assert worker_grants[table] == frozenset(), (table, worker_grants[table])


def test_the_declared_grant_matrices_cover_the_comparison_tables() -> None:
    for table in _tables():
        assert RUNTIME_GRANTS[WEB_ROLE][table] == frozenset({"SELECT"})
        assert RUNTIME_GRANTS[WORKER_ROLE][table] == frozenset()
        declared = OBSERVER_GRANTS[table]
        assert "INSERT" in declared
        assert "UPDATE" not in declared and "DELETE" not in declared


@pytest.mark.parametrize("statement", ["insert", "update", "delete"])
def test_web_and_worker_connections_cannot_write_comparisons(
    web_dsn: str, worker_dsn: str, statement: str
) -> None:
    sql_text = {
        "insert": "INSERT INTO {t} DEFAULT VALUES",
        "update": "UPDATE {t} SET {c} = {c}",
        "delete": "DELETE FROM {t}",
    }[statement]
    for dsn in (web_dsn, worker_dsn):
        for table, columns in _tables().items():
            with psycopg.connect(dsn) as conn:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(sql_text.format(t=table, c=sorted(columns)[0]))


def test_worker_connection_cannot_read_comparisons_but_web_can(
    web_dsn: str, worker_dsn: str
) -> None:
    for table in _tables():
        with psycopg.connect(web_dsn) as conn:
            conn.execute(f"SELECT count(*) FROM {table}").fetchone()
        with psycopg.connect(worker_dsn) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(f"SELECT count(*) FROM {table}")


def test_the_observer_can_neither_rewrite_nor_delete_stored_comparisons(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    observer_dsn: str,
    catalog: Catalog,
) -> None:
    anomaly, _ = _collect(owner, web, observer, catalog)
    assert _evidence(anomaly.cid)
    for table, columns in _tables().items():
        for statement in (
            f"UPDATE {table} SET {sorted(columns)[0]} = {sorted(columns)[0]}",
            f"DELETE FROM {table}",
        ):
            with psycopg.connect(observer_dsn) as conn:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    conn.execute(statement)


# ===========================================================================
# 8. S8: raw and sha256 are stored; replay agrees and notices tampering
# ===========================================================================


def test_s8_raw_evidence_is_stored_with_its_sha256_and_replays_consistently(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    anomaly, world = _collect(owner, web, observer, catalog)

    stored = _evidence(anomaly.cid)
    assert len(stored) >= len(world.calls)
    import hashlib

    for row in stored:
        assert row["raw_sha256"] == hashlib.sha256(bytes(row["raw"])).hexdigest()
    replay = replay_release_history(observer.release_history(anomaly.oid))
    assert replay.consistent, replay.integrity
    assert replay.external_queries == () and replay.model_requests == ()
    # the same plan, replayed twice, gives the same comparison
    assert _comparison(anomaly) == _comparison(anomaly)


def test_s8_rewritten_comparison_evidence_makes_the_replay_inconsistent(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
) -> None:
    anomaly, _ = _collect(owner, web, observer, catalog)
    assert replay_release_history(observer.release_history(anomaly.oid)).consistent
    table = next(
        t
        for t, columns in _tables().items()
        if {"raw", "raw_sha256", "comparison_id"} <= columns
    )
    row = _stored(table, anomaly.cid)[0]
    # content changed, hash kept consistent (the table's CHECKs allow that)
    body = b'{"status":"success","data":{"resultType":"vector","result":[]}}'
    change = "raw = %s::bytea, raw_sha256 = %s"
    import hashlib

    params = (body, hashlib.sha256(body).hexdigest())
    # Test-only owner corruption; product roles cannot rewrite evidence.
    with psycopg.connect(DSN) as conn:
        conn.execute("SET LOCAL session_replication_role = replica")
        done = conn.execute(
            f"UPDATE {table} SET {change} WHERE comparison_id=%s AND query_key=%s",
            (*params, anomaly.cid, row["query_key"]),
        )
        assert done.rowcount == 1

    replay = replay_release_history(observer.release_history(anomaly.oid))

    assert not replay.consistent
    assert replay.integrity


def _fail_while_missing(test: Any) -> Any:
    @functools.wraps(test)
    def run(*args: Any, **kwargs: Any) -> None:
        if _PR4A_MISSING is not None:
            pytest.fail(f"PR4A_INTERFACE_MISSING: {_PR4A_MISSING}", pytrace=False)
        test(*args, **kwargs)

    return run


for _name in [n for n in list(globals()) if n.startswith("test_")]:
    globals()[_name] = _fail_while_missing(globals()[_name])
