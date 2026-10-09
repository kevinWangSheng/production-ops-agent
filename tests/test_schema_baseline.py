"""The baseline migration is the frozen inline DDL, byte for byte (no PG needed)."""

import ast
import pathlib

from opspilot import schema

ROOT = pathlib.Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "opspilot" / "migrations" / "versions"
LEGACY = ROOT / "tests" / "integration" / "legacy_schema_2026-10-05.sql"


def _executed_sql(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text())
    upgrade = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
    )
    statements = []
    for node in ast.walk(upgrade):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and isinstance(node.args[0], ast.Constant)
        ):
            statements.append(str(node.args[0].value))
    return statements


def _sql_lines(text: str) -> list[str]:
    """Statement lines only: indentation, blank lines and ``--`` comments removed."""
    lines = (line.strip() for line in text.splitlines())
    return [line for line in lines if line and not line.startswith("--")]


def test_head_is_the_newest_revision() -> None:
    assert schema.BASELINE_REVISION == "0001_baseline"
    assert schema.head_revision() == "0009_postmortem_generation"


def test_baseline_sql_equals_frozen_legacy_ddl() -> None:
    chunks = _executed_sql(VERSIONS / "0001_baseline.py")
    assert len(chunks) == 4, "one op.execute per former install()"
    legacy = _sql_lines(LEGACY.read_text())
    baseline = [line for chunk in chunks for line in _sql_lines(chunk)]
    assert baseline == legacy
    joined = "\n".join(baseline)
    assert joined.count("CREATE TABLE IF NOT EXISTS") == 15
    assert joined.count("ADD COLUMN IF NOT EXISTS") == 13


def test_every_later_migration_has_a_downgrade() -> None:
    for path in sorted(VERSIONS.glob("*.py")):
        if path.name == "0001_baseline.py":
            continue
        tree = ast.parse(path.read_text())
        downgrade = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == "downgrade"
            ),
            None,
        )
        assert downgrade is not None, f"{path.name} has no downgrade()"
        body = downgrade.body
        assert not (len(body) == 1 and isinstance(body[0], (ast.Pass, ast.Raise))), (
            f"{path.name}: downgrade() must undo upgrade(), not pass/raise"
        )
