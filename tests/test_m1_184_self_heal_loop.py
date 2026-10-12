"""The web process's self-heal background loop (#184 step 1, S1-D1)."""

from __future__ import annotations

import threading

import pytest

from opspilot.web import __main__ as web_main


class _Workbench:
    def __init__(self, fail_first: int) -> None:
        self.calls = 0
        self.fail_first = fail_first
        self.reached = threading.Event()

    def self_heal_pass(self) -> list[object]:
        self.calls += 1
        if self.calls >= self.fail_first + 2:
            self.reached.set()
        if self.calls <= self.fail_first:
            raise RuntimeError("database down")
        return []


def test_a_failed_pass_is_logged_and_the_next_one_runs() -> None:
    workbench = _Workbench(fail_first=2)
    stop = threading.Event()
    thread = threading.Thread(
        target=web_main.self_heal_loop,
        args=(workbench, 0.01, stop),
        daemon=True,
    )
    thread.start()
    assert workbench.reached.wait(5)
    stop.set()
    thread.join(5)
    assert not thread.is_alive()
    assert workbench.calls >= 4


def test_a_set_stop_ends_the_loop_before_any_pass() -> None:
    workbench = _Workbench(fail_first=0)
    stop = threading.Event()
    stop.set()
    web_main.self_heal_loop(workbench, 30, stop)  # type: ignore[arg-type]
    assert workbench.calls == 0


@pytest.mark.parametrize(
    ("raw", "expected"), [(None, 30.0), ("0", 0.0), ("2.5", 2.5), ("30", 30.0)]
)
def test_the_interval_comes_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: float
) -> None:
    if raw is None:
        monkeypatch.delenv("OPSPILOT_SELF_HEAL_INTERVAL_SECONDS", raising=False)
    else:
        monkeypatch.setenv("OPSPILOT_SELF_HEAL_INTERVAL_SECONDS", raw)
    assert web_main._self_heal_interval() == expected


@pytest.mark.parametrize("raw", ["-1", "soon", "nan", "inf"])
def test_an_invalid_interval_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("OPSPILOT_SELF_HEAL_INTERVAL_SECONDS", raw)
    with pytest.raises(SystemExit):
        web_main._self_heal_interval()
