"""#392 credential entries the installer writes for the web: the secret is
read from stdin and only its PBKDF2 hash / sha256 digest is printed."""

import io

import pytest

from opspilot.deploy import credentials
from opspilot.web.auth import token_digest, verify_password


def _run(monkeypatch, capsys, argv, stdin):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = credentials.main(argv)
    return code, capsys.readouterr()


def test_a_ui_user_entry_holds_a_hash_that_verifies_the_password(monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, ["ui-user", "operator"], "s3cret-pass\n")
    assert code == 0
    name, _, encoded = out.out.strip().partition("=")
    assert name == "operator"
    assert verify_password("s3cret-pass", encoded)
    assert "s3cret-pass" not in out.out + out.err


def test_an_event_token_entry_maps_the_digest_to_the_actor(monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, ["event-token", "alertmanager"], "tok-123\n")
    assert code == 0
    assert out.out.strip() == f"{token_digest('tok-123')}=alertmanager"
    assert "tok-123" not in out.out.replace(token_digest("tok-123"), "")


@pytest.mark.parametrize(
    "argv",
    [["ui-user", "op=x"], ["ui-user", ""], ["event-token", "a,b"], ["other", "x"]],
)
def test_an_entry_that_would_not_parse_back_is_refused(monkeypatch, capsys, argv):
    code, _ = _run(monkeypatch, capsys, argv, "secret\n")
    assert code != 0


def test_an_empty_secret_is_refused(monkeypatch, capsys):
    code, out = _run(monkeypatch, capsys, ["ui-user", "operator"], "\n")
    assert code != 0
    assert out.out == ""
