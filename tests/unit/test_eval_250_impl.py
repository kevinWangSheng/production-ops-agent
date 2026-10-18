"""r11: current request identities are checked before any judge transport call."""

import json
from dataclasses import replace

import pytest

from opspilot.evaluation.calibration_flow import load_verified_calibration
from tests.unit.test_eval_b3_impl import (
    invoke,
    make_input,
    r9_identity_artifact,
)


@pytest.fixture(scope="module")
def verified_calibration(tmp_path_factory):
    root, pack, configs = r9_identity_artifact.__wrapped__(tmp_path_factory)
    return load_verified_calibration(
        root / "calibration.json", pack=pack, config=configs["zai"]
    )


@pytest.mark.parametrize("certified", [False, True])
@pytest.mark.parametrize(
    "field",
    [
        "subject_whitespace",
        "subject_unicode",
        "evidence",
        "report",
        "service",
        "operation",
    ],
)
def test_judge_rejects_noncanonical_request_identifiers(
    field, certified, verified_calibration
):
    request, diagnoses, verdict = make_input()
    if field.startswith("subject_"):
        subject = "subject with space" if field == "subject_whitespace" else "ｓubject"
        request = replace(
            request,
            spec=replace(
                request.spec,
                stimulus=replace(request.spec.stimulus, subject_id=subject),
            ),
        )
    elif field == "evidence":
        request = replace(
            request,
            delivered={"e1": replace(request.delivered["e1"], evidence_id="bad id")},
        )
    elif field == "report":
        raw = json.loads(request.report.raw)
        raw["claims"][0]["evidence_ids"] = [" e1 "]
        request = replace(request, report=replace(request.report, raw=json.dumps(raw)))
    else:
        request = replace(
            request,
            answer=replace(
                request.answer,
                injected=replace(
                    request.answer.injected,
                    **{
                        field: "bad service" if field == "service" else "bad  operation"
                    },
                ),
            ),
        )
    observation, transport = invoke(
        request,
        diagnoses,
        verdict,
        calibration=verified_calibration if certified else None,
    )
    assert observation.result.status == "invalid"
    assert observation.error_code == "SCHEMA_INVALID"
    assert observation.result.calibrated is certified
    assert transport.calls == []


@pytest.mark.parametrize("certified", [False, True])
def test_judge_allows_canonical_request_identifiers(certified, verified_calibration):
    observation, transport = invoke(
        calibration=verified_calibration if certified else None
    )
    assert observation.result.status == "ok"
    assert observation.error_code is None
    assert observation.result.calibrated is certified
    assert len(transport.calls) == 1
