"""Workbench pages and actions for imported runbooks (C2a, #566).

Interface: ``docs/tasks/2026-11-19-tool-expansion-c2.md`` section 4. The
actor is always the signed-in principal, never a form field. Store codes
are kept; ``RunbookError`` groups them like the postmortem review's R3
classes so the app maps them to the same statuses (409 conflict, 422
refused, 400 invalid, 404 not found, 503 unavailable).

Who is a known target: with a target registry configured, one of its keys
(``UNKNOWN_TARGET`` otherwise); without one, any identifier the intake
accepts -- the same targets a Run can be started on.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, cast
from uuid import UUID

from opspilot.intake import _reject_ambiguous_identifier, _reject_ambiguous_text
from opspilot.knowledge.contract import ReviewErrorClass
from opspilot.knowledge.runbooks import RunbookDraft, RunbookResult
from opspilot.persistence import PersistenceError
from opspilot.web.review import ReviewError, check_key, check_reason

#: Prefix of every workbench idempotency key in the runbook request table.
KEY_PREFIX = "runbook:"
#: A URL-encoded 32 KiB runbook (up to nine bytes per character of
#: multi-byte text) plus the other fields.
MAX_FORM_BYTES = 512 * 1024
TARGET_MAX_CHARS = 256

RUNBOOK_ERROR_CLASSES: Mapping[str, ReviewErrorClass] = {
    "GENERATION_CONFLICT": "conflict",
    "IDEMPOTENCY_CONFLICT": "conflict",
    "RUNBOOK_NAME_TAKEN": "conflict",
    "SUPERSEDE_TARGET_INACTIVE": "refused",
    "SUPERSEDE_MISMATCH": "refused",
    "REVISION_NOT_ACTIVE": "refused",
    "INTEGRITY_REFUSED": "refused",
    "UNKNOWN_TARGET": "refused",
    "INVALID_INPUT": "invalid",
    "RUNBOOK_NAME_INVALID": "invalid",
    "RUNBOOK_DESCRIPTION_INVALID": "invalid",
    "RUNBOOK_CONTENT_INVALID": "invalid",
    "RUNBOOK_SOURCE_INVALID": "invalid",
    "REASON_REQUIRED": "invalid",
    "NOT_FOUND": "not_found",
    "STORAGE_UNAVAILABLE": "unavailable",
    "TIMEOUT": "unavailable",
    "RETRY": "unavailable",
}
_FIELDS = {
    "RUNBOOK_NAME_INVALID": ("name",),
    "RUNBOOK_DESCRIPTION_INVALID": ("description",),
    "RUNBOOK_CONTENT_INVALID": ("content",),
    "RUNBOOK_SOURCE_INVALID": ("source_label", "source_ref"),
    "REASON_REQUIRED": ("reason",),
    "UNKNOWN_TARGET": ("resource_uid",),
}


_FIELD_CODES = {
    "name": "RUNBOOK_NAME_INVALID",
    "description": "RUNBOOK_DESCRIPTION_INVALID",
    "source_label": "RUNBOOK_SOURCE_INVALID",
    "source_ref": "RUNBOOK_SOURCE_INVALID",
}


class RunbookError(ReviewError):
    """A refused runbook request, in the shape of ``ReviewError``."""

    def __init__(
        self,
        code: str,
        *,
        current_generation: int | None = None,
        fields: Sequence[str] = (),
    ) -> None:
        super().__init__(
            code,
            current_generation=current_generation,
            fields=fields or _FIELDS.get(code, ()),
        )
        self.error_class = RUNBOOK_ERROR_CLASSES.get(code, "unavailable")


class RunbookStoreSurface(Protocol):
    def submit(
        self, draft: RunbookDraft, *, idempotency_key: str, actor_id: str
    ) -> RunbookResult: ...

    def approve(
        self,
        submission_id: UUID,
        *,
        expected_generation: int,
        idempotency_key: str,
        actor_id: str,
        entry_generation: int | None = None,
        reason: str | None = None,
    ) -> RunbookResult: ...

    def reject(
        self,
        submission_id: UUID,
        *,
        reason: str,
        expected_generation: int,
        idempotency_key: str,
        actor_id: str,
    ) -> RunbookResult: ...

    def revoke(
        self,
        entry_id: UUID,
        revision: int,
        *,
        reason: str,
        expected_generation: int,
        idempotency_key: str,
        actor_id: str,
    ) -> RunbookResult: ...

    def overview(self) -> dict[str, Any]: ...

    def submission(self, submission_id: UUID) -> dict[str, Any]: ...

    def entry(self, entry_id: UUID) -> dict[str, Any]: ...


@dataclass
class RunbookReview:
    store: RunbookStoreSurface
    #: The registry's target keys, or ``None`` without a registry.
    target_keys: Callable[[], Sequence[str] | None]

    # -- reads ----------------------------------------------------------

    def overview(self) -> dict[str, Any]:
        page = _read(self.store.overview)
        targets: dict[str, dict[str, list[Any]]] = {}
        for row in page["entries"]:
            targets.setdefault(row["resource_uid"], _groups())[row["state"]].append(row)
        for row in page["submissions"]:
            if row["state"] in ("pending", "rejected"):
                targets.setdefault(row["resource_uid"], _groups())[row["state"]].append(
                    row
                )
        return {
            "targets": dict(sorted(targets.items())),
            "known_targets": list(self.target_keys() or ()),
        }

    def submission(self, submission_id: UUID) -> dict[str, Any]:
        return cast(dict[str, Any], _read(self.store.submission, submission_id))

    def entry(self, entry_id: UUID) -> dict[str, Any]:
        return cast(dict[str, Any], _read(self.store.entry, entry_id))

    def known_targets(self) -> list[str]:
        return list(self.target_keys() or ())

    # -- actions --------------------------------------------------------

    def submit(self, fields: Mapping[str, str], actor_id: str) -> dict[str, Any]:
        key = KEY_PREFIX + check_key(fields.get("idempotency_key", ""))
        target = fields.get("resource_uid", "")
        try:
            if not 1 <= len(target) <= TARGET_MAX_CHARS or target != target.strip():
                raise ValueError
            _reject_ambiguous_identifier(target)
        except ValueError:
            raise RunbookError("INVALID_INPUT", fields=("resource_uid",)) from None
        keys = self.target_keys()
        if keys is not None and target not in keys:
            raise RunbookError("UNKNOWN_TARGET")
        for name in ("name", "description", "source_label", "source_ref"):
            value = fields.get(name, "")
            try:
                if value:
                    _reject_ambiguous_text(value)
            except ValueError:
                # The field's own code (C2b interface: NUL and other control
                # characters are that field's ``*_INVALID``, 400).
                raise RunbookError(_FIELD_CODES[name], fields=(name,)) from None
        raw_supersedes = fields.get("supersedes_entry_id", "")
        try:
            supersedes = UUID(raw_supersedes) if raw_supersedes else None
        except ValueError:
            raise RunbookError(
                "INVALID_INPUT", fields=("supersedes_entry_id",)
            ) from None
        draft = RunbookDraft(
            resource_uid=target,
            name=fields.get("name", ""),
            description=fields.get("description", ""),
            content=fields.get("content", ""),
            source_label=fields.get("source_label", ""),
            source_ref=fields.get("source_ref", ""),
            supersedes_entry_id=supersedes,
        )
        result = _act(self.store.submit, draft, idempotency_key=key, actor_id=actor_id)
        return _result(result)

    def decide(
        self, submission_id: UUID, fields: Mapping[str, str], actor_id: str
    ) -> dict[str, Any]:
        key = KEY_PREFIX + check_key(fields.get("idempotency_key", ""))
        action = fields.get("action", "")
        expected = _number(fields, "expected_generation")
        reason = _reason(fields.get("reason") or None)
        if action == "approve":
            raw_entry = fields.get("entry_generation", "")
            entry_generation = (
                _number(fields, "entry_generation") if raw_entry else None
            )
            result = _act(
                self.store.approve,
                submission_id,
                expected_generation=expected,
                idempotency_key=key,
                actor_id=actor_id,
                entry_generation=entry_generation,
                reason=reason,
            )
        elif action == "reject":
            if reason is None:
                raise RunbookError("REASON_REQUIRED")
            result = _act(
                self.store.reject,
                submission_id,
                reason=reason,
                expected_generation=expected,
                idempotency_key=key,
                actor_id=actor_id,
            )
        else:
            raise RunbookError("INVALID_INPUT", fields=("action",))
        return _result(result)

    def revoke(
        self, entry_id: UUID, fields: Mapping[str, str], actor_id: str
    ) -> dict[str, Any]:
        key = KEY_PREFIX + check_key(fields.get("idempotency_key", ""))
        revision = _number(fields, "revision")
        expected = _number(fields, "expected_generation")
        reason = _reason(fields.get("reason") or None)
        if reason is None:
            raise RunbookError("REASON_REQUIRED")
        result = _act(
            self.store.revoke,
            entry_id,
            revision,
            reason=reason,
            expected_generation=expected,
            idempotency_key=key,
            actor_id=actor_id,
        )
        return _result(result)


def _groups() -> dict[str, list[Any]]:
    return {
        "active": [],
        "pending": [],
        "rejected": [],
        "superseded": [],
        "revoked": [],
    }


def _number(fields: Mapping[str, str], name: str) -> int:
    raw = fields.get(name, "")
    if not raw.isdigit():
        raise RunbookError("INVALID_INPUT", fields=(name,))
    return int(raw)


def _reason(value: str | None) -> str | None:
    try:
        return check_reason(value)
    except ReviewError:
        raise RunbookError("INVALID_INPUT", fields=("reason",)) from None


def _read(func: Callable[..., Any], *args: Any) -> Any:
    try:
        return func(*args)
    except PersistenceError as exc:
        raise RunbookError(str(exc)) from None


def _act(
    func: Callable[..., RunbookResult], *args: Any, **kwargs: Any
) -> RunbookResult:
    try:
        return func(*args, **kwargs)
    except PersistenceError as exc:
        raise RunbookError(str(exc)) from None


def _result(result: RunbookResult) -> dict[str, Any]:
    return {**result.payload(), "replayed": result.replayed}
