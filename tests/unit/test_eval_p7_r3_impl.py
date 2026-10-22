"""P7 r3 driver changes (P7-D20 raw bodies, P7-D21 tuning debug); fake transports."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "acceptance"))

from eval_p7_support import DriverHarness, snapshot  # noqa: E402

from opspilot.evaluation.judge import (  # noqa: E402
    HTTPJudgeTransport,
    JudgeHTTPResponse,
)
from scripts import eval_p7  # noqa: E402


@pytest.fixture
def harness(tmp_path, monkeypatch, capsys):
    return DriverHarness(tmp_path, monkeypatch, capsys, eval_p7).init()


def debug(h, session, *args, **kwargs):
    h.costs(session)
    return h.cli("debug", "--session", session, "--judge-env", h.env, *args, **kwargs)


def rows(h):
    return [
        json.loads(line)
        for line in (h.private / "exposure.jsonl").read_bytes().splitlines()
    ]


def test_tuning_debug_resends_formal_validation_items_but_formal_refuses(harness):
    h = harness.ready()
    h.formal()
    validation = {i.item_id for i in h.items if i.split == "validation"}
    families = {i.source_family for i in h.items if i.split == "validation"}
    with h.mp.context() as m:
        # The judge prompt moved on from the exported packet's convention.
        m.setattr(eval_p7, "judge", SimpleNamespace(PROMPT="changed prompt"))
        debug(h, "plain-debug", code=3, reason="JUDGE_PROMPT_CHANGED")
        h.calls.clear()
        debug(h, "tuning-debug", "--tuning")
    assert len(h.calls) == 36 * 3
    records = [
        json.loads(p.read_bytes()) for p in (h.ws / "debug/tuning-debug").glob("*.json")
    ]
    assert validation <= {r["item_id"] for r in records}
    row = rows(h)[-1]
    assert row["kind"] == "debug" and row["tuning"] is True
    assert families <= set(row["debug_families"])
    h.costs("canary-formal-2")
    h.calls.clear()
    before = snapshot(h.root)
    h.cli(
        "formal",
        "--run",
        "canary-formal-2",
        "--pack-hash",
        h.pack_hash,
        "--judge-env",
        h.env,
        code=3,
        reason="VALIDATION_EXPOSED",
    )
    assert snapshot(h.root) == before and not h.calls


def test_tuning_debug_refuses_validation_items_no_formal_run_exposed(harness):
    h = harness.seed().export().annotations()
    item = next(i.item_id for i in h.items if i.split == "validation")
    h.costs("tuning-debug")
    before = snapshot(h.root)
    h.cli(
        "debug",
        "--session",
        "tuning-debug",
        "--judge-env",
        h.env,
        "--tuning",
        "--item",
        item,
        code=3,
        reason="VALIDATION_EXPOSURE_FORBIDDEN",
    )
    assert snapshot(h.root) == before and not h.calls
    h.cli("debug", "--session", "tuning-debug", "--judge-env", h.env, "--tuning")
    assert len(h.calls) == 12 * 3


def test_rejected_answers_keep_their_raw_response_body(harness):
    h = harness.seed().export()
    answer = HTTPJudgeTransport.send

    def send(transport, **kwargs):
        response = answer(transport, **kwargs)
        if "z.ai" in kwargs["endpoint"]:
            return JudgeHTTPResponse(200, (b"not a completion",))
        if "openai" in kwargs["endpoint"]:
            return JudgeHTTPResponse(503, (b'{"error": "busy"}',))
        return response

    h.mp.setattr(HTTPJudgeTransport, "send", send)
    debug(h, "raw-debug")
    directory = h.ws / "debug/raw-debug"
    assert (directory / "zai-001.raw").read_bytes() == b"not a completion"
    assert (directory / "openai-001.raw").read_bytes() == b'{"error": "busy"}'
    assert not list(directory.glob("deepseek-*.raw"))
    assert len(list(directory.glob("*.raw"))) == 24
    record = json.loads((directory / "openai-001.json").read_bytes())
    assert record["observation"]["error_code"] == "JUDGE_HTTP_ERROR"
    # Raw bodies stay out of the observation-based summaries.
    assert (
        h.cli("cost", "--session", "raw-debug")["cost"]["providers"]["zai"]["calls"]
        == 12
    )
