"""Imported runbooks through the workbench (#566 C2a; C2 interface sections 1, 2, 4).

Independent contract tests (the author did not see the implementation).
Every change goes through the signed-in JSON API; assertions read the
persisted rows as the owner. Includes the C2a part of the F13 step 2 / 3
re-proof table on the new source (pending and rejected never become a
revision; approval records reviewer, time, reason and version; revisions
are immutable; revoke ends retrieval; supersede makes a new revision).
Database times come from PostgreSQL and are asserted as ordered and
tz-aware, never as wall-clock values.
"""

from __future__ import annotations

import hashlib
import os
from uuid import uuid4

import pytest

from tests.m1_web_support import UI_USER
from tests.runbook_support import (
    REVIEWER,
    TARGET_A,
    TARGET_B,
    Harness,
    scratch_database,
    unique_name,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="需要显式启用临时 PG 验收"
)

#: Statuses the interface does not pin; the code is what is asserted.
REFUSED = {409, 422}


@pytest.fixture(scope="module")
def dsn():
    with scratch_database("opspilot_rb_accept") as scratch:
        yield scratch


@pytest.fixture(scope="module")
def h(dsn):
    harness = Harness(dsn)
    try:
        yield harness
    finally:
        harness.close()


def _code(response):
    return response.json()["code"]


# -- authentication and origin ------------------------------------------


def test_every_runbook_route_requires_sign_in(h):
    sid = h.submitted()
    for path in ("/runbooks", "/runbooks/new", f"/runbooks/submissions/{sid}"):
        assert h.get(path, signed_in=False, accept="application/json").status == 401
    before = h.counts()
    for path in (
        "/runbooks",
        f"/runbooks/submissions/{sid}/review",
        f"/runbooks/{uuid4()}/revoke",
    ):
        assert h.post(path, {"idempotency_key": uuid4().hex}, user=None).status == 401
    assert h.counts() == before


@pytest.mark.parametrize("origin", [None, "https://evil.example.test"])
def test_writes_without_the_allowed_origin_are_refused_and_write_nothing(h, origin):
    before = h.counts()
    response = h.post(
        "/runbooks",
        {
            "resource_uid": TARGET_A,
            "name": unique_name(),
            "description": "d",
            "content": "c",
            "source_label": "l",
            "source_ref": "",
            "idempotency_key": uuid4().hex,
        },
        origin=origin,
    )
    assert response.status == 403
    assert h.counts() == before


def test_forged_identity_fields_are_ignored(h):
    """The actor is the authenticated principal, never a form field (F13 D6)."""
    sid = h.submitted(actor_id="forged-worker", principal_kind="worker")
    h.approved(sid, actor_id="forged-worker", principal_kind="worker")
    assert h.submission(sid)["submitted_by"] == UI_USER
    [decision] = h.decisions(sid)
    assert decision["decided_by"] == REVIEWER
    for row in h.audit(sid):
        assert row["principal_kind"] == "basic_auth"
        assert row["actor_id"] in {UI_USER, REVIEWER}


# -- submission: names, sizes, targets -----------------------------------


def test_submit_returns_a_pending_submission_with_its_original_text(h):
    content = "# Steps\n\npassword=not-redacted-at-submit\n"
    name = unique_name("Disk Full_v2")
    response = h.submit(name=name, content=content, source_ref="holmes:path#sha256=00")
    assert response.status == 200, response.text
    sid = response.json()["submission_id"]
    row = h.submission(sid)
    assert row["name"] == name.lower().replace(" ", "-").replace("_", "-")
    assert row["content"] == content  # not redacted at submit (interface 1)
    assert row["content_sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert row["resource_uid"] == TARGET_A
    assert row["supersedes_entry_id"] is None
    assert row["submitted_by"] == UI_USER
    assert row["submitted_at"].tzinfo is not None
    assert h.decisions(sid) == []
    [audit] = h.audit(sid)
    assert (audit["object_kind"], audit["action"]) == ("runbook_submission", "submit")
    assert audit["resulting_generation"] == audit["expected_generation"] + 1


@pytest.mark.parametrize(
    "name",
    [
        "",
        "bad/name",
        "-leading",
        "trailing-",
        "two--dashes",
        "two  spaces",
        "ünicode",
        "a" * 101,
    ],
)
def test_names_outside_the_rule_are_refused(h, name):
    before = h.counts()
    response = h.submit(name=name)
    assert response.status == 400, response.text
    assert h.counts() == before


def test_a_hundred_character_name_is_accepted(h):
    name = ("n" + uuid4().hex) * 4
    name = name[:100]
    assert h.submit(name=name).status == 200


@pytest.mark.parametrize(
    ("field", "value", "ok"),
    [
        ("description", "", False),
        ("description", "d" * 300, True),
        ("description", "d" * 301, False),
        ("content", "", False),
        ("content", "x" * 32768, True),
        ("content", "x" * 32769, False),
        # 32 KiB is counted in UTF-8 bytes (octet_length), not characters.
        ("content", "é" * 16384, True),
        ("content", "é" * 16384 + "x", False),
        ("source_label", "", False),
        ("source_label", "l" * 100, True),
        ("source_label", "l" * 101, False),
        ("source_ref", "r" * 500, True),
        ("source_ref", "r" * 501, False),
    ],
)
def test_field_size_limits(h, field, value, ok):
    before = h.counts()
    response = h.submit(**{field: value})
    if ok:
        assert response.status == 200, response.text
        assert h.submission(response.json()["submission_id"])[field] == value
    else:
        assert response.status == 400, response.text
        assert h.counts() == before


def test_an_unknown_target_is_refused(h):
    before = h.counts()
    response = h.submit(resource_uid="not-a-known-target")
    assert response.status in {400, 404, 422}, response.text
    assert _code(response) == "TARGET_UNKNOWN"
    assert h.counts() == before


# -- idempotency ---------------------------------------------------------


def test_same_key_same_request_replays_and_same_key_other_request_conflicts(h):
    key = uuid4().hex
    name = unique_name()
    first = h.submit(name=name, idempotency_key=key)
    assert first.status == 200, first.text
    after_first = h.counts()
    replay = h.submit(name=name, idempotency_key=key)
    assert replay.status == 200, replay.text
    assert replay.json()["submission_id"] == first.json()["submission_id"]
    assert h.counts() == after_first
    other = h.submit(name=unique_name(), idempotency_key=key)
    assert other.status == 409, other.text
    assert _code(other) == "IDEMPOTENCY_CONFLICT"
    assert h.counts() == after_first


def test_a_replayed_approval_changes_nothing(h):
    sid = h.submitted()
    path = f"/runbooks/submissions/{sid}/review"
    fields = {
        "action": "approve",
        "expected_generation": h.submission_generation(sid),
        "idempotency_key": uuid4().hex,
    }
    first = h.post(path, fields, user=REVIEWER)
    assert first.status == 200, first.text
    after = h.counts()
    # Same key and same request: the stored result, not a second decision.
    replay = h.post(path, fields, user=REVIEWER)
    assert replay.status == 200, replay.text
    assert replay.json()["entry_id"] == first.json()["entry_id"]
    assert replay.json()["revision"] == first.json()["revision"]
    assert h.counts() == after


# -- review: approve, reject, decide once, generation ---------------------


def test_approve_creates_revision_one_equal_to_the_submission(h):
    """F13 step 3 on the new source: reviewer, time, version and provenance."""
    sid = h.submitted(
        source_label="HolmesGPT 评测夹具", source_ref="holmesgpt@x:a#sha256=1"
    )
    result = h.approved(sid)
    entry_id = str(result["entry_id"])
    assert result["revision"] == 1
    submission = h.submission(sid)
    [revision] = h.revisions(entry_id)
    for column in (
        "resource_uid",
        "name",
        "description",
        "content",
        "content_sha256",
        "source_label",
        "source_ref",
    ):
        assert revision[column] == submission[column], column
    assert str(revision["submission_id"]) == sid
    assert revision["revision"] == 1 and revision["supersedes_revision"] is None
    assert revision["state"] == "active"
    assert revision["approved_by"] == REVIEWER
    assert revision["approved_at"] >= submission["submitted_at"]
    [decision] = h.decisions(sid)
    assert (decision["decision"], decision["decided_by"]) == ("approved", REVIEWER)
    assert decision["decided_at"].tzinfo is not None
    entry = h.entry(entry_id)
    assert entry["resource_uid"] == TARGET_A and entry["latest_revision"] == 1
    assert any(a["action"] == "approve" for a in h.audit(sid) + h.audit(entry_id))
    for row in h.audit(entry_id):
        assert row["actor_id"] == REVIEWER and row["principal_kind"] == "basic_auth"
        assert row["resulting_generation"] == row["expected_generation"] + 1


def test_self_approval_is_allowed_and_audited_as_such(h):
    sid = h.submitted(user=UI_USER)
    response = h.review(sid, user=UI_USER)
    assert response.status == 200, response.text
    entry_id = str(response.json()["entry_id"])
    assert h.submission(sid)["submitted_by"] == UI_USER
    assert h.decisions(sid)[0]["decided_by"] == UI_USER
    assert h.revisions(entry_id)[0]["approved_by"] == UI_USER
    approve = [a for a in h.audit(sid) + h.audit(entry_id) if a["action"] == "approve"]
    assert approve and {a["actor_id"] for a in approve} == {UI_USER}


def test_reject_needs_a_reason_and_is_final(h):
    sid = h.submitted()
    before = h.counts()
    missing = h.review(sid, "reject")
    assert missing.status == 400, missing.text
    assert h.counts() == before and h.decisions(sid) == []
    reason = "steps target another service"
    response = h.review(sid, "reject", reason=reason)
    assert response.status == 200, response.text
    [decision] = h.decisions(sid)
    assert (decision["decision"], decision["reason"], decision["decided_by"]) == (
        "rejected",
        reason,
        REVIEWER,
    )
    reject = [a for a in h.audit(sid) if a["action"] == "reject"]
    assert [a["reason"] for a in reject] == [reason]
    # F13 step 2: a rejected submission never becomes a revision.
    assert (
        h.rows(
            "SELECT 1 FROM opspilot_runbook_revisions WHERE submission_id::text = %s",
            (sid,),
        )
        == []
    )
    after = h.counts()
    again = h.review(sid, "approve")
    assert again.status in REFUSED, again.text
    assert h.counts() == after and len(h.decisions(sid)) == 1


def test_an_approved_submission_cannot_be_decided_again(h):
    sid, _ = h.publish()
    after = h.counts()
    for action, extra in (("approve", {}), ("reject", {"reason": "late"})):
        response = h.review(sid, action, **extra)
        assert response.status in REFUSED, response.text
    assert h.counts() == after and len(h.decisions(sid)) == 1


def test_a_stale_expected_generation_is_a_generation_conflict(h):
    sid = h.submitted()
    before = h.counts()
    response = h.review(sid, expected_generation=h.submission_generation(sid) + 5)
    assert response.status == 409, response.text
    assert _code(response) == "GENERATION_CONFLICT"
    assert h.counts() == before


@pytest.mark.parametrize("action", ["", "publish", "supersede", "return", "revoke"])
def test_unknown_review_actions_are_refused(h, action):
    sid = h.submitted()
    before = h.counts()
    response = h.review(sid, action)
    assert response.status == 400, response.text
    assert h.counts() == before


def test_unknown_submission_and_entry_are_not_found(h):
    missing = h.post(
        f"/runbooks/submissions/{uuid4()}/review",
        {"action": "approve", "expected_generation": 1, "idempotency_key": uuid4().hex},
        user=REVIEWER,
    )
    assert missing.status == 404, missing.text
    assert h.get(f"/runbooks/submissions/{uuid4()}").status == 404
    assert h.get(f"/runbooks/{uuid4()}").status == 404
    assert h.get("/runbooks/not-a-uuid").status == 404


# -- active-name uniqueness ----------------------------------------------


def test_an_active_name_is_taken_on_the_same_target_at_submit(h):
    name = unique_name("disk")
    h.publish(name=name)
    before = h.counts()
    for variant in (name, name.upper(), name.replace("-", " ", 1)):
        response = h.submit(name=variant)
        assert response.status in REFUSED, response.text
        assert _code(response) == "RUNBOOK_NAME_TAKEN"
    assert h.counts() == before
    # Another target may use the same name.
    _, other = h.publish(name=name, resource_uid=TARGET_B)
    assert other["revision"] == 1


def test_two_pending_with_one_name_only_the_first_approval_wins(h):
    name = unique_name("race")
    first = h.submitted(name=name)
    second = h.submitted(name=name)
    h.approved(first)
    before = h.counts()
    response = h.review(second)
    assert response.status in REFUSED, response.text
    assert _code(response) == "RUNBOOK_NAME_TAKEN"
    assert h.counts() == before and h.decisions(second) == []
    assert h.active_names(TARGET_A).count(name) == 1


def test_after_revoke_the_name_can_be_used_again_as_a_new_entry(h):
    name = unique_name("again")
    _, old = h.publish(name=name)
    assert h.revoke(str(old["entry_id"]), 1).status == 200
    _, new = h.publish(name=name)
    assert new["entry_id"] != old["entry_id"] and new["revision"] == 1
    assert [r["state"] for r in h.revisions(str(old["entry_id"]))] == ["revoked"]


# -- supersede -----------------------------------------------------------


def test_supersede_makes_revision_two_and_the_old_one_superseded(h):
    """F13 step 3 on the new source: replacement keeps immutable history."""
    name = unique_name("replace")
    _, old = h.publish(name=name, content="# v1\n")
    entry_id = str(old["entry_id"])
    generation_before = h.entry(entry_id)["generation"]
    sid = h.submitted(name=name, supersedes_entry_id=entry_id, content="# v2\n")
    result = h.approved(sid)
    assert str(result["entry_id"]) == entry_id and result["revision"] == 2
    one, two = h.revisions(entry_id)
    assert (one["state"], two["state"]) == ("superseded", "active")
    assert one["content"] == "# v1\n" and two["content"] == "# v2\n"
    assert two["supersedes_revision"] == 1
    entry = h.entry(entry_id)
    assert entry["latest_revision"] == 2 and entry["generation"] > generation_before
    assert h.active_names(TARGET_A).count(name) == 1


def test_a_stale_entry_generation_on_supersede_is_a_generation_conflict(h):
    name = unique_name("stale")
    _, old = h.publish(name=name)
    entry_id = str(old["entry_id"])
    sid = h.submitted(name=name, supersedes_entry_id=entry_id)
    before = h.counts()
    response = h.review(sid, entry_generation=h.entry(entry_id)["generation"] + 3)
    assert response.status == 409, response.text
    assert _code(response) == "GENERATION_CONFLICT"
    assert h.counts() == before


def _supersede_refusal(h, entry_id, **overrides):
    """The refusal code, whether given at submit or at approval."""
    before = h.counts()
    response = h.submit(supersedes_entry_id=entry_id, **overrides)
    if response.status == 200:
        sid = response.json()["submission_id"]
        before = h.counts()
        response = h.review(sid)
    assert response.status in REFUSED, response.text
    assert h.counts() == before
    return _code(response)


def test_supersede_target_must_match_name_and_target(h):
    name = unique_name("match")
    _, old = h.publish(name=name)
    entry_id = str(old["entry_id"])
    assert (
        _supersede_refusal(h, entry_id, name=unique_name("other"))
        == "SUPERSEDE_MISMATCH"
    )
    assert (
        _supersede_refusal(h, entry_id, name=name, resource_uid=TARGET_B)
        == "SUPERSEDE_MISMATCH"
    )
    assert [r["state"] for r in h.revisions(entry_id)] == ["active"]


def test_supersede_of_a_revoked_entry_is_refused(h):
    name = unique_name("inactive")
    _, old = h.publish(name=name)
    entry_id = str(old["entry_id"])
    assert h.revoke(entry_id, 1).status == 200
    assert _supersede_refusal(h, entry_id, name=name) == "SUPERSEDE_TARGET_INACTIVE"


def test_a_pending_supersede_whose_entry_was_revoked_meanwhile_is_refused(h):
    name = unique_name("meanwhile")
    _, old = h.publish(name=name)
    entry_id = str(old["entry_id"])
    sid = h.submitted(name=name, supersedes_entry_id=entry_id)
    assert h.revoke(entry_id, 1).status == 200
    before = h.counts()
    response = h.review(sid)
    assert response.status in REFUSED, response.text
    assert _code(response) == "SUPERSEDE_TARGET_INACTIVE"
    assert h.counts() == before


# -- revoke --------------------------------------------------------------


def test_revoke_records_reason_and_reviewer_and_is_irreversible(h):
    """F13 step 3 on the new source: revoke ends retrieval, history stays."""
    _, published = h.publish()
    entry_id = str(published["entry_id"])
    content = h.revisions(entry_id)[0]["content"]
    reason = "评测题切换"
    response = h.revoke(entry_id, 1, reason=reason)
    assert response.status == 200, response.text
    [revision] = h.revisions(entry_id)
    assert revision["state"] == "revoked" and revision["content"] == content
    [tomb] = h.revocations(entry_id)
    assert (tomb["revision"], tomb["reason"], tomb["revoked_by"]) == (
        1,
        reason,
        REVIEWER,
    )
    assert tomb["revoked_at"] >= revision["approved_at"]
    revoke = [a for a in h.audit(entry_id) if a["action"] == "revoke"]
    assert [(a["reason"], a["actor_id"]) for a in revoke] == [(reason, REVIEWER)]
    after = h.counts()
    again = h.revoke(entry_id, 1)
    assert again.status in REFUSED, again.text
    assert h.counts() == after
    # No route or action brings it back.
    assert h.post(
        f"/runbooks/{entry_id}/restore", {"idempotency_key": uuid4().hex}
    ).status in {
        404,
        405,
    }
    assert [r["state"] for r in h.revisions(entry_id)] == ["revoked"]


def test_revoke_needs_a_reason(h):
    _, published = h.publish()
    entry_id = str(published["entry_id"])
    before = h.counts()
    response = h.revoke(entry_id, 1, reason="")
    assert response.status == 400, response.text
    assert h.counts() == before
    assert h.revisions(entry_id)[0]["state"] == "active"


def test_only_the_active_revision_can_be_revoked(h):
    name = unique_name("old-rev")
    _, old = h.publish(name=name)
    entry_id = str(old["entry_id"])
    h.approved(h.submitted(name=name, supersedes_entry_id=entry_id))
    before = h.counts()
    response = h.revoke(entry_id, 1)
    assert response.status in REFUSED, response.text
    assert h.counts() == before
    assert [r["state"] for r in h.revisions(entry_id)] == ["superseded", "active"]


def test_revoke_with_a_stale_generation_is_a_generation_conflict(h):
    _, published = h.publish()
    entry_id = str(published["entry_id"])
    before = h.counts()
    response = h.revoke(
        entry_id, 1, expected_generation=h.entry(entry_id)["generation"] + 2
    )
    assert response.status == 409, response.text
    assert _code(response) == "GENERATION_CONFLICT"
    assert h.counts() == before


# -- F13 step 2 on the new source ----------------------------------------


def test_pending_and_rejected_runbooks_are_never_active(h):
    pending = h.submitted(name=unique_name("pending"))
    rejected = h.submitted(name=unique_name("rejected"))
    assert h.review(rejected, "reject", reason="no").status == 200
    for sid in (pending, rejected):
        name = h.submission(sid)["name"]
        assert name not in h.active_names(TARGET_A)
        assert (
            h.rows(
                "SELECT 1 FROM opspilot_runbook_revisions WHERE submission_id::text = %s",
                (sid,),
            )
            == []
        )


# -- pages and the HTML form path ----------------------------------------


def test_html_form_posts_redirect_back_with_303(h):
    response = h.post(
        "/runbooks",
        {
            "resource_uid": TARGET_A,
            "name": unique_name("html"),
            "description": "d",
            "content": "c",
            "source_label": "l",
            "source_ref": "r",
            "idempotency_key": uuid4().hex,
        },
        accept="text/html",
    )
    assert response.status == 303, response.text
    assert response.headers["location"].startswith("/runbooks")


def test_pages_show_states_provenance_and_original_text(h):
    marker = f"step-marker-{uuid4().hex[:8]}"
    active_name = unique_name("page-active")
    _, active = h.publish(
        name=active_name, content=f"# Steps\n{marker}\n", source_label="page label"
    )
    pending_name = unique_name("page-pending")
    pending = h.submitted(name=pending_name, content=f"<b>{marker}</b>")
    rejected_name = unique_name("page-rejected")
    assert (
        h.review(h.submitted(name=rejected_name), "reject", reason="nope").status == 200
    )
    revoked_name = unique_name("page-revoked")
    _, revoked = h.publish(name=revoked_name)
    revoke_reason = f"revoked-{uuid4().hex[:6]}"
    assert h.revoke(str(revoked["entry_id"]), 1, reason=revoke_reason).status == 200

    listing = h.get("/runbooks")
    assert listing.status == 200
    for name in (active_name, pending_name, rejected_name, revoked_name):
        assert name in listing.text, name
    assert 'href="/runbooks"' in listing.text  # the navigation entry

    assert h.get("/runbooks/new").status == 200

    review_page = h.get(f"/runbooks/submissions/{pending}")
    assert review_page.status == 200
    assert marker in review_page.text
    assert f"<b>{marker}</b>" not in review_page.text  # shown as text, escaped

    entry_page = h.get(f"/runbooks/{active['entry_id']}")
    assert entry_page.status == 200
    assert "page label" in entry_page.text and REVIEWER in entry_page.text
    revoked_page = h.get(f"/runbooks/{revoked['entry_id']}")
    assert revoke_reason in revoked_page.text
