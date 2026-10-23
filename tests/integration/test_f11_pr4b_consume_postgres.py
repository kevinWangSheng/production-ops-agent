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
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4, uuid5

import psycopg
import pytest

from opspilot.acceptance_recovery import OBSERVER_GRANTS
from opspilot.investigation.context import InvestigationInput, initial_messages
from opspilot.observation import ObservationStore
from opspilot.observation.releases import ReleaseStore
from opspilot.persistence import DurableStore
from opspilot.persistence.grants import RUNTIME_GRANTS, WEB_ROLE, WORKER_ROLE
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
from opspilot.web.service import _INTAKE_NAMESPACE, WorkbenchError
from opspilot.web.store import MappingTargetRegistry
from scripts.m0.postgres_lab import DSN
from tests.f11_release_support import (
    AFTER_HASH,
    BEFORE_HASH,
    EvidenceBuilder,
    digest,
    planned_texts,
    pods_for,
)
from tests.integration.test_f11_pr4a_comparison_postgres import (
    Anomaly,
    _anomalous,
    _drain,
    _record,
    _tick,
    _world,
)
from tests.integration.test_f11_release_observation_postgres import (  # noqa: F401 - fixtures
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
from tests.m1_web_support import EVENT_TOKEN, ORIGIN, UI_PASSWORD, UI_USER, basic, call
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
    by ``tests.integration.test_f11_release_observation_postgres._target``."""
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


def test_an_anomaly_on_a_quiet_target_opens_one_incident_with_one_queued_run(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    case = _ready(owner, web, observer, catalog, bench)
    workbench = bench(case.registry())

    receipts = _receipts(workbench.release_anomaly_pass(), case)

    incident, run = _expect_created(case)
    assert run["state"] == "queued"
    assert incident["lifecycle"] == "open"
    assert incident["target_id"] == case.target_id  # same opspilot_targets row
    assert incident["related_incident_id"] is None
    assert _on_target(case.target_id) == [
        {"incident_id": incident["incident_id"], "lifecycle": "open"}
    ]
    (receipt,) = receipts
    assert receipt.disposition == "created"
    assert (receipt.incident_id, receipt.run_id) == (
        incident["incident_id"],
        run["run_id"],
    )
    assert receipt.ending_id == case.anomaly.ending_id
    assert receipt.anomaly_key == case.anomaly_key
    assert receipt.related_incident_id is None and receipt.input_id is None
    assert receipt.handoff_reason is None  # r3 G3
    assert receipt.replayed is False
    link = _link(case.oid)
    assert link["ending_id"] == case.anomaly.ending_id
    assert link["anomaly_key"] == case.anomaly_key
    assert link["related_incident_id"] is None and link["input_id"] is None
    assert link["handoff_reason"] is None  # r3 G3
    assert link["consumed_at"] is not None
    # the release itself stays anomalous: only the association is written
    assert _one(
        f"SELECT status FROM {RELEASES} WHERE observation_id=%s", (case.oid,)
    ) == {"status": "anomalous"}


def test_r1_the_incident_and_run_ids_derive_from_the_anomaly_key(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    case = _ready(owner, web, observer, catalog, bench)

    bench(case.registry()).release_anomaly_pass()

    incident, run = _expect_created(case)
    assert incident["intake_key"] == case.intake_key
    assert incident["incident_id"] == case.incident_id
    assert run["run_id"] == case.run_id


def test_the_run_input_is_v6_with_release_identity_versions_instances_and_comparison(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    case = _ready(owner, web, observer, catalog, bench)
    comparison = _comparison_row(case)
    assert comparison is not None

    bench(case.registry()).release_anomaly_pass()

    _, run = _expect_created(case)
    run_input = _run_input(run["run_id"])
    assert (
        run_input["version"]
        == INPUT_VERSION_RELEASE
        == "opspilot-investigation-input-v6"
    )
    InvestigationInput.from_json(run_input)  # this build reads its own v6 row
    # the Run is authorized for the release's target, nothing else
    assert run_input["bound_target_id"] == case.resource_uid
    assert run_input["scope_facts"]["target_ids"] == [case.resource_uid]
    fact = _text(_fact(run_input))
    record = _one(f"SELECT * FROM {RELEASES} WHERE observation_id=%s", (case.oid,))
    expected = {
        # identity (P4-05: observation_id, source, source_release_id, target five)
        "observation_id": str(case.oid),
        "source": record["source"],
        "source_release_id": case.source_release_id,
        "resource_uid": case.resource_uid,
        "cluster_uid": IDENTITY["cluster_uid"],
        "integration_id": IDENTITY["integration_id"],
        "namespace": IDENTITY["namespace"],
        "workload": WORKLOAD,
        # before/after revisions and auxiliary RS hashes
        "before image": BEFORE_IMAGES["checkout"],
        "after image": AFTER_IMAGES["checkout"],
        "before template": digest("tmpl-before"),
        "after template": digest("tmpl-after"),
        "before hash": BEFORE_HASH,
        "after hash": AFTER_HASH,
        # the ending, its sample and the policy it was judged under
        "ending_id": str(case.anomaly.ending_id),
        "anomaly_key": case.anomaly_key,
        "sample_id": str(case.anomaly.sample_id),
        "policy revision": case.policy_revision,
        # release_evaluation summary
        "verdict": "degraded",
        # the PR-4a comparison record
        "comparison status": comparison["status"],
    }
    missing = {name: value for name, value in expected.items() if value not in fact}
    assert not missing, missing
    assert any(form in fact for form in _instants(record["released_at"])), (
        "released_at is not in the release fact"
    )
    # new-instance evidence: every instance of the anomaly sample
    sample = _one(
        "SELECT release_evaluation FROM opspilot_observation_samples WHERE sample_id=%s",
        (case.anomaly.sample_id,),
    )
    uids = [i["pod_uid"] for i in sample["release_evaluation"]["instances"]]
    assert uids and all(uid in fact for uid in uids)
    # the comparison's own signals (before_service / before_old / after_*)
    for scope in ("before_service", "before_old", "after_service", "after_new"):
        assert scope in fact, scope


def test_p4_05_the_model_sees_the_release_fact_once_behind_the_boundary(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    case = _ready(owner, web, observer, catalog, bench)

    bench(case.registry()).release_anomaly_pass()

    _, run = _expect_created(case)
    run_input = InvestigationInput.from_json(_run_input(run["run_id"]))
    messages, _ = initial_messages(run_input, evidence_context=None)
    assert RELEASE_CONTEXT_BOUNDARY
    labelled = [
        m
        for m in messages
        if m["role"] == "user"
        and str(m["content"]).startswith(RELEASE_CONTEXT_BOUNDARY)
    ]
    assert len(labelled) == 1, [str(m["content"])[:80] for m in messages]
    assert str(case.oid) in labelled[0]["content"]
    assert case.source_release_id in labelled[0]["content"]
    # R5: the question is a fixed template; untrusted release fields stay in
    # the labelled message only
    question = messages[1]["content"]
    assert messages[1]["role"] == "user"
    assert case.source_release_id not in question
    assert case.source_release_id not in run_input.question
    assert WORKLOAD in run_input.question
    assert IDENTITY["namespace"] in run_input.question


def test_r3_the_release_run_frame_ends_at_consumption_and_anchors_at_released_at(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    case = _ready(owner, web, observer, catalog, bench)
    before = _db_now().replace(microsecond=0)

    bench(case.registry()).release_anomaly_pass()

    after = _db_now()
    _, run = _expect_created(case)
    policies = (_run_input(run["run_id"])["evidence_context"] or {}).get(
        "time_policies"
    )
    assert policies, "the release Run has no time policy"
    policy = policies[0]
    start = datetime.fromisoformat(policy["window"]["start"])
    end = datetime.fromisoformat(policy["window"]["end"])
    assert end - start == FRAME
    assert before <= end <= after
    released = case.anomaly.released_at.astimezone(timezone.utc).replace(microsecond=0)
    assert datetime.fromisoformat(policy["anchor"]) == released
    default = policy["default_query_window"]
    assert datetime.fromisoformat(default["start"]) == released - LOOKBACK
    assert datetime.fromisoformat(default["end"]) == end
    # the frame end is the recorded consumption instant (DB clock)
    consumed = _link(case.oid)["consumed_at"]
    assert end == consumed.astimezone(timezone.utc).replace(microsecond=0)


# ===========================================================================
# 1b. idempotent: repeat, concurrent, failed transaction (item 1)
# ===========================================================================


def test_a_second_pass_creates_no_second_incident_run_or_link(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    case = _ready(owner, web, observer, catalog, bench)
    workbench = bench(case.registry())
    workbench.release_anomaly_pass()
    incident, run = _expect_created(case)

    again = _receipts(workbench.release_anomaly_pass(), case)

    assert all(r.replayed is True for r in again), again
    assert _expect_created(case) == (incident, run)
    assert len(_on_target(case.target_id)) == 1


def test_two_concurrent_passes_create_one_incident_run_and_link(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    case = _ready(owner, web, observer, catalog, bench)
    benches = [bench(case.registry()), bench(case.registry())]
    barrier = threading.Barrier(2)
    results: list[Any] = [None, None]

    def run(index: int) -> None:
        barrier.wait()
        try:
            results[index] = list(benches[index].release_anomaly_pass())
        except Exception as exc:  # a loser may fail; the state must not
            results[index] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    benches[0].release_anomaly_pass()  # a later pass settles any loser

    incident, run_row = _expect_created(case)
    assert len(_on_target(case.target_id)) == 1
    mine = [
        r
        for result in results
        if isinstance(result, list)
        for r in _receipts(result, case)
    ]
    assert mine, results
    assert {r.incident_id for r in mine} == {incident["incident_id"]}
    assert [r.replayed for r in mine].count(False) <= 1


def test_a_failed_consumption_rolls_back_whole_and_the_next_pass_retries(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    """Section 4: a storage failure rolls back the whole transaction; the next
    pass retries. Fault: the link insert fails for this observation only."""
    case = _ready(owner, web, observer, catalog, bench)
    workbench = bench(case.registry())
    function = f"pr4b_fault_{uuid4().hex[:8]}"
    with psycopg.connect(DSN) as conn:
        conn.execute(
            f"CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ "
            f"BEGIN IF NEW.observation_id = '{case.oid}'::uuid THEN "
            "RAISE EXCEPTION 'injected link failure'; END IF; RETURN NEW; END $$"
        )
        conn.execute(
            f"CREATE TRIGGER {function} BEFORE INSERT ON {LINKS} "
            f"FOR EACH ROW EXECUTE FUNCTION {function}()"
        )
    try:
        try:
            workbench.release_anomaly_pass()
        except Exception:
            pass  # the pass may report the failure; nothing may be committed
        assert _by_intake(case.intake_key) == []
        assert _on_target(case.target_id) == []
        assert _links(case.oid) == []
        assert _release_incident(case.oid) is None
        assert [e.observation_id for e in web.pending_anomaly_endings(limit=200)].count(
            case.oid
        ) == 1
    finally:
        with psycopg.connect(DSN) as conn:
            conn.execute(f"DROP TRIGGER {function} ON {LINKS}")
            conn.execute(f"DROP FUNCTION {function}()")

    workbench.release_anomaly_pass()

    _expect_created(case)


# ===========================================================================
# 1c. the comparison record first (P4-01-A, item 11)
# ===========================================================================


def test_p4_01_consumption_waits_for_the_comparison_record(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    _settle(observer, bench, web)
    case = _case(owner, web, observer, catalog)
    assert _comparison_row(case) is None
    workbench = bench(case.registry())

    assert _receipts(workbench.release_anomaly_pass(), case) == []
    assert _links(case.oid) == [] and _release_incident(case.oid) is None
    assert _by_intake(case.intake_key) == []

    _compare(observer, case)
    workbench.release_anomaly_pass()

    _expect_created(case)


#: r3 G9 (lead 2026-10-22): without a comparison record the consumer waits
#: until DB clock >= ending.recorded_at + 510 s, then consumes with
#: ``{status: unavailable, reason_codes: [COMPARISON_NOT_RECORDED]}``.
COMPARISON_WAIT = timedelta(seconds=510)


def test_g9_without_a_record_the_ending_is_consumed_after_the_wait_bound(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    _settle(observer, bench, web)
    case = _case(owner, web, observer, catalog)
    assert _comparison_row(case) is None
    recorded = _one(
        "SELECT recorded_at FROM opspilot_observation_endings WHERE ending_id=%s",
        (case.anomaly.ending_id,),
    )["recorded_at"]
    _tamper(
        "UPDATE opspilot_observation_endings SET recorded_at=%s WHERE ending_id=%s",
        (recorded - COMPARISON_WAIT - timedelta(seconds=30), case.anomaly.ending_id),
    )

    bench(case.registry()).release_anomaly_pass()

    _, run = _expect_created(case)
    assert _comparison_row(case) is None  # the consumer files no PR-4a record
    fact = _text(_fact(_run_input(run["run_id"])))
    assert "COMPARISON_NOT_RECORDED" in fact
    assert "unavailable" in fact


def test_item_11_an_unavailable_comparison_does_not_block_and_the_snapshot_says_so(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    _settle(observer, bench, web)
    case = _case(owner, web, observer, catalog)
    failing = _world(fail_all=True)
    for _ in range(10):
        _tick(observer, failing)
        if _comparison_row(case) is not None:
            break
        time.sleep(0.2)
    comparison = _comparison_row(case)
    assert comparison is not None and comparison["status"] == "unavailable", comparison

    bench(case.registry()).release_anomaly_pass()

    _, run = _expect_created(case)
    assert "unavailable" in _text(_fact(_run_input(run["run_id"])))


# ===========================================================================
# 2. an open incident on the target takes one event (item 2, U8, R4)
# ===========================================================================


@pytest.mark.parametrize("lifecycle", ["open", "observing_recovery", "resolved"])
def test_an_unclosed_incident_on_the_target_takes_one_event_and_nothing_else(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
    lifecycle: str,
) -> None:
    _settle(observer, bench, web)
    case = _case(owner, web, observer, catalog)
    existing = _seed(owner, case.target_id, lifecycle=lifecycle)
    before = _incident(existing)
    runs_before = _runs(existing)
    _compare(observer, case)
    workbench = bench(case.registry())

    receipts = _receipts(workbench.release_anomaly_pass(), case)
    workbench.release_anomaly_pass()  # repeat: still one event

    # no second investigation
    assert _by_intake(case.intake_key) == []
    assert {r["incident_id"] for r in _on_target(case.target_id)} == {existing}
    assert _runs(existing) == runs_before  # same Run, state, watermark (F2 合同:34)
    # human control and lifecycle untouched
    after = _incident(existing)
    for column in (
        "lifecycle",
        "mode",
        "control_generation",
        "current_run_id",
        "state",
        "priority",  # r3: B1's LEAST escalation is for alerts only
    ):
        assert after[column] == before[column], column
    (event,) = _events(existing)
    link = _link(case.oid)
    assert link["disposition"] == "attached"
    assert link["incident_id"] == existing
    assert link["run_id"] is None and link["related_incident_id"] is None
    assert link["input_id"] == event["input_id"]
    assert _release_incident(case.oid) == existing
    (receipt,) = receipts
    assert receipt.disposition == "attached"
    assert (receipt.incident_id, receipt.input_id, receipt.run_id) == (
        existing,
        event["input_id"],
        None,
    )


def test_r4_the_attached_event_is_a_bounded_release_observation_event(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    _settle(observer, bench, web)
    case = _case(owner, web, observer, catalog)
    existing = _seed(owner, case.target_id)
    _compare(observer, case)

    bench(case.registry()).release_anomaly_pass()

    (event,) = _events(existing)
    assert event["kind"] == "event"
    assert event["actor"] == "release_observation"
    assert event["input_id"] == case.event_id(existing)  # R1
    content = event["content"]
    assert content["channel"] == "release_observation"
    assert isinstance(content["text"], str) and content["text"].strip()
    # a "bounded" summary: the contract gives no number (gap G6); the
    # model-visible text must at least stay well under the v6 fact bound
    assert len(content["text"]) <= 8192


# ===========================================================================
# 3-4. closed incidents and several candidates (items 3-4, P4-04 = C3)
# ===========================================================================


def test_only_closed_incidents_open_a_new_related_incident_without_reopening(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    _settle(observer, bench, web)
    case = _case(owner, web, observer, catalog)
    now = _db_now()
    older = _seed(
        owner, case.target_id, lifecycle="closed", created_at=now - timedelta(hours=3)
    )
    latest = _seed(
        owner, case.target_id, lifecycle="closed", created_at=now - timedelta(hours=1)
    )
    other = _record(owner, web)  # another target (pending release only)
    elsewhere = _seed(owner, other.target_id, lifecycle="closed", created_at=now)
    closed_before = {i: _incident(i) for i in (older, latest)}
    _compare(observer, case)

    bench(case.registry()).release_anomaly_pass()

    (created,) = _by_intake(case.intake_key)
    incident = _incident(created["incident_id"])
    assert incident["related_incident_id"] == latest
    assert incident["related_incident_id"] != elsewhere
    (run,) = _runs(incident["incident_id"])
    assert run["state"] == "queued"
    link = _link(case.oid)
    assert link["disposition"] == "created_related"
    assert (link["incident_id"], link["related_incident_id"], link["run_id"]) == (
        incident["incident_id"],
        latest,
        run["run_id"],
    )
    assert _release_incident(case.oid) == incident["incident_id"]
    for incident_id, before in closed_before.items():
        assert _incident(incident_id) == before  # never reopened or touched
        assert _events(incident_id) == []


def test_several_candidates_pick_the_latest_unclosed_on_the_same_target_only(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    _settle(observer, bench, web)
    case = _case(owner, web, observer, catalog)
    other = _record(owner, web)  # another target (pending release only)
    now = _db_now()
    old_open = _seed(owner, case.target_id, created_at=now - timedelta(hours=3))
    chosen = _seed(
        owner, case.target_id, lifecycle="resolved", created_at=now - timedelta(hours=2)
    )
    newer_closed = _seed(owner, case.target_id, lifecycle="closed", created_at=now)
    handoff = _seed(owner, None, created_at=now)  # target NULL: never a candidate
    other_target = _seed(owner, other.target_id, created_at=now)
    _compare(observer, case)

    bench(case.registry()).release_anomaly_pass()

    link = _link(case.oid)
    assert (link["disposition"], link["incident_id"]) == ("attached", chosen)
    assert len(_events(chosen)) == 1
    for untouched in (old_open, newer_closed, handoff, other_target):
        assert _events(untouched) == [], untouched
    assert _by_intake(case.intake_key) == []


def test_a_created_at_tie_picks_the_larger_incident_id(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    _settle(observer, bench, web)
    case = _case(owner, web, observer, catalog)
    moment = _db_now() - timedelta(minutes=5)
    first = _seed(owner, case.target_id, created_at=moment)
    second = _seed(owner, case.target_id, created_at=moment)
    _compare(observer, case)

    bench(case.registry()).release_anomaly_pass()

    assert _link(case.oid)["incident_id"] == max(first, second)


# ===========================================================================
# 5. nothing for releases without an anomaly ending (item 5)
# ===========================================================================


def test_healthy_cancelled_and_pending_releases_produce_no_work(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    _settle(observer, bench, web)
    # healthy: three contiguous sufficient samples (as the PR-2a tests do)
    healthy = _observing(owner, web, catalog, released_ago=2000)
    authorized_at = healthy.release.record.released_at + timedelta(seconds=500)
    _set_authorized_at(healthy.session_id, authorized_at)
    pods = pods_for(AFTER_IMAGES)
    for offset in (300, 600, 900):
        window_end = authorized_at + timedelta(seconds=offset)
        lease = _claim(observer, healthy.session_id)
        context = observer.release_context(healthy.session_id)
        texts = planned_texts(context, window_end, [p.uid for p in pods])
        evidence = (
            EvidenceBuilder(window_end=window_end, texts=texts)
            .sufficient(pods)
            .evidence()
        )
        receipt = observer.submit_release_sample(lease, evidence)
    assert receipt.release_status == "healthy", receipt
    # cancelled while observing
    cancelled = _observing(owner, web, catalog)
    assert _control(web, cancelled.oid, "cancel", 0).outcome == "applied"
    # pending: never authorized
    pending = _record(owner, web)
    subjects = [healthy.release, cancelled.release, pending]
    statuses = {
        _one(f"SELECT status FROM {RELEASES} WHERE observation_id=%s", (s.oid,))[
            "status"
        ]
        for s in subjects
    }
    assert statuses == {"healthy", "cancelled", "pending"}, statuses
    registry: dict[str, dict[str, str]] = {}
    for subject in subjects:
        uid = _one(
            "SELECT resource_uid FROM opspilot_targets WHERE target_id=%s",
            (subject.target_id,),
        )["resource_uid"]
        registry[uid] = _registry_entry()
    workbench = bench(registry)

    for _ in range(3):  # repeated passes over healthy releases stay idle
        receipts = workbench.release_anomaly_pass()
        assert not [
            r for r in receipts if r.observation_id in {s.oid for s in subjects}
        ]

    for subject in subjects:
        assert _links(subject.oid) == []
        assert _release_incident(subject.oid) is None
        assert _on_target(subject.target_id) == []


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


@pytest.mark.parametrize("registry", ["missing", "mismatch"])
def test_r7_an_unresolvable_target_is_handed_off_without_a_run(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
    registry: str,
) -> None:
    case = _ready(owner, web, observer, catalog, bench)
    _damage_target_row(case, registry)
    # the web identities file still lists the target unchanged (not R7's registry)
    workbench = bench(case.registry())

    receipts = _receipts(workbench.release_anomaly_pass(), case)

    link = _link(case.oid)
    assert link["disposition"] == "handoff_created", link
    assert link["run_id"] is None
    incident = _incident(link["incident_id"])
    assert incident["current_run_id"] is None
    assert _runs(incident["incident_id"]) == []
    assert _release_incident(case.oid) == incident["incident_id"]
    (receipt,) = receipts
    assert (receipt.disposition, receipt.run_id) == ("handoff_created", None)
    # r3 G3: the reason is on the link row and on the receipt
    assert link["handoff_reason"] == "RELEASE_TARGET_UNRESOLVED"
    assert receipt.handoff_reason == "RELEASE_TARGET_UNRESOLVED"
    # not pending any more: a later pass adds nothing
    later = _receipts(workbench.release_anomaly_pass(), case)
    assert all(r.replayed for r in later), later
    assert len(_links(case.oid)) == 1
    assert _links(case.oid)[0]["incident_id"] == incident["incident_id"]


# ===========================================================================
# 7. permissions (item 7, P4-03, section 4 grants)
# ===========================================================================


def _grants(role: str) -> dict[str, frozenset[str]]:
    with psycopg.connect(DSN) as conn:
        return catalog_grants(conn, role)


def _columns_of(grant: str) -> set[str]:
    return set(grant[grant.index("(") + 1 : -1].split(",")) if "(" in grant else set()


def test_link_table_grants_web_select_insert_and_nobody_else() -> None:
    web_grants = _grants(WEB_ROLE)[LINKS]
    assert "SELECT" in web_grants
    assert any(g == "INSERT" or g.startswith("INSERT(") for g in web_grants), web_grants
    assert not any(
        g.split("(")[0] in {"UPDATE", "DELETE", "TRUNCATE"} for g in web_grants
    )
    assert _grants(WORKER_ROLE)[LINKS] == frozenset()
    assert _grants(OBSERVER_ROLE)[LINKS] == frozenset()
    # the release row: web may now update incident_id, worker nothing
    release_web = _grants(WEB_ROLE)[RELEASES]
    assert any(
        g.startswith("UPDATE(") and "incident_id" in _columns_of(g) for g in release_web
    ), release_web
    for role in (WORKER_ROLE, OBSERVER_ROLE):
        assert not any(
            g == "UPDATE"
            or (g.startswith("UPDATE(") and "incident_id" in _columns_of(g))
            for g in _grants(role)[RELEASES]
        ), role


def test_the_declared_grant_matrices_match_for_the_link_table() -> None:
    assert "SELECT" in RUNTIME_GRANTS[WEB_ROLE][LINKS]
    assert any(g.startswith("INSERT") for g in RUNTIME_GRANTS[WEB_ROLE][LINKS])
    assert RUNTIME_GRANTS[WORKER_ROLE][LINKS] == frozenset()
    assert not OBSERVER_GRANTS.get(LINKS)
    assert any(
        g.startswith("UPDATE(") and "incident_id" in _columns_of(g)
        for g in RUNTIME_GRANTS[WEB_ROLE][RELEASES]
    )


def test_observer_and_worker_cannot_write_the_association(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    observer_dsn: str,
    worker_dsn: str,
) -> None:
    anomaly = _anomalous(owner, web, observer, catalog)
    target = _one(
        f"SELECT target_id FROM {RELEASES} WHERE observation_id=%s", (anomaly.oid,)
    )
    incident = _seed(owner, target["target_id"])
    for dsn in (observer_dsn, worker_dsn):
        _refused(
            dsn,
            f"UPDATE {RELEASES} SET incident_id=%s WHERE observation_id=%s",
            (incident, anomaly.oid),
        )
        _refused(dsn, f"INSERT INTO {LINKS} DEFAULT VALUES")
        _refused(dsn, f"SELECT count(*) FROM {LINKS}")
        _refused(dsn, f"DELETE FROM {LINKS}")
    assert _release_incident(anomaly.oid) is None


def test_web_cannot_link_a_non_anomalous_release(
    owner: DurableStore, web: ReleaseStore, web_dsn: str
) -> None:
    pending = _record(owner, web)
    incident = _seed(owner, pending.target_id)
    _refused(
        web_dsn,
        f"UPDATE {RELEASES} SET incident_id=%s WHERE observation_id=%s",
        (incident, pending.oid),
    )
    assert _release_incident(pending.oid) is None


def test_web_cannot_link_an_anomalous_release_without_a_link_row_in_the_transaction(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    web_dsn: str,
) -> None:
    anomaly = _anomalous(owner, web, observer, catalog)
    target = _one(
        f"SELECT target_id FROM {RELEASES} WHERE observation_id=%s", (anomaly.oid,)
    )
    incident = _seed(owner, target["target_id"])
    _refused(
        web_dsn,
        f"UPDATE {RELEASES} SET incident_id=%s WHERE observation_id=%s",
        (incident, anomaly.oid),
    )
    assert _release_incident(anomaly.oid) is None


def test_web_cannot_relink_an_associated_release_or_change_other_columns(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
    web_dsn: str,
) -> None:
    case = _ready(owner, web, observer, catalog, bench)
    bench(case.registry()).release_anomaly_pass()
    incident, _ = _expect_created(case)
    other = _seed(owner, case.target_id)
    statement = f"UPDATE {RELEASES} SET {{}} WHERE observation_id=%s"
    for change, params in (
        ("incident_id=%s", (other,)),
        ("incident_id=NULL", ()),
        ("status='healthy'", ()),
        ("source_release_id='rewritten'", ()),
        ("incident_id=%s, status='healthy'", (other,)),
    ):
        _refused(
            web_dsn,
            statement.format(change),
            (*params, case.oid),
            (psycopg.errors.InsufficientPrivilege,),
        )
    assert _release_incident(case.oid) == incident["incident_id"]
    for change in ("UPDATE {t} SET anomaly_key=anomaly_key", "DELETE FROM {t}"):
        _refused(web_dsn, change.format(t=LINKS))
    assert len(_links(case.oid)) == 1


# ===========================================================================
# 8. suspension does not stop intake (item 8, R10)
# ===========================================================================


def test_a_suspended_target_gets_a_paused_incident_and_run_claimable_after_lifting(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    """Item 8 as amended (user 2026-10-22): during suspension the incident and
    its Run are created ``paused`` (as alert intake and the suspend write
    path), no query or model call before lifting; after lifting the Run can
    be claimed."""
    case = _ready(owner, web, observer, catalog, bench)
    generation = _one(
        "SELECT generation FROM opspilot_target_suspensions WHERE target_id=%s",
        (case.target_id,),
    )["generation"]
    current = owner.set_target_suspension(
        case.target_id, True, expected_generation=generation, actor="test:pr4b"
    )
    lifted = False
    try:
        workbench = bench(case.registry())
        workbench.release_anomaly_pass()

        incident, run = _expect_created(case)
        assert run["state"] == "paused", run
        assert incident["state"] == "paused", incident
        assert incident["lifecycle"] == "open"
        assert incident["incident_id"] not in owner.claimable_incidents(limit=1000)

        # lift through the public controls: the operator's target gate, then
        # the incident's resume (a Run parked by suspension is a human's to
        # resume, as for every other incident)
        workbench.suspension(
            scope="target",
            target_id=case.resource_uid,
            suspended=False,
            expected_generation=current,
            idempotency_key=f"lift-{uuid4()}",
            actor_id="operator:test",
        )
        lifted = True
        workbench.control(
            incident["incident_id"],
            actor_id="operator:test",
            action="resume",
            expected_generation=_generation(incident["incident_id"]),
            idempotency_key=f"resume-{uuid4()}",
        )

        (after,) = _runs(incident["incident_id"])
        assert after["run_id"] == run["run_id"]
        assert after["state"] == "queued", after
        assert incident["incident_id"] in owner.claimable_incidents(limit=1000)
    finally:
        if not lifted:
            owner.set_target_suspension(
                case.target_id, False, expected_generation=current, actor="test:pr4b"
            )


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


def test_a_new_run_after_cancel_keeps_the_v6_release_fact(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    case, workbench, incident, original = _created_case(
        owner, web, observer, catalog, bench
    )

    _cancel_and_new_run(workbench, incident["incident_id"])

    successor = _successor_input(incident["incident_id"], case.run_id)
    assert successor["version"] == INPUT_VERSION_RELEASE
    assert _fact(successor) == _fact(original)


def test_a_follow_up_after_timeout_keeps_the_v6_release_fact(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    case, workbench, incident, original = _created_case(
        owner, web, observer, catalog, bench
    )
    with owner.transaction() as conn:
        conn.execute(
            "UPDATE opspilot_runs SET deadline=clock_timestamp()-interval '1 second' "
            "WHERE run_id=%s",
            (case.run_id,),
        )
    owner.sweep_expired_runs(incident_id=incident["incident_id"])

    workbench.control(
        incident["incident_id"],
        actor_id="operator:test",
        action="follow_up",
        expected_generation=_generation(incident["incident_id"]),
        idempotency_key=f"follow-{uuid4()}",
        text="Did the new version cause it?",
    )

    successor = _successor_input(incident["incident_id"], case.run_id)
    assert successor["version"] == INPUT_VERSION_RELEASE
    assert _fact(successor) == _fact(original)


def test_r6_a_fresh_rebuild_restores_the_release_fact_and_anchor_from_the_link(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    case, workbench, incident, original = _created_case(
        owner, web, observer, catalog, bench
    )
    with psycopg.connect(DSN) as conn:  # a Run row without its snapshot
        conn.execute(
            "UPDATE opspilot_runs SET input=NULL WHERE run_id=%s", (case.run_id,)
        )

    _cancel_and_new_run(workbench, incident["incident_id"])

    successor = _successor_input(incident["incident_id"], case.run_id)
    assert successor["version"] == INPUT_VERSION_RELEASE
    assert _fact(successor) == _fact(original)
    first = original["evidence_context"]["time_policies"][0]
    again = successor["evidence_context"]["time_policies"][0]
    for key in ("window", "anchor", "default_query_window"):
        assert again[key] == first[key], key


@pytest.mark.parametrize(
    "damage", ["link_deleted", "link_anomaly_key", "release_unlinked"]
)
def test_r6_a_fresh_rebuild_from_damaged_release_records_is_incompatible_state(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
    damage: str,
) -> None:
    case, workbench, incident, _ = _created_case(owner, web, observer, catalog, bench)
    with psycopg.connect(DSN) as conn:
        conn.execute(
            "UPDATE opspilot_runs SET input=NULL WHERE run_id=%s", (case.run_id,)
        )
    if damage == "link_deleted":
        _tamper(f"DELETE FROM {LINKS} WHERE observation_id=%s", (case.oid,))
    elif damage == "link_anomaly_key":
        _tamper(
            f"UPDATE {LINKS} SET anomaly_key=%s WHERE observation_id=%s",
            (f"tampered:{uuid4()}", case.oid),
        )
    else:
        _tamper(
            f"UPDATE {RELEASES} SET incident_id=NULL WHERE observation_id=%s",
            (case.oid,),
        )
    try:
        workbench.control(
            incident["incident_id"],
            actor_id="operator:test",
            action="cancel",
            expected_generation=_generation(incident["incident_id"]),
            idempotency_key=f"cancel-{uuid4()}",
        )
        runs_before = _runs(incident["incident_id"])

        with pytest.raises(WorkbenchError) as refused:
            workbench.control(
                incident["incident_id"],
                actor_id="operator:test",
                action="new_run",
                expected_generation=_generation(incident["incident_id"]),
                idempotency_key=f"new-run-{uuid4()}",
            )

        assert refused.value.code == "INCOMPATIBLE_STATE"
        assert _runs(incident["incident_id"]) == runs_before
    finally:
        if damage == "release_unlinked":  # not pending again for later tests
            _tamper(
                f"UPDATE {RELEASES} SET incident_id=%s WHERE observation_id=%s",
                (incident["incident_id"], case.oid),
            )


# ===========================================================================
# 10. replay checks the association (item 10, R9)
# ===========================================================================


def test_r9_replay_is_consistent_after_consumption(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
    history: ObservationStore,
) -> None:
    case, _, _, _ = _created_case(owner, web, observer, catalog, bench)

    replay = history.replay_session(case.anomaly.session_id)

    assert replay.consistent, replay.integrity


@pytest.mark.parametrize(
    "damage", ["link_ending", "link_anomaly_key", "link_incident", "release_incident"]
)
def test_r9_a_tampered_association_replays_link_mismatch(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
    history: ObservationStore,
    damage: str,
) -> None:
    case, _, _, _ = _created_case(owner, web, observer, catalog, bench)
    stranger = _seed(owner, case.target_id)
    if damage == "link_ending":
        # another release's anomaly ending, not linked anywhere
        other = _anomalous(owner, web, observer, catalog)
        _tamper(
            f"UPDATE {LINKS} SET ending_id=%s WHERE observation_id=%s",
            (other.ending_id, case.oid),
        )
    elif damage == "link_anomaly_key":
        _tamper(
            f"UPDATE {LINKS} SET anomaly_key=%s WHERE observation_id=%s",
            (f"tampered:{uuid4()}", case.oid),
        )
    elif damage == "link_incident":
        _tamper(
            f"UPDATE {LINKS} SET incident_id=%s WHERE observation_id=%s",
            (stranger, case.oid),
        )
    else:
        _tamper(
            f"UPDATE {RELEASES} SET incident_id=%s WHERE observation_id=%s",
            (stranger, case.oid),
        )

    try:
        replay = history.replay_session(case.anomaly.session_id)

        assert not replay.consistent
        assert "LINK_MISMATCH" in replay.integrity, replay.integrity
    finally:
        if damage == "link_ending":  # free the other ending for later passes
            _tamper(
                f"UPDATE {LINKS} SET ending_id=%s WHERE observation_id=%s",
                (case.anomaly.ending_id, case.oid),
            )


# ===========================================================================
# 12. no release gate writes: no outbound call from the consumer (item 12)
# ===========================================================================


def test_item_12_a_pass_makes_no_outbound_call_or_subprocess(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PostgreSQL goes through libpq (C), so any Python-level socket or
    process launched during the pass is a call to something else
    (Kubernetes, CI, traffic policy, Prometheus or a model)."""
    case = _ready(owner, web, observer, catalog, bench)
    workbench = bench(case.registry())
    attempts: list[Any] = []

    def refuse_connect(self: socket.socket, address: Any) -> None:
        attempts.append(("connect", address))
        raise OSError("outbound connection refused by the test")

    def refuse_process(*args: Any, **kwargs: Any) -> None:
        attempts.append(("process", args))
        raise OSError("subprocess refused by the test")

    monkeypatch.setattr(socket.socket, "connect", refuse_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse_connect)
    monkeypatch.setattr(subprocess, "Popen", refuse_process)

    workbench.release_anomaly_pass()

    monkeypatch.undo()
    assert attempts == []
    _expect_created(case)


# ===========================================================================
# the release incident is an ordinary incident on the web pages
# ===========================================================================


def test_the_release_incident_page_renders(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
) -> None:
    case, workbench, incident, _ = _created_case(owner, web, observer, catalog, bench)
    auth = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager"},
        auth_revision="f11-pr4b-acceptance",
        allowed_origins=frozenset({ORIGIN}),
    )
    clock = DurableClock(owner)
    app = create_app(workbench, Authenticator(auth), clock)

    response = call(
        app, "GET", f"/incidents/{incident['incident_id']}", headers=basic()
    )

    assert response.status == 200, response.text
    assert str(incident["incident_id"]) in response.text


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
