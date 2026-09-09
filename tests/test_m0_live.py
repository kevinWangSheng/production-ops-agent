"""Real HTTP boundary exercised with memory transports; no credentials or live uploads."""

import asyncio
import copy
import json
from datetime import timedelta
from uuid import uuid4

import httpx2
import pytest

from scripts.m0.config import PROFILE, Config, ConfigError
from scripts.m0.live import (
    MODEL_PROFILE,
    code_digest,
    digest,
    execute,
    utcnow,
    validate,
)
from scripts.m0.protocol import FIXTURE, no_network


def packet():
    deadline = (utcnow() + timedelta(hours=1)).isoformat()
    config = Config(
        PROFILE
        | {
            "DEEPSEEK_API_KEY": "fake-model-auth",
            "LANGSMITH_API_KEY": "fake-trace-auth",
            "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
            "LANGSMITH_PROJECT": "existing-m0",
            "OPSPILOT_EXPERIMENT_BUDGET_CNY": "2.00",
            "OPSPILOT_EXPERIMENT_DEADLINE_UTC": deadline,
            "OPSPILOT_DATABASE_URL": "host=127.0.0.1 port=55431 dbname=m0_budget user=m0_lab",
        }
    )
    contract = {
        "version": "m0-normal-1-v2",
        "model_profile": copy.deepcopy(MODEL_PROFILE),
        "approved": True,
        "approval_ref": "synthetic-approval-" + str(uuid4()),
        "experiment_id": str(uuid4()),
        "run_id": str(uuid4()),
        "deadline": deadline,
        "budget_cny": "2.00",
        "code_sha256": code_digest(),
        "deepseek_key_sha256": digest(b"fake-model-auth"),
        "langsmith_key_sha256": digest(b"fake-trace-auth"),
        "endpoint": config.values["LANGSMITH_ENDPOINT"],
        "workspace_id": str(uuid4()),
        "project_id": str(uuid4()),
        "project_name": "existing-m0",
        "billing_checked": True,
        "database_dsn": config.values["OPSPILOT_DATABASE_URL"],
    }
    return contract, config


class MemoryLedger:
    def __init__(self):
        self.claimed = False
        self.attempts = []
        self.saved = None

    def claim(self, contract):
        if self.claimed:
            raise ConfigError("ALREADY_CLAIMED")
        self.claimed = True

    def attempt(self, experiment_id, slot):
        assert self.claimed and slot not in self.attempts
        self.attempts.append(slot)

    def save(self, experiment_id, business, dto, usage):
        self.saved = (business, dto, usage)

    def trace_status(self, experiment_id, status):
        self.status = status


def scenario(contract, ledger, variant="normal"):
    seen, trace = [], []
    fixture = json.loads(FIXTURE.read_text())

    def respond(request):
        assert ledger.claimed
        seen.append(request)
        assert "x-tenant-id" not in request.headers
        if request.url.path.startswith("/sessions/"):
            return httpx2.Response(
                200,
                json={
                    "id": contract["project_id"],
                    "tenant_id": str(uuid4())
                    if variant == "wrong-workspace"
                    else contract["workspace_id"],
                    "name": contract["project_name"],
                },
            )
        if request.url.host == "api.deepseek.com":
            assert request.headers["authorization"] == "Bearer fake-model-auth"
            assert "x-api-key" not in request.headers
            body = json.loads(request.content)
            assert body["max_tokens"] == 4096 and body["thinking"] == {
                "type": "enabled"
            }
            second = len([r for r in seen if r.url.host == "api.deepseek.com"]) == 2
            if variant == "redirect":
                return httpx2.Response(
                    307, headers={"location": "https://evil.invalid"}
                )
            if variant == "large":
                return httpx2.Response(200, content=b"x" * 131073)
            if variant == "cancel":
                raise asyncio.CancelledError
            message = copy.deepcopy(fixture["assistant"])
            if second:
                assert (
                    body["messages"][-2]["reasoning_content"]
                    == fixture["assistant"]["reasoning_content"]
                )
                message = {
                    "role": "assistant",
                    "content": json.dumps(
                        {"target": "m0-target-a", "evidence_id": "m0-evidence-a"}
                    ),
                }
            elif variant == "wrong-tool":
                message["tool_calls"][0]["function"]["arguments"] = '{"target":"other"}'
            return httpx2.Response(
                200,
                json={
                    "model": (
                        "deepseek-v4.1-flash"
                        if variant == "wrong-model"
                        or (variant == "wrong-second-model" and second)
                        else None
                        if variant == "missing-model"
                        else "deepseek-v4-pro"
                    ),
                    "choices": [
                        {
                            "finish_reason": "stop" if second else "tool_calls",
                            "message": message,
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                },
            )
        assert request.headers["x-api-key"] == "fake-trace-auth"
        assert "authorization" not in request.headers
        if request.method == "POST":
            assert ledger.saved[0] == "completed"
            body = json.loads(request.content)
            for secret in (
                "SYNTHETIC_PRIVATE_PROTOCOL_SENTINEL",
                "fake-model-auth",
                "fake-trace-auth",
                "reasoning_content",
            ):
                assert secret not in request.content.decode()
            trace.append(body)
            return httpx2.Response(503 if variant == "trace-outage" else 200, json={})
        if variant == "read-404":
            return httpx2.Response(404, json={})
        returned = {
            "id": contract["run_id"],
            "session_id": contract["project_id"],
            "inputs": trace[0]["inputs"],
            "outputs": trace[0]["outputs"],
        }
        extras = {
            "server-metadata": {"metadata": {"ls_run_depth": 0}},
            "metadata-bool": {"metadata": {"ls_run_depth": False}},
            "metadata-depth": {"metadata": {"ls_run_depth": 1}},
            "metadata-private": {
                "metadata": {"ls_run_depth": 0, "reasoning_content": "private"}
            },
            "metadata-extra": {"metadata": {"ls_run_depth": 0}, "secret": "private"},
            "extra-false": False,
        }
        if variant in extras:
            returned["extra"] = extras[variant]
        if variant == "read-null":
            return httpx2.Response(200, content=b"null")
        if variant == "bad-read":
            returned["outputs"]["reasoning_content"] = "injected"
        return httpx2.Response(200, json=returned)

    return httpx2.MockTransport(respond), seen


@pytest.mark.parametrize(
    "variant, business, trace, models",
    [
        ("normal", "completed", "verified", 2),
        ("wrong-model", "failed", "pending", 1),
        ("wrong-second-model", "failed", "pending", 2),
        ("missing-model", "failed", "pending", 1),
        ("server-metadata", "completed", "verified", 2),
        ("metadata-bool", "completed", "unknown", 2),
        ("metadata-depth", "completed", "unknown", 2),
        ("metadata-private", "completed", "unknown", 2),
        ("metadata-extra", "completed", "unknown", 2),
        ("extra-false", "completed", "unknown", 2),
        ("read-null", "completed", "unknown", 2),
        ("wrong-workspace", "failed", "pending", 0),
        ("redirect", "failed", "pending", 1),
        ("large", "failed", "pending", 1),
        ("cancel", "failed", "pending", 1),
        ("wrong-tool", "failed", "pending", 1),
        ("trace-outage", "completed", "unknown", 2),
        ("read-404", "completed", "unknown", 2),
        ("bad-read", "completed", "unknown", 2),
    ],
)
def test_complete_boundary(variant, business, trace, models):
    contract, config = packet()
    ledger = MemoryLedger()
    transport, seen = scenario(contract, ledger, variant)
    with no_network():
        result = asyncio.run(execute(contract, config, ledger, transport=transport))
    assert result["business"] == business
    assert result["trace"] == trace
    if variant == "wrong-second-model":
        assert result["business_code"] == "LIVE_MODEL_PROFILE_MISMATCH"
        assert "trace-post" not in ledger.attempts
    if variant in ("wrong-model", "missing-model"):
        assert result["business_code"] == "LIVE_MODEL_PROFILE_MISMATCH"
        assert "model-2" not in ledger.attempts and "trace-post" not in ledger.attempts
    if variant == "read-null":
        assert ledger.attempts.count("trace-read-1") == 1
        assert "trace-read-2" not in ledger.attempts
        assert result["trace_code"] == "LIVE_TRACE_RESPONSE_INVALID"
    if variant.startswith("metadata-") or variant == "extra-false":
        assert result["trace_code"] == "TRACE_EXTRA_REJECTED"
    if variant == "server-metadata":
        assert result["trace_code"] == "TRACE_VERIFIED"
    assert len([r for r in seen if r.url.host == "api.deepseek.com"]) == models
    assert len(seen) <= 7 and result["actual_cost_cny"] is None
    assert ledger.saved[0] == business
    with no_network(), pytest.raises(ConfigError):
        asyncio.run(execute(contract, config, ledger, transport=transport))


@pytest.mark.parametrize(
    "field,value",
    [
        ("approved", False),
        ("billing_checked", False),
        ("budget_cny", "0"),
        ("budget_cny", "3.00"),
        ("code_sha256", "0" * 64),
        ("deepseek_key_sha256", "wrong"),
        ("endpoint", "https://evil.invalid"),
        ("deadline", "2026-01-01T00:00:00Z"),
        ("database_dsn", "host=production"),
        ("run_id", "not-uuid"),
    ],
)
def test_invalid_approval_never_claims(field, value):
    contract, config = packet()
    contract[field] = value
    ledger = MemoryLedger()
    with no_network(), pytest.raises(ConfigError):
        asyncio.run(execute(contract, config, ledger))
    assert not ledger.claimed


def test_approval_binding():
    contract, config = packet()
    assert validate(contract, config) > utcnow()
    config.values["LANGSMITH_WORKSPACE_ID"] = str(uuid4())
    with pytest.raises(ConfigError):
        validate(contract, config)


def test_deadline_rechecked_after_persistent_attempt():
    import time

    from scripts.m0.live import Wire

    contract, config = packet()
    ledger = MemoryLedger()
    ledger.claim(contract)
    ledger.attempt = lambda *args: time.sleep(0.06)
    seen = []

    async def run():
        wire = Wire(
            contract,
            config,
            ledger,
            utcnow() + timedelta(seconds=0.02),
            httpx2.MockTransport(lambda request: seen.append(request)),
        )
        try:
            with pytest.raises(ConfigError, match="LIVE_DEADLINE_OR_CANCELLED"):
                await wire.request(
                    "model-1", "POST", "https://api.deepseek.com/chat/completions", {}
                )
        finally:
            await wire.client.aclose()

    with no_network():
        asyncio.run(run())
    assert seen == []


@pytest.mark.parametrize(
    "profile",
    [
        None,
        {},
        MODEL_PROFILE | {"version_scope": "fixed_weights"},
        MODEL_PROFILE | {"accepted_response_model": "deepseek-v4-pro-0813"},
        MODEL_PROFILE | {"thinking": "disabled"},
    ],
)
def test_unapproved_or_unverifiable_profile_is_denied_before_claim(profile):
    contract, config = packet()
    contract["model_profile"] = profile
    ledger = MemoryLedger()
    with no_network(), pytest.raises(ConfigError):
        asyncio.run(execute(contract, config, ledger))
    assert not ledger.claimed


@pytest.mark.parametrize("include_profile", [True, False])
def test_legacy_approval_cannot_be_migrated_implicitly(include_profile):
    contract, config = packet()
    contract["version"] = "m0-normal-1-v1"
    if not include_profile:
        del contract["model_profile"]
    ledger = MemoryLedger()
    with no_network(), pytest.raises(ConfigError):
        asyncio.run(execute(contract, config, ledger))
    assert not ledger.claimed
