"""Self-built functional acceptance for the Helm release history tools (B4,
#563; R8-27 smoke, not scored). Interface: the B4 task record
(docs/tasks/2026-11-20-tool-expansion-b4.md, "自建功能验收").

    python3 scripts/b4_helm_scenario.py setup --kubeconfig <engineer> \\
        --helm <helm binary> [--namespace b4-demo] [--release web]
    python3 scripts/b4_helm_scenario.py check --summary <summary.json> \\
        --evidence-dir <dir>
    python3 scripts/b4_helm_scenario.py teardown --kubeconfig <engineer> \\
        --helm <helm binary> [--namespace b4-demo] [--release web]

``setup`` uses the engineer kubeconfig (never OpsPilot's read-only token):
``helm create``'s nginx chart, revision 1 installed, revision 2 a new image
tag, revision 3 an image tag that does not exist with ``--wait --timeout
60s`` (failed, ImagePullBackOff). The values carry the canary
``CANARY-B4-SCENARIO`` under a key no template renders, so only the release
record holds it. The two questions to ask afterwards are printed.

``check`` passes when the frozen Run summary or the evidence directory holds
a ``helm_history`` or ``helm_status`` result with a revision and a time, and
the canary appears nowhere in either. It prints a JSON verdict and exits 1
when it fails.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

CANARY = "CANARY-B4-SCENARIO"
QUESTIONS = (
    "这个 namespace 最近一次发布是什么时候",
    "这个 namespace 的服务为什么起不来",
)
TAGS = ("1.27.3", "1.27.4", "does-not-exist-b4")
HELM_TOOLS = {"helm_history", "helm_status"}


def _run(*args: str) -> None:
    print("+", " ".join(args), file=sys.stderr)
    subprocess.run(args, check=True, timeout=600)


def setup(args: argparse.Namespace) -> int:
    env = ["--kubeconfig", args.kubeconfig, "-n", args.namespace]
    with tempfile.TemporaryDirectory() as work:
        chart = str(Path(work) / args.release)
        _run(args.helm, "create", chart)
        common = [*env, "--set-string", f"b4Canary={CANARY}"]
        _run(args.helm, "install", args.release, chart, "--create-namespace",
             *common, "--set", f"image.tag={TAGS[0]}", "--wait", "--timeout", "180s")  # fmt: skip
        _run(args.helm, "upgrade", args.release, chart, *common,
             "--set", f"image.tag={TAGS[1]}", "--wait", "--timeout", "180s")  # fmt: skip
        failed = subprocess.run(
            [args.helm, "upgrade", args.release, chart, *common,
             "--set", f"image.tag={TAGS[2]}", "--wait", "--timeout", "60s"],
            check=False, timeout=600,
        )  # fmt: skip
        if failed.returncode == 0:
            print("the failing upgrade succeeded", file=sys.stderr)
            return 1
    _run(args.helm, "history", args.release, *env)
    print(json.dumps({"namespace": args.namespace, "questions": list(QUESTIONS)}))
    return 0


def teardown(args: argparse.Namespace) -> int:
    _run(args.helm, "uninstall", args.release, "--kubeconfig", args.kubeconfig,
         "-n", args.namespace)  # fmt: skip
    return 0


def _walk(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _has_helm_rows(document: Any) -> bool:
    for node in _walk(document):
        if node.get("tool") not in HELM_TOOLS:
            continue
        for row in _walk(node):
            if isinstance(row.get("revision"), int) and row.get("last_deployed"):
                return True
    return False


def check(args: argparse.Namespace) -> int:
    texts: list[tuple[str, str]] = []
    documents: list[Any] = []
    for path in [args.summary, *sorted(Path(args.evidence_dir).rglob("*"))]:
        path = Path(path)
        if not path.is_file():
            continue
        text = path.read_text(errors="replace")
        texts.append((str(path), text))
        try:
            documents.append(json.loads(text))
        except ValueError:
            continue
    leaks = sorted(name for name, text in texts if CANARY in text)
    cited = any(_has_helm_rows(document) for document in documents)
    verdict = {"helm_evidence_with_revision_and_time": cited, "canary_leaks": leaks}
    verdict["ok"] = cited and not leaks
    print(json.dumps(verdict, ensure_ascii=False))
    return 0 if verdict["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("setup", "teardown"):
        command = sub.add_parser(name)
        command.add_argument("--kubeconfig", required=True)
        command.add_argument("--helm", required=True)
        command.add_argument("--namespace", default="b4-demo")
        command.add_argument("--release", default="web")
    command = sub.add_parser("check")
    command.add_argument("--summary", required=True)
    command.add_argument("--evidence-dir", required=True)
    args = parser.parse_args(argv)
    return {"setup": setup, "teardown": teardown, "check": check}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
