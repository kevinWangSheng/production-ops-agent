"""#429 `opspilot.schema downgrade --to`: argument handling and the plan check
(no database), plus the Compose stop grace and the Rolling back notes."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from opspilot import schema

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_DIR = ROOT / "deploy" / "compose"


def test_downgrade_without_to_is_a_usage_error(monkeypatch, capsys):
    monkeypatch.setenv("OPSPILOT_DSN", "host=/nonexistent dbname=x")
    assert schema.main(["downgrade"]) == 2
    assert "downgrade needs --to <revision>" in capsys.readouterr().err


@pytest.mark.parametrize("action", ["migrate", "check"])
def test_to_is_only_valid_with_downgrade(monkeypatch, capsys, action):
    monkeypatch.setenv("OPSPILOT_DSN", "host=/nonexistent dbname=x")
    assert schema.main([action, "--to", "0023_retention_raw_cleanup"]) == 2
    assert "--to is only valid with downgrade" in capsys.readouterr().err


def test_downgrade_needs_the_owner_dsn(monkeypatch, capsys):
    monkeypatch.delenv("OPSPILOT_DSN", raising=False)
    assert schema.main(["downgrade", "--to", "0023_retention_raw_cleanup"]) == 2
    assert "OPSPILOT_DSN is required" in capsys.readouterr().err


HEAD = schema.head_revision()


@pytest.mark.parametrize("target", ["0023", "base", "head", "-1", "nope"])
def test_plan_refuses_anything_but_an_exact_known_revision(target):
    with pytest.raises(schema.DowngradeRefused, match=f"unknown revision {target}:"):
        schema.downgrade_plan(HEAD, target)


def test_plan_refuses_an_unversioned_database():
    with pytest.raises(schema.DowngradeRefused, match="no alembic_version table"):
        schema.downgrade_plan(None, "0023_retention_raw_cleanup")


def test_plan_refuses_a_revision_this_build_does_not_know():
    with pytest.raises(schema.DowngradeRefused, match="which this build does not know"):
        schema.downgrade_plan("9999_future", "0023_retention_raw_cleanup")


def test_plan_refuses_the_current_and_newer_revisions():
    with pytest.raises(schema.DowngradeRefused, match="is not lower than the current"):
        schema.downgrade_plan(HEAD, HEAD)
    with pytest.raises(schema.DowngradeRefused, match="is not lower than the current"):
        schema.downgrade_plan("0022_incident_association", "0023_retention_raw_cleanup")


def test_plan_accepts_any_strictly_older_revision():
    schema.downgrade_plan("0024_trace_outbox", "0023_retention_raw_cleanup")
    schema.downgrade_plan("0024_trace_outbox", "0001_baseline")


def test_worker_and_observer_get_forty_seconds_to_drain():
    services = yaml.safe_load((COMPOSE_DIR / "compose.yaml").read_text())["services"]
    graced = {name for name, svc in services.items() if "stop_grace_period" in svc}
    assert graced == {"worker", "observer"}
    assert services["worker"]["stop_grace_period"] == "40s"
    assert services["observer"]["stop_grace_period"] == "40s"


def test_rolling_back_notes_stop_downgrade_then_upgrade_the_old_checkout():
    text = (COMPOSE_DIR / "UPGRADE.md").read_text()
    assert "no down-migrations" not in text
    upgrade, rolling_back = text.split("\n## Rolling back\n")
    assert "[Rolling back](#rolling-back)" in upgrade
    steps = [
        line.split()[1]
        for line in rolling_back.splitlines()
        if line[:1].isdigit() and line[1:3] == ". "
    ]
    assert steps == ["Stop", "Downgrade", "Upgrade"]
    assert "compose stop web worker observer" in rolling_back
    assert (
        "compose run --rm --no-deps migrate python -m opspilot.schema downgrade --to"
        in rolling_back
    )
    assert "fix forward" in rolling_back and "backup" in rolling_back
