"""PR-B3 acceptance tests authored only from the public interface and contract.

Authority: docs/tasks/2026-10-14-eval-pr-b3-interface.md (cited B3:<line>),
docs/tasks/2026-10-14-eval-pr-b-contract.md section 4 (C:<line>), contract
r8 P6/P7/P9/D16, PRODUCT-CONSTRAINTS.md:48 and evals/judge.env.example. No
B3 implementation is read. Fully offline: fake transports only, and a socket
guard fails the module if anything opens a network connection. Unclear points
and the readings taken are in EVAL_B3_AMBIGUITIES.md.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import socket
from dataclasses import fields, replace
from pathlib import Path

import pytest
from eval_b3_support import (
    CANARIES,
    CANARY_OF,
    COVERAGE,
    CREDENTIAL_NAMES,
    ENDPOINTS,
    MAX_REQUEST,
    MAX_RESPONSE,
    MODELS,
    OBSERVATION,
    PLAIN,
    PRIVATE_KEYS,
    SCENARIO_ROOT,
    SOL_MODEL,
    USAGE,
    ZAI_CANARY,
    FakeTransport,
    NetworkGuard,
    Probe,
    _diag,
    annotation,
    author_item,
    build_world,
    canonical_bytes,
    completion,
    content,
    env_pairs,
    env_text,
    error_code,
    everywhere,
    expected_user_payload,
    false_fail,
    false_pass,
    http,
    http_error,
    judge_input,
    leak_surface,
    load,
    make_pack,
    missed_diagnosis,
    not_json,
    ok_reply,
    oversize,
    payload,
    sha_file,
    strings_in,
    timeout,
    transports,
    usage_value,
    user_payload,
    write_env,
)
from eval_contract_support import (
    EID,
    OPERATION,
    attempt,
    capture,
    case,
    claim,
    diagnosis,
    record,
    report_payload,
    trace,
)

from opspilot.evaluation import (
    JudgeResult,
    evaluate,
    load_scenario,
    run_gate,
)
from opspilot.evaluation.calibration_flow import (
    Adjudication,
    LabelPack,
    freeze_label_pack,
    load_label_pack,
    load_verified_calibration,
    run_calibration,
)
from opspilot.evaluation.judge import (
    JudgeError,
    JudgeHTTPResponse,
    judge_config_hash,
    judge_once,
)
from opspilot.tools.registry import canonical_hash

# ----------------------------------------------------------------- fixtures


@pytest.fixture(scope="module", autouse=True)
def network_guard():
    """Item 6: no network connection for the whole module, fixtures included."""
    guard = NetworkGuard()
    with pytest.MonkeyPatch.context() as mp:
        guard.install(mp)
        yield guard
    assert guard.attempts == [], f"network attempted: {guard.attempts}"


@pytest.fixture(autouse=True)
def no_network(network_guard):
    before = len(network_guard.attempts)
    yield
    assert network_guard.attempts[before:] == [], "test opened a network connection"


@pytest.fixture(scope="module")
def configs(network_guard, tmp_path_factory):
    directory = tmp_path_factory.mktemp("judge-env")
    path = write_env(directory, env_text())
    return {provider: load(path, provider) for provider in MODELS}


@pytest.fixture(scope="module")
def world(network_guard):
    return build_world()


@pytest.fixture(scope="module")
def pack(world):
    return make_pack(world)


@pytest.fixture(scope="module")
def frozen(pack, tmp_path_factory):
    directory = tmp_path_factory.mktemp("label-pack") / "pack"
    return directory, freeze_label_pack(directory, pack)


@pytest.fixture(scope="module")
def selected(world, pack, configs, tmp_path_factory):
    destination = tmp_path_factory.mktemp("calibration") / "selected"
    record = run_calibration(
        pack, configs=configs, transports=transports(world), destination=destination
    )
    return destination, record


@pytest.fixture(scope="module")
def verified(selected, pack, configs):
    destination, _ = selected
    return load_verified_calibration(destination, pack=pack, config=configs["zai"])


def expect_code(exc: BaseException, codes) -> str:
    code = error_code(exc)
    assert code is not None, f"no fixed error code on {type(exc).__name__}"
    if codes is not None:
        assert code in codes, code
    return code


def assert_no_leak(exc: BaseException, *secrets: str) -> None:
    surface = leak_surface(exc)
    for secret in secrets or CANARIES:
        assert secret not in surface


def config_error(path, codes, *, provider="zai", environ=None, secrets=CANARIES):
    with pytest.raises(JudgeError) as caught:
        load(path, provider, environ)
    exc = caught.value
    expect_code(exc, codes)
    assert_no_leak(exc, *secrets)
    # B1 interface I:80 (all evaluation modules): converted errors carry no chain.
    assert exc.__cause__ is None and exc.__context__ is None
    return exc


def call_once(request, config, *, respond, calibration=None):
    transport = FakeTransport(respond)
    observation = judge_once(
        request, config=config, calibration=calibration, transport=transport
    )
    return observation, transport


OBSERVATION_CODES = frozenset(  # r3 item 3: the only JudgeObservation.error_code values
    {
        "CONFIG_INVALID",
        "JUDGE_IDENTITY_MISMATCH",
        "SCHEMA_INVALID",
        "JUDGE_PRIVATE_FIELD",
        "JUDGE_OUTPUT_INVALID",
        "JUDGE_VERDICT_MISMATCH",
        "JUDGE_REQUEST_OVERSIZE",
        "JUDGE_RESPONSE_OVERSIZE",
        "JUDGE_TIMEOUT",
        "JUDGE_HTTP_ERROR",
        "JUDGE_TRANSPORT_ERROR",
        "JUDGE_RESPONSE_IDENTITY_INVALID",
    }
)


def assert_failed(observation, transport, status):
    statuses = {status} if isinstance(status, str) else set(status)
    assert observation.result.status in statuses
    assert observation.passed is False
    assert observation.valid is False
    assert observation.error_code in OBSERVATION_CODES
    # P7-D27: retry only after a transport failure, up to four attempts.
    retries = observation.error_code == "JUDGE_TRANSPORT_ERROR"
    assert len(transport.calls) <= (4 if retries else 1)


# ============================================================ 1. config file


def test_zai_config_loads_fixed_identity_from_private_file(tmp_path):
    config = load(write_env(tmp_path, env_text()))
    assert config.provider == "zai"
    assert config.endpoint == ENDPOINTS["zai"]
    assert (config.request_model, config.response_model) == (
        "glm-5.3-flashx",
        "glm-5.3-flashx",
    )
    assert config.api_key == ZAI_CANARY
    assert config.timeout_seconds == 900
    assert config.max_request_bytes == MAX_REQUEST
    assert config.max_response_bytes == MAX_RESPONSE
    assert config.max_output_tokens == 65536
    for shown in (repr(config), str(config)):
        assert all(secret not in shown for secret in CANARIES)


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_control_provider_configs_have_fixed_identities(tmp_path, provider):
    config = load(write_env(tmp_path, env_text()), provider)
    assert config.provider == provider
    assert config.endpoint == ENDPOINTS[provider]
    assert config.request_model == MODELS[provider]
    assert config.response_model == MODELS[provider]
    assert config.api_key == CANARY_OF[provider]
    assert all(secret not in repr(config) for secret in CANARIES)


def test_relative_path_rejected(tmp_path, monkeypatch):
    write_env(tmp_path, env_text())
    monkeypatch.chdir(tmp_path)
    config_error(Path(".env.judge"), {"CONFIG_ABSOLUTE_PATH_REQUIRED"})


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o400, 0o700, 0o666])
def test_only_exact_0600_accepted(tmp_path, mode):
    path = write_env(tmp_path, env_text(), mode=mode)
    try:
        config_error(path, {"CONFIG_PRIVATE_REGULAR_FILE_REQUIRED"})
    finally:
        os.chmod(path, 0o600)


def test_0644_rejected_with_private_file_code(tmp_path):
    # judge.env.example: plain cp under umask 022 yields 0644, which must fail.
    path = write_env(tmp_path, env_text(), mode=0o644)
    config_error(path, {"CONFIG_PRIVATE_REGULAR_FILE_REQUIRED"})


def test_symlinked_config_rejected(tmp_path):
    real = write_env(tmp_path, env_text(), name="real.env")
    link = tmp_path.resolve() / ".env.judge"
    link.symlink_to(real)
    config_error(link, {"CONFIG_UNREADABLE"})


def test_directory_and_missing_file_rejected(tmp_path):
    directory = tmp_path.resolve() / "dir.env"
    directory.mkdir(mode=0o700)
    config_error(directory, {"CONFIG_UNREADABLE"})
    config_error(tmp_path.resolve() / "absent.env", {"CONFIG_UNREADABLE"})


def test_config_over_16384_bytes_rejected(tmp_path):
    padding = ["# " + "x" * 120] * 140  # comments only; > 16384 bytes in total
    path = write_env(tmp_path, env_text(extra=padding))
    assert path.stat().st_size > 16384
    config_error(path, {"CONFIG_PRIVATE_REGULAR_FILE_REQUIRED"})


def test_unknown_key_rejected_without_echoing_values(tmp_path):
    path = write_env(
        tmp_path, env_text(extra=["OPSPILOT_JUDGE_EXTRA=canary-unknown-value"])
    )
    config_error(
        path,
        {"CONFIG_SYNTAX"},
        secrets=(*CANARIES, "canary-unknown-value", "OPSPILOT_JUDGE_EXTRA"),
    )


def test_duplicate_key_rejected(tmp_path):
    extra = [f"{CREDENTIAL_NAMES['zai']}=canary-second-zai-value"]
    path = write_env(tmp_path, env_text(extra=extra))
    config_error(
        path,
        {"CONFIG_SYNTAX"},
        secrets=(*CANARIES, "canary-second-zai-value"),
    )


@pytest.mark.parametrize(
    ("provider", "dropped"),
    [
        ("zai", CREDENTIAL_NAMES["zai"]),
        ("openai", CREDENTIAL_NAMES["openai"]),
        ("openai", "OPENAI_JUDGE_MODEL"),
        ("openai", "OPENAI_JUDGE_RESPONSE_MODEL"),
        ("deepseek", CREDENTIAL_NAMES["deepseek"]),
    ],
)
def test_missing_required_key_rejected(tmp_path, provider, dropped):
    path = write_env(tmp_path, env_text(drop=(dropped,)))
    config_error(path, {"CONFIG_INVALID"}, provider=provider)


def test_empty_credential_rejected(tmp_path):
    path = write_env(tmp_path, env_text(override={CREDENTIAL_NAMES["zai"]: ""}))
    config_error(path, {"CONFIG_INVALID", "CONFIG_SYNTAX"})


@pytest.mark.parametrize(
    ("name", "value", "code"),
    [  # r2 item 6: renamed fixed model vs changed fixed BASE_URL
        ("OPSPILOT_JUDGE_MODEL", "glm-4.6", "JUDGE_IDENTITY_MISMATCH"),
        ("ZAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4", "CONFIG_INVALID"),
        ("ZAI_BASE_URL", "http://api.z.ai/api/paas/v4", "CONFIG_INVALID"),
    ],
)
def test_zai_identity_cannot_be_redirected(tmp_path, name, value, code):
    path = write_env(tmp_path, env_text(override={name: value}))
    config_error(path, {code})


def test_comments_blank_lines_and_paired_quotes_are_literal(tmp_path):
    literal = "canary-$HOME-${PATH}-zai"
    pairs = [
        (n, f'"{literal}"' if n == CREDENTIAL_NAMES["zai"] else v)
        for n, v in env_pairs()
    ]
    text = "\n# comment line\n\n" + env_text(pairs)
    config = load(write_env(tmp_path, text))
    assert config.api_key == literal  # quotes stripped, no interpolation


def test_process_environment_is_never_read(tmp_path, monkeypatch):
    monkeypatch.setenv(CREDENTIAL_NAMES["zai"], "canary-process-env-zai")
    config = load(write_env(tmp_path, env_text()), environ={})
    assert config.api_key == ZAI_CANARY
    missing = write_env(
        tmp_path, env_text(drop=(CREDENTIAL_NAMES["zai"],)), name="b.env"
    )
    config_error(
        missing,
        {"CONFIG_INVALID"},
        secrets=("canary-process-env-zai",),
    )


def test_environ_cannot_fill_missing_credential(tmp_path):
    missing = write_env(tmp_path, env_text(drop=(CREDENTIAL_NAMES["zai"],)))
    environ = {CREDENTIAL_NAMES["zai"]: "canary-environ-zai"}
    with pytest.raises(JudgeError) as caught:
        load(missing, "zai", environ)
    expect_code(caught.value, {"CONFIG_INVALID"})
    assert_no_leak(caught.value, "canary-environ-zai")


def test_environ_conflict_rejected_without_values(tmp_path):
    path = write_env(tmp_path, env_text())
    environ = {CREDENTIAL_NAMES["zai"]: "canary-conflicting-zai"}
    config_error(
        path,
        {"CONFIG_ENV_CONFLICT"},
        environ=environ,
        secrets=(*CANARIES, "canary-conflicting-zai"),
    )


def test_unrelated_environ_keys_ignored(tmp_path):
    environ = {"PATH": "/usr/bin", "HOME": "/home/x", "LANGSMITH_API_KEY": "canary-ls"}
    config = load(write_env(tmp_path, env_text()), environ=environ)
    assert config.api_key == ZAI_CANARY


# ======================================================= 2. judge_config_hash


def _changed(value, name):
    if name == "provider":
        return next(p for p in MODELS if p != value)
    if isinstance(value, bool):
        return not value
    if isinstance(value, int | float):
        return value - 1
    if isinstance(value, str):
        return value + "-changed"
    return None


def test_config_hash_covers_every_non_secret_field(configs):
    config = configs["zai"]
    base = judge_config_hash(config)
    assert len(base) == 64 and base == base.lower()
    names = {f.name for f in fields(config)}
    listed = {  # B3:16
        "provider",
        "endpoint",
        "request_model",
        "response_model",
        "api_key",
        "prompt",
        "adapter_version",
        "schema_version",
        "timeout_seconds",
        "max_request_bytes",
        "max_response_bytes",
        "max_output_tokens",
    }
    assert listed <= names
    for name in names - {"api_key"}:
        twin = copy.copy(config)
        object.__setattr__(twin, name, _changed(getattr(config, name), name))
        assert judge_config_hash(twin) != base, name


def test_config_hash_ignores_credential(configs, tmp_path):
    config = configs["zai"]
    twin = copy.copy(config)
    object.__setattr__(twin, "api_key", "canary-rotated-zai")
    assert judge_config_hash(twin) == judge_config_hash(config)
    rotated = env_text(override={CREDENTIAL_NAMES["zai"]: "canary-rotated-zai"})
    assert judge_config_hash(load(write_env(tmp_path, rotated))) == judge_config_hash(
        config
    )
    assert ZAI_CANARY not in judge_config_hash(config)


def test_providers_hash_differently(configs):
    hashes = {judge_config_hash(c) for c in configs.values()}
    assert len(hashes) == 3


# ============================================== 3. judge_once: request side


def test_request_body_is_the_p6_whitelist_only(configs):
    request = judge_input()
    observation, transport = call_once(
        request, configs["zai"], respond=ok_reply((diagnosis(),), True)
    )
    assert observation.result.status == "ok"
    (call,) = transport.calls
    assert call.endpoint == ENDPOINTS["zai"]
    assert call.timeout_seconds == 900
    assert call.max_response_bytes == MAX_RESPONSE
    auth = {k.lower(): v for k, v in call.headers.items()}["authorization"]
    assert auth == f"Bearer {ZAI_CANARY}"
    body = payload(call.body)
    assert body["model"] == "glm-5.3-flashx"
    assert body["stream"] is True
    assert "stream_options" not in body
    assert body["max_tokens"] == 65536
    assert "max_completion_tokens" not in body
    assert "tools" not in body and "tool_choice" not in body
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert body["messages"][0]["content"] == configs["zai"].prompt  # r2 item 7
    sent = user_payload(call.body)
    assert sent == expected_user_payload(request)
    assert len(sent) == 9
    for forbidden in (
        b"private-cart-flag",
        b"private-variant",
        b"contract-fault",
        *(s.encode() for s in CANARIES),
    ):
        assert forbidden not in call.body


@pytest.mark.parametrize(
    ("provider", "limit_key"),
    [("openai", "max_completion_tokens"), ("deepseek", "max_tokens")],
)
def test_control_provider_request_shapes(configs, provider, limit_key):
    request = judge_input()
    observation, transport = call_once(
        request,
        configs[provider],
        respond=ok_reply((diagnosis(),), True, model=MODELS[provider]),
    )
    assert observation.result.status == "ok"
    (call,) = transport.calls
    assert call.endpoint == ENDPOINTS[provider]
    headers = {k.lower(): v for k, v in call.headers.items()}
    assert headers["authorization"] == f"Bearer {CANARY_OF[provider]}"
    body = payload(call.body)
    assert body["model"] == MODELS[provider]
    assert body[limit_key] == 65536
    other = {"max_tokens", "max_completion_tokens"} - {limit_key}
    assert not other & set(body)
    assert user_payload(call.body) == expected_user_payload(request)
    for secret in CANARIES:
        assert secret.encode() not in call.body


def _with_unreferenced(marker: str):
    data = case()
    extra = record(
        evidence_id="ev-unreferenced",
        rows=[{"service": "cart", "operation": OPERATION, "note": marker}],
    )
    data["delivered"] = {**data["delivered"], "ev-unreferenced": extra}
    data["outcome"] = replace(data["outcome"], evidence_ids=(EID, "ev-unreferenced"))
    return judge_input(data)


def test_unreferenced_evidence_neither_sent_nor_changes_body(configs):
    """C:158 item 4: changing uncited evidence does not change the judge body."""
    bodies = []
    for marker in ("uncited-marker-one", "uncited-marker-two"):
        _, transport = call_once(
            _with_unreferenced(marker),
            configs["zai"],
            respond=ok_reply((diagnosis(),), True),
        )
        (call,) = transport.calls
        assert marker.encode() not in call.body
        bodies.append(call.body)
    assert bodies[0] == bodies[1]


def test_other_run_evidence_never_sent(configs):
    data = case()
    other = record(evidence_id="ev-other-run", rows=[{"note": "other-run-marker"}])
    data["delivered"] = {**data["delivered"], "ev-other-run": other}
    data["report"] = capture(
        report_payload(claims=[claim(evidence_ids=[EID, "ev-other-run"])])
    )
    _, transport = call_once(
        judge_input(data), configs["zai"], respond=ok_reply((), False)
    )
    for call in transport.calls:
        assert b"other-run-marker" not in call.body


def test_answer_flag_and_scenario_path_do_not_change_body(configs, tmp_path):
    """C:158 item 1 on the judge side: flag/variant and scenario path never reach it."""
    original = SCENARIO_ROOT / "eval-cart-failure"
    moved = tmp_path / "elsewhere" / "renamed-dir" / "eval-cart-failure"
    shutil.copytree(original, moved)
    answer_path = moved / "answer.json"
    doc = json.loads(answer_path.read_text())
    doc["injected"]["flag"] = "flag-marker-changed"
    doc["injected"]["variant"] = "variant-marker-changed"
    answer_path.write_text(json.dumps(doc))
    bodies = []
    for directory in (original, moved):
        spec, answer = load_scenario(directory)
        data = case()
        data["spec"], data["answer"] = spec, answer
        data["outcome"] = replace(data["outcome"], scenario_id=spec.scenario_id)
        _, transport = call_once(
            judge_input(data), configs["zai"], respond=ok_reply((), False)
        )
        (call,) = transport.calls
        bodies.append(call.body)
        for marker in (
            b"cartFailure",
            b"flag-marker-changed",
            b"variant-marker-changed",
        ):
            assert marker not in call.body
        assert b"eval-cart-failure" not in call.body
        assert b"renamed-dir" not in call.body
    assert bodies[0] == bodies[1]


def _view_with(key: str, nested: bool):
    data = case()
    row = {"service": "cart", "operation": OPERATION}
    view = {"tool": "traces_search", "status": "ok", "trust": "untrusted-evidence"}
    if nested:
        row[key] = "private-protocol-marker"
        view["content"] = [row]
    else:
        view["content"] = [row]
        view[key] = "private-protocol-marker"
    data["delivered"] = {
        EID: replace(
            data["delivered"][EID], view=view, view_sha256=canonical_hash(view)
        )
    }
    return judge_input(data)


def _report_with(key: str, in_claim: bool):
    data = case()
    doc = report_payload()
    if in_claim:
        doc["claims"][0][key] = "private-protocol-marker"
    else:
        doc[key] = "private-protocol-marker"
    data["report"] = capture(doc)
    return judge_input(data)


PLACEMENTS = {
    "view_top": lambda key: _view_with(key, nested=False),
    "view_nested": lambda key: _view_with(key, nested=True),
    "report_top": lambda key: _report_with(key, in_claim=False),
    "report_claim": lambda key: _report_with(key, in_claim=True),
}


@pytest.mark.parametrize("placement", sorted(PLACEMENTS))
@pytest.mark.parametrize("key", PRIVATE_KEYS)
def test_private_protocol_fields_never_reach_transport(configs, key, placement):
    """PRODUCT-CONSTRAINTS:48, C:158 item 3, B3:26: invalid with zero transport calls."""
    observation, transport = call_once(
        PLACEMENTS[placement](key),
        configs["zai"],
        respond=ok_reply((diagnosis(),), True),
    )
    assert transport.calls == []
    assert observation.result.status == "invalid"
    assert observation.passed is False and observation.valid is False
    assert "private-protocol-marker" not in repr(observation)


CREDENTIAL_SLOTS = (  # B3:26 credential slot keys, variants and prefixed/suffixed names
    "api_key",
    "API-Key",
    "api.key",
    "ZAI_API_KEY",
    "authorization",
    "password",
    "passwd",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "client_secret",
    "credential",
    "credentials",
    "cookie",
    "bearer",
)


@pytest.mark.parametrize("placement", sorted(PLACEMENTS))
@pytest.mark.parametrize("key", CREDENTIAL_SLOTS)
def test_credential_slots_never_reach_transport(configs, key, placement):
    observation, transport = call_once(
        PLACEMENTS[placement](key),
        configs["zai"],
        respond=ok_reply((diagnosis(),), True),
    )
    assert transport.calls == []
    assert observation.result.status == "invalid"
    assert observation.passed is False


def test_outcome_is_required(configs):
    transport = FakeTransport(ok_reply((diagnosis(),), True))
    try:
        request = judge_input(outcome=None)
    except Exception as exc:  # strict DTO rejects at construction (B3:26)
        assert isinstance(exc, TypeError | ValueError) or error_code(exc)
        return
    try:
        observation = judge_once(
            request, config=configs["zai"], calibration=None, transport=transport
        )
    except Exception as exc:
        assert isinstance(exc, TypeError | ValueError) or error_code(exc)
    else:
        assert observation.passed is False
    assert transport.calls == []


def test_oversized_request_never_sent(configs):
    data = case()
    doc = report_payload()
    doc["gaps"] = ["g" * (MAX_REQUEST + 1)]
    data["report"] = capture(doc)
    observation, transport = call_once(
        judge_input(data), configs["zai"], respond=ok_reply((diagnosis(),), True)
    )
    assert transport.calls == []
    assert observation.result.status == "oversize"
    assert observation.passed is False


# ============================================= 3. judge_once: response side


def test_valid_uncalibrated_observation_cannot_pass(configs):
    observation, transport = call_once(
        judge_input(), configs["zai"], respond=ok_reply((diagnosis(),), True)
    )
    assert len(transport.calls) == 1
    assert observation.valid is True
    assert observation.recomputed_verdict is True
    assert observation.result.status == "ok"
    assert observation.result.calibrated is False
    assert observation.result.verdict is True
    assert observation.result.config_hash == judge_config_hash(configs["zai"])
    assert observation.passed is False
    assert observation.response_model == "glm-5.3-flashx"
    assert observation.elapsed_seconds >= 0
    assert observation.provider == "zai"
    assert observation.phase == "evaluation"  # r2 item 7
    assert observation.balance_delta is None  # B3:30: unknown, never a fake zero


def test_valid_calibrated_observation_passes(configs, verified):
    observation, transport = call_once(
        judge_input(),
        configs["zai"],
        respond=ok_reply((diagnosis(),), True),
        calibration=verified,
    )
    assert len(transport.calls) == 1
    assert observation.valid is True and observation.recomputed_verdict is True
    assert observation.result.calibrated is True
    assert observation.passed is True
    assert observation.error_code is None


def test_judge_true_cannot_pass_a_failed_gate(configs, verified):
    data = case()
    data["outcome"] = replace(data["outcome"], final_state="failed")
    request = judge_input(data)
    assert not run_gate(
        request.spec,
        request.answer,
        request.outcome,
        request.report,
        request.delivered,
        request.authorized,
    ).passed
    transport = FakeTransport(ok_reply((diagnosis(),), True))
    observation = judge_once(
        request, config=configs["zai"], calibration=verified, transport=transport
    )
    assert observation.passed is False
    assert len(transport.calls) <= 1


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_calibration_bound_to_other_config_cannot_pass(configs, verified, provider):
    transport = FakeTransport(ok_reply((diagnosis(),), True, model=MODELS[provider]))
    try:
        observation = judge_once(
            judge_input(),
            config=configs[provider],
            calibration=verified,
            transport=transport,
        )
    except Exception as exc:
        expect_code(exc, None)
    else:
        assert observation.passed is False


def test_calibration_bound_to_changed_prompt_cannot_pass(configs, verified):
    changed = copy.copy(configs["zai"])
    object.__setattr__(changed, "prompt", configs["zai"].prompt + " Be lenient.")
    transport = FakeTransport(ok_reply((diagnosis(),), True))
    try:
        observation = judge_once(
            judge_input(), config=changed, calibration=verified, transport=transport
        )
    except Exception as exc:
        expect_code(exc, None)
    else:
        assert observation.passed is False


def test_valid_negative_is_valid_but_not_passed(configs, verified):
    data = case(fault=False)
    text = json.loads(data["report"].raw)["claims"][0]["text"]
    bad = diagnosis(location="claims[0]", quote=text)
    observation, _ = call_once(
        judge_input(data),
        configs["zai"],
        respond=ok_reply((bad,), False),
        calibration=verified,
    )
    assert observation.valid is True
    assert observation.result.status == "ok"
    assert observation.recomputed_verdict is False
    assert observation.passed is False


RECOMPUTE_CASES = {
    # (diagnoses, claimed verdict, recomputed verdict): the code-side AND
    # disagrees with the model.
    "wrong_service_claimed_true": ((diagnosis(service="payment"),), True, False),
    "wrong_operation_claimed_true": ((diagnosis(operation="Charge"),), True, False),
    "unsupported_claimed_true": ((diagnosis(supported=False),), True, False),
    "no_diagnosis_on_fault_claimed_true": ((), True, False),
    "correct_diagnosis_claimed_false": ((diagnosis(),), False, True),
    "extra_wrong_cause_claimed_true": (
        (diagnosis(), diagnosis(service="currency", operation="Convert")),
        True,
        False,
    ),
}


@pytest.mark.parametrize("name", sorted(RECOMPUTE_CASES))
def test_verdict_is_recomputed_by_code(configs, verified, name):
    """P6 / B3:30: the code recomputes the verdict from the diagnoses."""
    diagnoses, verdict, recomputed = RECOMPUTE_CASES[name]
    observation, transport = call_once(
        judge_input(),
        configs["zai"],
        respond=ok_reply(diagnoses, verdict),
        calibration=verified,
    )
    # P7 r3 D18: a differing model verdict no longer invalidates the answer;
    # the observation carries the recomputed verdict.
    assert observation.valid is True and observation.error_code is None
    assert observation.result.status == "ok"
    assert observation.result.verdict is recomputed
    assert observation.recomputed_verdict is recomputed
    assert observation.passed is recomputed
    assert len(transport.calls) == 1


def test_signature_less_citation_recomputes_false(configs, verified):
    data = case()
    plain = record(
        rows=[{"service": "cart", "operation": OPERATION, "status_tags": {}}]
    )
    data["delivered"] = {EID: plain}
    observation, transport = call_once(
        judge_input(data),
        configs["zai"],
        respond=ok_reply((diagnosis(),), True),
        calibration=verified,
    )
    # P7 r3 D18: the model's True is recorded, the recomputed False counts.
    assert observation.valid is True and observation.error_code is None
    assert observation.result.verdict is False
    assert observation.passed is False
    assert len(transport.calls) == 1


LOCATION_CASES = {
    "bad_location": diagnosis(location="claims[7]"),
    "unknown_location": diagnosis(location="title"),
    "quote_not_found": diagnosis(quote="payment Charge failed badly."),
    "empty_quote": diagnosis(quote=""),
    "citation_not_in_claim": diagnosis(cited_evidence_ids=(EID, "ev-elsewhere")),
    "summary_citation_without_link": diagnosis(location="summary", linked_claim=None),
    "link_to_missing_claim": diagnosis(location="summary", linked_claim=4),
}


@pytest.mark.parametrize("name", sorted(LOCATION_CASES))
def test_location_quote_and_citation_checked(configs, verified, name):
    observation, transport = call_once(
        judge_input(),
        configs["zai"],
        respond=ok_reply((LOCATION_CASES[name],), True),
        calibration=verified,
    )
    assert_failed(observation, transport, "invalid")


def _diag_doc(**changes):
    doc = json.loads(content((diagnosis(),), True))["diagnoses"][0]
    doc.update(changes)
    return doc


SCHEMA_CASES = {
    "not_json": "The report passes.",
    "top_level_array": json.dumps([True]),
    "duplicate_key": content((diagnosis(),), True).replace(
        '"verdict": true', '"verdict": false, "verdict": true'
    ),
    "unknown_key_calibrated": content((diagnosis(),), True, calibrated=True),
    "unknown_key_status": content((diagnosis(),), True, status="ok"),
    "wrong_schema": content((diagnosis(),), True, schema="opspilot-judge-0"),
    "verdict_string": content((diagnosis(),), "true"),
    "verdict_int": content((diagnosis(),), 1),
    "missing_verdict": json.dumps({"schema": "opspilot-judge-1", "diagnoses": []}),
    "linked_claim_bool": content(
        (_diag_doc(location="summary", linked_claim=True),), True
    ),
    "supported_string": content((_diag_doc(supported="true"),), True),
    "factual_int": content((_diag_doc(factual_causal=1),), True),
    "cited_ids_string": content((_diag_doc(cited_evidence_ids=EID),), True),
    "extra_diag_field": content((_diag_doc(confidence=0.9),), True),
    "missing_diag_field": content(
        ({k: v for k, v in _diag_doc().items() if k != "reason"},), True
    ),
    "nan": content((diagnosis(),), True).replace(
        '"verdict": true', '"verdict": true, "score": NaN'
    ),
}


@pytest.mark.parametrize("name", sorted(SCHEMA_CASES))
def test_output_schema_is_strict(configs, verified, name):
    reply = lambda _call: http(completion(SCHEMA_CASES[name], usage=USAGE))  # noqa: E731
    observation, transport = call_once(
        judge_input(), configs["zai"], respond=reply, calibration=verified
    )
    assert_failed(observation, transport, "invalid")
    assert len(transport.calls) == 1


GOOD = content((diagnosis(),), True)
ENVELOPE_CASES = {
    "model_mismatch": (lambda: completion(GOOD, model="glm-5.2"), 200, {"error"}),
    "model_previous": (lambda: completion(GOOD, model="glm-5.3"), 200, {"error"}),
    "model_suffix": (
        lambda: completion(GOOD, model="glm-5.3-flashx-0901"),
        200,
        {"error"},
    ),
    "finish_length": (
        lambda: completion(GOOD, finish_reason="length"),
        200,
        {"invalid"},
    ),
    "finish_tool_calls": (
        lambda: completion(GOOD, finish_reason="tool_calls"),
        200,
        {"invalid"},
    ),
    "tool_calls": (
        lambda: completion(
            GOOD, message={"tool_calls": [{"id": "t", "type": "function"}]}
        ),
        200,
        {"invalid"},
    ),
    "two_choices": (
        lambda: completion(
            "",
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": GOOD},
                    "finish_reason": "stop",
                },
                {
                    "index": 1,
                    "message": {"role": "assistant", "content": GOOD},
                    "finish_reason": "stop",
                },
            ],
        ),
        200,
        {"invalid"},
    ),
    "no_choices": (lambda: completion("", choices=[]), 200, {"invalid"}),
    "index_one": (
        lambda: completion(
            "",
            choices=[
                {
                    "index": 1,
                    "message": {"role": "assistant", "content": GOOD},
                    "finish_reason": "stop",
                }
            ],
        ),
        200,
        {"invalid"},
    ),
    "role_user": (
        lambda: completion(GOOD, message={"role": "user"}),
        200,
        {"invalid"},
    ),
    "body_not_json": (lambda: b"<html>bad gateway</html>", 200, {"invalid"}),
    "http_500": (lambda: completion(GOOD), 500, {"error"}),
    "http_429": (lambda: completion(GOOD), 429, {"error"}),
    "http_401": (lambda: completion(GOOD), 401, {"error"}),
    "http_302": (lambda: completion(GOOD), 302, {"error"}),
}


@pytest.mark.parametrize("name", sorted(ENVELOPE_CASES))
def test_response_envelope_failures(configs, verified, name):
    build, status, expected = ENVELOPE_CASES[name]
    observation, transport = call_once(
        judge_input(),
        configs["zai"],
        respond=lambda _c: http(build(), status=status),
        calibration=verified,
    )
    assert_failed(observation, transport, expected)
    assert len(transport.calls) == 1  # P9: no retry, no redirect follow-up


def test_timeout_maps_to_timeout_without_retry(configs, verified):
    observation, transport = call_once(
        judge_input(),
        configs["zai"],
        respond=lambda _c: TimeoutError("slow"),
        calibration=verified,
    )
    assert_failed(observation, transport, "timeout")
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "error",
    [
        ConnectionError(f"refused {ZAI_CANARY}"),
        RuntimeError(f"provider said Bearer {ZAI_CANARY}"),
        OSError(f"socket closed {ZAI_CANARY}"),
    ],
)
def test_transport_errors_map_to_error_without_leaking(configs, verified, error):
    observation, transport = call_once(
        judge_input(), configs["zai"], respond=lambda _c: error, calibration=verified
    )
    assert_failed(observation, transport, "error")
    assert len(transport.calls) == 4  # P7-D27: three transport retries
    assert observation.attempts == 4
    shown = repr(observation)
    assert ZAI_CANARY not in shown and "provider said" not in shown


def test_oversized_response_is_oversize_and_reading_stops(configs, verified):
    consumed = []

    def chunks():
        for _ in range(64 * 16):  # up to 64 MiB if the judge never stops reading
            consumed.append(65536)
            yield b" " * 65536

    observation, transport = call_once(
        judge_input(),
        configs["zai"],
        respond=lambda _c: JudgeHTTPResponse(status_code=200, chunks=chunks()),
        calibration=verified,
    )
    assert_failed(observation, transport, "oversize")
    # r2 item 7: the first chunk over the limit is counted, the next is never read.
    assert sum(consumed) == MAX_RESPONSE + 65536


def test_response_just_over_limit_is_oversize(configs, verified):
    observation, transport = call_once(
        judge_input(),
        configs["zai"],
        respond=lambda _c: oversize(None, "zai"),
        calibration=verified,
    )
    assert_failed(observation, transport, "oversize")


def test_multi_chunk_response_accepted(configs, verified):
    body = completion(GOOD, usage=USAGE)
    observation, _ = call_once(
        judge_input(),
        configs["zai"],
        respond=lambda _c: http(body, chunk=7),
        calibration=verified,
    )
    assert observation.valid is True and observation.passed is True


def test_private_response_fields_and_raw_response_not_retained(configs, verified):
    marker = "private-reasoning-response-marker"
    observation, _ = call_once(
        judge_input(),
        configs["zai"],
        respond=ok_reply((diagnosis(),), True, message={"reasoning_content": marker}),
        calibration=verified,
    )
    assert observation.valid is True
    assert marker not in repr(observation)
    assert "chatcmpl-test" not in repr(observation)
    assert usage_value(observation.usage, "prompt_tokens") == 11
    assert usage_value(observation.usage, "completion_tokens") == 7
    assert usage_value(observation.usage, "total_tokens") == 18


@pytest.mark.parametrize(
    "usage",
    [None, {}, {"prompt_tokens": -1, "completion_tokens": True, "total_tokens": "18"}],
)
def test_missing_or_bad_usage_is_unknown(configs, usage):
    reply = lambda _c: http(completion(GOOD, usage=usage))  # noqa: E731
    observation, _ = call_once(judge_input(), configs["zai"], respond=reply)
    assert observation.valid is True
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        assert usage_value(observation.usage, name) is None


# ======================================================= 5. blind label pack


def test_fixture_labels_pass_pr_a_recheck(world):
    """Self-check of the authored pack against merged PR-A code (no B3 involved)."""
    for authored in world.authored:
        r = authored.item.request
        assert run_gate(
            r.spec, r.answer, r.outcome, r.report, r.delivered, r.authorized
        ).passed
        evaluation = evaluate(
            r.spec,
            r.answer,
            attempt(scenario_id=r.spec.scenario_id, scenario_version=r.spec.version),
            r.outcome,
            r.report,
            r.delivered,
            r.authorized,
            (trace(),),
            JudgeResult(
                "ok", True, authored.label.diagnoses, authored.label.verdict, "c", "p"
            ),
        )
        assert evaluation.judge_status == "ok", authored.item.item_id
        assert evaluation.diagnosis_pass is authored.label.verdict, (
            authored.item.item_id
        )
    validation = world.pick()
    assert len(world.authored) == 36 and len(validation) == 24
    assert sum(not a.positive for a in validation) == 12
    assert {t for a in validation for t in a.item.coverage} == set(COVERAGE)


def test_freeze_writes_private_index_and_returns_its_hash(frozen):
    directory, pack_hash = frozen
    names = {
        "items.json",
        "annotations-1.json",
        "annotations-2.json",
        "adjudications.json",
    }
    assert {p.name for p in directory.iterdir()} == names | {"pack.json"}
    for path in directory.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600, path.name
    index = json.loads((directory / "pack.json").read_text())
    assert index["schema"] == "opspilot-label-pack-1"
    listed = strings_in(index)
    for name in names:
        assert sha_file(directory / name) in listed, name
    assert pack_hash not in listed
    assert pack_hash == canonical_hash(index)
    assert pack_hash == hashlib.sha256(canonical_bytes(index)).hexdigest()


def test_frozen_pack_holds_no_credentials(frozen):
    directory, _ = frozen
    for path in directory.iterdir():
        for secret in CANARIES:
            assert secret.encode() not in path.read_bytes()


def test_annotators_may_share_a_model_identity(world, tmp_path):
    """B3:47: two distinct annotator identities; model identity may repeat."""
    labels = world.labels
    pack = LabelPack(
        world.items,
        (
            annotation(labels, 0),
            annotation(labels, 1, model_identity="claude-opus-5-5"),
        ),
        (),
    )
    pack_hash = freeze_label_pack(tmp_path / "pack", pack)
    assert load_label_pack(tmp_path / "pack", expected_pack_hash=pack_hash) == pack


def test_freeze_refuses_existing_destination(pack, tmp_path):
    target = tmp_path / "pack"
    target.mkdir()
    (target / "items.json").write_text("keep")
    with pytest.raises(Exception) as caught:
        freeze_label_pack(target, pack)
    expect_judge_error(caught.value, {"OUTPUT_EXISTS"})
    assert (target / "items.json").read_text() == "keep"


def test_load_roundtrip_binds_pack_hash(frozen, pack):
    directory, pack_hash = frozen
    loaded = load_label_pack(directory, expected_pack_hash=pack_hash)
    assert loaded == pack
    with pytest.raises(Exception) as caught:
        load_label_pack(directory, expected_pack_hash="0" * 64)
    expect_judge_error(caught.value, {"HASH_MISMATCH"})


def _copy_pack(frozen, tmp_path):
    directory, pack_hash = frozen
    target = tmp_path / "copy"
    shutil.copytree(directory, target)
    return target, pack_hash


def test_tampered_label_file_rejected(frozen, tmp_path):
    target, pack_hash = _copy_pack(frozen, tmp_path)
    path = target / "annotations-1.json"
    path.write_bytes(path.read_bytes().replace(b"true", b"false", 1))
    with pytest.raises(Exception) as caught:
        load_label_pack(target, expected_pack_hash=pack_hash)
    expect_judge_error(caught.value, {"HASH_MISMATCH"})


def test_resigned_index_with_unknown_field_rejected(frozen, tmp_path):
    target, _ = _copy_pack(frozen, tmp_path)
    index = json.loads((target / "pack.json").read_text())
    index["approved_by_user"] = True
    (target / "pack.json").write_text(json.dumps(index))
    with pytest.raises(Exception) as caught:
        load_label_pack(target, expected_pack_hash=canonical_hash(index))
    expect_judge_error(caught.value, {"SCHEMA_INVALID"})


def test_symlinked_pack_file_rejected(frozen, tmp_path):
    target, pack_hash = _copy_pack(frozen, tmp_path)
    real = tmp_path / "items-real.json"
    shutil.move(target / "items.json", real)
    (target / "items.json").symlink_to(real)
    with pytest.raises(Exception) as caught:
        load_label_pack(target, expected_pack_hash=pack_hash)
    expect_judge_error(caught.value, None)


def expect_judge_error(exc: BaseException, codes) -> str:
    """r2 item 6: label/calibration errors are JudgeError with a fixed code."""
    assert isinstance(exc, JudgeError), type(exc).__name__
    # r3 item 2: every converted error leaves no implicit or explicit chain.
    assert exc.__cause__ is None and exc.__context__ is None
    assert_no_leak(exc)
    return expect_code(exc, codes)


def assert_pack_rejected(
    tmp_path, build, configs, world, *, frozen_stage=True, codes=None
):
    """B3:46-51: invalid packs are refused before any judge call; freeze or load refuses too.

    codes=None only where r2 fixes no single code (EVAL_B3_AMBIGUITIES.md r2-tightening).
    """
    try:
        bad = build()
    except Exception as exc:  # strict DTO construction counts as rejection
        expect_judge_error(exc, codes)
        return
    ts = transports(world)
    destination = tmp_path / "calibration"
    with pytest.raises(Exception) as caught:
        run_calibration(bad, configs=configs, transports=ts, destination=destination)
    expect_judge_error(caught.value, codes)
    assert all(t.calls == [] for t in ts.values())
    assert not (destination / "calibration.json").exists()
    if not frozen_stage:
        return
    try:
        pack_hash = freeze_label_pack(tmp_path / "pack", bad)
    except Exception as exc:
        expect_judge_error(exc, codes)
        return
    with pytest.raises(Exception) as caught:
        load_label_pack(tmp_path / "pack", expected_pack_hash=pack_hash)
    expect_judge_error(caught.value, codes)


def _subset(world, keep):
    authored = [a for a in world.authored if keep(a)]
    labels = {a.item.item_id: a.label for a in authored}
    return LabelPack(
        items=tuple(a.item for a in authored),
        annotations=(annotation(labels, 0), annotation(labels, 1)),
        adjudications=(),
    )


def _swap(world, item_id, new_item=None, new_label=None):
    """Replace one item (and its label in both annotations)."""
    items = [new_item if i.item_id == item_id and new_item else i for i in world.items]
    labels = dict(world.labels)
    if new_label is not None:
        labels.pop(item_id)
        labels[new_label.item_id] = new_label
    return LabelPack(
        items=tuple(items),
        annotations=(annotation(labels, 0), annotation(labels, 1)),
        adjudications=(),
    )


def _first(world, **kw):
    return world.pick(**kw)[0]


def _mut_five_scenarios(world):
    return _subset(world, lambda a: a.item.scenario_id != "eval-normal-2")


def _mut_three_debug(world):
    a = _first(world, positive=True, kind="fault")
    return _swap(world, a.item.item_id, replace(a.item, split="debug"))


def _mut_unknown_split(world):
    a = _first(world, positive=True, kind="fault")
    return _swap(world, a.item.item_id, replace(a.item, split="holdout"))


def _mut_family_crosses(world):
    debug = world.pick(split="debug")[0]
    v = next(a for a in world.pick() if a.item.scenario_id == debug.item.scenario_id)
    return _swap(
        world, v.item.item_id, replace(v.item, source_family=debug.item.source_family)
    )


def _mut_eleven_negatives(world):
    a = _first(world, positive=False, kind="normal")
    spec, answer = a.item.request.spec, a.item.request.answer
    k = int(a.item.item_id.rsplit("-", 1)[1])
    positive = author_item(spec, answer, "validation", k, "hypothesis", ("hypothesis",))
    positive_item = replace(positive.item, item_id=a.item.item_id)
    positive_label = replace(positive.label, item_id=a.item.item_id)
    return _swap(world, a.item.item_id, positive_item, positive_label)


def _mut_coverage_missing(world):
    a = next(x for x in world.pick() if x.item.coverage == ("insufficient_evidence",))
    return _swap(
        world, a.item.item_id, replace(a.item, coverage=("extra_wrong_cause",))
    )


def _mut_duplicate_item_id(world):
    a, b = world.pick(positive=True, kind="fault")[:2]
    return LabelPack(
        items=tuple(
            replace(i, item_id=a.item.item_id) if i.item_id == b.item.item_id else i
            for i in world.items
        ),
        annotations=(annotation(world.labels, 0), annotation(world.labels, 1)),
        adjudications=(),
    )


def _mut_duplicate_input(world):
    a, b = world.pick(positive=True, kind="fault")[:2]
    twin = replace(a.item, item_id=b.item.item_id, source_family=b.item.source_family)
    label = replace(a.label, item_id=b.item.item_id)
    return _swap(world, b.item.item_id, twin, label)


def _mut_wrong_input_hash(world):
    a = _first(world, positive=True, kind="fault")
    return _swap(
        world,
        a.item.item_id,
        replace(a.item, input_hash="0" * 64),
        replace(a.label, input_hash="0" * 64),
    )


def _mut_label_hash_mismatch(world):
    a = _first(world, positive=True, kind="fault")
    return _swap(world, a.item.item_id, None, replace(a.label, input_hash="1" * 64))


def _mut_one_annotation(world):
    return LabelPack(world.items, (annotation(world.labels, 0),), ())


def _mut_three_annotations(world):
    third = annotation(
        world.labels,
        1,
        annotator_identity="annotator-c",
        model_identity="claude-haiku-5-5",
    )
    return LabelPack(
        world.items,
        (annotation(world.labels, 0), annotation(world.labels, 1), third),
        (),
    )


def _mut_same_identity(world):
    return LabelPack(
        world.items, (annotation(world.labels, 0), annotation(world.labels, 0)), ()
    )


def _mut_same_annotator_other_model(world):
    other = annotation(world.labels, 0, model_identity="claude-sonnet-5-5")
    return LabelPack(world.items, (annotation(world.labels, 0), other), ())


def _mut_annotation_missing_item(world):
    labels = dict(world.labels)
    labels.pop(world.authored[0].item.item_id)
    return LabelPack(
        world.items, (annotation(world.labels, 0), annotation(labels, 1)), ()
    )


def _mut_label_verdict_inconsistent(world):
    a = _first(world, positive=True, kind="fault")
    return _swap(world, a.item.item_id, None, replace(a.label, diagnoses=()))


def _mut_label_bad_quote(world):
    a = _first(world, positive=True, kind="fault")
    d = replace(a.label.diagnoses[0], quote="A sentence that the report never says.")
    return _swap(world, a.item.item_id, None, replace(a.label, diagnoses=(d,)))


def _mut_label_bad_location(world):
    a = _first(world, positive=True, kind="fault")
    d = replace(a.label.diagnoses[0], location="claims[9]")
    return _swap(world, a.item.item_id, None, replace(a.label, diagnoses=(d,)))


def _disagreeing_label(world):
    a = _first(world, positive=True, kind="normal")
    d = _diag(
        "claims[0]", OBSERVATION, "frontend", "GET /api/cart", (PLAIN,), supported=False
    )
    return a, replace(a.label, diagnoses=(d,), verdict=False)


def _mut_disagreement_unadjudicated(world):
    a, other = _disagreeing_label(world)
    second = dict(world.labels)
    second[a.item.item_id] = other
    return LabelPack(
        world.items, (annotation(world.labels, 0), annotation(second, 1)), ()
    )


def _mut_missed_diagnosis_disagreement(world):
    """Same verdict, different diagnoses (a missed diagnosis) still needs adjudication."""
    a = _first(world, positive=False, kind="fault", variant="extra_wrong_cause")
    second = dict(world.labels)
    second[a.item.item_id] = replace(a.label, diagnoses=a.label.diagnoses[1:])
    return LabelPack(
        world.items, (annotation(world.labels, 0), annotation(second, 1)), ()
    )


PACK_MUTATIONS = {
    name[len("_mut_") :]: fn
    for name, fn in globals().items()
    if name.startswith("_mut_")
}


# r2 item 6 fixed codes; None = r2 names no single code for this case.
MUTATION_CODES = {
    "five_scenarios": {"SCHEMA_INVALID"},
    "three_debug": {"SCHEMA_INVALID"},
    "unknown_split": {"SCHEMA_INVALID"},
    "duplicate_item_id": {"SCHEMA_INVALID"},
    "duplicate_input": None,
    "family_crosses": {"CALIBRATION_SET_OVERLAP"},
    "eleven_negatives": {"CALIBRATION_TOO_FEW_NEGATIVES"},
    "coverage_missing": {"CALIBRATION_COVERAGE_INCOMPLETE"},
    "wrong_input_hash": {"HASH_MISMATCH"},
    "label_hash_mismatch": {"HASH_MISMATCH"},
    "label_verdict_inconsistent": {"CALIBRATION_LABEL_INVALID"},
    "label_bad_quote": {"CALIBRATION_LABEL_INVALID"},
    "label_bad_location": {"CALIBRATION_LABEL_INVALID"},
    "disagreement_unadjudicated": {"CALIBRATION_UNRESOLVED"},
    "missed_diagnosis_disagreement": {"CALIBRATION_UNRESOLVED"},
    "one_annotation": None,
    "three_annotations": None,
    "same_identity": None,
    "same_annotator_other_model": None,
    "annotation_missing_item": None,
}


def test_every_mutation_has_a_code_decision():
    assert set(MUTATION_CODES) == set(PACK_MUTATIONS)


@pytest.mark.parametrize("name", sorted(PACK_MUTATIONS))
def test_invalid_pack_rejected_before_any_call(tmp_path, configs, world, name):
    assert_pack_rejected(
        tmp_path,
        lambda: PACK_MUTATIONS[name](world),
        configs,
        world,
        codes=MUTATION_CODES[name],
    )


@pytest.mark.parametrize(
    "model", ["glm-5.3", "deepseek-flash", "glm-5.3-flashx", "gpt-5.6-luna"]
)
def test_candidate_judge_cannot_be_annotator(tmp_path, configs, world, model):
    def build():
        return LabelPack(
            world.items,
            (
                annotation(world.labels, 0),
                annotation(world.labels, 1, model_identity=model),
            ),
            (),
        )

    assert_pack_rejected(tmp_path, build, configs, world)


def test_sol_cannot_be_annotator(tmp_path, configs, world):
    def build():
        return LabelPack(
            world.items,
            (
                annotation(world.labels, 0, model_identity=SOL_MODEL),
                annotation(world.labels, 1),
            ),
            (),
        )

    assert_pack_rejected(tmp_path, build, configs, world, frozen_stage=False)


def _annotation_file_hash(tmp_path, world, labels, index, name):
    """Hash of an annotations file as freeze writes it for this exact AnnotationSet."""
    first = annotation(labels, index)
    shadow = replace(
        first, annotator_identity="annotator-shadow", model_identity="claude-haiku-5-5"
    )
    freeze_label_pack(tmp_path / name, LabelPack(world.items, (first, shadow), ()))
    return sha_file(tmp_path / name / "annotations-1.json")


@pytest.fixture
def adjudicated(tmp_path, world):
    a, other = _disagreeing_label(world)
    second = dict(world.labels)
    second[a.item.item_id] = other
    h1 = _annotation_file_hash(tmp_path, world, world.labels, 0, "probe-1")
    h2 = _annotation_file_hash(tmp_path, world, second, 1, "probe-2")
    annotations = (annotation(world.labels, 0), annotation(second, 1))

    def pack_with(
        *, hashes=(h1, h2), record="User ruled 2026-10-14: hypothesis only.", item=a
    ):
        ruling = Adjudication(
            item_id=item.item.item_id,
            annotation_hashes=hashes,
            decision_record=record,
            label=item.label,
        )
        return LabelPack(world.items, annotations, (ruling,))

    return pack_with, (h1, h2)


def test_bound_adjudication_resolves_disagreement(
    adjudicated, configs, world, tmp_path
):
    pack_with, _ = adjudicated
    good = pack_with()
    pack_hash = freeze_label_pack(tmp_path / "pack", good)
    assert load_label_pack(tmp_path / "pack", expected_pack_hash=pack_hash) == good
    record = run_calibration(
        good,
        configs=configs,
        transports=transports(world),
        destination=tmp_path / "cal",
    )
    assert record.status == "selected" and record.selected_provider == "zai"


@pytest.mark.parametrize(
    "flaw", ["swapped_hashes", "unknown_hash", "empty_record", "blank_record"]
)
def test_adjudication_must_bind_both_files_and_a_record(
    adjudicated, configs, world, tmp_path, flaw
):
    pack_with, (h1, h2) = adjudicated
    build = {
        "swapped_hashes": lambda: pack_with(hashes=(h2, h1)),
        "unknown_hash": lambda: pack_with(hashes=("0" * 64, h2)),
        "empty_record": lambda: pack_with(record=""),
        "blank_record": lambda: pack_with(record="   "),
    }[flaw]
    # r2 item 6: adjudication defects are CALIBRATION_UNRESOLVED; r2 names no
    # single code for wrong/swapped file hashes (HASH_MISMATCH is also plausible).
    codes = {"CALIBRATION_UNRESOLVED"} if flaw.endswith("_record") else None
    assert_pack_rejected(tmp_path, build, configs, world, codes=codes)


def test_adjudication_forbidden_on_agreeing_item(adjudicated, configs, world, tmp_path):
    pack_with, _ = adjudicated
    agreeing = _first(world, positive=True, kind="fault")
    assert_pack_rejected(
        tmp_path,
        lambda: pack_with(item=agreeing),
        configs,
        world,
        codes={"CALIBRATION_UNRESOLVED"},
    )


# ========================================================= 5. run_calibration


def test_calibration_calls_once_per_debug_twice_per_validation_serially(
    world, pack, configs, tmp_path
):
    probe = Probe()
    ts = transports(world, probe)
    run_calibration(pack, configs=configs, transports=ts, destination=tmp_path / "cal")
    for name, transport in ts.items():
        assert len(transport.calls) == 12 + 48, name
        for authored in world.authored:
            expected = 1 if authored.item.split == "debug" else 2
            assert transport.per_item[authored.item.item_id] == expected
        for call in transport.calls:
            assert call.endpoint == ENDPOINTS[name]
    assert probe.max_active == 1


def test_calibration_accepts_an_observation_retried_after_transport_failure(
    world, pack, configs, selected, tmp_path
):
    """P7-D27: a transport retry is recorded and passes strict verification."""
    item = next(a.item.item_id for a in world.authored if a.item.split == "debug")
    ts = transports(
        world, zai={(item, 1): lambda _l, _p: JudgeError("JUDGE_TRANSPORT_ERROR")}
    )
    destination = tmp_path / "cal"
    record = run_calibration(
        pack, configs=configs, transports=ts, destination=destination
    )
    assert len(ts["zai"].calls) == 12 + 48 + 1
    assert ts["zai"].per_item[item] == 2  # the retry answered from the label
    _, baseline = selected
    assert record.results == baseline.results
    assert record.selected_provider == baseline.selected_provider == "zai"
    attempts = [
        json.loads((destination / f"zai-{n:03d}.json").read_bytes())["observation"].get(
            "attempts"
        )
        for n in range(1, 61)
    ]
    assert attempts.count(2) == 1 and attempts.count(1) == 59
    load_verified_calibration(destination, pack=pack, config=configs["zai"])


def test_selected_record_and_artifact(selected, frozen, configs, pack):
    destination, record = selected
    _, pack_hash = frozen
    assert record.status == "selected"
    assert record.selected_provider == "zai"
    assert "deepseek" not in record.eligible
    assert record.pack_hash == pack_hash
    # B3:51: the labels are frozen under destination/label-pack before any call.
    assert (
        load_label_pack(destination / "label-pack", expected_pack_hash=pack_hash)
        == pack
    )
    for name in MODELS:
        result = record.results[name]
        assert (result.items, result.comparisons, result.agreement) == (24, 48, 48)
        assert (result.negatives, result.false_passes, result.passed) == (12, 0, True)
    doc = json.loads((destination / "calibration.json").read_text())
    assert doc["schema"] == "opspilot-judge-calibration-1"
    listed = strings_in(doc)
    assert pack_hash in listed
    for config in configs.values():
        assert judge_config_hash(config) in listed
    for path in destination.rglob("*"):
        if path.is_file():
            for secret in CANARIES:
                assert secret.encode() not in path.read_bytes()


def test_artifact_keeps_no_private_response_fields(world, pack, configs, tmp_path):
    marker = "calibration-reasoning-marker"

    def with_reasoning(label, provider):
        return http(
            completion(
                content(label.diagnoses, label.verdict),
                model=MODELS[provider],
                message={"reasoning_content": marker},
                usage=USAGE,
            )
        )

    ts = transports(world, zai=everywhere(with_reasoning, world.authored))
    run_calibration(pack, configs=configs, transports=ts, destination=tmp_path / "cal")
    for path in (tmp_path / "cal").rglob("*"):
        if path.is_file():
            assert marker.encode() not in path.read_bytes()


def test_existing_destination_not_overwritten(world, pack, configs, tmp_path):
    destination = tmp_path / "cal"
    destination.mkdir()
    (destination / "calibration.json").write_text("keep")
    ts = transports(world)
    with pytest.raises(Exception) as caught:
        run_calibration(pack, configs=configs, transports=ts, destination=destination)
    expect_judge_error(caught.value, {"OUTPUT_EXISTS"})
    assert all(t.calls == [] for t in ts.values())  # r2 item 8: before sending
    assert (destination / "calibration.json").read_text() == "keep"


@pytest.mark.parametrize("missing", sorted(MODELS))
def test_all_three_providers_required(world, pack, configs, tmp_path, missing):
    ts = transports(world)
    partial = {k: v for k, v in configs.items() if k != missing}
    with pytest.raises(Exception) as caught:
        run_calibration(
            pack, configs=partial, transports=ts, destination=tmp_path / "a"
        )
    expect_judge_error(caught.value, None)
    partial_ts = {k: v for k, v in ts.items() if k != missing}
    with pytest.raises(Exception) as caught:
        run_calibration(
            pack, configs=configs, transports=partial_ts, destination=tmp_path / "b"
        )
    expect_judge_error(caught.value, None)
    assert all(t.calls == [] for t in ts.values())


def _false_fails(world, n):
    """n disagreeing (false-fail) comparisons on positive fault validation items."""
    out = {}
    for authored in world.pick(positive=True, kind="fault"):
        for rep in (1, 2):
            if len(out) < n:
                out[(authored.item.item_id, rep)] = false_fail
    return out


def _normal_negative(world):
    return _first(world, positive=False, kind="normal", variant="hidden_normal_cause")


SELECTION_CASES = {
    # name: (zai agreement offsets, openai offsets, expected status, selected, eligible)
    "glm48_sol48": (0, 0, "selected", "zai", {"zai", "openai"}),
    "glm46_sol48": (2, 0, "pending_user_decision", None, {"zai", "openai"}),
    "glm46_sol46": (2, 2, "selected", "zai", {"zai", "openai"}),
    "glm47_sol46": (1, 2, "selected", "zai", {"zai", "openai"}),
    "glm45_sol48": (3, 0, "pending_user_decision", None, {"openai"}),
    "glm48_sol45": (0, 3, "selected", "zai", {"zai"}),
    "both_fail": (3, 3, "no_baseline", None, set()),
}


@pytest.mark.parametrize("name", sorted(SELECTION_CASES))
def test_d16_selection_rule(world, pack, configs, tmp_path, name):
    zai_miss, sol_miss, status, chosen, eligible = SELECTION_CASES[name]
    ts = transports(
        world, zai=_false_fails(world, zai_miss), openai=_false_fails(world, sol_miss)
    )
    record = run_calibration(
        pack, configs=configs, transports=ts, destination=tmp_path / "cal"
    )
    assert record.status == status
    assert record.selected_provider == chosen
    assert set(record.eligible) == eligible
    assert record.results["zai"].agreement == 48 - zai_miss
    assert record.results["openai"].agreement == 48 - sol_miss
    assert record.results["deepseek"].agreement == 48


def test_false_pass_disqualifies_despite_47_agreement(world, pack, configs, tmp_path):
    item = _normal_negative(world).item.item_id
    ts = transports(world, zai={(item, 1): false_pass})
    record = run_calibration(
        pack, configs=configs, transports=ts, destination=tmp_path / "cal"
    )
    assert record.results["zai"].passed is False
    assert record.results["zai"].false_passes == 1
    assert record.results["zai"].agreement == 47
    assert "zai" not in record.eligible
    assert record.status == "pending_user_decision" and record.selected_provider is None


def test_deepseek_never_eligible_even_when_only_one_passing(
    world, pack, configs, tmp_path
):
    ts = transports(world, zai=_false_fails(world, 3), openai=_false_fails(world, 3))
    record = run_calibration(
        pack, configs=configs, transports=ts, destination=tmp_path / "cal"
    )
    assert record.results["deepseek"].passed is True
    assert record.status == "no_baseline"
    assert record.selected_provider is None and tuple(record.eligible) == ()


ERROR_KINDS = {
    "http_error": http_error,
    "timeout": timeout,
    "invalid": not_json,
    "oversize": oversize,
    "missed_key_diagnosis": missed_diagnosis,
}


@pytest.mark.parametrize("kind", sorted(ERROR_KINDS))
def test_failed_call_is_never_a_correct_false(world, pack, configs, tmp_path, kind):
    """B3:51 / C P7: an error on a negative item fails the provider, not agrees."""
    item = _normal_negative(world).item.item_id
    ts = transports(world, zai={(item, 1): ERROR_KINDS[kind]})
    record = run_calibration(
        pack, configs=configs, transports=ts, destination=tmp_path / "cal"
    )
    result = record.results["zai"]
    assert result.passed is False
    # B3:51: the spurious agreement of the failed call is deducted; not a false pass.
    assert (result.comparisons, result.agreement, result.false_passes) == (48, 47, 0)
    assert "zai" not in record.eligible
    assert record.status == "pending_user_decision"
    assert record.selected_provider is None
    assert len(ts["zai"].calls) == 60  # zero retries for the failed call


# ================================================ 5. load_verified_calibration


def test_verified_calibration_only_for_selected_provider(
    selected, pack, configs, verified
):
    destination, _ = selected
    assert verified is not None
    for provider in ("openai", "deepseek"):
        with pytest.raises(Exception) as caught:
            load_verified_calibration(destination, pack=pack, config=configs[provider])
        expect_judge_error(
            caught.value, {"CALIBRATION_UNRESOLVED", "JUDGE_IDENTITY_MISMATCH"}
        )


@pytest.mark.parametrize("name", ["glm46_sol48", "both_fail"])
def test_unresolved_record_yields_no_verified_calibration(
    world, pack, configs, tmp_path, name
):
    zai_miss, sol_miss, *_ = SELECTION_CASES[name]
    ts = transports(
        world, zai=_false_fails(world, zai_miss), openai=_false_fails(world, sol_miss)
    )
    run_calibration(pack, configs=configs, transports=ts, destination=tmp_path / "cal")
    for provider in MODELS:
        with pytest.raises(Exception) as caught:
            load_verified_calibration(
                tmp_path / "cal", pack=pack, config=configs[provider]
            )
        expect_judge_error(caught.value, {"CALIBRATION_UNRESOLVED"})  # r2 item 3


def _walk(value, visit):
    if isinstance(value, dict):
        visit(value)
        for v in value.values():
            _walk(v, visit)
    elif isinstance(value, list):
        for v in value:
            _walk(v, visit)


def test_forged_selection_rejected(world, pack, configs, tmp_path):
    ts = transports(world, zai=_false_fails(world, 2))  # pending: GLM 46 < Sol 48
    run_calibration(pack, configs=configs, transports=ts, destination=tmp_path / "cal")
    path = tmp_path / "cal" / "calibration.json"
    doc = json.loads(path.read_text())
    hits = []

    def promote(node):
        if (
            node.get("status") == "pending_user_decision"
            and "selected_provider" in node
        ):
            node["status"], node["selected_provider"] = "selected", "openai"
            hits.append(node)

    _walk(doc, promote)
    assert hits, "selection record not found in calibration.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(Exception) as caught:
        load_verified_calibration(tmp_path / "cal", pack=pack, config=configs["openai"])
    expect_judge_error(caught.value, {"DERIVED_RESULT_MISMATCH"})


def test_forged_counts_rejected(world, pack, configs, tmp_path):
    item = _normal_negative(world).item.item_id
    ts = transports(world, zai={(item, 1): false_pass}, openai=_false_fails(world, 3))
    record = run_calibration(
        pack, configs=configs, transports=ts, destination=tmp_path / "cal"
    )
    assert record.status == "no_baseline"
    path = tmp_path / "cal" / "calibration.json"
    doc = json.loads(path.read_text())
    hits = []

    def whitewash(node):
        if "false_passes" in node and node.get("false_passes"):
            node["false_passes"], node["passed"], node["reasons"] = 0, True, []
            hits.append(node)
        if node.get("status") == "no_baseline" and "selected_provider" in node:
            node["status"], node["selected_provider"] = "selected", "zai"
            node["eligible"] = ["zai"]
            hits.append(node)

    _walk(doc, whitewash)
    assert hits
    path.write_text(json.dumps(doc))
    with pytest.raises(Exception) as caught:
        load_verified_calibration(tmp_path / "cal", pack=pack, config=configs["zai"])
    expect_judge_error(caught.value, {"DERIVED_RESULT_MISMATCH"})


def test_verified_calibration_bound_to_pack_and_config(
    selected, world, configs, tmp_path
):
    destination, _ = selected
    # A valid pack with different bytes (r2 item 2: model identity may repeat),
    # so only the pack binding differs.
    labels = world.labels
    other_pack = LabelPack(
        world.items,
        (
            annotation(labels, 0),
            annotation(labels, 1, model_identity="claude-opus-5-5"),
        ),
        (),
    )
    with pytest.raises(Exception) as caught:
        load_verified_calibration(destination, pack=other_pack, config=configs["zai"])
    expect_judge_error(caught.value, {"HASH_MISMATCH"})
    prompt_changed = copy.copy(configs["zai"])
    object.__setattr__(prompt_changed, "prompt", configs["zai"].prompt + " v2")
    with pytest.raises(Exception) as caught:
        load_verified_calibration(
            destination, pack=make_pack(world), config=prompt_changed
        )
    # r2 item 1 validates the profile apart from the hash, and no valid zai
    # config hashes differently, so a changed prompt may fail the profile first.
    expect_judge_error(caught.value, {"HASH_MISMATCH", "CONFIG_INVALID"})


# ============================================== r3: review counterexamples


CASED_SLOTS = (  # r3 item 1: camel/Pascal/kebab/acronym spellings of sensitive slots
    "clientSecret",
    "ClientApiKey",
    "client-secret",
    "clientAPIKey",
    "AccessToken",
    "x-api-key",
    "userPassword",
    "sessionCookie",
    "reasoningContent",
    "RedactedThinking",
    "encrypted-content",
    "modelReasoning",
)


@pytest.mark.parametrize("placement", ["view_top", "view_nested"])
@pytest.mark.parametrize("key", CASED_SLOTS)
def test_r3_cased_sensitive_slots_never_reach_transport(configs, key, placement):
    observation, transport = call_once(
        PLACEMENTS[placement](key),
        configs["zai"],
        respond=ok_reply((diagnosis(),), True),
    )
    assert transport.calls == []
    assert observation.result.status == "invalid"
    assert observation.passed is False
    assert observation.error_code in OBSERVATION_CODES
    assert "private-protocol-marker" not in repr(observation)


@pytest.mark.parametrize(
    ("raw", "codes"),
    [  # r2 item 6: decode failure is CONFIG_UNREADABLE; NUL is rejected (code not fixed)
        (b"\xff\xfe\xfa", {"CONFIG_UNREADABLE"}),
        (b"\x00", None),
    ],
)
def test_r3_bad_bytes_config_error_has_no_chain_or_key_bytes(tmp_path, raw, codes):
    text = env_text().encode()
    name = CREDENTIAL_NAMES["zai"].encode()
    # Corrupt the credential line itself so a leaking decoder would echo the key.
    text = text.replace(
        name + b"=" + ZAI_CANARY.encode(), name + b"=" + ZAI_CANARY.encode() + raw
    )
    path = tmp_path.resolve() / ".env.judge"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(text)
    exc = config_error(path, codes if codes else CONFIG_ERROR_CODES)
    assert exc.__cause__ is None and exc.__context__ is None
    assert ZAI_CANARY.encode().hex() not in leak_surface(exc)


CONFIG_ERROR_CODES = frozenset(
    {
        "CONFIG_ABSOLUTE_PATH_REQUIRED",
        "CONFIG_PRIVATE_REGULAR_FILE_REQUIRED",
        "CONFIG_UNREADABLE",
        "CONFIG_SYNTAX",
        "CONFIG_ENV_CONFLICT",
        "CONFIG_INVALID",
        "JUDGE_IDENTITY_MISMATCH",
    }
)


class _CodedTransportError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _judge_error_with(code: str) -> BaseException:
    exc = JudgeError(code)
    try:
        exc.code = code
    except AttributeError:
        pass
    return exc


@pytest.mark.parametrize("factory", [_judge_error_with, _CodedTransportError])
@pytest.mark.parametrize(
    "code",
    [
        f"Bearer {ZAI_CANARY}",
        f"AUTH_{ZAI_CANARY}",
        "JUDGE_TOTALLY_FINE",
        "CONFIG_ENV_CONFLICT\n",
    ],
)
def test_r3_transport_error_codes_are_whitelisted(configs, verified, factory, code):
    error = factory(code)
    observation, transport = call_once(
        judge_input(), configs["zai"], respond=lambda _c: error, calibration=verified
    )
    assert_failed(observation, transport, "error")
    assert observation.error_code == "JUDGE_TRANSPORT_ERROR"
    assert len(transport.calls) == 4  # P7-D27: three transport retries
    assert ZAI_CANARY not in repr(observation)
    assert "TOTALLY_FINE" not in repr(observation)


@pytest.mark.parametrize(
    "name",
    ["OPENAI_JUDGE_MODEL", "OPENAI_JUDGE_RESPONSE_MODEL"],
)
@pytest.mark.parametrize(
    "value",
    [
        "gpt-6.1-astra",
        "gpt-6.1",
        "gpt-6.1-sol",
        "gpt-5.6-luna-2026-09-30",
        "GPT-5.6-LUNA",
        "gpt-5.6-luna-preview",
    ],
)
def test_r3_openai_config_identity_is_fixed(tmp_path, name, value):
    """r3 item 4: config request/response identity is exactly gpt-5.6-luna."""
    path = write_env(tmp_path, env_text(override={name: value}))
    config_error(path, {"JUDGE_IDENTITY_MISMATCH"}, provider="openai")


def test_r3_openai_dated_snapshot_response_accepted(configs):
    observation, _ = call_once(
        judge_input(),
        configs["openai"],
        respond=ok_reply((diagnosis(),), True, model="gpt-5.6-luna-2026-09-30"),
    )
    assert observation.valid is True
    assert observation.response_model == "gpt-5.6-luna-2026-09-30"


@pytest.mark.parametrize(
    "model",
    [
        "gpt-6.1-astra",
        "gpt-5.6-luna-preview",
        "gpt-5.6-luna-20260930",
        "gpt-5.6-luna-2026-02-30",  # impossible date
        "gpt-5.6-luna-2026-13-01",
        "gpt-5.6-luna-2026-09-30-x",
        "GPT-5.6-LUNA",
        "gpt-6.1-sol",
        "glm-5.3",
    ],
)
def test_r3_openai_response_identity_rejected(configs, verified, model):
    observation, transport = call_once(
        judge_input(),
        configs["openai"],
        respond=ok_reply((diagnosis(),), True, model=model),
    )
    assert_failed(observation, transport, "invalid")
    assert observation.error_code == "JUDGE_RESPONSE_IDENTITY_INVALID"


def test_r3_wrong_sol_identity_cannot_complete_d16(world, pack, configs, tmp_path):
    def astra(label, provider):
        return http(
            completion(
                content(label.diagnoses, label.verdict),
                model="gpt-6.1-astra",
                usage=USAGE,
            )
        )

    ts = transports(
        world, openai=everywhere(astra, world.authored), zai=_false_fails(world, 2)
    )
    record = run_calibration(
        pack, configs=configs, transports=ts, destination=tmp_path / "cal"
    )
    assert record.results["openai"].passed is False
    assert "openai" not in record.eligible
    with pytest.raises(Exception) as caught:
        load_verified_calibration(tmp_path / "cal", pack=pack, config=configs["openai"])
    expect_judge_error(caught.value, None)


def test_r3_swapped_summary_cache_rejected(world, pack, configs, tmp_path):
    """r3 item 5: calibration.json from a passing run cannot cover all-error raw records."""
    failing = tmp_path / "failing"
    ts = transports(world, zai=everywhere(http_error, world.authored))
    record = run_calibration(pack, configs=configs, transports=ts, destination=failing)
    assert record.results["zai"].passed is False and record.selected_provider is None
    passing = tmp_path / "passing"
    good = run_calibration(
        pack, configs=configs, transports=transports(world), destination=passing
    )
    assert good.status == "selected" and good.selected_provider == "zai"
    target = failing / "calibration.json"
    os.chmod(target, 0o600)
    target.write_bytes((passing / "calibration.json").read_bytes())
    with pytest.raises(Exception) as caught:
        load_verified_calibration(failing, pack=pack, config=configs["zai"])
    # r3 item 5 names HASH_MISMATCH/IDENTITY_MISMATCH; r2 item 3 names
    # DERIVED_RESULT_MISMATCH for saved derived values (see EVAL_B3_AMBIGUITIES.md).
    expect_judge_error(
        caught.value, {"HASH_MISMATCH", "IDENTITY_MISMATCH", "DERIVED_RESULT_MISMATCH"}
    )


def _raw_records(destination, provider):
    return sorted(
        p
        for p in destination.iterdir()
        if p.is_file() and p.name.startswith(provider) and p.name != "calibration.json"
    )


def test_r3_raw_records_are_the_source_of_truth(world, pack, configs, tmp_path):
    """r3 item 5: 60 per-call records per provider; a missing one is EVIDENCE_UNAVAILABLE,
    and a verified calibration stops passing once its source records change."""
    destination = tmp_path / "cal"
    run_calibration(
        pack, configs=configs, transports=transports(world), destination=destination
    )
    for provider in MODELS:
        assert len(_raw_records(destination, provider)) == 60, provider
    proof = load_verified_calibration(destination, pack=pack, config=configs["zai"])
    observation, _ = call_once(
        judge_input(),
        configs["zai"],
        respond=ok_reply((diagnosis(),), True),
        calibration=proof,
    )
    assert observation.passed is True
    first = _raw_records(destination, "zai")[0]
    first.unlink()
    with pytest.raises(Exception) as caught:
        load_verified_calibration(destination, pack=pack, config=configs["zai"])
    expect_judge_error(caught.value, {"EVIDENCE_UNAVAILABLE"})
    transport = FakeTransport(ok_reply((diagnosis(),), True))
    try:
        stale = judge_once(
            judge_input(), config=configs["zai"], calibration=proof, transport=transport
        )
    except Exception as exc:
        expect_judge_error(exc, None)
    else:
        assert stale.passed is False


# ================================================================= 6. network


def test_network_guard_is_armed(network_guard):
    before = len(network_guard.attempts)
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("api.z.ai", 443), timeout=1)
    with pytest.raises(ConnectionRefusedError):
        socket.getaddrinfo("api.openai.com", 443)
    probe = socket.socket()
    try:
        with pytest.raises(ConnectionRefusedError):
            probe.connect(("127.0.0.1", 9))
    finally:
        probe.close()
    assert len(network_guard.attempts) == before + 3
    del network_guard.attempts[before:]
