"""#169 without a database: each process reads only its own DSN variable,
and the grant declaration is well formed and keeps the P5 boundaries."""

import re

import pytest

from opspilot import worker_main
from opspilot.persistence.grants import RUNTIME_GRANTS, WEB_ROLE, WORKER_ROLE
from opspilot.web import __main__ as web_main

OWNER_DSN = "host=db dbname=opspilot user=owner"
_PRIVILEGE = re.compile(
    r"^(SELECT|INSERT|UPDATE|DELETE)(\(([a-z0-9_]+(,[a-z0-9_]+)*)\))?$"
)
_WRITES = ("INSERT", "UPDATE", "DELETE")


def _writes(role: str, relation: str) -> set[str]:
    return {p for p in RUNTIME_GRANTS[role][relation] if p.startswith(_WRITES)}


@pytest.mark.parametrize("value", [None, ""])
def test_the_web_refuses_to_start_without_its_dsn_even_with_the_owner_dsn_set(
    monkeypatch, value
):
    monkeypatch.setenv("OPSPILOT_DSN", OWNER_DSN)
    if value is None:
        monkeypatch.delenv("OPSPILOT_WEB_DSN", raising=False)
    else:
        monkeypatch.setenv("OPSPILOT_WEB_DSN", value)
    with pytest.raises(SystemExit, match="OPSPILOT_WEB_DSN is required"):
        web_main._serve()


@pytest.mark.parametrize("value", [None, ""])
def test_the_worker_refuses_to_start_without_its_dsn_even_with_the_owner_dsn_set(
    monkeypatch, value
):
    monkeypatch.setenv("OPSPILOT_DSN", OWNER_DSN)
    if value is None:
        monkeypatch.delenv("OPSPILOT_WORKER_DSN", raising=False)
    else:
        monkeypatch.setenv("OPSPILOT_WORKER_DSN", value)
    with pytest.raises(SystemExit, match="OPSPILOT_WORKER_DSN is required"):
        worker_main.main([])


def test_each_process_reads_its_own_dsn_and_never_the_owner_one():
    for module, own in (
        (web_main, "OPSPILOT_WEB_DSN"),
        (worker_main, "OPSPILOT_WORKER_DSN"),
    ):
        source = open(module.__file__).read()
        code = source.split('"""', 2)[2]  # past the module docstring
        assert f'os.environ.get("{own}")' in code
        assert '"OPSPILOT_DSN"' not in code


def test_the_declaration_is_well_formed_and_covers_the_same_relations():
    assert set(RUNTIME_GRANTS) == {WEB_ROLE, WORKER_ROLE}
    assert set(RUNTIME_GRANTS[WEB_ROLE]) == set(RUNTIME_GRANTS[WORKER_ROLE])
    for role, relations in RUNTIME_GRANTS.items():
        assert relations["alembic_version"] == {"SELECT"}
        for relation, privileges in relations.items():
            assert relation == "alembic_version" or relation.startswith("opspilot_")
            kinds = []
            for privilege in privileges:
                match = _PRIVILEGE.match(privilege)
                assert match, (role, relation, privilege)
                if match.group(3):
                    columns = match.group(3).split(",")
                    assert columns == sorted(set(columns)), (role, relation, privilege)
                kinds.append(match.group(1))
            # one entry per type: table-wide or one column list, never both
            assert len(kinds) == len(set(kinds)), (role, relation)


def test_the_worker_cannot_write_knowledge_and_the_web_cannot_write_drafts():
    for relation in (
        "opspilot_knowledge_entries",
        "opspilot_knowledge_revisions",
        "opspilot_knowledge_revocations",
    ):
        assert not _writes(WORKER_ROLE, relation), relation
    for relation in (
        "opspilot_postmortem_generation_attempts",
        "opspilot_postmortem_conclusions",
        "opspilot_postmortem_disputes",
        "opspilot_postmortem_proposals",
    ):
        assert not _writes(WEB_ROLE, relation), relation
    assert _writes(WEB_ROLE, "opspilot_postmortem_versions") == {
        "UPDATE(stale_reason,state)"
    }
    for privilege in _writes(WEB_ROLE, "opspilot_postmortem_generation_jobs"):
        assert not re.search(r"\blease_", privilege), privilege
    for privilege in _writes(WEB_ROLE, "opspilot_runs"):
        assert "epoch" not in privilege.split("(")[1], privilege
