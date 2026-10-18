"""Implementer unit tests for eval PR-C (live kind/OTel scenario driver).

Interface: docs/tasks/2026-10-18-eval-pr-c-interface.md. Offline only: the
lab, PostgreSQL and model are fakes; the independent acceptance tests live
under tests/acceptance/ and are written from the interface, not from here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import kind_lab
from tests.test_kind_lab_fault import _fake_kubectl

FLAGS = {
    "flags": {
        "paymentFailure": {
            "state": "ENABLED",
            "variants": {"100%": 1, "90%": 0.95, "off": 0},
            "defaultVariant": "off",
        },
        "productCatalogFailure": {
            "state": "ENABLED",
            "variants": {"on": True, "off": False},
            "defaultVariant": "off",
        },
        "cartFailure": {
            "state": "ENABLED",
            "variants": {"on": True, "off": False},
            "defaultVariant": "off",
        },
        "adFailure": {
            "state": "ENABLED",
            "variants": {"on": True, "off": False},
            "defaultVariant": "off",
        },
    }
}


def _raw(data: dict = FLAGS) -> bytes:
    return json.dumps(data, indent=2).encode()


# -- A2: kind_lab fault --flag ---------------------------------------------------


@pytest.mark.parametrize("flag", sorted(kind_lab.FAULT_FLAGS))
def test_mutate_moves_only_the_named_whitelisted_flag(flag: str) -> None:
    variant = kind_lab.FAULT_FLAGS[flag]
    out = json.loads(kind_lab.mutate_flags(_raw(), variant, "off", flag=flag))
    assert out["flags"][flag]["defaultVariant"] == variant
    for other, entry in FLAGS["flags"].items():
        if other != flag:
            assert out["flags"][other] == entry


def test_mutate_refuses_a_flag_outside_the_whitelist() -> None:
    with pytest.raises(SystemExit):
        kind_lab.mutate_flags(_raw(), "on", "off", flag="adFailure")


def test_mutate_refuses_a_whitelisted_flag_missing_from_the_file() -> None:
    data = json.loads(_raw())
    del data["flags"]["cartFailure"]
    with pytest.raises(SystemExit):
        kind_lab.mutate_flags(_raw(data), "on", "off", flag="cartFailure")


def test_mutate_refuses_a_variant_the_file_does_not_define() -> None:
    data = json.loads(_raw())
    data["flags"]["cartFailure"]["variants"] = {"off": False}
    with pytest.raises(SystemExit):
        kind_lab.mutate_flags(_raw(data), "on", "off", flag="cartFailure")


def test_cli_flag_injects_its_whitelisted_variant_and_restores(
    tmp_path: Path, monkeypatch
) -> None:
    state = tmp_path / "live.json"
    state.write_text(json.dumps(FLAGS, indent=2) + "\n")
    kubectl = _fake_kubectl(tmp_path / "kubectl", state, apply_patches=True)
    monkeypatch.setattr(kind_lab, "HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setattr(kind_lab, "KUBECTL", str(kubectl))
    argv = ["fault", "inject", "--experiment-id", "e1", "--flag", "cartFailure"]
    assert kind_lab.main(argv) == 0
    live = json.loads(state.read_text())["flags"]
    assert live["cartFailure"]["defaultVariant"] == "on"
    assert live["paymentFailure"]["defaultVariant"] == "off"
    assert kind_lab.main(["fault", "restore", "--experiment-id", "e1"]) == 0
    assert json.loads(state.read_text()) == FLAGS
    log = [
        json.loads(line)
        for line in (tmp_path / "history/e1/fault-log.jsonl").read_text().splitlines()
    ]
    assert [row["flag"] for row in log] == ["cartFailure", "cartFailure"]


def test_cli_refuses_a_flag_outside_the_whitelist() -> None:
    with pytest.raises(SystemExit):
        kind_lab.main(["fault", "inject", "--flag", "adFailure"])


# -- A5: public registration DTO -------------------------------------------------


def test_registration_dto_is_what_the_executor_factory_registers() -> None:
    from opspilot.tools import otel_demo
    from opspilot.tools.registry import TargetRegistry, ToolRegistry

    dto = otel_demo.otel_demo_registrations()
    assert dto.tools == otel_demo._registrations()
    assert dto.targets == (otel_demo._target(otel_demo.OtelDemoConfig()),)
    assert (
        ToolRegistry(dto.tools).revision
        == ToolRegistry(otel_demo._registrations()).revision
    )
    assert TargetRegistry(list(dto.targets)).revision
    with pytest.raises(AttributeError):
        dto.tools = ()  # type: ignore[misc]
