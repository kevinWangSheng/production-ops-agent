"""A1 (#553) implementer tests for ``scripts/k8s_authz_probe.py`` with a fake
API server transport: the probe table's verdicts, dry-run writes, the
ConfigMap secret search and the output self-check. No cluster runs."""

from __future__ import annotations

import ast
import json
import sys
import urllib.parse
from pathlib import Path

import pytest

from scripts import k8s_authz_probe as probe

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/k8s_authz_probe.py"
TOKEN = "aaaaaaaa357"
KNOWN = "dddddddd357"
DENIED_WORDS = ("secrets", "exec", "attach", "portforward", "proxy", "/token")
CUSTOM_GROUPS = ("argoproj.io", "fluxcd.io", "istio.io", "chaos-mesh.org", "coreos.com")


class FakeApi:
    """A read-only identity as RBAC would answer it."""

    def __init__(self, *, configmaps: list[dict] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []
        self.configmaps = configmaps or [
            {
                "metadata": {"namespace": "otel-demo", "name": "flagd-config"},
                "data": {"demo.flagd.json": "{}"},
            },
            {
                "metadata": {"namespace": "kube-system", "name": "kube-root-ca.crt"},
                "data": {"ca.crt": "pem"},
            },
        ]
        self.pod_name = "etcd-node"
        self.override: dict[str, int] = {}

    def __call__(
        self, method: str, path: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        self.calls.append((method, path, headers, body))
        for prefix, status in self.override.items():
            if path.startswith(prefix):
                return status, b""
        split = urllib.parse.urlsplit(path)
        query = urllib.parse.parse_qs(split.query)
        if split.path == "/apis/authorization.k8s.io/v1/selfsubjectrulesreviews":
            return 201, json.dumps(
                {
                    "status": {
                        "resourceRules": [
                            {"verbs": ["get", "list"], "resources": ["pods"]}
                        ],
                        "nonResourceRules": [],
                        "incomplete": False,
                    }
                }
            ).encode()
        if (
            method != "GET"
            or "watch" in query
            or any(h.startswith("Impersonate") for h in headers)
            or any(w in split.path for w in DENIED_WORDS)
            or any(g in split.path for g in CUSTOM_GROUPS)
        ):
            return 403, b""
        if split.path == "/api/v1/namespaces/kube-system/pods":
            return 200, json.dumps(
                {
                    "items": [
                        {
                            "metadata": {"name": self.pod_name},
                            "spec": {"containers": [{"name": "etcd"}]},
                        }
                    ]
                }
            ).encode()
        if split.path == "/api/v1/nodes":
            return 200, json.dumps(
                {"items": [{"metadata": {"name": "node-1"}}]}
            ).encode()
        if split.path == "/api/v1/configmaps":
            # two pages
            if "continue" not in query:
                page = {"metadata": {"continue": "p2"}, "items": self.configmaps[:1]}
            else:
                page = {"metadata": {}, "items": self.configmaps[1:]}
            return 200, json.dumps(page).encode()
        return 200, b"{}"


_API: list[FakeApi] = []


@pytest.fixture(autouse=True)
def _transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """``probe.request`` answered by the current FakeApi; checks the bearer."""

    def request(
        method: str,
        path: str,
        auth: str,
        *,
        env_file: Path,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, bytes]:
        assert auth == f"Bearer {TOKEN}"
        return _API[-1](method, path, dict(headers or {}), body)

    monkeypatch.setattr(probe, "request", request)


def _run(api: FakeApi, tmp_path: Path, *extra: str) -> int:
    _API.append(api)
    env = tmp_path / "k8s.env"
    env.write_text(
        "# comment\n"
        "OTHER=1\n"
        "OPSPILOT_K8S_API_URL=https://127.0.0.1:41234\n"
        f"OPSPILOT_K8S_TOKEN='{TOKEN}'\n"
        "OPSPILOT_K8S_CA_FILE=/nonexistent/ca.crt\n"
    )
    known = tmp_path / "known.txt"
    known.write_text(f"{KNOWN}\n\n")
    return probe.main(["--env-file", str(env), "--known-secrets", str(known), *extra])


def test_a_read_only_identity_matches_every_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeApi()
    out = tmp_path / "out"
    assert _run(api, tmp_path, "--out-dir", str(out), "--side", "worker") == 0
    report = json.loads((out / "probe.json").read_text())
    rules = json.loads((out / "rules.json").read_text())
    assert report["ok"] is True
    assert report["side"] == "worker"
    assert report["discovered"] == {
        "pod": "etcd-node",
        "container": "etcd",
        "node": "node-1",
    }
    expected = {r["name"]: r["expected"] for r in report["requests"]}
    assert expected["get the fault switch ConfigMap (Q5)"] == 200
    assert expected["read a kube-system pod log"] == 200
    assert expected["list kube-system pods"] == 200
    for name in (
        "list secrets, all namespaces",
        "watch pods",
        "exec (GET)",
        "exec (POST)",
        "node proxy",
        "issue itself a token (dry run)",
        "impersonate a user",
        "create a ConfigMap (dry run)",
        "delete a pod (dry run)",
    ):
        assert expected[name] == 403, name
    for name in (
        "list custom resource chaos-mesh.org",
        "list custom resource argoproj.io",
    ):
        assert expected[name] == [403, 404], name
    scan = report["configmap_secret_scan"]
    assert scan == {
        "configmaps_scanned": 2,
        "known_values": 2,
        "hits": 0,
        "hit_keys": [],
        "expected": 0,
        "matches": True,
    }
    assert set(rules["reviews"]) == {"otel-demo", "kube-system"}
    text = (out / "probe.json").read_text() + (out / "rules.json").read_text()
    assert TOKEN not in text
    # the bearer goes only in ``request``'s auth argument, never in headers
    assert all("Authorization" not in h for _, _, h, _ in api.calls)


def test_every_write_class_request_is_a_dry_run() -> None:
    table = probe.rows("otel-demo", {"pod": "p", "container": "c", "node": "n"})
    writes = [r for r in table if r["method"] in ("POST", "PUT", "PATCH", "DELETE")]
    assert writes
    for row in writes:
        assert "dryRun=All" in row["path"], row["name"]
        assert row["expected"] == 403


def test_a_granted_secret_or_write_fails_the_probe(tmp_path: Path) -> None:
    api = FakeApi()
    api.override = {"/api/v1/secrets": 200}
    assert _run(api, tmp_path) == 1
    api = FakeApi()
    api.override = {"/api/v1/namespaces?dryRun=All": 201}
    assert _run(api, tmp_path) == 1


def test_a_known_secret_in_a_configmap_is_counted_by_location_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import base64

    api = FakeApi(
        configmaps=[
            {
                "metadata": {"namespace": "otel-demo", "name": "a"},
                "data": {"k": f"x {KNOWN} y"},
            },
            {
                "metadata": {"namespace": "otel-demo", "name": "b"},
                "binaryData": {"bin": base64.b64encode(TOKEN.encode()).decode()},
            },
        ]
    )
    assert _run(api, tmp_path) == 1
    report = json.loads(capsys.readouterr().out)
    scan = report["configmap_secret_scan"]
    assert scan["hits"] == 2
    assert scan["hit_keys"] == ["otel-demo/a/k", "otel-demo/b/bin"]
    assert scan["matches"] is False


def test_an_unreadable_configmap_list_is_not_a_clean_scan(tmp_path: Path) -> None:
    api = FakeApi()
    api.override = {"/api/v1/configmaps?limit": 403}
    assert _run(api, tmp_path) == 1


def test_output_with_a_jwt_shape_or_the_token_is_refused_and_not_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeApi()
    api.pod_name = "eyJ" + "a" * 8 + "." + "b" * 8
    out = tmp_path / "out"
    assert _run(api, tmp_path, "--out-dir", str(out)) == 2
    assert not out.exists()
    printed = capsys.readouterr().out
    assert "JWT shape" in printed and api.pod_name not in printed

    api = FakeApi()
    api.pod_name = f"pod-{TOKEN}"
    assert _run(api, tmp_path, "--out-dir", str(out)) == 2
    assert not out.exists()
    assert TOKEN not in capsys.readouterr().out


def test_check_output_rules() -> None:
    probe.check_output('{"ok": true}', TOKEN, [KNOWN])
    with pytest.raises(probe.RefusedOutput):
        probe.check_output(f"x{KNOWN}x", TOKEN, [KNOWN])
    long_token = "eyJhead" + "." + "p" * 20 + "." + "s" * 20
    with pytest.raises(probe.RefusedOutput):
        probe.check_output("s" * 20, long_token, [])


def test_env_file_must_hold_the_three_values(tmp_path: Path) -> None:
    env = tmp_path / "e"
    env.write_text("OPSPILOT_K8S_API_URL=https://127.0.0.1:1\nOPSPILOT_K8S_TOKEN=x\n")
    with pytest.raises(ValueError):
        probe.read_env(env)
    env.write_text(
        "OPSPILOT_K8S_API_URL=http://127.0.0.1:1\nOPSPILOT_K8S_TOKEN=x\n"
        "OPSPILOT_K8S_CA_FILE=/c\n"
    )
    with pytest.raises(ValueError):
        probe.read_env(env)


def test_unreadable_inputs_print_no_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = probe.main(
        [
            "--env-file",
            str(tmp_path / "missing"),
            "--known-secrets",
            str(tmp_path / "k"),
        ],
    )
    assert code == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error": "cannot read the probe inputs",
    }


def test_the_probe_uses_the_standard_library_only() -> None:
    tree = ast.parse(SCRIPT.read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    assert modules - {"__future__"} <= set(sys.stdlib_module_names)


def test_the_https_transport_uses_no_proxy_and_no_redirects() -> None:
    import urllib.request

    send = probe.https_transport("https://127.0.0.1:1", TOKEN, None)  # type: ignore[arg-type]
    opener = send.__closure__  # the opener is captured by the closure
    assert opener is not None
    handlers = next(
        c.cell_contents
        for c in opener
        if isinstance(c.cell_contents, urllib.request.OpenerDirector)
    ).handlers
    proxies = [h for h in handlers if isinstance(h, urllib.request.ProxyHandler)]
    # ProxyHandler({}) replaces the environment-reading default
    assert not [p for p in proxies if p.proxies]
    assert any(isinstance(h, probe._NoRedirect) for h in handlers)


def test_without_known_secrets_the_report_is_not_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _API.append(FakeApi())
    env = tmp_path / "k8s.env"
    env.write_text(
        "OPSPILOT_K8S_API_URL=https://127.0.0.1:41234\n"
        f"OPSPILOT_K8S_TOKEN={TOKEN}\n"
        "OPSPILOT_K8S_CA_FILE=/nonexistent/ca.crt\n"
    )
    out = tmp_path / "probe.json"
    assert probe.main(["--env-file", str(env), "--output", str(out)]) == 1
    report = json.loads(out.read_text())
    assert report["known_secrets_supplied"] is False
    assert report["ok"] is False


def test_three_dot_joined_runs_count_as_a_jwt_shape() -> None:
    with pytest.raises(probe.RefusedOutput):
        probe.check_output(".".join(("a" * 8, "b" * 8, "c" * 8)), TOKEN, [])
    for harmless in ("events.k8s.io", "source.toolkit.fluxcd.io", "127.0.0.1"):
        probe.check_output(harmless, TOKEN, [])


def test_custom_resources_accept_rbac_or_unregistered_and_say_which(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeApi()
    api.override = {
        "/apis/argoproj.io/": 404,
        "/apis/chaos-mesh.org/": 404,
    }
    assert _run(api, tmp_path) == 0
    rows = {
        r["name"]: r
        for r in json.loads(capsys.readouterr().out)["requests"]
        if r["name"].startswith("list custom resource")
    }
    assert rows["list custom resource argoproj.io"]["denied_by"] == "not_registered"
    assert rows["list custom resource chaos-mesh.org"]["denied_by"] == "not_registered"
    assert rows["list custom resource networking.istio.io"]["denied_by"] == "rbac"
    assert all(r["matches"] for r in rows.values())


@pytest.mark.parametrize("status", [200, 500, 0])
def test_a_readable_or_unanswered_custom_resource_still_fails(
    tmp_path: Path, status: int
) -> None:
    api = FakeApi()
    api.override = {"/apis/argoproj.io/": status}
    assert _run(api, tmp_path) == 1


@pytest.mark.parametrize(
    "prefix",
    [
        "/api/v1/secrets",
        "/api/v1/pods?watch=true",
        "/api/v1/namespaces?dryRun=All",
        "/api/v1/namespaces/kube-system/pods/etcd-node/exec",
    ],
)
def test_core_denials_still_require_403(tmp_path: Path, prefix: str) -> None:
    api = FakeApi()
    api.override = {prefix: 404}
    assert _run(api, tmp_path) == 1
