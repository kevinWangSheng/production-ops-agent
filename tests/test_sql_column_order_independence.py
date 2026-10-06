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
SOURCES = [
    path
    for path in ROOT.rglob("*.py")
    if "migrations" not in path.parts and path.name != "schema.py"
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
        if "psycopg.connect(" in text:
            assert "row_factory=dict_row" in text, path


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
    }, files_with_star
