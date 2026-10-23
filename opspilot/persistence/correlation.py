"""The shared open-incident rule for a new anomaly (F2 U1-A, PR-C C3).

Each source picks its own candidates -- an alert the incidents opened by the
same fingerprint on the same target, a release (F11 PR-4) the incidents on
the same target -- and both decide with ``choose_correlation``. A candidate
that is not ``closed`` takes the anomaly as an event; when every candidate is
closed, a new incident relates to the latest of them; with no candidate the
new incident stands alone (C3 section 4: a closed incident is never reopened
by an anomaly, ``domain.subjects.disposition_for_new_anomaly``).

Callers read the candidates under the source's lock, with the scope and the
target suspension row locked before the candidate incident rows (R-C2), so
the lifecycle they pass in cannot change before they commit.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


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
