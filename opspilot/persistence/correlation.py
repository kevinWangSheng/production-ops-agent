"""The shared open-incident rule for a new anomaly (F2 U1-A, PR-C C3).

Each source picks its own candidates -- an alert the incidents opened by the
same fingerprint on the same target plus those a release anomaly opened there
and no other fingerprint has attached to (#343), a release (F11 PR-4) the
incidents on the same target -- and both decide with ``choose_correlation``. A candidate
that is not ``closed`` takes the anomaly as an event; when every candidate is
closed, a new incident relates to the latest of them; with no candidate the
new incident stands alone (C3 section 4: a closed incident is never reopened
by an anomaly, ``domain.subjects.disposition_for_new_anomaly``).

Callers read the candidates under the source's lock, with the scope and the
target suspension row locked before the candidate incident rows (R-C2), so
the lifecycle they pass in cannot change before they commit.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from opspilot.persistence.base import Connection, PersistenceError

#: #245 U5 ②: the most merge pointers one correlation follows. A merged
#: incident is never merged again (G4), so chains are acyclic; the bound
#: only caps the work.
MERGE_CHAIN_LIMIT = 16


@dataclass(frozen=True)
class CorrelationCandidate:
    incident_id: UUID
    lifecycle: str
    created_at: datetime


@dataclass(frozen=True)
class Correlation:
    """At most one of the two is set: attach to an open incident, or open a
    new incident related to a closed one; neither opens an unrelated one."""

    attach_to: UUID | None
    related_to: UUID | None


def choose_correlation(candidates: Sequence[CorrelationCandidate]) -> Correlation:
    """Latest by ``created_at`` (ties: the larger ``incident_id``) among the
    non-closed candidates, else among the closed ones."""

    def latest(rows: Sequence[CorrelationCandidate]) -> UUID | None:
        if not rows:
            return None
        return max(rows, key=lambda row: (row.created_at, row.incident_id)).incident_id

    open_ = [row for row in candidates if row.lifecycle != "closed"]
    if open_:
        return Correlation(attach_to=latest(open_), related_to=None)
    return Correlation(attach_to=None, related_to=latest(candidates))


def lock_merge_closure(
    conn: Connection, candidate_ids: Collection[UUID]
) -> dict[UUID, Mapping[str, Any]]:
    """#245 U5 ②: lock the candidates and every incident their merge
    pointers lead to, in one ``incident_id``-ordered ``FOR UPDATE`` (the
    order ``merge_into`` takes its two rows, so neither waits for the other
    while holding a row the other needs; review on #380). The chains are
    read without locks first; the locked rows are the ones to decide on, and
    ``resolve_merged`` refuses (``RETRY``) a chain that moved in between."""
    ids = set(candidate_ids)
    frontier = set(ids)
    for _ in range(MERGE_CHAIN_LIMIT):
        if not frontier:
            break
        pointers = {
            row["merged_into_incident_id"]
            for row in conn.execute(
                "SELECT merged_into_incident_id FROM opspilot_incidents WHERE incident_id=ANY(%s) AND merged_into_incident_id IS NOT NULL",
                (sorted(frontier),),
            ).fetchall()
        }
        frontier = pointers - ids
        ids |= pointers
    if not ids:
        return {}
    return {
        row["incident_id"]: row
        for row in conn.execute(
            "SELECT incident_id,lifecycle,created_at,merged_into_incident_id,target_id FROM opspilot_incidents WHERE incident_id=ANY(%s) ORDER BY incident_id FOR UPDATE",
            (sorted(ids),),
        ).fetchall()
    }


def resolve_merged(
    locked: Mapping[UUID, Mapping[str, Any]],
    candidate_ids: Sequence[UUID],
    target_id: UUID | None,
) -> list[CorrelationCandidate]:
    """Each candidate stands for the end of its merge chain, read from the
    locked rows; duplicates collapse. A chain whose end is bound to another
    target (rebound after the merge, U5 ①) or that is longer than
    ``MERGE_CHAIN_LIMIT`` is dropped: an anomaly never relates to or attaches
    to a merged incident. A pointer to a row outside the locked set moved
    after the unlocked read; the caller's transaction is refused with
    ``RETRY`` (the request is replayed)."""
    resolved: dict[UUID, CorrelationCandidate] = {}
    for candidate in candidate_ids:
        current = locked.get(candidate)
        if current is None:
            continue
        for _ in range(MERGE_CHAIN_LIMIT):
            pointer = current["merged_into_incident_id"]
            if pointer is None:
                break
            if pointer not in locked:
                raise PersistenceError("RETRY")
            current = locked[pointer]
        if current["merged_into_incident_id"] is not None:
            continue
        if current["target_id"] != target_id:
            continue
        resolved[current["incident_id"]] = CorrelationCandidate(
            current["incident_id"], current["lifecycle"], current["created_at"]
        )
    return list(resolved.values())
