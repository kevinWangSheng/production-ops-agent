"""#667 PR-B contract tests: read-only identity (S-4, S-14), job, registry.

Written from docs/tasks/2026-11-19-sregym-667.md "PR-B 公开接口" and the
pre-review (S-4 identity timing, S-14 wrong-cluster guard), without reading the
PR-B implementation. ``kubectl`` is a recording stand-in; no cluster is used.

The stand-in answers both ``kubectl config current-context`` and
``kubectl config view --raw --minify -o json`` from the same kubeconfig, and a
real kubeconfig file with the same content is passed, so the checks hold
whichever of the two the implementation reads.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import shlex
import stat
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.sregym_eval_support import LITE_17, REPO, code_of, mod

CA_PEM = b"-----BEGIN CERTIFICATE-----\nMIIBfakeCA\n-----END CERTIFICATE-----\n"
MANIFEST = REPO / "scripts" / "kind_lab" / "opspilot-readonly-rbac.yaml"


def _kubeconfig(
    context: str = "kind-sregym", server: str = "https://127.0.0.1:43211"
) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": context,
        "clusters": [
            {
                "name": context,
                "cluster": {
                    "server": server,
                    "certificate-authority-data": base64.b64encode(CA_PEM).decode(),
                },
            }
        ],
        "contexts": [
            {"name": context, "context": {"cluster": context, "user": context}}
        ],
        "users": [{"name": context, "user": {"token": "engineer-admin-token-xyz"}}],
    }


class FakeHost:
    """Records every kubectl call; answers from ``config`` and ``tokens``."""

    def __init__(self, config: dict, tokens: Sequence[str] = ("sa-token-1",)) -> None:
        self.config = config
        self.tokens = list(tokens)
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.can_i: dict[tuple[str, ...], str] = {}

    def kubectl(
        self,
        argv: Sequence[str],
        env: Mapping[str, str] | None = None,
        timeout: Any = None,
    ) -> subprocess.CompletedProcess:
        argv = list(argv)
        self.calls.append((argv, dict(env or {})))
        out = ""
        if "current-context" in argv:
            out = self.config["current-context"] + "\n"
        elif "config" in argv and "view" in argv:
            out = json.dumps(self.config)
        elif "create" in argv and "token" in argv:
            out = self.tokens.pop(0) + "\n"
        elif "can-i" in argv:
            out = self.can_i.get(tuple(argv), "no") + "\n"
        elif "apply" in argv:
            out = "applied\n"
        return subprocess.CompletedProcess(argv, 0, out, "")

    # unused Host members, present so a structural Host check passes
    def run_main(self, *a: Any, **k: Any) -> int:  # pragma: no cover
        raise AssertionError("run_main not expected")

    def start_filter_proxy(self, *a: Any, **k: Any) -> Any:  # pragma: no cover
        raise AssertionError("start_filter_proxy not expected")

    def port_in_use(self, port: int) -> bool:  # pragma: no cover
        return False

    def http_probe(self, *a: Any, **k: Any) -> Any:  # pragma: no cover
        raise AssertionError("http_probe not expected")

    def trace_roots(self, *a: Any, **k: Any) -> Any:  # pragma: no cover
        return {}

    def now(self) -> Any:  # pragma: no cover
        raise AssertionError("now not expected")

    def sleep(self, s: float) -> None:  # pragma: no cover
        raise AssertionError("sleep not expected")


def _write_kubeconfig(tmp_path: Path, config: dict) -> Path:
    path = tmp_path / "sregym.kubeconfig"
    path.write_text(yaml.safe_dump(config))
    return path


def _targets_kubeconfig(argv: list[str], env: dict[str, str], kubeconfig: Path) -> bool:
    if env.get("KUBECONFIG") == str(kubeconfig):
        return True
    joined = " ".join(argv)
    return (
        f"--kubeconfig={kubeconfig}" in joined or f"--kubeconfig {kubeconfig}" in joined
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- constants ---------------------------------------------------------------------------------


def test_identity_constants() -> None:
    ident = mod("identity")
    assert ident.IDENTITY_NAMESPACE == "opspilot-readonly"
    assert ident.SERVICE_ACCOUNT == "opspilot-investigator"
    assert Path(ident.RBAC_MANIFEST).resolve() == MANIFEST.resolve()
    assert ident.TOKEN_DURATION == "24h"
    assert ident.EXPECTED_CONTEXT == "kind-sregym"


def test_k8s_target_repr_hides_the_token() -> None:
    t = mod("identity").K8sTarget(
        api_url="https://127.0.0.1:1", ca_file="/x/ca.crt", token="tok-SECRET-1"
    )
    assert "tok-SECRET-1" not in repr(t)
    assert t.token == "tok-SECRET-1"
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.token = "other"  # type: ignore[misc]


# --- S-14 context guard ------------------------------------------------------------------------


def test_context_kind_sregym_on_loopback_passes(tmp_path: Path) -> None:
    config = _kubeconfig()
    mod("identity").check_context(FakeHost(config), _write_kubeconfig(tmp_path, config))


@pytest.mark.parametrize(
    "context", ["kind-holmes", "kind-opspilot-lab", "sregym", "prod"]
)
def test_wrong_context_is_refused(tmp_path: Path, context: str) -> None:
    config = _kubeconfig(context=context)
    with pytest.raises(mod("identity").IdentityError) as info:
        mod("identity").check_context(
            FakeHost(config), _write_kubeconfig(tmp_path, config)
        )
    assert code_of(info.value) == "WRONG_CLUSTER"


@pytest.mark.parametrize(
    "server",
    [
        "https://10.0.0.5:6443",
        "https://kind-sregym-control-plane:6443",
        "http://127.0.0.1:43211",
        "https://127.0.0.2:43211",
        "https://127.0.0.1",
    ],
)
def test_unexpected_server_is_refused(tmp_path: Path, server: str) -> None:
    config = _kubeconfig(server=server)
    with pytest.raises(mod("identity").IdentityError) as info:
        mod("identity").check_context(
            FakeHost(config), _write_kubeconfig(tmp_path, config)
        )
    assert code_of(info.value) == "KUBECONFIG_SERVER_UNEXPECTED"


# --- S-4 baseline ---------------------------------------------------------------------------------


def test_missing_baseline_passes(tmp_path: Path) -> None:
    mod("identity").check_baseline(tmp_path / "cluster_baseline_state.json")


def test_baseline_with_identity_passes(tmp_path: Path) -> None:
    path = tmp_path / "cluster_baseline_state.json"
    path.write_text(
        json.dumps({"namespaces": ["default", "kube-system", "opspilot-readonly"]})
    )
    mod("identity").check_baseline(path)


def test_baseline_without_identity_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "cluster_baseline_state.json"
    path.write_text(json.dumps({"namespaces": ["default", "kube-system"]}))
    with pytest.raises(mod("identity").IdentityError) as info:
        mod("identity").check_baseline(path)
    assert code_of(info.value) == "BASELINE_WITHOUT_IDENTITY"


# --- S-4 apply: static check first -------------------------------------------------------------


def test_apply_identity_applies_the_checked_manifest(tmp_path: Path) -> None:
    config = _kubeconfig()
    kc = _write_kubeconfig(tmp_path, config)
    host = FakeHost(config)
    mod("identity").apply_identity(host, kc)
    applies = [(a, e) for a, e in host.calls if "apply" in a]
    assert len(applies) == 1
    argv, env = applies[0]
    assert "-f" in argv
    assert Path(argv[argv.index("-f") + 1]).resolve() == MANIFEST.resolve()
    assert _targets_kubeconfig(argv, env, kc)


def test_apply_identity_refuses_a_violating_manifest_before_kubectl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = tmp_path / "bad-rbac.yaml"
    bad.write_text(
        MANIFEST.read_text().replace(
            "verbs: [get, list]", "verbs: [get, list, delete]", 1
        )
    )
    ident = mod("identity")
    monkeypatch.setattr(ident, "RBAC_MANIFEST", bad)
    config = _kubeconfig()
    host = FakeHost(config)
    with pytest.raises(ident.IdentityError) as info:
        ident.apply_identity(host, _write_kubeconfig(tmp_path, config))
    assert code_of(info.value) == "RBAC_MANIFEST_VIOLATION"
    assert not [a for a, _ in host.calls if "apply" in a]


# --- S-4 token per pass ---------------------------------------------------------------------------


def test_issue_token_writes_private_files_and_returns_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _kubeconfig()
    kc = _write_kubeconfig(tmp_path, config)
    out = tmp_path / "trial"
    out.mkdir()
    host = FakeHost(config, tokens=["sa-token-FIRST"])
    target = mod("identity").issue_token(host, kc, out)

    token_calls = [(a, e) for a, e in host.calls if "create" in a and "token" in a]
    assert len(token_calls) == 1
    argv, env = token_calls[0]
    joined = " ".join(argv)
    assert (
        "-n opspilot-readonly" in joined
        or "--namespace opspilot-readonly" in joined
        or ("--namespace=opspilot-readonly" in joined)
    )
    assert "create token opspilot-investigator" in joined
    assert "--duration 24h" in joined or "--duration=24h" in joined
    assert _targets_kubeconfig(argv, env, kc)

    assert target.token == "sa-token-FIRST"
    assert target.api_url == "https://127.0.0.1:43211"
    ca = Path(target.ca_file)
    assert ca.read_bytes() == CA_PEM
    readonly_kc = out / "readonly-kubeconfig.yaml"
    for p in (ca, readonly_kc):
        assert p.is_file() and _mode(p) == 0o600, p
    assert "sa-token-FIRST" in readonly_kc.read_text()
    assert "engineer-admin-token-xyz" not in readonly_kc.read_text()

    printed = capsys.readouterr()
    assert "sa-token-FIRST" not in printed.out + printed.err


def test_each_pass_gets_a_new_token(tmp_path: Path) -> None:
    config = _kubeconfig()
    kc = _write_kubeconfig(tmp_path, config)
    host = FakeHost(config, tokens=["tok-pass-1", "tok-pass-2"])
    ident = mod("identity")
    (tmp_path / "t1").mkdir()
    (tmp_path / "t2").mkdir()
    a = ident.issue_token(host, kc, tmp_path / "t1")
    b = ident.issue_token(host, kc, tmp_path / "t2")
    assert (a.token, b.token) == ("tok-pass-1", "tok-pass-2")
    assert (
        len([1 for argv, _ in host.calls if "create" in argv and "token" in argv]) == 2
    )


# --- S-5 layer 2 probe -------------------------------------------------------------------------------


def test_probe_can_i_runs_every_check_as_the_readonly_subject(tmp_path: Path) -> None:
    b = mod("boundary")
    config = _kubeconfig()
    kc = _write_kubeconfig(tmp_path, config)
    host = FakeHost(config)
    for c in b.CAN_I_CHECKS:
        host.can_i[tuple(b.can_i_argv(c, b.READONLY_SUBJECT))] = c.expect
    answers = mod("identity").probe_can_i(host, kc)
    assert set(answers) == {f"{c.verb} {c.resource}" for c in b.CAN_I_CHECKS}
    for c in b.CAN_I_CHECKS:
        assert answers[f"{c.verb} {c.resource}"].strip() == c.expect
    probes = [(a, e) for a, e in host.calls if "can-i" in a]
    assert len(probes) == len(b.CAN_I_CHECKS)
    for argv, env in probes:
        assert f"--as={b.READONLY_SUBJECT}" in argv
        assert _targets_kubeconfig(argv, env, kc)


# --- job ---------------------------------------------------------------------------------------------


def _job(tmp_path: Path, **over: Any) -> Any:
    job_mod = mod("job")
    fields = {
        "round": "r1",
        "seq": 3,
        "trial": 2,
        "work_dir": str(tmp_path / "work"),
        "evidence_hint": None,
        "ports": mod("ports").Ports(),
        "cluster_path": "filter_proxy_readonly",
        "k8s_target_file": str(tmp_path / "k8s-target.json"),
        "engineer_kubeconfig": str(tmp_path / "sregym.kubeconfig"),
        "secrets_file": str(tmp_path / "secrets.env"),
        "loki_env_file": str(tmp_path / "loki-readonly.env"),
        "trace": "off",
        "project_name": "opspilot-sregym-r1",
        "problem_digest": mod("boundary").problem_id_digest(LITE_17[2]),
    }
    fields.update(over)
    return job_mod.Job(**fields)


def test_job_defaults(tmp_path: Path) -> None:
    job = _job(tmp_path)
    assert job.diagnosis_wait_s == 900
    assert job.submit_margin_s == 240
    assert job.done_wait_s == 600


def test_job_has_no_problem_id_field() -> None:
    names = {f.name for f in dataclasses.fields(mod("job").Job)}
    assert not {
        n for n in names if n in ("problem_id", "problem", "pid", "problem_name")
    }


def test_job_round_trips_as_private_json_without_the_problem_id(tmp_path: Path) -> None:
    j = mod("job")
    job = _job(tmp_path)
    path = tmp_path / "job.json"
    j.write_job(path, job)
    assert _mode(path) == 0o600
    text = path.read_text()
    json.loads(text)
    for pid in LITE_17:
        assert pid not in text
        assert pid.replace("_", "") not in text.replace("_", "").replace("-", "")
    back = j.read_job(path)
    assert back == job


@pytest.mark.parametrize(
    "field", ["round", "seq", "trial", "work_dir", "problem_digest", "ports"]
)
def test_read_job_refuses_a_missing_field(tmp_path: Path, field: str) -> None:
    j = mod("job")
    path = tmp_path / "job.json"
    j.write_job(path, _job(tmp_path))
    data = json.loads(path.read_text())
    assert field in data
    del data[field]
    path.write_text(json.dumps(data))
    with pytest.raises(j.JobError) as info:
        j.read_job(path)
    assert code_of(info.value) == "JOB_INVALID"


@pytest.mark.parametrize(
    "field, value", [("seq", "three"), ("trial", None), ("round", 5)]
)
def test_read_job_refuses_a_wrong_type(tmp_path: Path, field: str, value: Any) -> None:
    j = mod("job")
    path = tmp_path / "job.json"
    j.write_job(path, _job(tmp_path))
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(j.JobError) as info:
        j.read_job(path)
    assert code_of(info.value) == "JOB_INVALID"


@pytest.mark.parametrize("raw", ["", "not json", "[]", "null"])
def test_read_job_refuses_garbage(tmp_path: Path, raw: str) -> None:
    j = mod("job")
    path = tmp_path / "job.json"
    path.write_text(raw)
    with pytest.raises(j.JobError) as info:
        j.read_job(path)
    assert code_of(info.value) == "JOB_INVALID"


def test_read_job_refuses_a_missing_file(tmp_path: Path) -> None:
    j = mod("job")
    with pytest.raises(j.JobError) as info:
        j.read_job(tmp_path / "absent.json")
    assert code_of(info.value) == "JOB_INVALID"


# --- agent registry ----------------------------------------------------------------------------


PY = "/opt/my envs/bin/python3"


def _check_entry(doc: dict, repo_root: Path) -> None:
    assert list(doc) == ["agents"] or set(doc) == {"agents"}
    assert len(doc["agents"]) == 1
    a = doc["agents"][0]
    assert a["name"] == "opspilot"
    assert shlex.split(a["kickoff_command"]) == [
        PY,
        "-m",
        "scripts.sregym_eval.agent_entry",
    ]
    assert a["kickoff_workdir"] == str(repo_root)
    assert a["kickoff_env"] is None
    assert a["install_script"] is None
    assert a["agent_version"] is None
    assert a["container_isolation"] is False


def test_registry_document(tmp_path: Path) -> None:
    reg = mod("agent_registry")
    assert reg.AGENT_NAME == "opspilot"
    _check_entry(reg.registry_document(PY, tmp_path), tmp_path)


def test_write_registry_is_readable_the_way_upstream_reads_it(tmp_path: Path) -> None:
    reg = mod("agent_registry")
    path = tmp_path / "agents.yaml"
    reg.write_registry(path, PY, REPO)
    data = yaml.safe_load(path.read_text())
    _check_entry(data, REPO)
    # upstream list_agents: kickoff_env None -> {}, container_isolation default True
    a = data["agents"][0]
    assert (a.get("kickoff_env") or {}) == {}
    assert a.get("container_isolation", True) is False
