"""External F11 seam: committed release records -> ``ReleaseOutcome`` (D8).

The acceptance entry for post-release observation (AGENTS.md: ``IncidentScenario
-> IncidentOutcome``, here with ``kind="release"``). Like ``acceptance_recovery``
it has no transport, no model and no SQL of its own: it projects what the
product committed, read in one snapshot by ``ReleaseStore.release_detail`` --
the release row, its control audit and its incident link. The release pages
read the same records through the same projection (``release_verdict``), so
the HTML page, its JSON and this outcome cannot disagree.

Field sources (``ReleaseOutcome``):

* ``verdict``: ``release_verdict`` of the stored status (A10: ``anomalous``
  is shown as ``degraded``; the stored value is never rewritten).
  ``pending`` and ``observing`` are not conclusions: ``concluded`` is False
  for them, and ``cancelled`` is its own label, never ``unknown``.
* ``ended_reason`` / ``paused`` / ``used_sample_count`` / ``max_samples`` /
  ``calibrated_policy_revision``: the release row.
* ``source`` / ``source_release_id`` / ``released_at`` / ``target`` /
  ``before_revision`` / ``after_revision``: the frozen release event and the
  registry target snapshot taken when it was received.
* ``incident_id`` / ``link_disposition`` / ``run_id`` /
  ``related_incident_id``: the committed incident link, if any.
* ``actions``: every applied control in audit order (``authorize``,
  ``pause``, ``resume``, ``cancel``, ``deadline_expired``), then
  ``link_<disposition>`` for the anomaly's consumption. Nothing here names a
  rollout, gate or traffic action: the product has none.
* ``human_controls``: the controls a signed-in operator submitted
  (``principal_kind='basic_auth'``), applied or refused, as
  ``<action>:<outcome>[:<reason>]``.
* ``handoff_reasons``: the link's handoff reason, and for an ``unknown``
  release its ended reason (the unknown handoff of PRODUCT-CONSTRAINTS).
* ``human_interaction``: ``handoff`` when there is a handoff reason, else
  ``human_control`` when an operator acted, else ``None``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timezone
from typing import Any

__all__ = [
    "CONCLUDED_VERDICTS",
    "RELEASE_VERDICTS",
    "ReleaseOutcome",
    "release_outcome",
    "release_outcome_from_durable",
    "release_verdict",
]

#: A10 / D5: stored release status -> the label pages and acceptance show.
RELEASE_VERDICTS: Mapping[str, str] = {
    "pending": "pending",
    "observing": "observing",
    "healthy": "healthy",
    "anomalous": "degraded",
    "unknown": "unknown",
    "cancelled": "cancelled",
}
#: F11 step 4's three conclusions; ``cancelled`` is an operator ending.
CONCLUDED_VERDICTS = frozenset({"healthy", "degraded", "unknown"})


def release_verdict(status: str) -> str:
    try:
        return RELEASE_VERDICTS[status]
    except KeyError:
        raise ValueError("UNKNOWN_RELEASE_STATUS") from None


@dataclass(frozen=True)
class ReleaseOutcome:
    """Only externally inspectable records of one release observation."""

    scenario_id: str
    observation_id: str
    verdict: str
    concluded: bool
    stored_status: str
    ended_reason: str | None
    paused: bool
    source: str
    source_release_id: str
    released_at: str
    target: Mapping[str, str]
    before_revision: Mapping[str, Any]
    after_revision: Mapping[str, Any]
    calibrated_policy_revision: str | None
    used_sample_count: int
    max_samples: int
    incident_id: str | None
    link_disposition: str | None
    run_id: str | None
    related_incident_id: str | None
    actions: tuple[str, ...]
    human_controls: tuple[str, ...]
    human_interaction: str | None
    handoff_reasons: tuple[str, ...]


def release_outcome(scenario: Any, detail: Mapping[str, Any]) -> ReleaseOutcome:
    """Project one ``ReleaseStore.release_detail`` snapshot."""
    release = detail["release"]
    if getattr(scenario, "kind", None) != "release" or scenario.subject_id != str(
        release.observation_id
    ):
        raise ValueError("SUBJECT_MISMATCH")
    link = detail.get("link")
    audit = detail.get("audit") or ()
    actions = [row["action"] for row in audit if row["outcome"] == "applied"]
    if link is not None:
        actions.append("link_" + link["disposition"])
    human = tuple(
        ":".join(
            part
            for part in (row["action"], row["outcome"], row["reason"])
            if part is not None
        )
        for row in audit
        if row["principal_kind"] == "basic_auth"
    )
    verdict = release_verdict(release.status)
    handoffs: list[str] = []
    if link is not None and link.get("handoff_reason"):
        handoffs.append(link["handoff_reason"])
    if verdict == "unknown" and release.ended_reason:
        handoffs.append(release.ended_reason)
    return ReleaseOutcome(
        scenario_id=scenario.scenario_id,
        observation_id=str(release.observation_id),
        verdict=verdict,
        concluded=verdict in CONCLUDED_VERDICTS,
        stored_status=release.status,
        ended_reason=release.ended_reason,
        paused=release.paused,
        source=release.source,
        source_release_id=release.source_release_id,
        released_at=release.released_at.astimezone(timezone.utc).isoformat(),
        target=dict(release.target_snapshot),
        before_revision=release.before_revision.model_dump(mode="json"),
        after_revision=release.after_revision.model_dump(mode="json"),
        calibrated_policy_revision=release.calibrated_policy_revision,
        used_sample_count=release.used_sample_count,
        max_samples=release.max_samples,
        incident_id=None if release.incident_id is None else str(release.incident_id),
        link_disposition=None if link is None else link["disposition"],
        run_id=None if link is None or link["run_id"] is None else str(link["run_id"]),
        related_incident_id=(
            None
            if link is None or link["related_incident_id"] is None
            else str(link["related_incident_id"])
        ),
        actions=tuple(actions),
        human_controls=human,
        human_interaction=(
            "handoff" if handoffs else "human_control" if human else None
        ),
        handoff_reasons=tuple(handoffs),
    )


def release_outcome_from_durable(
    scenario: Any, store: Any, observation_id: Any
) -> ReleaseOutcome:
    """Read ``observation_id``'s committed records from ``store`` (a
    ``ReleaseStore``) and project them; an unknown id is ``NOT_FOUND``."""
    detail = store.release_detail(observation_id)
    if detail is None:
        raise LookupError("NOT_FOUND")
    return release_outcome(scenario, detail)
