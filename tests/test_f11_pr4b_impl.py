"""Implementation tests for F11 PR-4b without PostgreSQL: the v6 input, its
bounded release fact, the release frame and the prompt revision."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest

from opspilot.investigation import context as context_module
from opspilot.investigation import loop as loop_module
from opspilot.investigation.context import (
    INPUT_VERSION_RELEASE,
    RELEASE_CONTEXT_BOUNDARY,
    RELEASE_CONTEXT_TOTAL_CHARS,
    RELEASE_OBSERVATION,
    ContextError,
    InvestigationInput,
    initial_messages,
)
from opspilot.investigation.inputs import ReleaseAnchor, release_observation_fact
from opspilot.tools.fixture import fixture_face
from opspilot.tools.registry import canonical

RELEASED = datetime(2026, 10, 22, 10, 0, 0, 500000, tzinfo=timezone.utc)
CONSUMED = RELEASED + timedelta(minutes=7)
DIGEST = "sha256:" + "a" * 64


def _release(**overrides: Any) -> dict[str, Any]:
    release = {
        "observation_id": str(uuid4()),
        "source": "deployer",
        "source_release_id": "rel-1",
        "target_snapshot": {
            "integration_id": "i",
            "cluster_uid": "c",
            "namespace": "otel-demo",
            "resource_uid": "uid",
            "workload": "checkout",
        },
        "released_at": RELEASED.isoformat(),
        "before_revision": {"images": {"app": DIGEST}, "pod_template_digest": DIGEST},
        "after_revision": {"images": {"app": DIGEST}, "pod_template_digest": DIGEST},
        "auxiliary": {
            "before_pod_template_hash": "aaa",
            "after_pod_template_hash": "bbb",
        },
    }
    release.update(overrides)
    return release


def _ending(**evaluation: Any) -> dict[str, Any]:
    return {
        "ending_id": str(uuid4()),
        "anomaly_key": "release-anomaly:x",
        "sample_id": str(uuid4()),
        "calibrated_policy_revision": "impl@1",
        "release_evaluation": {
            "verdict": "degraded",
            "reason_codes": ["AFTER_DEGRADED"],
            "instances": [{"pod_name": "p", "replicaset_name": "checkout-bbb"}],
            "signals": [],
            **evaluation,
        },
    }


ANCHOR = ReleaseAnchor(released_at=RELEASED, consumed_at=CONSUMED)


def _input(fact: dict[str, Any]) -> InvestigationInput:
    fresh = fixture_face().input_for(
        run_id=str(uuid4()),
        question="q",
        target_id="uid",
        deadline=CONSUMED + timedelta(minutes=10),
        model_requests=3,
        release=ANCHOR,
    )
    return InvestigationInput(
        **{
            **fresh.__dict__,
            "scope_facts": {**fresh.scope_facts, RELEASE_OBSERVATION: fact},
        }
    )


def test_release_frame_ends_at_consumption_and_anchors_at_release():
    start, end = ANCHOR.frame
    assert end == CONSUMED.replace(microsecond=0)
    assert end - start == timedelta(hours=24)
    assert ANCHOR.anchor == (RELEASED.replace(microsecond=0), None)
    assert ANCHOR.default_query_window[0] == RELEASED.replace(
        microsecond=0
    ) - timedelta(hours=1)
    late = ReleaseAnchor(
        released_at=CONSUMED + timedelta(hours=1), consumed_at=CONSUMED
    )
    assert late.anchor[1] == "future"


def test_v6_round_trips_and_is_shown_as_one_labelled_message():
    fact = release_observation_fact(_release(), _ending(), None, ANCHOR)
    assert fact["comparison"]["status"] == "unavailable"
    built = _input(fact)
    assert built.version == INPUT_VERSION_RELEASE
    again = InvestigationInput.from_json(built.as_json())
    assert again.scope_facts[RELEASE_OBSERVATION] == fact
    policy = built.evidence_context["time_policies"][0]  # type: ignore[index]
    assert policy["anchor_rule"] == "release_released_at"
    messages, _ = initial_messages(built, evidence_context=None)
    released = [
        m for m in messages if m["content"].startswith(RELEASE_CONTEXT_BOUNDARY)
    ]
    assert len(released) == 1
    assert released[0]["content"] == RELEASE_CONTEXT_BOUNDARY + "\n" + canonical(fact)


def test_fact_is_bounded_printable_and_deterministic():
    huge = "x" * 5000 + "\u0007"
    release = _release(
        source_release_id=huge,
        before_revision={
            "images": {f"c{i}": DIGEST for i in range(40)},
            "pod_template_digest": DIGEST,
        },
    )
    ending = _ending(
        instances=[{"pod_name": huge, "replicaset_name": huge} for _ in range(100)],
        reason_codes=[huge] * 100,
        signals=[
            {"scope": "after", "signal_name": huge, "verdict": "degraded", "value": 1.0}
        ]
        * 100,
    )
    first = release_observation_fact(release, ending, None, ANCHOR)
    second = release_observation_fact(release, ending, None, ANCHOR)
    assert first == second and first["truncated"] is True
    assert len(canonical(first)) <= RELEASE_CONTEXT_TOTAL_CHARS
    assert "\u0007" not in canonical(first)
    _input(first)  # the validator accepts what the builder produced


def test_validator_refuses_unbounded_or_unknown_facts():
    fact = release_observation_fact(_release(), _ending(), None, ANCHOR)
    with pytest.raises(ContextError, match="INPUT_INVALID"):
        _input({**fact, "extra": 1})
    with pytest.raises(ContextError, match="INPUT_INVALID"):
        _input({**fact, "source": "y" * 10_000})


def test_unknown_later_version_stays_incompatible():
    fact = release_observation_fact(_release(), _ending(), None, ANCHOR)
    raw = _input(fact).as_json()
    with pytest.raises(ContextError, match="INCOMPATIBLE_STATE"):
        InvestigationInput.from_json(
            {**raw, "version": "opspilot-investigation-input-v7"}
        )


def test_prompt_revision_moves_with_the_release_boundary(monkeypatch):
    before = loop_module.prompt_revision_versions()
    monkeypatch.setattr(loop_module, "RELEASE_CONTEXT_BOUNDARY", "changed")
    assert loop_module.prompt_revision_versions() != before
    assert context_module.RELEASE_CONTEXT_BOUNDARY == RELEASE_CONTEXT_BOUNDARY
