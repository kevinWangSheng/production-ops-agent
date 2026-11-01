"""Shared opt-in external acceptance fixtures."""

import logging
import sys
import threading

import pytest

pytest_plugins = ("tests.f6_fixtures",)


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
