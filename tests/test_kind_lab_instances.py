"""S7c: several independent kind labs on one Linux host (``OPSPILOT_LAB_INSTANCE``)."""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from scripts import kind_lab

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def instance(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    """Reload ``kind_lab`` under a given instance; restore it afterwards so
    other tests keep instance 0."""

    def load(value: str | None, runtime: str = "docker") -> ModuleType:
        if value is None:
            monkeypatch.delenv(kind_lab.INSTANCE_ENV, raising=False)
        else:
            monkeypatch.setenv(kind_lab.INSTANCE_ENV, value)
        monkeypatch.setenv("OPSPILOT_LAB_RUNTIME", runtime)
        monkeypatch.delenv("OPSPILOT_OTEL_PROMETHEUS_URL", raising=False)
        monkeypatch.delenv("OPSPILOT_OTEL_JAEGER_URL", raising=False)
        return importlib.reload(kind_lab)

    yield load
    monkeypatch.undo()
    importlib.reload(kind_lab)


def test_instance_zero_is_the_lab_as_before(instance):
    lab = instance(None)
    assert lab.CLUSTER == "opspilot-m1"
    assert lab.KUBE_CONTEXT == "kind-opspilot-m1"
    assert lab.NODE_CONTAINER == "opspilot-m1-control-plane"
    assert lab.LAB_DIR == ROOT / "tmp/m1-kind-lab"
    assert lab.PROMETHEUS == "http://127.0.0.1:19090"
    assert lab.JAEGER == "http://127.0.0.1:16686/jaeger/ui"
    assert lab.FRONTEND == "http://127.0.0.1:18080/"
    assert lab.CONTROL_ALIASES["web"] == "opspilot-web"
    assert lab.kind_config_file() == lab.CONFIG / "kind-config.yaml"


def test_other_instances_get_their_own_names_ports_and_files(
    instance, tmp_path, monkeypatch
):
    lab = instance("2")
    assert lab.LAB_DIR == ROOT / "tmp/m1-kind-lab-i2"
    monkeypatch.setattr(lab, "LAB_DIR", tmp_path / "m1-kind-lab-i2")
    assert lab.CLUSTER == "opspilot-m1-i2"
    assert lab.KUBE_CONTEXT == "kind-opspilot-m1-i2"
    assert lab.NODE_CONTAINER == "opspilot-m1-i2-control-plane"
    assert lab.PROMETHEUS == "http://127.0.0.1:19290"
    assert lab.JAEGER == "http://127.0.0.1:16886/jaeger/ui"
    assert lab.FRONTEND == "http://127.0.0.1:18280/"
    assert lab.CONTROL_ALIASES == {
        "web": "opspilot-web-i2",
        "worker": "opspilot-worker-i2",
        "observer": "opspilot-observer-i2",
    }
    assert lab.KIND_NETWORK_ENDPOINTS["webhook"] == "opspilot-web-i2:8080"
    rendered = lab.kind_config_file()
    assert rendered == tmp_path / "m1-kind-lab-i2/rendered/kind-config.yaml"
    text = rendered.read_text()
    assert "name: opspilot-m1-i2\n" in text
    for port in ("18280", "19290", "16886"):
        assert f"hostPort: {port}\n" in text
    for port in ("18080", "19090", "16686"):
        assert f"hostPort: {port}\n" not in text
    # the in-cluster NodePorts do not move
    for port in ("30080", "30090", "30686"):
        assert f"containerPort: {port}\n" in text
    assert str(rendered) in lab.kind_create_cmd()


def test_instances_never_share_a_lab_directory(instance):
    dirs = {instance(str(n)).LAB_DIR for n in range(kind_lab.MAX_INSTANCE + 1)}
    assert len(dirs) == kind_lab.MAX_INSTANCE + 1
    assert all(d.parent == ROOT / "tmp" for d in dirs)


@pytest.mark.parametrize("value", ["-1", "4", "x", "../other", ""])
def test_instance_must_be_a_small_integer(instance, value):
    with pytest.raises(SystemExit):
        instance(value)


def test_colima_holds_one_lab_only(instance):
    lab = instance("1", runtime="colima")
    with pytest.raises(SystemExit):
        lab.kind_config_file()


def test_injector_urls_follow_the_instance(instance):
    instance("1")
    from scripts import eval_live_inject

    inject = importlib.reload(eval_live_inject)
    try:
        assert inject.OTLP_URL == "http://127.0.0.1:18180/otlp-http/v1/traces"
        assert inject.JAEGER_API == "http://127.0.0.1:16786/jaeger/ui/api/traces"
    finally:
        instance(None)
        importlib.reload(eval_live_inject)
