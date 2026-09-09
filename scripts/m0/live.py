"""One-shot normal-1 experiment. An approval record is required; never auto-approve."""

import asyncio
import copy
import hashlib
import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import httpx2
from langsmith import Client
from psycopg.types.json import Jsonb

from .budget import PostgresBudget
from .config import ConfigError, load_config
from .contracts import BudgetError
from .protocol import (
    FIXTURE,
    ROOT,
    CaptureSession,
    ProtocolError,
    continuation,
    tool_result,
    trace_dto,
)
from .runtime import check_runtime

ENDPOINTS = {"https://api.smith.langchain.com", "https://eu.api.smith.langchain.com"}
MODEL_PROFILE = {
    "request_model": "deepseek-v4-flash",
    "accepted_response_model": "deepseek-v4-flash",
    "version_scope": "reported_alias",
    "thinking": "enabled",
    "reasoning_effort": "high",
}

SLOTS = (
    "project",
    "model-1",
    "model-2",
    "trace-post",
    "trace-read-1",
    "trace-read-2",
    "trace-read-3",
)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def code_digest():
    paths = [
        ROOT / "uv.lock",
        ROOT / "pyproject.toml",
        FIXTURE,
        *sorted((ROOT / "scripts/m0").glob("*.py")),
        ROOT / "scripts/m0/live.sql",
    ]
    return digest(
        b"".join(
            str(p.relative_to(ROOT)).encode() + b"\0" + p.read_bytes() for p in paths
        )
    )


def utcnow():
    return datetime.now(timezone.utc)


def denied():
    raise ConfigError("LIVE_CONTRACT_DENIED")


def validate(contract, config, now=None):
    """No network. Private file records human approval, not an adversarial OS boundary."""
    now = now or utcnow()
    v = config.values
    try:
        required = {
            "version",
            "model_profile",
            "runtime",
            "approved",
            "approval_ref",
            "experiment_id",
            "run_id",
            "deadline",
            "budget_cny",
            "code_sha256",
            "deepseek_key_sha256",
            "langsmith_key_sha256",
            "endpoint",
            "workspace_id",
            "project_id",
            "project_name",
            "billing_checked",
            "database_dsn",
        }
        if (
            set(contract) != required
            or contract["approved"] is not True
            or contract["billing_checked"] is not True
        ):
            denied()
        if contract["version"] != "m0-normal-1-v2" or contract["budget_cny"] != "2.00":
            denied()
        # The current endpoint cannot attest immutable backend weights.
        # A fixed-weight approval is unsupported, never silently downgraded to an alias.
        if contract["model_profile"] != MODEL_PROFILE:
            denied()
        for key in ("experiment_id", "run_id", "workspace_id", "project_id"):
            if str(UUID(contract[key])) != contract[key]:
                denied()
        if (
            not isinstance(contract["approval_ref"], str)
            or not 8 <= len(contract["approval_ref"]) <= 200
        ):
            denied()
        deadline = datetime.fromisoformat(contract["deadline"])
        if deadline.utcoffset() is None or not now < deadline <= now + timedelta(
            days=2
        ):
            denied()
        check_runtime(contract["runtime"])
        if contract["code_sha256"] != code_digest():
            denied()
        for name, key in (
            ("deepseek", "DEEPSEEK_API_KEY"),
            ("langsmith", "LANGSMITH_API_KEY"),
        ):
            if not v.get(key) or contract[name + "_key_sha256"] != digest(
                v[key].encode()
            ):
                denied()
        if contract["endpoint"] not in ENDPOINTS or contract["endpoint"] != v.get(
            "LANGSMITH_ENDPOINT"
        ):
            denied()
        if (
            contract["project_name"] != v.get("LANGSMITH_PROJECT")
            or not contract["project_name"]
        ):
            denied()
        # Omit workspace header: key's default must be the approved existing tenant.
        if v.get("LANGSMITH_WORKSPACE_ID", "") not in ("", contract["workspace_id"]):
            denied()
        if (
            v.get("OPSPILOT_EXPERIMENT_BUDGET_CNY") != "2.00"
            or v.get("OPSPILOT_EXPERIMENT_DEADLINE_UTC") != contract["deadline"]
        ):
            denied()
        # This experiment uses only its explicit isolated local database, never a target DSN.
        if (
            contract["database_dsn"]
            != "host=127.0.0.1 port=55431 dbname=m0_budget user=m0_lab"
        ):
            denied()
        if v.get("OPSPILOT_DATABASE_URL") != contract["database_dsn"]:
            denied()
    except (KeyError, TypeError, ValueError, AttributeError):
        denied()
    return deadline


def read_contract(path):
    if not path.is_absolute():
        denied()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_size > 16384
            ):
                denied()
            return json.loads(stream.read(16385))
    except (OSError, ValueError, UnicodeError):
        denied()


class LiveLedger(PostgresBudget):
    """Dedicated one-shot authorization claim; no automatic refund or model recovery."""

    def install_live(self):
        with self._transaction() as conn:
            conn.execute(Path(__file__).with_suffix(".sql").read_text())

    def claim(self, contract):
        with self._transaction() as conn:
            row = conn.execute(
                "INSERT INTO m0_live_once (experiment_id,run_id,approval_hash,contract_hash,deadline) SELECT %s,%s,%s,%s,%s WHERE clock_timestamp()<%s ON CONFLICT DO NOTHING RETURNING experiment_id",
                (
                    contract["experiment_id"],
                    contract["run_id"],
                    digest(contract["approval_ref"].encode()),
                    digest(json.dumps(contract, sort_keys=True).encode()),
                    contract["deadline"],
                    contract["deadline"],
                ),
            ).fetchone()
            if row is None:
                raise ConfigError("LIVE_ALREADY_CLAIMED_OR_EXPIRED")

    def attempt(self, experiment_id, slot):
        if slot not in SLOTS:
            denied()
        with self._transaction() as conn:
            row = conn.execute(
                "UPDATE m0_live_once SET attempts=array_append(attempts,%s) WHERE experiment_id=%s AND NOT %s=ANY(attempts) AND clock_timestamp()<deadline RETURNING experiment_id",
                (slot, experiment_id, slot),
            ).fetchone()
            if row is None:
                raise ConfigError("LIVE_ATTEMPT_DENIED")

    def save(self, experiment_id, business, dto, usage, business_code):
        with self._transaction() as conn:
            conn.execute(
                "UPDATE m0_live_once SET business=%s,outbox=%s,usage=%s WHERE experiment_id=%s",
                (business, Jsonb(dto), Jsonb(usage), experiment_id),
            )
            conn.execute(
                "INSERT INTO m0_live_diagnostics(experiment_id,business_code,trace_code) VALUES(%s,%s,'TRACE_NOT_ATTEMPTED') ON CONFLICT(experiment_id) DO UPDATE SET business_code=EXCLUDED.business_code",
                (experiment_id, business_code),
            )

    def trace_status(self, experiment_id, status, code):
        with self._transaction() as conn:
            conn.execute(
                "UPDATE m0_live_once SET trace_status=%s WHERE experiment_id=%s",
                (status, experiment_id),
            )
            conn.execute(
                "INSERT INTO m0_live_diagnostics(experiment_id,trace_code) VALUES(%s,%s) ON CONFLICT(experiment_id) DO UPDATE SET trace_code=EXCLUDED.trace_code",
                (experiment_id, code),
            )


class Wire:
    def __init__(self, contract, config, ledger, deadline, transport=None):
        self.contract, self.config, self.ledger, self.deadline = (
            contract,
            config,
            ledger,
            deadline,
        )
        self.model_count = 0
        self.client = httpx2.AsyncClient(
            transport=transport, trust_env=False, follow_redirects=False
        )

    async def request(self, slot, method, url, body=None):
        c, v = self.contract, self.config.values
        trace_url = c["endpoint"]
        routes = {
            "project": ("GET", f"{trace_url}/sessions/{c['project_id']}"),
            "model-1": ("POST", "https://api.deepseek.com/chat/completions"),
            "model-2": ("POST", "https://api.deepseek.com/chat/completions"),
            "trace-post": ("POST", f"{trace_url}/runs"),
            **{
                f"trace-read-{i}": ("GET", f"{trace_url}/runs/{c['run_id']}")
                for i in range(1, 4)
            },
        }
        if routes.get(slot) != (method, url):
            denied()
        content = (
            None if body is None else json.dumps(body, ensure_ascii=False).encode()
        )
        if content is not None and len(content) > 16384:
            raise ConfigError("LIVE_REQUEST_TOO_LARGE")
        remaining = (self.deadline - utcnow()).total_seconds()
        if remaining <= 0:
            raise ConfigError("LIVE_DEADLINE")
        self.ledger.attempt(c["experiment_id"], slot)
        remaining = (self.deadline - utcnow()).total_seconds()
        if remaining <= 0 or asyncio.current_task().cancelling():
            raise ConfigError("LIVE_DEADLINE_OR_CANCELLED")
        key = "DEEPSEEK_API_KEY" if slot.startswith("model-") else "LANGSMITH_API_KEY"
        headers = {"Content-Type": "application/json"}
        headers.update(
            {"Authorization": "Bearer " + v[key]}
            if key == "DEEPSEEK_API_KEY"
            else {"x-api-key": v[key]}
        )
        # Whole response deadline and cap, including body streaming; never surface raw errors.
        if slot.startswith("model-"):
            self.model_count += 1
        async with asyncio.timeout(
            min(180 if slot.startswith("model-") else 10, remaining)
        ):
            async with self.client.stream(
                method,
                url,
                content=content,
                headers=headers,
                timeout=min(180, remaining),
            ) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 131072:
                        raise ConfigError("LIVE_RESPONSE_TOO_LARGE")
                if response.status_code == 404 and slot.startswith("trace-read-"):
                    return None
                if response.status_code < 200 or response.status_code >= 300:
                    if response.status_code in (401, 403):
                        raise ConfigError("LIVE_AUTH_FAILED")
                    if response.status_code == 429:
                        raise ConfigError("LIVE_RATE_LIMITED")
                    raise ConfigError("LIVE_HTTP_FAILED")
                if slot == "trace-post":
                    return {}
                data = json.loads(raw)
                if slot.startswith("trace-read-") and type(data) is not dict:
                    raise ConfigError("LIVE_TRACE_RESPONSE_INVALID")
                return data


def trace_wire(contract, dto):
    """Use pinned LangSmith SDK serialization into an in-memory sink, then allowlist."""
    with CaptureSession() as session:
        client = Client(
            api_url="https://trace.invalid",
            api_key="synthetic-trace-auth",
            session=session,
            auto_batch_tracing=False,
            omit_traced_runtime_info=True,
            tracing_mode="langsmith",
            tracing_sampling_rate=1,
            timeout_ms=1000,
            info={},
        )
        try:
            now = utcnow()
            client.create_run(
                name="m0-normal-1",
                run_type="chain",
                project_name=contract["project_name"],
                id=contract["run_id"],
                inputs={"fixture": "m0-protocol-v1"},
                outputs=dto,
                start_time=now,
                end_time=now,
                session_id=contract["project_id"],
            )
        finally:
            client.close()
        if len(session.bodies) != 1:
            denied()
        result = session.bodies[0]
        allowed = {
            "id",
            "name",
            "run_type",
            "inputs",
            "outputs",
            "start_time",
            "end_time",
            "session_name",
            "session_id",
            "extra",
            "tags",
        }
        if (
            set(result) - allowed
            or result.get("extra")
            or result.get("tags")
            or result.get("outputs") != dto
        ):
            denied()
        return result


def trace_readback_code(returned, contract, dto):
    """Validate exported data exactly; accept only the observed root-depth decoration."""
    if type(returned) is not dict:
        return "TRACE_RESPONSE_INVALID"
    if (
        returned.get("id") != contract["run_id"]
        or returned.get("session_id") != contract["project_id"]
    ):
        return "TRACE_IDENTITY_MISMATCH"
    if returned.get("inputs") != {"fixture": "m0-protocol-v1"}:
        return "TRACE_INPUTS_MISMATCH"
    outputs = returned.get("outputs")
    if (
        type(outputs) is not dict
        or outputs != dto
        or any(type(outputs[k]) is not type(v) for k, v in dto.items())
    ):
        return "TRACE_OUTPUTS_MISMATCH"
    extra = returned.get("extra")
    if extra is not None and extra != {}:
        if type(extra) is not dict or set(extra) != {"metadata"}:
            return "TRACE_EXTRA_REJECTED"
        metadata = extra["metadata"]
        if (
            type(metadata) is not dict
            or set(metadata) != {"ls_run_depth"}
            or type(metadata["ls_run_depth"]) is not int
            or metadata["ls_run_depth"] != 0
        ):
            return "TRACE_EXTRA_REJECTED"
    return "TRACE_VERIFIED"


def failure_code(exc):
    """Preserve known failure categories without rendering provider exception prose."""
    if isinstance(exc, asyncio.CancelledError):
        return "LIVE_CANCELLED"
    if isinstance(exc, (TimeoutError, httpx2.TimeoutException)):
        return "LIVE_TIMEOUT"
    if isinstance(exc, httpx2.TransportError):
        return "LIVE_TRANSPORT_FAILED"
    if isinstance(exc, json.JSONDecodeError):
        return "LIVE_RESPONSE_INVALID"
    if isinstance(exc, ProtocolError):
        return "LIVE_PROTOCOL_FAILED"
    if isinstance(exc, BudgetError):
        return (
            "LIVE_STORAGE_UNAVAILABLE"
            if str(exc) == "STORAGE_UNAVAILABLE"
            else "LIVE_BUDGET_DENIED"
        )
    codes = {
        "LIVE_TRACE_RESPONSE_INVALID",
        "LIVE_PROJECT_RESPONSE_INVALID",
        "LIVE_HTTP_FAILED",
        "LIVE_AUTH_FAILED",
        "LIVE_RATE_LIMITED",
        "LIVE_RESPONSE_TOO_LARGE",
        "LIVE_REQUEST_TOO_LARGE",
        "LIVE_DEADLINE",
        "LIVE_DEADLINE_OR_CANCELLED",
        "LIVE_ATTEMPT_DENIED",
        "LIVE_ACCOUNT_MISMATCH",
        "LIVE_MODEL_PROFILE_MISMATCH",
        "LIVE_PROTOCOL_FAILED",
    }
    if isinstance(exc, ConfigError) and str(exc) in codes:
        return str(exc)
    return "LIVE_OPERATION_FAILED"


def model_message(response, finish):
    try:
        if (
            type(response) is not dict
            or type(response.get("choices")) is not list
            or len(response["choices"]) != 1
        ):
            raise ValueError
        choice = response["choices"][0]
        if type(choice) is not dict or choice.get("finish_reason") != finish:
            raise ValueError
        message = choice["message"]
        if type(message) is not dict or message.get("role") != "assistant":
            raise ValueError
        content = message["content"]
        if content is not None and type(content) is not str:
            raise ValueError
        calls = message.get("tool_calls")
        if finish == "tool_calls":
            if type(calls) is not list or len(calls) != 1:
                raise ValueError
        elif type(content) is not str or (calls is not None and calls != []):
            raise ValueError
        return message
    except (KeyError, TypeError, ValueError, IndexError):
        raise ConfigError("LIVE_PROTOCOL_FAILED") from None


def token_usage(response):
    usage = response.get("usage", {})
    if type(usage) is not dict:
        usage = {}
    reported = response.get("model")
    known = {
        "deepseek-v4-flash",
        "deepseek-v4-flash-0731",
        "DeepSeek-V4-Flash-0731",
        "deepseek-v4-pro",
        "deepseek-v4-pro-0813",
        "DeepSeek-V4-Pro-0813",
        "deepseek-v4.1-flash",
        "DeepSeek-V4.1-Flash",
    }
    result = (
        {
            "reported_model": reported
            if reported in known
            else "unreported_or_unrecognized"
        }
        if isinstance(reported, str)
        else {"reported_model": "unreported_or_unrecognized"}
    )
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if type(value) is int and 0 <= value <= 1000000:
            result[key] = value
    return result


async def execute(contract, config, ledger, *, transport=None):
    deadline = min(validate(contract, config), utcnow() + timedelta(seconds=420))
    ledger.claim(
        contract
    )  # Commit succeeds before ANY external request, including project GET.
    wire = Wire(contract, config, ledger, deadline, transport)
    business, trace, count, usage = "failed", "pending", 0, []
    business_code = "LIVE_PROTOCOL_FAILED"
    profile = contract["model_profile"]
    try:
        async with asyncio.timeout(max(0.001, (deadline - utcnow()).total_seconds())):
            project = await wire.request(
                "project",
                "GET",
                f"{contract['endpoint']}/sessions/{contract['project_id']}",
            )
            if type(project) is not dict:
                raise ConfigError("LIVE_PROJECT_RESPONSE_INVALID")
            if (project.get("id"), project.get("tenant_id"), project.get("name")) != (
                contract["project_id"],
                contract["workspace_id"],
                contract["project_name"],
            ):
                raise ConfigError("LIVE_ACCOUNT_MISMATCH")
            fixture = json.loads(FIXTURE.read_text())
            messages = copy.deepcopy(fixture["messages"])
            for step in (1, 2):
                body = {
                    "model": profile["request_model"],
                    "messages": messages,
                    "tools": fixture["tools"],
                    "max_tokens": 4096,
                    "reasoning_effort": profile["reasoning_effort"],
                    "thinking": {"type": profile["thinking"]},
                    "stream": False,
                }
                response = await wire.request(
                    f"model-{step}",
                    "POST",
                    "https://api.deepseek.com/chat/completions",
                    body,
                )
                message = model_message(response, "tool_calls" if step == 1 else "stop")
                usage.append(token_usage(response))
                if response.get("model") != profile["accepted_response_model"]:
                    business_code = "LIVE_MODEL_PROFILE_MISMATCH"
                    raise ConfigError(business_code)
                if step == 1:
                    if len(message["tool_calls"]) != 1:
                        raise ConfigError("LIVE_PROTOCOL_FAILED")
                    result = tool_result(message["tool_calls"][0], fixture)
                    messages += continuation(
                        message,
                        [result],
                        provider="deepseek",
                        run_id=contract["run_id"],
                        expected_run_id=contract["run_id"],
                    )
                else:
                    try:
                        valid = json.loads(message["content"]) == {
                            "target": "m0-target-a",
                            "evidence_id": "m0-evidence-a",
                        }
                    except (TypeError, ValueError):
                        valid = False
                    if not valid:
                        raise ConfigError("LIVE_PROTOCOL_FAILED")
            business = "completed"
            business_code = "LIVE_PROTOCOL_COMPLETED"
    except (Exception, asyncio.CancelledError) as exc:
        business_code = failure_code(exc)
    count = wire.model_count
    dto = trace_dto(
        {"run_id": contract["run_id"], "request_count": count, "status": business}
    )
    dto.update(
        experiment_id=contract["experiment_id"],
        adapter="m0-normal-1-v1",
        code_sha256=contract["code_sha256"],
    )
    trace_code = "TRACE_NOT_ATTEMPTED"
    try:
        ledger.save(contract["experiment_id"], business, dto, usage, business_code)
        # Failed or cancelled model chain does not gain further upload authority in this slice.
        if business == "completed":
            body = trace_wire(contract, dto)
            ledger.trace_status(
                contract["experiment_id"], "unknown", "TRACE_EXPORT_STARTED"
            )
            await wire.request(
                "trace-post", "POST", f"{contract['endpoint']}/runs", body
            )
            trace = "unknown"
            for i in range(1, 4):
                returned = await wire.request(
                    f"trace-read-{i}",
                    "GET",
                    f"{contract['endpoint']}/runs/{contract['run_id']}",
                )
                if returned is None:
                    trace_code = "TRACE_NOT_VISIBLE"
                    continue
                trace_code = trace_readback_code(returned, contract, dto)
                if trace_code == "TRACE_VERIFIED":
                    trace = "verified"
                break
        ledger.trace_status(contract["experiment_id"], trace, trace_code)
    except (Exception, asyncio.CancelledError) as exc:
        trace_code = failure_code(exc)
        trace = "unknown"  # Committed business/outbox survives exporter failure.
        try:
            ledger.trace_status(contract["experiment_id"], trace, trace_code)
        except (Exception, asyncio.CancelledError):
            pass  # Unavailable storage cannot be certified as having saved diagnostics.
    finally:
        await wire.client.aclose()
    return {
        "business": business,
        "business_code": business_code,
        "trace": trace,
        "trace_code": trace_code,
        "request_count": count,
        "unreconciled_reserved_cny": "2.00",
        "actual_cost_cny": None,
    }


def run_cli(env_file, approval_file):
    try:
        config = load_config(env_file)
        contract = read_contract(approval_file)
        validate(contract, config)
        # Explicit setup belongs to the local engineer; live never installs/migrates schema.
        from .postgres_lab import verify_server

        verify_server()
        ledger = LiveLedger(contract["database_dsn"])
        result = asyncio.run(execute(contract, config, ledger))
        print(json.dumps(result, sort_keys=True))
        return (
            0
            if result["business"] == "completed" and result["trace"] == "verified"
            else 1
        )
    except (Exception, KeyboardInterrupt):
        print(
            '{"status":"denied_or_failed","reason":"LIVE_STOPPED_CHECK_LOCAL_LEDGER"}'
        )
        return 3
