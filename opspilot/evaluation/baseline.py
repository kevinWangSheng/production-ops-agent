"""Render a newly verified round, never a trusted aggregate cache."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from opspilot.evaluation.storage import ArtifactRef, Resolver, _publish, load_round


def _text(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _rate(value: float | None) -> str:
    return "null" if value is None else f"{value:.2%}"


def write_baseline(
    round_path: Path, *, resolve: Resolver, destination: Path
) -> ArtifactRef:
    result = load_round(round_path, resolve=resolve)
    planned: Counter[str] = Counter()
    for row in result.rows:
        planned[row.mode] += row.planned
    lines = [
        "# OpsPilot 开发基线",
        "",
        f"轮次：{_text(result.round_id)}；manifest：{result.manifest_hash}",
        f"baseline_complete：{str(result.baseline_complete).lower()}",
        "incomplete_reasons：" + (", ".join(result.incomplete_reasons) or "无"),
        "",
        f"D5：实际计划（合计）：回放{planned['replay']}次、live{planned['live']}次、"
        f"structural{planned['structural']}次；样本量有限，置信不足。",
        "replay_miss 计失败并计入分母；structural 质量率为 null。",
        "",
        "| scenario | version | mode | planned | attempted | not_started | passed/attempted | failed | 质量率 | replay_miss/attempted | miss率 | 失败分类 |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in result.rows:
        classes = (
            ", ".join(f"{k}:{v}" for k, v in sorted(row.failure_classes.items()))
            or "无"
        )
        values = (
            _text(row.scenario_id),
            _text(row.scenario_version),
            row.mode,
            str(row.planned),
            str(row.attempted),
            str(row.not_started),
            f"{row.passed}/{row.attempted}",
            str(row.failed),
            _rate(row.pass_rate),
            f"{row.replay_miss}/{row.attempted}",
            _rate(row.replay_miss_rate),
            classes,
        )
        lines.append("| " + " | ".join(values) + " |")
    return _publish(
        destination, ("\n".join(lines) + "\n").encode("utf-8"), "text/markdown"
    )
