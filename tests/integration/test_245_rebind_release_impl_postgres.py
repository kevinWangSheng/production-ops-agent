"""#245 PR-1 implementation check on PostgreSQL: a release-anomaly incident
rebound to another target keeps renewing (Codex review on 9358d25e, P2).

The first successor after the rebind is a fresh input without the old
target's ``release_observation`` fact (G7, C3 section 7). The next successor
is bound to the same (new) target, so it continues; the release-fact check
must not demand the old target's fact back, nor refuse its absence.
"""

# ruff: noqa: F811 - pytest fixtures imported from the F11 modules
from __future__ import annotations

import os
from collections.abc import Callable
from uuid import UUID, uuid4

import pytest

from opspilot.investigation.context import RELEASE_OBSERVATION
from opspilot.observation import ObservationStore
from opspilot.observation.releases import ReleaseStore
from opspilot.persistence import DurableStore
from opspilot.web import Workbench
from tests.acceptance.test_343_alert_after_release_incident import (
    control,
    entry,
    release_first,
)
from tests.integration.test_f11_pr4b_consume_postgres import (  # noqa: F401 - fixtures
    _incident,
    bench,
)
from tests.integration.test_f11_release_observation_postgres import (  # noqa: F401 - fixtures
    Catalog,
    _rows,
    catalog,
    observer,
    observer_dsn,
    owner,
    web,
    web_dsn,
    worker_dsn,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)


def _inputs(incident_id: UUID) -> list[dict]:
    return [
        row["input"]
        for row in _rows(
            "SELECT input FROM opspilot_runs WHERE incident_id=%s ORDER BY control_generation,deadline",
            (incident_id,),
        )
    ]


@pytest.mark.parametrize("second", ["new_run", "follow_up"])
def test_successors_after_rebinding_a_release_incident_keep_renewing(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    catalog: Catalog,
    bench: Callable[..., Workbench],
    second: str,
) -> None:
    current, incident, _ = release_first(owner, web, observer, catalog, bench)
    other = f"rebound-{uuid4().hex[:8]}"
    plain = {k: v for k, v in entry().items() if k != "match"}
    workbench = bench({current.case.resource_uid: entry(), other: plain})
    assert RELEASE_OBSERVATION in _inputs(incident)[0]["scope_facts"]

    def act(action: str, **fields: str) -> None:
        workbench.control(
            incident,
            actor_id="operator:test",
            action=action,
            expected_generation=int(_incident(incident)["control_generation"]),
            idempotency_key=f"{action}-{uuid4()}",
            **fields,
        )

    act("rebind_target", to_target=other)
    act("follow_up", text="Look at the rebound target.")
    first = _inputs(incident)[-1]
    assert first["bound_target_id"] == other
    assert RELEASE_OBSERVATION not in first["scope_facts"]
    # The second successor continues the first one (same bound target).
    if second == "new_run":
        control(workbench, incident, "cancel")
        control(workbench, incident, "new_run")
    else:
        control(workbench, incident, "cancel")
        act("follow_up", text="And once more.")
    later = _inputs(incident)[-1]
    assert later is not first and len(_inputs(incident)) == 3
    assert later["bound_target_id"] == other
    assert later["scope_facts"]["target_ids"] == [other]
    assert RELEASE_OBSERVATION not in later["scope_facts"]
