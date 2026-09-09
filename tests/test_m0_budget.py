from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from scripts.m0.budget import PostgresBudget
from scripts.m0.contracts import BudgetError, RequestIdentity, RunContext


def request():
    return RequestIdentity(
        RunContext(
            uuid4(),
            uuid4(),
            "deepseek",
            datetime.now(timezone.utc) + timedelta(minutes=2),
        ),
        uuid4(),
    )


@pytest.mark.parametrize("value", [True, False, 1.1, -1, "2", None])
def test_invalid_amount_fails_before_storage(value):
    ledger = PostgresBudget("invalid secret-dsn")
    for method in (ledger.reserve, ledger.settle):
        with pytest.raises(BudgetError, match="^INVALID_INPUT$"):
            method(request(), value)


def test_connection_failure_is_sanitized():
    ledger = PostgresBudget(
        "host=127.0.0.1 port=1 password=synthetic-secret connect_timeout=1"
    )
    with pytest.raises(BudgetError, match="^STORAGE_UNAVAILABLE$") as error:
        ledger.reserve(request(), 2)
    assert error.value.__context__ is None
    assert "synthetic-secret" not in repr(error.value)


def test_lab_rejects_symlink_and_unowned_cluster(tmp_path, monkeypatch):
    from scripts.m0 import postgres_lab as lab

    root = tmp_path / "root"
    data = root / "tmp/m0-b/postgres"
    data.mkdir(parents=True)
    monkeypatch.setattr(lab, "ROOT", root)
    monkeypatch.setattr(lab, "DATA", data)
    monkeypatch.setattr(lab, "SOCKET", data.parent / "socket")
    (data / "PG_VERSION").write_text("17")
    with pytest.raises(RuntimeError, match="LAB_IDENTITY_CONFLICT"):
        lab.stop()
    (data / "m0-owner").write_text(lab.MARKER)
    (data / "postmaster.pid").symlink_to(tmp_path / "another-pid")
    with pytest.raises(RuntimeError, match="LAB_PATH_UNSAFE"):
        lab.stop()
