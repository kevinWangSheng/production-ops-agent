"""``scripts/m0/postgres_lab.py runtime-roles`` (#169): creates the web and
worker logins once, reuses a plain member, refuses any other existing login."""

import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from scripts.m0 import postgres_lab
from scripts.m0.postgres_lab import DSN

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)


def _drop(*names: str) -> None:
    with psycopg.connect(DSN, autocommit=True) as conn:
        for name in names:
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


def test_creates_then_reuses_plain_member_logins(capsys) -> None:
    logins = {
        f"web_lab_{uuid4().hex[:8]}": "opspilot_web",
        f"worker_lab_{uuid4().hex[:8]}": "opspilot_worker",
    }
    try:
        postgres_lab.runtime_roles(logins)
        postgres_lab.runtime_roles(logins)  # second run reuses them
        with psycopg.connect(DSN) as conn:
            for login, group in logins.items():
                assert conn.execute(
                    "SELECT pg_has_role(%s, %s, 'MEMBER')", (login, group)
                ).fetchone() == (True,)
        assert capsys.readouterr().out.count("user=") == 4
    finally:
        _drop(*logins)


@pytest.mark.parametrize(
    ("attribute", "reported"),
    [
        ("NOINHERIT", "INHERIT=False"),
        ("REPLICATION", "REPLICATION=True"),
        ("BYPASSRLS", "BYPASSRLS=True"),
        ("CREATEDB", "CREATEDB=True"),
    ],
)
def test_an_existing_login_with_other_attributes_is_refused_untouched(
    attribute, reported
) -> None:
    login = f"web_lab_{uuid4().hex[:8]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN {} IN ROLE opspilot_web").format(
                sql.Identifier(login), sql.SQL(attribute)
            )
        )
    try:
        with pytest.raises(RuntimeError, match=f"ROLE_CONFLICT: {login} .*{reported}"):
            postgres_lab.runtime_roles({login: "opspilot_web"})
        with psycopg.connect(DSN) as conn:
            row = conn.execute(
                "SELECT rolinherit, rolreplication, rolbypassrls, rolcreatedb "
                "FROM pg_roles WHERE rolname=%s",
                (login,),
            ).fetchone()
        assert row == (
            attribute != "NOINHERIT",
            attribute == "REPLICATION",
            attribute == "BYPASSRLS",
            attribute == "CREATEDB",
        )
    finally:
        _drop(login)


def test_an_existing_login_outside_the_group_is_refused() -> None:
    login = f"web_lab_{uuid4().hex[:8]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(login)))
    try:
        with pytest.raises(RuntimeError, match="membership="):
            postgres_lab.runtime_roles({login: "opspilot_web"})
    finally:
        _drop(login)
