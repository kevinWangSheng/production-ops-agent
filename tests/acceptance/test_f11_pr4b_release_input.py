"""Independent acceptance tests for F11 PR-4b without PostgreSQL: the v6 input
names and boundary, the release Run's time frame, the consumer's receipt and
its place in the web process, and the consumer's code face (no release gate
writes).

Written from ``docs/tasks/2026-10-21-f11-pr4-contract.md`` (P4-02, P4-05, R3,
R8, section 4 public interfaces, section 6 item 12) without reading the
implementation. The PostgreSQL behaviour is in
``tests/integration/test_f11_pr4b_consume_postgres.py``.
"""

from __future__ import annotations

import ast
import functools
import importlib
import inspect
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from opspilot.investigation import context as context_module
from opspilot.investigation import loop as loop_module
from opspilot.web import Workbench

ROOT = Path(__file__).resolve().parents[2]
FRAME = timedelta(hours=24)  # R3
LOOKBACK = timedelta(seconds=3600)  # R3
DISPOSITIONS = {"created", "attached", "created_related", "handoff_created"}

try:  # PR-4b public names (contract section 4)
    from opspilot.investigation.context import (
        INPUT_VERSION_RELEASE,
        RELEASE_CONTEXT_BOUNDARY,
        RELEASE_OBSERVATION,
    )

    _PR4B_MISSING: str | None = None
except ImportError as exc:  # implementation not merged yet: fail, do not error
    _PR4B_MISSING = f"{type(exc).__name__}: {exc}"


def _release_anchor() -> Any:
    for module in ("opspilot.investigation.inputs", "opspilot.investigation.context"):
        found = getattr(importlib.import_module(module), "ReleaseAnchor", None)
        if found is not None:
            return found
    pytest.fail("ReleaseAnchor is in neither context.py nor inputs.py", pytrace=False)


def _receipt_type() -> Any:
    for module in (
        "opspilot.observation.release_types",
        "opspilot.observation.release_commands",
        "opspilot.observation.releases",
    ):
        found = getattr(importlib.import_module(module), "ReleaseLinkReceipt", None)
        if found is not None:
            return found
    pytest.fail("ReleaseLinkReceipt is not importable", pytrace=False)


# ===========================================================================
# P4-05: the v6 input and its labelled boundary
# ===========================================================================


def test_v6_is_a_known_input_version_and_v1_to_v5_still_read() -> None:
    assert INPUT_VERSION_RELEASE == "opspilot-investigation-input-v6"
    assert RELEASE_OBSERVATION == "release_observation"
    known = context_module.KNOWN_INPUT_VERSIONS
    assert INPUT_VERSION_RELEASE in known
    for n in range(1, 6):
        assert f"opspilot-investigation-input-v{n}" in known


def test_the_release_boundary_labels_the_fact_as_untrusted_data() -> None:
    assert (
        isinstance(RELEASE_CONTEXT_BOUNDARY, str) and RELEASE_CONTEXT_BOUNDARY.strip()
    )
    text = RELEASE_CONTEXT_BOUNDARY.lower()
    assert "untrusted" in text
    assert "instruction" in text  # "not instructions", as K5 and knowledge
    # a boundary of its own, not a reuse of the alert or knowledge one
    assert RELEASE_CONTEXT_BOUNDARY not in {
        context_module.ALERT_CONTEXT_BOUNDARY,
        context_module.KNOWLEDGE_BOUNDARY,
    }


def test_the_release_boundary_is_part_of_prompt_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = loop_module.prompt_revision_versions()
    changed = RELEASE_CONTEXT_BOUNDARY + " (changed)"
    monkeypatch.setattr(context_module, "RELEASE_CONTEXT_BOUNDARY", changed)
    monkeypatch.setattr(loop_module, "RELEASE_CONTEXT_BOUNDARY", changed, raising=False)

    after = loop_module.prompt_revision_versions()

    assert after["prompt_revision"] != before["prompt_revision"]


# ===========================================================================
# R3: ReleaseAnchor reuses the J1-J3 frame semantics
# ===========================================================================


def _anchor_parts(anchor: Any) -> tuple[Any, Any, Any]:
    names = ("frame", "anchor", "default_query_window")
    missing = [n for n in names if not hasattr(anchor, n)]
    if missing:
        pytest.skip(
            "contract gap G7: ReleaseAnchor's accessors are not named in the "
            f"contract (AlertAnchor has {names}); missing {missing}"
        )
    moment = anchor.anchor
    moment = moment[0] if isinstance(moment, tuple) else moment
    return tuple(anchor.frame), moment, tuple(anchor.default_query_window)


def test_r3_the_frame_ends_at_consumption_and_the_anchor_is_released_at() -> None:
    consumed = datetime(2026, 10, 22, 12, 0, 30, 750000, tzinfo=timezone.utc)
    released = consumed - timedelta(minutes=10, microseconds=250000)
    anchor = _release_anchor()(released_at=released, consumed_at=consumed)

    (start, end), moment, (default_start, default_end) = _anchor_parts(anchor)

    assert end == consumed.replace(microsecond=0)
    assert end - start == FRAME
    assert moment == released.replace(microsecond=0)
    assert default_start == moment - LOOKBACK
    assert default_end == end


def test_r3_an_anchor_outside_the_frame_is_clamped_as_j2() -> None:
    consumed = datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)
    early = _release_anchor()(
        released_at=consumed - timedelta(hours=30), consumed_at=consumed
    )
    (start, end), moment, (default_start, _) = _anchor_parts(early)
    assert moment == start
    assert default_start == start  # not before the frame (J3)

    late = _release_anchor()(
        released_at=consumed + timedelta(minutes=5), consumed_at=consumed
    )
    (_, end), moment, _ = _anchor_parts(late)
    assert moment == end


# ===========================================================================
# section 4: the consumer's receipt and entry point
# ===========================================================================


def test_the_link_receipt_carries_the_contract_fields_and_dispositions() -> None:
    receipt_type = _receipt_type()
    fields = {
        "observation_id": uuid4(),
        "ending_id": uuid4(),
        "anomaly_key": "release-anomaly:" + str(uuid4()),
        "disposition": "created",
        "incident_id": uuid4(),
        "related_incident_id": None,
        "run_id": uuid4(),
        "input_id": None,
        "replayed": False,
    }
    for disposition in DISPOSITIONS:
        receipt = receipt_type(**{**fields, "disposition": disposition})
        assert receipt.disposition == disposition
        for name, value in fields.items():
            if name != "disposition":
                assert getattr(receipt, name) == value, name
    with pytest.raises((ValueError, TypeError)):
        receipt_type(**{**fields, "disposition": "merged"})


def test_the_pass_is_a_workbench_method_with_a_bounded_default_limit() -> None:
    method = getattr(Workbench, "release_anomaly_pass", None)
    assert callable(method), "Workbench.release_anomaly_pass is missing"
    limit = inspect.signature(method).parameters["limit"]
    assert limit.kind is inspect.Parameter.KEYWORD_ONLY
    assert limit.default == 20


def test_r8_the_self_heal_thread_runs_the_release_pass_after_self_heal() -> None:
    """P4-02/R8: same background thread as ``self_heal_pass``, after it, each
    pass's failure caught on its own."""
    from opspilot.web.__main__ import self_heal_loop

    calls: list[str] = []
    stop = threading.Event()

    class Bench:
        def self_heal_pass(self, **_: Any) -> list[Any]:
            calls.append("self_heal")
            raise RuntimeError("self-heal storage outage")

        def release_anomaly_pass(self, **_: Any) -> list[Any]:
            calls.append("release")
            if calls.count("release") >= 2:
                stop.set()
            raise RuntimeError("release storage outage")

    thread = threading.Thread(
        target=self_heal_loop, args=(Bench(), 0.01, stop), daemon=True
    )
    thread.start()
    thread.join(timeout=10)
    stop.set()  # a loop that never reaches the release pass stops here
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert calls[:4] == ["self_heal", "release", "self_heal", "release"], calls[:8]


# ===========================================================================
# item 12: the consumer's code face has no release-gate client
# ===========================================================================

#: The 4b files the contract allows (section 5) that the consumer runs in.
CONSUMER_FILES = (
    "opspilot/observation/release_commands.py",
    "opspilot/observation/release_types.py",
    "opspilot/persistence/controls.py",
    "opspilot/persistence/incidents.py",
    "opspilot/persistence/correlation.py",
    "opspilot/web/service.py",
)
#: Anything that could reach Kubernetes, CI, a traffic policy or any other
#: HTTP API, or launch a process that does.
FORBIDDEN_MODULES = {
    "kubernetes",
    "subprocess",
    "socket",
    "http",
    "urllib",
    "requests",
    "httpx",
    "aiohttp",
    "urllib3",
    "grpc",
}


def test_item_12_the_consumer_modules_import_no_network_or_process_client() -> None:
    found: dict[str, list[str]] = {}
    for relative in CONSUMER_FILES:
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in FORBIDDEN_MODULES:
                    found.setdefault(relative, []).append(name)
    assert found == {}


# ===========================================================================
# red while the implementation is missing: fail, do not error
# ===========================================================================


def _fail_while_missing(test: Any) -> Any:
    @functools.wraps(test)
    def run(*args: Any, **kwargs: Any) -> None:
        if _PR4B_MISSING is not None:
            pytest.fail(f"PR4B_INTERFACE_MISSING: {_PR4B_MISSING}", pytrace=False)
        test(*args, **kwargs)

    return run


for _name in [n for n in list(globals()) if n.startswith("test_")]:
    if _name != "test_item_12_the_consumer_modules_import_no_network_or_process_client":
        globals()[_name] = _fail_while_missing(globals()[_name])
