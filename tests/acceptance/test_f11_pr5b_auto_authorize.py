"""Independent acceptance tests for F11 PR-5b without PostgreSQL: where the
automatic release authorization pass lives and how the web process's
background thread runs it.

Written from the F11 PR-5 contract (``docs/tasks/2026-10-24-f11-pr5-contract.md``
r1: U-1 = A, D7, categories 6-8) without reading the PR-5b implementation.
The store-backed behaviour (intake -> one pass -> observing, the refusals,
idempotence, interval 0 in ``_serve``) is in
``tests/integration/test_f11_pr5b_auto_authorize_postgres.py``.

Assumed names (the contract fixes the behaviour, not every spelling):

* A1 (D7) the pass is ``Workbench.release_authorize_pass`` with a
  keyword-only ``limit`` defaulting to 20 ("单轮上限 20"), like its siblings
  ``self_heal_pass`` and ``release_anomaly_pass``.
* A2 (D7) the background thread is still ``opspilot.web.__main__.self_heal_loop``
  (``workbench, interval, stop``); it calls the three passes without
  arguments.
"""

from __future__ import annotations

import inspect
import threading
from typing import Any

import pytest

from opspilot.web import Workbench
from opspilot.web import __main__ as web_main

PASSES = ("self_heal_pass", "release_authorize_pass", "release_anomaly_pass")


def test_d7_the_pass_is_a_workbench_method_with_a_bounded_default_limit() -> None:
    method = getattr(Workbench, "release_authorize_pass", None)
    assert callable(method), "Workbench.release_authorize_pass is missing"
    limit = inspect.signature(method).parameters["limit"]
    assert limit.kind is inspect.Parameter.KEYWORD_ONLY
    assert limit.default == 20


class _Bench:
    """Records every pass call with its thread; ``failing`` passes raise."""

    def __init__(self, failing: set[str], stop: threading.Event, rounds: int) -> None:
        self.failing = failing
        self.stop = stop
        self.rounds = rounds
        self.calls: list[tuple[str, int]] = []
        self._lock = threading.Lock()

    def _record(self, name: str) -> list[Any]:
        with self._lock:
            self.calls.append((name, threading.get_ident()))
            if sum(1 for n, _ in self.calls if n == "release_anomaly_pass") >= (
                self.rounds
            ):
                self.stop.set()
        if name in self.failing:
            raise RuntimeError(f"{name} storage outage")
        return []

    def self_heal_pass(self, **_: Any) -> list[Any]:
        return self._record("self_heal_pass")

    def release_authorize_pass(self, **_: Any) -> list[Any]:
        return self._record("release_authorize_pass")

    def release_anomaly_pass(self, **_: Any) -> list[Any]:
        return self._record("release_anomaly_pass")


def _run_loop(failing: set[str], rounds: int = 3) -> tuple[_Bench, int]:
    stop = threading.Event()
    bench = _Bench(failing, stop, rounds)
    thread = threading.Thread(
        target=web_main.self_heal_loop, args=(bench, 0.01, stop), daemon=True
    )
    thread.start()
    thread.join(timeout=10)
    stop.set()  # a loop that never reaches the anomaly pass stops here
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert thread.ident is not None
    return bench, thread.ident


def _rounds(calls: list[tuple[str, int]]) -> list[list[str]]:
    """Split the call log at each anomaly pass (the round's last pass)."""
    rounds: list[list[str]] = []
    current: list[str] = []
    for name, _ in calls:
        current.append(name)
        if name == "release_anomaly_pass":
            rounds.append(current)
            current = []
    return rounds


@pytest.mark.parametrize(
    "failing",
    [
        set(),
        {"release_authorize_pass"},
        {"self_heal_pass", "release_anomaly_pass"},
        set(PASSES),
    ],
    ids=["none-fail", "authorize-fails", "others-fail", "all-fail"],
)
def test_d7_every_round_runs_all_three_passes_each_catching_its_own_failure(
    failing: set[str],
) -> None:
    bench, _ = _run_loop(failing, rounds=3)

    rounds = _rounds(bench.calls)
    assert len(rounds) >= 3, bench.calls
    for names in rounds[:3]:
        assert sorted(names) == sorted(PASSES), names


def test_d7_the_authorize_pass_runs_before_the_anomaly_pass_in_each_round() -> None:
    bench, _ = _run_loop(set(), rounds=2)

    for names in _rounds(bench.calls)[:2]:
        assert names.index("release_authorize_pass") < names.index(
            "release_anomaly_pass"
        ), names


def test_d7_the_three_passes_run_on_the_same_background_thread() -> None:
    bench, ident = _run_loop({"release_authorize_pass"}, rounds=2)

    threads = {thread for _, thread in bench.calls}
    assert threads == {ident}, bench.calls
    assert ident != threading.get_ident()


def test_d7_a_set_stop_runs_no_authorize_pass() -> None:
    stop = threading.Event()
    stop.set()
    bench = _Bench(set(), stop, rounds=1)

    web_main.self_heal_loop(bench, 30, stop)  # type: ignore[arg-type]

    assert bench.calls == []
