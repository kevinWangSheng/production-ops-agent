"""Observable gate decisions, API failures and publication races."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "review_gate", Path(__file__).parents[1] / "scripts/review_gate.py"
)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
SHA = "a" * 40


@pytest.fixture
def evidence():
    meta = {
        "headSha": SHA,
        "pullRequestNumber": 13,
        "repository": gate.REPOSITORY,
        "status": "completed",
        "mergeGateEnabled": False,
        "blockingSeverityThreshold": "P0",
    }
    rows = [
        f'| {icon} **{name}** | ✅ **Completed** <relative-time datetime="2026-09-09T10:00:00Z">2026-09-09T10:00:00Z</relative-time> | `{SHA[:7]}` | PR opened |'
        for icon, name in [("📝", "Code Review"), ("🔒", "Security Review")]
    ]
    body = "\n".join(
        [
            gate.MARKER,
            f"<!-- codex-security-review:v1 {json.dumps(meta)} -->",
            "## Codex Review Summary",
            "",
            "This comment shows the latest Codex review activity on this pull request.",
            "",
            "| Review | Status | Commit | Review trigger |",
            "| --- | --- | --- | --- |",
            *rows,
            "",
            "<details>vendor footer</details>",
        ]
    )
    return {
        "pr": {
            "number": 13,
            "state": "open",
            "draft": False,
            "head": {"sha": SHA},
            "base": {"ref": "main", "repo": {"full_name": gate.REPOSITORY}},
        },
        "comments": [
            {
                "user": {"id": gate.BOT_ID, "type": "Bot"},
                "body": body,
                "updated_at": "2026-09-09T10:00:01Z",
            }
        ],
        "threads": [],
    }


def test_completed_current_review_is_ready(evidence):
    assert gate.verdict(evidence) == "READY"


@pytest.mark.parametrize(
    "old,new",
    [
        (SHA, "b" * 40),
        (SHA, SHA[:7]),
        ('"pullRequestNumber": 13', '"pullRequestNumber": 14'),
        (gate.REPOSITORY, "other/repo"),
        ('"status": "completed"', '"status": "running"'),
        ("✅ **Completed**", "👀 **Running**"),
        ("✅ **Completed**", "❌ **Failed**"),
        ("codex-security-review:v1", "codex-security-review:v2"),
        (f"`{SHA[:7]}`", "`bbbbbbb`"),
    ],
)
def test_bad_or_stale_summary_never_passes(evidence, old, new):
    evidence["comments"][0]["body"] = evidence["comments"][0]["body"].replace(old, new)
    assert gate.verdict(evidence) != "READY"


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "user",
        "wrong_id",
        "quote",
        "fence",
        "extra_row",
        "future",
        "one_row",
    ],
)
def test_ambiguous_or_spoofed_evidence(evidence, mutation):
    comment = evidence["comments"][0]
    if mutation == "missing":
        evidence["comments"] = []
    elif mutation == "duplicate":
        evidence["comments"].append(copy.deepcopy(comment))
    elif mutation == "user":
        comment["user"]["type"] = "User"
    elif mutation == "wrong_id":
        comment["user"].update(id=1, login="chatgpt-codex-connector[bot]")
    elif mutation == "quote":
        comment["body"] = "> " + comment["body"].replace("\n", "\n> ")
    elif mutation == "fence":
        comment["body"] = "```\n" + comment["body"] + "\n```"
    elif mutation == "extra_row":
        comment["body"] += "\n| **Code Review** | Running |"
    elif mutation == "future":
        comment["updated_at"] = "2026-09-09T09:59:00Z"
    elif mutation == "one_row":
        comment["body"] = "\n".join(
            s for s in comment["body"].splitlines() if "🔒" not in s
        )
    assert gate.verdict(evidence) != "READY"


@pytest.mark.parametrize("outdated", [True, False])
def test_unresolved_thread_101_blocks(evidence, outdated):
    evidence["threads"] = [{"isResolved": True}] * 100 + [
        {"isResolved": False, "isOutdated": outdated}
    ]
    assert gate.verdict(evidence) == "UNRESOLVED_THREADS"


def test_resolution_allows_recheck_and_reopening_blocks(evidence):
    evidence["threads"] = [{"isResolved": True}]
    assert gate.verdict(evidence) == "READY"
    evidence["threads"][0]["isResolved"] = False
    assert gate.verdict(evidence) == "UNRESOLVED_THREADS"


@pytest.mark.parametrize("kind", ["review", "security review"])
def test_new_review_request_invalidates_completion(evidence, kind):
    evidence["comments"].append(
        {
            "body": f"@codex {kind}",
            "author_association": "OWNER",
            "updated_at": "2026-09-09T11:00:00Z",
        }
    )
    assert gate.verdict(evidence) == "NEW_REVIEW_REQUEST_PENDING"


def test_draft_and_head_change_block(evidence):
    evidence["pr"]["draft"] = True
    assert gate.verdict(evidence) != "READY"
    evidence["pr"]["draft"] = False
    evidence["pr"]["head"]["sha"] = "b" * 40
    assert gate.verdict(evidence) != "READY"


def test_rest_pagination_includes_all_pages(monkeypatch):
    monkeypatch.setattr(gate, "api", lambda *a, **k: [[1] * 100, [2]])
    assert gate.collection("issues/13/comments?per_page=100")[-1] == 2


def test_graphql_pagination_and_loop_rejection(monkeypatch):
    cursors = []

    def fake(path, payload):
        cursor = payload["variables"]["cursor"]
        cursors.append(cursor)
        return {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [{"isResolved": cursor is None}],
                            "pageInfo": {
                                "hasNextPage": cursor is None,
                                "endCursor": "next",
                            },
                        }
                    }
                }
            }
        }

    monkeypatch.setattr(gate, "api", fake)
    assert gate.threads(13) == [{"isResolved": True}, {"isResolved": False}]
    assert cursors == [None, "next"]

    def loop(path, payload):
        value = fake(path, payload)
        value["data"]["repository"]["pullRequest"]["reviewThreads"]["pageInfo"][
            "hasNextPage"
        ] = True
        return value

    monkeypatch.setattr(gate, "api", loop)
    with pytest.raises(gate.GateError, match="INVALID_CURSOR"):
        gate.threads(13)


@pytest.mark.parametrize(
    "response", ['{"errors":[{"message":"private"}],"data":{}}', "not json"]
)
def test_api_partial_or_malformed_response_blocks(monkeypatch, response):
    class Result:
        stdout = response

    monkeypatch.setattr(gate.subprocess, "run", lambda *a, **k: Result())
    with pytest.raises(gate.GateError):
        gate.api("graphql", {"query": "query"})


@pytest.mark.parametrize(
    "case",
    [
        "ready",
        "api_error",
        "head_changed",
        "summary_deleted",
        "shared_head",
        "threads_reopened",
    ],
)
def test_publisher_sets_pending_before_read_and_checks_races(
    monkeypatch, evidence, case
):
    calls = []
    monkeypatch.setattr(gate, "api", lambda *a, **k: copy.deepcopy(evidence["pr"]))
    monkeypatch.setattr(
        gate, "status", lambda sha, state, reason: calls.append((sha, state))
    )
    monkeypatch.setattr(
        gate, "open_prs", lambda: [evidence["pr"]] * (2 if case == "shared_head" else 1)
    )
    count = 0

    def snap(number):
        nonlocal count
        assert calls[0] == (SHA, "pending")
        count += 1
        if case == "api_error":
            raise gate.GateError("API_UNAVAILABLE")
        value = copy.deepcopy(evidence)
        if case == "head_changed":
            value["pr"]["head"]["sha"] = "b" * 40
        if count == 2 and case == "summary_deleted":
            value["comments"] = []
        if count == 2 and case == "threads_reopened":
            value["threads"] = [{"isResolved": False}]
        return value

    monkeypatch.setattr(gate, "snapshot", snap)
    if case == "api_error":
        with pytest.raises(gate.GateError):
            gate.evaluate(13, publish=True)
        assert calls[-1] == (SHA, "error")
    else:
        result = gate.evaluate(13, publish=True)
        assert (result["reason"] == "READY") == (case == "ready")
        assert calls[-1] == (SHA, "success" if case == "ready" else "failure")


def test_dry_run_never_publishes(monkeypatch, evidence):
    monkeypatch.setattr(gate, "api", lambda *a, **k: evidence["pr"])
    monkeypatch.setattr(gate, "snapshot", lambda n: evidence)
    monkeypatch.setattr(gate, "open_prs", lambda: [evidence["pr"]])
    monkeypatch.setattr(gate, "status", lambda *a: pytest.fail("unexpected write"))
    assert gate.evaluate(13)["reason"] == "READY"


def test_workflow_uses_main_and_has_no_pr_execution():
    workflow = (
        Path(__file__).parents[1] / ".github/workflows/review-gate.yml"
    ).read_text()
    assert "ref: refs/heads/main" in workflow
    assert "pull_request_review:" not in workflow
    assert "pull_request_review_comment:" not in workflow
    assert "cancel-in-progress: false" in workflow
    assert "make " not in workflow
    assert "secrets." not in workflow


def test_publisher_updates_one_check_without_status_history_growth(monkeypatch):
    calls = []

    def fake(path, payload=None, paginate=False, method=None):
        calls.append((path, payload, method))
        if paginate:
            return [
                {"check_runs": [{"id": 42, "name": gate.CONTEXT, "app": {"id": 15368}}]}
            ]
        return {}

    monkeypatch.setattr(gate, "api", fake)
    for _ in range(1001):
        gate.status(SHA, "pending", "Checking")
        gate.status(SHA, "success", "READY")
    writes = [c for c in calls if c[1] is not None]
    assert len(writes) == 2002
    assert all(c[0].endswith("/check-runs/42") and c[2] == "PATCH" for c in writes)
    assert all("/statuses/" not in c[0] for c in calls)


def test_first_check_creation_and_duplicate_rejection(monkeypatch):
    writes = []

    def create(path, payload=None, **kwargs):
        if kwargs.get("paginate"):
            return [{"check_runs": []}]
        writes.append(payload)

    monkeypatch.setattr(gate, "api", create)
    gate.status(SHA, "pending", "Checking")
    assert writes[0]["head_sha"] == SHA
    assert writes[0]["status"] == "in_progress"
    monkeypatch.setattr(
        gate,
        "api",
        lambda *a, **k: [
            {
                "check_runs": [
                    {"id": n, "name": gate.CONTEXT, "app": {"id": 15368}}
                    for n in (1, 2)
                ]
            }
        ],
    )
    with pytest.raises(gate.GateError, match="AMBIGUOUS_GATE_CHECKS"):
        gate.status(SHA, "success", "READY")


def test_captured_vendor_summary_format(evidence):
    # Public #11 vendor payload, replayed against a synthetic open/no-thread PR.
    # This proves parser compatibility, not that the actual merged PR was ready.
    path = Path(__file__).parents[1] / "docs/evidence/pr-review-gate/pr11-summary.json"
    comment = json.loads(path.read_text())
    evidence["comments"] = [comment]
    evidence["pr"]["number"] = 11
    evidence["pr"]["head"]["sha"] = "17d040fd3b5d967340253e532095eb3804a48e46"
    assert gate.verdict(evidence) == "READY"


@pytest.mark.parametrize(
    "association", ["NONE", "CONTRIBUTOR", "FIRST_TIMER", "FIRST_TIME_CONTRIBUTOR"]
)
def test_outsider_review_command_does_not_block(evidence, association):
    evidence["comments"].append(
        {
            "body": "@codex review",
            "author_association": association,
            "updated_at": "2026-09-09T11:00:00Z",
        }
    )
    assert gate.verdict(evidence) == "READY"


@pytest.fixture
def code_only(evidence):
    evidence["comments"] = []
    evidence["reviews"] = [
        {
            "id": 123,
            "user": {"id": gate.BOT_ID, "type": "Bot"},
            "state": "COMMENTED",
            "commit_id": SHA,
            "body": "\n### 💡 Codex Review\n\nHere are some automated review suggestions.",
            "submitted_at": "2026-09-09T10:00:00Z",
        }
    ]
    return evidence


def test_formal_commit_bound_code_only_review(code_only):
    assert gate.verdict(code_only) == "READY"


@pytest.mark.parametrize(
    "case",
    [
        "old_sha",
        "pending",
        "dismissed",
        "changes_requested",
        "wrong_bot",
        "error_body",
        "thumbs_only",
        "unresolved",
        "same_second_request",
        "security_request",
        "scoped_request",
    ],
)
def test_code_only_does_not_accept_missing_or_wrong_proof(code_only, case):
    review = code_only["reviews"][0]
    if case == "old_sha":
        review["commit_id"] = "b" * 40
    elif case in {"pending", "dismissed", "changes_requested"}:
        newer = copy.deepcopy(review)
        newer.update(id=124, state=case.upper())
        code_only["reviews"].append(newer)
    elif case == "wrong_bot":
        review["user"]["id"] = 1
    elif case == "error_body":
        review["body"] = "Could not review this PR"
    elif case == "thumbs_only":
        code_only["reviews"] = []
        code_only["reactions"] = [{"content": "+1", "user": review["user"]}]
    elif case == "unresolved":
        code_only["threads"] = [{"isResolved": False}]
    else:
        commands = {
            "same_second_request": "@codex review",
            "security_request": "@codex security review",
            "scoped_request": "@codex review only README",
        }
        code_only["comments"] = [
            {
                "body": commands[case],
                "author_association": "OWNER",
                "updated_at": "2026-09-09T10:00:00Z"
                if case == "same_second_request"
                else "2026-09-09T09:00:00Z",
            }
        ]
    assert gate.verdict(code_only) != "READY"


def test_existing_security_summary_cannot_be_bypassed_with_formal_review(
    evidence, code_only
):
    # Both fixtures are the same object under pytest caching; build fresh summary.
    captured = (
        Path(__file__).parents[1] / "docs/evidence/pr-review-gate/pr11-summary.json"
    )
    code_only["comments"] = [json.loads(captured.read_text())]
    assert gate.verdict(code_only) != "READY"


def test_late_scoped_result_cannot_satisfy_new_full_request(code_only):
    code_only["comments"] = [
        {"body": body, "author_association": "OWNER", "updated_at": when}
        for body, when in [
            ("@codex review only README", "2026-09-09T08:00:00Z"),
            ("@codex review", "2026-09-09T09:00:00Z"),
        ]
    ]
    assert gate.verdict(code_only) == "SCOPED_REVIEW_REQUEST"
