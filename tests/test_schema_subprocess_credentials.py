"""pg_dump never sees the owner password on its command line (Codex P1, PR #104)."""

import os
import pathlib
import stat
import subprocess
from typing import Any

import pytest

from opspilot import schema

PASSWORD = "s3cr:et-pw"
DSN = f"host=127.0.0.1 port=5432 dbname=m0_budget user=owner password={PASSWORD} sslmode=prefer"


class _Recorder:
    def __init__(self) -> None:
        self.argv: list[str] = []
        self.env: dict[str, str] = {}
        self.passfile_content: str | None = None
        self.passfile_mode: int | None = None

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.argv = list(argv)
        self.env = dict(kwargs["env"])
        passfile = self.env.get("PGPASSFILE")
        if passfile:
            path = pathlib.Path(passfile)
            self.passfile_content = path.read_text()
            self.passfile_mode = stat.S_IMODE(path.stat().st_mode)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def test_password_goes_through_a_private_passfile_not_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(schema.subprocess, "run", recorder)
    monkeypatch.setenv("PGPASSWORD", "leaked-from-parent")

    schema.schema_dump(DSN, pg_dump="pg_dump")

    assert recorder.argv, "pg_dump was not invoked"
    for element in recorder.argv:
        assert PASSWORD not in element, element
        assert "password" not in element, element
    dbname_arg = next(a for a in recorder.argv if a.startswith("--dbname="))
    for expected in (
        "host=127.0.0.1",
        "port=5432",
        "dbname=m0_budget",
        "user=owner",
        "sslmode=prefer",
    ):
        assert expected in dbname_arg
    assert "PGPASSWORD" not in recorder.env
    assert recorder.passfile_mode == 0o600
    assert recorder.passfile_content == "*:*:*:*:s3cr\\:et-pw\n"
    passfile = pathlib.Path(recorder.env["PGPASSFILE"])
    assert not passfile.exists() and not passfile.parent.exists()


def test_passfile_is_removed_when_pg_dump_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    def failing(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="connection refused"
        )

    monkeypatch.setattr(schema.subprocess, "run", failing)
    with pytest.raises(RuntimeError, match="connection refused") as failure:
        schema.schema_dump(DSN, pg_dump="pg_dump")
    assert PASSWORD not in str(failure.value)
    assert not pathlib.Path(seen["PGPASSFILE"]).parent.exists()


def test_dsn_without_password_leaves_the_users_pgpass_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(schema.subprocess, "run", recorder)
    monkeypatch.delenv("PGPASSFILE", raising=False)
    schema.schema_dump("host=127.0.0.1 dbname=m0_budget user=owner", pg_dump="pg_dump")
    assert "PGPASSFILE" not in recorder.env
    assert os.environ.get("PGPASSFILE") is None
