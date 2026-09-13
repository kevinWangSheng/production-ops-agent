"""One bounded M0-02 contract; no provider SDK or credentials in this module."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class Profile:
    allocation: str = "m0-02-20260910-convergence"
    model: str = "deepseek-v4-flash"
    reported_models: tuple[str, ...] = ("deepseek-v4-flash", "deepseek-flash")
    provider_models_response_sha256: str = (
        "0c5d2ba6ebb791e893b0e7efed32c64415a633ed774e8d89dad8e33c4d69ae77"
    )
    model_metadata_sha256: str = (
        "37a7903d6cc9ade127bd5d688953237d0c419de581a17351089bf62ee0eb900b"
    )
    model_metadata_at: str = "2026-09-10T02:28:19.494432+00:00"
    deadline: float = datetime.fromisoformat("2026-09-11T01:49:00+00:00").timestamp()
    context_tokens: int = 131072
    output_tokens: int = 32768
    input_tokens: int = 98304
    request_bytes: int = 524288
    response_bytes: int = 2097152
    request_seconds: int = 360
    run_seconds: int = 1800
    tool_seconds: int = 30
    tool_total_seconds: int = 240
    tool_queries: int = 20
    reservation_cny: float = 3.44064


PROFILE = Profile()
PHASES = {
    "report": (4, 2),
    "normal": (6, 7),
    "fault": (6, 7),
    "pg": (3.5, 4),
    "contingency": (0, 0),
}


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".pending")
    with temp.open("w") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def canonical_hash(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def envelope_check(raw):
    if len(raw) > PROFILE.request_bytes:
        raise ValueError("model request bytes denied")
    p = json.loads(raw)
    if p.get("model") != PROFILE.model:
        raise ValueError("model identity denied")
    if p.get("max_tokens") != PROFILE.output_tokens:
        raise ValueError("model output allowance denied")
    if p.get("thinking") != {"type": "enabled"} or p.get("reasoning_effort") != "high":
        raise ValueError("thinking profile not preserved")
    if p.get("stream"):
        raise ValueError("streaming denied")
    return p


def delivered_business(payload):
    # Allow-list roles and fields. Assistant protocol, including reasoning, is never exported.
    messages = [
        {k: m[k] for k in ("role", "content", "tool_call_id") if k in m}
        for m in payload.get("messages", [])
        if m.get("role") in {"user", "tool"}
    ]
    evidence = []

    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get("evidence_id"), str) and "tool" in value:
                evidence.append(
                    {
                        "evidence_id": value["evidence_id"],
                        "view_sha256": canonical_hash(value),
                        "query": value.get("query"),
                        "tool": value["tool"],
                    }
                )
            for item in value.values():
                if isinstance(item, (dict, list)):
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    decoder = json.JSONDecoder()
    for message in messages:
        content = message.get("content")
        if not isinstance(content, str):
            walk(content)
            continue
        cursor = 0
        while cursor < len(content):
            start = content.find("{", cursor)
            if start < 0:
                break
            try:
                value, end = decoder.raw_decode(content, start)
                walk(value)
                cursor = end
            except ValueError:
                cursor = start + 1
    return {
        "messages": messages,
        "business_messages_sha256": canonical_hash(messages),
        "evidence_views": evidence,
    }


class Budget:
    """Caller must hold the allocation OS lock for the full use of this object."""

    def __init__(self, path):
        self.path = Path(path)
        self.data = (
            json.loads(self.path.read_text())
            if self.path.exists()
            else {
                "allocation_id": PROFILE.allocation,
                "previous_allocation_reserved_cny": 24,
                "profile": asdict(PROFILE),
                "phase_limits": {
                    k: {"cny": v[0], "http": v[1]} for k, v in PHASES.items()
                },
                "attempts": [],
                "trace_uploads": 0,
            }
        )
        if self.data["allocation_id"] != PROFILE.allocation:
            raise ValueError("allocation mismatch")
        if self.data.get("previous_allocation_reserved_cny") != 24:
            raise ValueError("previous allocation reservation changed")

    def used(self, phase=None):
        return float(
            sum(
                (
                    Decimal(
                        str(
                            e["cost_upper_cny"]
                            if e.get("cost_upper_cny") is not None
                            else e["reservation_cny"]
                        )
                    )
                    for e in self.data["attempts"]
                    if phase is None or e["phase"] == phase
                ),
                Decimal(0),
            )
        )

    @staticmethod
    def _clock():
        return time.time()

    def reserve(self, run_id, phase, request_bytes):
        if self._clock() >= PROFILE.deadline:
            raise ValueError("allocation deadline")
        if self.data.get("blocked_reason") or any(
            e.get("usage_invalid") for e in self.data["attempts"]
        ):
            raise ValueError("invalid usage requires review")
        limits = self.data["phase_limits"][phase]
        if len(self.data["attempts"]) >= 20:
            raise ValueError("total HTTP budget")
        if sum(e["phase"] == phase for e in self.data["attempts"]) >= limits["http"]:
            raise ValueError("phase HTTP budget")
        # The trace half-CNY remains reserved and no uploads are enabled here.
        if Decimal(str(self.used())) + Decimal(str(PROFILE.reservation_cny)) > Decimal(
            "19.5"
        ):
            raise ValueError("total money budget")
        if Decimal(str(self.used(phase))) + Decimal(
            str(PROFILE.reservation_cny)
        ) > Decimal(str(limits["cny"])):
            raise ValueError("phase budget")
        e = {
            "run_id": run_id,
            "phase": phase,
            "ordinal": len(self.data["attempts"]) + 1,
            "started_at": self._clock(),
            "request_bytes": request_bytes,
            "status": "reserved",
            "reservation_cny": PROFILE.reservation_cny,
            "cost_upper_cny": None,
            "usage": None,
        }
        self.data["attempts"].append(e)
        save(self.path, self.data)
        return e

    def finish(self, entry, status, usage):
        entry.update(status=status, ended_at=self._clock())
        keys = (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens",
        )
        valid = isinstance(usage, dict) and all(
            type(usage.get(k)) is int and usage[k] >= 0 for k in keys
        )
        if valid:
            valid = (
                usage["prompt_tokens"] + usage["completion_tokens"]
                == usage["total_tokens"]
                and usage["prompt_cache_hit_tokens"] + usage["prompt_cache_miss_tokens"]
                == usage["prompt_tokens"]
                and usage["prompt_tokens"] <= 1048576
                and usage["completion_tokens"] <= PROFILE.output_tokens
            )
        if valid and status == 200:
            entry["usage"] = {k: usage[k] for k in keys}
            entry["cost_upper_cny"] = (
                usage["prompt_tokens"] * 3 + usage["completion_tokens"] * 9
            ) / 1_000_000
            entry["cost_basis"] = (
                "peak all-cache-miss upper bound; not invoice; old allocation unchanged"
            )
        elif status == 200 or usage is not None:
            entry["usage_invalid"] = True
            self.data["blocked_reason"] = (
                "invalid or above-bound provider usage requires review"
            )
        save(self.path, self.data)


def run_child(command, data, wall_seconds, grace_seconds=2):
    """Pipe-only private transport; child stays in parent's process group."""
    if wall_seconds <= 2 * grace_seconds:
        raise TimeoutError("child cleanup time unavailable")
    child = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        stdout, _ = child.communicate(data, timeout=wall_seconds - 2 * grace_seconds)
        if child.returncode:
            raise RuntimeError("transport child failed")
        return stdout
    except BaseException as original:
        if child.poll() is None:
            child.terminate()
            try:
                child.communicate(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                child.kill()
                try:
                    child.communicate(timeout=grace_seconds)
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError("child not reaped after kill") from exc
        if isinstance(original, subprocess.TimeoutExpired):
            raise TimeoutError("child wall deadline") from None
        raise


class ModelResponseDenied(RuntimeError):
    """Fixed public code, never contains a provider response or private field."""


def capture_response(
    out, run_id, ordinal, raw, http_status=200, complete=True, transport_error=None
):
    """Persist before validation; rejected output never becomes an accepted report."""
    import base64
    import re

    out = Path(out)
    private = out / "private-protocol"
    private.mkdir(mode=0o700, exist_ok=True)
    private.chmod(0o700)
    # Preserve exact bytes even if malformed JSON, with no credential-bearing headers.
    save(
        private / f"response-{ordinal}.json",
        {
            "run_id": run_id,
            "provider": "api.deepseek.com",
            "encoding": "base64",
            "response_bytes": base64.b64encode(raw).decode(),
            "http_status": http_status,
            "complete": complete,
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
    )
    business = {
        "run_id": run_id,
        "request_ordinal": ordinal,
        "http_status": http_status,
        "response_complete": complete,
        "requested_model": PROFILE.model,
        "response_model_allowlist": PROFILE.reported_models,
        "model_metadata_sha256": PROFILE.model_metadata_sha256,
        "provider_models_response_sha256": PROFILE.provider_models_response_sha256,
        "model_metadata_at": PROFILE.model_metadata_at,
        "identity_scope": "reported-name compatibility only; weights/version not pinned",
        "identity_accepted": False,
        "quality_assessment": "not_accepted",
        "response_sha256": hashlib.sha256(raw).hexdigest(),
    }
    if not complete or http_status != 200:
        code = (
            transport_error
            if transport_error
            in {"response byte limit exceeded", "response body interrupted"}
            else "HTTP response rejected"
        )
        business["boundary_code"] = code
        save(out / f"response-{ordinal}-business.json", business)
        raise ModelResponseDenied(code)
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("response is not an object")
    except (ValueError, UnicodeError):
        business["boundary_code"] = "model response JSON invalid"
        save(out / f"response-{ordinal}-business.json", business)
        raise ModelResponseDenied("model response JSON invalid") from None
    model = parsed.get("model")
    business["response_model"] = (
        model
        if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9/_:.-]{1,128}", model)
        else "invalid_or_absent"
    )
    observed_usage = parsed.get("usage")
    business["usage"] = (
        {
            k: v
            for k, v in observed_usage.items()
            if k
            in {
                "prompt_tokens",
                "completion_tokens",
                "total_tokens",
                "prompt_cache_hit_tokens",
                "prompt_cache_miss_tokens",
            }
            and type(v) is int
        }
        if isinstance(observed_usage, dict)
        else None
    )
    business["choices"] = []
    if isinstance(parsed.get("choices"), list):
        for choice in parsed["choices"]:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            finish = choice.get("finish_reason")
            business["choices"].append(
                {
                    "finish_reason": finish
                    if isinstance(finish, str)
                    and finish
                    in {
                        "stop",
                        "length",
                        "tool_calls",
                        "content_filter",
                        "insufficient_system_resource",
                    }
                    else "unknown",
                    "content": content if isinstance(content, str) else None,
                }
            )
    business["identity_accepted"] = (
        isinstance(model, str) and model in PROFILE.reported_models
    )
    if "error" in parsed:
        business["boundary_code"] = "provider error envelope"
    elif not business["identity_accepted"]:
        business["boundary_code"] = "response model identity mismatch"
    save(out / f"response-{ordinal}-business.json", business)
    if "boundary_code" in business:
        raise ModelResponseDenied(business["boundary_code"])
    return parsed


def source_scope(record, scope):
    """Authoritative source extraction from allowed backend formats, never question text."""
    envelope = record.get("data", {})
    if envelope.get("integration_id") != scope["integration_id"]:
        raise ValueError("returned integration outside scope")
    endpoint = record["tool"].removeprefix("otel_")
    backend = envelope.get("data", {})
    sources = set()
    if endpoint == "traces":
        for trace in backend.get("data", []):
            for process in trace.get("processes", {}).values():
                if isinstance(process.get("serviceName"), str):
                    sources.add(process["serviceName"])
    elif endpoint == "logs":
        for hit in backend.get("hits", {}).get("hits", []):
            service = hit.get("_source", {}).get("resource", {}).get("service.name")
            if isinstance(service, str):
                sources.add(service)
    elif endpoint == "metrics":
        if scope.get("metrics_scope") != "integration":
            raise ValueError("integration metric scope required")
        for row in backend.get("data", {}).get("result", []):
            labels = row.get("metric", {})
            for key in ("service_name", "service.name", "service"):
                if isinstance(labels.get(key), str):
                    sources.add(labels[key])
    elif endpoint == "services":
        # This endpoint describes the integration; its view is filtered separately.
        return {
            "level": "integration",
            "services": sorted(
                set(envelope.get("services", [])) & set(scope["services"])
            ),
        }
    if sources - set(scope["services"]):
        raise ValueError("returned service outside scope")
    return {
        "level": "service" if sources else "integration; service identity unknown",
        "services": sorted(sources),
    }


def save_observation(out, record, view, scope):
    out = Path(out)
    raw_path = out / (record["evidence_id"] + "-raw.json")
    view_path = out / (record["evidence_id"] + "-tool-model-view.json")
    save(raw_path, record)
    save(view_path, view)
    save(
        out / (record["evidence_id"] + "-manifest.json"),
        {
            "evidence_id": record["evidence_id"],
            "raw_sha256": canonical_hash(record),
            "view_sha256": canonical_hash(view),
            "canonical_hash_algorithm": "sha256(json.dumps ensure_ascii=False sort_keys=True UTF-8)",
            "raw_file_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            "view_file_sha256": hashlib.sha256(view_path.read_bytes()).hexdigest(),
            "projection_version": "m0-02-v2",
            "query": record.get("query"),
            "scope": scope,
            "actual_sources": view.get("actual_sources"),
            "error": view.get("error"),
        },
    )
