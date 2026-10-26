"""Shared harness for the independent F11 four-step acceptance tests.

Written from ``docs/tasks/2026-10-24-f11-pr5-contract.md`` (section 4, the
decision table and its rulings), the F11 task record (A1-A11, PR-4b, PR-5)
and the F11 steps of ``feature_list.json``, without reading the F11
implementation. Only public interfaces drive the product:

* the web app over HTTP (``create_app``): the release intake, the release
  pages and their controls, the incident page;
* ``Workbench.release_authorize_pass`` / ``release_anomaly_pass``;
* ``ReleaseStore`` and ``ObservationStore`` public methods under the
  non-owner runtime roles (``opspilot_web``, ``opspilot_observer``);
* the Observer's ``ObserverLoop.poll_once`` against a fake Prometheus built
  here from the public query-plan builder;
* ``ObservationStore.replay_session``;
* ``opspilot.acceptance_release.release_outcome_from_durable`` (D8).

SQL is used only to read tables earlier F11 contracts name and for the
fixture constructions the earlier F11 acceptance tests also use (backdating
a session's ``authorized_at`` as superuser, making a sample job due now).

Database hygiene: every module gets its own scratch database (created from
``template0`` on the lab server ``scripts.m0.postgres_lab.DSN`` and migrated
to head), so no state is shared with any other module. Every release a
module delivers is cancelled at teardown if it is not terminal.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from opspilot import schema
from opspilot.observation import ObservationStore, release_policy
from opspilot.observation.release_types import (
    ReleaseSampleLease,
    ReleaseSampleReceipt,
)
from opspilot.observation.releases import ReleaseRecord, ReleaseStore
from opspilot.observer.loop import ObserverLoop
from opspilot.observer.prometheus import InstantResult
from opspilot.observer.release_evaluation import (
    build_release_query_plan,
    normalize_query,
)
from opspilot.persistence import DurableStore, PoolConfig, close_pools
from opspilot.persistence.grants import WEB_ROLE, WORKER_ROLE
from opspilot.tools.profiles import select_profile
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
from opspilot.web.store import MappingTargetRegistry
from scripts.m0.postgres_lab import DSN as LAB_DSN
from tests.f11_comparison_support import OLD_RS, ComparisonWorld
from tests.f11_pr3_support import RELEASE_ACTOR, RELEASE_TOKEN, bearer_for
from tests.f11_release_support import (
    AFTER_HASH,
    BEFORE_HASH,
    EMPTY_CATALOG,
    HEALTHY_CONTEXT,
    PROFILE,
    EvidenceBuilder,
    Pod,
    catalog_text,
    digest,
    pods_for,
    policy_content,
    policy_entry,
)
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
from tests.runtime_roles_support import login
from tests.target_support import IDENTITY, PROFILE_ID, WORKLOAD

PG = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)

POOL = PoolConfig(min_size=1, max_size=6, timeout=5.0)
OBSERVER_ROLE = "opspilot_observer"
PG_DUMP = os.environ.get("OPSPILOT_PG_DUMP", "pg_dump")
DB_TIME_ZONE = "America/Los_Angeles"

SESSION = PROFILE.session  # deadline 3600 s, max 40 samples, cadence 60 s
WINDOW = timedelta(seconds=PROFILE.evaluation_window_seconds)  # 300 s
DEADLINE = SESSION.deadline_seconds
MAX_SAMPLES = SESSION.max_samples
BEFORE_IMAGES = {"checkout": digest("f11-steps-before-image")}
AFTER_IMAGES = {"checkout": digest("f11-steps-after-image")}

LIST = "/version-changes"
JSON_ACCEPT = {"accept": "application/json"}
HTML_ACCEPT = {"accept": "text/html"}
TERMINAL_STATUSES = {"healthy", "anomalous", "unknown", "cancelled"}
#: D5: the labels a page or the acceptance projection shows.
LABELS = {
    "pending": "pending",
    "observing": "observing",
    "healthy": "healthy",
    "anomalous": "degraded",
    "unknown": "unknown",
    "cancelled": "cancelled",
}
#: D4 fixed disclaimer (contract: 「不批准、不阻断、不执行发布」).
DISCLAIMER = "不批准、不阻断、不执行发布"


# ===========================================================================
# the per-module environment
# ===========================================================================


@dataclass
class Env:
    dsn: str  # owner of the scratch database
    web_dsn: str
    observer_dsn: str
    worker_dsn: str
    owner: DurableStore
    web: ReleaseStore
    observer: ObservationStore
    reader: ReleaseStore  # owner-side reads
    history: ObservationStore  # owner-side replay
    catalog_path: Path
    catalog_entries: list[dict[str, Any]] = field(default_factory=list)
    registry: dict[str, dict[str, str]] = field(default_factory=dict)
    delivered: list[UUID] = field(default_factory=list)
    _stores: list[DurableStore] = field(default_factory=list)

    # -- SQL reads and fixture constructions --------------------------------

    def rows(self, statement: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            return list(conn.execute(statement, params).fetchall())

    def one(self, statement: str, params: Sequence[Any] = ()) -> dict[str, Any]:
        found = self.rows(statement, params)
        assert len(found) == 1, found
        return found[0]

    def count(self, statement: str, params: Sequence[Any] = ()) -> int:
        return int(self.one(statement, params)["n"])

    def now(self) -> datetime:
        with psycopg.connect(self.dsn) as conn:
            row = conn.execute("SELECT clock_timestamp()").fetchone()
        assert row is not None
        return row[0]

    def wait_past(self, moment: datetime, limit: float = 60.0) -> None:
        stop = time.monotonic() + limit
        while self.now() < moment:
            assert time.monotonic() < stop, f"{moment} did not pass"
            time.sleep(0.2)

    def set_authorized_at(self, session_id: UUID, at: datetime) -> None:
        """Backdate a session's authorization so windows wholly after it can
        end in the past (the construction the PR-2a/PR-4 tests use; the
        product guard freezes the column, so triggers are off for this one
        superuser transaction)."""
        with psycopg.connect(self.dsn) as conn:
            conn.execute("SET LOCAL session_replication_role = replica")
            done = conn.execute(
                "UPDATE opspilot_observation_sessions SET authorized_at=%s "
                "WHERE session_id=%s",
                (at, session_id),
            )
            assert done.rowcount == 1

    def make_due(self, session_id: UUID, *, expire_lease: bool = False) -> None:
        lease = ",active_sample_lease_until=clock_timestamp()-interval '1 second'"
        with psycopg.connect(self.dsn) as conn:
            conn.execute(
                "UPDATE opspilot_observation_sessions "
                "SET active_sample_due_at=clock_timestamp()-interval '1 day'"
                + (lease if expire_lease else "")
                + " WHERE session_id=%s AND active_sample_job_id IS NOT NULL",
                (session_id,),
            )

    def make_all_due(self, oid: UUID) -> None:
        for session in self.sessions(oid):
            self.make_due(session["session_id"], expire_lease=True)

    # -- release reads -------------------------------------------------------

    def record(self, oid: UUID) -> ReleaseRecord:
        return self.reader.get_release(oid)

    def sessions(self, oid: UUID) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT session_id,state,authorized_at,deadline_at,observation_generation,"
            "tracking_since,tracking_until FROM opspilot_observation_sessions "
            "WHERE release_observation_id=%s ORDER BY observation_generation,session_id",
            (oid,),
        )

    def authorized(self, oid: UUID) -> list[dict[str, Any]]:
        return [s for s in self.sessions(oid) if s["state"] == "authorized"]

    def samples(self, oid: UUID) -> int:
        return self.count(
            "SELECT count(*) AS n FROM opspilot_observation_samples s "
            "JOIN opspilot_observation_sessions o ON o.session_id=s.session_id "
            "WHERE o.release_observation_id=%s",
            (oid,),
        )

    def audit(self, oid: UUID) -> tuple[dict[str, Any], ...]:
        return self.reader.release_audit(oid)

    def links(self, oid: UUID) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT * FROM opspilot_release_incident_links WHERE observation_id=%s",
            (oid,),
        )

    def incidents_on(self, target_id: UUID) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT incident_id,lifecycle FROM opspilot_incidents WHERE target_id=%s",
            (target_id,),
        )

    def runs_of(self, incident_id: UUID) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT run_id,state,input FROM opspilot_runs WHERE incident_id=%s "
            "ORDER BY control_generation",
            (incident_id,),
        )

    def events_of(self, incident_id: UUID) -> list[dict[str, Any]]:
        return self.rows(
            "SELECT input_id,content,actor FROM opspilot_inputs "
            "WHERE incident_id=%s AND kind='event' ORDER BY sequence",
            (incident_id,),
        )

    def totals(self) -> dict[str, int]:
        return {
            table: self.count(f"SELECT count(*) AS n FROM {table}")
            for table in (
                "opspilot_incidents",
                "opspilot_runs",
                "opspilot_inputs",
                "opspilot_release_incident_links",
            )
        }

    # -- the calibrated policy catalog (PR-2 公开接口 1: a test catalog) ------

    def add_policy(self, entry: dict[str, Any]) -> None:
        self.catalog_entries.append(entry)
        self.catalog_path.write_text(
            catalog_text(*self.catalog_entries), encoding="utf-8"
        )

    def drop_policy(self, entry: dict[str, Any]) -> None:
        self.catalog_entries.remove(entry)
        text = (
            catalog_text(*self.catalog_entries)
            if self.catalog_entries
            else EMPTY_CATALOG
        )
        self.catalog_path.write_text(text, encoding="utf-8")

    # -- the web process -----------------------------------------------------

    def bench(self) -> Workbench:
        """A Workbench under ``opspilot_web`` whose target registry lists every
        target this module delivered a release for."""
        store = DurableStore(self.web_dsn, pool=POOL)
        store.install()
        self._stores.append(store)
        events = DurableEventLog(store)
        evidence = DurableEvidenceStore(store)
        ledger = DurableWebLedger(store)
        profile = select_profile({})
        return Workbench(
            incidents=DurableIncidentStore(store),
            events=events,
            evidence=evidence,
            ledger=ledger,
            run_versions=profile.versions(),
            tool_face=profile.face(DurableClock(store)),
            run_seconds=600,
            health_profile=PROFILE,
            targets=MappingTargetRegistry(
                {k: dict(v) for k, v in self.registry.items()}
            ),
            releases=ReleaseStore(self.web_dsn, pool=POOL),
        )

    def app(self, workbench: Workbench | None = None) -> Any:
        auth = AuthConfig(
            ui_users={UI_USER: hash_password(UI_PASSWORD, salt=b"fixed-salt-16byt")},
            event_tokens={
                token_digest(EVENT_TOKEN): "alertmanager",
                token_digest(RELEASE_TOKEN): RELEASE_ACTOR,
            },
            auth_revision="f11-steps-acceptance",
            allowed_origins=frozenset({ORIGIN}),
        )
        return create_app(
            workbench or self.bench(),
            Authenticator(auth),
            DurableClock(self.owner),
            releases=self.web,
        )

    def authorize_pass(self) -> list[Any]:
        return self.bench().release_authorize_pass()

    def anomaly_pass(self) -> list[Any]:
        return self.bench().release_anomaly_pass()

    def tick(self, source: Any) -> list[Any]:
        return ObserverLoop(store=self.observer, source=source, batch=50).poll_once()

    def close(self) -> None:
        for store in self._stores:
            with contextlib.suppress(Exception):
                store.close()


def _scratch_database(stack: contextlib.ExitStack) -> str:
    name = f"opspilot_f11steps_{uuid4().hex[:12]}"
    with psycopg.connect(LAB_DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )

    def drop() -> None:
        close_pools()
        with psycopg.connect(LAB_DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )

    stack.callback(drop)
    dsn = make_conninfo(LAB_DSN, dbname=name)
    schema.migrate(dsn, pg_dump=PG_DUMP)
    # The database session time zone is a deployment setting, not part of
    # any F11 contract; pin a non-UTC one so every module runs under the
    # same, deliberately non-trivial setting (instants must not change).
    with psycopg.connect(LAB_DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("ALTER DATABASE {} SET timezone TO {}").format(
                sql.Identifier(name), sql.Literal(DB_TIME_ZONE)
            )
        )
    return dsn


@contextlib.contextmanager
def module_env(tmp: Path) -> Iterator[Env]:
    """A scratch database at head, three non-owner logins, a test catalog."""
    with contextlib.ExitStack() as stack:
        dsn = _scratch_database(stack)
        web_dsn = stack.enter_context(login(dsn, WEB_ROLE))
        observer_dsn = stack.enter_context(login(dsn, OBSERVER_ROLE))
        worker_dsn = stack.enter_context(login(dsn, WORKER_ROLE))
        owner = DurableStore(dsn, pool=POOL)
        owner.install()
        stack.callback(owner.close)
        patch = stack.enter_context(pytest.MonkeyPatch.context())
        catalog_path = tmp / f"release-calibrations-{uuid4().hex}.json"
        catalog_path.write_text(EMPTY_CATALOG, encoding="utf-8")
        patch.setattr(release_policy, "RELEASE_POLICY_CATALOG_PATH", catalog_path)
        env = Env(
            dsn=dsn,
            web_dsn=web_dsn,
            observer_dsn=observer_dsn,
            worker_dsn=worker_dsn,
            owner=owner,
            web=ReleaseStore(web_dsn, pool=POOL),
            observer=ObservationStore(observer_dsn, pool=POOL),
            reader=ReleaseStore(dsn, pool=POOL),
            history=ObservationStore(dsn, pool=POOL),
            catalog_path=catalog_path,
        )
        stack.callback(env.close)
        stack.callback(_cancel_leftovers, env)
        yield env


def _cancel_leftovers(env: Env) -> None:
    for oid in env.delivered:
        with contextlib.suppress(Exception):
            record = env.record(oid)
            if record.status in TERMINAL_STATUSES:
                continue
            env.web.control_release(
                oid,
                action="cancel",
                actor="operator:f11-steps-cleanup",
                expected_control_generation=record.control_generation,
                idempotency_key=f"f11-steps-cleanup-{uuid4().hex}",
                calibrated_policy_revision=None,
            )


# ===========================================================================
# delivering a release over HTTP (PR-3 intake, release-deployer)
# ===========================================================================


def registry_entry(**overrides: str) -> dict[str, str]:
    return {
        "integration_id": IDENTITY["integration_id"],
        "cluster_uid": IDENTITY["cluster_uid"],
        "namespace": IDENTITY["namespace"],
        "workload": WORKLOAD,
        "health_profile_id": PROFILE_ID,
        **overrides,
    }


def iso_utc(moment: datetime) -> str:
    """``released_at`` exactly as an aware UTC ISO-8601 instant with an
    explicit offset and microseconds (byte-comparable with a stored value
    rendered the same way)."""
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def cluster_of(uid: str) -> str:
    """Each test target is its own cluster: the PR-3 r2 resolver refuses a
    payload whose (cluster_uid, namespace, workload) also names another
    registry entry (``TARGET_AMBIGUOUS``), and every test target is a
    checkout Deployment in the profile's namespace."""
    return f"{IDENTITY['cluster_uid']}-{uid.rsplit('-', 1)[-1]}"


def release_payload(uid: str, release_id: str, released_at: datetime) -> dict[str, Any]:
    return {
        "format_version": 1,
        "source": "kind-harness-f11-steps",
        "source_release_id": release_id,
        "released_at": iso_utc(released_at),
        "target": {
            **IDENTITY,
            "cluster_uid": cluster_of(uid),
            "resource_uid": uid,
            "workload": WORKLOAD,
        },
        "before": {
            "images": dict(BEFORE_IMAGES),
            "pod_template_digest": digest(f"before-template-{release_id}"),
        },
        "after": {
            "images": dict(AFTER_IMAGES),
            "pod_template_digest": digest(f"after-template-{release_id}"),
        },
        "auxiliary": {
            "deployment_uid": f"dep-{release_id}",
            "before_replicaset_uid": f"rs-before-{release_id}",
            "after_replicaset_uid": f"rs-after-{release_id}",
            "before_pod_template_hash": BEFORE_HASH,
            "after_pod_template_hash": AFTER_HASH,
            "deployment_generation": 4,
        },
    }


def intake(app: Any, body: bytes, headers: Mapping[str, str] | None = None) -> Any:
    return call(
        app,
        "POST",
        "/intake/version-changes",
        headers={
            "content-type": "application/json",
            **(bearer_for(RELEASE_TOKEN) if headers is None else headers),
        },
        body=body,
    )


@dataclass
class Delivered:
    oid: UUID
    uid: str
    payload: dict[str, Any]
    body: bytes
    record: ReleaseRecord
    entry: dict[str, Any] | None

    @property
    def target_id(self) -> UUID:
        return self.record.target_id


def policy_for(record: ReleaseRecord, **overrides: Any) -> dict[str, Any]:
    """The catalog entry that matches ``record`` exactly (D3)."""
    tracking = overrides.pop("min_tracking_seconds", record.min_tracking_seconds)
    return policy_entry(
        policy_content(
            record.target_snapshot,
            min_tracking_seconds=tracking,
            health_profile_revision=record.health_profile_revision,
            **overrides,
        )
    )


def deliver(
    env: Env,
    *,
    released_ago: float = 2000.0,
    calibrate: bool = True,
    app: Any = None,
) -> Delivered:
    """One release over ``POST /intake/version-changes`` (201, pending) on a
    fresh registered target; ``calibrate`` lists its exact policy."""
    uid = f"deployment/checkout-f11s-{uuid4().hex[:10]}"
    env.registry[uid] = registry_entry(cluster_uid=cluster_of(uid))
    payload = release_payload(
        uid,
        f"f11s-{uuid4().hex[:12]}",
        env.now() - timedelta(seconds=released_ago),
    )
    body = json.dumps(payload, sort_keys=True).encode()
    response = intake(app or env.app(), body)
    assert response.status == 201, (response.status, response.text[:500])
    oid = UUID(response.json()["observation_id"])
    env.delivered.append(oid)
    record = env.record(oid)
    assert (record.status, record.paused) == ("pending", False), record
    entry = None
    if calibrate:
        entry = policy_for(record)
        env.add_policy(entry)
    return Delivered(oid, uid, payload, body, record, entry)


def observing(env: Env, **release: Any) -> tuple[Delivered, UUID]:
    """Delivered over HTTP and authorized by one automatic pass (D7)."""
    case = deliver(env, **release)
    env.authorize_pass()
    record = env.record(case.oid)
    assert record.status == "observing", record
    (session,) = env.authorized(case.oid)
    return case, session["session_id"]


# ===========================================================================
# samples: the Observer store API and the Observer tick
# ===========================================================================


def claims(env: Env, session_id: UUID) -> list[ReleaseSampleLease]:
    return [
        lease
        for lease in env.observer.claim_due_samples(uuid4(), limit=50)
        if isinstance(lease, ReleaseSampleLease) and lease.session_id == session_id
    ]


def claim(env: Env, session_id: UUID) -> ReleaseSampleLease:
    env.make_due(session_id)
    leases = claims(env, session_id)
    assert len(leases) == 1, leases
    return leases[0]


def submit(
    env: Env,
    session_id: UUID,
    window_end: datetime,
    pods: Sequence[Pod],
    *,
    after: Mapping[str, float] | None = None,
    context: Mapping[str, float] | None = None,
    mapping_ok: bool = True,
) -> ReleaseSampleReceipt:
    """One Observer sample of ``[window_end - W, window_end]`` with planned
    query texts and raw answers (``tests.f11_release_support``)."""
    lease = claim(env, session_id)
    frozen = env.observer.release_context(session_id)
    if not mapping_ok:
        # the new ReplicaSet's pods run the *before* image: the version
        # mapping does not hold, so the after slice is omitted (PR-2 B02)
        texts = build_release_query_plan(
            frozen, window_end=window_end, instance_uids=[]
        )
        builder = EvidenceBuilder(window_end=window_end, texts=dict(texts))
        builder.mapping(pods_for(BEFORE_IMAGES))
        builder.signals("context", {**HEALTHY_CONTEXT, **(context or {})})
        return env.observer.submit_release_sample(lease, builder.evidence())
    texts = build_release_query_plan(
        frozen, window_end=window_end, instance_uids=[p.uid for p in pods]
    )
    evidence = (
        EvidenceBuilder(window_end=window_end, texts=dict(texts))
        .sufficient(pods, after=after, context=context)
        .evidence()
    )
    return env.observer.submit_release_sample(lease, evidence)


def healthy_until(
    env: Env,
    session_id: UUID,
    authorized_at: datetime,
    pods: Sequence[Pod],
    offsets: Sequence[int],
) -> list[ReleaseSampleReceipt]:
    return [
        submit(env, session_id, authorized_at + timedelta(seconds=s), pods)
        for s in offsets
    ]


class ReleaseWorld:
    """A fake Prometheus for the Observer tick.

    For each registered session it answers that session's release sample
    plan (built by the public ``build_release_query_plan`` at the asked
    instant) with sufficient raw evidence for ``pods``, with ``after`` /
    ``context`` overrides; everything else (the PR-4a comparison queries) is
    answered by ``ComparisonWorld`` with one old pod; incident leases get an
    empty vector. Records every expression it was asked."""

    def __init__(self, comparison: ComparisonWorld | None = None) -> None:
        self.sessions: dict[UUID, tuple[Any, tuple[Pod, ...], dict[str, Any]]] = {}
        self.comparison = comparison or ComparisonWorld(
            old_pods=pods_for(BEFORE_IMAGES, 1, replicaset=OLD_RS)
        )
        self.calls: list[str] = []
        self.sample_calls: list[UUID] = []
        self._lock = threading.Lock()

    def answer(
        self,
        env: Env,
        session_id: UUID,
        pods: Sequence[Pod],
        **overrides: Any,
    ) -> None:
        self.sessions[session_id] = (
            env.observer.release_context(session_id),
            tuple(pods),
            overrides,
        )

    def instant(self, expr: str, *, at: Any, timeout_seconds: int) -> InstantResult:
        with self._lock:
            self.calls.append(expr)
        body = b'{"status":"success","data":{"resultType":"vector","result":[]}}'
        return InstantResult(expr, "no_data", None, body, 200)

    def instant_vector(self, expr: str, *, at: datetime, timeout_seconds: int) -> Any:
        from tests.test_f11_pr2b_acceptance import SENT_AT, _vector_result

        with self._lock:
            self.calls.append(expr)
        wanted = normalize_query(expr)
        for session_id, (frozen, pods, overrides) in self.sessions.items():
            plan = build_release_query_plan(
                frozen, window_end=at, instance_uids=[p.uid for p in pods]
            )
            for key, text in plan.items():
                if normalize_query(text) != wanted:
                    continue
                with self._lock:
                    self.sample_calls.append(session_id)
                builder = EvidenceBuilder(window_end=at, texts=dict(plan)).sufficient(
                    pods,
                    after=overrides.get("after"),
                    context=overrides.get("context"),
                )
                return _vector_result(expr, builder.results[key], SENT_AT)
        return self.comparison.instant_vector(
            expr, at=at, timeout_seconds=timeout_seconds
        )


# ===========================================================================
# pages and controls over HTTP
# ===========================================================================


def detail_path(oid: UUID) -> str:
    return f"/version-changes/{oid}"


def control_path(oid: UUID) -> str:
    return f"/version-changes/{oid}/control"


def get_json(app: Any, path: str) -> Any:
    response = call(app, "GET", path, headers={**basic(), **JSON_ACCEPT})
    assert response.status == 200, (path, response.status, response.text[:500])
    return response.json()


def get_html(app: Any, path: str) -> str:
    response = call(app, "GET", path, headers={**basic(), **HTML_ACCEPT})
    assert response.status == 200, (path, response.status, response.text[:500])
    assert response.headers.get("content-type", "").startswith("text/html")
    return str(response.text)


def strings(document: Any) -> list[str]:
    """Every string value (and key) anywhere in a JSON document."""
    found: list[str] = []
    if isinstance(document, str):
        found.append(document)
    elif isinstance(document, Mapping):
        for key, value in document.items():
            found.append(str(key))
            found.extend(strings(value))
    elif isinstance(document, (list, tuple)):
        for value in document:
            found.extend(strings(value))
    return found


def instant_of(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def has_instant(document: Any, moment: datetime) -> bool:
    return any(instant_of(s) == moment for s in strings(document))


def post_control(
    app: Any,
    oid: UUID,
    action: str,
    expected: int,
    *,
    key: str | None = None,
    headers: Mapping[str, str] | None = None,
    origin: bool = True,
) -> Any:
    base = {**basic(), **JSON_ACCEPT}
    if origin:
        base.update(same_origin())
    return post_form(
        app,
        control_path(oid),
        {
            "action": action,
            "expected_control_generation": str(expected),
            "idempotency_key": key or f"f11s-{uuid4().hex}",
        },
        headers={**base, **(headers or {})},
    )


def refused(response: Any, status: int, code: str | None = None) -> None:
    assert response.status == status, (response.status, response.text[:500])
    if code is not None:
        assert response.json()["code"] == code, response.text[:500]


# ===========================================================================
# a delayed regression, end to end up to the anomaly pass
# ===========================================================================


@dataclass
class Regression:
    case: Delivered
    session_id: UUID
    authorized_at: datetime
    world: ReleaseWorld
    pods: tuple[Pod, ...]
    healthy: list[ReleaseSampleReceipt]


def comparisons(env: Env, oid: UUID) -> list[dict[str, Any]]:
    return env.rows(
        "SELECT * FROM opspilot_release_comparisons WHERE observation_id=%s", (oid,)
    )


def regress(
    env: Env,
    *,
    world: ReleaseWorld | None = None,
    healthy_offsets: Sequence[int] = (300, 600),
    after: Mapping[str, float] | None = None,
    comparison: Any = None,
    **release: Any,
) -> Regression:
    """Delivered over HTTP, authorized by the pass, some healthy samples,
    then one degraded sample taken by the Observer tick before the
    deadline; further ticks collect the PR-4a comparison. No anomaly pass.
    With ``comparison`` (a fake Prometheus) the degraded sample is
    submitted through the Observer store API instead and the ticks that
    collect the comparison query ``comparison`` only."""
    case, sid = observing(env, **release)
    authorized_at = case.record.released_at + timedelta(seconds=100)
    env.set_authorized_at(sid, authorized_at)
    pods = pods_for(AFTER_IMAGES)
    healthy = healthy_until(env, sid, authorized_at, pods, healthy_offsets)
    for receipt in healthy:
        assert receipt.evaluation.verdict == "healthy", receipt
        assert receipt.release_status == "observing", receipt
    degraded = dict(after or {"error_ratio": 0.5})
    world = world or ReleaseWorld()
    if comparison is None:
        world.answer(env, sid, pods, after=degraded)
        env.make_due(sid)
    else:
        # the degraded sample through the Observer store API, so that every
        # comparison query reaches ``comparison`` alone
        last = max(healthy_offsets, default=0) + 300
        receipt = submit(
            env, sid, authorized_at + timedelta(seconds=last), pods, after=degraded
        )
        assert receipt.release_status == "anomalous", receipt
    for _ in range(6):
        env.tick(world if comparison is None else comparison)
        if comparisons(env, case.oid):
            break
    record = env.record(case.oid)
    assert record.status == "anomalous", record
    assert record.deadline_at == case.record.deadline_at
    return Regression(case, sid, authorized_at, world, pods, healthy)
