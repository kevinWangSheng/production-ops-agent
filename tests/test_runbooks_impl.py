"""Implementer unit tests for imported runbooks (C2a, #566); no PostgreSQL.

The contract and acceptance tests are the independent test author's; these
pin the pure validation and the workbench mapping.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from opspilot.knowledge.runbooks import (
    CONTENT_MAX_BYTES,
    RunbookDraft,
    RunbookResult,
    _validated,
    normalize_name,
)
from opspilot.persistence import PersistenceError
from opspilot.web.runbooks import KEY_PREFIX, RunbookError, RunbookReview


@pytest.mark.parametrize(
    ("raw", "name"),
    [
        ("Database Troubleshooting", "database-troubleshooting"),
        ("python_memory  troubleshooting", "python-memory-troubleshooting"),
        ("  app-101 ", "app-101"),
    ],
)
def test_names_are_normalized_like_upstream(raw: str, name: str) -> None:
    assert normalize_name(raw) == name


@pytest.mark.parametrize("raw", ["", "a--b", "-a", "a/b", "é", "x" * 101, None])
def test_bad_names_are_refused(raw: Any) -> None:
    with pytest.raises(PersistenceError, match="RUNBOOK_NAME_INVALID"):
        normalize_name(raw)


def _draft(**overrides: Any) -> RunbookDraft:
    values: dict[str, Any] = {
        "resource_uid": "checkout",
        "name": "db",
        "description": "When the database times out",
        "content": "# Steps\n1. check",
        "source_label": "HolmesGPT 评测夹具",
        "source_ref": "",
    }
    values.update(overrides)
    return RunbookDraft(**values)


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"description": " "}, "RUNBOOK_DESCRIPTION_INVALID"),
        ({"description": "d" * 301}, "RUNBOOK_DESCRIPTION_INVALID"),
        ({"content": "\n"}, "RUNBOOK_CONTENT_INVALID"),
        ({"content": "é" * (CONTENT_MAX_BYTES // 2 + 1)}, "RUNBOOK_CONTENT_INVALID"),
        ({"source_label": ""}, "RUNBOOK_SOURCE_INVALID"),
        ({"source_ref": "r" * 501}, "RUNBOOK_SOURCE_INVALID"),
        ({"resource_uid": ""}, "INVALID_INPUT"),
    ],
)
def test_drafts_outside_the_bounds_are_refused(
    overrides: dict[str, Any], code: str
) -> None:
    with pytest.raises(PersistenceError, match=code):
        _validated(_draft(**overrides))


def test_content_of_exactly_32_kib_is_accepted() -> None:
    _, name = _validated(_draft(content="a" * CONTENT_MAX_BYTES))
    assert name == "db"


def test_a_result_round_trips_through_its_payload() -> None:
    result = RunbookResult(
        action="approve",
        object_kind="runbook_submission",
        object_id=uuid4(),
        generation=2,
        submission_id=uuid4(),
        entry_id=uuid4(),
        revision=1,
        entry_generation=1,
        state="approved",
    )
    again = RunbookResult.from_payload(result.payload())
    assert again.replayed and again.payload() == result.payload()


class _Store:
    def __init__(self, error: str | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.error = error

    def _call(self, name: str, *args: Any, **kwargs: Any) -> RunbookResult:
        self.calls.append((name, args, kwargs))
        if self.error:
            raise PersistenceError(self.error)
        return RunbookResult(
            action=name,  # type: ignore[arg-type]
            object_kind="runbook_submission",
            object_id=UUID(int=1),
            generation=1,
        )

    def submit(self, *args: Any, **kwargs: Any) -> RunbookResult:
        return self._call("submit", *args, **kwargs)

    def approve(self, *args: Any, **kwargs: Any) -> RunbookResult:
        return self._call("approve", *args, **kwargs)

    def reject(self, *args: Any, **kwargs: Any) -> RunbookResult:
        return self._call("reject", *args, **kwargs)

    def revoke(self, *args: Any, **kwargs: Any) -> RunbookResult:
        return self._call("revoke", *args, **kwargs)


def _form(**overrides: str) -> dict[str, str]:
    fields = {
        "idempotency_key": "k1",
        "resource_uid": "checkout",
        "name": "Database Troubleshooting",
        "description": "db timeouts",
        "content": "steps",
        "source_label": "manual",
    }
    fields.update(overrides)
    return fields


def test_submit_passes_the_principal_and_prefixes_the_key() -> None:
    store = _Store()
    review = RunbookReview(store=store, target_keys=lambda: None)  # type: ignore[arg-type]
    review.submit(_form(), "alice")
    name, (draft,), kwargs = store.calls[0]
    assert name == "submit" and kwargs == {
        "idempotency_key": KEY_PREFIX + "k1",
        "actor_id": "alice",
    }
    assert draft.resource_uid == "checkout" and draft.supersedes_entry_id is None


def test_a_registry_limits_targets_to_its_keys() -> None:
    review = RunbookReview(store=_Store(), target_keys=lambda: ("cart",))  # type: ignore[arg-type]
    with pytest.raises(RunbookError) as caught:
        review.submit(_form(), "alice")
    assert caught.value.code == "UNKNOWN_TARGET"
    assert caught.value.error_class == "refused"


@pytest.mark.parametrize(
    ("form", "code"),
    [
        (_form(resource_uid="check out"), "INVALID_INPUT"),
        (_form(supersedes_entry_id="nope"), "INVALID_INPUT"),
        (_form(idempotency_key=""), "INVALID_INPUT"),
    ],
)
def test_malformed_forms_are_invalid(form: dict[str, str], code: str) -> None:
    review = RunbookReview(store=_Store(), target_keys=lambda: None)  # type: ignore[arg-type]
    with pytest.raises(Exception) as caught:
        review.submit(form, "alice")
    assert getattr(caught.value, "code", None) == code


def test_reject_and_revoke_need_a_reason() -> None:
    review = RunbookReview(store=_Store(), target_keys=lambda: None)  # type: ignore[arg-type]
    fields = {"idempotency_key": "k", "expected_generation": "1", "action": "reject"}
    with pytest.raises(RunbookError, match="REASON_REQUIRED"):
        review.decide(uuid4(), fields, "alice")
    fields = {"idempotency_key": "k", "expected_generation": "1", "revision": "1"}
    with pytest.raises(RunbookError, match="REASON_REQUIRED"):
        review.revoke(uuid4(), fields, "alice")


@pytest.mark.parametrize(
    ("code", "error_class"),
    [
        ("RUNBOOK_NAME_TAKEN", "conflict"),
        ("GENERATION_CONFLICT", "conflict"),
        ("SUPERSEDE_MISMATCH", "refused"),
        ("RUNBOOK_CONTENT_INVALID", "invalid"),
        ("NOT_FOUND", "not_found"),
        ("STORAGE_UNAVAILABLE", "unavailable"),
    ],
)
def test_store_codes_keep_their_class(code: str, error_class: str) -> None:
    review = RunbookReview(store=_Store(code), target_keys=lambda: None)  # type: ignore[arg-type]
    fields = {"idempotency_key": "k", "expected_generation": "1", "action": "approve"}
    with pytest.raises(RunbookError) as caught:
        review.decide(uuid4(), fields, "alice")
    assert (caught.value.code, caught.value.error_class) == (code, error_class)
