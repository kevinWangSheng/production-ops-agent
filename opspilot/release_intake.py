"""Release webhook payload parsing and target resolution (F11 PR-3).

``POST /intake/releases`` only registers a release (contract P3-02): the
route in :mod:`opspilot.web.app` authenticates the dedicated actor, caps the
body, and hands the bytes here. This module turns them into the frozen
:class:`~opspilot.observation.releases.ReleaseEvent` plus the one registry
entry the payload names, or a refusal code; it never touches storage, and a
refusal here writes nothing and queries nothing (U6).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import timezone
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from opspilot.domain.base import DTO
from opspilot.observation.release_policy import ReleaseText
from opspilot.observation.releases import ReleaseEvent

if TYPE_CHECKING:
    from opspilot.web.store import TargetRegistry

#: The only event-token actor that may submit a release (contract U4). The
#: Alertmanager actor, or any other configured token, is refused with 403.
RELEASE_ACTOR = "release-deployer"
#: Independent body cap for a single release event (D4); not the 1 MiB
#: Alertmanager group cap.
RELEASE_MAX_BODY_BYTES = 128 * 1024
#: ``min_tracking_seconds`` frozen into every release registered through the
#: webhook: the M the user chose on 2026-10-21 (F11 PR-3 r1). Authorization
#: still requires a calibrated policy carrying the same M: authorization
#: refuses unless the policy's ``min_tracking_seconds`` equals the frozen row
#: value (``release_commands`` policy match), so this constant must stay in
#: sync with ``observation/policies/release-calibrations.json``.
RELEASE_MIN_TRACKING_SECONDS = 1500

_TARGET_FIELDS = (
    "resource_uid",
    "integration_id",
    "cluster_uid",
    "namespace",
    "workload",
)
_EVENT_FIELDS = frozenset(ReleaseEvent.model_fields)
_TOP_LEVEL_FIELDS = _EVENT_FIELDS | {"target"}


class ReleaseIntakeError(Exception):
    """A refusal with its HTTP status and stable code (D3)."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


class ReleaseTarget(DTO):
    resource_uid: ReleaseText
    integration_id: ReleaseText
    cluster_uid: ReleaseText
    namespace: ReleaseText
    workload: ReleaseText


@dataclass(frozen=True)
class ParsedRelease:
    event: ReleaseEvent
    target: ReleaseTarget


@dataclass(frozen=True)
class ResolvedTarget:
    resource_uid: str
    #: The five registry fields ``ReleaseStore.record_release`` completes and
    #: compares the registry row with.
    identity: dict[str, str]
    health_profile_id: str | None


def _invalid() -> ReleaseIntakeError:
    return ReleaseIntakeError(400, "INVALID_RELEASE_PAYLOAD")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_KEY")
        result[key] = value
    return result


def _reject_constant(_: str) -> Any:
    raise ValueError("NON_FINITE_NUMBER")


def _strict_int(value: object) -> bool:
    return type(value) is int


def parse_release(body: bytes) -> ParsedRelease:
    """The release event and target of one webhook body, or 400.

    Business objects are ``extra=forbid`` (D1): an unknown key inside the
    event, ``target``, ``before``/``after`` or ``auxiliary`` is refused. An
    unknown *top-level* key is tolerated: it stays only in the raw body the
    store hashes and keeps as redacted, length-limited audit text, and never
    reaches the DTO, the semantic hash, the target or a query.
    """
    try:
        payload = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _invalid() from None
    if not isinstance(payload, dict):
        raise _invalid()
    # P3-01: both identity fields are mandatory and taken verbatim; JSON
    # true is not format version 1 and a number is not a timestamp.
    if not _strict_int(payload.get("format_version")):
        raise _invalid()
    if not isinstance(payload.get("released_at"), str):
        raise _invalid()
    auxiliary = payload.get("auxiliary")
    if isinstance(auxiliary, dict) and "deployment_generation" in auxiliary:
        generation = auxiliary["deployment_generation"]
        if generation is not None and not _strict_int(generation):
            raise _invalid()
    target = payload.get("target")
    if not isinstance(target, dict):
        raise _invalid()
    event_fields = {k: v for k, v in payload.items() if k in _EVENT_FIELDS}
    try:
        event = ReleaseEvent.model_validate_json(
            json.dumps(event_fields, ensure_ascii=False)
        )
        parsed_target = ReleaseTarget.model_validate_json(
            json.dumps(target, ensure_ascii=False)
        )
    except (ValidationError, ValueError):
        # Never echo the failure: it may quote the rejected input.
        raise _invalid() from None
    # P3-01: a blank id is no identity; and an instant the store cannot
    # normalize to UTC (its semantic hash does) is not a release time.
    if not event.source_release_id.strip():
        raise _invalid()
    try:
        event.released_at.astimezone(timezone.utc)
    except OverflowError:
        raise _invalid() from None
    return ParsedRelease(event=event, target=parsed_target)


def resolve_target(
    target: ReleaseTarget, registry: TargetRegistry | None
) -> ResolvedTarget:
    """Exactly one ``OPSPILOT_TARGET_IDENTITIES`` entry, or 422 (U6, r2).

    The payload's ``resource_uid`` must be a registry key, otherwise
    ``TARGET_UNRESOLVED`` (a matching cluster/namespace/workload elsewhere
    does not resolve it). When another entry has the payload's
    ``(cluster_uid, namespace, workload)`` the target is ``TARGET_AMBIGUOUS``;
    otherwise any of the five fields differing is ``TARGET_MISMATCH``.
    """
    entries: dict[str, Mapping[str, Any]] = {}
    if registry is not None:
        for key in registry.keys():
            found = registry.resolve(key)
            if found is not None:
                entries[key] = asdict(found)
    wanted = target.model_dump()
    uid = wanted["resource_uid"]
    entry = entries.get(uid)
    if entry is None:
        raise ReleaseIntakeError(422, "TARGET_UNRESOLVED")
    natural = (wanted["cluster_uid"], wanted["namespace"], wanted["workload"])
    if any(
        other != uid
        and (found.get("cluster_uid"), found.get("namespace"), found.get("workload"))
        == natural
        for other, found in entries.items()
    ):
        raise ReleaseIntakeError(422, "TARGET_AMBIGUOUS")
    identity = {
        field: uid if field == "resource_uid" else entry.get(field)
        for field in _TARGET_FIELDS
    }
    if identity != wanted:
        raise ReleaseIntakeError(422, "TARGET_MISMATCH")
    profile_id = entry.get("health_profile_id")
    return ResolvedTarget(
        resource_uid=uid,
        identity={k: str(v) for k, v in identity.items()},
        health_profile_id=profile_id if isinstance(profile_id, str) else None,
    )


#: ``ReleaseReceipt.reason`` of a rejected delivery -> HTTP answer (D3).
#: The store has already committed the rejected delivery's audit row.
REJECTION_STATUS = {
    "RELEASE_IDENTITY_CONFLICT": 409,
    "RELEASE_TIME_IN_FUTURE": 422,
    "TARGET_MISMATCH": 422,
    "TARGET_IDENTITY_MISSING": 422,
    "HEALTH_PROFILE_TARGET_MISMATCH": 422,
    # Server-side configuration, not the caller's payload: a retry cannot
    # fix it, so it is not a 503.
    "HEALTH_PROFILE_UNAVAILABLE": 500,
    "INVALID_RELEASE_POLICY": 500,
}
