"""#851: a failed read of the target's standing instructions at Run creation
is recorded in the Run input.

Independent acceptance tests from the pre-review table
``docs/tasks/2026-11-24-851-prereview.md`` (851-1..851-10), written by a test
author who did not implement it, through the same public seams as the #780
tests (``tests/acceptance/test_780_standing_instructions_postgres.py``): the
workbench intake and control API, the recorded Run input, and the messages a
scripted provider receives. Only the standing-instructions read fails (the
catalog source is the real store with that one read raising a storage
error), so the runbook list is still read. Run with ``M1_DURABLE_POSTGRES=1``.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from opspilot.investigation.context import (
    STANDING_INSTRUCTIONS_BOUNDARY,
    InvestigationInput,
)
from opspilot.persistence import PersistenceError
from opspilot.tools.otel_demo import TARGET_ID
from tests.acceptance.test_780_standing_instructions_postgres import (  # noqa: F401
    QUESTION,
    U,
    _body,
    _catalog_index,
    _harness,
    _marker,
    _revision_of,
    _run_once,
    ca_file,
)
from tests.standing_instructions_support import (
    EXT_SWITCH_ON_PROMPT_REVISION,
    FACT,
    V9,
    body_of,
    messages_containing,
    sent_text,
    si_publish,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)

#: 851-2: the whole marker.
MARKER = {
    "policy_version": "opspilot-standing-instructions-v1",
    "status": "unavailable",
}
CATALOG = "runbook_catalog"


class _StandingReadFails:
    """The real runbook store, except that reading the target's standing
    instructions raises a storage error (a statement timeout on that one
    snapshot)."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def active_standing_instructions(self, resource_uid: str):
        raise PersistenceError("TIMEOUT")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


@pytest.fixture
def lone(ca_file):  # noqa: F811
    with _harness(ca_file, "opspilot_851_si") as harness:
        yield harness


def _down(h):
    return h.workbench_app(catalog=_StandingReadFails(h.runbooks), switch="1")


def test_a_failed_read_records_the_marker_and_the_run_matches_one_without(lone):
    h, target = lone, TARGET_ID
    runbook = h.rb_publish(resource_uid=target, name=f"down-rb-{U}")
    si_publish(h, _body("down"), resource_uid=target)

    incident, run = h.submit(QUESTION, target_id=target, app=_down(h))
    assert str(h.store.rebuild(incident)["run"]["run_id"]) == str(run)
    recorded = h.recorded(incident)
    assert recorded["version"] == V9
    assert recorded["scope_facts"][FACT] == MARKER
    # The runbook list read just before is unaffected.
    catalog = recorded["scope_facts"][CATALOG]
    assert catalog["status"] == "ok"
    assert [e["skill_id"] for e in catalog["entries"]] == [runbook["name"]]
    InvestigationInput.from_json(recorded)

    # 851-8 / 851-9: no message, and the revision of a Run without
    # instructions.
    outcome, provider = _run_once(h, incident)
    first = provider.requests[0]["messages"]
    assert [m["role"] for m in first[:4]] == ["system", "user", "user", "user"]
    assert _catalog_index(first) == 2
    assert not any(
        isinstance(m.get("content"), str)
        and m["content"].startswith(STANDING_INSTRUCTIONS_BOUNDARY)
        for request in provider.requests
        for m in request["messages"]
    )
    assert _marker("down") not in sent_text(provider)
    assert _revision_of(outcome) == EXT_SWITCH_ON_PROMPT_REVISION


def test_a_successor_rereads_instead_of_carrying_the_marker_or_the_fact(lone):
    h, target = lone, TARGET_ID
    si_publish(h, _body("succ"), resource_uid=target)
    incident, run = h.submit(QUESTION, target_id=target, app=_down(h))
    assert h.recorded(incident)["scope_facts"][FACT] == MARKER

    # 851-10: a successor created by a web whose read works gets the fact ...
    assert h.control(incident, "cancel").status == 200
    renewed = h.control(incident, "new_run")
    assert renewed.status == 200, renewed.text
    successor = h.store.rebuild(incident)["run"]
    assert str(successor["run_id"]) != str(run)
    assert body_of(successor["input"]["scope_facts"][FACT]) == _body("succ")
    InvestigationInput.from_json(successor["input"])

    # ... and one whose read fails records the marker, not the earlier fact.
    assert h.control(incident, "cancel").status == 200
    again = h.control(incident, "new_run", app=_down(h))
    assert again.status == 200, again.text
    third = h.store.rebuild(incident)["run"]
    assert str(third["run_id"]) != str(successor["run_id"])
    assert third["input"]["scope_facts"][FACT] == MARKER
    assert third["input"]["version"] == V9
    InvestigationInput.from_json(third["input"])
    assert not messages_containing(
        [{"content": str(third["input"]["scope_facts"])}], _marker("succ")
    )
