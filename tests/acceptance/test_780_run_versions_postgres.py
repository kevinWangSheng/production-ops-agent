"""#780 decision 780-12 (user ruling 2026-11-24, option A): a Run whose input
carries a real standing-instructions fact has its own ``versions``.

Independent acceptance tests (the author did not implement it) through the
public seams: the workbench intake and control API, the worker attempt
(``FetchHarness.run``: the product runner under the deployment's unchanged
``profile.versions()``), the run row, the conclusion step, the
``run_completed`` event and the lab trace root span (in-memory exporter).

Contract:

* a fact Run's ``opspilot_runs.versions`` is the deployment's dict with
  ``prompt_revision`` replaced by
  ``prompt_revision_versions(variant, knowledge_tool=True,
  standing_instructions=True)``; the conclusion step, the ``run_completed``
  event and the trace root metadata carry the same value;
* a Run without the fact, and a #851 marker Run, keep exactly
  ``profile.versions()``;
* the worker, configured as before, claims and completes a fact Run; a Run
  whose row revision does not match the one its input implies ends blocked
  with ``INCOMPATIBLE_STATE`` (the row is rewritten directly to simulate it);
* successors and follow-ups compute from their own re-read input.

Each test has its own database (one active entry per target). Run with
``M1_DURABLE_POSTGRES=1``.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import psycopg
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from psycopg.types.json import Jsonb

from opspilot import tracing
from opspilot.investigation.loop import prompt_revision_versions
from opspilot.tools.otel_demo import TARGET_ID
from tests.acceptance.f7_pg_support import ScriptedProvider, assistant
from tests.acceptance.test_780_standing_instructions_postgres import (  # noqa: F401
    QUESTION,
    _body,
    _harness,
    _revision_of,
    _run_once,
    ca_file,
)
from tests.acceptance.test_851_standing_instructions_unavailable_postgres import (
    MARKER,
    _down,
)
from tests.runbook_fetch_support import report_round, traces
from tests.standing_instructions_support import (
    EXT_SWITCH_ON_PROMPT_REVISION,
    FACT,
    message_action,
    si_publish,
)
from tests.test_tracing import lab_env

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required: M1_DURABLE_POSTGRES=1",
)

#: The extension profile's variant with the knowledge switch on and no
#: TodoWrite / cluster name (the harness's environment).
VARIANT = "replay-candidate-k8s"
INCOMPATIBLE = "INCOMPATIBLE_STATE"


@pytest.fixture
def lone(ca_file):  # noqa: F811
    with _harness(ca_file, "opspilot_780_ver") as harness:
        yield harness
    tracing.reset()


def _base(h) -> dict[str, str]:
    versions = dict(h.profile.versions())
    # The harness deployment is the one the #780 tests pin.
    assert versions["prompt_revision"] == EXT_SWITCH_ON_PROMPT_REVISION
    assert (
        prompt_revision_versions(VARIANT, knowledge_tool=True)["prompt_revision"]
        == EXT_SWITCH_ON_PROMPT_REVISION
    )
    return versions


def _standing(h) -> dict[str, str]:
    revision = prompt_revision_versions(
        VARIANT, knowledge_tool=True, standing_instructions=True
    )["prompt_revision"]
    assert revision != EXT_SWITCH_ON_PROMPT_REVISION
    return {**_base(h), "prompt_revision": revision}


def _row_versions(h, run: Any) -> dict[str, str]:
    return h.one(
        "SELECT versions FROM opspilot_runs WHERE run_id::text = %s", (str(run),)
    )["versions"]


def _set_row_versions(h, run: Any, versions: dict[str, str]) -> None:
    with psycopg.connect(h.dsn, autocommit=True) as conn:
        updated = conn.execute(
            "UPDATE opspilot_runs SET versions = %s WHERE run_id::text = %s",
            (Jsonb(versions), str(run)),
        ).rowcount
    assert updated == 1


def _run_state(h, run: Any) -> str:
    return h.one(
        "SELECT state FROM opspilot_runs WHERE run_id::text = %s", (str(run),)
    )["state"]


def _conclusion_revisions(h, run: Any) -> list[str]:
    return [
        r["revision"]
        for r in h.rows(
            "SELECT response->'conclusion'->>'prompt_revision' AS revision "
            "FROM opspilot_steps WHERE run_id::text = %s "
            "AND response ? 'conclusion'",
            (str(run),),
        )
    ]


def _completed_revisions(h, incident: UUID, run: Any) -> list[str]:
    return [
        e.payload.get("prompt_revision")
        for e in h.events.read_after(incident, 0, limit=1000)
        if e.kind == "run_completed" and e.payload.get("run_id") == str(run)
    ]


def _current_run(h, incident: UUID) -> str:
    return str(h.store.rebuild(incident)["run"]["run_id"])


def _blocked_attempt(h, incident: UUID):
    provider = ScriptedProvider([assistant(tool_calls=[traces()]), report_round()])
    return h.run(incident, provider)


# -- 1, 3, 6: a fact Run agrees everywhere and completes ---------------------------


def test_a_fact_run_carries_its_own_revision_everywhere_and_completes(lone):
    h, target = lone, TARGET_ID
    si_publish(h, _body("ver-fact"), resource_uid=target)
    incident, run = h.submit(QUESTION, target_id=target)
    assert FACT in h.recorded(incident)["scope_facts"]
    expected = _standing(h)

    # The row is the Run's own dict, created before any worker touches it.
    assert _row_versions(h, run) == expected

    # The worker, configured exactly as before, claims and completes it.
    exporter = InMemorySpanExporter()
    tracer = tracing.configure(lab_env(), exporter=exporter)
    outcome, _provider = _run_once(h, incident)
    tracer.shutdown()
    assert outcome.status == "published", outcome
    assert _run_state(h, run) == "completed"

    revision = expected["prompt_revision"]
    assert _revision_of(outcome) == revision
    assert _conclusion_revisions(h, run) == [revision]
    assert _completed_revisions(h, incident, run) == [revision]
    roots = [s for s in exporter.get_finished_spans() if s.name == "opspilot.run"]
    assert roots, "no Run root span exported"
    for root in roots:
        attributes = dict(root.attributes or {})
        assert attributes["langsmith.metadata.run_id"] == str(run)
        assert attributes["langsmith.metadata.prompt_revision"] == revision
    # The row did not move during the attempt.
    assert _row_versions(h, run) == expected


# -- 2: no fact, or the #851 marker: the deployment's dict, byte for byte ----------


def test_runs_without_the_fact_or_with_the_marker_keep_the_deployment_versions(
    lone,
):
    h, target = lone, TARGET_ID
    base = _base(h)
    plain, plain_run = h.submit(QUESTION, target_id=target)
    assert FACT not in h.recorded(plain)["scope_facts"]
    assert _row_versions(h, plain_run) == base

    si_publish(h, _body("ver-marker"), resource_uid=target)
    marked, marked_run = h.submit(QUESTION, target_id=target, app=_down(h))
    assert h.recorded(marked)["scope_facts"][FACT] == MARKER
    assert _row_versions(h, marked_run) == base

    outcome, _provider = _run_once(h, marked)
    assert outcome.status == "published", outcome
    assert _conclusion_revisions(h, marked_run) == [base["prompt_revision"]]
    assert _completed_revisions(h, marked, marked_run) == [base["prompt_revision"]]


# -- 4: a row revision that does not match the input blocks ------------------------


def test_a_row_revision_that_does_not_match_the_input_blocks_the_run(lone):
    h, target = lone, TARGET_ID
    base, standing = _base(h), _standing(h)

    # A Run without the fact whose row claims the standing dict.
    plain, plain_run = h.submit(QUESTION, target_id=target)
    _set_row_versions(h, plain_run, standing)
    outcome = _blocked_attempt(h, plain)
    assert (outcome.status, outcome.reason) == ("blocked", INCOMPATIBLE), outcome
    assert _run_state(h, plain_run) == "blocked"
    assert _conclusion_revisions(h, plain_run) == []

    # A fact Run whose row carries the deployment's (base) dict.
    si_publish(h, _body("ver-mismatch"), resource_uid=target)
    fact, fact_run = h.submit(QUESTION, target_id=target)
    assert FACT in h.recorded(fact)["scope_facts"]
    _set_row_versions(h, fact_run, base)
    outcome = _blocked_attempt(h, fact)
    assert (outcome.status, outcome.reason) == ("blocked", INCOMPATIBLE), outcome
    assert _run_state(h, fact_run) == "blocked"
    assert _conclusion_revisions(h, fact_run) == []
    assert _completed_revisions(h, fact, fact_run) == []


# -- 5: successors and follow-ups compute from their own input ---------------------


def test_successors_and_follow_ups_compute_their_own_versions(lone):
    h, target = lone, TARGET_ID
    base, standing = _base(h), _standing(h)
    first = si_publish(h, _body("ver-succ-1"), resource_uid=target)
    incident, run = h.submit(QUESTION, target_id=target)
    assert _row_versions(h, run) == standing

    # Revoke, then re-investigate: the successor has no fact and is base.
    h.rb_revoke(first["entry_id"], 1)
    assert h.control(incident, "cancel").status == 200
    renewed = h.control(incident, "new_run")
    assert renewed.status == 200, renewed.text
    successor = _current_run(h, incident)
    assert successor != str(run)
    assert FACT not in h.recorded(incident)["scope_facts"]
    assert _row_versions(h, successor) == base
    # The earlier Run's row is unchanged.
    assert _row_versions(h, run) == standing

    outcome, _provider = _run_once(h, incident)
    assert outcome.status == "published", outcome
    assert _conclusion_revisions(h, successor) == [base["prompt_revision"]]

    # New instructions, then a follow-up: the continuation Run re-reads them
    # and carries the standing dict; the successor's row stays base.
    si_publish(h, _body("ver-succ-2"), resource_uid=target)
    asked = message_action(h, incident, "Also check the payment service.")
    assert 200 <= asked.status < 300, asked.text
    h.workbench.relay(incident)
    relayed = _current_run(h, incident)
    assert relayed not in {str(run), successor}
    assert FACT in h.recorded(incident)["scope_facts"]
    assert _row_versions(h, relayed) == standing
    assert _row_versions(h, successor) == base
