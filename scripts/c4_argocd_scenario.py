"""Self-built functional acceptance for the ArgoCD Application tools (C4,
#568; R8-27 smoke, not scored). Interface: the C4 task record
(docs/tasks/2026-11-20-tool-expansion-c4.md, "自建功能验收").

    python3 scripts/c4_argocd_scenario.py setup --kubeconfig <engineer> \\
        [--kubectl kubectl]
    python3 scripts/c4_argocd_scenario.py check --summary <summary.json> \\
        --evidence-dir <dir>
    python3 scripts/c4_argocd_scenario.py teardown --kubeconfig <engineer> \\
        [--kubectl kubectl]

``setup`` uses the engineer kubeconfig (never OpsPilot's read-only token). It
downloads ArgoCD v3.6.1 ``core-install.yaml``, refuses it unless the sha256
matches, pins every ``quay.io/argoproj/argocd`` image to the recorded digest,
installs it into a labelled ``argocd`` namespace and waits for the
controllers. It then creates the ``guestbook`` Application (automated sync
into the labelled namespace ``c4-demo``), syncs it at two target revisions
(two history entries) and finally points it at the missing path
``guestbook-missing`` (ComparisonError, Unknown/OutOfSync). ``helm.values``
carries the canary ``CANARY-C4-SCENARIO``. The ArgoCD overlay ClusterRole for
OpsPilot's read-only identity is applied. The question to ask is printed.

``check`` passes when the frozen Run summary or the evidence directory holds
an ``argocd_app_get`` or ``argocd_app_history`` result, the ArgoCD evidence
carries a sync status and at least two history rows with a revision and a
deploy time, and the canary appears nowhere in either. It prints a JSON
verdict and exits 1 when it fails.

``teardown`` deletes only what setup built, by name after checking the
scenario label: the ``guestbook`` Application, the ``c4-demo`` namespace, the
objects of the pinned core manifest and the ``argocd`` namespace. The overlay
ClusterRole stays (other lab runs use it; applying it again is idempotent).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

CANARY = "CANARY-C4-SCENARIO"
QUESTION = "guestbook 应用现在的同步状态是什么，最近几次同步是什么时候"
CORE_URL = "https://raw.githubusercontent.com/argoproj/argo-cd/v3.6.1/manifests/core-install.yaml"
CORE_SHA256 = "0b38189d6e1fd76ef5c3db10355026c48a02020d82bf21a1a1b5ab16dffe4f3c"
IMAGE_TAG = "quay.io/argoproj/argocd:v3.6.1"
IMAGE_PINNED = (
    "quay.io/argoproj/argocd:v3.6.1"
    "@sha256:296e3d74f449b2ce45265613fcb68c18ed7366488360c8ea32f5f2a93d2e5c54"
)
REPO_URL = "https://github.com/argoproj/argocd-example-apps"
# ``guestbook`` is a plain directory; a ``helm`` block makes ArgoCD treat the
# source as Helm (ApplicationSource.ExplicitType, v3.6.1), so the canary in
# ``helm.values`` needs the chart twin ``helm-guestbook`` (present at both
# revisions).
APP_PATH = "helm-guestbook"
MISSING_PATH = "guestbook-missing"
REVISIONS = (
    "e27374f5ea0afcf87ffcf1c9c377797862437312",
    "8088f4c0d970abb09e250248cc97e35623447cb5",
)
APP = "guestbook"
ARGOCD_NS = "argocd"
DEMO_NS = "c4-demo"
LABEL_KEY = "app.kubernetes.io/managed-by"
LABEL_VALUE = "c4-argocd-scenario"
OVERLAY = (
    Path(__file__).resolve().parent / "kind_lab" / "opspilot-readonly-rbac-argocd.yaml"
)
ARGOCD_TOOLS = {"argocd_app_get", "argocd_app_history"}


def render_core_manifest(raw: bytes) -> str:
    digest = hashlib.sha256(raw).hexdigest()
    if digest != CORE_SHA256:
        raise SystemExit(f"core-install.yaml sha256 {digest} != {CORE_SHA256}")
    text = raw.decode("utf-8")
    if IMAGE_TAG not in text:
        raise SystemExit(f"{IMAGE_TAG} not found in core-install.yaml")
    return text.replace(IMAGE_TAG, IMAGE_PINNED)


def _download_core() -> str:
    with urllib.request.urlopen(CORE_URL, timeout=120) as response:  # noqa: S310
        return render_core_manifest(response.read())


def application(path: str, revision: str) -> dict[str, Any]:
    return {
        "apiVersion": "argoproj.io/v1alpha1",
        "kind": "Application",
        "metadata": {
            "name": APP,
            "namespace": ARGOCD_NS,
            "labels": {LABEL_KEY: LABEL_VALUE},
        },
        "spec": {
            "project": "default",
            "source": {
                "repoURL": REPO_URL,
                "path": path,
                "targetRevision": revision,
                "helm": {"values": f"c4Canary: {CANARY}\n"},
            },
            "destination": {
                "server": "https://kubernetes.default.svc",
                "namespace": DEMO_NS,
            },
            "syncPolicy": {"automated": {}},
        },
    }


def namespace(name: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": name, "labels": {LABEL_KEY: LABEL_VALUE}},
    }


class Kubectl:
    def __init__(self, binary: str, kubeconfig: str) -> None:
        self.base = [binary, "--kubeconfig", kubeconfig]

    def run(self, *args: str, stdin: str | None = None, check: bool = True) -> str:
        print("+", " ".join(["kubectl", *args]), file=sys.stderr)
        done = subprocess.run(
            [*self.base, *args], input=stdin, capture_output=True, text=True,
            check=False, timeout=900,
        )  # fmt: skip
        if check and done.returncode != 0:
            raise SystemExit(f"kubectl {' '.join(args)} failed: {done.stderr.strip()}")
        return done.stdout

    def apply(self, manifest: dict[str, Any]) -> None:
        self.run("apply", "-f", "-", stdin=json.dumps(manifest))

    def label(self, kind: str, name: str, ns: str | None = None) -> str | None:
        """The scenario label of the object, ``None`` when it does not exist."""
        args = [
            "get",
            kind,
            name,
            "-o",
            "json",
            "--ignore-not-found",
            *(["-n", ns] if ns else []),
        ]
        done = subprocess.run([*self.base, *args], capture_output=True, text=True,
                              check=False, timeout=120)  # fmt: skip
        if done.returncode != 0:
            if "the server doesn't have a resource type" in done.stderr:
                return None  # the Application CRD is gone, so is the Application
            raise SystemExit(f"kubectl {' '.join(args)} failed: {done.stderr.strip()}")
        out = done.stdout
        if not out.strip():
            return None
        labels = json.loads(out).get("metadata", {}).get("labels") or {}
        return str(labels.get(LABEL_KEY, ""))

    def app(self) -> dict[str, Any]:
        out = self.run(
            "get", "applications.argoproj.io", APP, "-n", ARGOCD_NS, "-o", "json"
        )
        app: dict[str, Any] = json.loads(out)
        return app


def _owned_or_absent(
    kubectl: Kubectl, kind: str, name: str, ns: str | None = None
) -> bool:
    return kubectl.label(kind, name, ns) in (None, LABEL_VALUE)


def _wait(what: str, ready: Any, timeout: float = 600.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready():
            print(f"ready: {what}", file=sys.stderr)
            return
        time.sleep(5)
    raise SystemExit(f"timed out waiting for {what}")


def _synced_at(app: dict[str, Any], revision: str) -> bool:
    status = app.get("status") or {}
    history = status.get("history") or []
    return (status.get("sync") or {}).get("status") == "Synced" and any(
        row.get("revision") == revision for row in history
    )


def _comparison_error(app: dict[str, Any]) -> bool:
    conditions = (app.get("status") or {}).get("conditions") or []
    return any(c.get("type") == "ComparisonError" for c in conditions)


def setup(args: argparse.Namespace) -> int:
    kubectl = Kubectl(args.kubectl, args.kubeconfig)
    for ns in (ARGOCD_NS, DEMO_NS):
        if not _owned_or_absent(kubectl, "namespace", ns):
            print(
                f"namespace {ns} exists without {LABEL_KEY}={LABEL_VALUE}",
                file=sys.stderr,
            )
            return 1
    core = _download_core()
    kubectl.apply(namespace(ARGOCD_NS))
    kubectl.apply(namespace(DEMO_NS))
    with tempfile.TemporaryDirectory() as work:
        rendered = Path(work) / "core-install.yaml"
        rendered.write_text(core)
        kubectl.run("apply", "--server-side", "-n", ARGOCD_NS, "-f", str(rendered))
    kubectl.run("wait", "--for=condition=Established", "--timeout=180s",
                "crd/applications.argoproj.io")  # fmt: skip
    kubectl.run("wait", "--for=condition=Available", "--timeout=600s", "deployment", "--all",
                "-n", ARGOCD_NS)  # fmt: skip
    kubectl.run("rollout", "status", "--timeout=600s", "statefulset/argocd-application-controller",
                "-n", ARGOCD_NS)  # fmt: skip
    kubectl.run("apply", "-f", str(OVERLAY))
    for revision in REVISIONS:
        kubectl.apply(application(APP_PATH, revision))
        _wait(f"sync at {revision}", lambda r=revision: _synced_at(kubectl.app(), r))
    kubectl.apply(application(MISSING_PATH, REVISIONS[-1]))
    _wait("ComparisonError", lambda: _comparison_error(kubectl.app()))
    status = kubectl.app().get("status") or {}
    print(json.dumps({
        "application": f"{ARGOCD_NS}/{APP}",
        "sync": (status.get("sync") or {}).get("status"),
        "health": (status.get("health") or {}).get("status"),
        "history": [row.get("revision") for row in status.get("history") or []],
        "question": QUESTION,
    }, ensure_ascii=False))  # fmt: skip
    return 0


def teardown(args: argparse.Namespace) -> int:
    kubectl = Kubectl(args.kubectl, args.kubeconfig)
    targets = [("applications.argoproj.io", APP, ARGOCD_NS), ("namespace", DEMO_NS, None),
               ("namespace", ARGOCD_NS, None)]  # fmt: skip
    foreign = [name for kind, name, ns in targets
               if kubectl.label(kind, name, ns) not in (None, LABEL_VALUE)]  # fmt: skip
    if foreign:
        print(f"not built by this script, left alone: {foreign}", file=sys.stderr)
        return 1
    kubectl.run("delete", "applications.argoproj.io", APP, "-n", ARGOCD_NS,
                "--ignore-not-found", "--timeout=300s")  # fmt: skip
    kubectl.run("delete", "namespace", DEMO_NS, "--ignore-not-found", "--timeout=300s")
    with tempfile.TemporaryDirectory() as work:
        rendered = Path(work) / "core-install.yaml"
        rendered.write_text(_download_core())
        kubectl.run("delete", "-n", ARGOCD_NS, "-f", str(rendered), "--ignore-not-found",
                    "--timeout=300s")  # fmt: skip
    kubectl.run(
        "delete", "namespace", ARGOCD_NS, "--ignore-not-found", "--timeout=300s"
    )
    return 0


def _walk(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _argocd_nodes(document: Any) -> Iterator[dict[str, Any]]:
    for node in _walk(document):
        if node.get("tool") in ARGOCD_TOOLS:
            yield node


def _has_sync_status(node: dict[str, Any]) -> bool:
    for item in _walk(node):
        sync = item.get("sync")
        if isinstance(sync, dict) and sync.get("status"):
            return True
    return False


def _history_rows(node: dict[str, Any]) -> set[int]:
    ids = set()
    for row in _walk(node):
        has_revision = bool(row.get("revision")) or bool(row.get("revisions"))
        if isinstance(row.get("id"), int) and has_revision and row.get("deployed_at"):
            ids.add(row["id"])
    return ids


def verdict(documents: list[Any], texts: list[tuple[str, str]]) -> dict[str, Any]:
    nodes = [node for document in documents for node in _argocd_nodes(document)]
    history: set[int] = set()
    for node in nodes:
        history |= _history_rows(node)
    result: dict[str, Any] = {
        "argocd_evidence_cited": bool(nodes),
        "sync_status": any(_has_sync_status(node) for node in nodes),
        "history_rows_with_revision_and_time": len(history),
        "canary_leaks": sorted(name for name, text in texts if CANARY in text),
    }
    result["ok"] = (
        result["argocd_evidence_cited"]
        and result["sync_status"]
        and len(history) >= 2
        and not result["canary_leaks"]
    )
    return result


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
    result = verdict(documents, texts)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("setup", "teardown"):
        command = sub.add_parser(name)
        command.add_argument("--kubeconfig", required=True)
        command.add_argument("--kubectl", default="kubectl")
    command = sub.add_parser("check")
    command.add_argument("--summary", required=True)
    command.add_argument("--evidence-dir", required=True)
    args = parser.parse_args(argv)
    return {"setup": setup, "teardown": teardown, "check": check}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
