"""#809 contract: SREGym p12 gets a read-only account on the system-under-test
Valkey, made by the harness (B4-U6 = a).

Written from the ruling (docs/tasks/2026-11-22-next-batches.md B4-U6,
prereview row U6), #773's public interface (the product's Redis tool needs a
password account whose own ``ACL GETUSER`` passes
``opspilot.tools.redis_inspect.account_problem``; the lab grant in
``.github/workflows/ci.yml``) and the PR-B entry stand-ins, without reading an
implementation. The product gains no anonymous mode: the account is
harness-only scaffolding and is disclosed.

Rules under test:

* in p12 (``valkey_auth_disruption``) one user is made, whose grant starts
  from ``reset``, denies ``-@all`` before any allow, never allows write,
  dangerous, admin or all commands, and passes #773's own account check
  (``+acl|whoami`` and ``+acl|getuser`` included). Since #830 (R6) the SREGym
  ``before_fault_injection`` hook makes it; the entry, after ``/get_app`` and
  before the question goes to the web, only checks it as itself;
* the worker gets that user and its password as ``OPSPILOT_REDIS_USERNAME`` /
  ``OPSPILOT_REDIS_PASSWORD``; the web gets no Redis credential; the password
  is fresh per pass and is written to no record or summary;
* other problems send the Valkey nothing;
* the p12 summary carries the disclosure ``VALKEY_READONLY_ACCOUNT_BY_HARNESS``
  once; other problems' summaries do not.

Assumptions beyond the interface (also marked ``# ASSUMPTION:``):

* ``EntryHost.valkey_check(port=, username=, password=)`` authenticates as
  the account and returns its ``ACL GETUSER`` reply (#830);
* the entry knows p12 from its job (``seq`` 12 with p12's digest); the
  driver adds the disclosure from the problem it runs, so the summary carries
  it even when the entry record is lost.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from opspilot.tools.redis_inspect import account_problem
from tests.sregym_eval_support import LITE_17, mod
from tests.test_sregym_eval_b_entry_contract import Setup, _record, _run, _setup
from tests.test_sregym_eval_b_round_contract import PID as P03
from tests.test_sregym_eval_kafka_contract import ASTRONOMY, _round, _summary_of

P12 = "valkey_auth_disruption"
DISCLOSURE = "VALKEY_READONLY_ACCOUNT_BY_HARNESS"
NEVER = {"+@all", "allcommands", "+@write", "+@dangerous", "+@admin", "nopass"}


def test_p12_is_seq_12() -> None:
    assert LITE_17[11] == P12
    assert mod("upstream").problem_seq(P12) == 12


# --- entry stand-in ----------------------------------------------------------------------------
#
# #830 (R6): the account is made before the fault by the SREGym hook
# (``opspilot_sregym_hooks``, covered in ``test_sregym_eval_valkey_830_contract``);
# the entry only checks it as itself. The stand-in plays the hook's two files
# and the account's own ``AUTH`` + ``ACL GETUSER``.


class Valkey:
    """Records each account check and what the entry had done before it."""

    def __init__(self, s: Setup) -> None:
        self.s = s
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> Any:
        h = self.s.harness
        self.calls.append(
            {
                **kwargs,
                "events": list(h.events),
                "paths": [r.path for r in h.fake.requests],
            }
        )
        h.events.append("valkey_check")
        return _getuser(list(HOOKS.VALKEY_GRANT))


HOOKS = mod("opspilot_sregym_hooks")


def _getuser(rules: Sequence[str]) -> list[Any]:
    """What Valkey answers to ``ACL GETUSER`` after a ``reset`` grant."""
    perms = ["-@all", *(r for r in rules if r.startswith("+"))]
    keys = " ".join(r for r in rules if r.startswith("~"))
    return ["flags", ["on"], "passwords", ["x" * 64], "commands", " ".join(perms),
            "keys", keys, "channels", "", "selectors", []]  # fmt: skip


def _hook_files(work: Path, password: str, status: str = "created") -> None:
    (work / HOOKS.SECRET_FILE).write_text(password + "\n")
    (work / HOOKS.RESULT_FILE).write_text(
        json.dumps(
            {
                "user": HOOKS.VALKEY_USER,
                "status": status,
                "code": None if status == "created" else "EXEC_TIMEOUT",
                "before_fault_injection": True,
            }
        )  # fmt: skip
    )


@pytest.fixture
def entry(tmp_path: Path):  # noqa: ANN201
    made: list[Setup] = []

    def _make(
        name: str, pid: str, seq: int, app: Any, password: str | None = None
    ) -> tuple[Setup, Valkey]:
        d = tmp_path / name
        d.mkdir()
        s = _setup(
            d, app=app, seq=seq, problem_digest=mod("boundary").problem_id_digest(pid)
        )
        if password is not None:
            _hook_files(s.work_dir, password)
        valkey = Valkey(s)
        s.harness.valkey_check = valkey  # type: ignore[attr-defined]
        made.append(s)
        return s, valkey

    yield _make
    for s in made:
        s.harness.server.shutdown()
        s.harness.server.server_close()


def _go(s: Setup) -> int:
    s.harness.fake.advance("diagnosis")
    return _run(s)


def _p12(
    entry: Any,  # noqa: ANN401
    name: str = "p12",
    password: str = "p12-pass-0123456789abcdef",
) -> tuple[Setup, Valkey]:
    s, valkey = entry(name, P12, 12, ASTRONOMY, password)
    assert _go(s) == 0
    return s, valkey


# --- p12: the account step ----------------------------------------------------------------------


def test_p12_grant_is_read_only_and_passes_the_tools_own_check() -> None:
    rules = [r.lower() for r in HOOKS.setuser_line("pw").split()[3:]]
    plus = [r for r in rules if r.startswith("+")]
    assert "reset" in rules and rules.index("reset") < min(
        rules.index(r) for r in rules if r.startswith(("+", ">"))
    )
    assert "on" in rules
    assert "-@all" in rules and rules.index("-@all") < rules.index(plus[0])
    assert not NEVER & set(rules), rules
    assert {"+@read", "+acl|whoami", "+acl|getuser"} <= set(plus)
    assert len([r for r in rules if r.startswith(">")]) == 1
    assert account_problem(_getuser(rules)) is None


def test_p12_entry_checks_the_account_as_itself_after_get_app_before_the_question(
    entry,  # noqa: ANN001
) -> None:
    s, valkey = _p12(entry)
    (call,) = valkey.calls
    assert call["username"] == HOOKS.VALKEY_USER
    assert call["password"] == "p12-pass-0123456789abcdef"
    assert "/get_app" in call["paths"]
    assert "intake" not in call["events"]
    ev = s.harness.events
    assert ev.index("valkey_check") < ev.index("intake")
    assert ev.index("valkey_check") < ev.index("start_worker")
    account = _record(s)["valkey_account"]
    assert account["status"] == "created" and account["code"] is None
    assert account["created_before_fault_injection"] is True
    assert "VALKEY_READONLY_ACCOUNT_FAILED" not in _record(s)["disclosures"]


def test_p12_worker_holds_the_account_and_web_holds_no_credential(entry) -> None:  # noqa: ANN001
    s, _ = _p12(entry)
    pw = "p12-pass-0123456789abcdef"
    (web,) = s.harness.web_envs
    (worker,) = s.harness.worker_envs
    assert worker["OPSPILOT_REDIS_USERNAME"] == HOOKS.VALKEY_USER
    assert worker["OPSPILOT_REDIS_PASSWORD"] == pw
    assert re.fullmatch(
        r"redis://127\.0\.0\.1:\d+(/\d+)?", worker["OPSPILOT_REDIS_URL"]
    )
    assert web.get("OPSPILOT_REDIS_URL") == worker["OPSPILOT_REDIS_URL"]
    assert "OPSPILOT_REDIS_USERNAME" not in web
    assert "OPSPILOT_REDIS_PASSWORD" not in web
    assert pw not in " ".join(web.values())


def test_p12_password_is_never_recorded(entry) -> None:  # noqa: ANN001
    a, _ = _p12(entry, "a", "pass-a-0123456789abcdef")
    b, _ = _p12(entry, "b", "pass-b-0123456789abcdef")
    for s, pw in ((a, "pass-a-0123456789abcdef"), (b, "pass-b-0123456789abcdef")):
        for path in s.work_dir.rglob("*.json"):
            assert pw not in path.read_text(), path


def test_other_problems_send_the_valkey_nothing(entry) -> None:  # noqa: ANN001
    s, valkey = entry("p03", P03, 3, ASTRONOMY)
    assert _go(s) == 0
    assert valkey.calls == []
    assert DISCLOSURE not in _record(s)["disclosures"]
    assert "valkey_account" not in _record(s)
    (worker,) = s.harness.worker_envs
    assert len(worker["OPSPILOT_REDIS_PASSWORD"]) >= 16


# --- driver: the disclosure in the summary -------------------------------------------------------


def test_p12_summary_discloses_the_harness_account_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = _round(tmp_path, monkeypatch, "127.0.0.1 kafka\n")
    r.prepare()
    driver = mod("driver")
    driver.run_trial(r.cfg, r.host, P12, 1)
    driver.run_trial(r.cfg, r.host, P03, 1)
    p12 = _summary_of(r, 12)["disclosures"]
    assert p12.count(DISCLOSURE) == 1
    assert DISCLOSURE not in _summary_of(r, 3)["disclosures"]
    assert DISCLOSURE not in set(mod("summary").ALWAYS_DISCLOSED)


def test_p12_disclosure_survives_a_lost_entry_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = _round(tmp_path, monkeypatch, "127.0.0.1 kafka\n")
    original = r.fake.run_main

    def run_main(argv, cwd, env, log_path, timeout):  # noqa: ANN001, ANN202
        code = original(argv, cwd, env, log_path, timeout)
        job = json.loads(Path(env["SREGYM_EVAL_JOB"]).read_text())
        (Path(job["work_dir"]) / "entry-record.json").unlink()
        return code

    r.fake.run_main = run_main  # type: ignore[method-assign]
    r.host = r.fake.as_host()
    r.prepare()
    mod("driver").run_trial(r.cfg, r.host, P12, 1)
    assert DISCLOSURE in _summary_of(r, 12)["disclosures"]
