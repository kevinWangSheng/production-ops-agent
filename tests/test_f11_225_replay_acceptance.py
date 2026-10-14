"""Independent acceptance tests for issue #225, item 2: offline release replay
must check the reason-specific edge of *every* ending, including the
non-terminal ones of paused (``authority_revoked``) and scope-revoked
(``scope_suspended``) sessions.

Written from ``docs/tasks/2026-10-13-f11-pr2-contract.md`` (the ending table
of 固定语义 8: ``authority_revoked / binding_stale / scope_suspended`` →
session ``revoked``, ``transition`` NULL, the release subject is not ended by
it; 固定语义 12: any integrity failure → ``consistent=False``,
``verdict='unknown'``; the fixed code for endings is ``ENDING_MISMATCH``)
without reading any fix.

Legal histories are produced by the public store primitives on PostgreSQL
(pause through ``ReleaseStore.control_release``; scope revocation through a
target suspension seen by ``ObservationStore.submit_release_sample``) and
exported with ``ObservationStore.release_history``; the replay itself is the
pure offline ``replay_release_history``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest

from opspilot.observation import ObservationStore
from opspilot.observation.releases import ReleaseStore
from opspilot.observer.release_replay import replay_release_history
from opspilot.persistence import DurableStore
from opspilot.persistence.grants import WEB_ROLE
from scripts.m0.postgres_lab import DSN
from tests.f11_release_support import empty_evidence
from tests.runtime_roles_support import login

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1",
    reason="explicit PG opt-in required (legal histories come from the store)",
)

if os.environ.get("M1_DURABLE_POSTGRES") == "1":
    from tests.integration.test_f11_release_observation_postgres import (
        OBSERVER_ROLE,
        POOL,
        Catalog,
        _claim,
        _control,
        _observing,
        _submit_empty,
        _thaw,
        _window_end,
    )


@pytest.fixture(scope="module")
def owner() -> Iterator[DurableStore]:
    store = DurableStore(DSN, pool=POOL)
    store.install()
    yield store
    store.close()


@pytest.fixture(scope="module")
def web() -> Iterator[ReleaseStore]:
    with login(DSN, WEB_ROLE) as dsn:
        yield ReleaseStore(dsn, pool=POOL)


@pytest.fixture(scope="module")
def observer() -> Iterator[ObservationStore]:
    with login(DSN, OBSERVER_ROLE) as dsn:
        yield ObservationStore(dsn, pool=POOL)


@pytest.fixture(scope="module")
def history() -> ObservationStore:
    return ObservationStore(DSN, pool=POOL)


@pytest.fixture(scope="module")
def paused_oid(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    tmp_path_factory: pytest.TempPathFactory,
) -> UUID:
    """Observing, one adopted (empty, unknown) sample, then paused: its only
    session is revoked with an ``authority_revoked`` ending."""
    with pytest.MonkeyPatch.context() as patch:
        catalog = Catalog(patch, tmp_path_factory.mktemp("paused-225"))
        observing = _observing(owner, web, catalog, released_ago=600)
        _submit_empty(observer, observing.session_id)
        assert _control(web, observing.oid, "pause", 0).outcome == "applied"
    return observing.oid


@pytest.fixture(scope="module")
def scope_revoked_oid(
    owner: DurableStore,
    web: ReleaseStore,
    observer: ObservationStore,
    tmp_path_factory: pytest.TempPathFactory,
) -> UUID:
    """Observing, one adopted sample, then a sample submitted while the target
    is suspended: the session is revoked by the scope, the release stays
    observing."""
    with pytest.MonkeyPatch.context() as patch:
        catalog = Catalog(patch, tmp_path_factory.mktemp("scope-225"))
        observing = _observing(owner, web, catalog, released_ago=600)
        _submit_empty(observer, observing.session_id)
        target = observing.release.target_id
        lease = _claim(observer, observing.session_id)
        generation = owner.set_target_suspension(
            target, True, expected_generation=0, actor="t"
        )
        try:
            receipt = observer.submit_release_sample(
                lease, empty_evidence(_window_end())
            )
        finally:
            owner.set_target_suspension(
                target, False, expected_generation=generation, actor="t"
            )
        assert (receipt.disposition, receipt.reason) == ("history_only", "suspended")
    return observing.oid


HISTORIES = {
    "paused": ("paused_oid", "authority_revoked"),
    "scope_revoked": ("scope_revoked_oid", "scope_suspended"),
}


def _stored(
    request: pytest.FixtureRequest, history: ObservationStore, kind: str
) -> tuple[dict[str, Any], str]:
    fixture, reason = HISTORIES[kind]
    oid = request.getfixturevalue(fixture)
    return dict(history.release_history(oid)), reason


def _ending_index(stored: dict[str, Any], reason: str) -> int:
    found = [i for i, e in enumerate(stored["endings"]) if e["ended_reason"] == reason]
    assert len(found) == 1, [e["ended_reason"] for e in stored["endings"]]
    return found[0]


@pytest.mark.parametrize("kind", sorted(HISTORIES))
def test_an_untampered_non_terminal_ending_replays_consistently(
    request: pytest.FixtureRequest, history: ObservationStore, kind: str
) -> None:
    stored, reason = _stored(request, history, kind)
    ending = stored["endings"][_ending_index(stored, reason)]
    # the legal edge, as the contract's ending table fixes it
    assert ending["transition"] is None
    assert (
        ending["subject_state_before"] == ending["subject_state_after"] == "observing"
    )

    result = replay_release_history(stored)

    assert result.consistent is True, result.integrity
    assert result.integrity == ()
    assert result.stored_status == result.replayed_status == "observing"


TAMPERS = {
    # a revocation ending carries no subject transition
    "transition_release_unknown": {"transition": "release_unknown"},
    "transition_release_healthy": {"transition": "release_healthy"},
    # a revocation does not end the subject: before == after
    "state_after_unknown": {"subject_state_after": "unknown"},
    "state_after_cancelled": {"subject_state_after": "cancelled"},
    # an edge that is self-consistent but not the subject's real state
    # (the subject was observing under an authorized session)
    "states_pending": {
        "subject_state_before": "pending",
        "subject_state_after": "pending",
    },
    # a terminal-looking edge with the revocation reason
    "edge_observing_to_unknown": {
        "transition": "release_unknown",
        "subject_state_before": "observing",
        "subject_state_after": "unknown",
    },
}


@pytest.mark.parametrize("tamper", sorted(TAMPERS))
@pytest.mark.parametrize("kind", sorted(HISTORIES))
def test_a_tampered_non_terminal_ending_edge_is_an_integrity_failure(
    request: pytest.FixtureRequest,
    history: ObservationStore,
    kind: str,
    tamper: str,
) -> None:
    stored, reason = _stored(request, history, kind)
    tampered = _thaw(stored)
    index = _ending_index(stored, reason)
    tampered["endings"][index].update(TAMPERS[tamper])
    assert tampered["endings"][index] != stored["endings"][index], "tamper applied"

    result = replay_release_history(tampered)

    assert "ENDING_MISMATCH" in result.integrity, result.integrity
    assert result.consistent is False
    assert result.verdict == "unknown"
    assert result.external_queries == () and result.model_requests == ()
