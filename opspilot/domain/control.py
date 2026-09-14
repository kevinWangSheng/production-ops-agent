"""Human control and suspension (C3 section 4, "人工控制与结果所有权" / "全局与目标级暂停").

Two separate control layers, and the outer one always wins:

* Per-subject control is a conditional update. Every human operation carries an
  ``expected_version``; a match applies the action and increments that subject's
  ``control_generation``, a mismatch raises ``CONTROL_CONFLICT`` and changes
  nothing.
* Global and per-target suspension is a deterministic scope state with its own
  generation. While either is in force it outranks a subject resume, automatic
  mode and a separately granted observation authorization. It never blocks event
  intake, history reads or recording a human operation.
"""

from collections.abc import Iterable
from typing import Literal

from .base import DTO, Count, DomainError, Text
from .intake import Target

ControlAction = Literal[
    "pause",
    "resume",
    "takeover",
    "follow_up",
    "correct",
    "cancel_run",
    "close",
    "reopen",
    "authorize_observation",
    "revoke_observation",
]

ObservationMode = Literal["automatic", "human_owned"]


class ControlState(DTO):
    """A subject's control version and the decisions recorded against it."""

    control_generation: Count = 0
    mode: ObservationMode = "automatic"
    paused: bool = False
    observation_authorized: bool = False


class ScopeVersions(DTO):
    """Control versions that a claim, reservation, request or adoption rechecks."""

    subject_control_generation: Count
    global_suspension_generation: Count
    target_suspension_generation: Count


class SuspensionState(DTO):
    """Global and per-target suspension, each with its own generation.

    Target scope is a set of resolved immutable target identities. It is never a
    set of names, so a model-supplied service name cannot widen or narrow it.
    """

    global_suspended: bool = False
    global_generation: Count = 0
    suspended_target_uids: frozenset[Text] = frozenset()
    target_generation: Count = 0

    def blocks(self, target: Target) -> bool:
        if not isinstance(target, Target):
            raise DomainError("INVALID_INPUT", "suspension scope needs a Target")
        return (
            self.global_suspended or target.resource_uid in self.suspended_target_uids
        )


def apply_control(
    state: ControlState, action: ControlAction, expected_version: int
) -> ControlState:
    """Apply one human operation as a conditional update.

    Returns the next control state with ``control_generation`` incremented.
    Raises ``CONTROL_CONFLICT`` when ``expected_version`` does not match the
    current generation, and ``ILLEGAL_TRANSITION`` when the action does not
    apply to the current control state.
    """
    if not isinstance(state, ControlState):
        raise DomainError("INVALID_INPUT", "control state is required")
    if type(expected_version) is not int or expected_version < 0:
        raise DomainError("INVALID_INPUT", "expected_version must be a count")
    if action not in _ACTIONS:
        raise DomainError("INVALID_INPUT", f"unknown control action {action!r}")
    if expected_version != state.control_generation:
        raise DomainError(
            "CONTROL_CONFLICT",
            f"expected {expected_version}, current {state.control_generation}",
        )

    update: dict[str, object] = {"control_generation": state.control_generation + 1}
    if action == "pause":
        update["paused"] = True
    elif action == "resume":
        # Resuming only lifts this layer. It does not restore an observation
        # authorization, an old task or a sampling window.
        update["paused"] = False
    elif action == "takeover":
        # Takeover stops automatic observation by default.
        update["mode"] = "human_owned"
        update["observation_authorized"] = False
    elif action == "authorize_observation":
        if state.mode != "human_owned":
            raise DomainError(
                "ILLEGAL_TRANSITION",
                "separate observation authorization needs human_owned",
            )
        # The authorization covers observation only; it does not restore
        # automatic investigation.
        update["observation_authorized"] = True
    elif action == "revoke_observation":
        update["observation_authorized"] = False
    return state.model_copy(update=update)


_ACTIONS = frozenset(
    (
        "pause",
        "resume",
        "takeover",
        "follow_up",
        "correct",
        "cancel_run",
        "close",
        "reopen",
        "authorize_observation",
        "revoke_observation",
    )
)


def suspend_targets(
    state: SuspensionState, targets: Iterable[Target]
) -> SuspensionState:
    """Add resolved target identities to the suspended scope, bumping its generation."""
    resolved = _resolved_uids(state, targets)
    return state.model_copy(
        update={
            "suspended_target_uids": state.suspended_target_uids | resolved,
            "target_generation": state.target_generation + 1,
        }
    )


def release_targets(
    state: SuspensionState, targets: Iterable[Target]
) -> SuspensionState:
    """Lift target suspension for resolved identities, bumping its generation."""
    resolved = _resolved_uids(state, targets)
    return state.model_copy(
        update={
            "suspended_target_uids": state.suspended_target_uids - resolved,
            "target_generation": state.target_generation + 1,
        }
    )


def _resolved_uids(state: SuspensionState, targets: Iterable[Target]) -> frozenset[str]:
    if not isinstance(state, SuspensionState):
        raise DomainError("INVALID_INPUT", "suspension state is required")
    if isinstance(targets, (str, bytes, Target)) or not isinstance(targets, Iterable):
        raise DomainError("INVALID_INPUT", "targets must be an iterable of Target")
    collected = tuple(targets)
    if not collected or any(not isinstance(item, Target) for item in collected):
        raise DomainError(
            "INVALID_INPUT", "target scope resolves to registered Target identities"
        )
    return frozenset(item.resource_uid for item in collected)


def suspend_globally(state: SuspensionState, suspended: bool) -> SuspensionState:
    """Set global suspension, bumping its generation."""
    if not isinstance(state, SuspensionState) or type(suspended) is not bool:
        raise DomainError("INVALID_INPUT", "suspension state and flag are required")
    return state.model_copy(
        update={
            "global_suspended": suspended,
            "global_generation": state.global_generation + 1,
        }
    )


def automatic_investigation_allowed(
    *,
    control: ControlState,
    suspension: SuspensionState,
    target: Target,
    lifecycle_allows: bool,
) -> bool:
    """Whether the Agent may start a new investigation model call or tool query."""
    if type(lifecycle_allows) is not bool:
        raise DomainError("INVALID_INPUT", "lifecycle_allows must be a bool")
    if suspension.blocks(target):
        return False
    if control.paused or control.mode != "automatic":
        return False
    return lifecycle_allows


def observation_sampling_allowed(
    *,
    control: ControlState,
    suspension: SuspensionState,
    target: Target,
    lifecycle_allows: bool,
) -> bool:
    """Whether the Observer may take a new sample."""
    if type(lifecycle_allows) is not bool:
        raise DomainError("INVALID_INPUT", "lifecycle_allows must be a bool")
    if suspension.blocks(target) or control.paused or not lifecycle_allows:
        return False
    if control.mode == "automatic":
        return True
    return control.observation_authorized


def event_intake_allowed(suspension: SuspensionState) -> bool:
    """Suspension never stops durable event intake."""
    if not isinstance(suspension, SuspensionState):
        raise DomainError("INVALID_INPUT", "suspension state is required")
    return True


def human_control_allowed(suspension: SuspensionState) -> bool:
    """Suspension never stops recording a human operation or reading history."""
    if not isinstance(suspension, SuspensionState):
        raise DomainError("INVALID_INPUT", "suspension state is required")
    return True


def check_scope_versions(observed: ScopeVersions, current: ScopeVersions) -> None:
    """Raise ``CONTROL_CONFLICT`` when any recorded control version moved on."""
    if not isinstance(observed, ScopeVersions) or not isinstance(
        current, ScopeVersions
    ):
        raise DomainError("INVALID_INPUT", "both version snapshots are required")
    if observed != current:
        raise DomainError("CONTROL_CONFLICT", "control version changed")
