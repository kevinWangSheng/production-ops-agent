"""Offline acceptance checks for F14 step 3 (#385).

These checks consume only committed evidence.  They deliberately do not
import or execute the implementation under review, contact HolmesGPT, or
start Docker/kind.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "docs/evidence/f14-upstream-issues"
MAPPING = ROOT / "docs/evidence/f14-upstream-mapping/mapping.json"
PIN = ROOT / "docs/evidence/f14-holmes-baseline/pin.json"
PAGE = ROOT / "docs/testing/f14-upstream-issues.md"
UPSTREAM_FILES = EVIDENCE / "upstream-files.json"

SLUGS = {
    "upstream-2139-empty-result",
    "upstream-2356-failed-source",
    "upstream-2376-null-timeout",
    "upstream-2168-oversize-summary",
    "max-output-token-reservation",
    "raw-alert-in-prompt",
    "rollout-baseline-in-memory",
}
TARGET_ROWS = {
    ("F1", 4, "MAX_OUTPUT_TOKEN_RESERVATION 只定义不使用"),
    ("F7", 2, "日志、runbook 与告警里的恶意指令不扩大查询范围"),
    ("F11", 1, "由观察到的发布触发调查，保留精确的前后版本与服务身份"),
    ("F3", 4, "空结果被报成错误（NO_DATA 分支走不到）"),
}
HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")
HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
REF = re.compile(r"^(?P<path>[^:]+):L(?P<start>[1-9][0-9]*)-L(?P<end>[1-9][0-9]*)$")
UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def load(path: Path) -> Any:
    assert path.is_file(), f"missing committed evidence: {path.relative_to(ROOT)}"
    return json.loads(path.read_text())


def pin_id() -> str:
    return load(PIN)["image_id"]


def records() -> dict[str, dict[str, Any]]:
    result = {}
    for slug in SLUGS:
        record_path = EVIDENCE / slug / "record.json"
        record = load(record_path)
        assert record.get("slug") == slug
        result[slug] = record
    return result


def mapping_rows() -> list[dict[str, Any]]:
    table = load(MAPPING)
    assert isinstance(table.get("rows"), list)
    return table["rows"]


def _row_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return row.get("feature"), row.get("step"), row.get("mechanism")


def _run_files(slug: str, finding: dict[str, Any]) -> list[Path]:
    paths = finding.get("evidence", [])
    return [EVIDENCE / slug / path for path in paths if path.startswith("runs/")]


def test_all_seven_records_and_top_level_contract():
    recs = records()
    assert set(recs) == SLUGS
    for slug, record in recs.items():
        assert record.get("schema") == "opspilot.f14.upstream-issue-record.v1"
        assert record.get("kind") in {"upstream_issue", "step2_candidate"}
        assert (record["upstream_ref"] is not None) == (
            record["kind"] == "upstream_issue"
        )
        assert isinstance(record.get("background_refs"), list)
        assert record.get("reporter_claims")
        assert isinstance(record.get("code_at_pin"), list) and record["code_at_pin"]
        assert isinstance(record.get("code_at_master"), dict)
        assert HEX40.fullmatch(record["code_at_master"].get("sha", ""))
        assert UTC.fullmatch(record["code_at_master"].get("checked_at", ""))
        assert isinstance(record["code_at_master"].get("changed_files_relevant"), list)
        pin_paths = {
            m.group("path")
            for ref in record["code_at_pin"]
            if (m := REF.fullmatch(ref))
        }
        assert pin_paths and all(
            REF.fullmatch(ref)
            and int(REF.fullmatch(ref).group("start"))
            <= int(REF.fullmatch(ref).group("end"))
            for ref in record["code_at_pin"]
        )
        assert set(record["code_at_master"]["changed_files_relevant"]) <= pin_paths
        assert isinstance(record.get("related_fixes"), list)
        for fix in record["related_fixes"]:
            assert isinstance(fix.get("number"), int)
            assert fix.get("state") in {"open", "closed", "merged"}
            assert isinstance(fix.get("merged"), bool)
            assert fix.get("merged") or fix.get("in_pin") is None
        assert record.get("image_id") == pin_id()
        assert isinstance(record.get("configs"), list)
        for config in record["configs"]:
            assert {"name", "sha256", "differs_from_pin", "reason"} <= config.keys()
            assert HEX64.fullmatch(config["sha256"])
            if config["differs_from_pin"]:
                assert config["reason"].strip()
            else:
                assert config["sha256"] == load(PIN)["config"]["sha256"]
        assert record.get("reproduced_at") is None or UTC.fullmatch(
            record["reproduced_at"]
        )
        assert isinstance(record.get("own_findings"), list)
        assert isinstance(record.get("mapping_rows"), list)
        if slug == "rollout-baseline-in-memory":
            assert record["configs"] == []
        if slug == "upstream-2356-failed-source":
            assert record.get("opspilot_side") is not None
        else:
            assert record.get("opspilot_side") is None


def test_upstream_refs_claims_and_mapping_rows_are_bound():
    recs = records()
    for slug, record in recs.items():
        claims = record["reporter_claims"]
        ids = [claim.get("id") for claim in claims]
        assert len(ids) == len(set(ids)) and all(
            re.fullmatch(r"C\d+", str(i)) for i in ids
        )
        for claim in claims:
            assert claim.get("quote", "").strip() and claim.get("source", "").strip()
        if record["kind"] == "upstream_issue":
            ref = record["upstream_ref"]
            assert ref["repo"] == "HolmesGPT/holmesgpt"
            assert ref["number"] in {2139, 2356, 2376, 2168}
            assert ref["kind"] in {"issue", "pr"}
            expected_kind = "pr" if ref["number"] in {2139, 2168} else "issue"
            assert ref["kind"] == expected_kind
            assert (
                ref["url"]
                == f"https://github.com/HolmesGPT/holmesgpt/{'pull' if expected_kind == 'pr' else 'issues'}/{ref['number']}"
            )
            assert ref["state"] in {"open", "closed", "merged"} and UTC.fullmatch(
                ref["checked_at"]
            )
        else:
            assert all(claim["source"].startswith("mapping.json ") for claim in claims)
            origin = json.loads(
                subprocess.check_output(
                    [
                        "git",
                        "show",
                        "origin/main:docs/evidence/f14-upstream-mapping/mapping.json",
                    ],
                    text=True,
                )
            )
            candidate = next(
                row["candidate_defect"]
                for row in origin["rows"]
                if Path(row["candidate_defect"]["step3_ref"].rstrip("/")).name == slug
            )
            assert claims[0]["quote"] == candidate["claim"]
        triples = {_row_key(row) for row in mapping_rows()}
        assert record["mapping_rows"]
        assert all(_row_key(row) in triples for row in record["mapping_rows"])


def test_findings_verdict_evidence_and_q8_rules():
    recs = records()
    for slug, record in recs.items():
        claim_ids = {claim["id"] for claim in record["reporter_claims"]}
        findings = record["local_findings"]
        assert findings
        keys = [(f.get("claim_id"), f.get("group")) for f in findings]
        assert len(keys) == len(set(keys))
        assert {f.get("claim_id") for f in findings} == claim_ids
        if slug == "upstream-2356-failed-source":
            assert {(f["claim_id"], f["group"]) for f in findings} == {
                (cid, group) for cid in claim_ids for group in ("prometheus", "jaeger")
            }
        for finding in findings:
            assert finding["verdict"] in {
                "reproduced",
                "not_reproduced",
                "contradicted",
                "not_attempted",
            }
            assert finding["mode"] in {"scripted_model", "real_model", "source_only"}
            assert isinstance(finding.get("runs"), int) and finding["runs"] >= 0
            assert finding["note"].strip()
            if finding["mode"] == "source_only":
                assert (
                    finding["verdict"] == "not_attempted"
                    and finding["runs"] == 0
                    and finding["k"] is None
                )
            else:
                assert (
                    finding["runs"] >= 1
                    and isinstance(finding["k"], int)
                    and 0 <= finding["k"] <= finding["runs"]
                )
                assert finding["evidence"]
                run_files = _run_files(slug, finding)
                assert len(run_files) == finding["runs"] and all(
                    path.is_file() for path in run_files
                )
                assert all(load(path).get("image_id") == pin_id() for path in run_files)
                if finding["mode"] == "real_model":
                    if finding.get("group") == "jaeger":
                        assert finding["runs"] >= 1
                    else:
                        assert finding["runs"] >= 3
                else:
                    assert finding["runs"] >= 1
            if finding["verdict"] == "reproduced":
                assert finding["k"] >= 1
            elif finding["verdict"] in {"not_reproduced", "contradicted"}:
                assert finding["k"] == 0
            if finding["verdict"] in {"reproduced", "contradicted"}:
                assert finding["evidence"]
        if slug == "rollout-baseline-in-memory":
            assert len(findings) == 1 and findings[0]["mode"] == "source_only"
            assert "K8s" in findings[0]["note"] or "watch" in findings[0]["note"]
            assert any(
                "holmes_operator/trigger_executor.py:L29-L34" in ref
                for ref in record["code_at_pin"]
            )


def test_run_freezes_contain_only_contract_fields_and_no_raw_prompt_reasoning():
    forbidden_keys = {"messages", "raw_prompt", "reasoning", "ledger", "raw_output"}
    for slug in SLUGS:
        for path in (EVIDENCE / slug).rglob("*.json"):
            data = load(path)
            if path.name == "record.json":
                continue
            if "runs" not in path.parts and "opspilot" not in path.parts:
                continue
            assert data.get("schema") in {
                "opspilot.f14.upstream-issue-run.v1",
                "opspilot-lab-summary-1",
            }
            if data.get("schema") == "opspilot.f14.upstream-issue-run.v1":
                assert data["image_id"] == pin_id()
                assert HEX64.fullmatch(data["config_sha256"])
                assert UTC.fullmatch(data["started_at"]) and UTC.fullmatch(
                    data["finished_at"]
                )
                for request in data.get("requests", []):
                    assert HEX64.fullmatch(request["body_sha256"])
                    for snippet in request.get("snippets", {}).values():
                        assert len(snippet.get("text", "")) <= 4000 and HEX64.fullmatch(
                            snippet.get("sha256", "")
                        )
                for checks_key in ("answer_checks", "stdout_checks", "proxy_checks"):
                    for check in data.get(checks_key, {}).values():
                        assert set(check) >= {"hit", "excerpt"}
                        assert check["excerpt"] is None or len(check["excerpt"]) <= 240

            def walk(value: Any):
                if isinstance(value, dict):
                    assert not forbidden_keys.intersection(value), (
                        f"raw content key in {path}"
                    )
                    for child in value.values():
                        walk(child)
                elif isinstance(value, list):
                    for child in value:
                        walk(child)

            walk(data)


def test_opspilot_side_and_q7_are_derived_consistently():
    record = records()["upstream-2356-failed-source"]
    side = record["opspilot_side"]
    assert side["stimulus"]["base_scenario"] == "dv2-2b214b"
    assert set(side["groups"]) == {"prometheus", "jaeger"}
    assert set(side["holmes"]) == {"prometheus", "jaeger"}
    for group, value in side["groups"].items():
        assert value["n"] == len(value["runs"])
        for run in value["runs"]:
            summary = EVIDENCE / "upstream-2356-failed-source" / run["summary"]
            assert (
                summary.is_file()
                and hashlib.sha256(summary.read_bytes()).hexdigest()
                == run["summary_sha256"]
            )
            assert HEX64.fullmatch(run["first_request_sha256"])
            assert isinstance(run["first_request_mentions_unreachable"], bool)
            assert isinstance(run["report_names_missing_source"], bool)
    q7 = side["q7"]
    assert q7["decided_by"] == "agent_S8"
    assert q7["n1_triggered"] == (q7["input_gap"] and q7["report_gap"])
    hp = side["holmes"]["prometheus"]
    assert hp["k_input"] == sum(
        1
        for _ in hp["input_runs"]
        if (EVIDENCE / "upstream-2356-failed-source" / _).is_file()
    )
    assert hp["k_report"] == sum(
        1
        for _ in hp["report_runs"]
        if (EVIDENCE / "upstream-2356-failed-source" / _).is_file()
    )
    assert len(side["groups"]["prometheus"]["runs"]) == 3
    assert len(hp["report_runs"]) == 3
    assert len(side["groups"]["jaeger"]["runs"]) == 1
    assert len(side["holmes"]["jaeger"]["report_runs"]) == 1


def test_mapping_writeback_only_changes_allowed_rows_and_binds_records():
    current = load(MAPPING)
    origin_text = subprocess.check_output(
        ["git", "show", "origin/main:docs/evidence/f14-upstream-mapping/mapping.json"],
        text=True,
    )
    origin = json.loads(origin_text)
    assert (
        current["schema"] == origin["schema"]
        and current["upstream"] == origin["upstream"]
    )
    assert current.get("opspilot_commit") == origin.get("opspilot_commit")
    assert current.get("contract") == origin.get("contract")
    current_by_key = {_row_key(row): row for row in current["rows"]}
    origin_by_key = {_row_key(row): row for row in origin["rows"]}
    assert set(current_by_key) == set(origin_by_key)
    candidate_slugs = {
        Path(row["candidate_defect"]["step3_ref"].rstrip("/")).name
        for row in origin["rows"]
        if "candidate_defect" in row
    }
    assert candidate_slugs == {
        "max-output-token-reservation",
        "raw-alert-in-prompt",
        "rollout-baseline-in-memory",
    }
    assert all(slug in records() for slug in candidate_slugs)
    for key, old in origin_by_key.items():
        new = current_by_key[key]
        if key not in TARGET_ROWS:
            assert new == old, f"unrelated mapping row changed: {key}"
    assert not any("candidate_defect" in row for row in current["rows"])
    recs = records()
    for slug, record in recs.items():
        record_path = f"docs/evidence/f14-upstream-issues/{slug}/record.json"
        for mapped in record["mapping_rows"]:
            row = current_by_key[_row_key(mapped)]
            if _row_key(mapped) in TARGET_ROWS and _row_key(mapped) != (
                "F3",
                4,
                "空结果被报成错误（NO_DATA 分支走不到）",
            ):
                assert row.get("step3_record") == record_path
            if row["class"] == "confirmed_defect" and row.get("step3_record"):
                assert row["step3_record"] == record_path
                assert record_path in row["runtime_evidence"]
                assert (
                    "docs/evidence/f14-holmes-baseline/pin.json"
                    in row["runtime_evidence"]
                )
    for row in current["rows"]:
        if row["class"] == "confirmed_defect" and row.get("step3_record"):
            assert (
                "docs/evidence/f14-holmes-baseline/pin.json" in row["runtime_evidence"]
            )


def test_upstream_manifest_and_human_page_are_complete():
    manifest = load(UPSTREAM_FILES)
    assert manifest.get("schema") == "opspilot.f14.upstream-files.v1"
    assert HEX40.fullmatch(manifest.get("commit", "")) and HEX40.fullmatch(
        manifest.get("tree", "")
    )
    files = manifest.get("files")
    assert isinstance(files, dict) and files
    cited = set()
    for record in records().values():
        for ref in record["code_at_pin"]:
            cited.add(REF.fullmatch(ref).group("path"))
    assert set(files) == cited
    for path, item in files.items():
        assert (
            HEX40.fullmatch(item.get("blob", ""))
            and isinstance(item.get("lines"), int)
            and item["lines"] >= 0
        )
    text = PAGE.read_text()
    headings = [
        "## 选题",
        "## 方法",
        "## 结论",
        "## #2356 成对运行与 N1 判定",
        "## mapping.json 回写",
        "## 运行与费用",
        "## 可向上游报告的条目",
        "## 怎么核对",
        "## 未确认",
    ]
    positions = [text.index(h) for h in headings]
    assert positions == sorted(positions)
    for slug in SLUGS:
        assert f"{slug}/record.json" in text
    assert "f14-upstream-issues.md" in (ROOT / "docs/README.md").read_text()


def test_baseline_files_are_byte_identical_to_origin_and_f14_stays_unpassed():
    baseline_files = [
        "pin.json",
        "request-check.json",
        "summary.json",
        "run1-tool-errors.json",
        "proxy-log-holmes.txt",
        "isolation-probe.json",
        "authz-probe.json",
    ]
    for name in baseline_files:
        path = ROOT / "docs/evidence/f14-holmes-baseline" / name
        expected = subprocess.check_output(
            ["git", "show", f"origin/main:docs/evidence/f14-holmes-baseline/{name}"]
        )
        assert path.read_bytes() == expected
    features = load(ROOT / "feature_list.json")
    assert (
        next(feature for feature in features if feature["id"] == "F14")["passes"]
        is False
    )


def test_capture_default_request_check_function_remains_unchanged():
    path = ROOT / "scripts/holmes_baseline/capture.py"
    current = ast.parse(path.read_text())
    origin = ast.parse(
        subprocess.check_output(
            ["git", "show", "origin/main:scripts/holmes_baseline/capture.py"], text=True
        )
    )

    def function(tree: ast.AST, name: str) -> ast.AST:
        return next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        )

    assert ast.dump(
        function(current, "request_check"), include_attributes=False
    ) == ast.dump(function(origin, "request_check"), include_attributes=False)

    def assigned_literal(tree: ast.AST, name: str) -> Any:
        node = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == name
                for target in node.targets
            )
        )
        return ast.literal_eval(node.value)

    assert assigned_literal(current, "REPLY") == assigned_literal(origin, "REPLY")


def test_new_evidence_has_no_obvious_secrets():
    for path in EVIDENCE.rglob("*"):
        if not path.is_file() or path.name == "upstream-files.json":
            continue
        text = path.read_text(errors="replace")
        assert "Authorization: Basic" not in text
        assert not re.search(r"\bsk-[A-Za-z0-9]{20,}\b", text)
