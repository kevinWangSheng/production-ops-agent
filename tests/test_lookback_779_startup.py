"""#779: an unusable ``OPSPILOT_MAX_LOOKBACK`` stops both processes at
startup, and the default cap leaves the extended profile's versions as they
were (B4-1, B4-3)."""

from __future__ import annotations

import pytest

from opspilot.tools import otel_demo_ext
from opspilot.tools.profiles import MAX_LOOKBACK_ENV, PROFILE_ENV, select_profile


@pytest.mark.parametrize(
    ("profile", "value"),
    [
        ("otel-demo-ext", "14d"),
        ("otel-demo-ext", "30D"),
        ("otel-demo-ext", " 7d"),
        ("fixture", "7d"),
        ("otel-demo", "30d"),
        ("otel-demo", "1h"),
    ],
)
def test_an_unusable_cap_does_not_start(profile, value):
    with pytest.raises(SystemExit) as stopped:
        select_profile({PROFILE_ENV: profile, MAX_LOOKBACK_ENV: value})
    assert stopped.value.code == "MAX_LOOKBACK_INVALID"


@pytest.mark.parametrize("value", [None, "", "24h"])
def test_the_default_cap_keeps_the_extended_versions(value):
    env = {PROFILE_ENV: "otel-demo-ext"}
    if value is not None:
        env[MAX_LOOKBACK_ENV] = value
    versions = select_profile(env).versions()
    assert versions["tool_schema_revision"] == otel_demo_ext.TOOL_SCHEMA_REVISION


def test_a_longer_cap_is_a_different_tool_version():
    env = {PROFILE_ENV: "otel-demo-ext", MAX_LOOKBACK_ENV: "30d"}
    revision = select_profile(env).versions()["tool_schema_revision"]
    assert revision != otel_demo_ext.TOOL_SCHEMA_REVISION


def test_a_successor_under_a_lowered_cap_is_refused_and_creates_nothing():
    """#779 review: a 7d Run's successor after the cap is lowered to 24h
    would carry a frame every tool call refuses (WINDOW_TOO_LARGE); the
    control is refused instead and no Run is created."""
    from uuid import uuid4

    from opspilot.tools.otel_demo import TARGET_ID
    from tests.m1_tool_support import FakeClock
    from tests.m1_web_support import basic, build_workbench, post_form, same_origin

    headers = {**basic(), **same_origin()}
    env = {PROFILE_ENV: "otel-demo-ext"}
    clock = FakeClock()
    longer = select_profile({**env, MAX_LOOKBACK_ENV: "7d"})
    app, workbench, clock = build_workbench(clock=clock, tool_face=longer.face(clock))
    created = post_form(
        app,
        "/intake/ui",
        {
            "target_id": TARGET_ID,
            "question": "Why did checkout fail last week?",
            "idempotency_key": f"t779-{uuid4().hex}",
            "lookback": "7d",
        },
        headers=headers,
    )
    assert created.status == 201, created.text
    incident = created.json()["incident_id"]

    def control(action: str, generation: int):
        return post_form(
            app,
            f"/incidents/{incident}/control",
            {
                "action": action,
                "expected_generation": str(generation),
                "idempotency_key": f"{action}-{uuid4().hex}",
            },
            headers=headers,
        )

    assert control("cancel", 0).status == 200
    # Redeployed with the cap unset (24h).
    workbench.tool_face = select_profile(env).face(clock)
    refused = control("new_run", 1)
    assert refused.status == 409, refused.text
    assert refused.json()["code"] == "INPUT_INVALID"
    assert len(workbench.incidents.runs) == 1
