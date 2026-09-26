"""Tool profile selection for the workbench and the worker.

A *profile* is the tool half of the product composition: the model-visible
face the workbench records into every Run's input, the ``versions`` both
processes must agree on (C3 §5: a Run recorded under one face is blocked on
claim by a worker running another), and the executor factory the worker
drives. Selection is by ``OPSPILOT_TOOL_PROFILE``:

* ``fixture`` (default): the canned Prometheus view, no network -- what CI,
  the tests and the local demo use;
* ``otel-demo``: real read-only Prometheus and Jaeger queries against the
  pinned OTel Demo lab (``opspilot.tools.otel_demo``), configured by
  ``OPSPILOT_OTEL_PROMETHEUS_URL``, ``OPSPILOT_OTEL_JAEGER_URL`` and the
  optional ``OPSPILOT_OTEL_TOKEN``.

Both processes must be started with the same value.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from opspilot.investigation.inputs import ToolFace
from opspilot.investigation.runner import ExecutorFactory
from opspilot.persistence import DurableStore
from opspilot.tools.executor import Clock, EvidenceSink
from opspilot.tools.fixture import (
    fixture_executor_factory,
    fixture_face,
    fixture_versions,
)
from opspilot.tools.otel_demo import (
    OtelDemoConfig,
    otel_demo_executor_factory,
    otel_demo_face,
    otel_demo_versions,
)

__all__ = ["PROFILE_ENV", "PROFILE_NAMES", "ToolProfile", "select_profile"]

PROFILE_ENV = "OPSPILOT_TOOL_PROFILE"
PROFILE_NAMES = ("fixture", "otel-demo")


@dataclass(frozen=True)
class ToolProfile:
    name: str
    face: Callable[[Clock], ToolFace]
    versions: Callable[[], dict[str, str]]
    executor_factory: Callable[[DurableStore, EvidenceSink, Clock], ExecutorFactory]


def _fixture() -> ToolProfile:
    return ToolProfile(
        name="fixture",
        face=lambda clock: fixture_face(),
        versions=fixture_versions,
        executor_factory=lambda store, evidence, clock: fixture_executor_factory(
            store, evidence=evidence, clock=clock
        ),
    )


def _otel_demo(env: Mapping[str, str]) -> ToolProfile:
    config = OtelDemoConfig.from_env(env)
    return ToolProfile(
        name="otel-demo",
        face=otel_demo_face,
        versions=otel_demo_versions,
        executor_factory=lambda store, evidence, clock: otel_demo_executor_factory(
            store, evidence=evidence, clock=clock, config=config
        ),
    )


def select_profile(env: Mapping[str, str]) -> ToolProfile:
    """The profile ``env`` names; an unknown name is a startup error."""
    name = env.get(PROFILE_ENV) or "fixture"
    if name == "fixture":
        return _fixture()
    if name == "otel-demo":
        return _otel_demo(env)
    raise SystemExit(f"{PROFILE_ENV} must be one of {', '.join(PROFILE_NAMES)}")
