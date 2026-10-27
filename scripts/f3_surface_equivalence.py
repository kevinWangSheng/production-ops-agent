"""F3 第 5 步两次真实 Run 在 v11（#319）下的模型可见面等价证明。

#319 只扩大共享脱敏规则（JSON 转义写法的 URL authority），把
``PROJECTION_REVISION`` 从 v10 升到 v11、otel-demo 工具 schema 哈希随之变化。
F3 第 5 步的两次真实 Run 在 v10 下运行。若 Run 的原始输入里有新规则会改写的
形态，v10 的模型视图会原样保留它（v10 规则不认）；所以「模型实际看到的每条
工具结果在当前规则与 v10 规则下输出相同」加「新规则零匹配」就说明 v11 会投影
出同样的字节。

做法：从 LangSmith 读出每次 Run 的完整 trace，按调用顺序取每次 LLM 调用的
全部 ``role=tool`` 消息（后一次调用的工具消息以前一次为前缀，取并集）。对每条
消息分别用当前规则和固定提交 ``V10_RULES_COMMIT`` 的 ``opspilot/redaction.py``
跑 ``redact_credentials(text)`` 与 ``redact_json_bytes(text)``（视图是 JSON，镜像
执行路径），两版输出不同即计入 ``changed_by_current_redaction``；新规则在原文与
JSON 解码后的每个字符串（键与值）上都必须零匹配。另记幂等改写数（当前规则对
消息本身的改写，v10 下同样存在）以示透明。冻结文件只写计数与哈希，不写消息
原文（ADR-0006）。

trace 导出把单条属性截到 20000 字符（``opspilot/tracing.py`` ``_MAX_ATTR_CHARS``），
被截的工具消息只检查到截断处；``--db-extract`` 用 Run 的 DB 抽取（证据行的完整
``view`` 与步骤里的工具结果）对同一判据交叉核对。DB 抽取不入库。

限制：
- 不覆盖被 v10 视图截断隐藏的匹配：截断前的原始数据不在 trace 里。
- DB 抽取只有原始源数据的哈希与字节数，没有原始字节，所以也不能补查上一条。

用法（需 M0_ENV_FILE 提供 LANGSMITH_*）：
  M0_ENV_FILE=... .venv/bin/python scripts/f3_surface_equivalence.py \\
    -o docs/evidence/f3-step5-competing-cause/v11-equivalence.json \\
    --db-extract run-1=<path> --db-extract run-2=<path>
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator, Sequence
from hashlib import sha256
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opspilot import redaction as current  # noqa: E402
from opspilot.redaction import _ESCAPED_AUTHORITY_USERINFO  # noqa: E402
from opspilot.tools.otel_demo import otel_demo_versions  # noqa: E402
from opspilot.tools.outcomes import PROJECTION_REVISION  # noqa: E402

SCHEMA = "f3-surface-equivalence-v1"
LLM_RUN = "deepseek.chat.completions"
TRUNCATED = "...[truncated]"
#: main before #319 (the v10 projection): the rules F3 step 5 ran under.
V10_RULES_COMMIT = "c968783b92d2d5bfa560cfe027c38cb7b330a194"
CHANGE = {
    "key": "tool_schema_revision",
    "from": "otel-demo-68ed2ca8f5d6",
    "to": "otel-demo-a3a95a0f888e",
    "projection_from": "m1-01-tool-view-v10",
    "projection_to": "m1-01-tool-view-v11",
    "cause": "#319",
}
IDEMPOTENCE_NOTE = (
    "gateway RESULT_TOO_LARGE refusal view: max_view_tokens/view_tokens keys "
    "match the credential-name rule on re-redaction; not source data, "
    "identical under v10 and v11"
)
#: F3 step-5 Runs and their LangSmith root runs (docs/evidence/f3-acceptance-live).
RUNS = {
    "run-1": (
        "dbf02dca-b980-5f66-b9e7-5c62b200f411",
        "https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/"
        "projects/p/fddc0e84-a2c2-4978-8098-649395630411/"
        "r/00000000-0000-0000-3f27-476a5c84f334?poll=true",
    ),
    "run-2": (
        "1a9b37f1-3bfd-5a04-aa6f-0ad3b8f83530",
        "https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/"
        "projects/p/fddc0e84-a2c2-4978-8098-649395630411/"
        "r/00000000-0000-0000-936e-285b738d4089?poll=true",
    ),
}

Rules = Callable[[str], tuple[str, bytes]]


def rules_of(module: ModuleType) -> Rules:
    """Both execution-path redactions of a text under one rule module."""

    def apply(text: str) -> tuple[str, bytes]:
        return module.redact_credentials(text), module.redact_json_bytes(
            text.encode("utf-8")
        )

    return apply


def v10_rules(commit: str = V10_RULES_COMMIT) -> Rules:
    """``opspilot/redaction.py`` at ``commit``, loaded as its own module."""
    source = subprocess.run(
        ["git", "show", f"{commit}:opspilot/redaction.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "redaction_v10.py"
        path.write_bytes(source)
        spec = importlib.util.spec_from_file_location("redaction_v10", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return rules_of(module)


def tool_messages(calls: Sequence[Sequence[dict[str, Any]]]) -> list[str]:
    """Every distinct tool-result message, in order, across the LLM calls.

    ``calls`` are the prompt message lists in call order. Each call's tool
    messages must extend the previous call's (the transcript only grows);
    anything else raises, since the union would then be ambiguous.
    """
    seen: list[str] = []
    for messages in calls:
        tools = [
            str(m.get("content") or "") for m in messages if m.get("role") == "tool"
        ]
        if tools[: len(seen)] != seen:
            raise ValueError("tool messages are not a growing prefix across calls")
        seen = tools
    return seen


def _strings(value: Any) -> Iterator[str]:
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def escaped_matches(text: str) -> int:
    """New-rule matches in the text and in every JSON-decoded string of it."""
    count = len(_ESCAPED_AUTHORITY_USERINFO.findall(text))
    try:
        decoded = json.loads(text)
    except ValueError:
        return count
    return count + sum(
        len(_ESCAPED_AUTHORITY_USERINFO.findall(s)) for s in _strings(decoded)
    )


def rewritten(text: str, rules: Rules) -> bool:
    """Whether ``rules`` rewrite this model-visible text (idempotence)."""
    credentials, json_bytes = rules(text)
    return credentials != text or json_bytes != text.encode("utf-8")


def check(
    messages: Iterable[str], *, now: Rules, before: Rules
) -> dict[str, int | str]:
    """Counts and digest for one Run's tool messages (no contents)."""
    items = list(messages)
    return {
        "tool_messages": len(items),
        "tool_message_bytes": sum(len(m.encode("utf-8")) for m in items),
        "tool_messages_sha256": sha256("\n".join(items).encode("utf-8")).hexdigest(),
        "changed_by_current_redaction": sum(now(m) != before(m) for m in items),
        "escaped_authority_matches": sum(escaped_matches(m) for m in items),
        "idempotence_changes_pre_existing": sum(rewritten(m, now) for m in items),
        "trace_truncated_messages": sum(m.endswith(TRUNCATED) for m in items),
    }


def extract_texts(extract: dict[str, Any]) -> list[str]:
    """Full evidence views and step tool results of a Run's DB extract."""
    return [
        json.dumps(row["view"], ensure_ascii=False)
        for row in extract["raw"]["evidence"]
    ] + [
        json.dumps(result, ensure_ascii=False)
        for step in extract["raw"]["steps"]
        for result in (step.get("tool_results") or [])
    ]


def fetch_calls(client: Any, trace_url: str) -> list[list[dict[str, Any]]]:
    """Prompt messages of every LLM call of the trace, in call order."""
    path = urlsplit(trace_url).path.split("/")
    project_id, root_id = path[path.index("p") + 1], path[path.index("r") + 1]
    root = client.read_run(root_id)
    rows = list(client.list_runs(project_id=project_id, trace_id=root.trace_id))
    llm = [r for r in rows if r.name == LLM_RUN]
    llm.sort(key=lambda r: int((r.extra or {})["metadata"]["model_call_seq"]))
    for row in llm:
        omitted = (row.extra or {})["metadata"].get(
            "otel.span.opspilot.model.prompt_messages_omitted", 0
        )
        if omitted:
            raise ValueError("a traced prompt omits messages")
    return [list((r.inputs or {})["messages"]) for r in llm]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument(
        "--db-extract", action="append", default=[], help="run-N=<db-extract.json>"
    )
    args = parser.parse_args()
    extracts = dict(item.partition("=")[::2] for item in args.db_extract)
    if set(extracts) != set(RUNS):
        parser.error("--db-extract is required for every run")

    from scripts import lab_evidence
    from scripts.m1_live_flash_loop import load_langsmith_env, resolve_env_file

    load_langsmith_env(resolve_env_file())
    client = lab_evidence.langsmith_client()
    now, before = rules_of(current), v10_rules()
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "change": CHANGE,
        "computed_with": {
            "tool_schema_revision": otel_demo_versions()["tool_schema_revision"],
            "projection_revision": PROJECTION_REVISION,
            "v10_rules_commit": V10_RULES_COMMIT,
        },
        "runs": {},
    }
    for name, (run_id, url) in RUNS.items():
        calls = fetch_calls(client, url)
        counts = check(tool_messages(calls), now=now, before=before)
        texts = extract_texts(json.loads(Path(extracts[name]).read_text()))
        result["runs"][name] = {
            "run_id": run_id,
            "trace_url": url,
            "llm_calls": len(calls),
            **counts,
            "idempotence_change_note": IDEMPOTENCE_NOTE,
            "db_extract_texts_checked": len(texts),
            "db_extract_v10_v11_diff": sum(now(t) != before(t) for t in texts),
            "db_extract_escaped_authority_matches": sum(
                escaped_matches(t) for t in texts
            ),
        }
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({n: r for n, r in result["runs"].items()}, ensure_ascii=False))
    clean = all(
        run[key] == 0
        for run in result["runs"].values()
        for key in (
            "changed_by_current_redaction",
            "escaped_authority_matches",
            "db_extract_v10_v11_diff",
            "db_extract_escaped_authority_matches",
        )
    )
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
