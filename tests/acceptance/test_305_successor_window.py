"""#305: the authorization frame of a Run that succeeds an alert Run after a
re-fired (attached) episode.

Independent acceptance tests written from
``docs/tasks/2026-10-24-305-successor-window-contract.md`` (user decision
2026-10-22: W1-B, W2-A, W3-A; R1-R7 reversible defaults) section 3 items
1-8 and the F2 task record "#305 后继 Run 授权窗口合同（r1）". The
implementation branch was not read.

Observable surface only: the Alertmanager webhook, the web control route
(``cancel`` / ``new_run`` / ``follow_up`` / ``pause`` / ``resume``), the
committed Run rows read back as data (``opspilot_runs``), the public alert
projection (``alert_intake_records``) and the fixture worker driven through
the product runner (to publish a conclusion or hand a Run off). No internal
call order is asserted. Item 4 (the gateway) and W3-A (a release attachment)
are in ``tests/integration/test_305_successor_window_postgres.py``.

Assumptions where the contract leaves a name or a value open:

* S1 "创建时的 DB 时刻" is read as ``deadline - run_seconds`` of the
  successor Run row (contract item 2 names this reading) and compared with
  the frame end at +-1 s; the frame end is whole UTC seconds (J1).
* S2 the successor's ``alert_starts_at`` scope fact is the attached
  episode's: ``original`` is its ``startsAt`` text as sent, ``anchor`` its
  normalized instant, ``adjusted`` ``None`` (R1 "锚点取它的
  starts_at/starts_at_raw", R2 "替换 scope_facts.alert_starts_at").
* S3 R2 "不加新键": the set of keys of ``time_policies[0]`` and of
  ``scope_facts`` is the first Run's; ``anchor_rule`` stays
  ``alert_starts_at`` and ``reference_rule`` is unchanged.
* S4 item 6 "重复投递" is a replay of the first (created) episode and a
  repeat of an attached episode that arrives *before* the successor's frame
  end. A repeat of an already-framed attached episode arriving *after* a
  re-framed successor is a contract gap (R1 counts it as the latest attached
  firing, item 6 says repeats do not re-frame); it is not asserted here.
* S5 item 7 (R5): with a NULL input the fresh fallback re-frames whenever an
  attached firing exists ("有附加就按 B 取框").
* S6 item 8: fixture revision is pinned to ``fixture-5``.  The OTel schema
  revision is derived from the public schema inputs and is therefore checked
  for shape and cross-interface agreement rather than pasted here.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.acceptance import alert_intake_records
from opspilot.investigation.loop import ModelError, investigation_versions
from opspilot.persistence import DurableStore
from opspilot.tools import otel_demo
from opspilot.tools.fixture import TOOL_SCHEMA_REVISION as FIXTURE_REVISION
from opspilot.tools.fixture import fixture_face, fixture_versions
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
from scripts.m0.postgres_lab import DSN
from tests.m1_investigation_support import reply, report_from_transcript
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
    bearer,
    post_form,
    post_json,
    same_origin,
)

_PG = os.environ.get("M1_DURABLE_POSTGRES") == "1"
pg = pytest.mark.skipif(not _PG, reason="explicit PG opt-in: M1_DURABLE_POSTGRES=1")

TARGET = "checkout-prod"
ENTRY = {
    "integration_id": "m0-otel-20260909",
    "cluster_uid": "opspilot-m1",
    "namespace": "otel-demo",
    "workload": "checkout",
    "health_profile_id": "otel-demo-checkout",
    "match": {
        "version": 1,
        "labels": {"namespace": "otel-demo", "service": "checkout"},
    },
}
RUN_SECONDS = 600
FRAME = timedelta(hours=24)
LOOKBACK = timedelta(hours=1)
ALERT_INPUT_VERSION = "opspilot-investigation-input-v4"


# -- fixtures ----------------------------------------------------------------


@pytest.fixture(scope="module")
def database():
    if not _PG:
        pytest.skip("explicit PG opt-in: M1_DURABLE_POSTGRES=1")
    name = f"t305_window_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    dsn = make_conninfo(DSN, dbname=name)
    try:
        schema.migrate(dsn, pg_dump=os.environ.get("OPSPILOT_PG_DUMP", "pg_dump"))
        yield dsn
    finally:
        from opspilot.persistence import close_pools

        close_pools()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


class Env:
    def __init__(self, dsn, app, store, events, evidence):
        self.dsn, self.app, self.store = dsn, app, store
        self.events, self.evidence = events, evidence


@pytest.fixture
def env(database, tmp_path, monkeypatch):
    path = tmp_path / "target-identities.json"
    path.write_text(json.dumps({TARGET: ENTRY}), encoding="utf-8")
    monkeypatch.setenv("OPSPILOT_TARGET_IDENTITIES", str(path))
    store = DurableStore(database)
    store.install()
    events = DurableEventLog(store)
    events.install()
    evidence = DurableEvidenceStore(store)
    evidence.install()
    ledger = DurableWebLedger(store)
    ledger.install()
    # The fixture profile, so the product runner can publish or hand off a
    # Run without any network (items 3 and 5).
    workbench = Workbench(
        incidents=DurableIncidentStore(store),
        events=events,
        evidence=evidence,
        ledger=ledger,
        run_versions=fixture_versions(),
        tool_face=fixture_face(),
        run_seconds=RUN_SECONDS,
        targets=MappingTargetRegistry.from_file(str(path)),
    )
    auth = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager"},
        auth_revision="t305-window-acceptance-v1",
        allowed_origins=frozenset({ORIGIN}),
    )
    app = create_app(workbench, Authenticator(auth), DurableClock(store))
    try:
        yield Env(database, app, store, events, evidence)
    finally:
        store.close()


# -- helpers -----------------------------------------------------------------


def _instant(value) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _db_now(dsn) -> datetime:
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT clock_timestamp()").fetchone()[0].astimezone(UTC)


def _alert(fingerprint, starts_at: str, status="firing"):
    return {
        "status": status,
        "labels": {
            "alertname": "CheckoutPlaceOrderErrorRatioHigh",
            "namespace": "otel-demo",
            "service": "checkout",
            "severity": "critical",
        },
        "annotations": {"summary": "checkout error ratio is high"},
        "startsAt": starts_at,
        "endsAt": "0001-01-01T00:00:00Z" if status == "firing" else starts_at,
        "fingerprint": fingerprint,
        "generatorURL": "http://prometheus:9090/graph?g0.expr=rate%28x%5B5m%5D%29",
    }


def _send(env, fingerprint, starts_at, status="firing"):
    response = post_json(
        env.app,
        "/intake/alertmanager",
        {
            "version": "4",
            "status": status,
            "alerts": [_alert(fingerprint, starts_at, status)],
        },
        headers=bearer(),
    )
    assert response.status == 200, response.text
    (item,) = response.json()["results"]
    return item


def _generation(env, incident_id) -> int:
    with psycopg.connect(env.dsn) as conn:
        return conn.execute(
            "SELECT control_generation FROM opspilot_incidents WHERE incident_id=%s",
            (UUID(str(incident_id)),),
        ).fetchone()[0]


def _control(env, incident_id, action, text=None):
    fields = {
        "action": action,
        "expected_generation": str(_generation(env, incident_id)),
        "idempotency_key": f"t305-{action}-{uuid4().hex}",
    }
    if text is not None:
        fields["text"] = text
    response = post_form(
        env.app,
        f"/incidents/{incident_id}/control",
        fields,
        headers={**basic(), **same_origin(), "accept": "application/json"},
    )
    assert response.status == 200, (action, response.status, response.text)
    return response.json()


def _runs(env, incident_id):
    """Every Run of the incident in creation order, as committed."""
    with psycopg.connect(env.dsn) as conn:
        rows = conn.execute(
            "SELECT run_id,state,deadline,versions,input,input::text AS raw "
            "FROM opspilot_runs WHERE incident_id=%s ORDER BY deadline,run_id",
            (UUID(str(incident_id)),),
        ).fetchall()
    keys = ("run_id", "state", "deadline", "versions", "input", "raw")
    return [dict(zip(keys, row, strict=True)) for row in rows]


def _deliveries(env, incident_id):
    with psycopg.connect(env.dsn) as conn:
        return alert_intake_records(conn, str(incident_id)).deliveries


def _policy(run_input):
    return run_input["evidence_context"]["time_policies"][0]


def _time_contract(run_input):
    policy = _policy(run_input)
    return (
        run_input["scope_facts"]["alert_starts_at"],
        policy["window"],
        policy["anchor"],
        policy["anchor_rule"],
        policy["default_query_window"],
    )


def _first_firing(env, minutes_ago=30):
    """A new alert incident whose first episode started ``minutes_ago``."""
    fingerprint = uuid4().hex[:16]
    starts = _text(_db_now(env.dsn) - timedelta(minutes=minutes_ago))
    created = _send(env, fingerprint, starts)
    assert created["outcome"] == "created", created
    return fingerprint, created["incident_id"]


def _refire(env, fingerprint, incident_id):
    """A new episode of the same alert, started and received strictly after
    the first Run's frame end (the first delivery's receipt)."""
    time.sleep(1.2)
    starts = _text(_db_now(env.dsn))
    time.sleep(1.1)
    attached = _send(env, fingerprint, starts)
    assert attached["outcome"] == "attached", attached
    assert attached["incident_id"] == incident_id
    return starts


def _assert_reframed(successor, first, starts_raw):
    """Contract item 2 on one successor Run row (W1-B, R1, R2)."""
    run_input = successor["input"]
    policy = _policy(run_input)
    frame_start = _instant(policy["window"]["start"])
    frame_end = _instant(policy["window"]["end"])
    created_at = _instant(successor["deadline"]) - timedelta(seconds=RUN_SECONDS)
    # S1: the frame ends at the successor's creation instant.
    assert abs((frame_end - created_at).total_seconds()) <= 1, (frame_end, created_at)
    assert frame_end.microsecond == 0
    assert frame_end - frame_start == FRAME
    # the frame moved past the first Run's
    assert frame_end > _instant(_policy(first["input"])["window"]["end"])
    anchor = _instant(starts_raw)
    assert _instant(policy["anchor"]) == anchor
    assert policy["anchor_rule"] == "alert_starts_at"
    default = policy["default_query_window"]
    assert _instant(default["start"]) == max(anchor - LOOKBACK, frame_start)
    assert _instant(default["end"]) == frame_end
    # S2: the scope fact names the attached episode.
    fact = run_input["scope_facts"]["alert_starts_at"]
    assert fact["original"] == starts_raw
    assert _instant(fact["anchor"]) == anchor
    assert fact["adjusted"] is None
    # S3 / R2: no new keys, same version (item 8).
    first_input = first["input"]
    assert set(policy) == set(_policy(first_input))
    assert set(run_input["scope_facts"]) == set(first_input["scope_facts"])
    assert policy["reference_rule"] == _policy(first_input)["reference_rule"]
    assert run_input["version"] == first_input["version"] == ALERT_INPUT_VERSION
    assert successor["versions"] == first["versions"]


# -- the scripted worker (product runner, fixture profile) -------------------


def _poll(env, incident_id, replies):
    from tests.integration.test_m1_web_worker_postgres import _worker

    loop, _ = _worker(
        env.store, env.events, env.evidence, replies, subject=UUID(str(incident_id))
    )
    return [o.status for _, o in loop.poll_once()]


def _publish(env, incident_id):
    from tests.integration.test_m1_web_worker_postgres import _tool_round

    def final(model_call):
        payload = json.loads(report_from_transcript(model_call).content)
        return reply(content=json.dumps(payload, ensure_ascii=False))

    assert _poll(env, incident_id, [_tool_round(), final]) == ["published"]


def _hand_off(env, incident_id):
    statuses = _poll(env, incident_id, [ModelError("MODEL_REJECTED")])
    assert statuses == ["handed_off"], statuses


# ===========================================================================
# item 8 without PostgreSQL: the version strings this change must not move
# ===========================================================================


def test_item8_tool_schema_and_prompt_revisions_are_unchanged():
    assert FIXTURE_REVISION == "fixture-5"
    assert otel_demo.TOOL_SCHEMA_REVISION.startswith("otel-demo-")
    assert len(otel_demo.TOOL_SCHEMA_REVISION) == len("otel-demo-") + 12
    assert fixture_versions()["tool_schema_revision"] == "fixture-5"
    assert otel_demo.otel_demo_versions()["tool_schema_revision"] == (
        otel_demo.TOOL_SCHEMA_REVISION
    )
    # R7: no handoff note or prompt constant changes.
    assert investigation_versions()["prompt_revision"] == (
        "prompt-replay-candidate-bef70649a3a8"
    )


# ===========================================================================
# item 1: no attachment -> J5 unchanged
# ===========================================================================


@pg
def test_item1_without_attachment_new_run_keeps_the_first_frame(env):
    _, incident = _first_firing(env)
    time.sleep(1.1)
    _control(env, incident, "cancel")
    _control(env, incident, "new_run")
    first, successor = _runs(env, incident)
    assert _time_contract(successor["input"]) == _time_contract(first["input"])
    assert successor["input"]["version"] == first["input"]["version"]
    assert successor["versions"] == first["versions"]


@pg
def test_item1_without_attachment_timeout_renewal_keeps_the_first_frame(env):
    _, incident = _first_firing(env)
    (first,) = _runs(env, incident)
    with psycopg.connect(env.dsn) as conn:
        conn.execute(
            "UPDATE opspilot_runs SET deadline=clock_timestamp()-interval '1 second' "
            "WHERE run_id=%s",
            (first["run_id"],),
        )
    time.sleep(1.1)
    _control(env, incident, "follow_up", "continue from the alert evidence")
    runs = _runs(env, incident)
    assert len(runs) == 2
    assert _time_contract(runs[1]["input"]) == _time_contract(runs[0]["input"])


# ===========================================================================
# item 2: firing -> attached -> cancel + new_run re-frames
# ===========================================================================


@pg
def test_item2_cancel_and_new_run_after_attached_episode_reframes(env):
    fingerprint, incident = _first_firing(env)
    starts = _refire(env, fingerprint, incident)
    _control(env, incident, "cancel")
    _control(env, incident, "new_run")
    first, successor = _runs(env, incident)
    _assert_reframed(successor, first, starts)
    attached = [d for d in _deliveries(env, incident) if d["outcome"] == "attached"]
    assert attached
    frame_end = _instant(_policy(successor["input"])["window"]["end"])
    assert frame_end >= _instant(attached[-1]["received_at"]).replace(microsecond=0)


@pg
def test_item2_r1_the_latest_received_attached_episode_is_the_anchor(env):
    fingerprint, incident = _first_firing(env)
    _refire(env, fingerprint, incident)
    later = _refire(env, fingerprint, incident)
    _control(env, incident, "cancel")
    _control(env, incident, "new_run")
    first, successor = _runs(env, incident)
    _assert_reframed(successor, first, later)


# ===========================================================================
# item 3: follow-up after a published conclusion re-frames the same way
# ===========================================================================


@pg
def test_item3_follow_up_after_conclusion_reframes(env):
    fingerprint, incident = _first_firing(env)
    _publish(env, incident)
    (first,) = _runs(env, incident)
    assert first["state"] == "completed"
    starts = _refire(env, fingerprint, incident)
    _control(
        env, incident, "follow_up", "the alert fired again; look at the new episode"
    )
    runs = _runs(env, incident)
    assert len(runs) == 2
    _assert_reframed(runs[1], runs[0], starts)


# ===========================================================================
# item 5: the same Run re-queued keeps its input byte for byte (W2-A)
# ===========================================================================


@pg
def test_item5_follow_up_on_waiting_human_run_keeps_same_run_and_input(env):
    fingerprint, incident = _first_firing(env)
    _hand_off(env, incident)
    (before,) = _runs(env, incident)
    assert before["state"] == "waiting_human"
    _refire(env, fingerprint, incident)
    _control(env, incident, "follow_up", "the alert fired again")
    (after,) = _runs(env, incident)
    assert after["run_id"] == before["run_id"]
    assert after["state"] == "queued"
    assert after["raw"] == before["raw"]


@pg
def test_item5_pause_and_resume_keep_same_run_and_input(env):
    fingerprint, incident = _first_firing(env)
    (before,) = _runs(env, incident)
    _control(env, incident, "pause")
    _refire(env, fingerprint, incident)
    _control(env, incident, "resume")
    (after,) = _runs(env, incident)
    assert after["run_id"] == before["run_id"]
    assert after["raw"] == before["raw"]


# ===========================================================================
# item 6: what does not re-frame
# ===========================================================================


@pg
def test_item6_replay_and_resolved_of_the_first_episode_do_not_reframe(env):
    fingerprint, incident = _first_firing(env)
    first_starts = _deliveries(env, incident)[0]["starts_at_raw"]
    time.sleep(1.1)
    replay = _send(env, fingerprint, first_starts)
    assert replay["outcome"] == "replayed", replay
    time.sleep(1.1)
    resolved = _send(env, fingerprint, first_starts, status="resolved")
    assert resolved["outcome"] == "resolved_attached", resolved
    time.sleep(1.1)
    _control(env, incident, "cancel")
    _control(env, incident, "new_run")
    first, successor = _runs(env, incident)
    assert _time_contract(successor["input"]) == _time_contract(first["input"])


@pg
def test_item6_attachment_before_previous_frame_end_does_not_reframe_again(env):
    fingerprint, incident = _first_firing(env)
    starts = _refire(env, fingerprint, incident)
    # S4: a repeat of the attached episode before the successor exists.
    again = _send(env, fingerprint, starts)
    assert again["outcome"] == "attached", again
    _control(env, incident, "cancel")
    _control(env, incident, "new_run")
    first, second = _runs(env, incident)
    _assert_reframed(second, first, starts)
    # After the re-framed successor: only a resolved_attached of that episode.
    time.sleep(1.1)
    done = _send(env, fingerprint, starts, status="resolved")
    assert done["outcome"] == "resolved_attached", done
    time.sleep(1.1)
    _control(env, incident, "cancel")
    _control(env, incident, "new_run")
    runs = _runs(env, incident)
    assert len(runs) == 3
    assert _time_contract(runs[2]["input"]) == _time_contract(second["input"])


# ===========================================================================
# item 7: the fresh fallback takes the same anchor (R5)
# ===========================================================================


@pg
def test_item7_fresh_fallback_with_attachment_takes_the_attached_anchor(env):
    fingerprint, incident = _first_firing(env)
    (first,) = _runs(env, incident)
    starts = _refire(env, fingerprint, incident)
    with psycopg.connect(env.dsn) as conn:
        conn.execute(
            "UPDATE opspilot_runs SET input=NULL, deadline=clock_timestamp() "
            "- interval '1 second' WHERE run_id=%s",
            (first["run_id"],),
        )
    _control(env, incident, "follow_up", "rebuild the alert run")
    runs = _runs(env, incident)
    assert len(runs) == 2 and runs[0]["input"] is None
    _assert_reframed(runs[1], first, starts)


@pg
def test_item7_fresh_fallback_without_attachment_keeps_j5(env):
    _, incident = _first_firing(env)
    (first,) = _runs(env, incident)
    time.sleep(1.1)
    with psycopg.connect(env.dsn) as conn:
        conn.execute(
            "UPDATE opspilot_runs SET input=NULL, deadline=clock_timestamp() "
            "- interval '1 second' WHERE run_id=%s",
            (first["run_id"],),
        )
    _control(env, incident, "follow_up", "rebuild the alert run")
    runs = _runs(env, incident)
    assert len(runs) == 2
    assert _time_contract(runs[1]["input"]) == _time_contract(first["input"])


# ===========================================================================
# item 8 on PostgreSQL: version strings of a re-framed successor
# ===========================================================================


@pg
def test_item8_reframed_successor_keeps_input_version_and_tool_schema_revision(env):
    fingerprint, incident = _first_firing(env)
    _refire(env, fingerprint, incident)
    _control(env, incident, "cancel")
    _control(env, incident, "new_run")
    first, successor = _runs(env, incident)
    assert successor["input"]["version"] == ALERT_INPUT_VERSION
    assert first["input"]["version"] == ALERT_INPUT_VERSION
    assert successor["versions"] == first["versions"] == fixture_versions()
    assert successor["versions"]["tool_schema_revision"] == "fixture-5"
