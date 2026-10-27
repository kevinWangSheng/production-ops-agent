"""F7 step 1 (offline part): every enabled entry point is read-only toward targets.

``feature_list.json`` F7 step 1: "Attempt target mutations and release-gate
changes through all enabled entry points and verify external authorization
denies them". Contract sources:

* ``docs/tasks/2026-10-15-f7-contract.md`` S1a (gateway refuses writes), S1b
  (no target-mutation or release-gate route on any enabled entry point), S1e /
  U1-a (no Kubernetes data source or credential: #92 does not apply), R5
  (table-driven enumeration of the web routes and the registry);
* ``docs/tasks/2026-10-16-f7-authz-audit.md`` A5: "enabled entry points" = all
  web routes, the ``web``/``worker``/``observer``/``schema``/``replay``
  process entries and the tool registry;
* C3 section 8 (tool gateway: no shell/SQL/code execution, Kubernetes only
  get/list/watch/logs) and section 9 (release observation offers no approve
  or block operation).

What this module does NOT cover (other evidence owns it): the external
PostgreSQL role denials (S1c, ``tests/integration/test_runtime_roles_postgres.py``)
and the Prometheus read-only proxy probe on kind (S1d/S1f,
``scripts/f7_authz_probe.py`` evidence). Written against public interfaces
only; the implementation under change in PR-C is not read.
"""

from __future__ import annotations

import ast
import copy
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from opspilot.tools import (
    FORBIDDEN_VERBS,
    MAX_REQUEST_TIMEOUT_SECONDS,
    MAX_RESULT_BYTES,
    READ_ONLY_VERBS,
    ToolContractError,
)
from opspilot.web import AuthConfig, Authenticator, create_app, hash_password
from opspilot.web.service import CONTROL_ACTIONS
from tests.m1_tool_support import build, registration
from tests.m1_web_support import (
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
    build_workbench,
    call,
    post_form,
    same_origin,
)

PRODUCT_CONSTRAINT_SCENARIOS = ("F7:readonly",)

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "opspilot"
UI = {**basic(), **same_origin(), "accept": "application/json"}

# -- S1b: the complete web surface -------------------------------------------

#: Every route the workbench serves, with the OpsPilot record each write
#: route acts on. A new route makes this test fail until it is classified.
EXPECTED_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/"): "read: incident list",
    ("GET", "/controls"): "read: suspension state",
    ("POST", "/controls/suspension"): "write: OpsPilot investigation suspension",
    ("GET", "/incidents/{incident_id}"): "read: incident page",
    ("GET", "/postmortems/{postmortem_id}/versions/{version}"): "read: postmortem",
    ("GET", "/knowledge/{entry_id}"): "read: knowledge entry",
    (
        "POST",
        "/postmortems/{postmortem_id}/versions/{version}/review",
    ): "write: OpsPilot postmortem review",
    ("POST", "/knowledge/{entry_id}/revoke"): "write: OpsPilot knowledge revocation",
    ("POST", "/intake/ui"): "write: OpsPilot incident intake",
    ("POST", "/intake/events"): "write: OpsPilot event intake",
    ("POST", "/intake/alertmanager"): "write: OpsPilot alert intake",
    ("POST", "/intake/version-changes"): "write: OpsPilot release observation intake",
    ("POST", "/incidents/{incident_id}/control"): "write: OpsPilot incident control",
    ("GET", "/incidents/{incident_id}/events"): "read: progress stream",
    ("GET", "/incidents/{incident_id}/evidence/{evidence_id}"): "read: evidence",
    # F11 PR-5a (pre-review D1), registered by the independent test author.
    ("GET", "/version-changes"): "read: release observation list",
    ("GET", "/version-changes/{observation_id}"): "read: release observation page",
    (
        "POST",
        "/version-changes/{observation_id}/control",
    ): "write: OpsPilot release observation control",
}

#: Words that would name a target mutation or a release gate (C3 sections 8, 9).
MUTATION_WORDS = (
    "approve-release",
    "block",
    "deploy",
    "rollback",
    "rollout",
    "restart",
    "scale",
    "exec",
    "patch",
    "kubectl",
    "promote",
    "gate",
    "release",
    "target",
)

#: Human control actions: decisions on OpsPilot's own incident record only.
EXPECTED_CONTROL_ACTIONS = frozenset(
    {
        "follow_up",
        "correct",
        "cancel",
        "pause",
        "resume",
        "new_run",
        "register_remediation",
        "takeover",
        "close",
        "reopen",
        "rebind_target",
    }
)

TARGET_MUTATION_ACTIONS = (
    "restart",
    "rollback",
    "rollout_restart",
    "deploy",
    "scale",
    "delete",
    "exec",
    "approve_release",
    "block_release",
    "approve",
    "block",
)


class _Untouchable:
    """A dependency that must not be reached: any attribute access fails."""

    def __init__(self, name: str) -> None:
        object.__setattr__(self, "_name", name)

    def __getattr__(self, item: str) -> Any:
        raise AssertionError(f"{self._name}.{item} was reached")


def _auth() -> Authenticator:
    return Authenticator(
        AuthConfig(
            ui_users={UI_USER: hash_password(UI_PASSWORD, salt=b"fixed-salt-16byt")},
            event_tokens={},
            auth_revision="f7-step1",
            allowed_origins=frozenset({ORIGIN}),
        )
    )


def _routes(app: Any) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for route in app.routes:
        methods = getattr(route, "methods", None)
        assert methods, f"route without methods (mount/websocket?): {route!r}"
        for method in methods:
            if method == "HEAD":
                continue
            found.add((method, route.path))
    return found


def test_s1b_web_routes_equal_the_classified_allow_list():
    app = create_app(_Untouchable("workbench"), _auth(), _Untouchable("clock"))

    assert _routes(app) == set(EXPECTED_ROUTES)
    # No mounted sub-application or schema surface could add routes unseen.
    assert all(type(r).__name__ == "APIRoute" for r in app.routes), app.routes
    assert app.openapi_url is None and app.docs_url is None


def test_s1b_the_production_web_entry_builds_the_same_app():
    """``python -m opspilot.web serve`` builds its app with ``create_app``
    and adds nothing: no mount, router or extra route registration."""
    tree = ast.parse((PACKAGE / "web" / "__main__.py").read_text())
    calls = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Attribute, ast.Name))
    }
    assert "create_app" in calls
    assert not calls & {
        "mount",
        "include_router",
        "add_route",
        "add_api_route",
        "api_route",
        "route",
    }


def test_s1b_no_route_names_a_target_mutation_or_release_gate():
    for (method, path), role in EXPECTED_ROUTES.items():
        lowered = path.lower()
        for word in MUTATION_WORDS:
            assert word not in lowered, (method, path)
        if method != "GET":
            assert role.startswith("write: OpsPilot "), (method, path, role)


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("POST", "/targets/checkout/restart", 404),
        ("POST", "/targets/checkout/rollback", 404),
        ("POST", "/targets/checkout/scale", 404),
        ("POST", "/releases/r-1/approve", 404),
        ("POST", "/releases/r-1/block", 404),
        ("POST", "/releases/r-1/rollback", 404),
        ("POST", "/deployments/checkout/rollout", 404),
        ("POST", "/api/v1/namespaces/otel-demo/pods/x/exec", 404),
        ("POST", "/openapi.json", 404),
        ("GET", "/docs", 404),
        ("DELETE", "/incidents/00000000-0000-0000-0000-000000000001", 405),
        ("PUT", "/incidents/00000000-0000-0000-0000-000000000001/control", 405),
        ("PATCH", "/controls/suspension", 405),
        ("DELETE", "/knowledge/00000000-0000-0000-0000-000000000001", 405),
    ],
)
def test_s1b_target_mutation_and_release_gate_requests_have_no_route(
    method: str, path: str, status: int
):
    app, workbench, _ = build_workbench()
    before = _state(workbench)

    response = call(app, method, path, headers=UI)

    assert response.status == status, (method, path, response.text)
    assert _state(workbench) == before


def _state(workbench: Any) -> Any:
    incidents = workbench.incidents
    return copy.deepcopy(
        {
            "incidents": incidents.incidents,
            "runs": incidents.runs,
            "controls": incidents.controls,
            "inputs": incidents.inputs,
        }
    )


def test_s1b_control_actions_are_decisions_on_opspilot_records_only():
    assert CONTROL_ACTIONS == EXPECTED_CONTROL_ACTIONS


@pytest.mark.parametrize("action", TARGET_MUTATION_ACTIONS)
def test_s1b_the_control_route_refuses_target_mutation_actions(action: str):
    app, workbench, _ = build_workbench()
    submitted = post_form(
        app,
        "/intake/ui",
        {
            "target_id": "checkout-prod",
            "question": "Why is checkout erroring?",
            "idempotency_key": f"f7-step1-{action}",
        },
        headers=UI,
    )
    assert submitted.status == 201, submitted.text
    incident = submitted.json()["incident_id"]
    before = _state(workbench)

    response = post_form(
        app,
        f"/incidents/{incident}/control",
        {"action": action, "expected_generation": "0", "idempotency_key": action},
        headers=UI,
    )

    assert response.status == 400, response.text
    assert response.json()["code"] == "INVALID_ACTION"
    assert _state(workbench) == before


@pytest.mark.parametrize("scope", ["release", "cluster", "deployment", "namespace"])
def test_s1b_suspension_scopes_are_opspilot_scopes_only(scope: str):
    app, workbench, _ = build_workbench()
    before = _state(workbench)

    response = post_form(
        app,
        "/controls/suspension",
        {
            "scope": scope,
            "target_id": "checkout-prod",
            "suspended": "true",
            "expected_generation": "0",
            "idempotency_key": f"f7-step1-{scope}",
        },
        headers=UI,
    )

    assert response.status == 400, response.text
    assert response.json()["code"] == "INVALID_INPUT"
    assert _state(workbench) == before


@pytest.mark.parametrize(
    "action", ["approve_release", "block", "rollback", "deploy", "restart"]
)
def test_s1b_the_review_route_takes_only_postmortem_review_actions(action: str):
    """C3 section 9: ``approve`` on this route is a postmortem review of an
    OpsPilot record; no release approval or block action exists. An unknown
    action is refused before the review service is reached."""
    app = create_app(
        _Untouchable("workbench"),
        _auth(),
        _Untouchable("clock"),
        review=_Untouchable("review"),
    )
    response = post_form(
        app,
        "/postmortems/00000000-0000-0000-0000-000000000001/versions/1/review",
        {"action": action, "expected_generation": "0", "idempotency_key": "k"},
        headers=UI,
    )
    assert response.status == 400, response.text
    assert response.json()["code"] == "INVALID_INPUT"
    assert response.json()["fields"] == ["action"]


# -- S1e / U1-a: process entries and the product package -----------------------

#: Process entries (A5): web, worker, observer, schema, replay.
PROCESS_ENTRIES = (
    "opspilot.web.__main__",
    "opspilot.worker_main",
    "opspilot.observer.__main__",
    "opspilot.schema",
    "opspilot.observer.replay",
)

#: Client libraries for Kubernetes, GitHub and Argo (and close relatives).
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
        "argo",
        "argocd",
        "argo_workflows",
        "hera",
    }
)

#: Credential locations and variables for those systems.
FORBIDDEN_TEXT = re.compile(
    r"KUBECONFIG|kubeconfig|\.kube/|serviceaccount/token|KUBERNETES_SERVICE_HOST"
    r"|GITHUB_TOKEN|GH_TOKEN|GITHUB_PAT|GH_ENTERPRISE_TOKEN|api\.github\.com"
    r"|ARGOCD_|ARGO_TOKEN|ARGO_SERVER"
)

ENV_PREFIXES = ("OPSPILOT_", "LANGSMITH_", "DEEPSEEK_", "PG")


def _sources() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def test_u1a_every_process_entry_exists():
    for module in PROCESS_ENTRIES:
        path = PACKAGE.parent / (module.replace(".", "/") + ".py")
        assert path.is_file(), module


def test_u1a_no_product_module_imports_a_kubernetes_github_or_argo_client():
    offenders = []
    for path in _sources():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
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
                names = [node.args[0].value]
            for name in names:
                if name.split(".")[0] in FORBIDDEN_IMPORT_ROOTS:
                    offenders.append((str(path.relative_to(REPO)), name))
    assert offenders == []


def test_u1a_no_product_module_names_a_kubeconfig_or_forge_token():
    offenders = []
    for path in _sources():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if FORBIDDEN_TEXT.search(node.value):
                    offenders.append((str(path.relative_to(REPO)), node.value[:80]))
    assert offenders == []


def _env_names(tree: ast.AST) -> set[str]:
    """String keys handed to ``environ.get``/``getenv``/``env.get``/``[...]``."""
    names: set[str] = set()
    for node in ast.walk(tree):
        key = None
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "getenv", "pop", "setdefault"}
            and node.args
        ):
            owner = node.func.value
            owner_name = getattr(owner, "attr", None) or getattr(owner, "id", None)
            if owner_name in {"environ", "env", "os", "environment"}:
                key = node.args[0]
        elif isinstance(node, ast.Subscript):
            owner = node.value
            owner_name = getattr(owner, "attr", None) or getattr(owner, "id", None)
            if owner_name in {"environ", "env"}:
                key = node.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            names.add(key.value)
    return names


def test_u1a_environment_reads_are_opspilot_model_trace_or_postgres_only():
    names: dict[str, str] = {}
    for path in _sources():
        tree = ast.parse(path.read_text(), filename=str(path))
        for name in _env_names(tree):
            names[name] = str(path.relative_to(REPO))
    assert names, "the scan found no environment reads at all"
    outside = {n: p for n, p in names.items() if not n.startswith(ENV_PREFIXES)}
    assert outside == {}
    assert not [n for n in names if FORBIDDEN_TEXT.search(n)]


@pytest.mark.parametrize("module", PROCESS_ENTRIES)
def test_u1a_importing_a_process_entry_loads_no_cluster_or_forge_client(module: str):
    """The transitive import closure of each process entry, in a fresh
    interpreter: what the process would actually have loaded at startup."""
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
    loaded = json.loads(done.stdout.strip().splitlines()[-1])
    assert any(name.startswith("opspilot") for name in loaded)
    assert (
        sorted(name for name in loaded if name.split(".")[0] in FORBIDDEN_IMPORT_ROOTS)
        == []
    )


# -- S1a: the tool registry and executor ---------------------------------------
# These restate, at the acceptance level, the gateway assertions that
# ``tests/test_m1_tool_boundaries.py`` already makes (S1a evidence in the
# contract): registration-time refusal of write verbs and a single read-only
# executor entry point.


@pytest.mark.parametrize(
    "verb",
    sorted(FORBIDDEN_VERBS),
)
def test_s1a_the_registry_refuses_every_write_verb(verb: str):
    with pytest.raises(ToolContractError, match="WRITE_CAPABILITY_FORBIDDEN"):
        registration(verb=verb)


@pytest.mark.parametrize("verb", ["approve", "deploy", "block", "post", "put"])
def test_s1a_the_registry_refuses_any_verb_outside_the_read_only_set(verb: str):
    with pytest.raises(ToolContractError):
        registration(verb=verb)


def test_s1a_the_registry_refuses_a_registration_that_is_not_read_only():
    with pytest.raises(ToolContractError, match="WRITE_CAPABILITY_FORBIDDEN"):
        registration(read_only=False)


def test_s1a_the_read_only_verb_set_is_disjoint_from_write_verbs():
    assert READ_ONLY_VERBS == {"get", "list", "watch", "logs", "query", "describe"}
    assert not READ_ONLY_VERBS & FORBIDDEN_VERBS


def test_s1a_the_executor_offers_exactly_one_read_only_entry_point():
    executor, _, _, _ = build()
    public = {name for name in dir(executor) if not name.startswith("_")}
    assert public == {"execute", "operations_used", "scope", "tool_seconds_used"}


def _shipped_registrations() -> list[Any]:
    # No public enumeration of a profile's registrations exists; the two
    # private builders are what the profiles' executor factories register.
    from opspilot.tools import fixture, otel_demo

    return [fixture._registration(), *otel_demo._registrations()]


def test_s1a_every_shipped_tool_is_a_bounded_read():
    shipped = _shipped_registrations()
    assert shipped
    for item in shipped:
        assert item.read_only is True, item.name
        assert item.verb in READ_ONLY_VERBS, item.name
        assert item.may_contain_secrets is False, item.name
        assert 0 < item.request_timeout_seconds <= MAX_REQUEST_TIMEOUT_SECONDS
        assert 0 < item.max_result_bytes <= MAX_RESULT_BYTES
        lowered = item.name.lower()
        assert not any(word in lowered for word in FORBIDDEN_VERBS), item.name
