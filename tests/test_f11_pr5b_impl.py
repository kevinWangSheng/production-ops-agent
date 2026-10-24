"""F11 PR-5b D7: the release authorization pass in the web background loop."""

from __future__ import annotations

import threading

from opspilot.web import __main__ as web_main


class _Workbench:
    def __init__(self, failing: str) -> None:
        self.calls: list[str] = []
        self.failing = failing
        self.done = threading.Event()

    def _pass(self, name: str) -> list[object]:
        self.calls.append(name)
        if name == "release_anomaly_pass" and len(self.calls) >= 6:
            self.done.set()
        if name == self.failing:
            raise RuntimeError("database down")
        return []

    def self_heal_pass(self) -> list[object]:
        return self._pass("self_heal_pass")

    def release_authorize_pass(self) -> list[object]:
        return self._pass("release_authorize_pass")

    def release_anomaly_pass(self) -> list[object]:
        return self._pass("release_anomaly_pass")


def _two_rounds(failing: str) -> list[str]:
    workbench = _Workbench(failing)
    stop = threading.Event()
    thread = threading.Thread(
        target=web_main.self_heal_loop, args=(workbench, 0.01, stop), daemon=True
    )
    thread.start()
    assert workbench.done.wait(5)
    stop.set()
    thread.join(5)
    assert not thread.is_alive()
    return workbench.calls[:6]


ROUND = ["self_heal_pass", "release_authorize_pass", "release_anomaly_pass"]


def test_authorization_runs_after_self_heal_and_before_the_anomaly_pass() -> None:
    assert _two_rounds(failing="") == ROUND * 2


def test_each_pass_failure_is_its_own() -> None:
    for failing in ROUND:
        assert _two_rounds(failing=failing) == ROUND * 2
