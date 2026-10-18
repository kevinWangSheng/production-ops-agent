"""A5 defaults and credential-free registration DTO public contracts."""

from dataclasses import FrozenInstanceError, asdict, fields
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest

from opspilot.investigation.runner import InvestigationRunner
from opspilot.tools.otel_demo import OtelDemoConfig, otel_demo_registrations
from opspilot.worker import RecoveryPlan
from tests.acceptance import eval_c_support as s
from tests.acceptance.test_eval_c_live_driver import offline  # noqa: F401


def test_runner_hooks_default_to_none():
    defaults = {field.name: field.default for field in fields(InvestigationRunner)}
    assert defaults["capture_delivery"] is None
    assert defaults["attempt_wrapper"] is None


def test_runner_explicit_none_and_omitted_hooks_have_same_terminal_outcome():
    """An already-completed incident stays completed without model/tool work.

    Claimed-Run parity belongs to the durable suites named in r3 section 8.
    This regression exercises resume's observable terminal outcome offline.
    """
    incident_id, run_id = UUID(int=1), UUID(int=2)
    plan = RecoveryPlan(
        incident_id=incident_id,
        run_id=run_id,
        state="completed",
        control_generation=1,
        run={"state": "completed", "run_id": str(run_id)},
        steps=(),
        pending_tools=(),
        conclusion=None,
    )
    outcomes = []
    for kwargs in ({}, {"capture_delivery": None, "attempt_wrapper": None}):
        forbidden = Mock(side_effect=AssertionError("terminal Run started work"))
        worker = SimpleNamespace(
            recover=lambda _: plan, resume=forbidden, claim=forbidden
        )
        runner = InvestigationRunner(
            store=Mock(),
            worker=worker,
            model=SimpleNamespace(complete=forbidden),
            executor_factory=forbidden,
            clock=s.Clock(),
            **kwargs,
        )
        outcomes.append(runner.resume(incident_id))
        forbidden.assert_not_called()
    assert outcomes[0] == outcomes[1]
    assert outcomes[0].status == "already_completed"


def test_public_otel_registrations_are_frozen_and_credential_free():
    config = OtelDemoConfig(
        token=s.SECRET,
        prometheus_username="CANARY-USER",
        prometheus_password="CANARY-PASSWORD-82e1",
    )
    dto = otel_demo_registrations(config)
    assert isinstance(dto.tools, tuple) and dto.tools
    assert isinstance(dto.targets, tuple) and dto.targets
    visible = repr(asdict(dto))
    for marker in (s.SECRET, "CANARY-USER", "CANARY-PASSWORD-82e1"):
        assert marker not in visible
    assert all(hasattr(target, "credential_ref") for target in dto.targets)
    with pytest.raises(FrozenInstanceError):
        dto.tools = ()
    default = otel_demo_registrations()
    assert [tool.name for tool in default.tools] == [tool.name for tool in dto.tools]
    assert [target.target_id for target in default.targets] == [
        target.target_id for target in dto.targets
    ]
