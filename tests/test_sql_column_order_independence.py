"""Product SQL must not depend on column order (takeover may accept it, PR #104).

``opspilot.schema migrate --accept-column-order`` stamps a grown database
whose tables list the same columns in a different physical order. That is
only safe while the product never reads rows positionally or inserts without
naming columns. Static check over ``opspilot/`` (migrations excluded: they
are DDL, not queries).
"""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1] / "opspilot"
#: C6a (#570): the read-only SQL tool queries the database under
#: investigation, never OpsPilot's own schema; it must keep result columns in
#: order and by position (duplicate names such as ``SELECT id, id``), so this
#: guard of OpsPilot's own tables does not apply to it.
EXTERNAL_DATABASE_TOOLS = {ROOT / "tools" / "sql_query.py"}
SOURCES = [
    path
    for path in ROOT.rglob("*.py")
    if "migrations" not in path.parts
    and path.name != "schema.py"
    and path not in EXTERNAL_DATABASE_TOOLS
]

INSERT_WITHOUT_COLUMNS = re.compile(
    r"INSERT\s+INTO\s+\w+\s*(VALUES|SELECT|DEFAULT)", re.I
)
SELECT_STAR = re.compile(r"SELECT\s+\*", re.I)
POSITIONAL_ROW = re.compile(r"(fetchone\(\)|fetchall\(\)|row)\[\d+\]")


def test_inserts_always_name_their_columns() -> None:
    offenders = [
        f"{path.relative_to(ROOT.parent)}:{match.start()}"
        for path in SOURCES
        for match in INSERT_WITHOUT_COLUMNS.finditer(path.read_text())
    ]
    assert not offenders, offenders


def test_rows_are_read_by_name_never_by_position() -> None:
    offenders = [
        f"{path.relative_to(ROOT.parent)}:{match.start()}"
        for path in SOURCES
        for match in POSITIONAL_ROW.finditer(path.read_text())
    ]
    assert not offenders, offenders
    for path in SOURCES:
        text = path.read_text()
        assert "tuple_row" not in text, path
        # The only product connection is dict_row, so SELECT * is read by name.
        # Direct connections set it as a keyword; the psycopg_pool pool passes
        # it through ``kwargs`` (#77).
        if "psycopg.connect(" in text or "ConnectionPool(" in text:
            assert (
                "row_factory=dict_row" in text or '"row_factory": dict_row' in text
            ), path


def test_select_star_only_behind_the_dict_row_connection() -> None:
    files_with_star = {
        path.relative_to(ROOT).as_posix()
        for path in SOURCES
        if SELECT_STAR.search(path.read_text())
    }
    assert files_with_star <= {
        "persistence/controls.py",
        "persistence/incidents.py",
        "persistence/steps.py",
        # F13 read snapshots return whole rows as dicts (_StoreBase pool)
        "knowledge/store.py",
    }, files_with_star
