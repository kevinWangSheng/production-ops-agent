"""Documentation contract for the #393 upgrade order (B4)."""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
UPGRADE = REPO / "deploy/compose/UPGRADE.md"


def test_upgrade_document_states_stop_migrate_init_roles_start_order() -> None:
    assert UPGRADE.is_file(), f"missing {UPGRADE.relative_to(REPO)}"
    text = UPGRADE.read_text().lower()
    blocks = re.split(r"\n\s*\n", text)

    def phase_position(verb: str) -> int:
        pattern = rf"\b{verb}(?:s|ed|ing)?\b"
        offset = 0
        for block in blocks:
            if re.search(pattern, block) and all(
                runtime in block for runtime in ("web", "worker", "observer")
            ):
                return offset
            offset += len(block) + 2
        raise AssertionError(f"document must state runtime {verb} phase")

    stopped = phase_position("stop")
    migrate = text.index("migrate")
    init_roles = text.index("init-roles")
    started = phase_position("start")
    assert stopped < migrate < init_roles < started
