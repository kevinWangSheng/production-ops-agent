"""F2 PR-B2 public-interface acceptance checks.

These checks are intentionally independent of the PostgreSQL behaviour tests:
they pin the names, signatures and event contract from section 3 of the B2
contract.  The implementation may be absent while these tests are authored.
"""

from __future__ import annotations

import inspect

from opspilot.investigation import progress
from opspilot.persistence import DurableStore


def test_b2_store_and_progress_interfaces_are_public_and_keyword_safe():
    assert hasattr(DurableStore, "park_backlog"), "DurableStore.park_backlog"
    method = DurableStore.park_backlog
    parameters = inspect.signature(method).parameters
    assert list(parameters) == ["self", "max_queued", "limit"]
    assert parameters["max_queued"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["limit"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["limit"].default == 100

    assert getattr(progress, "BACKLOG_FULL", None) == "BACKLOG_FULL"
    for name, expected in {
        "announce_backlog_full": [
            "log",
            "subject_id",
            "run_id",
            "control_generation",
        ],
        "park_backlog": ["store", "log", "max_queued"],
    }.items():
        assert hasattr(progress, name), f"progress.{name}"
        assert list(inspect.signature(getattr(progress, name)).parameters) == expected
