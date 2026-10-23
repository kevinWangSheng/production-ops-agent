"""Building the Run input snapshot the runner rebuilds from (C3 §5).

``InvestigationRunner`` refuses a Run whose row carries no input
(``INPUT_MISSING``), so every path that creates a Run -- the workbench's
intake, ``new_run``, the renewal a note on a timed-out Run starts (#47), and
the bounded live scripts -- must record one, built the same way. This
module is that one way. It knows nothing about the tool source: the
``ToolFace`` is the model-visible half of a tool profile (schemas, evidence
context, discipline variant) and is supplied by the composition root.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from unicodedata import category

from opspilot.investigation.context import (
    ALERT_CONTEXT_TRUNCATED,
    ALERT_CONTEXT_UNPRINTABLE,
    ALERT_STARTS_AT,
    RELEASE_CONTEXT_TOTAL_CHARS,
    RELEASE_CONTEXT_VALUE_CHARS,
    ContextError,
    InvestigationInput,
    continuation_context,
)
from opspilot.investigation.limits import M1_FROZEN_LIMITS, RunLimits
from opspilot.redaction import redact_credentials
from opspilot.tools.registry import canonical

__all__ = [
    "ALERT_ANCHOR_RULE",
    "ALERT_FRAME_SECONDS",
    "RELEASE_ANCHOR_RULE",
    "AlertAnchor",
    "ReleaseAnchor",
    "ToolFace",
    "continuation_input",
    "release_observation_fact",
]

#: F1: an alert Run's authorized frame is the fixed 24 h ending when the
#: alert was received -- the same length and place as a submitted Run's.
ALERT_FRAME_SECONDS = 24 * 3600
#: How far before the anchor the default query window starts (J3), the
#: upstream one-hour default moved from the frame's end to the anchor.
ALERT_DEFAULT_LOOKBACK_SECONDS = 3600
#: Names what fixed ``anchor`` in an alert Run's time policy (J3).
ALERT_ANCHOR_RULE = "alert_starts_at"
#: Names what fixed ``anchor`` in a release Run's time policy (F11 R3).
RELEASE_ANCHOR_RULE = "release_released_at"


def _utc_seconds(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(microsecond=0)


class _FrameAnchor:
    """J1-J3 over a frame end and an anchor instant (``_instants``): the
    24 h frame ending at the first, the anchor clamped into it and the default query
    window from an hour before the anchor. Whole UTC seconds, so a rebuild
    from the same two instants yields the same frame byte for byte."""

    _anchor_rule: str

    def _instants(self) -> tuple[datetime, datetime]:
        raise NotImplementedError

    @property
    def frame(self) -> tuple[datetime, datetime]:
        """J1: ``[frame_end - 24h, frame_end]``, whole UTC seconds."""
        end = _utc_seconds(self._instants()[0])
        return end - timedelta(seconds=ALERT_FRAME_SECONDS), end

    @property
    def anchor(self) -> tuple[datetime, str | None]:
        """J2: the anchor instant clamped into the frame, and which edge
        clamped it."""
        start, end = self.frame
        moment = _utc_seconds(self._instants()[1])
        if moment > end:
            return end, "future"
        if moment < start:
            return start, "before_frame"
        return moment, None

    @property
    def default_query_window(self) -> tuple[datetime, datetime]:
        """J3: from an hour before the anchor (not before the frame) to the
        frame's end."""
        start, end = self.frame
        anchor, _ = self.anchor
        return (
            max(anchor - timedelta(seconds=ALERT_DEFAULT_LOOKBACK_SECONDS), start),
            end,
        )

    def anchored(self, context: Mapping[str, Any] | None) -> dict[str, Any]:
        """``context`` with its first time policy moved onto this frame (J3).

        The face's own policy (id, mode, targets, reference rule) is kept;
        only its window is replaced and the anchor and default query window
        added. A face with no time policy cannot carry an anchor.
        """
        if not isinstance(context, Mapping):
            raise ContextError("INPUT_INVALID")
        policies = context.get("time_policies")
        if not isinstance(policies, list) or not policies:
            raise ContextError("INPUT_INVALID")
        first = policies[0]
        if not isinstance(first, Mapping):
            raise ContextError("INPUT_INVALID")
        start, end = self.frame
        default_start, default_end = self.default_query_window
        anchor, _ = self.anchor
        policy = {
            **first,
            "window": {"start": _iso(start), "end": _iso(end)},
            "anchor": _iso(anchor),
            "anchor_rule": self._anchor_rule,
            "default_query_window": {
                "start": _iso(default_start),
                "end": _iso(default_end),
            },
        }
        return {**context, "time_policies": [policy, *policies[1:]]}


@dataclass(frozen=True)
class AlertAnchor(_FrameAnchor):
    """The alert facts an alert Run's frame and anchor derive from (J1-J2).

    ``received_at`` is the database instant the alert was first received
    (the incident's earliest delivery), ``starts_at`` its normalized
    ``startsAt`` and ``original`` the ``startsAt`` text as Alertmanager sent
    it. Everything else is computed, so a fresh rebuild (J5) from the same
    three facts yields the same frame and anchor byte for byte.
    """

    received_at: datetime
    starts_at: datetime
    original: str

    def __post_init__(self) -> None:
        for moment in (self.received_at, self.starts_at):
            if not isinstance(moment, datetime) or moment.utcoffset() is None:
                raise ContextError("INPUT_INVALID")
        if not isinstance(self.original, str):
            raise ContextError("INPUT_INVALID")

    _anchor_rule = ALERT_ANCHOR_RULE

    def _instants(self) -> tuple[datetime, datetime]:
        return self.received_at, self.starts_at

    def scope_fact(self) -> dict[str, Any]:
        anchor, adjusted = self.anchor
        return {
            "anchor": _iso(anchor),
            "original": self.original,
            "adjusted": adjusted,
        }


@dataclass(frozen=True)
class ReleaseAnchor(_FrameAnchor):
    """The release facts a release-anomaly Run's frame and anchor derive
    from (F11 R3): the frame is the fixed 24 h ending at ``consumed_at``,
    the database instant the anomaly was consumed into the incident, and
    the anchor is ``released_at`` clamped into it (J2), so the default query
    window starts an hour before the release. A rebuild from the stored link
    and release yields the same frame byte for byte."""

    released_at: datetime
    consumed_at: datetime

    def __post_init__(self) -> None:
        for moment in (self.released_at, self.consumed_at):
            if not isinstance(moment, datetime) or moment.utcoffset() is None:
                raise ContextError("INPUT_INVALID")

    _anchor_rule = RELEASE_ANCHOR_RULE

    def _instants(self) -> tuple[datetime, datetime]:
        return self.consumed_at, self.released_at

    def scope_fact(self) -> dict[str, Any]:
        anchor, adjusted = self.anchor
        return {"anchor": _iso(anchor), "adjusted": adjusted}


def _iso(moment: datetime) -> str:
    # The form ``Window.as_json`` writes, so the transport parses it back.
    return moment.isoformat()


@dataclass(frozen=True)
class ToolFace:
    """What the model sees of a tool profile, independent of any Run.

    ``evidence_context`` builds the v4 evidence context a Run cites, keyed to
    that Run's own id (the loop's projection discards a context bound to a
    different Run).
    """

    tool_schemas: tuple[Mapping[str, Any], ...]
    variant_id: str
    evidence_context: Callable[[str], Mapping[str, Any] | None]

    def input_for(
        self,
        *,
        run_id: str,
        question: str,
        target_id: str,
        deadline: datetime,
        model_requests: int,
        limits: RunLimits = M1_FROZEN_LIMITS,
        alert: AlertAnchor | None = None,
        release: ReleaseAnchor | None = None,
    ) -> InvestigationInput:
        """A fresh Run's input over ``question`` against one authorized target.

        ``bound_target_id`` and ``scope_facts`` record the intake's
        authorization facts for audit and continuation; the tool gateway
        re-issues authorization on every attempt from its own scope.
        ``alert`` (an Alertmanager Run, M1-04 J1-J3) fixes the frame at the
        alert's receipt instead of the face's clock and anchors the default
        query window at its ``startsAt``; the input is then v3. ``release``
        (a release-anomaly Run, F11 R3) frames the Run at the anomaly's
        consumption and anchors the default window at ``released_at``; its
        anchor is recorded in the caller's ``release_observation`` fact.
        """
        context = self.evidence_context(run_id)
        scope_facts: dict[str, Any] = {
            "target_ids": [target_id],
            "deadline": deadline.isoformat(),
        }
        if alert is not None:
            context = alert.anchored(context)
            scope_facts[ALERT_STARTS_AT] = alert.scope_fact()
        if release is not None:
            context = release.anchored(context)
        return InvestigationInput(
            question=question,
            model_requests=model_requests,
            limits=limits,
            tool_schemas=tuple(dict(item) for item in self.tool_schemas),
            evidence_context=context,
            variant_id=self.variant_id,
            bound_target_id=target_id,
            scope_facts=scope_facts,
        )


def continuation_input(
    snapshot: Mapping[str, Any],
    *,
    new_run_id: str,
    deadline: datetime,
    authorized_targets: frozenset[str],
) -> InvestigationInput:
    """The successor Run's input: the C3 continuation of ``snapshot``'s Run.

    Same shape the bounded live runs use: the previous input with the
    carried evidence re-bound to the new Run id, the deterministic handoff
    note appended to the question, and the successor's own deadline in its
    scope facts (a renewed Run must not keep refusing on the old wall). An
    alert Run's frame, anchor and default query window carry over unchanged
    (F5, J5): the time policies and ``alert_starts_at`` are the previous
    Run's, never re-taken at renewal.
    Raises ``ContextError`` (``INPUT_MISSING`` when the previous Run has no
    snapshot) exactly as ``continuation_context`` does.
    """
    run = snapshot.get("run")
    if not isinstance(run, Mapping) or run.get("input") is None:
        raise ContextError("INPUT_MISSING")
    previous = InvestigationInput.from_json(run["input"])
    cont = continuation_context(
        snapshot, new_run_id=new_run_id, authorized_targets=authorized_targets
    )
    return replace(
        previous,
        question=f"{previous.question}\n\n{cont.handoff_note}",
        evidence_context=cont.evidence_context,
        scope_facts={**previous.scope_facts, "deadline": deadline.isoformat()},
    )


#: Per-list bounds of ``release_observation_fact``: instances per list,
#: images per revision, signals and reason codes per evaluation.
RELEASE_FACT_INSTANCES = 16
RELEASE_FACT_IMAGES = 8
RELEASE_FACT_SIGNALS = 48
RELEASE_FACT_REASONS = 32
_RELEASE_TARGET_FIELDS = (
    "integration_id",
    "cluster_uid",
    "namespace",
    "resource_uid",
    "workload",
)
_RELEASE_AUXILIARY_FIELDS = (
    "before_replicaset_uid",
    "after_replicaset_uid",
    "before_pod_template_hash",
    "after_pod_template_hash",
)
_SIGNAL_FIELDS = (
    "scope",
    "signal_name",
    "verdict",
    "value",
    "sample_count",
    "latest_sample_at",
    "reason_codes",
)


def release_observation_fact(
    release: Mapping[str, Any],
    ending: Mapping[str, Any],
    comparison: Mapping[str, Any] | None,
    anchor: ReleaseAnchor,
    *,
    comparison_reason: str = "COMPARISON_NOT_RECORDED",
) -> dict[str, Any]:
    """The ``release_observation`` scope fact of a release-anomaly Run (F11
    P4-05), from JSON-shaped records: ``release`` (a ``ReleaseRecord`` dump),
    ``ending`` (a ``ReleaseAnomalyEnding`` dump) and ``comparison`` (a stored
    ``ReleaseComparison`` dump, or ``None`` when none was recorded, which the
    fact states as ``unavailable`` with ``comparison_reason``).

    Every text is redacted (``redact_credentials``), its unprintable
    characters made U+FFFD and cut at ``RELEASE_CONTEXT_VALUE_CHARS`` with
    the alert context's marker; lists keep their first entries up to fixed
    counts. When the whole is still over ``RELEASE_CONTEXT_TOTAL_CHARS`` the
    instance lists, then the images, then the signals, then the reason codes
    are dropped in that order, which always fits (every remaining text is
    bounded and their number fixed). ``truncated`` says whether anything was cut or dropped.
    Deterministic: the same records give the same bytes.
    """
    cut = False

    def text(value: object) -> str | None:
        nonlocal cut
        if value is None:
            return None
        result = "".join(
            "\ufffd" if category(char) in ALERT_CONTEXT_UNPRINTABLE else char
            for char in redact_credentials(str(value))
        )
        if len(result) > RELEASE_CONTEXT_VALUE_CHARS:
            cut = True
            result = result[:RELEASE_CONTEXT_VALUE_CHARS] + ALERT_CONTEXT_TRUNCATED
        return result

    def first(items: object, limit: int) -> list[Any]:
        nonlocal cut
        values = list(items) if isinstance(items, (list, tuple)) else []
        if len(values) > limit:
            cut = True
        return values[:limit]

    def revision(value: Mapping[str, Any]) -> dict[str, Any]:
        images = sorted(dict(value.get("images") or {}).items())
        return {
            "images": {
                str(text(name)): text(digest)
                for name, digest in first(images, RELEASE_FACT_IMAGES)
            },
            "pod_template_digest": text(value.get("pod_template_digest")),
        }

    def instances(items: object) -> list[dict[str, Any]]:
        return [
            {
                "pod_name": text(item.get("pod_name")),
                "replicaset_name": text(item.get("replicaset_name")),
            }
            for item in first(items, RELEASE_FACT_INSTANCES)
        ]

    def signals(items: object) -> list[dict[str, Any]]:
        out = []
        for item in first(items, RELEASE_FACT_SIGNALS):
            entry: dict[str, Any] = {}
            for name in _SIGNAL_FIELDS:
                if name not in item:
                    continue
                value = item[name]
                if name == "reason_codes":
                    entry[name] = [
                        text(code) for code in first(value, RELEASE_FACT_REASONS)
                    ]
                elif name in ("value", "sample_count") or value is None:
                    entry[name] = value
                else:
                    entry[name] = text(value)
            out.append(entry)
        return out

    def window(value: object) -> dict[str, Any] | None:
        if not isinstance(value, Mapping):
            return None
        return {"start": text(value.get("start")), "end": text(value.get("end"))}

    evaluation = ending["release_evaluation"]
    auxiliary = release.get("auxiliary") or {}
    if comparison is None:
        compared: dict[str, Any] = {
            "status": "unavailable",
            "reason_codes": [text(comparison_reason)],
            "window_before": None,
            "window_after": None,
            "before_instances": [],
            "after_instances": [],
            "signals": [],
        }
    else:
        compared = {
            "status": text(comparison.get("status")),
            "reason_codes": [
                text(code)
                for code in first(comparison.get("reason_codes"), RELEASE_FACT_REASONS)
            ],
            "window_before": window(comparison.get("window_before")),
            "window_after": window(comparison.get("window_after")),
            "before_instances": instances(comparison.get("before_instances")),
            "after_instances": instances(comparison.get("after_instances")),
            "signals": signals(comparison.get("signals")),
        }
    fact: dict[str, Any] = {
        "observation_id": text(release["observation_id"]),
        "source": text(release["source"]),
        "source_release_id": text(release["source_release_id"]),
        "target": {
            name: text((release.get("target_snapshot") or {}).get(name))
            for name in _RELEASE_TARGET_FIELDS
        },
        "released_at": text(release["released_at"]),
        "anchor": anchor.scope_fact(),
        "before_revision": revision(release["before_revision"]),
        "after_revision": revision(release["after_revision"]),
        "auxiliary": {
            name: text(auxiliary.get(name)) for name in _RELEASE_AUXILIARY_FIELDS
        },
        "ending_id": text(ending["ending_id"]),
        "anomaly_key": text(ending["anomaly_key"]),
        "sample_id": text(ending["sample_id"]),
        "calibrated_policy_revision": text(ending["calibrated_policy_revision"]),
        "release_evaluation": {
            "verdict": text(evaluation.get("verdict")),
            "reason_codes": [
                text(code)
                for code in first(evaluation.get("reason_codes"), RELEASE_FACT_REASONS)
            ],
            "instances": instances(evaluation.get("instances")),
            "signals": signals(evaluation.get("signals")),
        },
        "comparison": compared,
        "truncated": False,
    }

    def total() -> int:
        return len(canonical({**fact, "truncated": True}))

    stages = (
        lambda: (
            fact["release_evaluation"].update(instances=[]),
            compared.update(before_instances=[], after_instances=[]),
        ),
        lambda: (
            fact["before_revision"].update(images={}),
            fact["after_revision"].update(images={}),
        ),
        lambda: (
            fact["release_evaluation"].update(signals=[]),
            compared.update(signals=[]),
        ),
        lambda: (
            fact["release_evaluation"].update(reason_codes=[]),
            compared.update(reason_codes=[]),
        ),
    )
    for stage in stages:
        if total() <= RELEASE_CONTEXT_TOTAL_CHARS:
            break
        cut = True
        stage()
    fact["truncated"] = cut
    return fact
