"""#809/#830 implementer check: a failed p12 account step is disclosed and the
pass goes on (lead decision; the contract tests cover the success path)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.sregym_eval_support import mod
from tests.test_sregym_eval_b_entry_contract import _record, _run, _setup
from tests.test_sregym_eval_kafka_contract import ASTRONOMY
from tests.test_sregym_eval_valkey_809_contract import P12, _hook_files

PASSWORD = "impl" * 8


def _p12(tmp_path: Path, check: Any) -> Any:  # noqa: ANN401
    s = _setup(
        tmp_path, app=ASTRONOMY, seq=12,
        problem_digest=mod("boundary").problem_id_digest(P12),
    )  # fmt: skip
    _hook_files(s.work_dir, PASSWORD)
    s.harness.valkey_check = check  # type: ignore[attr-defined]
    try:
        s.harness.fake.advance("diagnosis")
        assert _run(s) == 0
    finally:
        s.harness.server.shutdown()
        s.harness.server.server_close()
    return s


def test_p12_account_failure_is_disclosed_and_the_pass_goes_on(
    tmp_path: Path,
) -> None:
    def refuse(**kwargs: Any) -> Any:
        raise ConnectionRefusedError("NOAUTH")

    s = _p12(tmp_path, refuse)
    record = _record(s)
    assert record["disclosures"].count("VALKEY_READONLY_ACCOUNT_FAILED") == 1
    assert record["valkey_account"]["status"] == "failed"
    assert record["valkey_account"]["code"] == "CHECK_FAILED:ConnectionRefusedError"
    assert "intake" in s.harness.events
    (worker,) = s.harness.worker_envs
    assert worker["OPSPILOT_REDIS_USERNAME"] and worker["OPSPILOT_REDIS_PASSWORD"]


def test_p12_error_reply_text_is_recorded_without_the_password(
    tmp_path: Path,
) -> None:
    """#830: the reply text is kept; an echoed password is masked."""
    from opspilot.tools.redis_inspect import RedisReplyError

    def refuse(**kwargs: Any) -> Any:
        raise RedisReplyError(f"WRONGPASS bad '{kwargs['password']}': x")

    s = _p12(tmp_path, refuse)
    account = _record(s)["valkey_account"]
    assert account["code"] == "CHECK_FAILED:RedisReplyError"
    assert account["reply"] == "WRONGPASS bad '<redacted>': x"
    assert PASSWORD not in str(_record(s))
