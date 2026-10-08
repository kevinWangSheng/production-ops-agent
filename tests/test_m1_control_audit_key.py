"""Crash recovery binds a control audit row to the request that made it (#128).

Two intents (keys A and B) for the same incident, action, operator and
expected generation; only A's store transaction committed before the
process died, before A's confirm row. B's retry must get ``CONTROL_CONFLICT``
(it never ran); A's retry is the replay. PR #120 proved this for
``register_remediation``; this module covers every other action, including
the ones that carry no text, which used to be confirmed unconditionally.
"""

from __future__ import annotations

import pytest

from opspilot.persistence import PersistenceError
from tests.m1_web_support import (
    basic,
    build_workbench,
    post_form,
    same_origin,
    submit_incident,
)
from tests.target_support import ANY_TARGETS

ACTOR = "alice"

# (action, extra form fields, actions that must run first to make it legal at
# the generation under test)
CASES = {
    "takeover": ({}, ()),
    "pause": ({}, ()),
    "cancel": ({}, ()),
    "resume": ({}, ()),
    "new_run": ({}, ("cancel",)),
    "follow_up": ({"text": "Same note twice."}, ()),
    "correct": ({"text": "Same note twice."}, ()),
}


def _control(app, incident, fields):
    return post_form(
        app,
        f"/incidents/{incident}/control",
        fields,
        headers={**basic(), **same_origin()},
    )


def _form(action, generation, key, extra):
    return {
        "action": action,
        "expected_generation": str(generation),
        "idempotency_key": key,
        **extra,
    }


def _intent(action, generation, extra):
    intent = {"action": action, "actor_id": ACTOR, "expected_generation": generation}
    if "text" in extra:
        intent["text"] = extra["text"]
    return intent


def _prepare(app, incident, prerequisites):
    generation = 0
    for action in prerequisites:
        done = _control(app, incident, _form(action, generation, f"pre-{action}", {}))
        assert done.status == 200, done.text
        generation = done.json()["generation"]
    return generation


def _crash_confirm_of(workbench, suffix):
    """Make the ledger lose the ``control`` confirm row of one key."""
    real_put = workbench.ledger.put

    def crashing_put(namespace, key, value):
        if namespace == "control" and key.endswith(suffix):
            raise PersistenceError("STORAGE_UNAVAILABLE")
        return real_put(namespace, key, value)

    workbench.ledger.put = crashing_put
    return lambda: setattr(workbench.ledger, "put", real_put)


@pytest.mark.parametrize("action", sorted(CASES))
def test_crash_recovery_confirms_a_decision_only_against_its_own_key(action):
    extra, prerequisites = CASES[action]
    app, workbench, _ = build_workbench(targets=ANY_TARGETS)
    incident = submit_incident(app).json()["incident_id"]
    subject = workbench.list_incidents()[0].incident_id
    generation = _prepare(app, incident, prerequisites)

    # A: intent durable, store committed, process died before the confirm row.
    restore = _crash_confirm_of(workbench, ":A")
    try:
        crashed = _control(app, incident, _form(action, generation, "A", extra))
    finally:
        restore()
    assert crashed.status == 503, crashed.text
    applied = generation + 1
    assert workbench.list_incidents()[0].control_generation == applied
    assert workbench.ledger.get("control", f"{subject}:A") is None
    audit = workbench.incidents.controls[-1]
    assert audit["action"] == action and audit["resulting"] == applied
    assert audit["payload"]["idempotency_key"] == f"{subject}:A"

    # B: intent recorded earlier, died before applying.
    workbench.ledger.put(
        "control_intent", f"{subject}:B", _intent(action, generation, extra)
    )
    retry_b = _control(app, incident, _form(action, generation, "B", extra))
    assert retry_b.status == 409, retry_b.text
    assert retry_b.json() == {"code": "CONTROL_CONFLICT", "current_generation": applied}
    assert workbench.ledger.get("control", f"{subject}:B") is None
    # A page load reconciles pending intents from the audit: still only A.
    workbench.reconcile(subject)
    assert workbench.ledger.get("control", f"{subject}:B") is None
    confirmed_a = workbench.ledger.get("control", f"{subject}:A")
    assert confirmed_a is not None and int(confirmed_a["generation"]) == applied

    retry_a = _control(app, incident, _form(action, generation, "A", extra))
    assert retry_a.status == 200, retry_a.text
    assert retry_a.json()["generation"] == applied
    assert retry_a.json()["replayed"] is True
    assert [c["action"] for c in workbench.incidents.controls].count(action) == 1
    kinds = [e.kind for e in workbench.events.read_after(subject, 0)]
    assert kinds.count("control_applied") == len(prerequisites) + 1


@pytest.mark.parametrize("action", ["takeover", "pause", "cancel", "resume"])
def test_an_audit_row_without_a_key_never_confirms_a_textless_intent(action):
    """Rows written before #128 carry no key. Nothing proves which request
    produced them, so the retry is refused with the current generation rather
    than reported as a replay (fail closed, no false success)."""
    app, workbench, _ = build_workbench(targets=ANY_TARGETS)
    incident = submit_incident(app).json()["incident_id"]
    subject = workbench.list_incidents()[0].incident_id
    workbench.ledger.put("control_intent", f"{subject}:A", _intent(action, 0, {}))
    assert workbench.incidents.control(subject, 0, action, ACTOR) == 1
    assert workbench.incidents.controls[-1]["payload"] is None

    workbench.reconcile(subject)
    assert workbench.ledger.get("control", f"{subject}:A") is None
    retry = _control(app, incident, _form(action, 0, "A", {}))
    assert retry.status == 409, retry.text
    assert retry.json() == {"code": "CONTROL_CONFLICT", "current_generation": 1}
    assert workbench.ledger.get("control", f"{subject}:A") is None
    assert workbench.ledger.get("control_intent", f"{subject}:A") is not None
    assert [e.kind for e in workbench.events.read_after(subject, 0)].count(
        "control_applied"
    ) == 0


def test_a_text_audit_row_without_a_key_is_still_matched_by_its_text():
    """A follow_up/correct row written before #128 has its text as the proof;
    that recovery keeps working across the upgrade."""
    app, workbench, _ = build_workbench(targets=ANY_TARGETS)
    incident = submit_incident(app).json()["incident_id"]
    subject = workbench.list_incidents()[0].incident_id
    text = "Restarted at 09:00."
    workbench.ledger.put(
        "control_intent", f"{subject}:A", _intent("follow_up", 0, {"text": text})
    )
    workbench.ledger.put(
        "control_intent", f"{subject}:B", _intent("follow_up", 0, {"text": "Other."})
    )
    assert (
        workbench.incidents.control(
            subject, 0, "follow_up", ACTOR, {"text": text, "channel": "web"}
        )
        == 1
    )
    retry_b = _control(app, incident, _form("follow_up", 0, "B", {"text": "Other."}))
    assert retry_b.status == 409 and retry_b.json()["code"] == "CONTROL_CONFLICT"
    retry_a = _control(app, incident, _form("follow_up", 0, "A", {"text": text}))
    assert retry_a.status == 200 and retry_a.json()["replayed"] is True


def test_a_keyed_text_row_is_not_confirmed_for_a_different_key_with_the_same_text():
    app, workbench, _ = build_workbench(targets=ANY_TARGETS)
    incident = submit_incident(app).json()["incident_id"]
    subject = workbench.list_incidents()[0].incident_id
    text = "Same note twice."
    for key in ("A", "B"):
        workbench.ledger.put(
            "control_intent", f"{subject}:{key}", _intent("correct", 0, {"text": text})
        )
    assert (
        workbench.incidents.control(
            subject,
            0,
            "correct",
            ACTOR,
            {"text": text, "channel": "web", "idempotency_key": f"{subject}:A"},
        )
        == 1
    )
    retry_b = _control(app, incident, _form("correct", 0, "B", {"text": text}))
    assert retry_b.status == 409 and retry_b.json()["code"] == "CONTROL_CONFLICT"
    retry_a = _control(app, incident, _form("correct", 0, "A", {"text": text}))
    assert retry_a.status == 200 and retry_a.json()["replayed"] is True


@pytest.mark.parametrize("action", ["follow_up", "correct"])
def test_a_text_audit_row_without_a_key_is_ambiguous_between_same_text_intents(
    action,
):
    """Independent review of PR #152, P2: two pending intents with the same
    text and a pre-#128 row (text, no key). Neither the page reconcile nor
    either retry may claim the row: refused with the current generation."""
    app, workbench, _ = build_workbench(targets=ANY_TARGETS)
    incident = submit_incident(app).json()["incident_id"]
    subject = workbench.list_incidents()[0].incident_id
    text = "Same note twice."
    for key in ("A", "B"):
        workbench.ledger.put(
            "control_intent", f"{subject}:{key}", _intent(action, 0, {"text": text})
        )
    assert (
        workbench.incidents.control(
            subject, 0, action, ACTOR, {"text": text, "channel": "web"}
        )
        == 1
    )
    workbench.reconcile(subject)
    for key in ("B", "A"):
        retry = _control(app, incident, _form(action, 0, key, {"text": text}))
        assert retry.status == 409, retry.text
        assert retry.json() == {"code": "CONTROL_CONFLICT", "current_generation": 1}
        assert workbench.ledger.get("control", f"{subject}:{key}") is None
    assert [e.kind for e in workbench.events.read_after(subject, 0)].count(
        "control_applied"
    ) == 0


def test_a_textless_new_run_retry_with_another_key_is_refused_by_the_store():
    """The store's run-id replay branch (same derived run id, stale summary)
    refuses a key other than the one on the audit row."""
    app, workbench, _ = build_workbench(targets=ANY_TARGETS)
    incident = submit_incident(app).json()["incident_id"]
    subject = workbench.list_incidents()[0].incident_id
    assert _control(app, incident, _form("cancel", 0, "c", {})).status == 200
    won = _control(app, incident, _form("new_run", 1, "A", {}))
    assert won.status == 200 and won.json()["generation"] == 2
    run_id = workbench.list_incidents()[0].current_run_id
    with pytest.raises(PersistenceError, match="CONTROL_CONFLICT"):
        workbench.incidents.new_run(
            subject,
            run_id,
            expected_generation=1,
            deadline=workbench.incidents.now(),
            budget_limit=1,
            versions={},
            actor=ACTOR,
            payload={"channel": "web", "idempotency_key": f"{subject}:B"},
        )
    assert (
        workbench.incidents.new_run(
            subject,
            run_id,
            expected_generation=1,
            deadline=workbench.incidents.now(),
            budget_limit=1,
            versions={},
            actor=ACTOR,
            payload={"channel": "web", "idempotency_key": f"{subject}:A"},
        )
        == 2
    )
