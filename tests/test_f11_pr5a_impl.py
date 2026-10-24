"""Implementation tests for F11 PR-5a without PostgreSQL: the verdict
projection (D5), the D3 policy resolution, the offered controls and the
pages of a workbench that has no release store."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from opspilot.acceptance_release import release_outcome, release_verdict
from opspilot.observation import release_policy
from opspilot.observation.releases import calibrated_revision_for
from opspilot.persistence import PersistenceError
from opspilot.web import Authenticator, create_app
from opspilot.web.service import _release_actions
from tests.f11_pr3_support import build_app, config
from tests.m1_web_support import basic, call, same_origin

TARGET = {
    "integration_id": "i",
    "cluster_uid": "c",
    "namespace": "otel-demo",
    "resource_uid": "uid",
    "workload": "checkout",
}
DIGEST = "sha256:" + "a" * 64
#: F7 C3 sections 8-9: words no route may carry.
MUTATION_WORDS = (
    "approve",
    "block",
    "deploy",
    "rollback",
    "rollout",
    "restart",
    "promote",
    "gate",
    "release",
    "target",
)


@pytest.mark.parametrize(
    ("status", "verdict"),
    [
        ("pending", "pending"),
        ("observing", "observing"),
        ("healthy", "healthy"),
        ("anomalous", "degraded"),
        ("unknown", "unknown"),
        ("cancelled", "cancelled"),
    ],
)
def test_verdict_projection(status, verdict):
    assert release_verdict(status) == verdict


def test_an_unknown_status_is_not_projected():
    with pytest.raises(ValueError, match="UNKNOWN_RELEASE_STATUS"):
        release_verdict("approved")


def _catalog(tmp_path, monkeypatch, targets: list[dict[str, str]], profile="hp@1"):
    policies = []
    for n, target in enumerate(targets):
        content = {
            "format_version": 1,
            "target": target,
            "health_profile_revision": profile,
            "health_profile_sha256": "d" * 64,
            "min_tracking_seconds": 2,
            "evaluator": "release-after-v1",
            "mapping": "pod-uid-replicaset-image-v1",
            "evidence_path": f"docs/evidence/{n}.md",
            "evidence_sha256": "c" * 64,
            "approval_ref": "test",
            "approved_at": "2026-10-24T00:00:00Z",
        }
        digest = release_policy.sha256(release_policy.canonical_json(content).encode())
        policies.append(
            {
                "policy_id": f"p{n}",
                "revision": f"p{n}@{digest[:12]}",
                "content_sha256": digest,
                "content": content,
            }
        )
    path = tmp_path / "catalog.json"
    path.write_text(
        release_policy.canonical_json({"format_version": 1, "policies": policies})
    )
    monkeypatch.setattr(release_policy, "RELEASE_POLICY_CATALOG_PATH", path)
    return [p["revision"] for p in policies]


def _release(**overrides: Any) -> SimpleNamespace:
    values = {
        "target_snapshot": dict(TARGET),
        "health_profile_revision": "hp@1",
        "calibrated_policy_revision": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_resume_policy_is_the_frozen_revision_once_set(tmp_path, monkeypatch):
    _catalog(tmp_path, monkeypatch, [])
    release = _release(calibrated_policy_revision="frozen@1")
    assert calibrated_revision_for(release) == "frozen@1"  # type: ignore[arg-type]


def test_resume_policy_is_the_one_exact_catalog_entry(tmp_path, monkeypatch):
    other = {**TARGET, "workload": "payment"}
    revisions = _catalog(tmp_path, monkeypatch, [other, TARGET])
    assert calibrated_revision_for(_release()) == revisions[1]  # type: ignore[arg-type]
    with pytest.raises(PersistenceError, match="RELEASE_POLICY_UNCALIBRATED"):
        calibrated_revision_for(_release(health_profile_revision="hp@2"))  # type: ignore[arg-type]


def test_resume_policy_refuses_several_entries(tmp_path, monkeypatch):
    _catalog(tmp_path, monkeypatch, [TARGET, TARGET])
    with pytest.raises(PersistenceError, match="RELEASE_POLICY_AMBIGUOUS"):
        calibrated_revision_for(_release())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("status", "paused", "sessions", "offered"),
    [
        ("pending", False, [], ["pause", "cancel"]),
        ("pending", True, [], ["resume", "cancel"]),
        ("observing", False, [{"state": "authorized"}], ["pause", "cancel"]),
        ("observing", False, [{"state": "revoked"}], ["pause", "resume", "cancel"]),
        ("observing", True, [{"state": "revoked"}], ["resume", "cancel"]),
        ("healthy", False, [], []),
        ("anomalous", False, [], []),
        ("unknown", False, [], []),
        ("cancelled", False, [], []),
    ],
)
def test_offered_controls(status, paused, sessions, offered):
    release = SimpleNamespace(status=status, paused=paused)
    assert _release_actions(release, sessions) == offered


def test_new_routes_name_no_mutation():
    app, _ = build_app(None)
    paths = {route.path for route in app.routes if "version-changes" in route.path}
    assert paths == {
        "/intake/version-changes",
        "/version-changes",
        "/version-changes/{observation_id}",
        "/version-changes/{observation_id}/control",
    }
    for path in paths - {"/intake/version-changes"}:
        for word in MUTATION_WORDS:
            assert word not in path


def test_pages_without_a_release_store():
    app, workbench = build_app(None)
    assert workbench._release_store() is None
    listed = call(app, "GET", "/version-changes", headers=basic())
    assert listed.status == 200 and listed.json() == {"releases": []}
    html = call(
        app, "GET", "/version-changes", headers={**basic(), "accept": "text/html"}
    )
    assert html.status == 200 and "No version changes observed yet" in html.text
    missing = call(app, "GET", f"/version-changes/{uuid4()}", headers=basic())
    assert missing.status == 404 and missing.json()["code"] == "UNKNOWN_RELEASE"
    form = {"content-type": "application/x-www-form-urlencoded", **basic()}
    body = b"action=pause&expected_control_generation=0&idempotency_key=k"
    path = f"/version-changes/{uuid4()}/control"
    assert call(app, "POST", path, headers=form, body=body).status == 403
    unknown = call(app, "POST", path, headers={**form, **same_origin()}, body=body)
    assert unknown.status == 404 and unknown.json()["code"] == "UNKNOWN_RELEASE"
    approve = body.replace(b"pause", b"approve")
    refused = call(app, "POST", path, headers={**form, **same_origin()}, body=approve)
    assert refused.status == 400 and refused.json()["code"] == "INVALID_INPUT"


def test_routes_register_without_touching_the_workbench():
    class Untouchable:
        def __getattr__(self, item: str) -> Any:
            raise AssertionError(item)

    create_app(Untouchable(), Authenticator(config()), Untouchable())  # type: ignore[arg-type]


def _detail(status: str, *, ended_reason=None, audit=(), link=None) -> dict[str, Any]:
    release = SimpleNamespace(
        observation_id=uuid4(),
        status=status,
        ended_reason=ended_reason,
        paused=False,
        source="deployer",
        source_release_id="rel-1",
        released_at=datetime(2026, 10, 24, tzinfo=timezone.utc),
        target_snapshot=dict(TARGET),
        before_revision=SimpleNamespace(model_dump=lambda mode: {"d": DIGEST}),
        after_revision=SimpleNamespace(model_dump=lambda mode: {"d": DIGEST}),
        calibrated_policy_revision="p@1",
        used_sample_count=3,
        max_samples=40,
        incident_id=None,
    )
    return {"release": release, "audit": list(audit), "link": link}


def _scenario(detail: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        scenario_id="s",
        kind="release",
        subject_id=str(detail["release"].observation_id),
    )


def _audit(action, outcome, principal, reason=None):
    return {
        "action": action,
        "outcome": outcome,
        "principal_kind": principal,
        "reason": reason,
    }


def test_unknown_is_a_handoff_with_its_reason():
    detail = _detail(
        "unknown",
        ended_reason="deadline_expired",
        audit=[
            _audit("authorize", "applied", "system"),
            _audit("pause", "applied", "basic_auth"),
            _audit("resume", "rejected", "basic_auth", "CONTROL_CONFLICT"),
            _audit("deadline_expired", "applied", "observer"),
        ],
    )
    outcome = release_outcome(_scenario(detail), detail)
    assert outcome.verdict == "unknown" and outcome.concluded
    assert outcome.handoff_reasons == ("deadline_expired",)
    assert outcome.human_interaction == "handoff"
    assert outcome.actions == ("authorize", "pause", "deadline_expired")
    assert outcome.human_controls == (
        "pause:applied",
        "resume:rejected:CONTROL_CONFLICT",
    )


def test_cancelled_is_not_a_conclusion():
    detail = _detail(
        "cancelled",
        ended_reason="cancelled",
        audit=[_audit("cancel", "applied", "basic_auth")],
    )
    outcome = release_outcome(_scenario(detail), detail)
    assert outcome.verdict == "cancelled" and not outcome.concluded
    assert outcome.handoff_reasons == ()
    assert outcome.human_interaction == "human_control"


def test_projection_refuses_another_subject():
    detail = _detail("healthy")
    scenario = SimpleNamespace(scenario_id="s", kind="release", subject_id="x")
    with pytest.raises(ValueError, match="SUBJECT_MISMATCH"):
        release_outcome(scenario, detail)
    incident = SimpleNamespace(
        scenario_id="s",
        kind="incident",
        subject_id=str(detail["release"].observation_id),
    )
    with pytest.raises(ValueError, match="SUBJECT_MISMATCH"):
        release_outcome(incident, detail)
