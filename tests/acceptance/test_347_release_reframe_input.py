"""#347 public input contract: policy-only replacement and source separation.

These tests complement the PG/control/gateway cases. F11's existing record
builders supply valid release facts; assertions concern the public input API,
not SQL, implementation helpers or call order.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import timedelta

import pytest

from opspilot import schema
from opspilot.investigation.context import (
    KNOWN_INPUT_VERSIONS,
    ContextError,
    InvestigationInput,
)
from opspilot.investigation.inputs import (
    ReleaseAnchor,
    continuation_input,
    release_observation_fact,
)
from opspilot.persistence.grants import RUNTIME_GRANTS
from opspilot.tools.fixture import fixture_face, fixture_versions
from opspilot.tools.otel_demo import otel_demo_versions
from tests.test_f11_pr4b_impl import CONSUMED, RELEASED, _ending, _release


def _original():
    anchor = ReleaseAnchor(released_at=RELEASED, consumed_at=CONSUMED)
    fresh = fixture_face().input_for(
        run_id="347-first",
        question="Investigate this release",
        target_id="uid",
        deadline=CONSUMED + timedelta(minutes=10),
        model_requests=3,
        release=anchor,
    )
    context = dict(fresh.evidence_context)
    # A legal inherited view in the input, with its original policy ref.
    # A second policy verifies replacement is confined to time_policies[0].
    context["view_bindings"] = {
        "ev-inherited": {
            "status": "ok",
            "citable_as_fact": True,
            "target_refs": ["uid"],
            "time_scope_refs": [context["time_policies"][0]["id"]],
        }
    }
    context["time_policies"] = [
        *context["time_policies"],
        {
            "id": "347-current",
            "mode": "current",
            "all_authorized_targets": True,
            "max_source_age_seconds": 60,
        },
    ]
    return replace(
        fresh,
        evidence_context=context,
        scope_facts={
            **fresh.scope_facts,
            "release_observation": release_observation_fact(
                _release(), _ending(), None, anchor
            ),
        },
    )


def _anchor(created):
    try:
        return ReleaseAnchor(
            released_at=RELEASED, consumed_at=CONSUMED, frame_end=created
        )
    except TypeError as exc:
        if "unexpected keyword argument 'frame_end'" not in str(exc):
            raise
        pytest.fail(
            "#347 release reframe is not implemented: ReleaseAnchor.frame_end is missing",
            pytrace=False,
        )


def _continue(snapshot, **kwargs):
    try:
        return continuation_input(snapshot, **kwargs)
    except TypeError as exc:
        if "unexpected keyword argument 'release'" not in str(exc):
            raise
        pytest.fail(
            "#347 release continuation is not implemented: continuation_input(release=...) is missing",
            pytrace=False,
        )


@pytest.mark.parametrize("hours,adjusted", [(2, None), (26, "before_frame")])
def test_ac1_ac7_release_anchor_moves_frame_and_clamps_at_old_release(hours, adjusted):
    """AC1/7: release source time remains the anchor of a moved 24h frame."""
    created = CONSUMED + timedelta(hours=hours)
    moved = _anchor(created)
    end = created.replace(microsecond=0)
    start = end - timedelta(hours=24)
    expected_anchor = max(RELEASED.replace(microsecond=0), start)
    assert moved.frame == (start, end)
    assert moved.anchor == (expected_anchor, adjusted)
    assert moved.default_query_window == (
        max(expected_anchor - timedelta(hours=1), start),
        end,
    )
    assert moved.scope_fact() == {
        "anchor": expected_anchor.isoformat(),
        "adjusted": adjusted,
    }


def test_ac1_only_first_policy_fields_move_and_inherited_refs_and_handoff_stay():
    """AC1/7/10 plus Desired behavior: inherited refs and handoff unchanged."""
    previous = _original()
    snapshot = {
        "run": {"run_id": "347-first", "input": previous.as_json()},
        "steps": [],
    }
    kwargs = dict(
        new_run_id="347-next",
        deadline=CONSUMED + timedelta(hours=3),
        authorized_targets=frozenset({"uid"}),
    )
    baseline = continuation_input(snapshot, **kwargs)
    carried = baseline.evidence_context["view_bindings"]["ev-inherited"]
    assert carried["time_scope_refs"] == [
        previous.evidence_context["time_policies"][0]["id"]
    ]
    moved = _continue(
        snapshot, **kwargs, release=_anchor(kwargs["deadline"] - timedelta(minutes=10))
    )
    assert moved.question == baseline.question
    assert moved.scope_facts == baseline.scope_facts
    assert moved.version == baseline.version == "opspilot-investigation-input-v6"
    context = moved.evidence_context
    assert {k: v for k, v in context.items() if k != "time_policies"} == {
        k: v for k, v in baseline.evidence_context.items() if k != "time_policies"
    }
    assert (
        context["time_policies"][1:] == baseline.evidence_context["time_policies"][1:]
    )
    after, before = (
        context["time_policies"][0],
        baseline.evidence_context["time_policies"][0],
    )
    permitted = {"window", "anchor", "default_query_window"}
    assert {k: v for k, v in after.items() if k not in permitted} == {
        k: v for k, v in before.items() if k not in permitted
    }
    assert (
        after["window"]["end"]
        == (kwargs["deadline"] - timedelta(minutes=10))
        .replace(microsecond=0)
        .isoformat()
    )
    assert InvestigationInput.from_json(moved.as_json()) == moved


def test_release_argument_on_input_without_release_fact_is_input_invalid():
    """Key interface: a submitted Run cannot acquire a release reframe."""
    submitted = fixture_face().input_for(
        run_id="347-submitted",
        question="q",
        target_id="uid",
        deadline=CONSUMED + timedelta(minutes=10),
        model_requests=3,
    )
    snapshot = {
        "run": {"run_id": "347-submitted", "input": submitted.as_json()},
        "steps": [],
    }
    with pytest.raises(ContextError) as caught:
        _continue(
            snapshot,
            new_run_id="347-next",
            deadline=CONSUMED + timedelta(hours=3),
            authorized_targets=frozenset({"uid"}),
            release=ReleaseAnchor(released_at=RELEASED, consumed_at=CONSUMED),
        )
    assert caught.value.code == "INPUT_INVALID"


def test_ac10_versions_revisions_alembic_head_and_all_runtime_grants_are_frozen():
    """AC10: pin the entire public runtime-grant declaration on pre-#347 main."""
    assert KNOWN_INPUT_VERSIONS == tuple(
        f"opspilot-investigation-input-v{i}" for i in range(1, 7)
    )
    assert fixture_versions()["tool_schema_revision"] == "fixture-5"
    assert otel_demo_versions()["tool_schema_revision"] == "otel-demo-a3a95a0f888e"
    assert (
        fixture_versions()["prompt_revision"]
        == otel_demo_versions()["prompt_revision"]
        == "prompt-replay-candidate-bef70649a3a8"
    )
    assert schema.head_revision() == "0023_retention_raw_cleanup"
    grants = {
        role: {table: sorted(values) for table, values in tables.items()}
        for role, tables in RUNTIME_GRANTS.items()
    }
    # Fingerprint of every role/table/grant, rather than only touched tables.
    frozen = hashlib.sha256(
        json.dumps(grants, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert frozen == "d4d18f74ad4263b6a0d7695f1c1cec25e3d94e2df1f30be25d151cf674861c09"
