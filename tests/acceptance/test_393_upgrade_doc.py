"""Documentation contract for the #393 upgrade order (B4)."""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
UPGRADE = REPO / "deploy/compose/UPGRADE.md"


def test_upgrade_document_states_stop_migrate_init_roles_start_order() -> None:
    assert UPGRADE.is_file(), f"missing {UPGRADE.relative_to(REPO)}"
    text = UPGRADE.read_text().lower()
    items = [
        item.strip()
        for item in re.split(r"\n\s*(?=(?:\d+\.\s+|[-*]\s+))|(?<=[.!?])\s+", text)
        if item.strip()
    ]

    def runtime_item(verb: str, start: int = 0) -> int:
        pattern = rf"\b{verb}(?:s|ed|ing)?\b"
        for index, item in enumerate(items[start:], start):
            if re.search(pattern, item) and all(
                runtime in item for runtime in ("web", "worker", "observer")
            ):
                return index
        raise AssertionError(f"document must state runtime {verb} phase")

    stopped = runtime_item("stop")
    migrate = next(i for i, item in enumerate(items) if "migrate" in item)
    init_roles = next(i for i, item in enumerate(items) if "init-roles" in item)
    started = runtime_item("start", init_roles + 1)
    assert stopped < migrate < init_roles < started
