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
