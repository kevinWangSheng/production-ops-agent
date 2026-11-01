"""F14 step 3 (#385): upstream issue reproduction records, offline part.

Layout and field rules are the seams of ``~/.agent/handoffs/m3-opt-2026-11-01/
seams-385.md`` (contract r3 + r3.1). This module never talks to docker,
kind or GitHub: it freezes raw Holmes output and captured model requests into
``runs/<run_id>.json``, extracts the OpsPilot-side fields (S7) from a frozen
OpsPilot summary plus the first model request, validates every
``record.json``, builds the step 3 upstream file list (L1) and writes the
step 3 conclusions back into the step 2 mapping table (R4, Q2, Q3).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from scripts.holmes_baseline import mapping as hmapping

RECORD_SCHEMA = "opspilot.f14.upstream-issue-record.v1"
RUN_SCHEMA = "opspilot.f14.upstream-issue-run.v1"
EVIDENCE_PREFIX = "docs/evidence/f14-upstream-issues/"
PIN_PATH = "docs/evidence/f14-holmes-baseline/pin.json"
SLUGS: dict[str, str] = {
    "upstream-2139-empty-result": "upstream_issue",
    "upstream-2356-failed-source": "upstream_issue",
    "upstream-2376-null-timeout": "upstream_issue",
    "upstream-2168-oversize-summary": "upstream_issue",
    "max-output-token-reservation": "step2_candidate",
    "raw-alert-in-prompt": "step2_candidate",
    "rollout-baseline-in-memory": "step2_candidate",
}
ISSUE_NUMBERS = {
    "upstream-2139-empty-result": (2139, "pr"),
    "upstream-2356-failed-source": (2356, "issue"),
    "upstream-2376-null-timeout": (2376, "issue"),
    "upstream-2168-oversize-summary": (2168, "pr"),
}
PAIRED = "upstream-2356-failed-source"
# own_findings must be non-empty here (L4; 修订 1 for the raw alert case)
OWN_FINDINGS_REQUIRED = frozenset({PAIRED, "raw-alert-in-prompt"})
VERDICTS = frozenset({"reproduced", "not_reproduced", "contradicted", "not_attempted"})
MODES = frozenset({"scripted_model", "real_model", "source_only"})
GROUPS = ("prometheus", "jaeger")
UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA64 = re.compile(r"^[0-9a-f]{64}$")
REPO = "HolmesGPT/holmesgpt"
SNIPPET_CHARS = 4000
EXCERPT_CHARS = 240
ERROR_CHARS = 500
FAILED_HEADING = "# Disabled & failed Toolsets"
ALERT_MARKER = "#This is context from the issue:"
# L3: the deterministic half of "the report names the missing source".
SOURCE_TERMS = {
    "prometheus": ("prometheus", "metric"),
    "jaeger": ("jaeger", "trace"),
}
UNAVAILABLE_TERMS = (
    "unreachable",
    "not reachable",
    "unavailable",
    "not available",
    "inaccessible",
    "refused",
    "could not connect",
    "couldn't connect",
    "could not reach",
    "couldn't reach",
    "cannot reach",
    "can't reach",
    "unable to reach",
    "unable to connect",
    "unable to access",
    "unable to query",
    "failed to connect",
    "failed to query",
    "failed to reach",
    "connection error",
    "connection failed",
    "(failed",
    "无法",
    "不可达",
    "不可用",
)


# --- small helpers


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_sha256(value: Any) -> str:
    """The digest pin.json uses for the Holmes config (``config.sha256``)."""
    return sha256_bytes(json.dumps(value, sort_keys=True).encode())


def slug_of(step3_ref: str) -> str:
    return step3_ref.rstrip("/").rsplit("/", 1)[-1]


def record_path(slug: str) -> str:
    return f"{EVIDENCE_PREFIX}{slug}/record.json"


def row_key(row: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    return (row.get("feature"), row.get("step"), row.get("mechanism"))


# --- freezing one Holmes Run


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    return ""


def failed_toolsets_snippet(system_prompt: str) -> str | None:
    """The heading of the disabled/failed section and only its FAILED lines;
    the 40-odd disabled builtin names stay out (R7)."""
    # the template trims whitespace, so the heading may follow the previous
    # section on the same line
    at = system_prompt.find(FAILED_HEADING)
    if at < 0:
        return None
    lines = system_prompt[at:].splitlines()
    kept = [FAILED_HEADING]
    for line in lines[1:]:
        if line.startswith("# ") or line.startswith("If you need a system"):
            break
        if "FAILED" in line or line.startswith("<no toolsets"):
            kept.append(line)
    return "\n".join(kept)[:SNIPPET_CHARS]


def alert_snippet(user_prompt: str) -> str | None:
    """The user-message paragraph that carries the issue text verbatim."""
    at = user_prompt.find(ALERT_MARKER)
    if at < 0:
        return None
    return user_prompt[at : at + SNIPPET_CHARS]


def request_summary(index: int, body: Mapping[str, Any]) -> dict[str, Any]:
    messages = [m for m in body.get("messages") or [] if isinstance(m, dict)]
    system = "\n".join(
        _text(m.get("content")) for m in messages if m.get("role") == "system"
    )
    users = [_text(m.get("content")) for m in messages if m.get("role") == "user"]
    snippets: dict[str, dict[str, str]] = {}
    for name, text in (
        ("disabled_failed_toolsets", failed_toolsets_snippet(system)),
        ("alert", alert_snippet(users[0]) if users else None),
    ):
        if text is not None:
            snippets[name] = {"sha256": sha256_bytes(text.encode()), "text": text}
    tool_names = [
        (t.get("function") or {}).get("name")
        for t in body.get("tools") or []
        if isinstance(t, dict)
    ]
    return {
        "index": index,
        "body_sha256": canonical_sha256(body),
        "message_count": len(messages),
        "tool_names": tool_names,
        "tool_names_sha256": canonical_sha256(tool_names),
        "max_tokens": body.get("max_tokens", body.get("max_completion_tokens")),
        "snippets": snippets,
    }


def holmes_result(output: Any) -> dict[str, Any]:
    """``holmes ask`` writes one result object; ``investigate`` a list."""
    if isinstance(output, list):
        return output[0] if output and isinstance(output[0], dict) else {}
    return output if isinstance(output, dict) else {}


def tool_results(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for index, call in enumerate(result.get("tool_calls") or []):
        res = call.get("result") or {}
        params = res.get("params")
        rows.append(
            {
                "index": index,
                "tool": call.get("tool_name"),
                "toolset": call.get("toolset_name"),
                "status": res.get("status"),
                "error": None
                if res.get("error") is None
                else str(res.get("error"))[:ERROR_CHARS],
                "params": params if isinstance(params, dict) else None,
            }
        )
    return rows


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?。！？;；])\s+|\n+", text)
    return [p.strip() for p in parts if p.strip()]


def sentence_hit(
    text: str, *, any_of: Sequence[str], also: Sequence[str] = ()
) -> dict[str, Any]:
    """First sentence containing one of ``any_of`` and (if given) one of
    ``also``, case-insensitive; ``excerpt`` is at most 240 characters (L6)."""
    for sentence in sentences(text):
        low = sentence.lower()
        if any(t.lower() in low for t in any_of) and (
            not also or any(t.lower() in low for t in also)
        ):
            return {"hit": True, "excerpt": sentence[:EXCERPT_CHARS]}
    return {"hit": False, "excerpt": None}


def names_missing_source(text: str, source: str) -> dict[str, Any]:
    """L3: a source name and an unavailability word in the same sentence."""
    return sentence_hit(text, any_of=SOURCE_TERMS[source], also=UNAVAILABLE_TERMS)


def freeze_run(
    *,
    run_id: str,
    slug: str,
    case: str,
    mode: str,
    entry: str,
    image_id: str,
    config_sha256: str,
    started_at: str,
    finished_at: str,
    exit_code: int,
    timed_out: bool,
    output: Any,
    captured: Sequence[Mapping[str, Any]],
    answer_checks: Mapping[str, Mapping[str, Any]] | None = None,
    stdout_checks: Mapping[str, Mapping[str, Any]] | None = None,
    proxy_checks: Mapping[str, Mapping[str, Any]] | None = None,
    tool_checks: Mapping[str, Mapping[str, Any]] | None = None,
    request_checks: Mapping[str, Mapping[str, Any]] | None = None,
    model_env_overrides: Mapping[str, str] | None = None,
    balance_window: Mapping[str, Any] | None = None,
    stimulus: Mapping[str, Any] | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """``runs/<run_id>.json``: hashes, snippets, tool statuses and checks;
    no messages, no reasoning, no full answer."""
    result = holmes_result(output)
    answer = result.get("result") if isinstance(result.get("result"), str) else None
    return {
        "schema": RUN_SCHEMA,
        "run_id": run_id,
        "slug": slug,
        "case": case,
        "mode": mode,
        "entry": entry,
        "image_id": image_id,
        "config_sha256": config_sha256,
        "config": dict(config or {}),
        "stimulus": dict(stimulus or {}),
        "model_env_overrides": dict(model_env_overrides or {}),
        "started_at": started_at,
        "finished_at": finished_at,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "requests": [request_summary(i, c["body"]) for i, c in enumerate(captured)],
        "tool_results": tool_results(result),
        "answer_sha256": None if answer is None else sha256_bytes(answer.encode()),
        "answer_checks": {k: dict(v) for k, v in (answer_checks or {}).items()},
        "stdout_checks": {k: dict(v) for k, v in (stdout_checks or {}).items()},
        "proxy_checks": {k: dict(v) for k, v in (proxy_checks or {}).items()},
        "tool_checks": {k: dict(v) for k, v in (tool_checks or {}).items()},
        "request_checks": {k: dict(v) for k, v in (request_checks or {}).items()},
        "balance_window": None if balance_window is None else dict(balance_window),
    }


# --- OpsPilot side (S7, L5)


def first_request_fields(raw: bytes, source: str) -> dict[str, Any]:
    """sha256 of the first serialized model request, the ``data_sources``
    projection it carries, and whether it says ``source`` was unreachable at
    the start (it cannot today: the projection is static, otel_demo.py)."""
    body = json.loads(raw)
    found: list[Any] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if "data_sources" in value and isinstance(value["data_sources"], dict):
                found.append(value["data_sources"])
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str) and value[:1] in "{[" and "data_sources" in value:
            try:
                walk(json.loads(value))
            except ValueError:
                pass

    walk(body)
    if not found:
        raise SystemExit("first request carries no data_sources projection")
    text = raw.decode()
    mentions = names_missing_source(text, source)["hit"] or any(
        word in text.lower() for word in ("unreachable at start", "开始时不可达")
    )
    return {
        "first_request_sha256": sha256_bytes(raw),
        "data_sources": found[0],
        "first_request_mentions_unreachable": bool(mentions),
    }


def report_text(report: Mapping[str, Any]) -> str:
    parts = [str(report.get("conclusion") or ""), str(report.get("summary") or "")]
    parts += [str(g) for g in report.get("gaps") or []]
    return "\n".join(parts)


def opspilot_run(
    summary_path: Path,
    summary_rel: str,
    first_request: bytes,
    source: str,
    versions: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One R of ``opspilot_side.groups.<source>.runs`` (seams 2.5)."""
    raw = summary_path.read_bytes()
    summary = json.loads(raw)
    report = summary.get("report") or {}
    if not isinstance(report, dict):
        report = {}
    coverage = ((summary.get("f3_coverage") or {}).get("coverage") or {}).get(
        "sources"
    ) or []
    entry = next((s for s in coverage if s.get("name") == source), None)
    # the lab summary has no versions block; m1_live_runner's first-request
    # tap writes them beside the request (``<path>.versions.json``)
    versions = versions or summary.get("versions") or {}
    return {
        "run_id": summary["run_id"],
        "summary": summary_rel,
        "summary_sha256": sha256_bytes(raw),
        "versions": {
            k: versions.get(k)
            for k in (
                "tool_schema_revision",
                "prompt_revision",
                "context_policy_revision",
            )
        },
        **first_request_fields(first_request, source),
        "report_gaps": [str(g) for g in report.get("gaps") or []],
        "report_coverage": entry,
        "report_names_missing_source": names_missing_source(
            report_text(report), source
        )["hit"],
    }


def q7(opspilot_side: Mapping[str, Any]) -> dict[str, Any]:
    """Q7 + L3 on the Prometheus group only (Q6)."""
    holmes = opspilot_side["holmes"]["prometheus"]
    ops = opspilot_side["groups"]["prometheus"]["runs"]
    input_gap = (
        holmes["k_input"] >= 1
        and bool(ops)
        and not any(r["first_request_mentions_unreachable"] for r in ops)
    )
    report_gap = (
        holmes["k_report"] >= 1
        and bool(ops)
        and not any(r["report_names_missing_source"] for r in ops)
    )
    return {
        "input_gap": input_gap,
        "report_gap": report_gap,
        "n1_triggered": input_gap and report_gap,
        "decided_by": "agent_S8",
    }


# --- record validation (seams section 2)


def _is_utc(value: Any) -> bool:
    return isinstance(value, str) and bool(UTC.fullmatch(value))


def _ref_ok(ref: Any) -> bool:
    return isinstance(ref, str) and hmapping.parse_ref(ref) is not None


def _upstream_ref_problems(
    where: str, ref: Any, number: int | None, kind: str | None
) -> list[str]:
    if not isinstance(ref, dict):
        return [f"{where}: upstream ref is not an object"]
    problems = []
    if ref.get("repo") != REPO:
        problems.append(f"{where}: repo {ref.get('repo')!r}")
    if ref.get("kind") not in ("issue", "pr"):
        problems.append(f"{where}: kind {ref.get('kind')!r}")
    if number is not None and (ref.get("number"), ref.get("kind")) != (number, kind):
        problems.append(f"{where}: number/kind {ref.get('number')}/{ref.get('kind')}")
    part = "pull" if ref.get("kind") == "pr" else "issues"
    if ref.get("url") != f"https://github.com/{REPO}/{part}/{ref.get('number')}":
        problems.append(f"{where}: url {ref.get('url')!r}")
    if ref.get("state") not in ("open", "closed", "merged"):
        problems.append(f"{where}: state {ref.get('state')!r}")
    if not _is_utc(ref.get("checked_at")):
        problems.append(f"{where}: checked_at")
    return problems


def _finding_problems(
    slug: str, where: str, finding: Mapping[str, Any], directory: Path, image_id: str
) -> list[str]:
    problems = []
    verdict, mode = finding.get("verdict"), finding.get("mode")
    runs, k = finding.get("runs"), finding.get("k")
    evidence = finding.get("evidence")
    if verdict not in VERDICTS:
        problems.append(f"{where}: verdict {verdict!r}")
    if mode not in MODES:
        problems.append(f"{where}: mode {mode!r}")
    if not isinstance(finding.get("note"), str) or not finding["note"].strip():
        problems.append(f"{where}: empty note")
    if not isinstance(evidence, list) or not all(isinstance(e, str) for e in evidence):
        return problems + [f"{where}: evidence is not a list of paths"]
    if (mode == "source_only") != (verdict == "not_attempted"):
        problems.append(f"{where}: source_only <=> not_attempted")
    if mode == "source_only":
        if runs != 0 or k is not None:
            problems.append(f"{where}: source_only needs runs 0 and k null")
        return problems
    if not isinstance(runs, int) or runs < 1:
        return problems + [f"{where}: runs {runs!r}"]
    if not isinstance(k, int) or not 0 <= k <= runs:
        problems.append(f"{where}: k {k!r}")
    elif verdict == "reproduced" and k < 1:
        problems.append(f"{where}: reproduced with k 0")
    elif verdict in ("not_reproduced", "contradicted") and k != 0:
        problems.append(f"{where}: {verdict} with k {k}")
    minimum = 3 if mode == "real_model" and finding.get("group") != "jaeger" else 1
    if runs < minimum:
        problems.append(f"{where}: {mode} needs runs >= {minimum}")
    if not evidence:
        problems.append(f"{where}: no evidence")
    run_files = [e for e in evidence if e.startswith("runs/") and e.endswith(".json")]
    if len(run_files) != runs:
        problems.append(f"{where}: {len(run_files)} run files for runs {runs}")
    for rel in evidence:
        path = directory / rel
        if rel.startswith("/") or ".." in Path(rel).parts or not path.is_file():
            problems.append(f"{where}: evidence {rel} missing")
            continue
        if rel in run_files:
            run = json.loads(path.read_text())
            if run.get("schema") != RUN_SCHEMA or run.get("slug") != slug:
                problems.append(f"{where}: {rel} is not a run of {slug}")
            if run.get("image_id") != image_id:
                problems.append(f"{where}: {rel} image_id differs from pin.json")
            if run.get("mode") != mode:
                problems.append(f"{where}: {rel} mode {run.get('mode')!r}")
    return problems


def _opspilot_problems(directory: Path, side: Any) -> list[str]:
    where = f"{PAIRED}: opspilot_side"
    if not isinstance(side, dict):
        return [f"{where} is not an object"]
    problems = []
    groups, holmes = side.get("groups") or {}, side.get("holmes") or {}
    for group in GROUPS:
        g = groups.get(group)
        if not isinstance(g, dict) or not isinstance(g.get("runs"), list):
            problems.append(f"{where}: group {group} missing")
            continue
        if g.get("n") != len(g["runs"]):
            problems.append(f"{where}: {group} n != len(runs)")
        for run in g["runs"]:
            rel = run.get("summary", "")
            path = directory / rel
            if not rel.startswith("opspilot/") or not path.is_file():
                problems.append(f"{where}: {group} summary {rel} missing")
            elif sha256_bytes(path.read_bytes()) != run.get("summary_sha256"):
                problems.append(f"{where}: {group} summary sha256 differs")
            if not SHA64.fullmatch(str(run.get("first_request_sha256"))):
                problems.append(f"{where}: {group} first_request_sha256")
            if not isinstance(run.get("data_sources"), dict):
                problems.append(f"{where}: {group} data_sources")
        h = holmes.get(group)
        if not isinstance(h, dict):
            problems.append(f"{where}: holmes {group} missing")
            continue
        for key in ("input_runs", "report_runs"):
            for rel in h.get(key) or []:
                if not (directory / rel).is_file():
                    problems.append(f"{where}: holmes {group} {rel} missing")
        for key, runs in (("k_input", "input_runs"), ("k_report", "report_runs")):
            if not isinstance(h.get(key), int) or not 0 <= h[key] <= len(
                h.get(runs) or []
            ):
                problems.append(f"{where}: holmes {group} {key}")
    if not problems:
        if (
            groups["prometheus"]["n"] != 3
            or len(holmes["prometheus"]["report_runs"]) != 3
        ):
            problems.append(f"{where}: Prometheus group needs 3 + 3 runs (Q8)")
        if side.get("q7") != q7(side):
            problems.append(f"{where}: q7 does not follow from the runs")
    return problems


def record_problems(
    slug: str,
    record: Mapping[str, Any],
    directory: Path,
    pin: Mapping[str, Any],
    mapping_rows: Iterable[Mapping[str, Any]],
) -> list[str]:
    problems: list[str] = []
    kind = SLUGS.get(slug)
    if kind is None:
        return [f"{slug}: unknown slug"]
    if record.get("schema") != RECORD_SCHEMA:
        problems.append(f"{slug}: schema")
    if record.get("slug") != slug or record.get("kind") != kind:
        problems.append(f"{slug}: slug/kind")
    if kind == "upstream_issue":
        number, issue_kind = ISSUE_NUMBERS[slug]
        problems += _upstream_ref_problems(
            slug, record.get("upstream_ref"), number, issue_kind
        )
    elif record.get("upstream_ref") is not None:
        problems.append(f"{slug}: step2_candidate needs upstream_ref null")
    for i, ref in enumerate(record.get("background_refs") or []):
        problems += _upstream_ref_problems(
            f"{slug} background_refs[{i}]", ref, None, None
        )
        if not str((ref or {}).get("why") or "").strip():
            problems.append(f"{slug}: background_refs[{i}] without why")
    claims = record.get("reporter_claims")
    if not isinstance(claims, list) or not claims:
        return problems + [f"{slug}: no reporter_claims"]
    ids = [c.get("id") for c in claims]
    if len(set(ids)) != len(ids) or not all(isinstance(i, str) and i for i in ids):
        problems.append(f"{slug}: claim ids")
    for claim in claims:
        if (
            not str(claim.get("quote") or "").strip()
            or not str(claim.get("source") or "").strip()
        ):
            problems.append(f"{slug}: claim {claim.get('id')} quote/source")
    code = record.get("code_at_pin")
    if not isinstance(code, list) or not code or not all(_ref_ok(r) for r in code):
        problems.append(f"{slug}: code_at_pin")
        code = []
    master = record.get("code_at_master") or {}
    if not SHA40.fullmatch(str(master.get("sha"))) or not _is_utc(
        master.get("checked_at")
    ):
        problems.append(f"{slug}: code_at_master sha/checked_at")
    pin_paths = {hmapping.parse_ref(r)[0] for r in code if hmapping.parse_ref(r)}  # type: ignore[index]
    if not set(master.get("changed_files_relevant") or []) <= pin_paths:
        problems.append(f"{slug}: changed_files_relevant outside code_at_pin")
    for fix in record.get("related_fixes") or []:
        if fix.get("merged") is False and fix.get("in_pin") is not None:
            problems.append(f"{slug}: unmerged fix #{fix.get('number')} with in_pin")
    image_id = pin["image_id"]
    if record.get("image_id") != image_id:
        problems.append(f"{slug}: image_id differs from pin.json")
    for cfg in record.get("configs") or []:
        if (
            cfg.get("differs_from_pin") is True
            and not str(cfg.get("reason") or "").strip()
        ):
            problems.append(f"{slug}: config {cfg.get('name')} differs without reason")
        if (
            cfg.get("differs_from_pin") is False
            and cfg.get("sha256") != pin["config"]["sha256"]
        ):
            problems.append(f"{slug}: config {cfg.get('name')} sha differs from pin")
    findings = record.get("local_findings")
    if not isinstance(findings, list) or not findings:
        return problems + [f"{slug}: no local_findings"]
    seen = set()
    for i, finding in enumerate(findings):
        where = f"{slug} local_findings[{i}]"
        group = finding.get("group")
        if finding.get("claim_id") not in ids:
            problems.append(f"{where}: unknown claim_id")
        if slug == PAIRED:
            if group not in GROUPS:
                problems.append(f"{where}: group {group!r}")
        elif group is not None:
            problems.append(f"{where}: group must be null")
        key = (finding.get("claim_id"), group)
        if key in seen:
            problems.append(f"{where}: duplicate claim/group")
        seen.add(key)
        problems += _finding_problems(slug, where, finding, directory, image_id)
    for claim_id in ids:
        wanted = (
            [(claim_id, g) for g in GROUPS] if slug == PAIRED else [(claim_id, None)]
        )
        for key in wanted:
            if key not in seen:
                problems.append(f"{slug}: no finding for {key}")
    ran = any(f.get("mode") != "source_only" for f in findings)
    if ran != (record.get("reproduced_at") is not None):
        problems.append(f"{slug}: reproduced_at null iff nothing ran")
    if record.get("reproduced_at") is not None and not _is_utc(record["reproduced_at"]):
        problems.append(f"{slug}: reproduced_at format")
    own = record.get("own_findings")
    if not isinstance(own, list) or (slug in OWN_FINDINGS_REQUIRED) != bool(own):
        problems.append(f"{slug}: own_findings")
    if (slug == PAIRED) != (record.get("opspilot_side") is not None):
        problems.append(f"{slug}: opspilot_side only for {PAIRED}")
    if slug == PAIRED:
        problems += _opspilot_problems(directory, record.get("opspilot_side"))
    keys = {row_key(r) for r in mapping_rows}
    for row in record.get("mapping_rows") or []:
        if (row.get("feature"), row.get("step"), row.get("mechanism")) not in keys:
            problems.append(f"{slug}: mapping row {row} not in mapping.json")
    return problems


def load_records(root: Path) -> dict[str, dict[str, Any]]:
    base = root / EVIDENCE_PREFIX
    return {
        slug: json.loads((base / slug / "record.json").read_text())
        for slug in SLUGS
        if (base / slug / "record.json").is_file()
    }


def cited_paths(records: Mapping[str, Mapping[str, Any]]) -> list[str]:
    paths = set()
    for record in records.values():
        for ref in record.get("code_at_pin") or []:
            parsed = hmapping.parse_ref(ref)
            if parsed is not None:
                paths.add(parsed[0])
        for finding in record.get("own_findings") or []:
            for ref in finding.get("code_at_pin") or []:
                parsed = hmapping.parse_ref(ref)
                if parsed is not None:
                    paths.add(parsed[0])
    return sorted(paths)


def manifest_problems(
    records: Mapping[str, Mapping[str, Any]], manifest: Mapping[str, Any]
) -> list[str]:
    problems = []
    if (manifest.get("schema"), manifest.get("commit"), manifest.get("tree")) != (
        hmapping.FILES_SCHEMA,
        hmapping.COMMIT,
        hmapping.TREE,
    ):
        problems.append("step 3 upstream-files.json schema/commit/tree")
    files = manifest.get("files") or {}
    if sorted(files) != cited_paths(records):
        problems.append(
            "step 3 upstream-files.json does not list exactly the cited files"
        )
    for slug, record in records.items():
        refs = list(record.get("code_at_pin") or [])
        for finding in record.get("own_findings") or []:
            refs += finding.get("code_at_pin") or []
        for ref in refs:
            parsed = hmapping.parse_ref(ref)
            if parsed and parsed[0] in files and parsed[2] > files[parsed[0]]["lines"]:
                problems.append(
                    f"{slug}: {ref} beyond {files[parsed[0]]['lines']} lines"
                )
    return problems


def all_problems(root: Path) -> list[str]:
    base = root / EVIDENCE_PREFIX
    pin = json.loads((root / PIN_PATH).read_text())
    table = json.loads(
        (root / "docs/evidence/f14-upstream-mapping/mapping.json").read_text()
    )
    records = load_records(root)
    problems = [f"{slug}: record.json missing" for slug in SLUGS if slug not in records]
    for slug, record in records.items():
        problems += record_problems(slug, record, base / slug, pin, table["rows"])
    manifest_path = base / "upstream-files.json"
    if not manifest_path.is_file():
        problems.append("step 3 upstream-files.json missing")
    else:
        problems += manifest_problems(records, json.loads(manifest_path.read_text()))
    problems += writeback_problems(table, records)
    return problems


# --- mapping.json write-back (seams section 3)


def candidate_finding(record: Mapping[str, Any], claim: str) -> dict[str, Any] | None:
    ids = [
        c["id"] for c in record.get("reporter_claims") or [] if c.get("quote") == claim
    ]
    if len(ids) != 1:
        return None
    found = [
        f for f in record.get("local_findings") or [] if f.get("claim_id") == ids[0]
    ]
    return found[0] if len(found) == 1 else None


def conclusion_note(finding: Mapping[str, Any], path: str) -> str:
    k = "-" if finding.get("k") is None else finding["k"]
    return (
        f"第 3 步：{finding['verdict']}（{k}/{finding['runs']}，"
        f"{finding['mode']}），见 {path}。"
    )


def writeback(
    table: Mapping[str, Any], records: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """The table with step 3 conclusions applied; idempotent (rows that
    already carry ``step3_record`` are left alone)."""
    out = copy.deepcopy(dict(table))
    referenced: dict[tuple[Any, Any, Any], list[str]] = {}
    for slug, record in records.items():
        for row in record.get("mapping_rows") or []:
            key = (row.get("feature"), row.get("step"), row.get("mechanism"))
            referenced.setdefault(key, []).append(record_path(slug))
    for row in out["rows"]:
        candidate = row.get("candidate_defect")
        if candidate is not None:
            slug = slug_of(candidate["step3_ref"])
            record = records.get(slug)
            if record is None:
                raise SystemExit(f"no record for candidate {slug}")
            finding = candidate_finding(record, candidate["claim"])
            if finding is None:
                raise SystemExit(f"{slug}: no single finding for the candidate claim")
            path = record_path(slug)
            del row["candidate_defect"]
            row["step3_record"] = path
            row["note"] = f"{row['note']} {conclusion_note(finding, path)}"
            if finding["verdict"] == "reproduced":
                row["class"] = "confirmed_defect"
                row["runtime_evidence"] = [
                    *row["runtime_evidence"],
                    *(p for p in (path, PIN_PATH) if p not in row["runtime_evidence"]),
                ]
            continue
        if row.get("class") == "confirmed_defect" and "step3_record" not in row:
            for path in referenced.get(row_key(row), []):
                if path not in row["runtime_evidence"]:
                    row["runtime_evidence"] = [*row["runtime_evidence"], path]
    return out


def writeback_problems(
    table: Mapping[str, Any], records: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    """What is wrong with an already written-back table."""
    problems = []
    for row in table["rows"]:
        rid = "{}#{} {}".format(*row_key(row))
        if "candidate_defect" in row:
            problems.append(f"{rid}: still a candidate")
        path = row.get("step3_record")
        if path is None:
            continue
        slug = slug_of(path.rsplit("/", 1)[0])
        record = records.get(slug)
        if record is None or path != record_path(slug):
            problems.append(f"{rid}: step3_record {path} has no record")
            continue
        claims = [
            c
            for c in record.get("reporter_claims") or []
            if record.get("kind") == "step2_candidate"
        ]
        finding = (
            candidate_finding(record, claims[0]["quote"]) if len(claims) == 1 else None
        )
        if finding is None:
            problems.append(f"{rid}: record has no single candidate finding")
            continue
        if "第 3 步：" not in row.get("note", ""):
            problems.append(f"{rid}: note lacks the step 3 conclusion")
        reproduced = finding["verdict"] == "reproduced"
        if reproduced != (row.get("class") == "confirmed_defect"):
            problems.append(
                f"{rid}: class does not follow verdict {finding['verdict']}"
            )
        if reproduced and not {path, PIN_PATH} <= set(
            row.get("runtime_evidence") or []
        ):
            problems.append(
                f"{rid}: confirmed defect must cite the record and pin.json"
            )
    keys = {row_key(r): r for r in table["rows"]}
    for slug, record in records.items():
        for ref in record.get("mapping_rows") or []:
            row = keys.get((ref.get("feature"), ref.get("step"), ref.get("mechanism")))
            if (
                row is not None
                and row.get("class") == "confirmed_defect"
                and "step3_record" not in row
            ):
                if record_path(slug) not in row.get("runtime_evidence", []):
                    problems.append(
                        f"{slug}: confirmed row {ref} does not cite the record"
                    )
    return problems
