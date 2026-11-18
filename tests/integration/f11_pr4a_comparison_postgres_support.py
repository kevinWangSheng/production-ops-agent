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
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from opspilot.observation import ObservationStore
from opspilot.observation.releases import (
    ReleaseAuxiliary,
    ReleaseEvent,
    ReleaseRevision,
    ReleaseStore,
)
from opspilot.observer.loop import ObserverLoop
from opspilot.persistence import DurableStore
from scripts.m0.postgres_lab import DSN
from tests.f11_comparison_support import (
    BEFORE_WINDOW,
    OLD_RS,
    ComparisonWorld,
    comparison_id,
    comparison_tables,
    evaluate,
    to_result,
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
from tests.integration.f11_release_observation_postgres_support import (  # noqa: F401 - fixtures
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


# ===========================================================================
# 2. S2 / S3: the old slice is known or unknown, never guessed
# ===========================================================================


# ===========================================================================
# 3. S4: the instance set must equal the mapped set exactly
# ===========================================================================


# ===========================================================================
# 4. S5: released_at later than the rollout is unknown, not retried earlier
# ===========================================================================


# ===========================================================================
# 5. S9: too few samples
# ===========================================================================


# ===========================================================================
# 6. S6: bounded attempts and queries
# ===========================================================================


# ===========================================================================
# 7. permissions: Observer INSERT, web SELECT, worker nothing
# ===========================================================================


def _grants(role: str) -> dict[str, frozenset[str]]:
    with psycopg.connect(DSN) as conn:
        return catalog_grants(conn, role)


# ===========================================================================
# 8. S8: raw and sha256 are stored; replay agrees and notices tampering
# ===========================================================================


def _fail_while_missing(test: Any) -> Any:
    @functools.wraps(test)
    def run(*args: Any, **kwargs: Any) -> None:
        if _PR4A_MISSING is not None:
            pytest.fail(f"PR4A_INTERFACE_MISSING: {_PR4A_MISSING}", pytrace=False)
        test(*args, **kwargs)

    return run


for _name in [n for n in list(globals()) if n.startswith("test_")]:
    globals()[_name] = _fail_while_missing(globals()[_name])
