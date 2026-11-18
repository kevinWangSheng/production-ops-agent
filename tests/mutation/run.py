"""Run the invariant mutation list against a test selection.

    .venv/bin/python -m tests.mutation.run [--suite PATH ...] [--only M01,M04]

Copies the checkout (without .git, .venv, docs, tmp) to a scratch directory,
checks that the selection passes unmutated there, then applies each mutation
in turn, runs the selection with ``-x`` and records ``killed`` (a test
failed), ``survived`` (all passed: the invariant is unguarded), ``stale``
(the mutated text is not in the file exactly once) or ``error`` (collection
or usage failure). PG mutations run only with ``M1_DURABLE_POSTGRES=1``, each
on a freshly created and migrated database next to ``OPSPILOT_LAB_DSN``.
Exit status is non-zero when anything survived, went stale or errored.
This is a maintenance tool, not a required CI check.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

from tests.mutation.mutations import MUTATIONS, Mutation

REPO = Path(__file__).resolve().parents[2]
DEFAULT_SUITE = ("tests/safety",)
COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".venv", "tmp", "docs", "__pycache__", ".pytest_cache", "node_modules"
)


def _copy(dest: Path) -> None:
    for item in REPO.iterdir():
        if item.name in {".git", ".venv", "tmp", "docs", ".playwright-mcp"}:
            continue
        target = dest / item.name
        if item.is_dir():
            shutil.copytree(item, target, ignore=COPY_IGNORE, symlinks=True)
        else:
            shutil.copy2(item, target)


def _fresh_database(env: dict[str, str], copy: Path) -> str:
    """Create a database next to the lab one and migrate it with the copy."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    base = env.get("OPSPILOT_LAB_DSN") or (
        "host=127.0.0.1 port=55431 dbname=m0_budget user=m0_lab"
    )
    name = f"mutation_{uuid4().hex[:12]}"
    admin = make_conninfo(base, dbname="postgres")
    code = (
        "import psycopg, sys\n"
        "from psycopg import sql\n"
        "from opspilot import schema\n"
        "admin, dsn, name, pg_dump = sys.argv[1:]\n"
        "with psycopg.connect(admin, autocommit=True) as c:\n"
        "    c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))\n"
        "schema.migrate(dsn, pg_dump=pg_dump)\n"
    )
    dsn = make_conninfo(base, dbname=name)
    subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            admin,
            dsn,
            name,
            env.get("OPSPILOT_PG_DUMP", "pg_dump"),
        ],
        cwd=copy,
        env=env,
        check=True,
        capture_output=True,
    )
    assert conninfo_to_dict(dsn)["dbname"] == name
    return dsn


def _drop_database(env: dict[str, str], dsn: str) -> None:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    name = conninfo_to_dict(dsn)["dbname"]
    with psycopg.connect(make_conninfo(dsn, dbname="postgres"), autocommit=True) as c:
        c.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)
            )
        )


def _pytest(copy: Path, suite: list[str], env: dict[str, str]) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *suite],
        cwd=copy,
        env=env,
        capture_output=True,
        text=True,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    return proc.returncode, (lines[-1] if lines else proc.stderr.strip()[-200:])


def _run_one(
    mutation: Mutation, copy: Path, suite: list[str], env: dict[str, str]
) -> tuple[str, str]:
    originals: dict[Path, str] = {}
    for rel, old, new in mutation.edits:
        path = copy / rel
        originals.setdefault(path, path.read_text())
        current = path.read_text()
        if current.count(old) != 1:
            for restore, content in originals.items():
                restore.write_text(content)
            return "stale", f"{rel}: text occurs {current.count(old)} times"
        path.write_text(current.replace(old, new))
    run_env = dict(env)
    dsn = None
    try:
        if mutation.pg:
            dsn = _fresh_database(env, copy)
            run_env["OPSPILOT_LAB_DSN"] = dsn
            run_env["OPSPILOT_TEST_DATABASE_URL"] = dsn
        code, summary = _pytest(copy, suite, run_env)
    finally:
        for restore, content in originals.items():
            restore.write_text(content)
        if dsn is not None:
            _drop_database(env, dsn)
    if code == 1:
        return "killed", summary
    if code == 0:
        return "survived", summary
    return "error", f"pytest exit {code}: {summary}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--suite", action="append", help="pytest path (repeatable)")
    parser.add_argument("--only", help="comma-separated mutation ids")
    args = parser.parse_args(argv)
    suite = args.suite or list(DEFAULT_SUITE)
    chosen = [m for m in MUTATIONS if not args.only or m.id in args.only.split(",")]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    pg = env.get("M1_DURABLE_POSTGRES") == "1"
    rows: list[tuple[str, str, str, str]] = []
    with tempfile.TemporaryDirectory(prefix="opspilot-mutation-") as scratch:
        copy = Path(scratch) / "repo"
        copy.mkdir()
        _copy(copy)
        env["PYTHONPATH"] = str(copy)
        baseline_env = dict(env)
        dsn = _fresh_database(env, copy) if pg else None
        if dsn:
            baseline_env["OPSPILOT_LAB_DSN"] = dsn
            baseline_env["OPSPILOT_TEST_DATABASE_URL"] = dsn
        try:
            code, summary = _pytest(copy, suite, baseline_env)
        finally:
            if dsn:
                _drop_database(env, dsn)
        print(f"baseline: exit {code}: {summary}", flush=True)
        if code != 0:
            print("the selection does not pass unmutated; nothing to measure")
            return 2
        for mutation in chosen:
            if mutation.pg and not pg:
                rows.append((mutation.id, "skipped (no PG)", mutation.invariant, ""))
                continue
            started = time.monotonic()
            status, summary = _run_one(mutation, copy, suite, env)
            took = f"{time.monotonic() - started:.0f}s"
            rows.append(
                (mutation.id, status, mutation.invariant, f"{summary} [{took}]")
            )
            print(
                f"{mutation.id}\t{status}\t{mutation.invariant}\t{summary}", flush=True
            )
    print()
    print("| id | result | invariant | pytest summary |")
    print("|---|---|---|---|")
    for row in rows:
        print("| " + " | ".join(row) + " |")
    bad = [r for r in rows if r[1] in {"survived", "stale", "error"}]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
