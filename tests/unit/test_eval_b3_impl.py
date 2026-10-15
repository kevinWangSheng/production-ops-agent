"""PR-B3 implementation regressions: only fake transports, no real credentials."""

import hashlib
import json
import os
import stat
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx2
import pytest

from opspilot.acceptance import IncidentOutcome
from opspilot.evaluation import (
    DeliveredRecord,
    JudgedDiagnosis,
    ReportCapture,
    Window,
    canonical_view_hash,
    investigator_input,
    load_scenario,
    load_scenarios,
)
from opspilot.evaluation.calibration_flow import (
    COVERAGE,
    Adjudication,
    AnnotationSet,
    BlindLabel,
    CalibrationItem,
    LabelPack,
    final_labels,
    freeze_label_pack,
    label_pack_hash,
    load_label_pack,
    load_verified_calibration,
    run_calibration,
)
from opspilot.evaluation.codec import canonical_bytes
from opspilot.evaluation.judge import (
    HTTPJudgeTransport,
    JudgeConfig,
    JudgeError,
    JudgeHTTPResponse,
    JudgeInput,
    judge_config_hash,
    judge_once,
    load_judge_config,
    safe_input,
)
from opspilot.investigation.loop import serialized_request
from tests.m1_investigation_support import (
    assemble,
    reply,
    report_from_transcript,
    tool_call,
)

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ROOT / "evals/scenarios"
START = datetime(2026, 10, 14, tzinfo=UTC)
END = START + timedelta(minutes=5)
SECRET = "fake-test-key-do-not-persist"


def sha(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def configs():
    return {
        "zai": JudgeConfig(
            "zai",
            "https://api.z.ai/api/paas/v4/chat/completions",
            "glm-5.3",
            "glm-5.3",
            SECRET,
        ),
        "openai": JudgeConfig(
            "openai",
            "https://api.openai.com/v1/chat/completions",
            "gpt-6.1-sol",
            "gpt-6.1-sol",
            SECRET,
        ),
        "deepseek": JudgeConfig(
            "deepseek",
            "https://api.deepseek.com/chat/completions",
            "deepseek-flash",
            "deepseek-flash",
            SECRET,
        ),
    }


def make_input(sid="eval-cart-failure", *, positive=True, suffix="single"):
    spec, answer = load_scenario(SCENARIOS / sid)
    spec = replace(
        spec, stimulus=replace(spec.stimulus, question=f"Investigate sample {suffix}")
    )
    fault = spec.kind == "fault"
    injected = answer.injected
    service = injected.service if fault else "wrong-service"
    operation = injected.operation if fault else "wrong/Operation"
    causal = fault and positive or not fault and not positive
    text = (
        f"{service} {operation} caused errors."
        if causal
        else "Evidence does not identify a cause."
    )
    view = {
        "tool": "traces_search",
        "content": [
            {
                "service": service,
                "operation": answer.signature.any_of[0].operation
                if fault
                else operation,
                "span_id": "span-1",
                "trace_id": "trace-1",
                "start_us": int((START + timedelta(seconds=1)).timestamp() * 1_000_000),
                "operation_truncated": False,
                "status_tags": {"error": True},
            }
        ],
    }
    evidence = DeliveredRecord(
        "e1",
        spec.stimulus.target_id,
        "ok",
        True,
        frozenset({"w"}),
        Window(START, END),
        view,
        canonical_view_hash(view),
        True,
    )
    report = ReportCapture(
        json.dumps(
            {
                "schema_version": "m0-report-v2",
                "assessment_status": "completed",
                "conclusion": "partial",
                "summary": text,
                "claims": [
                    {
                        "kind": "fact",
                        "text": text,
                        "evidence_ids": ["e1"],
                        "target_refs": [spec.stimulus.target_id],
                        "time_scope_ref": "w",
                    }
                ],
                "gaps": [],
                "next_steps": [],
            }
        ),
        "stop",
    )
    outcome = IncidentOutcome(
        sid,
        "completed",
        ("e1",),
        "report_available",
        (),
        ("read_only",),
        None,
        (),
        True,
    )
    request = JudgeInput(
        spec, answer, report, {"e1": evidence}, {"w": Window(START, END)}, outcome
    )
    diagnoses = (
        (
            JudgedDiagnosis(
                "claims[0]",
                text,
                True,
                service,
                operation,
                ("e1",),
                None,
                True,
                "Visible support.",
            ),
        )
        if causal
        else ()
    )
    return request, diagnoses, positive


def completion(config, diagnoses=(), verdict=True, **overrides):
    content = {
        "schema": "opspilot-judge-1",
        "diagnoses": asdict_labels(diagnoses),
        "verdict": verdict,
    }
    response = {
        "model": config.response_model,
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(content)},
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 12, "total_tokens": 32},
    }
    response.update(overrides)
    return canonical_bytes(response)


def asdict_labels(diagnoses):
    return [asdict(d) for d in diagnoses]


class FakeTransport:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def send(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.value, Exception):
            raise self.value
        value = self.value(kwargs) if callable(self.value) else self.value
        return (
            value
            if isinstance(value, JudgeHTTPResponse)
            else JudgeHTTPResponse(200, (value,))
        )


def invoke(
    request=None,
    diagnoses=None,
    verdict=None,
    *,
    config=None,
    response=None,
    calibration=None,
):
    if request is None:
        request, diagnoses, verdict = make_input()
    config = configs()["zai"] if config is None else config
    transport = FakeTransport(
        completion(config, diagnoses or (), verdict) if response is None else response
    )
    observation = judge_once(
        request, config=config, calibration=calibration, transport=transport
    )
    return observation, transport


def test_config_template_and_secret_hash_boundary(tmp_path):
    path = tmp_path / "private-config"
    path.write_text("ZAI_API_KEY=" + SECRET + "\nOPENAI_API_KEY=other-fake\n")
    path.chmod(0o600)
    config = load_judge_config(path, environ={})
    assert config.provider == "zai" and config.api_key == SECRET
    assert SECRET not in repr(config)
    assert judge_config_hash(config) == judge_config_hash(
        replace(config, api_key="rotated")
    )
    assert judge_config_hash(config) != judge_config_hash(
        replace(config, max_output_tokens=100)
    )
    assert judge_config_hash(config) != judge_config_hash(
        replace(config, max_response_bytes=100)
    )
    with pytest.raises(JudgeError, match="CONFIG_INVALID"):
        load_judge_config(path, environ={}, provider="openai")


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o400, 0o700])
def test_config_permissions_checked_before_read(tmp_path, mode):
    path = tmp_path / "private-config"
    path.write_text("not-even-config")
    path.chmod(mode)
    with pytest.raises(JudgeError, match="CONFIG_PRIVATE_REGULAR_FILE_REQUIRED"):
        load_judge_config(path, environ={})


@pytest.mark.parametrize(
    "payload,code",
    [
        ("UNKNOWN=" + SECRET, "CONFIG_SYNTAX"),
        ("ZAI_API_KEY=x\nZAI_API_KEY=y", "CONFIG_SYNTAX"),
        ("ZAI_API_KEY=$(echo secret)", "CONFIG_SYNTAX"),
        ("ZAI_API_KEY='broken", "CONFIG_SYNTAX"),
        ("ZAI_API_KEY=x\nZAI_BASE_URL=https://evil.example", "CONFIG_INVALID"),
        ("ZAI_API_KEY=x\nOPSPILOT_JUDGE_MODEL=glm-other", "JUDGE_IDENTITY_MISMATCH"),
    ],
)
def test_config_fixed_errors(tmp_path, payload, code):
    path = tmp_path / "private-config"
    path.write_text(payload)
    path.chmod(0o600)
    with pytest.raises(JudgeError) as caught:
        load_judge_config(path, environ={})
    assert str(caught.value) == code
    assert caught.value.__suppress_context__ or caught.value.__context__ is None


def test_config_symlink_conflict_and_no_ambient_fallback(tmp_path, monkeypatch):
    path = tmp_path / "private-config"
    path.write_text("ZAI_API_KEY=x")
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(JudgeError, match="CONFIG_UNREADABLE"):
        load_judge_config(link, environ={})
    with pytest.raises(JudgeError, match="CONFIG_ENV_CONFLICT"):
        load_judge_config(path, environ={"ZAI_API_KEY": SECRET})
    monkeypatch.setenv("ZAI_API_KEY", SECRET)
    path.write_text("# no key")
    with pytest.raises(JudgeError, match="CONFIG_INVALID"):
        load_judge_config(path, environ={"ZAI_API_KEY": SECRET})
    with pytest.raises(JudgeError, match="CONFIG_ABSOLUTE_PATH_REQUIRED"):
        load_judge_config(Path("relative"), environ={})


@pytest.mark.parametrize("provider", ["zai", "openai", "deepseek"])
def test_adapters_fresh_messages_no_retries_no_credentials_in_body(provider):
    config = configs()[provider]
    observation, transport = invoke(config=config)
    assert (
        observation.valid and observation.recomputed_verdict and not observation.passed
    )
    assert not observation.result.calibrated
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["headers"]["Authorization"] == "Bearer " + SECRET
    body = json.loads(call["body"])
    assert body["model"] == config.request_model and body["stream"] is False
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert (
        body["max_completion_tokens" if provider == "openai" else "max_tokens"] == 16384
    )
    assert SECRET.encode() not in call["body"]
    assert observation.usage["total_tokens"] == 32


@pytest.mark.parametrize(
    "response,status,code",
    [
        (TimeoutError(SECRET), "timeout", "JUDGE_TIMEOUT"),
        (RuntimeError(SECRET), "error", "JUDGE_TRANSPORT_ERROR"),
        (JudgeHTTPResponse(401, (SECRET.encode(),)), "error", "JUDGE_HTTP_ERROR"),
        (b'{"model":"x","model":"y"}', "invalid", "SCHEMA_INVALID"),
        (b"not-json", "invalid", "SCHEMA_INVALID"),
    ],
)
def test_call_failures_are_credential_free_and_zero_retry(response, status, code):
    observation, transport = invoke(response=response)
    assert observation.result.status == status and observation.error_code == code
    assert not observation.passed and not observation.valid
    assert len(transport.calls) == 1 and SECRET not in repr(observation)


@pytest.mark.parametrize(
    "mutation",
    [
        "identity",
        "length",
        "missing",
        "bool_string",
        "extra",
        "location",
        "quote",
        "citation",
        "and",
        "private_output",
        "negative_link",
        "multiple_choices",
    ],
)
def test_output_schema_and_p6_recompute(mutation):
    request, diagnoses, verdict = make_input()
    config = configs()["zai"]
    obj = json.loads(completion(config, diagnoses, verdict))
    choice = obj["choices"][0]
    content = json.loads(choice["message"]["content"])
    if mutation == "identity":
        obj["model"] = "other"
    elif mutation == "length":
        choice["finish_reason"] = "length"
    elif mutation == "multiple_choices":
        obj["choices"].append(choice.copy())
    elif mutation == "missing":
        del content["diagnoses"][0]["reason"]
    elif mutation == "bool_string":
        content["verdict"] = "true"
    elif mutation == "extra":
        content["calibrated"] = True
    elif mutation == "private_output":
        content["reasoning_content"] = SECRET
    elif mutation == "negative_link":
        content["diagnoses"][0]["linked_claim"] = -1
    elif mutation == "and":
        content["diagnoses"][0]["supported"] = False
    else:
        content["diagnoses"][0][
            {
                "location": "location",
                "quote": "quote",
                "citation": "cited_evidence_ids",
            }[mutation]
        ] = ["unknown"] if mutation == "citation" else "absent"
    choice["message"]["content"] = json.dumps(content)
    observation, transport = invoke(
        request, diagnoses, verdict, response=canonical_bytes(obj)
    )
    assert not observation.valid and not observation.passed
    assert observation.result.status == (
        "error" if mutation == "identity" else "invalid"
    )
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "key",
    [
        "reasoning_content",
        "thinking",
        "encrypted_content",
        "OPENAI_API_KEY",
        "password",
        "token",
        "credential",
        "api-key",
        "client_secret",
        "refresh-token",
        "x-api-key",
        "db_api_key",
        "http.request.header.authorization",
    ],
)
def test_private_dto_fields_never_reach_transport(key):
    request, diagnoses, verdict = make_input()
    record = request.delivered["e1"]
    view = {**record.view, key: "private-sentinel"}
    request = replace(
        request,
        delivered={
            "e1": replace(record, view=view, view_sha256=canonical_view_hash(view))
        },
    )
    observation, transport = invoke(request, diagnoses, verdict)
    assert not observation.valid and observation.result.status == "invalid"
    assert transport.calls == []


def test_unknown_top_level_dto_never_reaches_transport():
    request, diagnoses, verdict = make_input()
    dto = asdict(request)
    dto["reasoning_content"] = "private-sentinel"
    observation, transport = invoke(dto, diagnoses, verdict)
    assert observation.result.status == "invalid" and not transport.calls


def test_unreferenced_and_other_run_evidence_cannot_change_body():
    request, diagnoses, verdict = make_input()
    original, first = invoke(request, diagnoses, verdict)
    extra = replace(
        request.delivered["e1"],
        evidence_id="not-referenced",
        view={"arbitrary": "sentinel"},
    )
    changed = replace(request, delivered={**request.delivered, "not-referenced": extra})
    _, second = invoke(changed, diagnoses, verdict)
    assert first.calls[0]["body"] == second.calls[0]["body"]
    assert original.valid
    other_run = replace(changed, outcome=replace(request.outcome, evidence_ids=()))
    assert safe_input(other_run)["cited_evidence"] == []


def test_request_and_chunked_response_oversize():
    config = replace(configs()["zai"], max_request_bytes=100)
    observation, transport = invoke(config=config)
    assert observation.result.status == "oversize" and not transport.calls
    config = replace(configs()["zai"], max_response_bytes=10)
    observation, transport = invoke(
        config=config, response=JudgeHTTPResponse(200, (b"123456", b"789012"))
    )
    assert observation.result.status == "oversize" and len(transport.calls) == 1


def test_http_transport_mock_redirect_size_and_total_deadline():
    calls = []
    config = configs()["zai"]

    def handler(request):
        calls.append(request)
        return httpx2.Response(
            302, headers={"Location": "https://other.example"}, content=b"redirect"
        )

    transport = HTTPJudgeTransport(transport=httpx2.MockTransport(handler))
    request, _, _ = make_input()
    observation = judge_once(
        request, config=config, calibration=None, transport=transport
    )
    assert len(calls) == 1 and observation.error_code == "JUDGE_HTTP_ERROR"
    transport = HTTPJudgeTransport(
        transport=httpx2.MockTransport(
            lambda _: httpx2.Response(200, content=b"x" * 20)
        )
    )
    observation = judge_once(
        request,
        config=replace(config, max_response_bytes=10),
        calibration=None,
        transport=transport,
    )
    assert observation.result.status == "oversize"

    class SlowStream(httpx2.AsyncByteStream):
        async def __aiter__(self):
            import asyncio

            for _ in range(20):
                await asyncio.sleep(0.002)
                yield b"x"

    transport = HTTPJudgeTransport(
        transport=httpx2.MockTransport(
            lambda _: httpx2.Response(200, stream=SlowStream())
        )
    )
    observation = judge_once(
        request,
        config=replace(config, timeout_seconds=0.015),
        calibration=None,
        transport=transport,
    )
    assert observation.result.status == "timeout"


@pytest.mark.parametrize("change", ["answer", "flag", "path"])
def test_answer_flag_and_path_do_not_change_actual_investigation_requests(
    tmp_path, change, monkeypatch
):
    original = SCENARIOS / "eval-cart-failure"
    destination = tmp_path / "renamed-scenario"
    destination.mkdir()
    for name in ("scenario.json", "answer.json"):
        (destination / name).write_bytes((original / name).read_bytes())
    if change in ("answer", "flag"):
        payload = json.loads((destination / "answer.json").read_text())
        if change == "answer":
            payload["expected_output"] = ["evaluator-only-sentinel"]
        else:
            payload["injected"]["flag"] = "evaluator-only-sentinel"
        (destination / "answer.json").write_text(json.dumps(payload))
    sequences = []
    frozen_tool = []
    for directory in (original, destination):
        spec, _ = load_scenario(directory)
        stimulus = investigator_input(spec)
        loop, request, model, _, _, _ = assemble(
            replies=[
                reply(tool_calls=[tool_call()], finish="tool_calls"),
                report_from_transcript,
            ],
            model_requests=2,
        )
        actual_execute = loop.executor.execute

        def fixed_execute(tool_request):
            if not frozen_tool:
                frozen_tool.append(actual_execute(tool_request))
            return frozen_tool[0]

        monkeypatch.setattr(loop.executor, "execute", fixed_execute)
        loop.run(replace(request, question=stimulus["question"]))
        assert len(model.calls) == 2
        sequences.append([serialized_request(call) for call in model.calls])
    assert sequences[0][0] == sequences[1][0]  # actual first boundary call
    assert sequences[0] == sequences[1]  # fixed replies/tools, full request sequence


@pytest.fixture
def pack():
    items, labels = [], []
    for spec, _ in load_scenarios(SCENARIOS):
        for number in range(6):
            item_id = f"{spec.scenario_id}-{number}"
            request, diagnoses, verdict = make_input(
                spec.scenario_id, positive=number % 2 == 1, suffix=item_id
            )
            split = "debug" if number < 2 else "validation"
            item_hash = sha(safe_input(request))
            items.append(
                CalibrationItem(
                    item_id,
                    spec.scenario_id,
                    split,
                    f"{spec.scenario_id}-{split}-{number}",
                    item_hash,
                    request,
                    tuple(sorted(COVERAGE)),
                )
            )
            labels.append(BlindLabel(item_id, item_hash, diagnoses, verdict))
    return LabelPack(
        tuple(items),
        (
            AnnotationSet("author-a", "blind-model-a", tuple(labels)),
            AnnotationSet("author-b", "blind-model-b", tuple(labels)),
        ),
        (),
    )


def calibration_transports(pack, candidate_configs, *, errors=(), replacements=None):
    labels = final_labels(pack)
    by_input = {sha(safe_input(i.request)): labels[i.item_id] for i in pack.items}
    transports = {}
    for provider, config in candidate_configs.items():

        def handler(call, provider=provider, config=config):
            if provider in errors:
                return JudgeHTTPResponse(500, (b"secret-error",))
            body = json.loads(call["body"])
            payload = json.loads(body["messages"][1]["content"])
            label = by_input[sha(payload)]
            if replacements and (provider, label.item_id) in replacements:
                label = replacements[provider, label.item_id]
            return completion(config, label.diagnoses, label.verdict)

        transports[provider] = FakeTransport(handler)
    return transports


def test_pack_freeze_roundtrip_immutable_and_hash_bound(tmp_path, pack):
    path = tmp_path / "pack"
    pack_hash = freeze_label_pack(path, pack)
    assert pack_hash == label_pack_hash(pack)
    assert load_label_pack(path, expected_pack_hash=pack_hash) == pack
    assert all(os.stat(p).st_mode & 0o777 == 0o600 for p in path.iterdir())
    with pytest.raises(JudgeError, match="OUTPUT_EXISTS"):
        freeze_label_pack(path, pack)
    (path / "items.json").write_bytes(b"{}")
    with pytest.raises(JudgeError, match="HASH_MISMATCH"):
        load_label_pack(path, expected_pack_hash=pack_hash)


@pytest.mark.parametrize(
    "mutation",
    [
        "distribution",
        "overlap",
        "coverage",
        "input_hash",
        "same_author",
        "missing_labels",
        "negative_shortfall",
        "duplicate_input",
    ],
)
def test_bad_label_pack_fails_closed(pack, mutation):
    items = list(pack.items)
    annotations = list(pack.annotations)
    if mutation == "distribution":
        items[0] = replace(items[0], split="validation")
    elif mutation == "overlap":
        items[2] = replace(items[2], source_family=items[0].source_family)
    elif mutation == "coverage":
        items = [replace(i, coverage=()) for i in items]
    elif mutation == "input_hash":
        items[0] = replace(items[0], input_hash="0" * 64)
    elif mutation == "duplicate_input":
        items[1] = replace(
            items[1], request=items[0].request, input_hash=items[0].input_hash
        )
    elif mutation == "same_author":
        annotations[1] = replace(
            annotations[1], annotator_identity=annotations[0].annotator_identity
        )
    elif mutation == "missing_labels":
        annotations[0] = replace(annotations[0], labels=annotations[0].labels[:-1])
    elif mutation == "negative_shortfall":
        # Every item reconstructed as positive, valid labels yet no negatives.
        new_items, new_labels = [], []
        for item in items:
            request, diagnoses, verdict = make_input(
                item.scenario_id, positive=True, suffix=item.item_id
            )
            new_items.append(
                replace(item, request=request, input_hash=sha(safe_input(request)))
            )
            new_labels.append(
                BlindLabel(item.item_id, sha(safe_input(request)), diagnoses, verdict)
            )
        items = new_items
        annotations = [replace(a, labels=tuple(new_labels)) for a in annotations]
    with pytest.raises(JudgeError):
        label_pack_hash(
            replace(pack, items=tuple(items), annotations=tuple(annotations))
        )


def test_disagreement_requires_hash_bound_human_record(tmp_path, pack):
    labels = list(pack.annotations[1].labels)
    # Short reason change is still a recorded disagreement; no silent arbitration.
    index = next(i for i, label in enumerate(labels) if label.diagnoses)
    labels[index] = replace(
        labels[index],
        diagnoses=(
            replace(labels[index].diagnoses[0], reason="Independent second reading."),
        ),
    )
    annotations = (
        pack.annotations[0],
        replace(pack.annotations[1], labels=tuple(labels)),
    )
    changed = replace(pack, annotations=annotations)
    with pytest.raises(JudgeError, match="CALIBRATION_UNRESOLVED"):
        final_labels(changed)
    decision = Adjudication(
        labels[index].item_id,
        tuple(sha(a) for a in annotations),
        "user-reviewed-record-1",
        annotations[0].labels[index],
    )
    adjudicated = replace(changed, adjudications=(decision,))
    pack_hash = freeze_label_pack(tmp_path / "adjudicated", adjudicated)
    assert (
        load_label_pack(tmp_path / "adjudicated", expected_pack_hash=pack_hash)
        == adjudicated
    )
    with pytest.raises(JudgeError, match="CALIBRATION_UNRESOLVED"):
        final_labels(
            replace(
                adjudicated,
                adjudications=(replace(decision, annotation_hashes=("x", "y")),),
            )
        )


def test_all_three_same_samples_p7_selected_and_verified_judge(tmp_path, pack):
    candidate_configs = configs()
    transports = calibration_transports(pack, candidate_configs)
    selection = run_calibration(
        pack,
        configs=candidate_configs,
        transports=transports,
        destination=tmp_path / "run",
    )
    assert selection.status == "selected" and selection.selected_provider == "zai"
    assert selection.eligible == ("zai", "openai")
    for provider, result in selection.results.items():
        assert (
            result.items,
            result.comparisons,
            result.agreement,
            result.negatives,
            result.false_passes,
        ) == (24, 48, 48, 12, 0)
        assert result.passed and len(transports[provider].calls) == 60
    messages = [
        [json.loads(c["body"])["messages"][1] for c in transport.calls]
        for transport in transports.values()
    ]
    assert messages[0] == messages[1] == messages[2]
    calibration = load_verified_calibration(
        tmp_path / "run/calibration.json", pack=pack, config=candidate_configs["zai"]
    )
    observation, _ = invoke(calibration=calibration)
    assert observation.passed and observation.result.calibrated
    assert observation.result.calibration_hash == label_pack_hash(pack)
    forged = replace(calibration, pack_hash="forged")
    observation, _ = invoke(calibration=forged)
    assert not observation.passed and not observation.result.calibrated
    with pytest.raises(JudgeError, match="CALIBRATION_UNRESOLVED"):
        load_verified_calibration(
            tmp_path / "run/calibration.json",
            pack=pack,
            config=candidate_configs["deepseek"],
        )
    assert SECRET not in (tmp_path / "run/calibration.json").read_text()
    with pytest.raises(JudgeError, match="OUTPUT_EXISTS"):
        run_calibration(
            pack,
            configs=candidate_configs,
            transports=transports,
            destination=tmp_path / "run",
        )
    assert len(transports["zai"].calls) == 60


@pytest.mark.parametrize(
    "errors,status,eligible",
    [
        (("zai",), "pending_user_decision", ("openai",)),
        (("zai", "openai"), "no_baseline", ()),
        (("deepseek",), "selected", ("zai", "openai")),
    ],
)
def test_d16_never_fallback_to_sol_or_select_deepseek(
    tmp_path, pack, errors, status, eligible
):
    candidate_configs = configs()
    selection = run_calibration(
        pack,
        configs=candidate_configs,
        transports=calibration_transports(pack, candidate_configs, errors=errors),
        destination=tmp_path / "run",
    )
    assert selection.status == status and selection.eligible == eligible
    assert selection.selected_provider == ("zai" if status == "selected" else None)
    for provider in errors:
        result = selection.results[provider]
        assert not result.passed and result.agreement == 0
        assert result.false_passes == 0  # errors are not true verdicts
        assert "JUDGE_OBSERVATION_FAILED" in result.reasons
    if status != "selected":
        with pytest.raises(JudgeError, match="CALIBRATION_UNRESOLVED"):
            load_verified_calibration(
                tmp_path / "run/calibration.json",
                pack=pack,
                config=candidate_configs["openai"],
            )


def test_p7_46_boundary_and_sol_better_requires_decision(tmp_path, pack):
    # One positive fault item is missed twice: 46/48, no false passes.
    item = next(
        i
        for i in pack.items
        if i.split == "validation"
        and i.request.spec.kind == "fault"
        and final_labels(pack)[i.item_id].verdict
    )
    missed = BlindLabel(item.item_id, item.input_hash, (), False)
    configs_ = configs()
    replacements = {("zai", item.item_id): missed}
    selection = run_calibration(
        pack,
        configs=configs_,
        transports=calibration_transports(pack, configs_, replacements=replacements),
        destination=tmp_path / "run",
    )
    # Missing a gold key diagnosis blocks P7 even where final boolean count is46.
    assert selection.results["zai"].agreement == 46
    assert not selection.results["zai"].passed
    assert "KEY_DIAGNOSIS_MISSING" in selection.results["zai"].reasons
    assert selection.status == "pending_user_decision"


def test_calibration_readback_recomputes_results_and_observations(tmp_path, pack):
    configs_ = configs()
    run_calibration(
        pack,
        configs=configs_,
        transports=calibration_transports(pack, configs_),
        destination=tmp_path / "run",
    )
    path = tmp_path / "run/calibration.json"
    original = json.loads(path.read_bytes())
    forged = json.loads(path.read_bytes())
    forged["selection"]["results"]["zai"]["agreement"] = 47
    path.write_bytes(canonical_bytes(forged))
    with pytest.raises(JudgeError, match="DERIVED_RESULT_MISMATCH"):
        load_verified_calibration(path, pack=pack, config=configs_["zai"])
    forged = original
    forged["observations"]["zai"][0]["observation"]["result"]["verdict"] = not forged[
        "observations"
    ]["zai"][0]["observation"]["result"]["verdict"]
    path.write_bytes(canonical_bytes(forged))
    with pytest.raises(JudgeError):
        load_verified_calibration(path, pack=pack, config=configs_["zai"])


def test_46_agreement_eligible_but_sol_better_stops_at_selection(tmp_path, pack):
    item = next(
        i
        for i in pack.items
        if i.split == "validation"
        and i.request.spec.kind == "fault"
        and final_labels(pack)[i.item_id].verdict
    )
    gold = final_labels(pack)[item.item_id]
    disagreement = replace(
        gold,
        diagnoses=tuple(replace(d, supported=False) for d in gold.diagnoses),
        verdict=False,
    )
    candidate_configs = configs()
    selection = run_calibration(
        pack,
        configs=candidate_configs,
        transports=calibration_transports(
            pack, candidate_configs, replacements={("zai", item.item_id): disagreement}
        ),
        destination=tmp_path / "run",
    )
    assert selection.results["zai"].agreement == 46
    assert selection.results["zai"].passed
    assert selection.results["openai"].agreement == 48
    assert (
        selection.status == "pending_user_decision"
        and selection.selected_provider is None
    )
    assert selection.eligible == ("zai", "openai")


def test_missing_causal_diagnosis_cannot_be_correct_negative(tmp_path, pack):
    # A normal negative report with two factual causes: returning one retains
    # False, but loses a key diagnosis and must lose agreement credit.
    item = next(
        i
        for i in pack.items
        if i.split == "validation"
        and i.request.spec.kind == "normal"
        and not final_labels(pack)[i.item_id].verdict
    )
    gold = final_labels(pack)[item.item_id]
    payload = json.loads(item.request.report.raw)
    payload["summary"] = "A second service caused errors."
    extra = replace(
        gold.diagnoses[0],
        location="summary",
        quote=payload["summary"],
        cited_evidence_ids=(),
        supported=False,
    )
    request = replace(item.request, report=ReportCapture(json.dumps(payload), "stop"))
    changed_item = replace(item, request=request, input_hash=sha(safe_input(request)))
    changed_label = replace(
        gold, input_hash=changed_item.input_hash, diagnoses=(*gold.diagnoses, extra)
    )
    changed_pack = replace(
        pack,
        items=tuple(
            changed_item if i.item_id == item.item_id else i for i in pack.items
        ),
        annotations=tuple(
            replace(
                a,
                labels=tuple(
                    changed_label if label.item_id == item.item_id else label
                    for label in a.labels
                ),
            )
            for a in pack.annotations
        ),
    )
    partial = replace(changed_label, diagnoses=gold.diagnoses)
    candidate_configs = configs()
    selection = run_calibration(
        changed_pack,
        configs=candidate_configs,
        transports=calibration_transports(
            changed_pack,
            candidate_configs,
            replacements={("zai", item.item_id): partial},
        ),
        destination=tmp_path / "run",
    )
    assert selection.results["zai"].agreement == 46
    assert selection.results["zai"].false_passes == 0
    assert not selection.results["zai"].passed
    assert "KEY_DIAGNOSIS_MISSING" in selection.results["zai"].reasons


def test_input_hash_equals_json_normalized_product_canonical(pack):
    from opspilot.evaluation import judge_request
    from opspilot.tools.registry import canonical_hash

    for item in pack.items:
        request = item.request
        payload = judge_request(
            request.spec,
            request.answer,
            request.report,
            request.delivered,
            request.authorized,
            outcome=request.outcome,
        )
        normalized = json.loads(json.dumps(payload, allow_nan=False))
        assert item.input_hash == canonical_hash(normalized)


def test_annotation_bytes_only_depend_on_annotation_set(tmp_path, pack):
    from opspilot.evaluation.calibration_flow import load_label_pack

    first = tmp_path / "first"
    second = tmp_path / "second"
    freeze_label_pack(first, pack)
    swapped = replace(pack, annotations=tuple(reversed(pack.annotations)))
    pack_hash = freeze_label_pack(second, swapped)
    assert (first / "annotations-1.json").read_bytes() == canonical_bytes(
        pack.annotations[0]
    )
    assert (first / "annotations-1.json").read_bytes() == (
        second / "annotations-2.json"
    ).read_bytes()
    assert load_label_pack(second, expected_pack_hash=pack_hash) == swapped


@pytest.mark.parametrize("model", ["glm-5.3", "deepseek-flash", "GLM-5.3"])
def test_fixed_candidate_annotator_rejected_during_freeze(tmp_path, pack, model):
    bad = replace(
        pack,
        annotations=(
            pack.annotations[0],
            replace(pack.annotations[1], model_identity=model),
        ),
    )
    with pytest.raises(JudgeError, match="CALIBRATION_UNRESOLVED") as caught:
        freeze_label_pack(tmp_path / "bad", bad)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert not (tmp_path / "bad").exists()


def test_config_hash_pure_but_modified_profile_never_sent():
    config = configs()["zai"]
    base = judge_config_hash(config)
    for field in (
        "provider",
        "endpoint",
        "request_model",
        "response_model",
        "prompt",
        "adapter_version",
        "schema_version",
    ):
        changed = replace(
            config,
            **{
                field: "openai"
                if field == "provider"
                else getattr(config, field) + "-changed"
            },
        )
        assert judge_config_hash(changed) != base
        observation, transport = invoke(config=changed)
        assert not observation.passed and not transport.calls


@pytest.mark.parametrize("kind", ["symlink", "directory", "missing", "bad_utf8"])
def test_config_io_conversion_removes_exception_chain(tmp_path, kind):
    path = tmp_path / "config"
    if kind == "symlink":
        path.symlink_to(tmp_path / "absent")
    elif kind == "directory":
        path.mkdir()
    elif kind == "bad_utf8":
        path.write_bytes(b"\xff")
        path.chmod(0o600)
    with pytest.raises(JudgeError, match="CONFIG_UNREADABLE") as caught:
        load_judge_config(path, environ={})
    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.parametrize("quoted", ["'", '"'])
def test_quoted_config_is_literal_and_same_environ_allowed(tmp_path, quoted):
    path = tmp_path / "config"
    value = "fake-$HOME-${PATH}-`not-executed`"
    path.write_text(f"ZAI_API_KEY={quoted}{value}{quoted}\n")
    path.chmod(0o600)
    config = load_judge_config(path, environ={"ZAI_API_KEY": value})
    assert config.api_key == value


def test_verified_directory_and_explicit_file_share_semantic_proof(tmp_path, pack):
    configs_ = configs()
    destination = tmp_path / "cal"
    run_calibration(
        pack,
        configs=configs_,
        transports=calibration_transports(pack, configs_),
        destination=destination,
    )
    path = destination / "calibration.json"
    # JSON whitespace/key order are not an integrity or correctness signal.
    obj = json.loads(path.read_bytes())
    path.write_text(json.dumps(obj, indent=2, sort_keys=False))
    by_directory = load_verified_calibration(
        destination, pack=pack, config=configs_["zai"]
    )
    by_file = load_verified_calibration(path, pack=pack, config=configs_["zai"])
    assert by_directory == by_file
    assert invoke(calibration=by_directory)[0].passed
    alias = tmp_path / "link"
    alias.symlink_to(destination)
    with pytest.raises(JudgeError, match="EVIDENCE_UNAVAILABLE") as caught:
        load_verified_calibration(alias, pack=pack, config=configs_["zai"])
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_invalid_calibration_dto_uses_judge_error_without_chain(tmp_path):
    with pytest.raises(JudgeError, match="SCHEMA_INVALID") as caught:
        run_calibration(
            None, configs={}, transports={}, destination=tmp_path / "invalid"
        )
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert not (tmp_path / "invalid").exists()


def test_config_nul_in_comment_is_rejected(tmp_path):
    path = tmp_path / "config"
    path.write_bytes(b"# comment\x00\nZAI_API_KEY=fake-only\n")
    path.chmod(0o600)
    with pytest.raises(JudgeError, match="CONFIG_SYNTAX") as caught:
        load_judge_config(path, environ={})
    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.parametrize(
    "key",
    [
        "clientSecret",
        "ClientSecret",
        "clientApiKey",
        "ClientAPIKey",
        "client-api-key-value",
        "providerReasoningContentBlob",
        "ProviderEncryptedContentEnvelope",
    ],
)
def test_review_p1_normalized_slots_never_reach_transport(key):
    request, diagnoses, verdict = make_input()
    record = request.delivered["e1"]
    view = {**record.view, key: "SYNTHETIC-PRIVATE-SLOT"}
    request = replace(
        request,
        delivered={
            "e1": replace(record, view=view, view_sha256=canonical_view_hash(view))
        },
    )
    observation, transport = invoke(request, diagnoses, verdict)
    assert observation.result.status == "invalid"
    assert transport.calls == []


def test_review_p2_config_read_error_cannot_retain_secret_bytes(tmp_path):
    path = tmp_path / "bad-encoding-config"
    marker = b"SYNTHETIC-KEY-IN-ERROR"
    path.write_bytes(b"ZAI_API_KEY=" + marker + b"\n# \xff")
    path.chmod(0o600)
    with pytest.raises(JudgeError, match="CONFIG_UNREADABLE") as caught:
        load_judge_config(path, environ={})
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert marker.decode() not in repr(caught.value)


def test_review_p2_arbitrary_transport_code_cannot_leak_authentication():
    def fail(call):
        raise JudgeError(call["headers"]["Authorization"])

    observation, transport = invoke(response=fail)
    assert observation.error_code == "JUDGE_TRANSPORT_ERROR"
    assert SECRET not in repr(observation)
    assert observation.result.status == "error" and len(transport.calls) == 1


def test_review_p2_wrong_openai_model_cannot_certify_d16(tmp_path, pack):
    candidate_configs = configs()
    candidate_configs["openai"] = replace(
        candidate_configs["openai"],
        request_model="gpt-4o-mini",
        response_model="gpt-4o-mini",
    )
    transports = calibration_transports(pack, candidate_configs)
    with pytest.raises(JudgeError, match="JUDGE_IDENTITY_MISMATCH"):
        run_calibration(
            pack,
            configs=candidate_configs,
            transports=transports,
            destination=tmp_path / "wrong-model",
        )
    assert all(not t.calls for t in transports.values())


def test_review_p2_summary_cache_cannot_detach_from_error_artifacts(tmp_path, pack):
    candidate_configs = configs()
    failed, good = tmp_path / "failed", tmp_path / "good"
    run_calibration(
        pack,
        configs=candidate_configs,
        transports=calibration_transports(pack, candidate_configs, errors=("zai",)),
        destination=failed,
    )
    run_calibration(
        pack,
        configs=candidate_configs,
        transports=calibration_transports(pack, candidate_configs),
        destination=good,
    )
    originals = {p.name: p.read_bytes() for p in failed.glob("zai-*.json")}
    summary = json.loads((failed / "calibration.json").read_bytes())
    other = json.loads((good / "calibration.json").read_bytes())
    summary["observations"]["zai"] = other["observations"]["zai"]
    summary["selection"] = other["selection"]
    (failed / "calibration.json").write_bytes(canonical_bytes(summary))
    assert all((failed / name).read_bytes() == raw for name, raw in originals.items())
    with pytest.raises(JudgeError, match="DERIVED_RESULT_MISMATCH") as caught:
        load_verified_calibration(failed, pack=pack, config=candidate_configs["zai"])
    assert caught.value.__cause__ is None and caught.value.__context__ is None


@pytest.mark.parametrize(
    "model,valid",
    [
        ("gpt-6.1-sol", True),
        ("gpt-6.1-sol-2026-10-14", True),
        ("gpt-6.1-sol-2026-99-99", False),
        ("gpt-6.1-sol-preview", False),
        ("gpt-4o-mini", False),
        ("gpt-6.1-sol-2026-10-14-extra", False),
    ],
)
def test_openai_response_identity_exact_or_calendar_snapshot(model, valid):
    request, diagnoses, verdict = make_input()
    config = configs()["openai"]
    observation, transport = invoke(
        request,
        diagnoses,
        verdict,
        config=config,
        response=completion(config, diagnoses, verdict, model=model),
    )
    assert observation.valid is valid
    assert len(transport.calls) == 1
    if valid:
        assert observation.response_model == model
    else:
        assert observation.result.status == "invalid"
        assert observation.error_code == "JUDGE_RESPONSE_IDENTITY_INVALID"


def test_verified_calibration_rechecks_live_per_call_artifacts(tmp_path, pack):
    c = configs()
    directory = tmp_path / "run"
    run_calibration(
        pack,
        configs=c,
        transports=calibration_transports(pack, c),
        destination=directory,
    )
    verified = load_verified_calibration(directory, pack=pack, config=c["zai"])
    assert invoke(calibration=verified)[0].passed
    original = directory / "zai-001.json"
    value = json.loads(original.read_bytes())
    value["input_hash"] = "0" * 64
    original.write_bytes(canonical_bytes(value))
    assert not invoke(calibration=verified)[0].result.calibrated
    with pytest.raises(JudgeError, match="IDENTITY_MISMATCH"):
        load_verified_calibration(directory, pack=pack, config=c["zai"])


@pytest.mark.parametrize(
    "key",
    [
        "CLIENTSECRET",
        "CLIENTAPIKEY",
        "CLIENTTOKEN",
        "Reasoning.Content",
        "provider_reasoning_content_suffix",
        "provider-reasoning-content-suffix",
    ],
)
def test_slot_normalization_case_and_separator_variants(key):
    request, diagnoses, verdict = make_input()
    record = request.delivered["e1"]
    view = {**record.view, key: "PRIVATE-SYNTHETIC-VALUE"}
    request = replace(
        request,
        delivered={
            "e1": replace(record, view=view, view_sha256=canonical_view_hash(view))
        },
    )
    observation, transport = invoke(request, diagnoses, verdict)
    assert observation.result.status == "invalid" and not transport.calls


@pytest.mark.parametrize("flaw", ["verdict", "quote", "location"])
def test_label_semantic_errors_use_calibration_domain_code(tmp_path, pack, flaw):
    original = pack.annotations[0].labels
    index = next(i for i, label in enumerate(original) if label.diagnoses)
    label = original[index]
    if flaw == "verdict":
        changed = replace(label, verdict=not label.verdict)
    else:
        diagnoses = list(label.diagnoses)
        diagnoses[0] = replace(diagnoses[0], **{flaw: "invalid-label-value"})
        changed = replace(label, diagnoses=tuple(diagnoses))
    labels = tuple(changed if i == index else v for i, v in enumerate(original))
    bad = replace(
        pack, annotations=tuple(replace(a, labels=labels) for a in pack.annotations)
    )
    with pytest.raises(JudgeError, match="CALIBRATION_LABEL_INVALID") as caught:
        freeze_label_pack(tmp_path / "bad-labels", bad)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    assert not (tmp_path / "bad-labels").exists()


@pytest.mark.parametrize("field", ["tool_calls", "function_call"])
@pytest.mark.parametrize("shape", ["empty", "null", "nonempty"])
def test_pr232_tool_fields_require_absent_or_null(field, shape):
    request, diagnoses, verdict = make_input()
    config = configs()["zai"]
    response = json.loads(completion(config, diagnoses, verdict))
    if shape == "empty":
        value = []
    elif shape == "null":
        value = None
    elif field == "function_call":
        value = {"name": "forbidden", "arguments": "{}"}
    else:
        value = [{"id": "forbidden", "type": "function"}]
    response["choices"][0]["message"][field] = value
    observation, transport = invoke(
        request, diagnoses, verdict, response=canonical_bytes(response)
    )
    assert len(transport.calls) == 1
    if shape == "null":
        assert observation.valid and observation.result.status == "ok"
        assert observation.recomputed_verdict is True
    else:
        assert observation.result.status == "invalid"
        assert not observation.valid and not observation.passed
        assert observation.error_code == "JUDGE_OUTPUT_INVALID"


@pytest.mark.parametrize(
    "stage", ["output_directory", "label_directory", "label_file", "observation_file"]
)
def test_pr232_directory_fsync_failure_stops_further_transports(
    tmp_path, pack, monkeypatch, stage
):
    destination = tmp_path / "calibration"
    label_directory = destination / "label-pack"
    c = configs()
    transports = calibration_transports(pack, c)
    original_fsync = os.fsync
    calls_at_failure = []

    def fail_directory_fsync(fd):
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            at_output = info.st_ino == tmp_path.stat().st_ino
            at_run = destination.exists() and info.st_ino == destination.stat().st_ino
            at_labels = (
                label_directory.exists()
                and info.st_ino == label_directory.stat().st_ino
            )
            should_fail = (
                stage == "output_directory"
                and at_output
                or stage == "label_directory"
                and at_run
                and label_directory.exists()
                or stage == "label_file"
                and at_labels
                and (label_directory / "items.json").exists()
                or stage == "observation_file"
                and at_run
                and (destination / "zai-001.json").exists()
            )
            if should_fail:
                calls_at_failure.append(sum(len(t.calls) for t in transports.values()))
                raise OSError("SYNTHETIC_DIRECTORY_FSYNC_FAILURE")
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)
    with pytest.raises(JudgeError, match="ARTIFACT_UNWRITABLE") as caught:
        run_calibration(pack, configs=c, transports=transports, destination=destination)
    expected = 1 if stage == "observation_file" else 0
    assert calls_at_failure == [expected]
    assert sum(len(t.calls) for t in transports.values()) == expected
    assert all(not transports[p].calls for p in ("openai", "deepseek"))
    assert not (destination / "calibration.json").exists()
    assert caught.value.__cause__ is None and caught.value.__context__ is None
