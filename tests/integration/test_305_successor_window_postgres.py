"""#305 on PostgreSQL: the gateway authorizes a re-framed successor's query
over the attached episode (contract item 4), and a release attachment does
not re-frame (W3-A).

Independent acceptance tests written from
``docs/tasks/2026-10-24-305-successor-window-contract.md`` (W1-B, W2-A, W3-A,
R1-R7) without reading the implementation branch. Entry points: the
Alertmanager webhook and the web control route, the otel-demo profile's
public executor factory over a Run's committed input (the tool gateway, with
a fake HTTP opener: no network), and ``Workbench.release_anomaly_pass`` with
the F11 PR-4a/4b acceptance helpers for the release attachment.

Assumptions where the contract leaves a value open:

* G1 item 4's example query ``[startsAt, startsAt+5m]`` presumes minutes
  between the episode and the successor; with real DB time seconds apart the
  query is ``[startsAt - 5m, startsAt + 1s]``: it covers the attached
  episode's start, ends before that episode was received (so inside a frame
  ending at the successor's creation) and after the first Run's frame end,
  so the first Run must refuse it.
* G2 "not refused" is asserted as: the outcome is not ``denied`` /
  ``WINDOW_OUT_OF_SCOPE`` and the request reached the (fake) Prometheus.
* G3 W3-A: an alert incident on a release target takes a release ``attached``
  link and no alert ``attached`` delivery; cancel + new_run then keeps the
  first Run's frame, anchor and default query window (J5).
"""

# ruff: noqa: F811 - pytest fixtures imported from the F11 modules
from __future__ import annotations

import io
import os
import time
import urllib.request
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest

from opspilot.investigation.context import InvestigationInput
from opspilot.persistence import DurableStore, Lease
from opspilot.tools import ToolRequest, Window
from opspilot.tools.otel_demo import (
    METRICS_TOOL,
    TARGET_ID,
    OtelDemoConfig,
    otel_demo_executor_factory,
)
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
from scripts.m0.postgres_lab import DSN
from tests.integration.test_f11_pr4b_consume_postgres import (  # noqa: F401
    _PR4B_MISSING,
    _case,
    _compare,
    _link,
    _registry_entry,
    _settle,
    bench,
)
from tests.integration.test_f11_release_observation_postgres import (  # noqa: F401
    POOL,
    catalog,
    observer,
    observer_dsn,
    owner,
    web,
    web_dsn,
)
from tests.m1_tool_support import FakeClock, RecordingSink
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

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

RUN_SECONDS = 600
LABELS = {"namespace": "otel-demo", "service": "checkout"}
ALERT_ENTRY = {
    "integration_id": "m0-otel-20260909",
    "cluster_uid": "opspilot-m1",
    "namespace": "otel-demo",
    "workload": "checkout",
    "health_profile_id": "otel-demo-checkout",
    "match": {"version": 1, "labels": LABELS},
}
EXPR = "sum by(service_name) (increase(traces_span_metrics_calls_total[5m]))"
EMPTY_BODY = (
    __import__("pathlib").Path(__file__).resolve().parents[1]
    / "fixtures"
    / "otel_demo"
    / "prometheus-range-empty.json"
).read_bytes()


# -- helpers -----------------------------------------------------------------


def _instant(value) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _text(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _db_now(dsn: str = DSN) -> datetime:
    with psycopg.connect(dsn) as conn:
        return conn.execute("SELECT clock_timestamp()").fetchone()[0].astimezone(UTC)


def _app(dsn: str, registry, *, profile_env, workbench=None):
    store = DurableStore(dsn, pool=POOL)
    store.install()
    if workbench is None:
        events = DurableEventLog(store)
        events.install()
        evidence = DurableEvidenceStore(store)
        evidence.install()
        ledger = DurableWebLedger(store)
        ledger.install()
        profile = select_profile(profile_env)
        workbench = Workbench(
            incidents=DurableIncidentStore(store),
            events=events,
            evidence=evidence,
            ledger=ledger,
            run_versions=profile.versions(),
            tool_face=profile.face(DurableClock(store)),
            run_seconds=RUN_SECONDS,
            targets=MappingTargetRegistry(registry),
        )
    auth = AuthConfig(
        ui_users={UI_USER: hash_password(UI_PASSWORD)},
        event_tokens={token_digest(EVENT_TOKEN): "alertmanager"},
        auth_revision="t305-window-integration-v1",
        allowed_origins=frozenset({ORIGIN}),
    )
    return create_app(workbench, Authenticator(auth), DurableClock(store)), store


def _send(app, fingerprint, starts_at):
    alert = {
        "status": "firing",
        "labels": {
            "alertname": "CheckoutPlaceOrderErrorRatioHigh",
            "severity": "critical",
            **LABELS,
        },
        "annotations": {"summary": "checkout error ratio is high"},
        "startsAt": starts_at,
        "endsAt": "0001-01-01T00:00:00Z",
        "fingerprint": fingerprint,
        "generatorURL": "http://prometheus:9090/graph?g0.expr=up",
    }
    response = post_json(
        app,
        "/intake/alertmanager",
        {"version": "4", "status": "firing", "alerts": [alert]},
        headers=bearer(),
    )
    assert response.status == 200, response.text
    (item,) = response.json()["results"]
    return item


def _control(app, dsn, incident_id, action):
    with psycopg.connect(dsn) as conn:
        generation = conn.execute(
            "SELECT control_generation FROM opspilot_incidents WHERE incident_id=%s",
            (UUID(str(incident_id)),),
        ).fetchone()[0]
    response = post_form(
        app,
        f"/incidents/{incident_id}/control",
        {
            "action": action,
            "expected_generation": str(generation),
            "idempotency_key": f"t305-{action}-{uuid4().hex}",
        },
        headers={**basic(), **same_origin(), "accept": "application/json"},
    )
    assert response.status == 200, (action, response.status, response.text)


def _runs(incident_id, dsn: str = DSN):
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT run_id,deadline,input FROM opspilot_runs WHERE incident_id=%s "
            "ORDER BY deadline,run_id",
            (UUID(str(incident_id)),),
        ).fetchall()
    return [{"run_id": r[0], "deadline": r[1], "input": r[2]} for r in rows]


def _alert_outcomes(incident_id, dsn: str = DSN):
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT outcome FROM opspilot_alert_deliveries WHERE incident_id=%s "
            "ORDER BY delivery_id",
            (UUID(str(incident_id)),),
        ).fetchall()
    return [r[0] for r in rows]


def _time_contract(run_input):
    policy = run_input["evidence_context"]["time_policies"][0]
    return (
        run_input["scope_facts"]["alert_starts_at"],
        policy["window"],
        policy["anchor"],
        policy["anchor_rule"],
        policy["default_query_window"],
    )


def _frame(run_input) -> Window:
    window = run_input["evidence_context"]["time_policies"][0]["window"]
    return Window(_instant(window["start"]), _instant(window["end"]))


# -- the gateway over a committed Run input ----------------------------------


class _Reply:
    def __init__(self, body: bytes):
        self._buffer = io.BytesIO(body)
        self.status = 200

    def read(self, n: int = -1) -> bytes:
        return self._buffer.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    def __init__(self):
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        return _Reply(EMPTY_BODY)


class _RunStore:
    """The store reads the factory's wiring makes for one Run: its deadline,
    the tool charge and the human control state (all unchanged)."""

    def __init__(self, lease: Lease, deadline: datetime):
        self.lease, self.deadline = lease, deadline

    def rebuild(self, incident_id):
        return {
            "run": {
                "run_id": self.lease.run_id,
                "deadline": self.deadline,
                "tool_operations_used": 0,
                "tool_seconds_used": 0.0,
            }
        }

    def charge_tool(self, lease, operation_id, seconds, **_):
        return None

    def control_state(self, incident_id):
        return {
            "incident_generation": self.lease.control_generation,
            "incident_state": "running",
            "global_suspended": False,
            "global_generation": self.lease.global_suspension_generation,
            "target_suspended": False,
            "target_generation": self.lease.target_suspension_generation,
        }


def _query(monkeypatch, incident_id, run, window: Window):
    """One metrics query with ``window`` through the profile's gateway, scoped
    from ``run``'s own committed input. Returns (outcome, requests sent)."""
    opener = _Opener()
    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: opener)
    lease = Lease(
        incident_id=UUID(str(incident_id)),
        run_id=run["run_id"],
        owner=uuid4(),
        epoch=1,
        control_generation=1,
        global_suspension_generation=0,
        target_suspension_generation=0,
    )
    store = _RunStore(lease, _instant(run["deadline"]))
    factory = otel_demo_executor_factory(
        store,
        evidence=RecordingSink(),
        clock=FakeClock(start=_frame(run["input"]).end + timedelta(seconds=1)),
        config=OtelDemoConfig(
            prometheus_url="http://127.0.0.1:19090",
            jaeger_url="http://127.0.0.1:16686/jaeger/ui",
        ),
    )
    executor = factory(lease, InvestigationInput.from_json(run["input"]))
    outcome = executor.execute(
        ToolRequest(
            step_id="step-1",
            tool_index=0,
            tool_name=METRICS_TOOL,
            target_ref=TARGET_ID,
            params={"expr": EXPR},
            window=window.as_json(),
        )
    )
    return outcome, opener.requests


# ===========================================================================
# item 4: the attached episode is queryable on the successor only
# ===========================================================================


def test_item4_query_over_attached_episode_is_authorized_only_on_successor(
    monkeypatch, tmp_path
):
    app, store = _app(
        DSN,
        {"checkout-prod": ALERT_ENTRY},
        profile_env={"OPSPILOT_TOOL_PROFILE": "otel-demo"},
    )
    try:
        fingerprint = uuid4().hex[:16]
        created = _send(app, fingerprint, _text(_db_now() - timedelta(minutes=30)))
        assert created["outcome"] == "created", created
        incident = created["incident_id"]
        time.sleep(1.2)
        starts = _db_now().replace(microsecond=0)
        time.sleep(1.1)
        attached = _send(app, fingerprint, _text(starts))
        assert attached["outcome"] == "attached", attached
        _control(app, DSN, incident, "cancel")
        _control(app, DSN, incident, "new_run")
        first, successor = _runs(incident)

        # G1: covers the attached episode's start, ends one second after it
        # (before its receipt, so inside a frame ending at the successor's
        # creation) and after the first Run's frame end.
        query = Window(starts - timedelta(minutes=5), starts + timedelta(seconds=1))
        assert query.end > _frame(first["input"]).end

        # The harness itself: a window inside the first Run's frame passes.
        inside = Window(query.start - timedelta(minutes=5), query.start)
        control, sent = _query(monkeypatch, incident, first, inside)
        assert control.status != "denied" and len(sent) == 1, control

        refused, sent = _query(monkeypatch, incident, first, query)
        assert (refused.status, refused.reason) == ("denied", "WINDOW_OUT_OF_SCOPE")
        assert sent == []

        accepted, sent = _query(monkeypatch, incident, successor, query)
        assert accepted.reason != "WINDOW_OUT_OF_SCOPE", accepted
        assert accepted.status != "denied", accepted
        assert len(sent) == 1
    finally:
        store.close()


# ===========================================================================
# W3-A: a release attachment alone does not re-frame
# ===========================================================================


def test_w3a_release_attachment_without_alert_attachment_keeps_the_frame(
    owner, web, observer, catalog, bench, web_dsn
):
    if _PR4B_MISSING:
        pytest.fail(_PR4B_MISSING, pytrace=False)
    _settle(observer, bench, web)
    case = _case(owner, web, observer, catalog)
    # One registry entry: the release target, bound by the alert's labels too.
    entry = {**_registry_entry(), "match": {"version": 1, "labels": LABELS}}
    workbench = bench({case.resource_uid: entry})
    app, store = _app(web_dsn, None, profile_env={}, workbench=workbench)
    try:
        created = _send(app, uuid4().hex[:16], _text(_db_now() - timedelta(minutes=30)))
        assert created["outcome"] == "created", created
        incident = UUID(created["incident_id"])
        time.sleep(1.2)

        _compare(observer, case)
        workbench.release_anomaly_pass()
        link = _link(case.oid)
        assert (link["disposition"], link["incident_id"]) == ("attached", incident)
        assert _alert_outcomes(incident) == ["created"]  # no alert attachment

        time.sleep(1.1)
        _control(app, web_dsn, incident, "cancel")
        _control(app, web_dsn, incident, "new_run")
        first, successor = _runs(incident)
        assert _time_contract(successor["input"]) == _time_contract(first["input"])
    finally:
        store.close()
