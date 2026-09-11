import json

import pytest

from scripts.m0_environment.holmes_baseline import validate_question_scope_binding


def scope():
    return {"window": {"start": 1.0, "end": 301.0}, "policy_revision": "p1"}


def test_matching_embedded_scope_is_accepted():
    validate_question_scope_binding(
        json.dumps(
            {
                "run_id": "r1",
                "window": scope()["window"],
                "requested_window": scope()["window"],
                "scope_revision": "p1",
            }
        ),
        run_id="r1",
        scope=scope(),
    )


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("window", {"start": 2.0, "end": 302.0}, "QUESTION_SCOPE_MISMATCH"),
        ("scope_revision", "old", "QUESTION_SCOPE_MISMATCH"),
        ("run_id", "old", "QUESTION_RUN_MISMATCH"),
    ],
)
def test_stale_embedded_metadata_is_denied(field, value, error):
    data = {field: value}
    with pytest.raises(ValueError, match=error):
        validate_question_scope_binding(json.dumps(data), run_id="r1", scope=scope())
