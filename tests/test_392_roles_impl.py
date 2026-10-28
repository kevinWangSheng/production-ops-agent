"""#392 init-roles without a database: the per-login secret files, the
runtime groups covered and the refusal of a login that is more than a plain
member of its group. Creation, reuse and the login-plus-schema-head check
against a real server run in the opt-in Compose end-to-end test."""

import pytest

from opspilot.deploy import roles


def _passfile(tmp_path, content: str, mode: int = 0o600):
    path = tmp_path / "web.pgpass"
    path.write_text(content)
    path.chmod(mode)
    return path


def test_every_runtime_group_gets_its_own_login_and_secret_file():
    assert {spec.group for spec in roles.LOGINS} == {
        "opspilot_web",
        "opspilot_worker",
        "opspilot_observer",
        "opspilot_retention",
    }
    assert len({spec.login for spec in roles.LOGINS}) == 4
    assert len({spec.secret for spec in roles.LOGINS}) == 4
    for spec in roles.LOGINS:
        assert spec.login != spec.group


def test_a_passfile_yields_its_login_and_password(tmp_path):
    path = _passfile(tmp_path, "*:*:*:opspilot_web_app:0123abcd\n")
    assert roles.read_passfile(path) == ("opspilot_web_app", "0123abcd")


@pytest.mark.parametrize(
    "content", ["", "*:*:*:opspilot_web_app\n", "a:b\n", "*:*:*::secretvalue\n"]
)
def test_a_malformed_passfile_is_refused_without_echoing_it(tmp_path, content):
    path = _passfile(tmp_path, content)
    with pytest.raises(roles.RoleSetupError) as caught:
        roles.read_passfile(path)
    assert "secretvalue" not in str(caught.value)


def test_a_passfile_readable_by_others_is_refused(tmp_path):
    path = _passfile(tmp_path, "*:*:*:opspilot_web_app:pw\n", mode=0o640)
    with pytest.raises(roles.RoleSetupError, match="0600"):
        roles.read_passfile(path)


PLAIN = roles.LoginAttributes(
    login=True,
    inherit=True,
    superuser=False,
    createrole=False,
    createdb=False,
    replication=False,
    bypassrls=False,
    groups=("opspilot_web",),
)


def test_a_plain_inheriting_member_of_its_group_only_is_reusable():
    assert roles.conflicts(PLAIN, "opspilot_web") == []


@pytest.mark.parametrize(
    ("change", "reported"),
    [
        ({"inherit": False}, "INHERIT=False"),
        ({"login": False}, "LOGIN=False"),
        ({"superuser": True}, "SUPERUSER=True"),
        ({"createrole": True}, "CREATEROLE=True"),
        ({"bypassrls": True}, "BYPASSRLS=True"),
        ({"groups": ("opspilot_web", "opspilot_worker")}, "membership"),
        ({"groups": ()}, "membership"),
    ],
)
def test_a_login_with_other_attributes_is_reported_not_altered(change, reported):
    from dataclasses import replace

    problems = roles.conflicts(replace(PLAIN, **change), "opspilot_web")
    assert any(reported in problem for problem in problems)
