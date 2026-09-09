"""Translate authenticated Codex review evidence into a commit status.

No PR code, model calls, credentials in files, or automatic merges. The publisher
trusts repository writers/Actions administrators; an Actions app binding is not
an identity boundary between workflows in the same repository.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
from datetime import datetime

REPOSITORY = "kevinWangSheng/production-ops-agent"
BOT_ID = 199175422
CONTEXT = "review-gate"
MARKER = "<!-- codex-pull-request-review-summary -->"
METADATA = re.compile(r"<!-- codex-security-review:v1 (\{[^\n]+\}) -->")
ROW = re.compile(
    r"^\| (?:📝|🔒) \*\*(Code Review|Security Review)\*\* \| "
    r'✅ \*\*Completed\*\* <relative-time datetime="([^"\n]+)">'
    r"[^<\n]+</relative-time> \| `([0-9a-f]{7,40})` \| [^|\n]+ \|$"
)


class GateError(Exception):
    """Safe fixed error category; never includes raw API output."""


def api(path, payload=None, paginate=False, method=None):
    if not (path.startswith(f"repos/{REPOSITORY}/") or path == "graphql"):
        raise GateError("API_PATH_REJECTED")
    args = ["gh", "api", "--hostname", "github.com", path]
    if method:
        args += ["--method", method]
    if paginate:
        args += ["--paginate", "--slurp"]
    if payload is not None:
        args += ["--input", "-"]
    try:
        result = subprocess.run(
            args,
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True,
            text=True,
            timeout=90,
            check=True,
        )
        data = json.loads(result.stdout)
    except (subprocess.SubprocessError, ValueError, OSError):
        raise GateError("API_UNAVAILABLE") from None
    if isinstance(data, dict) and data.get("errors"):
        raise GateError("API_PARTIAL_ERROR")
    return data


def collection(path):
    pages = api(f"repos/{REPOSITORY}/{path}", paginate=True)
    if not isinstance(pages, list) or any(not isinstance(p, list) for p in pages):
        raise GateError("INVALID_PAGINATION")
    return [item for page in pages for item in page]


def threads(number):
    query = """query($number:Int!,$cursor:String) {
      repository(owner:"kevinWangSheng",name:"production-ops-agent") {
        pullRequest(number:$number) {
          reviewThreads(first:100,after:$cursor) {
            nodes { id isResolved isOutdated }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }"""
    cursor, seen, found = None, set(), []
    for _ in range(100):
        data = api(
            "graphql",
            {"query": query, "variables": {"number": number, "cursor": cursor}},
        )
        try:
            page = data["data"]["repository"]["pullRequest"]["reviewThreads"]
            if not isinstance(page["nodes"], list):
                raise GateError("INVALID_THREADS")
            found.extend(page["nodes"])
            info = page["pageInfo"]
            if info["hasNextPage"] is False:
                return found
            cursor = info["endCursor"]
            if info["hasNextPage"] is not True or not cursor or cursor in seen:
                raise GateError("INVALID_CURSOR")
            seen.add(cursor)
        except (KeyError, TypeError):
            raise GateError("INVALID_THREADS") from None
    raise GateError("PAGINATION_LIMIT")


def snapshot(number):
    pr = api(f"repos/{REPOSITORY}/pulls/{number}")
    return {
        "pr": {k: pr[k] for k in ("number", "state", "draft", "head", "base")},
        "comments": collection(f"issues/{number}/comments?per_page=100"),
        "threads": threads(number),
        "reviews": collection(f"pulls/{number}/reviews?per_page=100"),
    }


def bot(comment):
    user = comment.get("user", {})
    return user.get("id") == BOT_ID and user.get("type") == "Bot"


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timezone missing")
    return parsed


def review_requests(data):
    return [
        c
        for c in data["comments"]
        if c.get("author_association") in {"OWNER", "MEMBER", "COLLABORATOR"}
        and re.match(r"^@codex (?:security )?review\b", c.get("body", "").strip())
    ]


def request_time(comment):
    return max(
        timestamp(comment["updated_at"]),
        timestamp(comment.get("created_at", comment["updated_at"])),
    )


def formal_code_review(data):
    """Code-only installations expose full commit IDs via formal reviews.

    No reaction fallback: a clean thumbs-up without a commit-bound review cannot
    be certified by this adapter. An explicit security request needs its summary.
    """
    requests = review_requests(data)
    if any(c["body"].strip().startswith("@codex security review") for c in requests):
        return "SECURITY_COMPLETION_MISSING"
    reviews = [r for r in data.get("reviews", []) if bot(r)]
    if not reviews:
        return "NO_COMMIT_BOUND_REVIEW"
    # Do not filter bad states first and accidentally fall back to old approval.
    latest = max(reviews, key=lambda r: r["id"])
    if latest.get("state") not in {"COMMENTED", "APPROVED"}:
        return "REVIEW_NOT_COMPLETED"
    if latest.get("commit_id") != data["pr"]["head"]["sha"]:
        return "REVIEW_WRONG_HEAD"
    if not latest.get("body", "").lstrip().startswith("### 💡 Codex Review\n"):
        return "UNKNOWN_FORMAL_REVIEW"
    completed = timestamp(latest["submitted_at"])
    if any(c["body"].strip() != "@codex review" for c in requests):
        return "SCOPED_REVIEW_REQUEST"
    if requests:
        request = max(requests, key=request_time)
        if request_time(request) >= completed:
            return "NEW_REVIEW_REQUEST_PENDING"
    return "READY"


def verdict(data):
    """Return a fixed reason, with READY the only successful result."""
    try:
        pr = data["pr"]
        sha = pr["head"]["sha"]
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            return "INVALID_HEAD"
        if (
            pr["state"] != "open"
            or pr["draft"] is not False
            or pr["base"]["ref"] != "main"
            or pr["base"]["repo"]["full_name"] != REPOSITORY
        ):
            return "PR_NOT_READY"
        if any(t.get("isResolved") is not True for t in data["threads"]):
            return "UNRESOLVED_THREADS"
        summaries = [
            c
            for c in data["comments"]
            if bot(c)
            and any(
                marker in c.get("body", "")
                for marker in (
                    "codex-pull-request-review-summary",
                    "codex-security-review:",
                    "## Codex Review Summary",
                )
            )
        ]
        if not summaries:
            return formal_code_review(data)
        if len(summaries) != 1:
            return "SUMMARY_MISSING_OR_AMBIGUOUS"
        summary = summaries[0]
        body = summary["body"]
        # Only the observed top-level vendor envelope is supported. Quoted,
        # fenced, duplicated or changed layouts require a reviewed adapter update.
        lines = body.splitlines()
        if lines[0] != MARKER or not METADATA.fullmatch(lines[1]):
            return "UNKNOWN_SUMMARY_FORMAT"
        meta = json.loads(METADATA.fullmatch(lines[1])[1])
        if (
            meta.get("repository") != REPOSITORY
            or type(meta.get("pullRequestNumber")) is not int
            or meta["pullRequestNumber"] != pr["number"]
            or meta.get("headSha") != sha
        ):
            return "SUMMARY_WRONG_SUBJECT"
        if meta.get("status") != "completed":
            return "REVIEW_NOT_COMPLETED"
        if (
            lines[2:7]
            != [
                "## Codex Review Summary",
                "",
                "This comment shows the latest Codex review activity on this pull request.",
                "",
                "| Review | Status | Commit | Review trigger |",
            ]
            or lines[7] != "| --- | --- | --- | --- |"
        ):
            return "UNKNOWN_SUMMARY_FORMAT"
        rows = [ROW.fullmatch(line) for line in lines[8:10]]
        if not all(rows) or {row[1] for row in rows} != {
            "Code Review",
            "Security Review",
        }:
            return "REVIEW_NOT_COMPLETED"
        if any(not sha.startswith(row[3]) for row in rows):
            return "SUMMARY_WRONG_SUBJECT"
        # The full security metadata SHA binds the envelope, not a loose prefix.
        # Any extra top-level review row is ambiguous and must not be ignored.
        if any(
            "**Code Review**" in s or "**Security Review**" in s for s in lines[10:]
        ):
            return "UNKNOWN_SUMMARY_FORMAT"
        completed = min(timestamp(row[2]) for row in rows)
        if any(timestamp(row[2]) > timestamp(summary["updated_at"]) for row in rows):
            return "INVALID_REVIEW_TIME"
        for comment in review_requests(data):
            if request_time(comment) >= completed:
                return "NEW_REVIEW_REQUEST_PENDING"
        return "READY"
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        return "INVALID_EVIDENCE"


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def status(sha, state, reason):
    """Update one app-owned check run; never exhaust commit-status history."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise GateError("INVALID_HEAD")
    pages = api(
        f"repos/{REPOSITORY}/commits/{sha}/check-runs"
        f"?check_name={CONTEXT}&filter=all&per_page=100",
        paginate=True,
    )
    try:
        runs = [run for page in pages for run in page["check_runs"]]
        owned = [
            run for run in runs if run["name"] == CONTEXT and run["app"]["id"] == 15368
        ]
        if len(owned) > 1:
            raise GateError("AMBIGUOUS_GATE_CHECKS")
    except (TypeError, KeyError):
        raise GateError("INVALID_CHECK_RUNS") from None
    payload = {
        "name": CONTEXT,
        "external_id": f"{REPOSITORY}:review-gate:v1",
        "status": "in_progress" if state == "pending" else "completed",
        "output": {"title": reason, "summary": reason},
    }
    if state != "pending":
        payload["conclusion"] = "success" if state == "success" else "failure"
    if owned:
        api(
            f"repos/{REPOSITORY}/check-runs/{owned[0]['id']}",
            payload,
            method="PATCH",
        )
    else:
        payload["head_sha"] = sha
        api(f"repos/{REPOSITORY}/check-runs", payload)


def open_prs():
    return collection("pulls?state=open&base=main&per_page=100")


def evaluate(number, publish=False):
    # Discover target before pending; if even this fails, no new status is safe.
    initial = api(f"repos/{REPOSITORY}/pulls/{number}")
    sha = initial["head"]["sha"]
    if publish:
        status(sha, "pending", "Checking current review evidence")
    try:
        first = snapshot(number)
        reason = verdict(first)
        peers = open_prs()
        if sum(p["head"]["sha"] == sha for p in peers) != 1:
            reason = "HEAD_SHARED_OR_PR_CLOSED"
        if first["pr"]["head"]["sha"] != sha:
            reason = "HEAD_CHANGED"
        if reason == "READY":
            second = snapshot(number)
            if digest(first) != digest(second):
                reason = "EVIDENCE_CHANGED"
            # Recheck duplicate SHA after collecting review evidence.
            if sum(p["head"]["sha"] == sha for p in open_prs()) != 1:
                reason = "HEAD_SHARED_OR_PR_CLOSED"
        if publish:
            status(sha, "success" if reason == "READY" else "failure", reason)
        return {"pr": number, "head": sha, "reason": reason}
    except (GateError, KeyError, TypeError):
        if publish:
            status(sha, "error", "Review evidence unavailable")
        raise GateError("REVIEW_EVIDENCE_UNAVAILABLE") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", type=int)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    if args.publish and (
        os.environ.get("GITHUB_REPOSITORY") != REPOSITORY
        or os.environ.get("GITHUB_WORKFLOW_REF")
        != f"{REPOSITORY}/.github/workflows/review-gate.yml@refs/heads/main"
    ):
        raise GateError("PUBLISH_REQUIRES_MAIN_WORKFLOW")
    numbers = [args.pr] if args.pr else [p["number"] for p in open_prs()]
    results = []
    for number in numbers:
        if type(number) is not int or number < 1:
            raise GateError("INVALID_PR")
        try:
            result = evaluate(number, args.publish)
        except GateError as error:
            result = {"pr": number, "reason": str(error)}
        results.append(result)
    print(json.dumps(results, sort_keys=True))
    return 0 if all(r["reason"] == "READY" for r in results) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GateError as error:
        print(json.dumps({"error": str(error)}))
        raise SystemExit(1) from None
