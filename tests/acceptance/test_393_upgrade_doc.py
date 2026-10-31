"""Documentation contract for the #393 upgrade order (B4)."""

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
UPGRADE = REPO / "deploy/compose/UPGRADE.md"


def test_upgrade_document_states_stop_migrate_init_roles_start_order() -> None:
    assert UPGRADE.is_file(), f"missing {UPGRADE.relative_to(REPO)}"
    text = UPGRADE.read_text().lower()
    positions = [
        text.index(term) for term in ("stop", "migrate", "init-roles", "start")
    ]
    assert positions == sorted(positions)
    assert all(term in text for term in ("web", "worker", "observer"))
