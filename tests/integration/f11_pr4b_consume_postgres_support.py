"""Independent PostgreSQL acceptance tests for F11 PR-4b: consuming a release
anomaly into an incident (create, attach, related, handoff), the v6 Run input,
continuation, replay and permissions.

Written from ``docs/tasks/2026-10-21-f11-pr4-contract.md`` (P4-01..P4-07
decided as recommended, R1-R12, section 4 public interfaces, section 6 items
1-12), the F11 task record (PR-4 r1: P4-04 = F2 PR-C C3 ``choose_correlation``)
and the public PR-4a/PR-C interfaces on ``main``, without reading the PR-4b
implementation.

Entry points are the public ones only: ``Workbench.release_anomaly_pass``,
``Workbench.control``, the web app, ``ObservationStore.replay_session`` and
plain SQL under the real runtime roles. Preconditions (an anomalous release
with its PR-4a comparison) come from the PR-4a acceptance helpers, which drive
the real Observer against a fake Prometheus.

Where the contract leaves a name or a value open, the test asserts only what
the contract states (values searched in the stored fact rather than key
names) or skips with ``contract gap: ...``.
"""

# ruff: noqa: F811 - pytest fixtures imported from the PR-2a module
from __future__ import annotations

import dataclasses
import functools
import json
import os
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4, uuid5

import psycopg
import pytest

from opspilot.observation import ObservationStore
from opspilot.observation.releases import ReleaseStore
from opspilot.persistence import DurableStore
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
from opspilot.web.service import _INTAKE_NAMESPACE
from opspilot.web.store import MappingTargetRegistry
from scripts.m0.postgres_lab import DSN
from tests.integration.f11_pr4a_comparison_postgres_support import (
    Anomaly,
    _anomalous,
    _drain,
    _tick,
    _world,
)
from tests.integration.f11_release_observation_postgres_support import (  # noqa: F401 - fixtures
    AFTER_IMAGES,
    BEFORE_IMAGES,
    OBSERVER_ROLE,
    POOL,
    Catalog,
    _claim,
    _control,
    _db_now,
    _observing,
    _refused,
    _rows,
    _set_authorized_at,
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
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
)
from tests.runtime_roles_support import catalog_grants
from tests.target_support import IDENTITY, PROFILE_ID, WORKLOAD

try:  # PR-4b public names (contract section 4)
    from opspilot.investigation.context import (
        INPUT_VERSION_RELEASE,
        RELEASE_CONTEXT_BOUNDARY,
        RELEASE_OBSERVATION,
    )

    _PR4B_MISSING: str | None = (
        None
        if hasattr(Workbench, "release_anomaly_pass")
        else "Workbench.release_anomaly_pass is missing"
    )
except ImportError as exc:  # implementation not merged yet: fail, do not error
    _PR4B_MISSING = f"{type(exc).__name__}: {exc}"
    INPUT_VERSION_RELEASE = "opspilot-investigation-input-v6"
    RELEASE_OBSERVATION = "release_observation"
    RELEASE_CONTEXT_BOUNDARY = ""

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("M1_DURABLE_POSTGRES") != "1",
        reason="explicit PG opt-in required",
    ),
]

LINKS = "opspilot_release_incident_links"  # contract section 4, migration 4b
RELEASES = "opspilot_release_observations"
DISPOSITIONS = {"created", "attached", "created_related", "handoff_created"}
FRAME = timedelta(hours=24)  # R3: 24 h frame
LOOKBACK = timedelta(seconds=3600)  # R3: default window starts 1 h before


# ===========================================================================
# workbench under the web role
# ===========================================================================


def _registry_entry(**overrides: str) -> dict[str, str]:
    """One ``OPSPILOT_TARGET_IDENTITIES`` entry for a release target recorded
    by ``tests.integration.f11_release_observation_postgres_support._target``."""
    return {
        "integration_id": IDENTITY["integration_id"],
        "cluster_uid": IDENTITY["cluster_uid"],
        "namespace": IDENTITY["namespace"],
        "workload": WORKLOAD,
        "health_profile_id": PROFILE_ID,
        **overrides,
    }


def _release_store_kwargs(web_dsn: str) -> dict[str, Any]:
    """The contract does not say how the Workbench reaches the release store
    (contract gap G1). If the implementation adds a constructor field whose
    name mentions ``release``, it gets a web-role ``ReleaseStore``; otherwise
    the Workbench is built exactly as ``opspilot.web.__main__`` builds it."""
    extra: dict[str, Any] = {}
    for field in dataclasses.fields(Workbench):
        if field.init and "release" in field.name:
            extra[field.name] = ReleaseStore(web_dsn, pool=POOL)
    return extra


@pytest.fixture
def bench(web_dsn: str) -> Iterator[Callable[..., Workbench]]:
    """Builds a Workbench under ``opspilot_web`` with a registry that lists
    exactly the given ``{resource_uid: entry}`` (P4-02-A: the web process
    consumes, as ``self_heal_pass`` does)."""
    stores: list[DurableStore] = []

    def build(entries: Mapping[str, Mapping[str, str]] | None = None) -> Workbench:
        store = DurableStore(web_dsn, pool=POOL)
        store.install()
        stores.append(store)
        events = DurableEventLog(store)
        events.install()
        evidence = DurableEvidenceStore(store)
        evidence.install()
        ledger = DurableWebLedger(store)
        ledger.install()
        profile = select_profile({})
        clock = DurableClock(store)
        return Workbench(
            incidents=DurableIncidentStore(store),
            events=events,
            evidence=evidence,
            ledger=ledger,
            run_versions=profile.versions(),
            tool_face=profile.face(clock),
            run_seconds=600,
            targets=MappingTargetRegistry(
                {k: dict(v) for k, v in (entries or {}).items()}
            ),
            **_release_store_kwargs(web_dsn),
        )

    yield build
    for store in stores:
        store.close()


# ===========================================================================
# scenario helpers
# ===========================================================================


@dataclass
class Case:
    anomaly: Anomaly
    target_id: UUID
    resource_uid: str
    anomaly_key: str
    source_release_id: str
    policy_revision: str

    @property
    def oid(self) -> UUID:
        return self.anomaly.oid

    @property
    def intake_key(self) -> str:
        return "intake:" + self.anomaly_key  # R1

    @property
    def incident_id(self) -> UUID:
        return uuid5(_INTAKE_NAMESPACE, self.intake_key)  # R1

    @property
    def run_id(self) -> UUID:
        return uuid5(_INTAKE_NAMESPACE, self.intake_key + ":run:0")  # R1

    def event_id(self, incident_id: UUID) -> UUID:
        return uuid5(_INTAKE_NAMESPACE, f"{self.anomaly_key}:event:{incident_id}")

    def registry(self, **overrides: str) -> dict[str, dict[str, str]]:
        return {self.resource_uid: _registry_entry(**overrides)}


def _one(statement: str, params: Any = ()) -> dict[str, Any]:
    rows = _rows(statement, params)
    assert len(rows) == 1, rows
    return rows[0]


def _settle(
    observer: ObservationStore, bench: Callable[..., Workbench], web: ReleaseStore
) -> None:
    """Leave no comparison and no anomaly ending pending from earlier tests,
    so this test's ending is the only one a pass sees. Leftovers of other
    tests are handed off (their targets are not in this empty registry)."""
    _drain(observer)
    workbench = bench({})
    for _ in range(10):
        if not web.pending_anomaly_endings(limit=200):
            return
        try:
            workbench.release_anomaly_pass()
        except Exception:  # a leftover another test damaged on purpose
            return


def _case(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    **release: Any,
) -> Case:
    """An anomalous release (PR-2 degraded sample), comparison not collected."""
    anomaly = _anomalous(owner, web, observer, catalog, **release)
    row = _one(
        f"SELECT r.target_id,t.resource_uid,r.source_release_id,r.calibrated_policy_revision "
        f"FROM {RELEASES} r JOIN opspilot_targets t ON t.target_id=r.target_id "
        "WHERE r.observation_id=%s",
        (anomaly.oid,),
    )
    ending = _one(
        "SELECT anomaly_key FROM opspilot_observation_endings WHERE ending_id=%s",
        (anomaly.ending_id,),
    )
    assert ending["anomaly_key"], ending
    return Case(
        anomaly=anomaly,
        target_id=row["target_id"],
        resource_uid=row["resource_uid"],
        anomaly_key=ending["anomaly_key"],
        source_release_id=row["source_release_id"],
        policy_revision=row["calibrated_policy_revision"],
    )


def _comparison_row(case: Case) -> dict[str, Any] | None:
    rows = _rows(
        "SELECT * FROM opspilot_release_comparisons WHERE comparison_id=%s",
        (case.anomaly.cid,),
    )
    return rows[0] if rows else None


def _compare(
    observer: ObservationStore, case: Case, world: Any = None
) -> dict[str, Any]:
    """Let the real Observer collect this ending's PR-4a comparison."""
    world = world or _world()
    for _ in range(6):
        _tick(observer, world)
        row = _comparison_row(case)
        if row is not None:
            return row
    pytest.fail("the PR-4a comparison was not recorded", pytrace=False)


def _ready(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
    **release: Any,
) -> Case:
    _settle(observer, bench, web)
    case = _case(owner, web, observer, catalog, **release)
    _compare(observer, case)
    return case


def _receipts(receipts: Any, case: Case) -> list[Any]:
    return [r for r in receipts if r.observation_id == case.oid]


def _links(oid: UUID) -> list[dict[str, Any]]:
    return _rows(f"SELECT * FROM {LINKS} WHERE observation_id=%s", (oid,))


def _link(oid: UUID) -> dict[str, Any]:
    (row,) = _links(oid)
    return row


def _release_incident(oid: UUID) -> UUID | None:
    return _one(f"SELECT incident_id FROM {RELEASES} WHERE observation_id=%s", (oid,))[
        "incident_id"
    ]


def _incident(incident_id: UUID) -> dict[str, Any]:
    return _one(
        "SELECT incident_id,intake_key,state,lifecycle,mode,control_generation,"
        "observation_generation,current_run_id,target_id,related_incident_id,priority,"
        "conclusion IS NULL AS unconcluded,created_at FROM opspilot_incidents "
        "WHERE incident_id=%s",
        (incident_id,),
    )


def _by_intake(intake_key: str) -> list[dict[str, Any]]:
    return _rows("SELECT * FROM opspilot_incidents WHERE intake_key=%s", (intake_key,))


def _on_target(target_id: UUID) -> list[dict[str, Any]]:
    return _rows(
        "SELECT incident_id,lifecycle FROM opspilot_incidents WHERE target_id=%s",
        (target_id,),
    )


def _runs(incident_id: UUID) -> list[dict[str, Any]]:
    return _rows(
        "SELECT run_id,state,control_generation,input,input_watermark FROM opspilot_runs "
        "WHERE incident_id=%s ORDER BY control_generation",
        (incident_id,),
    )


def _run_input(run_id: UUID) -> dict[str, Any]:
    value = _one("SELECT input FROM opspilot_runs WHERE run_id=%s", (run_id,))["input"]
    assert isinstance(value, dict), value
    return value


def _events(incident_id: UUID) -> list[dict[str, Any]]:
    return _rows(
        "SELECT input_id,kind,content,actor FROM opspilot_inputs "
        "WHERE incident_id=%s AND kind='event' ORDER BY sequence",
        (incident_id,),
    )


def _seed(
    owner: DurableStore,
    target_id: UUID | None,
    *,
    lifecycle: str = "open",
    created_at: datetime | None = None,
) -> UUID:
    """An existing incident on ``target_id`` (``None``: a handoff incident,
    which has no target, as an E5 alert handoff)."""
    incident_id = uuid4()
    if target_id is None:
        with owner.transaction() as conn:
            conn.execute(
                "INSERT INTO opspilot_incidents(incident_id,intake_key,state,lifecycle,"
                "current_run_id,target_id) VALUES(%s,%s,'waiting_human','open',NULL,NULL)",
                (incident_id, f"seed:{incident_id}"),
            )
    else:
        owner.accept(
            incident_id,
            uuid4(),
            f"seed:{incident_id}",
            deadline=_db_now() + timedelta(hours=1),
            budget_limit=5,
            versions={"seed": "pr4b"},
            target_id=target_id,
        )
    with owner.transaction() as conn:
        if lifecycle != "open":
            conn.execute(
                "UPDATE opspilot_incidents SET lifecycle=%s WHERE incident_id=%s",
                (lifecycle, incident_id),
            )
        if created_at is not None:
            conn.execute(
                "UPDATE opspilot_incidents SET created_at=%s WHERE incident_id=%s",
                (created_at, incident_id),
            )
    return incident_id


def _tamper(statement: str, params: Any) -> None:
    """Test-only owner corruption, triggers off; product roles cannot."""
    with psycopg.connect(DSN) as conn:
        conn.execute("SET LOCAL session_replication_role = replica")
        done = conn.execute(statement, params)
        assert done.rowcount == 1, statement


def _fact(run_input: Mapping[str, Any]) -> Any:
    facts = run_input.get("scope_facts") or {}
    assert RELEASE_OBSERVATION in facts, sorted(facts)
    return facts[RELEASE_OBSERVATION]


def _text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _instants(moment: datetime) -> set[str]:
    """Renderings of one instant a JSON fact may carry."""
    utc = moment.astimezone(timezone.utc)
    forms = {utc.isoformat(), utc.isoformat().replace("+00:00", "Z")}
    whole = utc.replace(microsecond=0)
    forms |= {whole.isoformat(), whole.isoformat().replace("+00:00", "Z")}
    return forms


def _expect_created(case: Case) -> tuple[dict[str, Any], dict[str, Any]]:
    """Exactly one new incident with one queued Run, linked ``created``."""
    incidents = _by_intake(case.intake_key)
    assert len(incidents) == 1, incidents
    incident = _incident(incidents[0]["incident_id"])
    runs = _runs(incident["incident_id"])
    assert len(runs) == 1, runs
    link = _link(case.oid)
    assert link["disposition"] == "created", link
    assert link["incident_id"] == incident["incident_id"]
    assert link["run_id"] == runs[0]["run_id"]
    assert _release_incident(case.oid) == incident["incident_id"]
    return incident, runs[0]


# ===========================================================================
# 1. a new incident and one queued Run (item 1, R1, P4-03, P4-05, R3, R5)
# ===========================================================================


# ===========================================================================
# 1b. idempotent: repeat, concurrent, failed transaction (item 1)
# ===========================================================================


# ===========================================================================
# 1c. the comparison record first (P4-01-A, item 11)
# ===========================================================================


#: r3 G9 (lead 2026-10-22): without a comparison record the consumer waits
#: until DB clock >= ending.recorded_at + 510 s, then consumes with
#: ``{status: unavailable, reason_codes: [COMPARISON_NOT_RECORDED]}``.
COMPARISON_WAIT = timedelta(seconds=510)


# ===========================================================================
# 2. an open incident on the target takes one event (item 2, U8, R4)
# ===========================================================================


# ===========================================================================
# 3-4. closed incidents and several candidates (items 3-4, P4-04 = C3)
# ===========================================================================


# ===========================================================================
# 5. nothing for releases without an anomaly ending (item 5)
# ===========================================================================


# ===========================================================================
# 6. unresolvable target: handoff without a Run (item 6, R7)
# ===========================================================================


def _damage_target_row(case: Case, how: str) -> None:
    """r3 G2: R7 resolves the five ``opspilot_targets`` fields against the
    release's ``target_snapshot``; the web identities file plays no part.
    ``missing``: the row has lost its identity; ``mismatch``: one field
    differs. Test-only owner change, triggers off."""
    change = (
        "integration_id=NULL,cluster_uid=NULL,namespace=NULL,workload=NULL"
        if how == "missing"
        else "namespace='elsewhere'"
    )
    _tamper(
        f"UPDATE opspilot_targets SET {change} WHERE target_id=%s", (case.target_id,)
    )


# ===========================================================================
# 7. permissions (item 7, P4-03, section 4 grants)
# ===========================================================================


def _grants(role: str) -> dict[str, frozenset[str]]:
    with psycopg.connect(DSN) as conn:
        return catalog_grants(conn, role)


def _columns_of(grant: str) -> set[str]:
    return set(grant[grant.index("(") + 1 : -1].split(",")) if "(" in grant else set()


# ===========================================================================
# 8. suspension does not stop intake (item 8, R10)
# ===========================================================================


# ===========================================================================
# 9. later Runs keep the release facts (item 9, R6)
# ===========================================================================


def _generation(incident_id: UUID) -> int:
    return int(_incident(incident_id)["control_generation"])


def _cancel_and_new_run(workbench: Workbench, incident_id: UUID) -> Any:
    workbench.control(
        incident_id,
        actor_id="operator:test",
        action="cancel",
        expected_generation=_generation(incident_id),
        idempotency_key=f"cancel-{uuid4()}",
    )
    return workbench.control(
        incident_id,
        actor_id="operator:test",
        action="new_run",
        expected_generation=_generation(incident_id),
        idempotency_key=f"new-run-{uuid4()}",
    )


def _created_case(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> tuple[Case, Workbench, dict[str, Any], dict[str, Any]]:
    case = _ready(owner, web, observer, catalog, bench)
    workbench = bench(case.registry())
    workbench.release_anomaly_pass()
    incident, run = _expect_created(case)
    return case, workbench, incident, _run_input(run["run_id"])


def _successor_input(incident_id: UUID, previous: UUID) -> dict[str, Any]:
    current = _incident(incident_id)["current_run_id"]
    assert current is not None and current != previous
    return _run_input(current)


# ===========================================================================
# 10. replay checks the association (item 10, R9)
# ===========================================================================


# ===========================================================================
# 12. no release gate writes: no outbound call from the consumer (item 12)
# ===========================================================================


# ===========================================================================
# the release incident is an ordinary incident on the web pages
# ===========================================================================


def _app(workbench: Workbench, owner: DurableStore) -> Any:
    auth = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager"},
        auth_revision="f11-pr4b-acceptance",
        allowed_origins=frozenset({ORIGIN}),
    )
    return create_app(workbench, Authenticator(auth), DurableClock(owner))


# ===========================================================================
# red while the implementation is missing: fail, do not error
# ===========================================================================


def _fail_while_missing(test: Any) -> Any:
    @functools.wraps(test)
    def run(*args: Any, **kwargs: Any) -> None:
        if _PR4B_MISSING is not None:
            pytest.fail(f"PR4B_INTERFACE_MISSING: {_PR4B_MISSING}", pytrace=False)
        test(*args, **kwargs)

    return run


for _name in [n for n in list(globals()) if n.startswith("test_")]:
    globals()[_name] = _fail_while_missing(globals()[_name])

# Re-exported for the kept acceptance tests (2026-11-18 test-suite slim).
# ruff: noqa: E402
from tests.integration.f11_pr4a_comparison_postgres_support import (
    _record,  # noqa: F401
)
