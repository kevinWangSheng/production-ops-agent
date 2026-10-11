"""Explicit lifecycle for one synthetic, local-only PostgreSQL lab. Keeps data."""

import argparse
import os
import shutil
import socket
import subprocess
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "tmp/m0-b/postgres"
SOCKET = ROOT / "tmp/m0-b/socket"
# The 55431 lab: start/stop/verify_server and CREATE DATABASE always talk to
# this server, whatever the environment says.
LAB_DSN = "host=127.0.0.1 port=55431 dbname=m0_budget user=m0_lab"
# What the tests import. OPSPILOT_LAB_DSN points the suite (and the child
# processes the cross-process tests spawn) at a throwaway server on another
# port, leaving the lab and its data alone; unset or empty means the lab.
DSN = os.environ.get("OPSPILOT_LAB_DSN") or LAB_DSN
MARKER = "opspilot-m0-b-synthetic-v1\n"


def binary(name):
    found = shutil.which(name)
    if found:
        return found
    path = Path("/opt/homebrew/opt/postgresql@17/bin") / name
    if not path.is_file():
        raise RuntimeError("POSTGRES_BINARY_MISSING")
    return str(path)


def run(name, *args):
    subprocess.run([binary(name), *map(str, args)], check=True, timeout=30)


def validate_paths():
    # Never follow a task directory redirected to another cluster.
    for path in (
        ROOT / "tmp",
        DATA.parent,
        DATA,
        SOCKET,
        DATA / "m0-owner",
        DATA / "postmaster.pid",
        DATA / "PG_VERSION",
        DATA / "postgresql.conf",
    ):
        if path.is_symlink():
            raise RuntimeError("LAB_PATH_UNSAFE")
    if (DATA / "PG_VERSION").exists():
        marker = DATA / "m0-owner"
        if not marker.is_file() or marker.read_text() != MARKER:
            raise RuntimeError("LAB_IDENTITY_CONFLICT")


def verify_server():
    """Read only identity check before database creation or stopping a process."""
    pid_file = DATA / "postmaster.pid"
    if not pid_file.is_file():
        raise RuntimeError("LAB_IDENTITY_CONFLICT")
    lines = pid_file.read_text().splitlines()
    if len(lines) < 4 or Path(lines[1]) != DATA or lines[3] != "55431":
        raise RuntimeError("LAB_IDENTITY_CONFLICT")
    failed = False
    try:
        with psycopg.connect(
            LAB_DSN.replace("dbname=m0_budget", "dbname=postgres"), connect_timeout=2
        ) as conn:
            actual = conn.execute("SHOW data_directory").fetchone()[0]
            if Path(actual).resolve() != DATA.resolve():
                raise RuntimeError("LAB_IDENTITY_CONFLICT")
            # Match server-side file to local pid; stale or mismatched pid refuses.
            server_pid = conn.execute(
                "SELECT split_part(pg_read_file('postmaster.pid'), E'\\n', 1)"
            ).fetchone()[0]
            if server_pid != lines[0]:
                raise RuntimeError("LAB_IDENTITY_CONFLICT")
    except psycopg.Error:
        failed = True
    if failed:
        raise RuntimeError("LAB_SERVER_UNAVAILABLE")


def stop():
    validate_paths()
    if (DATA / "postmaster.pid").exists():
        verify_server()
        run("pg_ctl", "-D", DATA, "-m", "fast", "-w", "stop")


def start():
    validate_paths()
    if not (DATA / "postmaster.pid").exists():
        # Refuse a occupied port before initialization or daemon startup.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 55431))
    DATA.parent.mkdir(parents=True, exist_ok=True)
    SOCKET.mkdir(mode=0o700, exist_ok=True)
    if not (DATA / "PG_VERSION").exists():
        run(
            "initdb",
            "-D",
            DATA,
            "-U",
            "m0_lab",
            "--auth=trust",
            "--no-locale",
            "--encoding=UTF8",
        )
        with (DATA / "postgresql.conf").open("a") as config:
            config.write(
                f"\nlisten_addresses='127.0.0.1'\nport=55431\nunix_socket_directories='{SOCKET}'\nshared_buffers=32MB\nwork_mem=1MB\nmaintenance_work_mem=16MB\nmax_connections=12\nmax_parallel_workers=0\nmax_worker_processes=0\nautovacuum=off\nlogging_collector=off\n"
            )
        (DATA / "m0-owner").write_text(MARKER)
    if not (DATA / "postmaster.pid").exists():
        run("pg_ctl", "-D", DATA, "-l", DATA.parent / "postgres.log", "-w", "start")
    verify_server()
    with psycopg.connect(
        LAB_DSN.replace("dbname=m0_budget", "dbname=postgres"),
        autocommit=True,
        connect_timeout=2,
    ) as conn:
        if not conn.execute(
            "SELECT 1 FROM pg_database WHERE datname='m0_budget'"
        ).fetchone():
            conn.execute("CREATE DATABASE m0_budget")


# Login roles for the web and worker processes (migration 0011 creates the
# NOLOGIN group roles and their grants). Trust auth on 127.0.0.1, so no
# password; a deployment creates its own logins with credentials outside the
# repository.
RUNTIME_LOGINS = {
    "opspilot_web_lab": "opspilot_web",
    "opspilot_worker_lab": "opspilot_worker",
}


def runtime_roles():
    """Create the lab's web and worker logins once; print the DSNs to use.

    Talks to ``DSN`` (``OPSPILOT_LAB_DSN`` when set), whose schema must
    already be at head: the group roles come from the migration, never from
    here. An existing login is reused when it is a plain member of its group
    and nothing else; anything else is refused rather than altered.
    """
    with psycopg.connect(DSN, autocommit=True, connect_timeout=2) as conn:
        for login, group in RUNTIME_LOGINS.items():
            if not conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname=%s", (group,)
            ).fetchone():
                raise RuntimeError(f"ROLE_MISSING: {group} (run make migrate first)")
            row = conn.execute(
                "SELECT r.rolcanlogin, r.rolsuper, r.rolcreaterole, r.rolcreatedb, "
                "array(SELECT g.rolname FROM pg_auth_members m JOIN pg_roles g "
                "ON g.oid = m.roleid WHERE m.member = r.oid ORDER BY 1) AS groups "
                "FROM pg_roles r WHERE r.rolname=%s",
                (login,),
            ).fetchone()
            if row is None:
                conn.execute(
                    sql.SQL("CREATE ROLE {} LOGIN IN ROLE {}").format(
                        sql.Identifier(login), sql.Identifier(group)
                    )
                )
            elif tuple(row) != (True, False, False, False, [group]):
                raise RuntimeError(f"ROLE_CONFLICT: {login}")
            print(f"{group}: {make_conninfo(DSN, user=login)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "restart", "runtime-roles"])
    action = parser.parse_args().action
    if action == "runtime-roles":
        runtime_roles()
    if action in ("stop", "restart"):
        stop()
    if action in ("start", "restart"):
        start()
