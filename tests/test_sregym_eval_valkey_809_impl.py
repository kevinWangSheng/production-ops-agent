"""#809 implementer check: a failed p12 account step is disclosed and the
pass goes on (lead decision; the contract tests cover the success path)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.sregym_eval_support import mod
from tests.test_sregym_eval_b_entry_contract import _record, _run, _setup
from tests.test_sregym_eval_kafka_contract import ASTRONOMY
from tests.test_sregym_eval_valkey_809_contract import P12


def test_p12_account_failure_is_disclosed_and_the_pass_goes_on(
    tmp_path: Path,
) -> None:
    s = _setup(
        tmp_path, app=ASTRONOMY, seq=12,
        problem_digest=mod("boundary").problem_id_digest(P12),
    )  # fmt: skip

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionRefusedError("NOAUTH")

    s.harness.valkey_admin = refuse  # type: ignore[attr-defined]
    try:
        s.harness.fake.advance("diagnosis")
        assert _run(s) == 0
    finally:
        s.harness.server.shutdown()
        s.harness.server.server_close()
    record = _record(s)
    assert record["disclosures"].count("VALKEY_READONLY_ACCOUNT_FAILED") == 1
    assert record["valkey_account"]["status"] == "failed"
    assert "intake" in s.harness.events
    (worker,) = s.harness.worker_envs
    assert worker["OPSPILOT_REDIS_USERNAME"] and worker["OPSPILOT_REDIS_PASSWORD"]
