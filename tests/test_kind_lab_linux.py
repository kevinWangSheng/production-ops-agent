"""S4 of the F1 closure plan: the kind lab scripts run on a Linux host.

The lab was bound to macOS/colima (docker context, ``colima`` calls,
``vm_stat``, the colima host address in the chart values, ``colima ssh`` for
the Holmes clock). These tests pin both platform branches: macOS keeps its
exact behaviour (same context, same colima commands, the committed values
files passed unchanged) and Linux uses the default docker socket, never
calls colima, reads ``/proc/meminfo`` and points the webhook and the
OpsPilot scrape targets at the kind bridge gateway or a configured address.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts import kind_lab
from scripts.holmes_baseline import run as hrun

ROOT = Path(kind_lab.ROOT)
INSTALLER = ROOT / "scripts/kind_lab/install_linux.sh"

LAB_VARS = (
    "OPSPILOT_LAB_RUNTIME",
    "OPSPILOT_LAB_DOCKER_CONTEXT",
    "OPSPILOT_LAB_HOST_ADDRESS",
    "DOCKER_CONTEXT",
)


@pytest.fixture(autouse=True)
def _clean_lab_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in LAB_VARS:
        monkeypatch.delenv(name, raising=False)


def _platform(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setattr(sys, "platform", name)


# --- runtime selection


def test_runtime_follows_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    _platform(monkeypatch, "darwin")
    assert kind_lab.lab_runtime() == "colima"
    _platform(monkeypatch, "linux")
    assert kind_lab.lab_runtime() == "docker"


def test_runtime_override_and_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    _platform(monkeypatch, "darwin")
    monkeypatch.setenv("OPSPILOT_LAB_RUNTIME", "docker")
    assert kind_lab.lab_runtime() == "docker"
    monkeypatch.setenv("OPSPILOT_LAB_RUNTIME", "podman")
    with pytest.raises(SystemExit):
        kind_lab.lab_runtime()


# --- docker context


def test_macos_keeps_the_colima_context(monkeypatch: pytest.MonkeyPatch) -> None:
    _platform(monkeypatch, "darwin")
    assert kind_lab.docker_context() == "colima-m1-kind"
    assert kind_lab.docker_env()["DOCKER_CONTEXT"] == "colima-m1-kind"


def test_linux_uses_the_default_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    _platform(monkeypatch, "linux")
    assert kind_lab.docker_context() is None
    assert "DOCKER_CONTEXT" not in kind_lab.docker_env()


def test_context_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    _platform(monkeypatch, "linux")
    monkeypatch.setenv("OPSPILOT_LAB_DOCKER_CONTEXT", "remote-lab")
    assert kind_lab.docker_env()["DOCKER_CONTEXT"] == "remote-lab"


def test_holmes_docker_env_follows_the_lab(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("F14_DOCKER_CONTEXT", raising=False)
    _platform(monkeypatch, "linux")
    assert "DOCKER_CONTEXT" not in hrun.docker_env()
    _platform(monkeypatch, "darwin")
    assert hrun.docker_env()["DOCKER_CONTEXT"] == "colima-m1-kind"


# --- host memory

MEMINFO = """MemTotal:       16367616 kB
MemFree:        15771648 kB
MemAvailable:   15835136 kB
Buffers:           30000 kB
SwapTotal:       8388604 kB
SwapFree:        8388604 kB
"""


def test_linux_memory_reads_meminfo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(MEMINFO)
    monkeypatch.setattr(kind_lab, "MEMINFO", meminfo)
    _platform(monkeypatch, "linux")

    def no_command(*args: object, **kwargs: object) -> None:
        raise AssertionError("Linux memory check must not run a command")

    monkeypatch.setattr(kind_lab, "capture", no_command)
    assert kind_lab.host_reclaimable_gib() == pytest.approx(15835136 / 2**20)
    assert "8191" in kind_lab.host_swap_summary()


def test_linux_memory_unknown_without_meminfo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(kind_lab, "MEMINFO", tmp_path / "absent")
    _platform(monkeypatch, "linux")
    assert kind_lab.host_reclaimable_gib() is None


def test_macos_memory_still_uses_vm_stat(monkeypatch: pytest.MonkeyPatch) -> None:
    _platform(monkeypatch, "darwin")
    seen: list[list[str]] = []
    vm_stat = (
        "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
        "Pages free:  65536.\nPages inactive:  65536.\n"
    )

    def fake(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, vm_stat, "")

    monkeypatch.setattr(kind_lab, "capture", fake)
    assert kind_lab.host_reclaimable_gib() == pytest.approx(2.0)
    assert seen == [["vm_stat"]]


# --- host address (webhook + OpsPilot scrape targets)

GATEWAYS = (
    '[{"Subnet":"fc00:f853:ccd:e793::/64","Gateway":"fc00:f853:ccd:e793::1"},'
    '{"Subnet":"172.18.0.0/16","Gateway":"172.18.0.1"}]'
)


def test_host_address_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _platform(monkeypatch, "darwin")
    assert kind_lab.lab_host_address() == "192.168.5.2"
    _platform(monkeypatch, "linux")
    seen: list[list[str]] = []

    def fake(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, GATEWAYS + "\n", "")

    monkeypatch.setattr(kind_lab, "capture", fake)
    assert kind_lab.lab_host_address() == "172.18.0.1"
    assert seen and seen[0][:4] == ["docker", "network", "inspect", "kind"]
    monkeypatch.setenv("OPSPILOT_LAB_HOST_ADDRESS", "10.0.0.7")
    assert kind_lab.lab_host_address() == "10.0.0.7"


def test_host_address_refuses_without_kind_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(
        kind_lab,
        "capture",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "no such network"),
    )
    with pytest.raises(SystemExit):
        kind_lab.lab_host_address()


def test_host_address_rejects_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPSPILOT_LAB_HOST_ADDRESS", "1.2.3.4/evil")
    with pytest.raises(SystemExit):
        kind_lab.lab_host_address()


@pytest.mark.parametrize("name", ["values.yaml", "alertmanager-values.yaml"])
def test_colima_address_passes_committed_values(name: str) -> None:
    assert kind_lab.lab_values_file(name, "192.168.5.2") == kind_lab.CONFIG / name


def test_other_address_renders_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(kind_lab, "LAB_DIR", tmp_path)
    am = kind_lab.lab_values_file("alertmanager-values.yaml", "172.18.0.1")
    assert tmp_path in am.parents
    text = am.read_text()
    assert "192.168.5.2" not in text
    hooks = [
        hook["url"]
        for receiver in yaml.safe_load(text)["config"]["receivers"]
        for hook in receiver.get("webhook_configs", [])
    ]
    assert hooks == ["http://172.18.0.1:8080/intake/alertmanager"]
    committed = (kind_lab.CONFIG / "alertmanager-values.yaml").read_text()
    assert text == committed.replace("192.168.5.2", "172.18.0.1")

    demo = kind_lab.lab_values_file("values.yaml", "172.18.0.1")
    scrape = yaml.safe_load(
        yaml.safe_load(demo.read_text())["prometheus"]["extraScrapeConfigs"]
    )
    targets = [t for job in scrape for c in job["static_configs"] for t in c["targets"]]
    assert targets == ["172.18.0.1:19464", "172.18.0.1:19465", "172.18.0.1:19466"]


# --- no colima on Linux


def _record(
    monkeypatch: pytest.MonkeyPatch, clusters: str = "", returncode: int = 1
) -> list[list[str]]:
    seen: list[list[str]] = []

    def fake(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(list(cmd))
        if cmd[:3] == ["kind", "get", "clusters"]:
            return subprocess.CompletedProcess(cmd, returncode, clusters, "stop here")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(kind_lab, "capture", fake)
    monkeypatch.setattr(kind_lab, "run", fake)
    return seen


def test_linux_up_never_calls_colima(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(MEMINFO)
    monkeypatch.setattr(kind_lab, "MEMINFO", meminfo)
    _platform(monkeypatch, "linux")
    seen = _record(monkeypatch)
    args = argparse.Namespace(skip_memory_check=False, wait=1)
    assert kind_lab.cmd_up(args) != 0  # stopped at ``kind get clusters``
    assert seen and not any(cmd[0] == "colima" for cmd in seen)
    assert ["kind", "get", "clusters"] in [cmd[:3] for cmd in seen]


def test_linux_up_refuses_low_memory_before_creating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 16367616 kB\nMemAvailable: 1048576 kB\n")
    monkeypatch.setattr(kind_lab, "MEMINFO", meminfo)
    _platform(monkeypatch, "linux")
    seen = _record(monkeypatch, clusters="", returncode=0)
    args = argparse.Namespace(skip_memory_check=False, wait=1)
    assert kind_lab.cmd_up(args) == 3
    assert not any(cmd[:3] == ["kind", "create", "cluster"] for cmd in seen)


def test_linux_up_starts_a_stopped_node(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(kind_lab, "LAB_DIR", tmp_path)
    monkeypatch.setattr(kind_lab, "KUBECONFIG", tmp_path / "kubeconfig")
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal: 16367616 kB\nMemAvailable: 1048576 kB\n")
    monkeypatch.setattr(kind_lab, "MEMINFO", meminfo)
    _platform(monkeypatch, "linux")
    seen: list[list[str]] = []

    def fake(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(list(cmd))
        if cmd[:3] == ["kind", "get", "clusters"]:
            return subprocess.CompletedProcess(cmd, 0, "opspilot-m1\n", "")
        # stop the walk at the kubeconfig export
        code = 7 if cmd[:3] == ["kind", "export", "kubeconfig"] else 0
        return subprocess.CompletedProcess(cmd, code, "", "")

    monkeypatch.setattr(kind_lab, "capture", fake)
    monkeypatch.setattr(kind_lab, "run", fake)
    args = argparse.Namespace(skip_memory_check=False, wait=1)
    # an existing cluster skips the memory check, like a running colima VM
    assert kind_lab.cmd_up(args) == 7
    assert ["docker", "start", "opspilot-m1-control-plane"] in seen
    assert not any(cmd[0] == "colima" for cmd in seen)


def test_linux_stop_stops_the_node_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _platform(monkeypatch, "linux")
    seen = _record(monkeypatch)
    assert kind_lab.cmd_stop(argparse.Namespace()) == 0
    assert seen == [["docker", "stop", "opspilot-m1-control-plane"]]


def test_macos_stop_still_stops_colima(monkeypatch: pytest.MonkeyPatch) -> None:
    _platform(monkeypatch, "darwin")
    seen: list[list[str]] = []

    def fake(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(list(cmd))
        out = '{"name":"m1-kind","status":"Running"}\n' if cmd[0] == "colima" else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")

    monkeypatch.setattr(kind_lab, "capture", fake)
    monkeypatch.setattr(kind_lab, "run", fake)
    assert kind_lab.cmd_stop(argparse.Namespace()) == 0
    assert ["colima", "stop", "m1-kind"] in seen


def test_holmes_clock_reads_host_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    _platform(monkeypatch, "linux")

    def no_colima(cmd: list[str], **kwargs: object) -> None:
        raise AssertionError(f"unexpected command {cmd}")

    monkeypatch.setattr("scripts.holmes_baseline.run.subprocess.run", no_colima)
    monkeypatch.setattr(hrun, "docker", lambda *a, **k: "1.0\n")
    monkeypatch.setattr("scripts.holmes_baseline.run.time.time", lambda: 1.0)
    skew = hrun.clock_skew()
    assert skew["host_vm"] == 0.0
    assert skew["host_container"] == 0.0


def test_holmes_clock_keeps_colima_ssh_on_macos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _platform(monkeypatch, "darwin")
    seen: list[list[str]] = []

    def fake(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "1.5\n", "")

    monkeypatch.setattr("scripts.holmes_baseline.run.subprocess.run", fake)
    monkeypatch.setattr(hrun, "docker", lambda *a, **k: "1.0\n")
    monkeypatch.setattr("scripts.holmes_baseline.run.time.time", lambda: 1.0)
    assert hrun.clock_skew()["host_vm"] == 0.5
    assert seen == [["colima", "ssh", "-p", "m1-kind", "--", "date", "+%s.%N"]]


# --- the documented Linux installer


def test_installer_is_strict_bash() -> None:
    text = INSTALLER.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in text
    assert INSTALLER.stat().st_mode & 0o111
    proc = subprocess.run(["bash", "-n", str(INSTALLER)], capture_output=True)
    assert proc.returncode == 0, proc.stderr


def test_installer_pins_versions_and_checksums() -> None:
    text = INSTALLER.read_text()
    for name in ("KIND", "KUBECTL", "HELM"):
        assert re.search(rf'^{name}_VERSION="v\d+\.\d+\.\d+"$', text, re.M), name
        assert re.search(rf'^{name}_SHA256="[0-9a-f]{{64}}"$', text, re.M), name
    assert re.search(r'^DOCKER_VERSION="5:\d+\.\d+\.\d+-', text, re.M)
    assert re.search(r'^DOCKER_GPG_SHA256="[0-9a-f]{64}"$', text, re.M)
    assert "sha256sum -c" in text or "sha256sum --check" in text
    # no unpinned "latest" downloads or curl | sh installers
    assert "latest" not in text
    assert not re.search(r"curl[^\n|]*\|\s*(sudo\s+)?(ba)?sh\b", text)


def test_installer_leaves_firewall_and_ports_alone() -> None:
    text = INSTALLER.read_text()
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "ufw" not in code
    assert "iptables" not in code
    assert "kind_lab.py up" in code
    assert "x86_64" in code
