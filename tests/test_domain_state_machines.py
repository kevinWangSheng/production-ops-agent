"""Every domain state machine rejects every transition it does not list.

The machines are declarative ``(state, trigger) -> state`` tables, so the
rejection property is checked exhaustively rather than by sampling: for each
machine, each state is fired with every trigger that machine knows and every
pair outside the table must raise ``ILLEGAL_TRANSITION``.
"""

import pytest

from opspilot.domain import STATE_MACHINES, DomainError, StateMachine

MACHINE_NAMES = sorted(STATE_MACHINES)

# (machine, state, trigger) pairs that must never be legal, each standing for a
# contract in C3 section 4 rather than for table coverage alone.
NAMED_REJECTIONS = [
    # A human close, a reopen or a fresh incident never reaches resolved:
    # only an independent healthy recovery observation does.
    ("incident", "open", "recovery_confirmed"),
    ("incident", "closed", "recovery_confirmed"),
    ("incident", "resolved", "start_recovery_observation"),
    ("incident", "closed", "human_close"),
    # A release observation status is terminal after healthy/anomalous/
    # unknown/cancelled; observing again creates a new record instead.
    ("release_observation", "healthy", "anomaly_detected"),
    ("release_observation", "cancelled", "observation_started"),
    ("release_observation", "unknown", "healthy_window_satisfied"),
    # Healthy needs sampling first; pending cannot finish healthy.
    ("release_observation", "pending", "healthy_window_satisfied"),
    ("release_observation", "pending", "anomaly_detected"),
    # A blocked Run is continued by a new Run, never resumed in place.
    ("run", "blocked", "claimed"),
    ("run", "completed", "claimed"),
    ("run", "cancelled", "human_resume"),
    ("run", "budget_exhausted", "claimed"),
    # A paused Run re-queues before it runs again, so it takes a new attempt.
    ("run", "paused", "claimed"),
    # A tool result cannot be committed before the call was dispatched, and a
    # settled operation never changes outcome.
    ("tool_operation", "planned", "result_committed"),
    ("tool_operation", "unknown", "result_committed"),
    ("tool_operation", "succeeded", "state_unknown"),
    # Rejected or revoked evidence never becomes adopted again.
    ("evidence", "history_only", "adopt"),
    ("evidence", "revoked", "adopt"),
    ("evidence", "recorded", "revoke"),
    # A settled job never re-executes.
    ("job", "succeeded", "claimed"),
    ("job", "cancelled", "claimed"),
    ("job", "blocked", "claimed"),
    ("job", "pending", "committed"),
    # A revoked or finished observation session never completes or samples.
    ("observation_session", "revoked", "observation_completed"),
    ("observation_session", "completed", "authority_revoked"),
    # An export is delivered or dropped once.
    ("export_outbox", "delivered", "bounded_drop"),
    ("export_outbox", "dropped", "delivered"),
    # Knowledge needs a reviewed postmortem, and a draft is not reviewed.
    ("postmortem", "draft", "human_approve"),
    ("postmortem", "approved", "human_reject"),
    ("postmortem", "rejected", "human_approve"),
    # An immutable knowledge revision settles once.
    ("knowledge_revision", "revoked", "supersede"),
    ("knowledge_revision", "superseded", "revoke"),
]


def _all_triggers(machine: StateMachine) -> frozenset[str]:
    return frozenset().union(*(machine.triggers(s) for s in machine.states))


@pytest.mark.parametrize("name", MACHINE_NAMES)
def test_every_unlisted_pair_is_rejected(name):
    machine = STATE_MACHINES[name]
    triggers = _all_triggers(machine)
    checked = 0
    for state in sorted(machine.states):
        allowed = machine.triggers(state)
        for trigger in sorted(triggers - allowed):
            with pytest.raises(DomainError) as excinfo:
                machine.fire(state, trigger)
            assert excinfo.value.code == "ILLEGAL_TRANSITION"
            checked += 1
    assert checked, f"{name} has no unlisted pair to reject"


@pytest.mark.parametrize("name", MACHINE_NAMES)
def test_listed_pairs_land_on_declared_states(name):
    machine = STATE_MACHINES[name]
    for state in sorted(machine.states):
        for trigger in sorted(machine.triggers(state)):
            assert machine.fire(state, trigger) in machine.states


@pytest.mark.parametrize("name", MACHINE_NAMES)
def test_terminal_states_reject_all_triggers(name):
    machine = STATE_MACHINES[name]
    terminals = [s for s in machine.states if machine.terminal(s)]
    # The incident lifecycle is deliberately the one machine with no terminal
    # state: a closed incident can always be explicitly reopened by a human.
    assert terminals or name == "incident"
    for state in terminals:
        for trigger in sorted(_all_triggers(machine)):
            with pytest.raises(DomainError, match="ILLEGAL_TRANSITION"):
                machine.fire(state, trigger)


def test_a_closed_incident_stays_reopenable():
    incident = STATE_MACHINES["incident"]
    assert not any(incident.terminal(state) for state in incident.states)
    assert incident.fire("closed", "human_reopen") == "open"


@pytest.mark.parametrize(("name", "state", "trigger"), NAMED_REJECTIONS)
def test_named_illegal_transitions(name, state, trigger):
    machine = STATE_MACHINES[name]
    assert state in machine.states
    with pytest.raises(DomainError) as excinfo:
        machine.fire(state, trigger)
    assert excinfo.value.code == "ILLEGAL_TRANSITION"


@pytest.mark.parametrize("name", MACHINE_NAMES)
@pytest.mark.parametrize("state", ["", "Open", "unknown_state", None, 0, True])
def test_unknown_state_is_invalid_input(name, state):
    with pytest.raises(DomainError) as excinfo:
        STATE_MACHINES[name].fire(state, "human_cancel")
    assert excinfo.value.code == "INVALID_INPUT"


@pytest.mark.parametrize("name", MACHINE_NAMES)
@pytest.mark.parametrize("trigger", ["", None, 0, True, "HUMAN_CANCEL"])
def test_non_trigger_values_never_transition(name, trigger):
    machine = STATE_MACHINES[name]
    state = sorted(s for s in machine.states if not machine.terminal(s))[0]
    with pytest.raises(DomainError) as excinfo:
        machine.fire(state, trigger)
    assert excinfo.value.code == "ILLEGAL_TRANSITION"


def test_machine_registry_matches_the_declared_lifecycles():
    assert MACHINE_NAMES == [
        "evidence",
        "export_outbox",
        "incident",
        "job",
        "knowledge_revision",
        "observation_session",
        "postmortem",
        "release_observation",
        "run",
        "tool_operation",
    ]


def test_incident_and_release_machines_share_no_state():
    incident = STATE_MACHINES["incident"]
    release = STATE_MACHINES["release_observation"]
    assert not incident.states & release.states


def test_a_table_may_not_point_at_an_undeclared_state():
    with pytest.raises(ValueError, match="unknown"):
        StateMachine("broken", {"a": {"go": "b"}})
