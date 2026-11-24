"""#667 PR-A contract, group 3: conductor client (S-5 layer 4, S-7).

Asserted against our own local HTTP stand-in that records each request: the
submit body is exactly ``{"stage": "diagnosis", "solution": text}``, sent at
most once per client; no other stage can be submitted; loopback only; no
system proxy; redirects are not followed; fixed error codes without the
response body. Waits use a fixed clock.
"""

from __future__ import annotations

import inspect

import pytest

from tests.sregym_eval_support import (
    FakeClock,
    Reply,
    StubServer,
    code_of,
    free_port,
    mod,
)

APP = {
    "app_name": "Zephyr Widgets",
    "namespace": "zephyr-ns",
    "namespaces": ["zephyr-ns"],
    "descriptions": "Synthetic storefront.",
    "stages": ["diagnosis"],
}
BODY_CANARY = "zz-body-canary-root-cause-hint"


def _client(url, **kw):
    return mod("conductor").ConductorClient(url, **kw)


def _err(fn):
    c = mod("conductor")
    with pytest.raises(c.ConductorError) as ei:
        fn()
    return code_of(ei.value), ei.value


# --- base URL and loopback ------------------------------------------------------


def test_base_url_from_env_defaults_and_overrides():
    c = mod("conductor")
    assert c.base_url_from_env({}) == "http://localhost:8000"
    assert (
        c.base_url_from_env({"API_HOSTNAME": "127.0.0.1", "API_PORT": "28000"})
        == "http://127.0.0.1:28000"
    )


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:28000", "http://localhost:28000", "http://[::1]:28000"],
)
def test_loopback_accepted(url):
    _client(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5:8000",
        "http://192.168.1.10:8000",
        "http://example.com:8000",
        "http://localhost.example.com:8000",
        "http://127.0.0.1.nip.io:8000",
        "http://0.0.0.0:8000",
    ],
)
def test_non_loopback_refused_at_construction(url):
    code, _ = _err(lambda: _client(url))
    assert code == "CONDUCTOR_NOT_LOOPBACK"


# --- submit ------------------------------------------------------------------


def _submit_server(status=200, body=None):
    def route(method, path):
        if method == "POST" and path == "/submit":
            return Reply(
                status,
                body
                if body is not None
                else {"status": "200", "message": "ok", "stage": "diagnosis"},
            )
        return Reply(404, {"detail": "nope"})

    return StubServer(route)


@pytest.mark.parametrize(
    "text", ["The cart service is down because X.", "", "multi\nline é {json}"]
)
def test_submit_body_is_exactly_diagnosis_and_solution(text):
    with _submit_server() as s:
        assert _client(s.url).submit_diagnosis(text) == 200
        assert len(s.seen) == 1
        req = s.seen[0]
        assert req.method == "POST"
        assert req.path == "/submit"
        assert req.json() == {"stage": "diagnosis", "solution": text}


def test_second_submit_after_success_sends_nothing():
    with _submit_server() as s:
        cl = _client(s.url)
        cl.submit_diagnosis("first")
        code, _ = _err(lambda: cl.submit_diagnosis("second"))
        assert code == "ALREADY_SUBMITTED"
        assert [r.path for r in s.seen] == ["/submit"]


def test_second_submit_after_failure_sends_nothing():
    with _submit_server(status=503, body={"detail": BODY_CANARY}) as s:
        cl = _client(s.url)
        first, _ = _err(lambda: cl.submit_diagnosis("first"))
        assert first == "SUBMIT_UNAVAILABLE"
        code, _ = _err(lambda: cl.submit_diagnosis("again"))
        assert code == "ALREADY_SUBMITTED"
        assert len(s.seen) == 1


@pytest.mark.parametrize(
    ("status", "want"),
    [
        (409, "SUBMIT_REJECTED"),
        (503, "SUBMIT_UNAVAILABLE"),
        (400, "SUBMIT_INVALID"),
        (422, "SUBMIT_INVALID"),
        (500, "CONDUCTOR_BAD_RESPONSE"),
    ],
)
def test_submit_error_codes_without_body(status, want):
    with _submit_server(status=status, body={"detail": BODY_CANARY}) as s:
        code, exc = _err(lambda: _client(s.url).submit_diagnosis("text"))
        assert code == want
        assert BODY_CANARY not in str(exc)
        assert BODY_CANARY not in repr(exc.args)


def test_no_method_submits_another_stage():
    cl_cls = mod("conductor").ConductorClient
    public = [n for n in dir(cl_cls) if not n.startswith("_")]
    for name in public:
        assert "mitigat" not in name.lower(), name
    submitters = [n for n in public if "submit" in n.lower()]
    assert submitters == ["submit_diagnosis"]
    params = list(inspect.signature(cl_cls.submit_diagnosis).parameters)
    assert params == ["self", "text"]


# --- status / get_app --------------------------------------------------------------


def _get_server(routes):
    def route(method, path):
        r = routes.get((method, path))
        return r() if callable(r) else (r or Reply(404, {"detail": "nope"}))

    return StubServer(route)


def test_status_parses_stage_and_remaining():
    c = mod("conductor")
    with _get_server(
        {
            ("GET", "/status"): Reply(
                200, {"stage": "diagnosis", "agent_remaining_seconds": 12.5}
            )
        }
    ) as s:
        st = _client(s.url).status()
        assert st == c.Status(stage="diagnosis", remaining_s=12.5)
        assert s.seen[0].method == "GET"


def test_status_null_stage_and_missing_remaining():
    with _get_server({("GET", "/status"): Reply(200, {"stage": None})}) as s:
        st = _client(s.url).status()
        assert st.stage is None and st.remaining_s is None


def test_status_not_started():
    with _get_server(
        {("GET", "/status"): Reply(400, {"detail": "No problem has been started"})}
    ) as s:
        code, _ = _err(lambda: _client(s.url).status())
        assert code == "CONDUCTOR_NOT_STARTED"


@pytest.mark.parametrize(
    "reply",
    [
        Reply(200, "not json at all"),
        Reply(200, {"no_stage": 1}),
        Reply(200, {"stage": 7}),
        Reply(200, ["diagnosis"]),
        Reply(500, {"detail": BODY_CANARY}),
    ],
)
def test_status_bad_response(reply):
    with _get_server({("GET", "/status"): reply}) as s:
        code, exc = _err(lambda: _client(s.url).status())
        assert code == "CONDUCTOR_BAD_RESPONSE"
        assert BODY_CANARY not in str(exc)


def test_get_app_parses_into_frozen_app_info():
    c = mod("conductor")
    body = dict(
        APP, namespaces=["zephyr-a", "zephyr-b"], stages=["diagnosis", "mitigation"]
    )
    with _get_server({("GET", "/get_app"): Reply(200, body)}) as s:
        app = _client(s.url).get_app()
    assert app == c.AppInfo(
        app_name="Zephyr Widgets",
        namespace="zephyr-ns",
        namespaces=("zephyr-a", "zephyr-b"),
        descriptions="Synthetic storefront.",
        stages=("diagnosis", "mitigation"),
    )
    with pytest.raises(Exception):
        app.app_name = "mutated"  # frozen


def test_get_app_without_diagnosis_stage():
    with _get_server(
        {("GET", "/get_app"): Reply(200, dict(APP, stages=["mitigation"]))}
    ) as s:
        code, _ = _err(lambda: _client(s.url).get_app())
        assert code == "NO_DIAGNOSIS_STAGE"


@pytest.mark.parametrize("drop", ["app_name", "namespace", "descriptions", "stages"])
def test_get_app_missing_field_is_bad_response(drop):
    body = {k: v for k, v in APP.items() if k != drop}
    with _get_server({("GET", "/get_app"): Reply(200, body)}) as s:
        code, _ = _err(lambda: _client(s.url).get_app())
        assert code == "CONDUCTOR_BAD_RESPONSE"


def test_get_app_not_started():
    with _get_server(
        {("GET", "/get_app"): Reply(400, {"detail": "No problem has been started"})}
    ) as s:
        code, _ = _err(lambda: _client(s.url).get_app())
        assert code == "CONDUCTOR_NOT_STARTED"


# --- transport: unreachable, timeout, proxy, redirect ------------------------------


def test_unreachable():
    url = f"http://127.0.0.1:{free_port()}"
    code, _ = _err(lambda: _client(url, timeout_s=1).status())
    assert code == "CONDUCTOR_UNREACHABLE"


def test_timeout_is_unreachable():
    with _get_server(
        {("GET", "/status"): Reply(200, {"stage": "setup"}, delay_s=1.5)}
    ) as s:
        code, _ = _err(lambda: _client(s.url, timeout_s=0.2).status())
        assert code == "CONDUCTOR_UNREACHABLE"


def test_system_proxy_is_not_used(monkeypatch):
    with (
        StubServer(lambda m, p: Reply(200, {"stage": "diagnosis"})) as proxy,
        _submit_server() as s,
    ):
        for var in (
            "HTTP_PROXY",
            "http_proxy",
            "HTTPS_PROXY",
            "https_proxy",
            "ALL_PROXY",
            "all_proxy",
        ):
            monkeypatch.setenv(var, proxy.url)
        for var in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(var, raising=False)
        cl = _client(s.url)
        assert cl.submit_diagnosis("via direct") == 200
        assert proxy.seen == []
        assert len(s.seen) == 1


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirects_are_not_followed(status):
    with StubServer(lambda m, p: Reply(200, {"stage": "diagnosis"})) as target:
        loc = {"Location": f"{target.url}/submit"}
        with StubServer(lambda m, p: Reply(status, {}, headers=loc)) as s:
            c = mod("conductor")
            with pytest.raises(c.ConductorError):
                _client(s.url).submit_diagnosis("text")
            with pytest.raises(c.ConductorError):
                _client(s.url).status()
        assert target.seen == []


# --- waits (fixed clock) ---------------------------------------------------------


def _stage_script(stages):
    """Serve /status with the listed stages in turn, then repeat the last."""
    seq = list(stages)

    def nxt():
        stage = seq.pop(0) if len(seq) > 1 else seq[0]
        if stage == "NOT_STARTED":
            return Reply(400, {"detail": "No problem has been started"})
        return Reply(200, {"stage": stage, "agent_remaining_seconds": 100.0})

    return _get_server({("GET", "/status"): nxt})


def test_wait_for_diagnosis_waits_through_setup_and_none():
    fc = FakeClock()
    with _stage_script(["setup", None, "setup", "diagnosis"]) as s:
        st = _client(s.url).wait_for_diagnosis(
            timeout_s=60, poll_s=2.0, clock=fc.clock, sleep=fc.sleep
        )
    assert st.stage == "diagnosis"
    assert len(s.seen) == 4
    assert fc.sleeps and all(x <= 2.0 for x in fc.sleeps)


@pytest.mark.parametrize("stage", ["tearing_down", "done", "mitigation"])
def test_wait_for_diagnosis_stage_passed(stage):
    fc = FakeClock()
    with _stage_script(["setup", stage]) as s:
        code, _ = _err(
            lambda: _client(s.url).wait_for_diagnosis(
                timeout_s=60, poll_s=2.0, clock=fc.clock, sleep=fc.sleep
            )
        )
    assert code == "STAGE_PASSED"


def test_wait_for_diagnosis_times_out_on_fixed_clock():
    fc = FakeClock()
    with _stage_script(["setup"]) as s:
        code, _ = _err(
            lambda: _client(s.url).wait_for_diagnosis(
                timeout_s=10, poll_s=2.0, clock=fc.clock, sleep=fc.sleep
            )
        )
    assert code == "STAGE_WAIT_TIMEOUT"
    assert fc.now - 1000.0 <= 10 + 2.0
    assert len(s.seen) <= 7


def test_wait_for_diagnosis_retries_connection_failures_within_timeout():
    port = free_port()
    fc = FakeClock()
    started: list[StubServer] = []

    def bring_up():
        if len(fc.sleeps) == 2 and not started:
            started.append(
                StubServer(lambda m, p: Reply(200, {"stage": "diagnosis"}), port=port)
            )

    fc.on_sleep = bring_up
    try:
        st = _client(f"http://127.0.0.1:{port}", timeout_s=1).wait_for_diagnosis(
            timeout_s=60, poll_s=2.0, clock=fc.clock, sleep=fc.sleep
        )
        assert st.stage == "diagnosis"
        assert started and len(started[0].seen) >= 1
    finally:
        for srv in started:
            srv.close()


def test_wait_for_diagnosis_unreachable_until_timeout():
    fc = FakeClock()
    url = f"http://127.0.0.1:{free_port()}"
    c = mod("conductor")
    with pytest.raises(c.ConductorError) as ei:
        _client(url, timeout_s=1).wait_for_diagnosis(
            timeout_s=6, poll_s=2.0, clock=fc.clock, sleep=fc.sleep
        )
    # ASSUMPTION: still-unreachable at the deadline reports either code.
    assert code_of(ei.value) in {"STAGE_WAIT_TIMEOUT", "CONDUCTOR_UNREACHABLE"}
    assert len(fc.sleeps) >= 2


def test_wait_for_done_polls_until_done():
    fc = FakeClock()
    with _stage_script(["diagnosis", "tearing_down", "done"]) as s:
        st = _client(s.url).wait_for_done(
            timeout_s=60, poll_s=2.0, clock=fc.clock, sleep=fc.sleep
        )
    assert st.stage == "done"
    assert len(s.seen) == 3


def test_wait_for_done_treats_not_started_as_finished():
    fc = FakeClock()
    with _stage_script(["tearing_down", "NOT_STARTED"]) as s:
        _client(s.url).wait_for_done(
            timeout_s=60, poll_s=2.0, clock=fc.clock, sleep=fc.sleep
        )
    assert len(s.seen) == 2


def test_wait_for_done_times_out():
    fc = FakeClock()
    with _stage_script(["tearing_down"]) as s:
        code, _ = _err(
            lambda: _client(s.url).wait_for_done(
                timeout_s=10, poll_s=2.0, clock=fc.clock, sleep=fc.sleep
            )
        )
    assert code == "STAGE_WAIT_TIMEOUT"


def test_waits_never_post():
    fc = FakeClock()
    with _stage_script(["setup", "diagnosis", "tearing_down", "done"]) as s:
        cl = _client(s.url)
        cl.wait_for_diagnosis(timeout_s=60, poll_s=2.0, clock=fc.clock, sleep=fc.sleep)
        cl.wait_for_done(timeout_s=60, poll_s=2.0, clock=fc.clock, sleep=fc.sleep)
    assert {r.method for r in s.seen} == {"GET"}
    assert {r.path for r in s.seen} == {"/status"}
