"""Opt-in register chain on a self-created Unix-socket PG, never the 55561 lab.

M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest tests/test_f11_u2_register_postgres.py
The fixture overrides OPSPILOT_LAB_DSN with its own initdb instance and stops it.
This module deliberately lives outside integration/ and its shared-DSN conftest.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.observer.health_profile import canonical_content, load_health_profile
from scripts import f11_u2_harness as h

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)


@pytest.fixture
def private_postgres(monkeypatch):
    found = shutil.which("initdb")
    binary_dir = (
        Path(found).parent if found else Path("/opt/homebrew/opt/postgresql@17/bin")
    )
    if os.geteuid() == 0 or any(
        not (binary_dir / name).is_file() for name in ("initdb", "pg_ctl", "pg_dump")
    ):
        pytest.skip(
            "self-owned initdb binaries/user unavailable; shared databases are never used"
        )
    # A short private socket path avoids macOS's Unix socket path length limit.
    with tempfile.TemporaryDirectory(
        prefix="u2-register-pg-",
        dir="/private/tmp" if Path("/private/tmp").is_dir() else "/tmp",
    ) as scratch:
        root = Path(scratch)
        data, socket_dir = root / "data", root / "socket"
        socket_dir.mkdir(mode=0o700)

        def run(name, *args):
            return subprocess.run(
                [str(binary_dir / name), *map(str, args)],
                capture_output=True,
                check=True,
                timeout=30,
            )

        try:
            run(
                "initdb",
                "-D",
                data,
                "-U",
                "u2_register_test",
                "--auth=trust",
                "--no-locale",
                "--encoding=UTF8",
            )
        except subprocess.CalledProcessError as exc:
            if (
                b"could not create shared memory segment" in exc.stderr
                and b"Operation not permitted" in exc.stderr
            ):
                pytest.skip(
                    "private initdb unavailable: sandbox denies shared memory; no shared DB used"
                )
            raise
        with (data / "postgresql.conf").open("a") as config:
            config.write(
                f"\nlisten_addresses=''\nport=55469\nunix_socket_directories='{socket_dir}'\nshared_buffers=16MB\nmax_connections=12\n"
            )
        try:
            run("pg_ctl", "-D", data, "-l", root / "postgres.log", "-w", "start")
            dsn = make_conninfo(
                host=str(socket_dir),
                port=55469,
                dbname="postgres",
                user="u2_register_test",
                connect_timeout=5,
            )
            monkeypatch.setenv("OPSPILOT_LAB_DSN", dsn)
            monkeypatch.delenv("OPSPILOT_TARGET_IDENTITIES", raising=False)
            # Assert server ownership before migrations or product writes.
            with psycopg.connect(dsn, options="-c statement_timeout=5000") as conn:
                assert (
                    Path(conn.execute("SHOW data_directory").fetchone()[0]).resolve()
                    == data.resolve()
                )
            schema.migrate(dsn, pg_dump=str(binary_dir / "pg_dump"))
            yield dsn
        finally:
            if (data / "postmaster.pid").exists():
                run("pg_ctl", "-D", data, "-m", "fast", "-w", "stop")


def test_register_creates_one_pending_release_and_replays_without_authorization(
    private_postgres, monkeypatch, tmp_path
):
    monkeypatch.setattr(h, "LEDGERS", tmp_path / "ledgers")
    monkeypatch.setattr(
        h, "kube", lambda *a, **kw: pytest.fail("register must not contact Kubernetes")
    )
    profile_path = tmp_path / "profile.json"
    profile = load_health_profile(
        h.ROOT / "opspilot/observer/profiles/otel-demo-checkout.json"
    )
    original_profile = (
        h.ROOT / "opspilot/observer/profiles/otel-demo-checkout.json"
    ).read_bytes()
    profile_path.write_bytes(original_profile)
    resource_uid = "u2-register-" + uuid4().hex
    identity = dict(
        integration_id="u2-private-test",
        cluster_uid="synthetic-cluster",
        namespace="otel-demo",
        workload="checkout",
    )
    registry = tmp_path / "registry.json"
    h.write_json(
        registry, {resource_uid: {**identity, "health_profile_id": profile.profile_id}}
    )
    before, after = (
        {
            "metadata": {"labels": {"app": "checkout"}},
            "spec": {"containers": [{"name": "checkout", "env": []}]},
        },
        {
            "metadata": {"labels": {"app": "checkout"}},
            "spec": {
                "containers": [
                    {
                        "name": "checkout",
                        "env": [{"name": "OPSPILOT_U2_RELEASE_ID", "value": "new"}],
                    }
                ]
            },
        },
    )
    released = h.utc_now() - timedelta(seconds=10)
    ledger = h.ledger_path("pg-register")
    h.write_json(
        ledger / "release.json",
        dict(
            release_id="private-" + uuid4().hex,
            released_at=released.isoformat(),
            before_template=before,
            after_template=after,
            before_revision=dict(
                images={"checkout": "sha256:" + "a" * 64},
                pod_template_digest="sha256:"
                + h.sha256(h.canonical_json(before).encode()),
            ),
            after_revision=dict(
                images={"checkout": "sha256:" + "a" * 64},
                pod_template_digest="sha256:"
                + h.sha256(h.canonical_json(after).encode()),
            ),
            deployment_uid="synthetic-deployment",
            before_replicaset_uid="rs-old",
            after_replicaset_uid="rs-new",
            before_pod_template_hash="old",
            after_pod_template_hash="new",
            deployment_generation=2,
        ),
    )
    h.occupation(
        "claim", "register-test", "private-db", h.utc_now() + timedelta(minutes=5)
    )
    args = [
        "--holder",
        "register-test",
        "--batch",
        "private-db",
        "register",
        "--run",
        "pg-register",
        "--registry",
        str(registry),
        "--resource-uid",
        resource_uid,
        "--profile",
        str(profile_path),
        "--minimum",
        "1200",
    ]
    assert h.main(args) == 0
    first = h.read_json(ledger / "registration.json")
    assert h.main(args) == 0
    second = h.read_json(ledger / "registration.json")
    assert second["profile_original_sha256"] == h.sha256(original_profile)
    assert all(
        (attempt / "profile-original.json").read_bytes() == original_profile
        for attempt in (ledger / "registration-attempts").iterdir()
    )
    assert (first["outcome"], second["outcome"]) == ("created", "replayed")
    assert first["observation_id"] == second["observation_id"]
    assert first["released_at"] == second["released_at"] == released.isoformat()
    assert (
        first["deadline_at"]
        == second["deadline_at"]
        == (released + timedelta(seconds=profile.session.deadline_seconds)).isoformat()
    )
    with psycopg.connect(private_postgres, options="-c statement_timeout=5000") as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM opspilot_release_observations WHERE status='pending'"
            ).fetchone()[0]
            == 1
        )
        target = conn.execute(
            "SELECT integration_id,cluster_uid,namespace,workload,resource_uid FROM opspilot_targets"
        ).fetchall()
        assert target == [
            (
                identity["integration_id"],
                identity["cluster_uid"],
                "otel-demo",
                "checkout",
                resource_uid,
            )
        ]
        assert conn.execute(
            "SELECT health_profile_revision,content FROM opspilot_health_profiles"
        ).fetchall() == [(profile.revision, canonical_content(profile))]
        assert conn.execute(
            "SELECT outcome,actor FROM opspilot_release_events ORDER BY event_id"
        ).fetchall() == [("created", "u2-harness"), ("replayed", "u2-harness")]
        for table in (
            "opspilot_observation_sessions",
            "opspilot_incidents",
            "opspilot_runs",
        ):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    h.occupation("release", "register-test", "private-db")
