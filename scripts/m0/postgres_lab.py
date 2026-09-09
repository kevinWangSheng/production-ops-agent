"""Explicit lifecycle for one synthetic, local-only PostgreSQL lab. Keeps data."""

import argparse
import shutil
import socket
import subprocess
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "tmp/m0-b/postgres"
SOCKET = ROOT / "tmp/m0-b/socket"
DSN = "host=127.0.0.1 port=55431 dbname=m0_budget user=m0_lab"
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
            DSN.replace("dbname=m0_budget", "dbname=postgres"), connect_timeout=2
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
        DSN.replace("dbname=m0_budget", "dbname=postgres"),
        autocommit=True,
        connect_timeout=2,
    ) as conn:
        if not conn.execute(
            "SELECT 1 FROM pg_database WHERE datname='m0_budget'"
        ).fetchone():
            conn.execute("CREATE DATABASE m0_budget")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "restart"])
    action = parser.parse_args().action
    if action in ("stop", "restart"):
        stop()
    if action in ("start", "restart"):
        start()
