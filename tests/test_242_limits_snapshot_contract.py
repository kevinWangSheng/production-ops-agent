"""#242 compaction PG round trip: the narrowed-window switch stays off product paths.

Written from the #242 contract pre-review (2026-10-24, section 5 items 1-2
and decision R-4) by an author who did not read the implementation branch:

1. every Run the workbench creates -- UI intake, event intake, cancel +
   new_run, and the timeout renewal after follow_up/correct -- records
   ``limits`` *equal* to ``M1_FROZEN_LIMITS`` (not merely ``within``);
   the product reads no environment variable that could narrow them;
2. a snapshot narrowed only in ``context_tokens`` keeps the input version,
   the tool face hash, the prompt revision set and the context policy
   revision of the default snapshot from the same face.

The PostgreSQL half (claim, compaction round trip, cross-claimer stubs,
script CLI) lives in
``tests/integration/test_242_compaction_pg_roundtrip_postgres.py``.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from opspilot.investigation.context import (
    InvestigationInput,
    context_policy_revision,
)
from opspilot.investigation.limits import M1_FROZEN_LIMITS, RunLimits
from opspilot.investigation.loop import DISCIPLINE_VARIANT, prompt_revision_versions
from opspilot.tools.fixture import FIXTURE_TARGET, fixture_face
from tests.m1_web_support import (
    basic,
    bearer,
    build_workbench,
    post_form,
    post_json,
    same_origin,
)

ROOT = Path(__file__).resolve().parents[1]
QUESTION = "Why is checkout erroring?"
FROZEN = M1_FROZEN_LIMITS.as_json()


def _workbench():
    return build_workbench(tool_face=fixture_face())


def _submit(app, *, key):
    response = post_form(
        app,
        "/intake/ui",
        {"target_id": FIXTURE_TARGET, "question": QUESTION, "idempotency_key": key},
        headers={**basic(), **same_origin()},
    )
    assert response.status == 201, response.text
    body = response.json()
    return UUID(body["incident_id"]), UUID(body["run_id"])


def _control(app, incident, fields):
    response = post_form(
        app,
        f"/incidents/{incident}/control",
        fields,
        headers={**basic(), **same_origin()},
    )
    assert response.status == 200, response.text
    return response


def _current_run(workbench, incident):
    summary = workbench.incidents.find_incident(incident)
    assert summary is not None
    return summary.current_run_id


def _recorded_limits(workbench, run_id):
    recorded = workbench.incidents.runs[run_id]["input"]
    assert recorded is not None, "Run was created without an input snapshot"
    # The raw JSON the row carries, and the decoded snapshot, both equal the
    # freeze: a narrowed (or widened) value is a product-path leak.
    assert recorded["limits"] == FROZEN
    assert InvestigationInput.from_json(recorded).limits == M1_FROZEN_LIMITS
    return recorded


# -- 1. web paths record the frozen limits exactly ---------------------------


def test_ui_intake_run_records_exactly_the_frozen_limits():
    app, workbench, _ = _workbench()
    incident, run_id = _submit(app, key="242-ui")
    assert _current_run(workbench, incident) == run_id
    _recorded_limits(workbench, run_id)


def test_event_intake_run_records_exactly_the_frozen_limits():
    app, workbench, _ = _workbench()
    response = post_json(
        app,
        "/intake/events",
        {
            "source": "alertmanager",
            "external_event_id": "alert-242",
            "target_id": FIXTURE_TARGET,
            "question": "CheckoutErrorRateHigh firing",
        },
        headers=bearer(),
    )
    assert response.status == 201, response.text
    _recorded_limits(workbench, UUID(response.json()["run_id"]))


def test_new_run_successor_records_exactly_the_frozen_limits():
    app, workbench, _ = _workbench()
    incident, first = _submit(app, key="242-new-run")
    _control(
        app,
        incident,
        {"action": "cancel", "expected_generation": "0", "idempotency_key": "c"},
    )
    _control(
        app,
        incident,
        {"action": "new_run", "expected_generation": "1", "idempotency_key": "n"},
    )
    fresh = _current_run(workbench, incident)
    assert fresh != first
    _recorded_limits(workbench, fresh)


@pytest.mark.parametrize("action", ["follow_up", "correct"])
def test_renewed_successor_after_a_note_records_exactly_the_frozen_limits(action):
    app, workbench, clock = _workbench()
    incident, old = _submit(app, key=f"242-renew-{action}")
    _recorded_limits(workbench, old)
    # A Run claimed by a worker that never finished, swept past its deadline.
    versions = workbench.incidents.runs[old]["versions"]
    workbench.incidents.claim(incident, old, uuid4(), versions, 600)
    clock.advance(601)
    assert workbench.snapshot(incident)["run"]["state"] == "waiting_human"
    _control(
        app,
        incident,
        {
            "action": action,
            "expected_generation": "0",
            "idempotency_key": f"note-{action}",
            "text": "Also compare against the previous hour.",
        },
    )
    fresh = _current_run(workbench, incident)
    assert fresh != old
    assert workbench.incidents.runs[fresh]["state"] == "queued"
    _recorded_limits(workbench, fresh)


_LIMIT_ENV = re.compile(
    # Product configuration is the ``OPSPILOT_*`` environment namespace.
    r"""["']OPSPILOT_[A-Z0-9_]*"""
    r"""(?:CONTEXT|LIMIT|OUTPUT_TOKENS|MODEL_REQUESTS)[A-Z0-9_]*["']"""
)


def test_no_product_module_names_an_environment_variable_for_run_limits():
    """R-4: the switch is a script argument only; no product env can narrow it."""
    hits = []
    for path in sorted((ROOT / "opspilot").rglob("*.py")):
        if "migrations" in path.parts:
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if _LIMIT_ENV.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert hits == []


# -- 2. narrowing context_tokens changes no version or hash ------------------


@pytest.mark.parametrize(
    "context_tokens",
    [
        M1_FROZEN_LIMITS.output_tokens + 1,
        76_536,
        M1_FROZEN_LIMITS.context_tokens,
    ],
)
def test_a_narrowed_window_keeps_every_version_and_hash(context_tokens):
    assert M1_FROZEN_LIMITS.output_tokens < context_tokens <= 1_000_000
    face = fixture_face()
    deadline = datetime.now(timezone.utc) + timedelta(minutes=10)
    run_id = str(uuid4())
    common = {
        "run_id": run_id,
        "question": QUESTION,
        "target_id": FIXTURE_TARGET,
        "deadline": deadline,
        "model_requests": 8,
    }
    narrowed_limits = replace(M1_FROZEN_LIMITS, context_tokens=context_tokens)
    assert isinstance(narrowed_limits, RunLimits)
    assert narrowed_limits.within(M1_FROZEN_LIMITS)
    default = face.input_for(**common)
    narrowed = face.input_for(**common, limits=narrowed_limits)
    policy_before = context_policy_revision()
    prompts_before = prompt_revision_versions(DISCIPLINE_VARIANT)

    assert narrowed.version == default.version
    assert narrowed.tool_face_sha256 == default.tool_face_sha256
    as_json, default_json = narrowed.as_json(), default.as_json()
    assert as_json["version"] == default_json["version"]
    assert as_json["tool_face_sha256"] == default_json["tool_face_sha256"]
    # Only the limits differ between the two recorded snapshots.
    assert {k for k in as_json if as_json[k] != default_json[k]} <= {"limits"}
    assert as_json["limits"]["context_tokens"] == context_tokens
    # The round trip through JSON keeps the narrowed value (the snapshot is
    # where a later claimer reads it from).
    assert InvestigationInput.from_json(as_json).limits == narrowed_limits
    assert context_policy_revision() == policy_before
    assert prompt_revision_versions(DISCIPLINE_VARIANT) == prompts_before
