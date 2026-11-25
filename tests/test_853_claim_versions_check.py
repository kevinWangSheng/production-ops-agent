"""#853: a storage failure in the web claim's post-claim versions check
(780-12) ends the attempt like any other post-claim storage failure: the
lease is released at once and a ``run_handoff`` (``parked: false``) records
the reason, so the next claim does not wait for ``lease_until``.

In-memory workbench; the storage failure is injected at the incident-store
seam (the first ``rebuild`` after the claim).
"""

from __future__ import annotations

from uuid import UUID

from opspilot.persistence import PersistenceError
from tests.m1_web_support import ScriptedInvestigator, build_workbench, submit_incident


def test_a_storage_failure_in_the_versions_check_releases_the_lease_and_hands_off():
    app, workbench, clock = build_workbench()
    # The standing variant exists, so the claim checks the row's versions.
    workbench.standing_run_versions = {"state": "v1-standing"}
    store = workbench.incidents
    response = submit_incident(app, key="t853-1")
    assert response.status == 201, response.text
    incident = UUID(response.json()["incident_id"])
    run_id = store.incidents[incident]["current_run_id"]

    claim, rebuild = store.claim, store.rebuild
    failing = {"armed": False, "raised": 0}

    def claim_either(incident_id, run, owner, versions, lease_seconds=30):
        # The durable store accepts either of the process's dicts.
        accepted = versions[0] if isinstance(versions, tuple) else versions
        lease = claim(incident_id, run, owner, accepted, lease_seconds)
        failing["armed"] = True
        return lease

    def rebuild_after_claim(incident_id):
        if failing["armed"] and not failing["raised"]:
            failing["raised"] += 1
            raise PersistenceError("STORAGE_UNAVAILABLE")
        return rebuild(incident_id)

    store.claim, store.rebuild = claim_either, rebuild_after_claim

    investigator = ScriptedInvestigator(clock)
    assert workbench.run_once(incident, investigator) is None
    assert failing["raised"] == 1
    assert investigator.contexts == []

    run = store.runs[run_id]
    assert (run["owner"], run["lease_until"]) == (None, None)
    handoffs = [
        e.payload
        for e in workbench.events.read_after(incident, 0, limit=100)
        if e.kind == "run_handoff"
    ]
    assert len(handoffs) == 1
    assert handoffs[0]["run_id"] == str(run_id)
    assert handoffs[0]["reasons"] == ["STORAGE_UNAVAILABLE"]
    assert handoffs[0]["parked"] is False

    # The Run is claimable again at once; the next attempt completes.
    outcome = workbench.run_once(incident, ScriptedInvestigator(clock))
    assert outcome is not None and outcome.execution == "completed", outcome
