"""Shared opt-in external acceptance fixtures."""

import inspect
import logging
import os
import pathlib
import re
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


# Gates that mean "needs the shared lab PostgreSQL the CI PG jobs provide".
# A test is marked ``pg`` when its module, or any fixture it uses, reads one of
# these, or when it lives under tests/integration. CI selects PG work with
# ``-m pg`` instead of grepping file text, so a file gated only through a
# driver fixture (tests/workbench_driver.py) is still selected.
_PG_GATE = re.compile(r"M1_DURABLE_POSTGRES|M0_B_POSTGRES|OPSPILOT_TEST_DATABASE_URL")
# A skip for one of these reasons outside a PG job means the test escaped the
# ``pg`` selection; inside a PG job it means the job lacks the gate variable.
_PG_SKIP_REASON = re.compile(
    r"M1_DURABLE_POSTGRES|M0_B_POSTGRES|OPSPILOT_TEST_DATABASE_URL"
    r"|explicit PG opt-in required|需要显式启用临时 PG 验收"
)
# Separate opt-ins that no CI job provides (M0 spike stores, throwaway
# clusters a test starts itself): a skip in a module gated on one of these is
# that module's own opt-in, not a hole in the ``pg`` selection.
_OWN_GATE = re.compile(
    r"M0_STEP_POSTGRES|M0_CONTROL_POSTGRES|M0_PG_OUTAGE|OPSPILOT_\d+_PG\b"
    r"|EVAL_C_PG_PORT|OPSPILOT_LITE_PG"
)
_gate_cache: dict[object, bool] = {}
_own_gate_cache: dict[object, bool] = {}


def _reads_pg_gate(key, get_source) -> bool:
    if key not in _gate_cache:
        try:
            _gate_cache[key] = bool(_PG_GATE.search(get_source()))
        except (OSError, TypeError):
            _gate_cache[key] = False
    return _gate_cache[key]


def _needs_lab_pg(item, root) -> bool:
    rel = item.path.relative_to(root).as_posix()
    if rel.startswith("tests/integration/"):
        return True
    if _reads_pg_gate(item.path, item.path.read_text):
        return True
    for mark in item.iter_markers("skipif"):
        if _PG_SKIP_REASON.search(str(mark.kwargs.get("reason", ""))):
            return True
    info = getattr(item, "_fixtureinfo", None)
    for defs in info.name2fixturedefs.values() if info else ():
        for fixturedef in defs:
            func = fixturedef.func
            code = getattr(func, "__code__", None)
            if code is None or not code.co_filename.startswith(str(root / "tests")):
                continue
            # Cheap file-level check first; only a gated file is parsed per fixture.
            source = pathlib.Path(code.co_filename)
            if _reads_pg_gate(source, source.read_text) and _reads_pg_gate(
                func, lambda f=func: inspect.getsource(f)
            ):
                return True
    return False


def pytest_collection_modifyitems(config, items):
    root = config.rootpath
    for item in items:
        if item.path.relative_to(root).as_posix() in SERIAL_FILES:
            item.add_marker(pytest.mark.serial)
        if _needs_lab_pg(item, root):
            item.add_marker(pytest.mark.pg)


def _utf8_safe(text):
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Under pytest-xdist a worker ships each report to the controller as
    UTF-8; captured output holding a lone surrogate (tests that log one on
    purpose) would abort the worker. Escape it in the captured sections only;
    outcomes are untouched. Serial runs are unaffected (#519)."""
    outcome = yield
    report = outcome.get_result()
    _guard_pg_selection(item, report)
    if not hasattr(item.config, "workerinput"):
        return
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


def _guard_pg_selection(item, report):
    """``OPSPILOT_PG_SELECTION_GUARD`` is set by CI: ``checks`` in the job that
    runs ``-m "not pg"``, ``pg`` in the PG jobs. A lab-PG skip there is a
    selection hole (the test would run nowhere), so it fails instead."""
    mode = os.environ.get("OPSPILOT_PG_SELECTION_GUARD")
    if mode not in {"checks", "pg"} or not report.skipped:
        return
    if hasattr(report, "wasxfail"):
        return
    longrepr = report.longrepr
    reason = longrepr[2] if isinstance(longrepr, tuple) else str(longrepr)
    if not _PG_SKIP_REASON.search(reason):
        return
    if item.path not in _own_gate_cache:
        _own_gate_cache[item.path] = bool(_OWN_GATE.search(item.path.read_text()))
    if _own_gate_cache[item.path]:
        return
    where = (
        "outside the pg selection (it runs in no CI job)"
        if mode == "checks"
        else "in a PG job (the job lacks the gate variable)"
    )
    report.outcome = "failed"
    report.longrepr = f"PG_SELECTION_HOLE: {item.nodeid} skipped {where}: {reason}"
