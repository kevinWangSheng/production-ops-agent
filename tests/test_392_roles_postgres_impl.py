"""#392 init-roles on a self-created Unix-socket PG (never a shared lab):
every login it creates stores a SCRAM verifier even when the server would
encrypt with MD5, and an existing login without one is refused untouched.

M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest tests/test_392_roles_postgres_impl.py
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.deploy import roles

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)

PORT = 55473


@pytest.fixture
def owner_dsn():
    found = shutil.which("initdb")
    binary_dir = (
        Path(found).parent if found else Path("/opt/homebrew/opt/postgresql@17/bin")
    )
    if os.geteuid() == 0 or any(
        not (binary_dir / name).is_file() for name in ("initdb", "pg_ctl")
    ):
        pytest.skip("self-owned initdb binaries/user unavailable")
    with tempfile.TemporaryDirectory(
        prefix="r392-pg-",
        dir="/private/tmp" if Path("/private/tmp").is_dir() else "/tmp",
    ) as scratch:
        root = Path(scratch)
        data, socket_dir = root / "data", root / "socket"
        socket_dir.mkdir(mode=0o700)
        password = secrets.token_urlsafe(32)
        pg_env = {
            key: value
            for key, value in os.environ.items()
            if key in ("PATH", "HOME", "TMPDIR", "LANG", "TZ") or key.startswith("LC_")
        }

        def run(name, *args):
            return subprocess.run(
                [str(binary_dir / name), *map(str, args)],
                capture_output=True,
                check=True,
                timeout=30,
                env=pg_env,
            )

        pwfile = root / "initdb-password"
        with os.fdopen(
            os.open(pwfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as stream:
            stream.write(password + "\n")
        try:
            run(
                "initdb",
                "-D",
                data,
                "-U",
                "opspilot_owner",
                "--auth=scram-sha-256",
                f"--pwfile={pwfile}",
                "--no-locale",
                "--encoding=UTF8",
            )
        finally:
            pwfile.unlink(missing_ok=True)
        with (data / "postgresql.conf").open("a") as config:
            # A server that would store new passwords as MD5: init-roles
            # must not depend on this setting.
            config.write(
                f"\nlisten_addresses=''\nport={PORT}\nunix_socket_directories='{socket_dir}'\n"
                "shared_buffers=16MB\nmax_connections=12\npassword_encryption=md5\n"
            )
        try:
            run("pg_ctl", "-D", data, "-l", root / "postgres.log", "-w", "start")
            dsn = make_conninfo(
                host=str(socket_dir),
                port=PORT,
                dbname="postgres",
                user="opspilot_owner",
                password=password,
                connect_timeout=5,
            )
            with psycopg.connect(dsn) as conn:
                assert (
                    Path(conn.execute("SHOW data_directory").fetchone()[0]).resolve()
                    == data.resolve()
                )
            schema.migrate(dsn)
            yield dsn
        finally:
            if (data / "postmaster.pid").exists():
                run("pg_ctl", "-D", data, "-m", "fast", "-w", "stop")


@pytest.fixture
def secrets_dir(tmp_path):
    for spec in roles.LOGINS:
        path = tmp_path / spec.secret
        path.write_text(f"*:*:*:{spec.login}:{secrets.token_hex(32)}\n")
        path.chmod(0o600)
    return tmp_path


def _verifiers(dsn: str) -> dict[str, str | None]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT rolname, rolpassword FROM pg_authid WHERE rolname = ANY(%s)",
            ([spec.login for spec in roles.LOGINS],),
        ).fetchall()
    return {name: verifier for name, verifier in rows}


def test_created_logins_store_scram_verifiers_and_can_log_in(owner_dsn, secrets_dir):
    report = roles.ensure_logins(owner_dsn, secrets_dir)

    assert len(report) == len(roles.LOGINS)
    verifiers = _verifiers(owner_dsn)
    assert set(verifiers) == {spec.login for spec in roles.LOGINS}
    assert all(
        verifier is not None and verifier.startswith("SCRAM-SHA-256$")
        for verifier in verifiers.values()
    )
    # Re-running reuses every login without re-keying it.
    assert roles.ensure_logins(owner_dsn, secrets_dir) == report
    assert _verifiers(owner_dsn) == verifiers


@pytest.mark.parametrize("password", ["md5", "none"])
def test_an_existing_login_without_a_scram_verifier_is_refused_untouched(
    owner_dsn, secrets_dir, password
):
    # The last login, so a refusal that came after creating the others
    # would show.
    spec = roles.LOGINS[-1]
    with psycopg.connect(owner_dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN INHERIT {} IN ROLE {}").format(
                sql.Identifier(spec.login),
                sql.SQL("PASSWORD {}").format(sql.Literal(secrets.token_hex(16)))
                if password == "md5"
                else sql.SQL("PASSWORD NULL"),
                sql.Identifier(spec.group),
            )
        )
    before = _verifiers(owner_dsn)
    assert before[spec.login] is None or before[spec.login].startswith("md5")

    with pytest.raises(roles.RoleSetupError) as caught:
        roles.ensure_logins(owner_dsn, secrets_dir)

    assert f"ROLE_CONFLICT: {spec.login}" in str(caught.value)
    assert "SCRAM=False" in str(caught.value)
    assert _verifiers(owner_dsn) == before == {spec.login: before[spec.login]}
