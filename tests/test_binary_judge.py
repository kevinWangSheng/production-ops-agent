"""Implementation-side tests for the Q9 binary judge (F1 closure S7a).

Offline: every judge call goes to a fake client. The end-to-end case runs on
the #432 development round's public evidence.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from opspilot.evaluation import binary_judge as bj
from opspilot.evaluation.codec import decode
from opspilot.evaluation.gate import parsed_report, run_scope
from opspilot.evaluation.model import load_scenarios_v2
from opspilot.evaluation.storage import EvaluationCapture
from scripts import eval_binary_judge as cli

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
    """Decides per (provider, prompt); records every body it was sent."""

    def __init__(self, decide: Callable[[str, str], str | bytes | Exception]) -> None:
        self.decide = decide
        self.bodies: list[tuple[str, dict[str, Any]]] = []

    def complete(self, config: bj.ProviderConfig, body: bytes) -> bytes:
        obj = json.loads(body)
        self.bodies.append((config.provider, obj))
        result = self.decide(config.provider, obj["messages"][0]["content"])
        if isinstance(result, Exception):
            raise result
        if isinstance(result, bytes):
            return result
        return _completion(
            config.model,
            json.dumps({"rationale": f"{config.provider} says", "choice": result}),
        )


def _captures() -> list[tuple[dict[str, Any], Any]]:
    doc = json.loads(DEV_ROUND.read_text())
    out = []
    for slot in doc["slots"]:
        path = EVIDENCE / slot["round"] / "runs" / slot["attempt_id"] / "summary.json"
        capture = decode(EvaluationCapture, json.loads(path.read_text())["capture"])
        out.append((slot, capture))
    return out


@pytest.fixture(scope="module")
def captures() -> list[tuple[dict[str, Any], Any]]:
    return _captures()


def _fault_capture(captures: list[tuple[dict[str, Any], Any]]) -> Any:
    for slot, capture in captures:
        if capture.answer.signature is not None and slot["gate"]["passed"]:
            return capture
    raise AssertionError("no fault capture with a passing gate")


def _subject(
    system: bj.System = "opspilot", text: str = "report", **kw: Any
) -> bj.JudgeSubject:
    base = bj.JudgeSubject(
        system=system,
        scenario_id="dv2-000001",
        attempt_id="a1",
        slot=1,
        category="regression",
        polarity="positive",
        expected_elements=("Names payment Charge errors as the cause.", "Cites spans."),
        output_text=text,
        q11=bj.Q11Result(True, {"execution": True}, ()),
    )
    return replace(base, **kw)


# --------------------------------------------------------------------------
# Prompt and projection


def test_prompt_is_upstream_strict_text_and_same_projection_for_both_systems() -> None:
    assert "A: All elements are presents" in bj.PROMPT_TEMPLATE
    ops = bj.judge_prompt(_subject("opspilot", "TEXT-X"))
    holmes = bj.judge_prompt(_subject("holmesgpt", "TEXT-X"))
    assert ops == holmes
    assert "- Names payment Charge errors as the cause.\n- Cites spans." in ops
    assert "{{" not in ops


def test_output_text_cannot_inject_template_slots() -> None:
    prompt = bj.judge_prompt(_subject(text="see {{expected}} here"))
    assert prompt.count("Names payment Charge errors") == 1
    assert "see {{expected}} here" in prompt


def test_render_report_lists_fields_without_raw_evidence_ids(captures: Any) -> None:
    capture = _fault_capture(captures)
    report = parsed_report(capture.report)
    assert report is not None
    text = bj.render_report(report, capture.delivered)
    assert text.startswith(f"Assessment: {report.assessment_status}\nConclusion: ")
    assert report.summary.strip() in text
    for claim in report.claims:
        assert claim.text.strip() in text
        for eid in claim.evidence_ids:
            assert eid not in text
    assert "E1 (" in text
    assert bj.render_report(report, capture.delivered) == text


# --------------------------------------------------------------------------
# Root-cause evidence citation column (section 1 D)


def test_root_cause_column_rule(captures: Any) -> None:
    capture = _fault_capture(captures)
    report = parsed_report(capture.report)
    assert report is not None
    scope = run_scope(capture.spec, capture.outcome)
    args = (capture.delivered, capture.authorized, scope)
    base = bj.root_cause_evidence_cited(capture.answer, report, *args)
    assert base is True
    inconclusive = report.model_copy(update={"conclusion": "inconclusive"})
    assert bj.root_cause_evidence_cited(capture.answer, inconclusive, *args) is False
    as_recommendation = report.model_copy(
        update={
            "claims": [
                c.model_copy(update={"kind": "recommendation"}) for c in report.claims
            ]
        }
    )
    assert (
        bj.root_cause_evidence_cited(capture.answer, as_recommendation, *args) is False
    )
    assert bj.root_cause_evidence_cited(capture.answer, None, *args) is False


def test_opspilot_subject_q11_equals_stored_gate(captures: Any) -> None:
    for slot, capture in captures:
        subject = bj.opspilot_subject(capture, slot=slot["slot"])
        assert subject.q11.passed is slot["gate"]["passed"]
        assert list(subject.q11.reasons) == slot["gate"]["reasons"]
        fault = capture.answer.signature is not None
        assert (subject.root_cause_cited is None) is not fault


# --------------------------------------------------------------------------
# Providers and verdicts


def test_provider_configs_names_missing_variable_not_value() -> None:
    with pytest.raises(bj.BinaryJudgeError) as err:
        bj.provider_configs({"ZAI_API_KEY": "x"})
    assert err.value.code == "JUDGE_KEY_MISSING:OPENAI_API_KEY"
    with pytest.raises(bj.BinaryJudgeError) as err:
        bj.provider_configs(ENV | {"OPENAI_BASE_URL": "https://evil.example/v1"})
    assert err.value.code == "JUDGE_BASE_URL_NOT_ALLOWED:OPENAI_BASE_URL"
    with pytest.raises(bj.BinaryJudgeError):
        bj.provider_configs(ENV | {"OPSPILOT_JUDGE_MODEL": "deepseek-flash"})
    configs = bj.provider_configs(ENV | {"OPENAI_JUDGE_MODEL": "gpt-x"})
    assert [(c.provider, c.model) for c in configs] == [
        ("zai", "glm-5.3-flashx"),
        ("openai", "gpt-x"),
    ]
    assert "not-a-key" not in repr(configs)
    assert "not-a-key" not in json.dumps(bj.judge_config_record(configs))


def test_request_body_per_provider_carries_no_key() -> None:
    zai, openai = bj.provider_configs(ENV)
    z = json.loads(bj.request_body(zai, "P"))
    o = json.loads(bj.request_body(openai, "P"))
    assert z["reasoning_effort"] == "high" and z["max_tokens"] == 16384
    assert o["max_completion_tokens"] == 16384 and "reasoning_effort" not in o
    assert z["messages"] == o["messages"] == [{"role": "user", "content": "P"}]
    assert z["response_format"] == o["response_format"]
    for raw in (bj.request_body(zai, "P"), bj.request_body(openai, "P")):
        assert b"not-a-key" not in raw


@pytest.mark.parametrize(
    ("reply", "status", "passed", "code"),
    [
        ("A", "ok", True, None),
        ("B", "ok", False, None),
        (b"not json", "error", None, "JUDGE_OUTPUT_INVALID"),
        (
            _completion("other-model", '{"rationale":"r","choice":"A"}'),
            "error",
            None,
            "JUDGE_IDENTITY_MISMATCH",
        ),
        (
            _completion("gpt-5.6-luna", '{"rationale":"r","choice":"A"}', "length"),
            "error",
            None,
            "JUDGE_OUTPUT_INVALID",
        ),
        (
            _completion("gpt-5.6-luna", '{"rationale":"r","choice":"C"}'),
            "error",
            None,
            "JUDGE_OUTPUT_INVALID",
        ),
        (bj.BinaryJudgeError("JUDGE_HTTP_ERROR"), "error", None, "JUDGE_HTTP_ERROR"),
    ],
)
def test_judge_once_outcomes(reply: Any, status: str, passed: Any, code: Any) -> None:
    _, openai = bj.provider_configs(ENV)
    verdict = bj.judge_once("P", openai, FakeClient(lambda _p, _t: reply))
    assert (verdict.status, verdict.passed, verdict.code) == (status, passed, code)


def test_dated_response_model_accepted_and_transport_error_retried_once() -> None:
    _, openai = bj.provider_configs(ENV)
    dated = _completion("gpt-5.6-luna-2026-10-01", '{"rationale":"r","choice":"A"}')
    assert bj.judge_once("P", openai, FakeClient(lambda _p, _t: dated)).passed is True
    calls: list[int] = []

    def flaky(_p: str, _t: str) -> Any:
        calls.append(1)
        return bj.BinaryJudgeError("JUDGE_TRANSPORT_ERROR") if len(calls) == 1 else "B"

    verdict = bj.judge_once("P", openai, FakeClient(flaky))
    assert (verdict.passed, verdict.attempts) == (False, 2)


# --------------------------------------------------------------------------
# Agreement, disputes, rulings, summary


def _items(
    decide: Callable[[str, str], Any], subjects: list[bj.JudgeSubject]
) -> list[bj.JudgedItem]:
    return bj.judge_subjects(subjects, bj.provider_configs(ENV), FakeClient(decide))


def test_vetoed_subject_is_not_sent_and_fails_whatever_judges_say() -> None:
    client = FakeClient(lambda _p, _t: "A")
    vetoed = _subject(
        q11=bj.Q11Result(False, {"permissions": False}, ("PERMISSION_VIOLATION",))
    )
    items = bj.judge_subjects([vetoed], bj.provider_configs(ENV), client)
    assert client.bodies == []
    assert items[0].agreement == "vetoed"
    assert bj.final_pass(items[0], {}) is False


def test_disputes_and_rulings_round_trip() -> None:
    subjects = [
        _subject(attempt_id="agree", text="good"),
        _subject(attempt_id="split", text="half", slot=2),
        _subject("holmesgpt", attempt_id="err", text="prose"),
    ]

    def decide(provider: str, prompt: str) -> Any:
        if "half" in prompt:
            return "A" if provider == "zai" else "B"
        if "prose" in prompt and provider == "openai":
            return bj.BinaryJudgeError("JUDGE_HTTP_ERROR")
        return "A"

    items = _items(decide, subjects)
    assert [i.agreement for i in items] == ["agreed", "disagreed", "judge_error"]
    md = bj.disputes_markdown(items)
    assert "共 2 处" in md
    assert "opspilot:dv2-000001:split" in md and "holmesgpt:dv2-000001:err" in md
    assert "zai says" in md and "half" in md and "JUDGE_HTTP_ERROR" in md
    assert "opspilot:dv2-000001:agree" not in md
    template = bj.rulings_template(items)
    assert set(template["rulings"]) == {
        "opspilot:dv2-000001:split",
        "holmesgpt:dv2-000001:err",
    }
    with pytest.raises(bj.BinaryJudgeError):
        bj.load_rulings(
            template | {"rulings": {"opspilot:dv2-000001:agree": "pass"}}, items
        )
    with pytest.raises(bj.BinaryJudgeError):
        bj.load_rulings(
            template | {"rulings": {"opspilot:dv2-000001:split": "maybe"}}, items
        )
    assert bj.load_rulings(template, items) == {}


def test_summary_pending_then_final_with_pass_k_and_veto_first() -> None:
    subjects = [
        _subject(scenario_id="dv2-00000a", attempt_id="a1", slot=1, text="good"),
        _subject(scenario_id="dv2-00000a", attempt_id="a2", slot=2, text="half"),
        _subject(scenario_id="dv2-00000b", attempt_id="b1", slot=1, text="good"),
        _subject(
            scenario_id="dv2-00000b",
            attempt_id="b2",
            slot=2,
            text="good",
            q11=bj.Q11Result(False, {"citations": False}, ("TARGET_MISMATCH",)),
        ),
        # Scenario c: one slot never produced a subject (missing = failure).
        _subject(scenario_id="dv2-00000c", attempt_id="c1", slot=1, text="good"),
    ]

    def decide(provider: str, prompt: str) -> Any:
        if "half" in prompt:
            return "A" if provider == "zai" else "B"
        return "A"

    items = _items(decide, subjects)
    config = {"prompt_sha256": bj.PROMPT_SHA256}
    pending = bj.summarize(items, {}, k=2, judge_config=config)
    ops = pending["systems"]["opspilot"]
    assert pending["status"] == ops["status"] == "awaiting_rulings"
    assert ops["single_run"] is None and ops["pending_rulings"] == [
        "opspilot:dv2-00000a:a2"
    ]
    final = bj.summarize(
        items, {"opspilot:dv2-00000a:a2": True}, k=2, judge_config=config
    )
    ops = final["systems"]["opspilot"]
    assert final["status"] == "final"
    # 6 slots (3 scenarios x 2); passes a1, a2 (ruled), b1, c1; b2 vetoed; c2 missing.
    assert ops["single_run"]["successes"] == 4 and ops["single_run"]["n"] == 6
    assert ops["pass^2"]["successes"] == 1 and ops["pass^2"]["n"] == 3
    assert ops["q11_vetoed"] == 1
    assert ops["judge"]["agreement"] == {
        "successes": 3,
        "n": 4,
        "rate": 0.75,
        "wilson95": bj.wilson(3, 4),
    }
    assert ops["root_cause_evidence_citation"]["in_comparison_denominator"] is False
    with pytest.raises(bj.BinaryJudgeError):
        bj.summarize(items, {}, k=1, judge_config=config)


def test_wilson_interval_known_value() -> None:
    assert bj.wilson(0, 0) is None
    assert bj.wilson(8, 10) == [0.4902, 0.9433]


def test_holmes_subject_q11() -> None:
    _spec, answer = next(
        (s, a) for s, a in load_scenarios_v2(DATASET) if s.scenario_id == "dv2-2b214b"
    )
    ok = bj.holmes_subject(
        {"result": "## Report\npayment Charge fails", "finish_reason": "stop"},
        answer,
        attempt_id="h1",
        slot=1,
        read_only=True,
    )
    assert ok.q11.passed and ok.output_text == "## Report\npayment Charge fails"
    assert ok.root_cause_cited is None
    assert ok.expected_elements == answer.expected_output
    stopped = bj.holmes_subject(
        {"result": "", "finish_reason": "length"},
        answer,
        attempt_id="h2",
        slot=2,
        read_only=False,
    )
    assert not stopped.q11.passed
    assert stopped.q11.reasons == ("PERMISSION_VIOLATION", "EXECUTION_NOT_COMPLETED")
    assert stopped.output_text is None


# --------------------------------------------------------------------------
# CLI, end to end on the #432 evidence with a fake client


def test_cli_end_to_end_on_432_evidence(tmp_path: Path) -> None:
    holmes_out = tmp_path / "holmes" / "h1.json"
    holmes_out.parent.mkdir()
    holmes_out.write_text(
        json.dumps(
            {
                "result": "payment Charge RPC errors cause checkout failures; "
                "trace 1a2b shows error spans",
                "finish_reason": "stop",
            }
        )
    )
    inputs = tmp_path / "holmes" / "inputs.json"
    inputs.write_text(
        json.dumps(
            {
                "schema": bj.HOLMES_INPUTS_SCHEMA,
                "attempts": [
                    {
                        "scenario_id": "dv2-2b214b",
                        "attempt_id": "h1",
                        "slot": 1,
                        "holmes_json": "h1.json",
                        "read_only": True,
                    },
                    {
                        "scenario_id": "dv2-2b214b",
                        "attempt_id": "h2",
                        "slot": 2,
                        "holmes_json": None,
                        "read_only": True,
                    },
                ],
            }
        )
    )

    def decide(provider: str, prompt: str) -> Any:
        # Split on regression reports only, to exercise the dispute path.
        if "Conclusion: partial" in prompt:
            return "A" if provider == "zai" else "B"
        return "A"

    out = tmp_path / "judge"
    args = cli.parser().parse_args(
        [
            "judge",
            "--dataset",
            str(DATASET),
            "--dev-round",
            str(DEV_ROUND),
            "--evidence-root",
            str(EVIDENCE),
            "--holmes",
            str(inputs),
            "--k",
            "2",
            "--out",
            str(out),
        ]
    )
    client = FakeClient(decide)
    result = cli.run_judge(args, client=client, environ=ENV)
    doc = json.loads((out / "items.json").read_text())
    ops = [i for i in doc["items"] if i["subject"]["system"] == "opspilot"]
    assert len(ops) == 36 and result["items"] == 38
    gated = json.loads(DEV_ROUND.read_text())["totals"]
    vetoed = sum(1 for i in ops if i["agreement"] == "vetoed")
    assert (
        vetoed
        == 36 - gated["capability"]["gate_passed"] - gated["regression"]["gate_passed"]
    )
    # Two calls (one per provider) per non-vetoed subject, none for vetoed ones.
    assert len(client.bodies) == 2 * (38 - vetoed - 1)
    assert "not-a-key" not in (out / "items.json").read_text()
    rulings = json.loads((out / "rulings.json").read_text())
    assert set(rulings["rulings"]) == {
        i["item_id"]
        for i in doc["items"]
        if i["agreement"] in ("disagreed", "judge_error")
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
    assert summary["status"] == "final"
    assert summary["systems"]["opspilot"]["single_run"]["n"] == 36
    assert summary["systems"]["holmesgpt"]["single_run"] == {
        "successes": 1,
        "n": 2,
        "rate": 0.5,
        "wilson95": bj.wilson(1, 2),
    }
    assert "output_text" not in summary_path.read_text()
    with pytest.raises(FileExistsError):
        cli.run_judge(args, client=client, environ=ENV)
