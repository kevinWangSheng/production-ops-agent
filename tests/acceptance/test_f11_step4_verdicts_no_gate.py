"""F11 step 4: "Verify outputs distinguish healthy, degraded and unknown; no
result changes a required CI check, deployment gate, rollout or traffic
policy".

Independent acceptance tests for ``docs/tasks/2026-10-24-f11-pr5-contract.md``
section 4, step 4, items 1-5 (A10 / D5 projection, D2 control refusals,
D8 ``release_outcome_from_durable``, the no-gate code surface, A11).

U-5 was revised 2026-11-13 for PR #588 after the user's 2026-11-12 X1/Q1/Q2
decisions.  The product may now hold one read-only Kubernetes credential in
the A2 extension profile, and model-written jq may run in its bounded child
process.  The code-surface proof therefore requires: Kubernetes access is
confined to ``opspilot/tools/kubernetes_core.py``; that transport is GET-only;
its only child-process entry is ``run_isolated`` with one ``Popen(env={})``
using the resolved jq executable;
the module has no ``kubectl`` client and no GitHub/GitLab/CI/forge client; and
the other product modules retain their original forbidden-client checks.  A2's
contract test records that web face/version construction works without
``OPSPILOT_K8S_*`` variables, while worker configuration is the only place
that reads them.  Item 4 still rejects KUBECONFIG, service-account token paths,
CI/forge credentials and credential files for every product entry point.

PostgreSQL tests use ``tests/acceptance/f11_steps_support.py``; the code
surface tests need no database.
"""

from __future__ import annotations

import ast
import json
import os
import re
import socket
import subprocess
import sys
import threading
import typing
from collections.abc import Callable
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest

from opspilot.acceptance import IncidentScenario
from opspilot.acceptance_release import release_outcome_from_durable
from opspilot.observation.releases import ReleaseStore
from opspilot.observer import __main__ as observer_main
from tests.acceptance import f11_steps_support as s
from tests.acceptance.test_f7_step1_entry_points import MUTATION_WORDS
from tests.f11_release_support import pods_for

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "opspilot"

#: The three long-running product processes (C3; F7 PROCESS_ENTRIES minus
#: the schema and replay tools, which an operator runs by hand).
PRODUCT_ENTRIES = (
    "opspilot.web.__main__",
    "opspilot.worker_main",
    "opspilot.observer.__main__",
)
RELEASE_CONTROLS = frozenset({"pause", "resume", "cancel"})
NOT_CONTROLS = ("approve", "block", "rollback", "promote", "deploy")


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Any:
    if os.environ.get("M1_DURABLE_POSTGRES") != "1":
        pytest.skip("explicit PG opt-in required: M1_DURABLE_POSTGRES=1")
    with s.module_env(Path(tmp_path_factory.mktemp("f11-step4"))) as built:
        yield built


# ===========================================================================
# item 1: one projection, three places
# ===========================================================================


def _scenario(oid: UUID) -> IncidentScenario:
    return IncidentScenario(
        scenario_id=f"f11-step4-{oid}",
        feature_id="F11",
        acceptance_step="4",
        kind="release",
        subject_id=str(oid),
    )


def _release_in(env: s.Env, status: str) -> s.Delivered:
    """One release whose stored status is ``status``, reached through the
    product paths only."""
    if status == "pending":
        return s.deliver(env, calibrate=False)
    if status == "observing":
        return s.observing(env)[0]
    if status == "healthy":
        case, sid = s.observing(env, released_ago=2500.0)
        authorized_at = case.record.released_at + timedelta(seconds=100)
        env.set_authorized_at(sid, authorized_at)
        pods = pods_for(s.AFTER_IMAGES)
        for offset in range(300, 2101, 300):
            at = authorized_at + timedelta(seconds=offset)
            if s.submit(env, sid, at, pods).release_status != "observing":
                break
        return case
    if status == "anomalous":
        regression = s.regress(env)
        env.anomaly_pass()
        return regression.case
    if status == "unknown":
        case = s.deliver(env, released_ago=s.DEADLINE - 4.0)
        env.authorize_pass()
        env.wait_past(case.record.deadline_at)
        env.tick(s.ReleaseWorld())
        return case
    if status == "cancelled":
        case = s.observing(env)[0]
        response = s.post_control(env.app(), case.oid, "cancel", 0)
        assert response.status == 200, response.text
        return case
    raise AssertionError(status)


def _list_rows(document: Any) -> list[dict[str, Any]]:
    """The list JSON is a list of rows, or an object holding one (D6 fixes
    the content, not the envelope)."""
    if isinstance(document, list):
        return document
    (rows,) = [v for v in document.values() if isinstance(v, list)]
    return list(rows)


@pytest.mark.parametrize("status", sorted(s.LABELS))
def test_item1_html_json_and_the_acceptance_projection_agree(
    env: s.Env, status: str
) -> None:
    case = _release_in(env, status)
    assert env.record(case.oid).status == status
    label = s.LABELS[status]
    app = env.app()

    document = s.get_json(app, s.detail_path(case.oid))
    listed = [
        row
        for row in _list_rows(s.get_json(app, s.LIST))
        if row.get("observation_id") == str(case.oid)
    ]
    page = s.get_html(app, s.detail_path(case.oid))
    outcome = release_outcome_from_durable(_scenario(case.oid), env.reader, case.oid)

    assert outcome.stored_status == status
    assert outcome.verdict == document["verdict"] == label
    assert [row["verdict"] for row in listed] == [label]
    assert label in page
    concluded = label in {"healthy", "degraded", "unknown"}
    assert outcome.concluded is concluded
    assert document["concluded"] is concluded
    if status == "unknown":
        record = env.record(case.oid)
        assert record.ended_reason
        assert record.ended_reason in outcome.handoff_reasons
        assert record.ended_reason in s.strings(document)
        assert record.ended_reason in page
    if status == "cancelled":
        assert outcome.verdict != "unknown"


def test_item1_a_stored_anomalous_is_never_shown_as_anomalous(env: s.Env) -> None:
    case = _release_in(env, "anomalous")
    app = env.app()
    document = s.get_json(app, s.detail_path(case.oid))

    assert document["verdict"] == "degraded"
    assert env.record(case.oid).status == "anomalous"  # storage keeps it (A10)


# ===========================================================================
# item 2: HTTP controls
# ===========================================================================


def _row_state(env: s.Env, oid: UUID) -> tuple[Any, ...]:
    record = env.record(oid)
    return (
        record.status,
        record.paused,
        record.control_generation,
        record.observation_generation,
        len(env.audit(oid)),
        len(env.sessions(oid)),
    )


def test_item2_a_stale_generation_is_a_conflict_and_changes_nothing(
    env: s.Env,
) -> None:
    case, _ = s.observing(env)
    app = env.app()
    generation = env.record(case.oid).control_generation
    assert s.post_control(app, case.oid, "pause", generation).status == 200
    before = _row_state(env, case.oid)

    response = s.post_control(app, case.oid, "resume", generation)  # stale

    s.refused(response, 409, "CONTROL_CONFLICT")
    record = env.record(case.oid)
    assert (record.status, record.paused) == ("observing", True)
    assert _row_state(env, case.oid)[:4] == before[:4]


def test_item2_the_same_key_replays_and_another_request_with_it_conflicts(
    env: s.Env,
) -> None:
    case, _ = s.observing(env)
    app = env.app()
    key = f"f11s-replay-{uuid4().hex}"
    first = s.post_control(app, case.oid, "pause", 0, key=key)
    assert first.status == 200, first.text
    after_first = _row_state(env, case.oid)

    replay = s.post_control(app, case.oid, "pause", 0, key=key)
    other = s.post_control(app, case.oid, "cancel", 1, key=key)

    assert replay.status == 200, replay.text
    for field in ("audit_id", "outcome", "status", "control_generation"):
        if field in first.json():
            assert replay.json()[field] == first.json()[field], field
    s.refused(other, 409, "IDEMPOTENCY_CONFLICT")
    assert _row_state(env, case.oid) == after_first


def test_item2_a_terminal_release_refuses_every_control(env: s.Env) -> None:
    case, _ = s.observing(env)
    app = env.app()
    assert s.post_control(app, case.oid, "cancel", 0).status == 200
    record = env.record(case.oid)
    assert record.status == "cancelled"

    for action in sorted(RELEASE_CONTROLS):
        response = s.post_control(app, case.oid, action, record.control_generation)
        s.refused(response, 409, "RELEASE_TERMINAL")
    assert env.record(case.oid).status == "cancelled"


@pytest.mark.parametrize("origin", ["missing", "cross-site"])
def test_item2_a_control_without_a_same_origin_is_refused_and_writes_nothing(
    env: s.Env, origin: str
) -> None:
    case, _ = s.observing(env)
    before = _row_state(env, case.oid)
    headers = {} if origin == "missing" else {"origin": "https://evil.example.test"}

    response = s.post_control(
        env.app(), case.oid, "pause", 0, origin=False, headers=headers
    )

    assert response.status in (400, 403), (response.status, response.text[:300])
    assert _row_state(env, case.oid) == before


@pytest.mark.parametrize("action", NOT_CONTROLS)
def test_item2_a_release_gate_action_is_invalid_input_and_writes_nothing(
    env: s.Env, action: str
) -> None:
    case, _ = s.observing(env)
    before = _row_state(env, case.oid)

    response = s.post_control(env.app(), case.oid, action, 0)

    s.refused(response, 400, "INVALID_INPUT")
    assert _row_state(env, case.oid) == before


def test_item2_cancel_ends_the_observation_and_nothing_samples_it_again(
    env: s.Env,
) -> None:
    case, sid = s.observing(env)
    response = s.post_control(env.app(), case.oid, "cancel", 0)
    assert response.status == 200, response.text
    world = s.ReleaseWorld()
    world.answer(env, sid, pods_for(s.AFTER_IMAGES))
    samples = env.samples(case.oid)

    for _ in range(2):
        env.make_all_due(case.oid)
        env.tick(world)
        env.authorize_pass()

    assert env.record(case.oid).status == "cancelled"
    assert env.samples(case.oid) == samples
    assert world.sample_calls == []
    assert env.authorized(case.oid) == []


# ===========================================================================
# item 3: code surface (no database)
# ===========================================================================


def _sources() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def _trees() -> list[tuple[Path, ast.AST]]:
    return [(p, ast.parse(p.read_text(), filename=str(p))) for p in _sources()]


def test_item3_no_web_route_names_a_release_gate_or_target_mutation() -> None:
    from opspilot.web import create_app
    from tests.acceptance.test_f7_step1_entry_points import (
        _auth,
        _routes,
        _Untouchable,
    )

    routes = _routes(
        create_app(_Untouchable("workbench"), _auth(), _Untouchable("clock"))
    )
    assert ("POST", "/version-changes/{observation_id}/control") in routes
    for _, path in routes:
        for word in MUTATION_WORDS:
            assert word not in path.lower(), (path, word)


def test_item3_the_release_control_action_set_is_exactly_pause_resume_cancel() -> None:
    hints = typing.get_type_hints(ReleaseStore.control_release)
    assert set(typing.get_args(hints["action"])) == RELEASE_CONTROLS


#: Kubernetes, GitHub / forge and CI client libraries.
FORBIDDEN_IMPORT_ROOTS = frozenset(
    {
        "kubernetes",
        "kubernetes_asyncio",
        "kr8s",
        "pykube",
        "lightkube",
        "openshift",
        "github",
        "ghapi",
        "gitlab",
        "jenkins",
        "jenkinsapi",
        "circleci",
        "buildkite",
        "travispy",
        "argo",
        "argocd",
        "argo_workflows",
        "hera",
        "flagger",
    }
)
#: CI / forge / cluster API endpoints and the kubectl-style CLIs.
FORBIDDEN_TEXT = re.compile(
    r"api\.github\.com|/repos/[^/]+/[^/]+/(?:check-runs|statuses|actions)"
    r"|gitlab\.com/api|circleci\.com/api|api\.buildkite\.com|/apis/apps/v1"
    r"|/api/v1/namespaces|kubernetes\.default|\bkubectl\b|\bhelm\b|\bargocd\b"
    r"|\bgh\s+(?:pr|api|workflow|run)\b"
)
KUBERNETES_CORE_RELATIVE = "opspilot/tools/kubernetes_core.py"
KUBERNETES_READ_API_PATH = re.compile(
    r"^/api/v1(?:/[^\s\"']*)?$|^/apis/[A-Za-z0-9_.-]+/v1(?:/[^\s\"']*)?$"
)


def _imported(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Name, ast.Attribute))
            and (
                getattr(node.func, "id", None) == "__import__"
                or getattr(node.func, "attr", None) == "import_module"
            )
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names.append(node.args[0].value)
    return names


def test_item3_no_product_module_imports_a_kubernetes_forge_or_ci_client() -> None:
    offenders = [
        (str(path.relative_to(REPO)), name)
        for path, tree in _trees()
        for name in _imported(tree)
        if name.split(".")[0] in FORBIDDEN_IMPORT_ROOTS
    ]
    assert offenders == []


def test_item3_no_product_module_names_kubectl_or_a_cluster_ci_or_forge_api() -> None:
    def forbidden(path: Path, value: str) -> str | None:
        relative = str(path.relative_to(REPO))
        for match in FORBIDDEN_TEXT.finditer(value):
            if (
                relative == KUBERNETES_CORE_RELATIVE
                and KUBERNETES_READ_API_PATH.fullmatch(match.group())
            ):
                continue
            return match.group()
        return None

    offenders = [
        (str(path.relative_to(REPO)), node.value[:80])
        for path, tree in _trees()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and forbidden(path, node.value) is not None
    ]
    assert offenders == []


#: ``opspilot/schema.py`` is the migration tool (``make migrate``), not a
#: product process; it shells out to ``pg_dump`` only (ambiguity note E2).
#: A2's Kubernetes module is the one bounded exception for jq's child process.
SUBPROCESS_ALLOWED = {"opspilot/schema.py"}
PROCESS_CALLS = {
    ("subprocess", name)
    for name in ("run", "Popen", "call", "check_call", "check_output", "getoutput")
} | {
    ("os", name)
    for name in (
        "system",
        "popen",
        "execv",
        "execve",
        "execvp",
        "execl",
        "spawnv",
        "spawnl",
        "posix_spawn",
        "posix_spawnp",
    )
}


def test_item3_only_migration_or_bounded_kubernetes_jq_starts_processes() -> None:
    offenders: list[tuple[str, str]] = []
    kubernetes_tree: ast.Module | None = None
    kubernetes_path = PACKAGE / "tools" / "kubernetes_core.py"

    def function_parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        return parents

    def enclosing_function(
        node: ast.AST, parents: dict[ast.AST, ast.AST]
    ) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        current = parents.get(node)
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return current
            current = parents.get(current)
        return None

    for path, tree in _trees():
        relative = str(path.relative_to(REPO))
        if relative == KUBERNETES_CORE_RELATIVE:
            kubernetes_tree = tree  # type: ignore[assignment]

        subprocess_aliases = {"subprocess"}
        os_aliases = {"os"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "subprocess":
                        if alias.asname is not None:
                            offenders.append((relative, "subprocess alias"))
                        else:
                            subprocess_aliases.add("subprocess")
                    if alias.name == "os" and alias.asname is not None:
                        os_aliases.add(alias.asname)
                    if alias.name.split(".")[0] in {"pty", "subprocess"}:
                        if (
                            relative not in SUBPROCESS_ALLOWED
                            and relative != KUBERNETES_CORE_RELATIVE
                        ):
                            offenders.append((relative, "imports a process module"))
            elif isinstance(node, ast.ImportFrom):
                if (
                    node.module
                    and node.module.split(".")[0] in {"pty", "subprocess"}
                    and relative not in SUBPROCESS_ALLOWED
                ):
                    offenders.append((relative, "from process module"))
                if node.module == "os":
                    bad = {a.name for a in node.names} & {n for _, n in PROCESS_CALLS}
                    if bad:
                        offenders.append((relative, f"from os import {sorted(bad)}"))
            if isinstance(node, ast.Call):
                function = node.func
                dynamic_process_import = (
                    isinstance(function, ast.Name)
                    and function.id == "__import__"
                ) or (
                    isinstance(function, ast.Attribute)
                    and function.attr == "import_module"
                )
                if dynamic_process_import and relative == KUBERNETES_CORE_RELATIVE:
                    offenders.append((relative, "dynamic process import"))

                if (
                    isinstance(function, ast.Attribute)
                    and isinstance(function.value, ast.Name)
                    and function.value.id in subprocess_aliases
                ):
                    pair = ("subprocess", function.attr)
                    if relative == KUBERNETES_CORE_RELATIVE:
                        if function.attr != "Popen":
                            offenders.append((relative, f"subprocess.{function.attr}"))
                        else:
                            parents = function_parents(tree)
                            owner = enclosing_function(node, parents)
                            if owner is None or owner.name != "run_isolated":
                                offenders.append((relative, "Popen outside run_isolated"))
                            env_keywords = [
                                keyword
                                for keyword in node.keywords
                                if keyword.arg == "env"
                            ]
                            if len(env_keywords) != 1 or not (
                                isinstance(env_keywords[0].value, ast.Dict)
                                and not env_keywords[0].value.keys
                                and not env_keywords[0].value.values
                            ):
                                offenders.append((relative, "Popen env is not {}"))
                            if any(keyword.arg == "shell" for keyword in node.keywords):
                                offenders.append((relative, "Popen shell keyword"))
                    elif relative not in SUBPROCESS_ALLOWED:
                        offenders.append((relative, f"{pair[0]}.{pair[1]}"))

                if (
                    isinstance(function, ast.Attribute)
                    and isinstance(function.value, ast.Name)
                    and function.value.id in os_aliases
                    and ("os", function.attr) in PROCESS_CALLS
                    and relative not in SUBPROCESS_ALLOWED
                ):
                    offenders.append((relative, f"os.{function.attr}"))

        if relative == KUBERNETES_CORE_RELATIVE:
            assert isinstance(tree, ast.Module)
            run_isolated = [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "run_isolated"
            ]
            require_jq = [
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "require_jq"
            ]
            if len(run_isolated) != 1:
                offenders.append((relative, "run_isolated function count"))
            if len(require_jq) != 1:
                offenders.append((relative, "require_jq function count"))
            if require_jq:
                jq_checks = [
                    node
                    for node in ast.walk(require_jq[0])
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "shutil"
                    and node.func.attr == "which"
                    and len(node.args) == 1
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "jq"
                ]
                if not jq_checks:
                    offenders.append((relative, "require_jq does not resolve jq"))

            popen_calls = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
                and node.func.attr == "Popen"
            ]
            if len(popen_calls) != 1:
                offenders.append((relative, "Popen call count"))

            forbidden_literals = {"/bin/sh", "sh", "bash", "-c"}
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute):
                    if (
                        isinstance(node.value, ast.Name)
                        and node.value.id == "sys"
                        and node.attr == "executable"
                    ):
                        offenders.append((relative, "sys.executable"))
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and (node.value in forbidden_literals or node.value == "sys.executable")
                ):
                    offenders.append((relative, f"forbidden process literal {node.value!r}"))

    all_run_isolated_calls = []
    for path, tree in _trees():
        relative = str(path.relative_to(REPO))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not (
                isinstance(function, ast.Name)
                and function.id == "run_isolated"
                or isinstance(function, ast.Attribute)
                and function.attr == "run_isolated"
            ):
                continue
            all_run_isolated_calls.append((relative, node))
            if not node.args:
                offenders.append((relative, "run_isolated has no argv"))
                continue
            argv = node.args[0]
            if isinstance(argv, (ast.List, ast.Tuple)) and argv.elts:
                first = argv.elts[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    offenders.append((relative, "run_isolated literal argv[0]"))
                if (
                    isinstance(first, ast.Attribute)
                    and isinstance(first.value, ast.Name)
                    and first.value.id == "sys"
                    and first.attr == "executable"
                ):
                    offenders.append((relative, "run_isolated sys.executable argv[0]"))

    if kubernetes_tree is not None and not all_run_isolated_calls:
        offenders.append((KUBERNETES_CORE_RELATIVE, "run_isolated call missing"))

    assert offenders == []
    schema_text = (PACKAGE / "schema.py").read_text()
    assert "pg_dump" in schema_text
    assert not FORBIDDEN_TEXT.search(schema_text)
    kubernetes_path = PACKAGE / "tools" / "kubernetes_core.py"
    if kubernetes_path.is_file():
        assert "jq" in kubernetes_path.read_text()


def test_item3_kubernetes_transport_constructs_get_only_requests() -> None:
    """Static GET-only proof keeps this F11 gate hermetic.

    The A2 contract tests separately exercise the injected opener and assert
    every observed request method is GET.  This check guards the implementation
    surface even when no Kubernetes server or worker process is available in
    the F11 acceptance environment.
    """
    kubernetes_path = PACKAGE / "tools" / "kubernetes_core.py"
    if not kubernetes_path.is_file():
        return
    tree = ast.parse(kubernetes_path.read_text(), filename=str(kubernetes_path))
    request_calls: list[ast.Call] = []
    data_open_calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        is_request = isinstance(function, ast.Name) and function.id == "Request"
        is_request = is_request or (
            isinstance(function, ast.Attribute) and function.attr == "Request"
        )
        if is_request:
            request_calls.append(node)
        is_urlopen = isinstance(function, ast.Name) and function.id == "urlopen"
        is_urlopen = is_urlopen or (
            isinstance(function, ast.Attribute) and function.attr == "urlopen"
        )
        is_open = isinstance(function, ast.Attribute) and function.attr == "open"
        if is_urlopen or is_open:
            if len(node.args) >= 2 or any(
                keyword.arg == "data" for keyword in node.keywords
            ):
                data_open_calls.append(node)

    offenders: list[str] = []
    for node in request_calls:
        if len(node.args) >= 2:
            offenders.append("Request has a second positional argument")
        if any(keyword.arg == "data" for keyword in node.keywords):
            offenders.append("Request has data=")
        methods = [keyword for keyword in node.keywords if keyword.arg == "method"]
        if len(methods) != 1:
            offenders.append("Request method is missing or duplicated")
        elif not (
            isinstance(methods[0].value, ast.Constant)
            and methods[0].value.value == "GET"
        ):
            offenders.append("Request method is not literal GET")
    offenders.extend("open/urlopen receives data" for _ in data_open_calls)
    assert request_calls, "kubernetes_core.py has no Request construction"
    assert offenders == []


_NETWORK_ROOTS = {
    "socket",
    "http.client",
    "urllib.request",
    "requests",
    "httpx",
    "aiohttp",
    "urllib3",
    "websocket",
    "websockets",
    "grpc",
}


def _closure(module: str) -> list[str]:
    probe = (
        "import importlib, json, sys\n"
        f"importlib.import_module({module!r})\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return list(json.loads(done.stdout.strip().splitlines()[-1]))


def _module_path(name: str) -> Path | None:
    base = REPO / name.replace(".", "/")
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


_HEALTH_CLIENT_ROOTS = _NETWORK_ROOTS - {"socket"}
_HEALTH_CLIENT_CALLS = {
    "connect",
    "connect_ex",
    "create_connection",
    "urlopen",
    "HTTPConnection",
    "HTTPSConnection",
    "PoolManager",
    "ClientSession",
    "request",
}


def _health_network_offenders(tree: ast.AST) -> list[str]:
    offenders: list[str] = []
    for imported in _imported(tree):
        if any(
            imported == root or imported.startswith(root + ".")
            for root in _HEALTH_CLIENT_ROOTS
        ):
            offenders.append(f"import {imported}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else None
            )
            if called in _HEALTH_CLIENT_CALLS:
                offenders.append(f"call {called}()")
    return offenders


def test_item3_in_the_observer_process_only_the_prometheus_source_does_network_io() -> (
    None
):
    """Static: of the product modules the Observer process loads, the one
    that imports a network library is the Prometheus read-only source."""
    networking = []
    for name in _closure("opspilot.observer.__main__"):
        if not name.startswith("opspilot"):
            continue
        path = _module_path(name)
        if path is None:
            continue
        tree = ast.parse(path.read_text())
        if name == "opspilot.health":
            # F8-Q2: health is an inbound-only listener owned by each process;
            # socket is permitted for binding, while client I/O remains banned.
            assert _health_network_offenders(tree) == []
            continue
        imported = _imported(tree)
        if any(
            i == root or i.startswith(root + ".")
            for i in imported
            for root in _NETWORK_ROOTS
        ):
            networking.append(name)
    assert networking == ["opspilot.observer.prometheus"]


# ===========================================================================
# item 4: the three entry points read no cluster, CI or forge credential
# ===========================================================================

#: Environment names of cluster, CI or forge credentials (and their files).
FORBIDDEN_ENV = re.compile(
    r"KUBE|KUBERNETES|SERVICE_?ACCOUNT|GITHUB|^GH_|GITLAB|JENKINS|BUILDKITE"
    r"|CIRCLE|TRAVIS|^CI_|^CI$|ARGO|^ACTIONS_|^RUNNER_|^BUILD_|^DRONE"
)
ALLOWED_K8S_ENV = re.compile(r"^OPSPILOT_K8S_[A-Z0-9_]+$")
ALLOWED_K8S_LAB_CODE = "KUBERNETES_NOT_LAB"
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]+$")
CREDENTIAL_PATHS = re.compile(
    r"/var/run/secrets/kubernetes\.io|serviceaccount/token|\.kube/config"
    r"|\.config/gh/|\.git-credentials"
)


ENV_OWNERS = {"environ", "env", "environment", "os"}
#: OpsPilot's own variables, the model's, tracing's and libpq's (F7 U1-a).
ENV_PREFIXES = ("OPSPILOT_", "LANGSMITH_", "DEEPSEEK_", "PG")


def _env_reads(tree: ast.Module) -> set[str]:
    """Names this module reads from an environment mapping: keys of
    ``environ.get`` / ``os.getenv`` / ``env[...]`` and of helpers called as
    ``helper(env, NAME, ...)``, a key given as a literal or as a module-level
    string constant."""
    constants = {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    def text(key: ast.AST | None) -> str | None:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            return key.value
        if isinstance(key, ast.Name):
            return constants.get(key.id)
        return None

    def owner(node: ast.AST) -> str | None:
        return getattr(node, "attr", None) or getattr(node, "id", None)

    found: set[str] = set()
    for node in ast.walk(tree):
        key: ast.AST | None = None
        if isinstance(node, ast.Call) and node.args:
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in {"get", "getenv", "pop", "setdefault"}
                and owner(func.value) in ENV_OWNERS
            ):
                key = node.args[0]
            elif len(node.args) >= 2 and owner(node.args[0]) in ENV_OWNERS:
                key = node.args[1]
        elif isinstance(node, ast.Subscript) and owner(node.value) in ENV_OWNERS:
            key = node.slice
        name = text(key)
        if name is not None and ENV_NAME.match(name):
            found.add(name)
    return found


@pytest.mark.parametrize("entry", PRODUCT_ENTRIES)
def test_item4_an_entry_point_reads_no_cluster_ci_or_forge_credential(
    entry: str,
) -> None:
    """Static check of what the entry's configuration reads: every
    environment read in the product modules its process loads, and every
    environment-variable-shaped string or credential path they contain.  A2's
    web face/version mapping test covers the fact that web does not read the
    allowed ``OPSPILOT_K8S_*`` names at runtime; this test keeps the names
    allowed while rejecting credential paths and every other cluster/CI/forge
    name."""
    reads: dict[str, str] = {}
    names: dict[str, str] = {}
    paths: list[tuple[str, str]] = []
    for module in _closure(entry):
        if not module.startswith("opspilot"):
            continue
        path = _module_path(module)
        if path is None:
            continue
        tree = ast.parse(path.read_text())
        for name in _env_reads(tree):
            reads[name] = module
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if ENV_NAME.match(node.value):
                    names[node.value] = module
                if CREDENTIAL_PATHS.search(node.value):
                    paths.append((module, node.value[:80]))
    assert reads, f"{entry}: the scan found no environment read at all"
    assert {n: m for n, m in reads.items() if not n.startswith(ENV_PREFIXES)} == {}
    assert {
        n: m
        for n, m in {**names, **reads}.items()
        if FORBIDDEN_ENV.search(n)
        and not ALLOWED_K8S_ENV.fullmatch(n)
        and n != ALLOWED_K8S_LAB_CODE
    } == {}
    assert paths == []


# ===========================================================================
# item 3 (runtime): the Observer's outbound HTTP goes to Prometheus only
# ===========================================================================


class _Prometheus(BaseHTTPRequestHandler):
    requests: list[tuple[str, str]] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        type(self).requests.append(("GET", self.path))
        body = b'{"status":"success","data":{"resultType":"vector","result":[]}}'
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        type(self).requests.append(("POST", self.path))
        self.send_response(405)
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        return


def test_item3_the_observer_process_talks_to_its_prometheus_and_nothing_else(
    env: s.Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    case, sid = s.observing(env)
    env.set_authorized_at(sid, case.record.released_at + timedelta(seconds=100))
    env.make_due(sid)
    _Prometheus.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Prometheus)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = server.server_address[:2]
    connects: list[Any] = []
    original: Callable[..., Any] = socket.socket.connect

    def record(self: socket.socket, target: Any) -> Any:
        connects.append(target)
        return original(self, target)

    try:
        loop = observer_main.build_loop(
            {
                observer_main.DSN_VAR: env.observer_dsn,
                observer_main.PROMETHEUS_URL_VAR: f"http://{address[0]}:{address[1]}",
            },
            stop=threading.Event(),
            store=env.observer,
        )
        monkeypatch.setattr(socket.socket, "connect", record)
        loop.poll_once()
        monkeypatch.undo()
    finally:
        server.shutdown()
        server.server_close()

    assert _Prometheus.requests, "the Observer sampled nothing"
    assert {method for method, _ in _Prometheus.requests} == {"GET"}
    assert {path.split("?")[0] for _, path in _Prometheus.requests} == {"/api/v1/query"}
    assert connects and {tuple(c[:2]) for c in connects} == {tuple(address)}
    assert env.samples(case.oid) == 1


# ===========================================================================
# item 4 (permissions): runtime roles cannot rewrite what was observed
# ===========================================================================

IDENTITY_COLUMNS = (
    "source",
    "source_release_id",
    "released_at",
    "before_revision",
    "after_revision",
    "auxiliary",
    "target_id",
    "target_snapshot",
    "deadline_at",
)
CALIBRATION_COLUMNS = (
    "calibrated_policy_revision",
    "calibrated_policy_content",
    "calibrated_policy_sha256",
)


def _new_value(env: s.Env, column: str) -> str:
    kind = env.one(
        "SELECT data_type FROM information_schema.columns WHERE table_schema='public' "
        "AND table_name='opspilot_release_observations' AND column_name=%s",
        (column,),
    )["data_type"]
    return {
        "text": "'tampered'",
        "character varying": "'tampered'",
        "jsonb": "'{\"tampered\": true}'::jsonb",
        "json": "'{\"tampered\": true}'::json",
        "uuid": "gen_random_uuid()",
        "timestamp with time zone": "clock_timestamp() - interval '1 day'",
        "integer": "1",
        "bigint": "1",
    }[kind]


@pytest.mark.parametrize("role", ["web", "observer", "worker"])
def test_item4_no_runtime_role_can_rewrite_identity_versions_or_calibration(
    env: s.Env, role: str
) -> None:
    case, _ = s.observing(env)
    dsn = {"web": env.web_dsn, "observer": env.observer_dsn, "worker": env.worker_dsn}[
        role
    ]
    before = env.one(
        "SELECT * FROM opspilot_release_observations WHERE observation_id=%s",
        (case.oid,),
    )
    refused = {}
    for column in (*IDENTITY_COLUMNS, *CALIBRATION_COLUMNS):
        statement = (
            f"UPDATE opspilot_release_observations SET {column}="
            f"{_new_value(env, column)} WHERE observation_id=%s"
        )
        with psycopg.connect(dsn) as conn:
            try:
                conn.execute(statement, (case.oid,))
                conn.commit()
                refused[column] = None
            except psycopg.Error as exc:
                refused[column] = exc.sqlstate
    assert refused == {c: "42501" for c in refused}, refused
    after = env.one(
        "SELECT * FROM opspilot_release_observations WHERE observation_id=%s",
        (case.oid,),
    )
    assert after == before


def test_item4_the_worker_role_has_no_access_to_release_tables(env: s.Env) -> None:
    for table in (
        "opspilot_release_observations",
        "opspilot_release_audit",
        "opspilot_release_incident_links",
        "opspilot_release_comparisons",
    ):
        with psycopg.connect(env.worker_dsn) as conn:
            with pytest.raises(psycopg.Error) as raised:
                conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
        assert raised.value.sqlstate == "42501", table


# ===========================================================================
# item 5: global suspension stops observing; lifting it does not resume
# ===========================================================================


def test_item5_global_suspension_stops_queries_and_lifting_it_does_not_resume(
    env: s.Env,
) -> None:
    case, sid = s.observing(env)
    env.set_authorized_at(sid, case.record.released_at + timedelta(seconds=100))
    world = s.ReleaseWorld()
    world.answer(env, sid, pods_for(s.AFTER_IMAGES))
    generation = env.owner.suspension_state()["global"]["generation"]
    suspended = env.owner.set_global_suspension(
        True, expected_generation=generation, actor="test:f11-step4"
    )
    try:
        for _ in range(2):
            env.make_all_due(case.oid)
            env.tick(world)
            env.authorize_pass()
        assert world.sample_calls == []
        assert env.samples(case.oid) == 0
    finally:
        env.owner.set_global_suspension(
            False, expected_generation=suspended, actor="test:f11-step4"
        )
    audit = env.audit(case.oid)
    sessions = env.sessions(case.oid)

    for _ in range(2):
        env.make_all_due(case.oid)
        env.tick(world)
        env.authorize_pass()

    # no sample, no new session, no new authorization or resume; the old
    # session may be retired (it was authorized under the old generation)
    assert world.sample_calls == []
    assert env.samples(case.oid) == 0
    assert env.audit(case.oid) == audit
    assert [x["session_id"] for x in env.sessions(case.oid)] == [
        x["session_id"] for x in sessions
    ]
    assert env.authorized(case.oid) == []
    # the operator still sees it and can resume it by hand (C3:114)
    document = s.get_json(env.app(), s.detail_path(case.oid))
    assert "resume" in s.strings(document)
