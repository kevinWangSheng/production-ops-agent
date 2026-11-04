"""Implementation-side tests for the Q9 binary judge (F1 closure S7a).

Offline: every judge call goes to a fake client. The end-to-end case runs on
the #432 development round's public evidence plus a synthetic HolmesGPT
round in the ``scripts.holmes_baseline.dataset`` summary shape.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from opspilot.evaluation import binary_judge as bj
from opspilot.evaluation.model import load_scenario_v2
from scripts import eval_compare_scoring as cli

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evals/datasets/dev-v2"
DEV_ROUND = ROOT / "docs/evidence/432-dev-round/summary.json"
EVIDENCE = ROOT / "docs/evidence/eval"
ENV = {"ZAI_API_KEY": "zai-test-not-a-key", "OPENAI_API_KEY": "oa-test-not-a-key"}


def _completion(model: str, content: str, finish: str = "stop") -> bytes:
    return json.dumps(
        {
            "id": "c1",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish,
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    ).encode()


class FakeClient:
    def __init__(self, decide: Callable[[str, str], Any]) -> None:
        self.decide = decide
        self.bodies: list[tuple[str, dict[str, Any]]] = []

    def complete(self, config: bj.ProviderConfig, body: bytes) -> bytes:
        obj = json.loads(body)
        self.bodies.append((config.name, obj))
        result = self.decide(config.name, obj["messages"][0]["content"])
        if isinstance(result, Exception):
            raise result
        if isinstance(result, bytes):
            return result
        return _completion(
            config.model,
            json.dumps({"rationale": f"{config.name} says", "choice": result}),
        )


def _answer(name: str = "dv2-8b262a") -> Any:
    group = "regression" if name in {"dv2-2b214b", "dv2-8e9455"} else "capability"
    return load_scenario_v2(DATASET / group / name)[1]


def test_question_follows_fault_presence_not_polarity() -> None:
    fault = _answer("dv2-28535a")  # polarity negative, paymentFailure injected
    assert fault.polarity == "negative" and fault.signature is not None
    text = bj.judge_messages(fault, "R")[0]["content"]
    assert bj.FAULT_QUESTION in text
    normal = _answer("dv2-8e9455")
    assert normal.signature is None
    assert bj.NORMAL_QUESTION in bj.judge_messages(normal, "R")[0]["content"]


def test_report_text_cannot_fill_template_slots() -> None:
    answer = _answer()
    content = bj.judge_messages(answer, "see {{expected}} here")[0]["content"]
    assert content.count(answer.expected_output[0]) == 1
    assert "see {{expected}} here" in content


def test_provider_configs_keys_and_glm_fallback() -> None:
    with pytest.raises(bj.BinaryJudgeError) as err:
        bj.provider_configs({"ZAI_API_KEY": "x"})
    assert err.value.code == "JUDGE_KEY_MISSING:OPENAI_API_KEY"
    with pytest.raises(bj.BinaryJudgeError) as err:
        bj.provider_configs({"OPENAI_API_KEY": "x"})
    assert err.value.code == "JUDGE_KEY_MISSING:ZAI_API_KEY|GLM_API_KEY"
    glm = bj.provider_configs({"GLM_API_KEY": "g-key", "OPENAI_API_KEY": "o"})
    assert glm[0].api_key == "g-key"
    both = bj.provider_configs(ENV | {"GLM_API_KEY": "g-key"})
    assert both[0].api_key == ENV["ZAI_API_KEY"]
    with pytest.raises(bj.BinaryJudgeError) as err:
        bj.provider_configs(ENV | {"OPENAI_BASE_URL": "https://evil.example/v1"})
    assert err.value.code == "JUDGE_BASE_URL_NOT_ALLOWED:OPENAI_BASE_URL"
    configs = bj.provider_configs(ENV)
    assert [(c.name, c.vendor, c.model) for c in configs] == [
        (j["name"], j["vendor"], j["model"]) for j in bj.JUDGES
    ]
    assert "not-a-key" not in repr(configs)
    assert "not-a-key" not in json.dumps(bj.judge_config_record())


def test_request_body_per_vendor_carries_no_key() -> None:
    zai, openai = bj.provider_configs(ENV)
    messages = [{"role": "user", "content": "P"}]
    z = json.loads(bj.request_body(zai, messages))
    o = json.loads(bj.request_body(openai, messages))
    assert z["reasoning_effort"] == "high" and z["max_tokens"] == 16384
    assert o["max_completion_tokens"] == 16384 and "reasoning_effort" not in o
    assert z["messages"] == o["messages"] == messages
    assert z["response_format"] == o["response_format"]
    for raw in (bj.request_body(zai, messages), bj.request_body(openai, messages)):
        assert b"not-a-key" not in raw


_MSG = [{"role": "user", "content": "P"}]
_OK = '{"rationale":"r","choice":"A"}'


@pytest.mark.parametrize(
    ("reply", "passed", "code"),
    [
        ("A", True, None),
        ("B", False, None),
        (b"not json", None, "JUDGE_OUTPUT_INVALID"),
        (_completion("other-model", _OK), None, "JUDGE_IDENTITY_MISMATCH"),
        (_completion("gpt-5.6-luna-2026-10-01", _OK), True, None),
        (_completion("gpt-5.6-luna", _OK, "length"), None, "JUDGE_OUTPUT_INVALID"),
        (
            _completion("gpt-5.6-luna", '{"rationale":"r","choice":"C"}'),
            None,
            "JUDGE_OUTPUT_INVALID",
        ),
        (bj.BinaryJudgeError("JUDGE_HTTP_ERROR"), None, "JUDGE_HTTP_ERROR"),
    ],
)
def test_judge_once_outcomes(reply: Any, passed: Any, code: Any) -> None:
    _, openai = bj.provider_configs(ENV)
    verdict = bj.judge_once(_MSG, openai, FakeClient(lambda _n, _t: reply))
    assert (verdict.passed, verdict.code) == (passed, code)


def test_transport_error_retried_once() -> None:
    _, openai = bj.provider_configs(ENV)
    calls: list[int] = []

    def flaky(_n: str, _t: str) -> Any:
        calls.append(1)
        return bj.BinaryJudgeError("JUDGE_TRANSPORT_ERROR") if len(calls) == 1 else "B"

    verdict = bj.judge_once(_MSG, openai, FakeClient(flaky))
    assert (verdict.passed, verdict.attempts) == (False, 2)


def _item(attempt_id: str, verdicts: tuple[Any, Any], **kw: Any) -> dict[str, Any]:
    return {
        "attempt_id": attempt_id,
        "system": "opspilot",
        "scenario_id": "dv2-aaaaaa",
        "slot": 1,
        "judge_applicable": True,
        "verdicts": dict(zip(bj.JUDGE_NAMES, verdicts, strict=True)),
        "checks": {"q20.read_only": True},
        "expected_elements": ["E1"],
        "report_text": f"text of {attempt_id}",
        "rationales": {"glm": "glm reason", "openai": None},
        "errors": {"glm": None, "openai": "JUDGE_HTTP_ERROR"},
        **kw,
    }


def test_disputes_and_rulings() -> None:
    items = [
        _item("agree", (True, True)),
        _item("split", (True, False)),
        _item("err", (True, None)),
        _item("veto", (True, False), checks={"q20.read_only": False}),
    ]
    md = bj.disputes_markdown(items)
    assert "共 2 处" in md
    assert "`split`" in md and "`err`" in md and "`agree`" not in md
    assert "`veto`" not in md and "JUDGE_HTTP_ERROR" in md and "glm reason" in md
    assert "text of split" in md
    template = bj.rulings_template(items)
    assert set(template["rulings"]) == {"split", "err"}
    with pytest.raises(bj.BinaryJudgeError):
        bj.load_rulings(template | {"rulings": {"agree": "pass"}}, items)
    with pytest.raises(bj.BinaryJudgeError):
        bj.load_rulings(template | {"rulings": {"split": "maybe"}}, items)
    assert bj.load_rulings(template | {"rulings": {"split": "fail"}}, items) == {
        "split": False
    }


def test_redraw_detected_unless_voided() -> None:
    items = [_item(f"a{i}", (True, True), slot=i) for i in range(3)]
    with pytest.raises(bj.BinaryJudgeError):
        bj.summarize(items, {}, k=2)
    out = bj.summarize(items, {}, k=2, voided={"a0"})
    assert out["voided_attempts"] == ["a0"]
    assert out["systems"]["opspilot"]["n_attempts"] == 2
    assert "a0" not in out["attempts"]


def test_comparison_covers_common_judged_scenarios_only() -> None:
    def item(system: str, sid: str, aid: str, applicable: bool, ok: bool) -> Any:
        return _item(
            aid,
            (ok, ok),
            system=system,
            scenario_id=sid,
            judge_applicable=applicable,
        )

    items = [
        item("opspilot", "dv2-q1", "o1", True, True),
        item("opspilot", "dv2-q1", "o2", True, True),
        item("opspilot", "dv2-alert", "o3", True, False),  # no Holmes entry
        item("opspilot", "dv2-det", "o4", False, True),  # deterministic only
        item("holmes", "dv2-q1", "h1", True, False),
        item("holmes", "dv2-q1", "h2", True, True),
        item("holmes", "dv2-det", "h3", False, True),
    ]
    out = bj.summarize(items, {}, k=2)
    comparison = out["comparison"]
    assert comparison["scenarios"] == ["dv2-q1"]
    ops, holmes = comparison["systems"]["opspilot"], comparison["systems"]["holmes"]
    assert (ops["n_attempts"], ops["passed"], ops["pass_k"]) == (2, 2, 1.0)
    assert (holmes["n_attempts"], holmes["passed"], holmes["pass_k"]) == (2, 1, 0.0)
    # Own scores keep every attempt; deterministic-only rows stand apart.
    assert out["systems"]["opspilot"]["n_attempts"] == 4
    assert out["deterministic_only"]["opspilot"]["n_attempts"] == 1
    assert out["deterministic_only"]["holmes"]["n_attempts"] == 1


# --------------------------------------------------------------------------
# End to end: #432 evidence and a real #470 summarize round

HOLMES_FIXTURE = ROOT / "tests/fixtures/s7a-holmes-round"


def _judge_args(out: Path, *extra: str) -> Any:
    return cli.parser().parse_args(
        [
            "judge",
            "--dataset",
            str(DATASET),
            "--opspilot-round",
            str(DEV_ROUND),
            "--evidence-root",
            str(EVIDENCE),
            "--holmes-round",
            str(HOLMES_FIXTURE / "round.json"),
            "--holmes-attempts",
            str(HOLMES_FIXTURE / "attempts"),
            "--k",
            "2",
            "--out",
            str(out),
            *extra,
        ]
    )


def _decide(name: str, prompt: str) -> Any:
    if "Conclusion: partial" in prompt:
        return "A" if name == "glm" else "B"
    return "A"


def test_holmes_side_from_real_470_round() -> None:
    dataset = {
        sid: load_scenario_v2(DATASET / group / sid)
        for group, sid in (
            ("regression", "dv2-2b214b"),
            ("regression", "dv2-8e9455"),
            ("capability", "dv2-c39f60"),
            ("capability", "dv2-30921c"),
        )
    }
    doc = json.loads((HOLMES_FIXTURE / "round.json").read_text())
    rows = {
        a["attempt_id"]: a
        for a in cli.holmes_attempts(dataset, doc, HOLMES_FIXTURE / "attempts")
    }
    # The alert-entry scenario dv2-30921c is outside the denominator.
    assert set(rows) == {
        "h-pay-1",
        "h-pay-2",
        "h-norm-1",
        "holmes-not-started-dv2-8e9455-2",
        "h-mf-1",
        "holmes-not-started-dv2-c39f60-2",
    }
    raw = json.loads((HOLMES_FIXTURE / "attempts/h-pay-1/out/holmes.json").read_text())
    # Only the final answer reaches a judge, never the raw output.
    assert rows["h-pay-1"]["report_text"] == raw["result"]
    assert rows["h-pay-1"]["checks"] == {
        "final_state": True,
        "read_only": True,
        "report": True,
    }
    assert not bj.vetoed(rows["h-pay-1"])
    assert rows["h-pay-2"]["checks"]["report"] is False  # blank result
    assert rows["h-norm-1"]["checks"]["read_only"] is False  # a write request
    # model_failure positive: failing is the expected terminal state.
    assert rows["h-mf-1"]["checks"]["final_state"] is True
    assert not rows["h-mf-1"]["judge_applicable"]
    assert rows["holmes-not-started-dv2-8e9455-2"]["checks"]["final_state"] is False


def test_holmes_raw_hash_and_root_checked(tmp_path: Path) -> None:
    import shutil

    root = tmp_path / "attempts"
    shutil.copytree(HOLMES_FIXTURE / "attempts", root)
    dataset = {"dv2-2b214b": load_scenario_v2(DATASET / "regression/dv2-2b214b")}
    doc = json.loads((HOLMES_FIXTURE / "round.json").read_text())
    doc["slots"] = [s for s in doc["slots"] if s["scenario_id"] == "dv2-2b214b"]
    (root / "h-pay-1/out/holmes.json").write_text('{"result": "tampered"}')
    with pytest.raises(bj.BinaryJudgeError) as err:
        cli.holmes_attempts(dataset, doc, root)
    assert err.value.code == "HOLMES_RAW_MISMATCH:h-pay-1"
    doc["slots"][0]["raw"] = "../../etc/holmes.json"
    with pytest.raises(bj.BinaryJudgeError) as err:
        cli.holmes_attempts(dataset, doc, root)
    assert err.value.code == "HOLMES_RAW_OUTSIDE_ROOT:h-pay-1"


def test_opspilot_not_started_slot_counts_as_failure() -> None:
    dataset = {"dv2-2b214b": load_scenario_v2(DATASET / "regression/dv2-2b214b")}
    # eval_dev_round's never-started slot shape.
    slot = {
        "scenario_id": "dv2-2b214b",
        "slot": 2,
        "round": None,
        "attempt_id": None,
        "status": "not_started",
        "expectations": {"passed": False, "checks": {}, "reasons": ["NOT_STARTED"]},
    }
    (item,) = cli.opspilot_attempts(dataset, {"slots": [slot]}, EVIDENCE)
    assert item["attempt_id"] == "opspilot-not-started-dv2-2b214b-2"
    assert bj.vetoed(item) and item["root_cause_citation"] is False
    assert bj.attempt_pass(item, {}) is False


def test_cli_end_to_end(tmp_path: Path) -> None:
    out = tmp_path / "judge"
    client = FakeClient(_decide)
    result = cli.run_judge(_judge_args(out), client=client, environ=ENV)
    doc = json.loads((out / "items.json").read_text())
    ops = [a for a in doc["attempts"] if a["system"] == "opspilot"]
    holmes = [a for a in doc["attempts"] if a["system"] == "holmes"]
    assert (len(ops), len(holmes), result["attempts"]) == (36, 6, 42)
    # Deterministic-only rows (4 scenarios x 2) never reach a judge; the
    # #432 gate failures are exactly those rows, so nothing else is vetoed.
    only = [a for a in ops if not a["judge_applicable"]]
    assert {a["scenario_id"] for a in only} == {
        "dv2-fcd9eb",
        "dv2-7a98c2",
        "dv2-c39f60",
        "dv2-9f0fd3",
    }
    assert not any(bj.vetoed(a) for a in ops)
    judged = [a for a in ops if a["judge_applicable"]]
    assert all("gate.signature" in a["checks"] for a in judged)
    assert not any(k.startswith("gate.") for a in only for k in a["checks"])
    # Holmes: only h-pay-1 passes Q11 and is judge-applicable.
    sent = [body for _name, body in client.bodies]
    assert len(sent) == 2 * (len(judged) + 1)
    holmes_prompts = [b for b in sent if "trace 1a2b" in b["messages"][0]["content"]]
    assert len(holmes_prompts) == 2
    assert all("tool_calls" not in b["messages"][0]["content"] for b in holmes_prompts)
    assert "not-a-key" not in (out / "items.json").read_text()
    md = (out / "disputes.md").read_text()
    assert "OpsPilot" not in md and "HolmesGPT" not in md
    rulings = json.loads((out / "rulings.json").read_text())
    assert set(rulings["rulings"]) == {
        a["attempt_id"] for a in doc["attempts"] if bj.disputed(a)
    }
    rulings["rulings"] = dict.fromkeys(rulings["rulings"], "fail")
    (out / "rulings.json").write_text(json.dumps(rulings))
    summary_path = tmp_path / "summary.json"
    cli.run_summarize(
        cli.parser().parse_args(
            [
                "summarize",
                "--items",
                str(out / "items.json"),
                "--rulings",
                str(out / "rulings.json"),
                "--out",
                str(summary_path),
            ]
        )
    )
    summary = json.loads(summary_path.read_text())
    assert summary["final"] is True
    assert summary["systems"]["opspilot"]["n_attempts"] == 36
    row = summary["systems"]["holmes"]
    assert (row["n_attempts"], row["passed"]) == (6, 2)  # h-pay-1, h-mf-1
    assert summary["comparison"]["scenarios"] == ["dv2-2b214b", "dv2-8e9455"]
    assert summary["comparison"]["systems"]["holmes"]["n_attempts"] == 4
    assert summary["comparison"]["systems"]["opspilot"]["n_attempts"] == 4
    assert summary["deterministic_only"]["holmes"]["n_attempts"] == 2
    assert "report_text" not in summary_path.read_text()
    with pytest.raises(FileExistsError):
        cli.run_judge(_judge_args(out), client=client, environ=ENV)


def test_cli_drops_registered_voids(tmp_path: Path) -> None:
    victim = json.loads(DEV_ROUND.read_text())["slots"][0]["attempt_id"]
    log = {
        "schema": cli.PAIRED_LOG_SCHEMA,
        "windows": [
            {
                "window_id": "w1",
                "void": {"class": "ENV_SETUP_FAILED"},
                "slots": [
                    {"system": "opspilot", "attempt_id": victim},
                    {"system": "holmes", "attempt_id": "h-pay-1"},
                ],
            }
        ],
    }
    check = {"voided": [{"window_id": "w1", "systems": ["opspilot"]}]}
    (tmp_path / "log.json").write_text(json.dumps(log))
    (tmp_path / "check.json").write_text(json.dumps(check))
    out = tmp_path / "judge"
    client = FakeClient(_decide)
    args = _judge_args(
        out,
        "--schedule-log",
        str(tmp_path / "log.json"),
        "--schedule-check",
        str(tmp_path / "check.json"),
    )
    result = cli.run_judge(args, client=client, environ=ENV)
    assert result["voided"] == 1 and result["attempts"] == 41
    summary_path = tmp_path / "summary.json"
    rulings = json.loads((out / "rulings.json").read_text())
    assert victim not in rulings["rulings"]
    cli.run_summarize(
        cli.parser().parse_args(
            [
                "summarize",
                "--items",
                str(out / "items.json"),
                "--out",
                str(summary_path),
            ]
        )
    )
    summary = json.loads(summary_path.read_text())
    assert summary["voided_attempts"] == [victim]
    assert summary["systems"]["opspilot"]["n_attempts"] == 35
    assert summary["systems"]["holmes"]["n_attempts"] == 6


def test_judge_applicable_negative_fault_scenario_keeps_fault_question() -> None:
    answer = dataclasses.replace(_answer(), polarity="negative")
    assert bj.judge_applicable(answer)
    assert bj.FAULT_QUESTION in bj.judge_messages(answer, "R")[0]["content"]
