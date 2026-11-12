"""Shared opt-in external acceptance fixtures."""

import logging
import sys
import threading

import pytest

pytest_plugins = ("tests.f6_fixtures",)

# Test files whose assertions depend on thread timing. CI runs them after the
# parallel pass with no other workers competing for the CPU (#519).
SERIAL_FILES = frozenset(
    {
        "tests/test_m1_web_worker.py",  # #447: stop-before-next-claim race
    }
)


def pytest_collection_modifyitems(config, items):
    root = config.rootpath
    for item in items:
        if item.path.relative_to(root).as_posix() in SERIAL_FILES:
            item.add_marker(pytest.mark.serial)


def _utf8_safe(text):
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Under pytest-xdist a worker ships each report to the controller as
    UTF-8; captured output holding a lone surrogate (tests that log one on
    purpose) would abort the worker. Escape it in the captured sections only;
    outcomes are untouched. Serial runs are unaffected (#519)."""
    outcome = yield
    if not hasattr(item.config, "workerinput"):
        return
    report = outcome.get_result()
    report.sections = [
        (title, _utf8_safe(content)) for title, content in report.sections
    ]
    if isinstance(report.longrepr, str):
        report.longrepr = _utf8_safe(report.longrepr)


@pytest.fixture(autouse=True)
def _undo_process_logging():
    """Tests that call an entry point's ``main()`` in-process run
    ``opspilot.logs.configure()``; undo it so the root level, handler and
    exception hooks do not leak into later tests (F8L-R9)."""
    root = logging.getLogger()
    level = root.level
    hooks = (sys.excepthook, threading.excepthook)
    yield
    from opspilot import logs

    for handler in list(root.handlers):
        if isinstance(handler, logs._Handler):
            root.removeHandler(handler)
    root.setLevel(level)
    sys.excepthook, threading.excepthook = hooks
