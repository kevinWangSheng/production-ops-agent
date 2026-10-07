"""The kind lab script's engineer-side writes: confined history, verified
fault round trips, lab-local Helm/kube state, and a health predicate that
covers every component it reports."""

import json
import os
import stat
from pathlib import Path

import pytest

from scripts import kind_lab
from scripts.kind_lab import FAULT_VARIANT, FLAG, NORMAL_VARIANT, mutate_flags

FLAGS = {
    "$schema": "https://flagd.dev/schema/v0/flags.json",
    "flags": {
        FLAG: {
            "state": "ENABLED",
            "variants": {"100%": 1, "10%": 0.1, "off": 0},
            "defaultVariant": "off",
        },
        "other": {"state": "ENABLED", "variants": {"on": True}, "defaultVariant": "on"},
    },
}


def test_inject_moves_only_payment_failure() -> None:
    out = json.loads(
        mutate_flags(json.dumps(FLAGS).encode(), FAULT_VARIANT, NORMAL_VARIANT)
    )
    assert out["flags"][FLAG]["defaultVariant"] == "100%"
    assert out["flags"]["other"] == FLAGS["flags"]["other"]
    assert out["flags"][FLAG]["variants"] == FLAGS["flags"][FLAG]["variants"]


def test_restore_round_trips() -> None:
    injected = mutate_flags(json.dumps(FLAGS).encode(), FAULT_VARIANT, NORMAL_VARIANT)
    restored = mutate_flags(injected, NORMAL_VARIANT, FAULT_VARIANT)
    assert json.loads(restored) == FLAGS


def test_repeat_injection_refused() -> None:
    injected = mutate_flags(json.dumps(FLAGS).encode(), FAULT_VARIANT, NORMAL_VARIANT)
    with pytest.raises(SystemExit):
        mutate_flags(injected, FAULT_VARIANT, NORMAL_VARIANT)


def test_unknown_variant_refused() -> None:
    with pytest.raises(SystemExit):
        mutate_flags(json.dumps(FLAGS).encode(), "50%", NORMAL_VARIANT)


# -- finding 1: fault history is confined to the lab-owned directory ---------


def test_history_root_is_fixed_under_repo_tmp() -> None:
    assert kind_lab.HISTORY_ROOT == kind_lab.ROOT / "tmp/m1-kind-lab/engineer-only"
    assert "OPSPILOT_KIND_LAB_HISTORY" not in Path(kind_lab.__file__).read_text()


def test_history_dir_refuses_escape(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "engineer-only"
    monkeypatch.setattr(kind_lab, "HISTORY_ROOT", root)
    assert kind_lab.history_dir("exp-1") == root / "exp-1"
    with pytest.raises(SystemExit):
        kind_lab.history_dir("../../postgres")
    # A symlink planted inside the root that points elsewhere is refused too.
    root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside)
    with pytest.raises(SystemExit):
        kind_lab.history_dir("linked")


# -- finding 2: no global Helm or kubeconfig state ------------------------------


def test_helm_and_kube_state_are_lab_local() -> None:
    env = kind_lab.lab_env()
    lab = kind_lab.ROOT / "tmp/m1-kind-lab"
    for key in ("HELM_CONFIG_HOME", "HELM_CACHE_HOME", "HELM_DATA_HOME", "KUBECONFIG"):
        assert Path(env[key]).is_relative_to(lab), key
    assert kind_lab.kubectl("get", "nodes")[:5] == [
        "kubectl",
        "--kubeconfig",
        str(kind_lab.KUBECONFIG),
        "--context",
        kind_lab.KUBE_CONTEXT,
    ]
    helm = kind_lab.helm("list")
    assert helm[:3] == ["helm", "--kubeconfig", str(kind_lab.KUBECONFIG)]
    assert "--kube-context" in helm and kind_lab.KUBE_CONTEXT in helm
    create = kind_lab.kind_create_cmd()
    assert "--kubeconfig" in create
    assert create[create.index("--kubeconfig") + 1] == str(kind_lab.KUBECONFIG)


# -- finding 3: a fault action is verified by re-reading the ConfigMap --------


def _fake_kubectl(
    path: Path, state: Path, *, apply_patches: bool, patch_exit: int = 0
) -> Path:
    """A ``kubectl`` stand-in: ``get configmap`` prints ``state``; ``patch``
    rewrites it (or silently does nothing when ``apply_patches`` is False),
    or fails outright with ``patch_exit`` when that is non-zero."""
    script = f"""#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
state = {str(state)!r}
if "get" in args:
    print(json.dumps({{"data": {{"demo.flagd.json": open(state).read()}}}}))
elif "patch" in args:
    if {patch_exit!r}:
        print("error: the server is unreachable", file=sys.stderr)
        sys.exit({patch_exit!r})
    if {apply_patches!r}:
        payload = json.loads(args[args.index("-p") + 1])
        open(state, "w").write(payload["data"]["demo.flagd.json"])
    print("configmap/flagd-config patched")
else:
    sys.exit("unexpected: " + " ".join(args))
"""
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _fault(monkeypatch, tmp_path: Path, kubectl: Path, action: str, exp: str) -> int:
    monkeypatch.setattr(kind_lab, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(kind_lab, "KUBECTL", str(kubectl))
    return kind_lab.main(["fault", action, "--experiment-id", exp])


def test_fault_round_trip_verified_against_live_configmap(
    tmp_path: Path, monkeypatch
) -> None:
    state = tmp_path / "live.json"
    state.write_text(json.dumps(FLAGS, indent=2) + "\n")
    kubectl = _fake_kubectl(tmp_path / "kubectl", state, apply_patches=True)
    assert _fault(monkeypatch, tmp_path, kubectl, "inject", "exp") == 0
    assert json.loads(state.read_text())["flags"][FLAG]["defaultVariant"] == "100%"
    assert _fault(monkeypatch, tmp_path, kubectl, "restore", "exp") == 0
    assert json.loads(state.read_text()) == FLAGS
    log = [
        json.loads(line)
        for line in (tmp_path / "history/exp/fault-log.jsonl").read_text().splitlines()
    ]
    assert [entry["action"] for entry in log] == ["inject", "restore"]
    assert log[0]["before_sha256"] == log[1]["after_sha256"]
    assert all(entry["verified"] is True for entry in log)


def test_restore_fails_when_configmap_does_not_match(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    state = tmp_path / "live.json"
    state.write_text(json.dumps(FLAGS, indent=2) + "\n")
    honest = _fake_kubectl(tmp_path / "kubectl-ok", state, apply_patches=True)
    assert _fault(monkeypatch, tmp_path, honest, "inject", "exp") == 0
    # The patch "succeeds" but the live ConfigMap keeps the injected flags.
    lying = _fake_kubectl(tmp_path / "kubectl-noop", state, apply_patches=False)
    code = _fault(monkeypatch, tmp_path, lying, "restore", "exp")
    assert code != 0
    assert "sha256" in capsys.readouterr().err.lower()
    assert json.loads(state.read_text())["flags"][FLAG]["defaultVariant"] == "100%"
    log = (tmp_path / "history/exp/fault-log.jsonl").read_text().splitlines()
    assert json.loads(log[-1])["action"] == "restore"
    assert json.loads(log[-1])["verified"] is False


@pytest.mark.parametrize("failure", ["patch_fails", "verification_mismatch"])
def test_failed_injection_leaves_no_history_so_retry_works(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    """Codex P2 on PR #111: history written before the patch wedged a retry."""
    state = tmp_path / "live.json"
    state.write_text(json.dumps(FLAGS, indent=2) + "\n")
    if failure == "patch_fails":
        broken = _fake_kubectl(
            tmp_path / "kubectl-broken", state, apply_patches=False, patch_exit=1
        )
    else:
        broken = _fake_kubectl(tmp_path / "kubectl-broken", state, apply_patches=False)
    assert _fault(monkeypatch, tmp_path, broken, "inject", "exp") != 0
    history = tmp_path / "history/exp"
    assert not (history / "flags-original.json").exists()
    assert not (history / "flags-injected.json").exists()
    assert json.loads(state.read_text()) == FLAGS
    # Nothing to restore either: the live flags were never changed.
    with pytest.raises(SystemExit):
        _fault(monkeypatch, tmp_path, broken, "restore", "exp")
    honest = _fake_kubectl(tmp_path / "kubectl-ok", state, apply_patches=True)
    assert _fault(monkeypatch, tmp_path, honest, "inject", "exp") == 0
    assert json.loads(state.read_text())["flags"][FLAG]["defaultVariant"] == "100%"
    assert _fault(monkeypatch, tmp_path, honest, "restore", "exp") == 0
    log = [
        json.loads(line)
        for line in (history / "fault-log.jsonl").read_text().splitlines()
    ]
    assert [e["verified"] for e in log] == [False, True, True]


def test_fake_kubectl_is_not_on_path() -> None:
    # Guard against the fakes above leaking into the real command.
    assert kind_lab.KUBECTL == "kubectl"
    assert not os.environ.get("KUBECTL")


# -- finding 4 (Codex thread): health ``ok`` covers every reported component --


def test_health_ok_requires_every_component(monkeypatch) -> None:
    monkeypatch.setattr(kind_lab, "prom_instant", lambda expr: (200, [{"value": 1}]))
    monkeypatch.setattr(
        kind_lab, "get_json", lambda url, timeout=10: (200, {"data": ["checkout"]})
    )
    monkeypatch.setattr(kind_lab, "deployments_not_ready", lambda: [])
    monkeypatch.setattr(kind_lab, "frontend_status", lambda: 503)
    report = kind_lab.health(quiet=True)
    assert report["frontend"]["ok"] is False
    assert report["ok"] is False
    for component in ("prometheus", "jaeger", "frontend", "deployments"):
        assert "ok" in report[component], component
    monkeypatch.setattr(kind_lab, "frontend_status", lambda: 200)
    assert kind_lab.health(quiet=True)["ok"] is True
    monkeypatch.setattr(kind_lab, "deployments_not_ready", lambda: ["checkout"])
    report = kind_lab.health(quiet=True)
    assert report["deployments"]["ok"] is False and report["ok"] is False
