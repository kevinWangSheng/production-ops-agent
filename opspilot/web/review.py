"""Workbench review of postmortems and knowledge (M1-03 step 3, F13).

The page side of D5, D6, D9 and D25: what the incident page, the version
page and the knowledge entry page show, and the five review actions. The
data comes only through the frozen interface (``opspilot.knowledge.contract``)
and ``KnowledgeStore`` reads; generation and staleness belong to the worker
and are never called here (D8, ``tests/test_knowledge_store_callers.py``).

* A request is a ``ReviewCommand``; the actor is the Basic Auth principal,
  kind ``basic_auth``, never a form field (R4).
* ``supersede`` is its own page action and is executed as the approval of a
  version whose proposals name existing entries, with every named entry's
  expected generation (D25). The page offers "approve" only for a version
  that replaces nothing and "supersede" only for one that does, and refuses
  the other spelling, so an approval never replaces knowledge unannounced.
* Store error codes are kept and grouped by ``REVIEW_ERROR_CLASSES`` (R3);
  a refusal carries the object's current generation.
* A committed action is announced on the incident's event stream with ids,
  version, state, generation and reason only (R6); pages reload their
  snapshot. ``append_once`` keyed by generation makes a replayed request
  repair a lost announcement instead of repeating it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from unicodedata import category
from uuid import UUID

from opspilot.intake import _reject_ambiguous_identifier, _reject_ambiguous_text
from opspilot.investigation.context import INPUT_CONTENT_FIELD_MAX_CHARS
from opspilot.knowledge.contract import (
    CODE_SECTIONS,
    MODEL_SECTIONS,
    REVIEW_ERROR_CLASSES,
    ReviewCommand,
)
from opspilot.knowledge.store import ActionResult, Actor
from opspilot.persistence import PersistenceError
from opspilot.web.events import EventLog

#: Prefix of every web idempotency key in the store's (global) request
#: table, so a workbench key can never occupy a worker's key.
KEY_PREFIX = "review:"
MAX_KEY_CHARS = 256
MAX_REASON_CHARS = INPUT_CONTENT_FIELD_MAX_CHARS
#: Every postmortem/knowledge event kind; the pages reload on each (R6).
EVENT_KINDS = (
    "postmortem_generated",
    "postmortem_generation_failed",
    "postmortem_stale",
    "postmortem_reviewed",
    "knowledge_changed",
)


class KnowledgeReview(Protocol):
    """The ``KnowledgeStore`` surface the workbench uses."""

    def incident_postmortem(self, incident_id: UUID) -> dict[str, Any]: ...

    def postmortem(self, postmortem_id: UUID) -> dict[str, Any]: ...

    def knowledge_history(self, entry_id: UUID) -> dict[str, Any]: ...

    def knowledge_from_postmortem(
        self, postmortem_id: UUID
    ) -> list[dict[str, Any]]: ...

    def audit_trail(
        self, object_kind: Literal["postmortem", "knowledge_entry"], object_id: UUID
    ) -> list[dict[str, Any]]: ...

    def approve(
        self,
        postmortem_id: UUID,
        version: int,
        *,
        expected_generation: int,
        idempotency_key: str,
        actor: Actor,
        entry_generations: Mapping[UUID, int] | None = None,
    ) -> ActionResult: ...

    def reject(
        self,
        postmortem_id: UUID,
        version: int,
        *,
        reason: str,
        expected_generation: int,
        idempotency_key: str,
        actor: Actor,
    ) -> ActionResult: ...

    def return_for_revision(
        self,
        postmortem_id: UUID,
        version: int,
        *,
        reason: str,
        expected_generation: int,
        idempotency_key: str,
        actor: Actor,
    ) -> ActionResult: ...

    def revoke(
        self,
        entry_id: UUID,
        revision: int,
        *,
        reason: str,
        expected_generation: int,
        idempotency_key: str,
        actor: Actor,
    ) -> ActionResult: ...


class ReviewError(Exception):
    """A refused review request. ``code`` is the store's (or ``INVALID_INPUT``
    for a malformed form), ``error_class`` its R3 group."""

    def __init__(
        self,
        code: str,
        *,
        current_generation: int | None = None,
        entry_generations: Mapping[str, int] | None = None,
        fields: Sequence[str] = (),
    ) -> None:
        super().__init__(code)
        self.code = code
        self.error_class = REVIEW_ERROR_CLASSES.get(code, "unavailable")
        self.current_generation = current_generation
        self.entry_generations = dict(entry_generations or {})
        self.fields = tuple(fields)

    def payload(self) -> dict[str, Any]:
        data: dict[str, Any] = {"code": self.code, "error_class": self.error_class}
        if self.current_generation is not None:
            data["current_generation"] = self.current_generation
        if self.entry_generations:
            data["entry_generations"] = self.entry_generations
        if self.fields:
            data["fields"] = list(self.fields)
        return data


def check_key(value: str) -> str:
    try:
        _reject_ambiguous_identifier(value)
        if not 1 <= len(value) <= MAX_KEY_CHARS:
            raise ValueError
    except ValueError:
        raise ReviewError("INVALID_INPUT", fields=("idempotency_key",)) from None
    return value


def check_reason(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        # zero-width (``Cf``) characters survive ``strip()`` and render as
        # nothing: such a reason is blank, as an intake question is
        if (
            all(char.isspace() or category(char) == "Cf" for char in value)
            or len(value) > MAX_REASON_CHARS
        ):
            raise ValueError
        _reject_ambiguous_text(value)
    except ValueError:
        raise ReviewError("INVALID_INPUT", fields=("reason",)) from None
    return value


@dataclass
class PostmortemReview:
    knowledge: KnowledgeReview
    events: EventLog

    # -- reads ----------------------------------------------------------

    def incident_view(self, incident_id: UUID) -> dict[str, Any]:
        """The incident page's postmortem section: generation status (the
        contract's ``IncidentPostmortemView``), each version's summary, the
        schedule, the latest attempts and the knowledge it published."""
        view = self._read(self.knowledge.incident_postmortem, incident_id)
        snapshot = view.get("postmortem")
        versions = (
            []
            if snapshot is None
            else [_version_summary(v) for v in snapshot["versions"]]
        )
        knowledge = (
            []
            if snapshot is None
            else self._read(
                self.knowledge.knowledge_from_postmortem, snapshot["postmortem_id"]
            )
        )
        return {
            "status": view["status"],
            "postmortem_id": None if snapshot is None else snapshot["postmortem_id"],
            "generation": None if snapshot is None else snapshot["generation"],
            "latest": versions[-1] if versions else None,
            "versions": list(reversed(versions)),
            "schedule": view.get("schedule"),
            "attempts": list(view.get("attempts") or ()),
            "knowledge": list(knowledge),
        }

    def version_page(self, postmortem_id: UUID, version: int) -> dict[str, Any]:
        """One version with its conclusions, disputes, proposals, generation
        record, review options and review history."""
        snapshot = self._read(self.knowledge.postmortem, postmortem_id)
        row = _find_version(snapshot, version)
        summary = _version_summary(row)
        document = _document(row["content"])
        targets = []
        for proposal in row["proposals"]:
            entry_id = proposal["supersedes_entry_id"]
            if entry_id is None:
                continue
            history = self._read(self.knowledge.knowledge_history, entry_id)
            active = [r for r in history["revisions"] if r["state"] == "active"]
            targets.append(
                {
                    "entry_id": entry_id,
                    "proposal_key": proposal["proposal_key"],
                    "generation": history["generation"],
                    "latest_revision": history["latest_revision"],
                    "active": active[0] if active else None,
                }
            )
        audit = [
            item
            for item in self._read(
                self.knowledge.audit_trail, "postmortem", postmortem_id
            )
            if item.get("version") == version
        ]
        published = [
            item
            for item in self._read(
                self.knowledge.knowledge_from_postmortem, postmortem_id
            )
            if item["source_version"] == version
        ]
        sections = []
        for name in (*CODE_SECTIONS, *MODEL_SECTIONS):
            items = [c for c in row["conclusions"] if c["section"] == name]
            if items:
                sections.append({"section": name, "conclusions": items})
        known = set(CODE_SECTIONS) | set(MODEL_SECTIONS)
        other = [c for c in row["conclusions"] if c["section"] not in known]
        if other:
            sections.append({"section": "other", "conclusions": other})
        return {
            "incident_id": snapshot["incident_id"],
            "postmortem_id": snapshot["postmortem_id"],
            "generation": snapshot["generation"],
            "latest_version": snapshot["latest_version"],
            "version": row,
            "summary": summary,
            "sections": sections,
            "generation_record": document.get("generation")
            if isinstance(document.get("generation"), Mapping)
            else None,
            "citation_errors": _citation_errors(document, row),
            "targets": targets,
            "published": published,
            "audit": audit,
            "latest_sequence": self.events.latest(snapshot["incident_id"]),
        }

    def entry_page(self, entry_id: UUID) -> dict[str, Any]:
        """A knowledge entry's revision history with tombstones (R2)."""
        history = self._read(self.knowledge.knowledge_history, entry_id)
        audit = self._read(self.knowledge.audit_trail, "knowledge_entry", entry_id)
        revisions = list(history["revisions"])
        return {
            "entry": history,
            "revisions": list(reversed(revisions)),
            "active": next((r for r in revisions if r["state"] == "active"), None),
            "audit": list(audit),
        }

    # -- the five actions -----------------------------------------------

    def review(self, command: ReviewCommand, actor_id: str) -> dict[str, Any]:
        """Apply one review command for the authenticated ``actor_id``."""
        problems = command.problems()
        if problems:
            raise ReviewError("INVALID_INPUT", fields=problems)
        check_key(command.idempotency_key)
        check_reason(command.reason)
        actor = Actor(actor_id, "basic_auth")
        key = KEY_PREFIX + command.idempotency_key
        if command.action == "revoke":
            return self._revoke(command, actor, key)
        assert command.postmortem_id is not None and command.version is not None
        snapshot = self._read(self.knowledge.postmortem, command.postmortem_id)
        row = _find_version(snapshot, command.version)
        if command.action in ("approve", "supersede"):
            named = {
                p["supersedes_entry_id"]
                for p in row["proposals"]
                if p["supersedes_entry_id"] is not None
            }
            if bool(named) != (command.action == "supersede"):
                raise ReviewError("INVALID_INPUT", fields=("action",))
            # D25: exactly the entries the version names. Proposals never
            # change, so a missing or extra entry is a malformed form, not a
            # concurrent change; only an outdated generation is a conflict.
            if set(command.entry_generations) != named:
                raise ReviewError("INVALID_INPUT", fields=("entry_generations",))
        try:
            if command.action in ("approve", "supersede"):
                result = self.knowledge.approve(
                    command.postmortem_id,
                    command.version,
                    expected_generation=command.expected_generation,
                    idempotency_key=key,
                    actor=actor,
                    entry_generations=dict(command.entry_generations),
                )
            elif command.action == "reject":
                result = self.knowledge.reject(
                    command.postmortem_id,
                    command.version,
                    reason=command.reason or "",
                    expected_generation=command.expected_generation,
                    idempotency_key=key,
                    actor=actor,
                )
            else:
                result = self.knowledge.return_for_revision(
                    command.postmortem_id,
                    command.version,
                    reason=command.reason or "",
                    expected_generation=command.expected_generation,
                    idempotency_key=key,
                    actor=actor,
                )
        except PersistenceError as exc:
            raise self._refusal(str(exc), command) from None
        incident_id = snapshot["incident_id"]
        self.events.append_once(
            incident_id,
            "postmortem_reviewed",
            {
                "postmortem_id": str(result.object_id),
                "version": result.version,
                "state": result.state,
                "generation": result.generation,
                "reason": command.reason,
            },
            key={
                "postmortem_id": str(result.object_id),
                "generation": result.generation,
            },
        )
        for item in result.published.values():
            self._announce_entry(
                incident_id,
                item["entry_id"],
                item["revision"],
                "active",
                item["generation"],
                # publication has no reason; the reason of a change is the
                # reviewer's (reject/return/revoke)
                None,
            )
        return {
            "action": command.action,
            "postmortem_id": str(result.object_id),
            "incident_id": str(incident_id),
            "version": result.version,
            "state": result.state,
            "generation": result.generation,
            "replayed": result.replayed,
            "published": {
                k: {**v, "entry_id": str(v["entry_id"])}
                for k, v in result.published.items()
            },
        }

    def _revoke(self, command: ReviewCommand, actor: Actor, key: str) -> dict[str, Any]:
        assert command.entry_id is not None and command.revision is not None
        try:
            result = self.knowledge.revoke(
                command.entry_id,
                command.revision,
                reason=command.reason or "",
                expected_generation=command.expected_generation,
                idempotency_key=key,
                actor=actor,
            )
        except PersistenceError as exc:
            raise self._refusal(str(exc), command) from None
        history = self._read(self.knowledge.knowledge_history, command.entry_id)
        source = next(
            (r for r in history["revisions"] if r["revision"] == command.revision), None
        )
        if source is not None:
            incident_id = self._read(
                self.knowledge.postmortem, source["source_postmortem_id"]
            )["incident_id"]
            self._announce_entry(
                incident_id,
                command.entry_id,
                command.revision,
                "revoked",
                result.generation,
                command.reason,
            )
        return {
            "action": "revoke",
            "entry_id": str(result.object_id),
            "revision": result.revision,
            "state": result.state,
            "generation": result.generation,
            "replayed": result.replayed,
        }

    # -- internals ------------------------------------------------------

    def _announce_entry(
        self,
        incident_id: UUID,
        entry_id: UUID,
        revision: int,
        state: str,
        generation: int,
        reason: str | None,
    ) -> None:
        self.events.append_once(
            incident_id,
            "knowledge_changed",
            {
                "entry_id": str(entry_id),
                "revision": revision,
                "state": state,
                "generation": generation,
                "reason": reason,
            },
            key={"entry_id": str(entry_id), "generation": generation},
        )

    def _refusal(self, code: str, command: ReviewCommand) -> ReviewError:
        """The store's refusal with the current generations (R3). A failed
        re-read leaves them out rather than masking the original code."""
        current: int | None = None
        entries: dict[str, int] = {}
        try:
            if command.action == "revoke":
                assert command.entry_id is not None
                current = self.knowledge.knowledge_history(command.entry_id)[
                    "generation"
                ]
            else:
                assert command.postmortem_id is not None
                snapshot = self.knowledge.postmortem(command.postmortem_id)
                current = snapshot["generation"]
                if code == "ENTRY_GENERATION_CONFLICT":
                    row = _find_version(snapshot, command.version or 0)
                    for proposal in row["proposals"]:
                        entry_id = proposal["supersedes_entry_id"]
                        if entry_id is not None:
                            entries[str(entry_id)] = self.knowledge.knowledge_history(
                                entry_id
                            )["generation"]
        except (PersistenceError, ReviewError):
            pass
        return ReviewError(code, current_generation=current, entry_generations=entries)

    @staticmethod
    def _read(func: Any, *args: Any) -> Any:
        try:
            return func(*args)
        except PersistenceError as exc:
            raise ReviewError(str(exc)) from None


def _find_version(snapshot: Mapping[str, Any], version: int) -> Mapping[str, Any]:
    for row in snapshot["versions"]:
        if row["version"] == version:
            return dict(row)
    raise ReviewError("NOT_FOUND")


def _version_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    conclusions = row["conclusions"]
    disputes = [d for c in conclusions for d in c["disputes"]]
    failed = [c["conclusion_key"] for c in conclusions if not c["citations_valid"]]
    replaces = [p for p in row["proposals"] if p["supersedes_entry_id"] is not None]
    reviewable = row["state"] == "under_review"
    # The page never offers what the store must refuse: a disputed version
    # or one with failed citations is only returned or rejected (D3, D16).
    approvable = reviewable and not disputes and not failed
    approve_action = "supersede" if replaces else "approve"
    return {
        "version": row["version"],
        "state": row["state"],
        "stale_reason": row["stale_reason"],
        "revises_version": row["revises_version"],
        "created_at": row["created_at"],
        "generated_by": row["generated_by"],
        "generated_by_kind": row["generated_by_kind"],
        "conclusions": len(conclusions),
        "uncertain": sum(1 for c in conclusions if c["certainty"] == "uncertain"),
        "citation_failures": failed,
        "disputes": disputes,
        "proposals": len(row["proposals"]),
        "replaces": len(replaces),
        "reviewable": reviewable,
        "approve_action": approve_action if approvable else None,
        "actions": ([approve_action] if approvable else [])
        + (["return", "reject"] if reviewable else []),
    }


def _document(content: object) -> dict[str, Any]:
    if not isinstance(content, str):
        return {}
    try:
        value = json.loads(content)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _citation_errors(
    document: Mapping[str, Any], row: Mapping[str, Any]
) -> dict[str, list[str]]:
    """Conclusion key -> citation error codes (D22): the stored validation
    record, or ``CITATIONS_INVALID`` for a failed row it does not name."""
    validation = document.get("validation")
    errors: dict[str, list[str]] = {}
    if isinstance(validation, Mapping) and isinstance(
        validation.get("errors"), Mapping
    ):
        for key, codes in validation["errors"].items():
            if isinstance(codes, list):
                errors[str(key)] = [str(c) for c in codes]
    for conclusion in row["conclusions"]:
        if not conclusion["citations_valid"]:
            errors.setdefault(conclusion["conclusion_key"], ["CITATIONS_INVALID"])
    return errors
