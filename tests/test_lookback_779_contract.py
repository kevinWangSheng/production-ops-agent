"""Independent contract tests for #779: a human-submitted Run may carry a
longer authorized lookback frame, up to a deployment cap.

Written from the user rulings B4-U1 (= option a) and B4-U2 (30 days) in
docs/tasks/2026-11-22-next-batches.md and the pre-review rows U1, U2, B4-2 ...
B4-5 and 必答类别 1-5 in docs/tasks/2026-11-22-next-batches-b4-prereview.md,
without reading any implementation. Red on ``main`` until it lands;
collection stays green.

Contract under test:

* ``POST /intake/ui`` takes ``lookback`` in ``24h`` / ``7d`` / ``30d``,
  default ``24h``. ``OPSPILOT_MAX_LOOKBACK`` (same enum, unset = ``24h``)
  caps it; above the cap the submission is refused ``LOOKBACK_NOT_ALLOWED``
  and nothing is created.
* The frame ends at submit time (the workbench clock, truncated to the
  second) and starts ``lookback`` earlier; it is fixed for the Run and
  inherited by its successor Runs. Alert and release Runs stay 24 h.
* The default query segment is still the frame's last hour; a query outside
  the frame is refused ``QUERY_OUT_OF_WINDOW`` with the frame echoed; inside
  a longer frame the executor does not refuse ``WINDOW_TOO_LARGE`` (B4-2).

Seams assumed beyond the rulings (also marked ``# ASSUMPTION:``):

* ``select_profile(env)`` reads ``OPSPILOT_MAX_LOOKBACK`` from the mapping it
  is given, as it reads every other deployment switch, and the web's tool
  face and the worker's executor both take the cap from it.
* A value outside the enum is ``400 INVALID_INPUT`` (the intake's existing
  validation code), checked before the cap.
* The same idempotency key with a different ``lookback`` is
  ``INTAKE_KEY_CONFLICT``, as a different question is: the lookback is part
  of what the key names.

No model, no PostgreSQL, no network beyond a loopback Loki fake.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from opspilot.investigation.context import (
    InvestigationInput,
    evidence_context_projection,
    initial_messages,
)
from opspilot.investigation.inputs import AlertAnchor, ReleaseAnchor
from opspilot.persistence import Lease
from opspilot.tools import ToolRequest
from opspilot.tools.otel_demo import TARGET_ID
from opspilot.tools.profiles import PROFILE_ENV, select_profile
from tests.m1_tool_support import NOW, FakeClock, RecordingSink
from tests.m1_web_support import (
    basic,
    build_workbench,
    call,
    post_form,
    same_origin,
)
from tests.test_m1_otel_demo_contract import FakeStore
from tests.test_tool_expansion_c5a_contract import FakeLoki

CAP_ENV = "OPSPILOT_MAX_LOOKBACK"
EXT = "otel-demo-ext"
LOKI = "grafana_loki_query"
DAY = timedelta(days=1)
LOOKBACKS = {"24h": DAY, "7d": 7 * DAY, "30d": 30 * DAY}
# The workbench clock carries a fraction; the frame end drops it (必答 1).
CLOCK_START = NOW + timedelta(microseconds=654_321)
UI = {**basic(), **same_origin()}


@pytest.fixture(autouse=True)
def _clean_environment(tmp_path, monkeypatch):
    # Configuration comes from the mapping each test passes, never the shell.
    monkeypatch.delenv(CAP_ENV, raising=False)
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    ca_file = tmp_path / "synthetic-k8s-ca.pem"
    ca_file.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("T779_K8S_CA_FILE", str(ca_file))


@pytest.fixture
def loki() -> Iterator[FakeLoki]:
    fake = FakeLoki()
    fake.start()
    yield fake
    fake.stop()


def _env(loki_url: str, cap: str | None = None) -> dict[str, str]:
    """One deployment's mapping, shared by its web and its worker."""
    env = {
        PROFILE_ENV: EXT,
        "OPSPILOT_K8S_API_URL": "https://127.0.0.1:16443",
        "OPSPILOT_K8S_TOKEN": "eeeeeeee779",
        "OPSPILOT_K8S_CA_FILE": os.environ["T779_K8S_CA_FILE"],
        "OPSPILOT_K8S_NAMESPACE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_LOKI_URL": loki_url,
        "OPSPILOT_LOKI_BEARER_TOKEN": "loki-ro-aaaaaaaa779",
    }
    if cap is not None:
        # ASSUMPTION: the profile reads the cap from the mapping it is given.
        env[CAP_ENV] = cap
    return env


class _Deployment:
    """A web (memory workbench) and a worker built from one mapping."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self.env = dict(env)
        self.profile = select_profile(self.env)
        clock = FakeClock(start=CLOCK_START)
        self.app, self.workbench, self.clock = build_workbench(
            clock=clock, tool_face=self.profile.face(clock)
        )

    def submit(self, *, key: str | None = None, **extra: str):
        fields = {
            "target_id": TARGET_ID,
            "question": "Why did checkout fail ten days ago?",
            "idempotency_key": key or f"t779-{uuid4().hex}",
            **extra,
        }
        return post_form(self.app, "/intake/ui", fields, headers=UI)

    def runs(self) -> dict[UUID, dict[str, Any]]:
        return self.workbench.incidents.runs

    def run_input(self, run_id: str | UUID) -> dict[str, Any]:
        return self.runs()[UUID(str(run_id))]["input"]

    def control(self, incident_id: str, action: str, generation: int):
        return post_form(
            self.app,
            f"/incidents/{incident_id}/control",
            {
                "action": action,
                "expected_generation": str(generation),
                "idempotency_key": f"{action}-{uuid4().hex}",
            },
            headers=UI,
        )

    def executor(self, incident_id: str, run_id: str):
        """The worker's executor for the recorded Run's input."""
        if shutil.which("jq") is None or shutil.which("python3") is None:
            pytest.skip("the extension worker needs jq and python3 on PATH")
        recorded = InvestigationInput.from_json(self.run_input(run_id))
        lease = Lease(
            incident_id=UUID(incident_id),
            run_id=UUID(run_id),
            owner=uuid4(),
            epoch=1,
            control_generation=0,
            global_suspension_generation=0,
            target_suspension_generation=0,
        )
        store = FakeStore(lease, deadline=NOW + timedelta(minutes=10))
        factory = self.profile.executor_factory(store, RecordingSink(), self.clock)
        return factory(lease, recorded)


def _frame(run_input: Mapping[str, Any]) -> dict[str, str]:
    [policy] = run_input["evidence_context"]["time_policies"]
    return dict(policy["window"])


def _expected(lookback: str) -> dict[str, str]:
    end = NOW  # CLOCK_START truncated to the second
    return {
        "start": (end - LOOKBACKS[lookback]).isoformat(),
        "end": end.isoformat(),
    }


def _z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _loki_call(frame: Mapping[str, str], params: dict[str, object]) -> ToolRequest:
    # The loop sets the top-level window to the Run's whole frame (B4-2).
    return ToolRequest(
        step_id="t779-step",
        tool_index=0,
        tool_name=LOKI,
        target_ref=TARGET_ID,
        params={"query": '{app="checkout"}', **params},
        window=dict(frame),
    )


def _ten_days_back() -> dict[str, str]:
    start = NOW - 10 * DAY
    return {"start": _z(start), "end": _z(start + timedelta(hours=1))}


def _created(response) -> dict[str, Any]:
    assert response.status == 201, response.text
    return response.json()


def _refused(response, status: int, code: str) -> None:
    assert response.status == status, response.text
    assert response.json()["code"] == code, response.text


# -- 1 / 3: the frame a submission records ----------------------------------


@pytest.mark.parametrize("lookback", ["24h", "7d", "30d"])
def test_a_submission_under_a_30d_cap_records_the_chosen_frame(loki, lookback):
    deployment = _Deployment(_env(loki.url, cap="30d"))
    created = _created(deployment.submit(lookback=lookback))
    assert _frame(deployment.run_input(created["run_id"])) == _expected(lookback)


def test_a_7d_cap_admits_7d(loki):
    deployment = _Deployment(_env(loki.url, cap="7d"))
    created = _created(deployment.submit(lookback="7d"))
    assert _frame(deployment.run_input(created["run_id"])) == _expected("7d")


@pytest.mark.parametrize("cap", [None, "24h", "7d", "30d"])
def test_a_default_submission_is_todays_24h_frame_byte_for_byte(loki, cap):
    deployment = _Deployment(_env(loki.url, cap=cap))
    default = _created(deployment.submit(key="t779-default"))
    explicit = _created(deployment.submit(key="t779-explicit", lookback="24h"))
    recorded = deployment.run_input(default["run_id"])
    assert _frame(recorded) == _expected("24h")
    other = deployment.run_input(explicit["run_id"])
    # Run ids differ; everything else about the two inputs is the same.
    assert json.dumps(recorded, sort_keys=True).replace(
        default["run_id"], "RUN"
    ) == json.dumps(other, sort_keys=True).replace(explicit["run_id"], "RUN")


def test_the_model_sees_the_longer_frame_without_a_prompt_change(loki):
    deployment = _Deployment(_env(loki.url, cap="30d"))
    long = _created(deployment.submit(key="t779-long", lookback="30d"))
    short = _created(deployment.submit(key="t779-short"))
    long_input = InvestigationInput.from_json(deployment.run_input(long["run_id"]))
    short_input = InvestigationInput.from_json(deployment.run_input(short["run_id"]))

    def rendered(value: InvestigationInput, run_id: str) -> tuple[str, str]:
        context = evidence_context_projection(value.evidence_context, run_id=run_id)
        messages, system = initial_messages(value, evidence_context=context)
        return json.dumps(messages, ensure_ascii=False), system

    long_messages, long_system = rendered(long_input, long["run_id"])
    _, short_system = rendered(short_input, short["run_id"])
    assert _expected("30d")["start"] in long_messages  # B4-5, L3b
    assert long_system == short_system  # no prompt change (B4-5)
    assert long_input.tool_schemas == short_input.tool_schemas
    # The model has no way to ask for another frame (B4-3, 必答 4).
    assert "lookback" not in json.dumps([dict(s) for s in long_input.tool_schemas])


# -- 2 / 7: refusals -----------------------------------------------------------


@pytest.mark.parametrize(
    ("cap", "lookback"),
    [(None, "7d"), (None, "30d"), ("24h", "30d"), ("7d", "30d")],
)
def test_above_the_cap_is_refused_and_creates_nothing(loki, cap, lookback):
    deployment = _Deployment(_env(loki.url, cap=cap))
    _refused(
        deployment.submit(key="t779-over", lookback=lookback),
        400,
        "LOOKBACK_NOT_ALLOWED",
    )
    assert deployment.runs() == {}
    assert deployment.workbench.incidents.incidents == {}
    # Nothing was recorded under the key: the same key within the cap is a
    # first submission, not a replay or a conflict.
    retried = _created(deployment.submit(key="t779-over"))
    assert retried["replayed"] is False
    assert len(deployment.runs()) == 1


@pytest.mark.parametrize(
    "lookback", ["1d", "14d", "90d", "24H", "30D", "720h", "30", "P30D", "all"]
)
def test_a_value_outside_the_enum_is_refused(loki, lookback):
    deployment = _Deployment(_env(loki.url, cap="30d"))
    # ASSUMPTION: the intake's existing validation code.
    _refused(deployment.submit(lookback=lookback), 400, "INVALID_INPUT")
    assert deployment.runs() == {}


def test_the_same_key_with_another_lookback_conflicts(loki):
    deployment = _Deployment(_env(loki.url, cap="30d"))
    first = _created(deployment.submit(key="t779-same", lookback="30d"))
    replay = deployment.submit(key="t779-same", lookback="30d")
    assert replay.status == 200 and replay.json()["run_id"] == first["run_id"]
    # ASSUMPTION: the lookback is part of what the key names.
    _refused(
        deployment.submit(key="t779-same", lookback="7d"), 409, "INTAKE_KEY_CONFLICT"
    )
    assert _frame(deployment.run_input(first["run_id"])) == _expected("30d")


# -- 5: successor Runs inherit the frame ----------------------------------------


def test_a_successor_run_inherits_the_30d_frame(loki):
    deployment = _Deployment(_env(loki.url, cap="30d"))
    created = _created(deployment.submit(lookback="30d"))
    incident = created["incident_id"]
    # Later than the submission, so a recomputed frame would differ.
    deployment.clock.advance(3 * 3600)
    assert deployment.control(incident, "cancel", 0).status == 200
    renewed = deployment.control(incident, "new_run", 1)
    assert renewed.status == 200, renewed.text
    successor = renewed.json()["run_id"]
    assert successor != created["run_id"]
    assert _frame(deployment.run_input(successor)) == _expected("30d")


# -- 6: alert and release Runs stay 24 h ----------------------------------------


def test_alert_and_release_runs_stay_24h_under_a_30d_cap(loki):
    face = select_profile(_env(loki.url, cap="30d")).face(FakeClock(start=CLOCK_START))
    received = NOW - timedelta(minutes=2)
    common = {
        "run_id": "t779-anchor",
        "question": "Why is checkout degraded?",
        "target_id": TARGET_ID,
        "deadline": NOW + timedelta(minutes=10),
        "model_requests": 12,
    }
    alert = face.input_for(
        **common,
        alert=AlertAnchor(
            received_at=received,
            starts_at=received - timedelta(minutes=5),
            original=_z(received - timedelta(minutes=5)),
        ),
    )
    release = face.input_for(
        **common,
        release=ReleaseAnchor(
            released_at=received - timedelta(minutes=30), consumed_at=received
        ),
    )
    for value in (alert, release):
        window = _frame(value.as_json())
        start = datetime.fromisoformat(window["start"])
        end = datetime.fromisoformat(window["end"])
        assert end - start == DAY, window


# -- 4: the executor honours the frame -------------------------------------------


def test_a_30d_run_reaches_the_source_ten_days_back(loki):
    deployment = _Deployment(_env(loki.url, cap="30d"))
    created = _created(deployment.submit(lookback="30d"))
    frame = _frame(deployment.run_input(created["run_id"]))
    assert frame == _expected("30d")
    line = "checkout payment timeout t779"
    loki.add(
        {"app": "checkout"}, _epoch_ns(NOW - 10 * DAY + timedelta(minutes=5)), line
    )
    executor = deployment.executor(created["incident_id"], created["run_id"])
    outcome = executor.execute(_loki_call(frame, _ten_days_back()))
    view = json.dumps(outcome.model_view)
    assert outcome.reason not in {
        "WINDOW_TOO_LARGE",
        "WINDOW_OUT_OF_SCOPE",
        "INVALID_PARAMS",
    }, view
    assert "QUERY_OUT_OF_WINDOW" not in view
    assert outcome.status == "ok", view
    assert line in view
    [request] = loki.requests
    assert request["path"].endswith("/loki/api/v1/query_range")


def test_a_30d_run_still_defaults_to_the_last_hour(loki):
    deployment = _Deployment(_env(loki.url, cap="30d"))
    created = _created(deployment.submit(lookback="30d"))
    frame = _frame(deployment.run_input(created["run_id"]))
    assert frame == _expected("30d")
    executor = deployment.executor(created["incident_id"], created["run_id"])
    outcome = executor.execute(_loki_call(frame, {}))
    assert outcome.status in {"ok", "no_data"}, json.dumps(outcome.model_view)
    sent = loki.sent()
    start_ns = _loki_ns(sent["start"])
    end_ns = _loki_ns(sent["end"])
    hour_ns = 3600 * 10**9
    assert abs((end_ns - start_ns) - hour_ns) <= 10**6, sent
    assert abs(end_ns - _epoch_ns(NOW)) <= 10**6, sent


@pytest.mark.parametrize("cap", [None, "30d"])
def test_a_24h_run_refuses_ten_days_back_and_states_its_frame(loki, cap):
    deployment = _Deployment(_env(loki.url, cap=cap))
    created = _created(deployment.submit())
    frame = _frame(deployment.run_input(created["run_id"]))
    assert frame == _expected("24h")
    executor = deployment.executor(created["incident_id"], created["run_id"])
    outcome = executor.execute(_loki_call(frame, _ten_days_back()))
    view = outcome.model_view
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), view
    notes = view.get("notes") or []
    assert notes and str(notes[0]).startswith("QUERY_OUT_OF_WINDOW"), view
    assert frame["start"] in notes[0] and frame["end"] in notes[0], notes
    # Nor can a call widen the frame the Run recorded (必答 4).
    widened = executor.execute(_loki_call(_expected("30d"), _ten_days_back()))
    assert widened.status == "denied", widened.model_view
    assert loki.requests == []


# -- the submit form ---------------------------------------------------------------


def test_the_submit_form_offers_the_lookback_choice_defaulting_to_24h(loki):
    deployment = _Deployment(_env(loki.url, cap="30d"))
    page = call(deployment.app, "GET", "/", headers={**basic(), "accept": "text/html"})
    assert page.status == 200, page.text
    form = re.search(r'<form[^>]*action="/intake/ui".*?</form>', page.text, flags=re.S)
    assert form, "the intake form is missing"
    select = re.search(
        r'<select[^>]*name="lookback"[^>]*>(.*?)</select>', form.group(0), flags=re.S
    )
    assert select, "the intake form has no lookback choice"
    options = re.findall(r"<option([^>]*)>", select.group(1))
    values = [re.search(r'value="([^"]*)"', o).group(1) for o in options]
    assert values == ["24h", "7d", "30d"], values
    selected = [v for v, o in zip(values, options) if "selected" in o]
    assert selected in (["24h"], []), selected  # first option is the default


# -- helpers for the Loki wire format -------------------------------------------

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _epoch_ns(moment: datetime) -> int:
    return (moment - _EPOCH) // timedelta(microseconds=1) * 1000


def _loki_ns(text: str) -> int:
    if re.fullmatch(r"\d+", text):
        value = int(text)
        return value if value > 10**14 else value * 10**9
    if re.fullmatch(r"\d+\.\d+", text):
        whole, fraction = text.split(".")
        return int(whole) * 10**9 + int(fraction.ljust(9, "0")[:9])
    return _epoch_ns(datetime.fromisoformat(text.replace("Z", "+00:00")))
