"""Release input strings must survive UTF-8 and PostgreSQL text/JSONB."""

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from opspilot.observation.releases import (
    ReleaseAuxiliary,
    ReleaseEvent,
    ReleaseRevision,
)

DIGEST = "sha256:" + "a" * 64
TEXT_PATHS = [
    ("source",),
    ("source_release_id",),
    *[
        (revision, field)
        for revision in ("before", "after")
        for field in ("image_key", "image_digest", "pod_template_digest")
    ],
    *[
        ("auxiliary", field)
        for field in (
            "deployment_uid",
            "before_replicaset_uid",
            "after_replicaset_uid",
            "before_pod_template_hash",
            "after_pod_template_hash",
        )
    ],
]


def _payload():
    return {
        "format_version": 1,
        "source": "deployments",
        "source_release_id": "release-1",
        "released_at": datetime(2026, 10, 13, tzinfo=timezone.utc),
        "before": {"images": {"app": DIGEST}, "pod_template_digest": DIGEST},
        "after": {"images": {"app": DIGEST}, "pod_template_digest": DIGEST},
        "auxiliary": {},
    }


@pytest.mark.parametrize("path", TEXT_PATHS)
@pytest.mark.parametrize(
    "invalid",
    ["\x00", "\ud800", "\udfff"],
    ids=["nul", "high-surrogate", "low-surrogate"],
)
@pytest.mark.parametrize("boundary", ["python", "json"])
def test_release_rejects_unstorable_strings_at_every_input_path(
    path, invalid, boundary
):
    payload = _payload()
    if len(path) == 1:
        payload[path[0]] += invalid
    elif path[1] == "image_key":
        payload[path[0]]["images"] = {"app" + invalid: DIGEST}
    elif path[1] == "image_digest":
        payload[path[0]]["images"]["app"] += invalid
    else:
        payload[path[0]][path[1]] = (
            DIGEST if path[1] == "pod_template_digest" else "value"
        ) + invalid
    with pytest.raises(ValidationError):
        if boundary == "python":
            ReleaseEvent.model_validate(payload)
        else:
            payload["released_at"] = payload["released_at"].isoformat()
            ReleaseEvent.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "invalid",
    ["\x00", "\ud800", "\udfff"],
    ids=["nul", "high-surrogate", "low-surrogate"],
)
def test_standalone_nested_dtos_reject_unstorable_text(invalid):
    with pytest.raises(ValidationError):
        ReleaseRevision(images={"app" + invalid: DIGEST}, pod_template_digest=DIGEST)
    with pytest.raises(ValidationError):
        ReleaseAuxiliary(deployment_uid="deployment" + invalid)


def test_release_keeps_valid_unicode_and_json_surrogate_pairs():
    payload = _payload()
    text = "发布-🚀"
    payload["source"] = payload["source_release_id"] = text
    for revision in ("before", "after"):
        payload[revision]["images"] = {text: DIGEST}
    for path in TEXT_PATHS:
        if path[0] == "auxiliary":
            payload["auxiliary"][path[1]] = text
    event = ReleaseEvent.model_validate(payload)
    assert event.source == event.source_release_id == text
    assert event.before.images == event.after.images == {text: DIGEST}
    assert set(event.auxiliary.model_dump(exclude_none=True).values()) == {text}
    payload["released_at"] = payload["released_at"].isoformat()
    assert ReleaseEvent.model_validate_json(json.dumps(payload)) == event
