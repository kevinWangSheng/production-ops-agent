"""Implementation-side tests for the Q9 binary judge (F1 closure S7a).

Offline: every judge call goes to a fake client. The end-to-end case runs on
the #432 development round's public evidence plus a synthetic HolmesGPT
round in the ``scripts.holmes_baseline.dataset`` summary shape.
"""

from __future__ import annotations

import dataclasses
import hashlib
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


def test_redraw_detected() -> None:
    items = [_item(f"a{i}", (True, True)) for i in range(3)]
    with pytest.raises(bj.BinaryJudgeError):
        bj.summarize(items, {}, k=2)


# --------------------------------------------------------------------------
# End to end on the #432 evidence


def _holmes_round(tmp: Path) -> tuple[Path, Path]:
    attempts = tmp / "holmes"
    (attempts / "h1").mkdir(parents=True)
    report = "payment Charge RPC errors cause checkout failures; trace 1a2b"
    (attempts / "h1" / "report.md").write_text(report)
    slot = {
        "system": "holmesgpt",
        "scenario_id": "dv2-2b214b",
        "in_denominator": True,
        "final_state_expected": True,
    }
    doc = {
        "schema": cli.HOLMES_ROUND_SCHEMA,
        "slots": [
            slot
            | {
                "slot": 1,
                "status": "ran",
                "attempt_id": "h1",
                "read_only": True,
                "report": {
                    "available": True,
                    "path": "h1/report.md",
                    "sha256": hashlib.sha256(report.encode()).hexdigest(),
                },
            },
            slot
            | {
                "slot": 2,
                "status": "not_started",
                "attempt_id": None,
                "final_state_expected": False,
                "report": {"available": False, "path": None, "sha256": None},
            },
            {
                "scenario_id": "dv2-30921c",
                "slot": 1,
                "in_denominator": False,
                "status": "not_applicable",
                "attempt_id": None,
            },
        ],
    }
    path = tmp / "holmes-round.json"
    path.write_text(json.dumps(doc))
    return path, attempts


def test_cli_end_to_end_on_432_evidence(tmp_path: Path) -> None:
    holmes_round, holmes_dir = _holmes_round(tmp_path)

    def decide(name: str, prompt: str) -> Any:
        if "Conclusion: partial" in prompt:
            return "A" if name == "glm" else "B"
        return "A"

    out = tmp_path / "judge"
    args = cli.parser().parse_args(
        [
            "judge",
            "--dataset",
            str(DATASET),
            "--opspilot-round",
            str(DEV_ROUND),
            "--evidence-root",
            str(EVIDENCE),
            "--holmes-round",
            str(holmes_round),
            "--holmes-attempts",
            str(holmes_dir),
            "--k",
            "2",
            "--out",
            str(out),
        ]
    )
    client = FakeClient(decide)
    result = cli.run_judge(args, client=client, environ=ENV)
    doc = json.loads((out / "items.json").read_text())
    ops = [a for a in doc["attempts"] if a["system"] == "opspilot"]
    holmes = [a for a in doc["attempts"] if a["system"] == "holmes"]
    assert len(ops) == 36 and len(holmes) == 2 and result["attempts"] == 38
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
    assert len(client.bodies) == 2 * (len(judged) + 1)
    assert "not-a-key" not in (out / "items.json").read_text()
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
    assert (row["n_attempts"], row["passed"], row["pass_k"]) == (2, 1, 0.0)
    assert "report_text" not in summary_path.read_text()
    with pytest.raises(FileExistsError):
        cli.run_judge(args, client=client, environ=ENV)


def test_holmes_report_hash_mismatch_refused(tmp_path: Path) -> None:
    holmes_round, holmes_dir = _holmes_round(tmp_path)
    (holmes_dir / "h1" / "report.md").write_text("tampered")
    dataset = {
        "dv2-2b214b": load_scenario_v2(DATASET / "regression/dv2-2b214b"),
        "dv2-30921c": load_scenario_v2(DATASET / "capability/dv2-30921c"),
    }
    with pytest.raises(bj.BinaryJudgeError) as err:
        cli.holmes_attempts(dataset, json.loads(holmes_round.read_text()), holmes_dir)
    assert err.value.code == "HOLMES_REPORT_MISMATCH:h1"


def test_judge_applicable_negative_fault_scenario_keeps_fault_question() -> None:
    answer = dataclasses.replace(_answer(), polarity="negative")
    assert bj.judge_applicable(answer)
    assert bj.FAULT_QUESTION in bj.judge_messages(answer, "R")[0]["content"]
