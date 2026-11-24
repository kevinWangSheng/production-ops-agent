"""One real ``otel-demo-ext`` tool gateway with every source faked at urllib.

The safety suite drives the shipped profile through its public factory
(``select_profile`` -> ``executor_factory`` -> ``ReadOnlyToolExecutor``) and
replaces only ``urllib.request.build_opener``: every Prometheus, Jaeger and
Kubernetes request any tool would send is captured here, so "refused before
dispatch" is observable as "no request left the gateway".
"""

from __future__ import annotations

import io
import json
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

from opspilot.persistence import Lease
from opspilot.tools import ReadOnlyToolExecutor, ToolRequest
from opspilot.tools.otel_demo import TARGET_ID
from opspilot.tools.outcomes import Window
from opspilot.tools.profiles import PROFILE_ENV, select_profile
from tests.m1_tool_support import FakeClock, RecordingSink

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
#: Inside the Run's authorized frame (the face frames the Run at ``NOW``).
WINDOW = Window(NOW - timedelta(hours=1), NOW - timedelta(minutes=1))
#: Ends after the authorized frame: no tool may read it.
OUTSIDE = Window(NOW + timedelta(minutes=10), NOW + timedelta(hours=1))
K8S_API = "https://127.0.0.1:16443"
NAMESPACE = "otel-demo"
#: Low-entropy synthetic credentials configured into the gateway.
K8S_TOKEN = "k8s-safety-aaaaaaaa"
OTEL_TOKEN = "otel-safety-aaaaaaaa"
ES_TOKEN = "es-safety-aaaaaaaa"


class _Reply:
    def __init__(self, body: bytes, status: int = 200):
        self._stream = io.BytesIO(body)
        self.status = status
        self.headers: dict[str, str] = {}

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@dataclass
class SourceDouble:
    """Every outbound request, answered with ``body`` (bytes or a callable)."""

    body: Any = b"{}"
    requests: list[str] = field(default_factory=list)

    def open(self, request, timeout=None):
        url = (
            request.full_url
            if isinstance(request, urllib.request.Request)
            else str(request)
        )
        self.requests.append(url)
        body = self.body(urlsplit(url)) if callable(self.body) else self.body
        if isinstance(body, tuple):
            status, payload = body
            raise urllib.error.HTTPError(
                url, status, "synthetic", {}, io.BytesIO(payload)
            )
        return _Reply(body)


class _Store:
    """The three ``DurableStore`` calls the executor wiring makes."""

    def __init__(self, lease: Lease):
        self.lease = lease
        #: What a human decision changes: tests flip these mid-call.
        self.suspended = False
        self.generation_bump = 0

    def rebuild(self, incident_id):
        return {
            "run": {
                "run_id": self.lease.run_id,
                "deadline": NOW + timedelta(minutes=10),
                "tool_operations_used": 0,
                "tool_seconds_used": 0.0,
            }
        }

    def charge_tool(self, lease, operation_id, seconds, **kwargs):
        return None

    def control_state(self, incident_id):
        return {
            "incident_generation": self.lease.control_generation + self.generation_bump,
            "incident_state": "running",
            "global_suspended": self.suspended,
            "global_generation": self.lease.global_suspension_generation,
            "target_suspended": False,
            "target_generation": self.lease.target_suspension_generation,
        }


def ext_env(ca_file: str) -> dict[str, str]:
    return {
        PROFILE_ENV: "otel-demo-ext",
        "OPSPILOT_K8S_API_URL": K8S_API,
        "OPSPILOT_K8S_TOKEN": K8S_TOKEN,
        "OPSPILOT_K8S_CA_FILE": ca_file,
        "OPSPILOT_K8S_NAMESPACE": NAMESPACE,
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_OTEL_TOKEN": OTEL_TOKEN,
        # #560: the Elasticsearch log tools register only with an address.
        "OPSPILOT_ES_URL": "http://127.0.0.1:19200",
        "OPSPILOT_ES_API_KEY": ES_TOKEN,
    }


@dataclass
class Gateway:
    executor: ReadOnlyToolExecutor
    source: SourceDouble
    evidence: RecordingSink
    control: _Store

    def call(
        self,
        tool: str,
        params: dict[str, Any],
        *,
        window: Window = WINDOW,
        target: str = TARGET_ID,
        index: int = 0,
    ):
        return self.executor.execute(
            ToolRequest(
                step_id="safety-step",
                tool_index=index,
                tool_name=tool,
                target_ref=target,
                params=params,
                window=window.as_json(),
            )
        )


def gateway(monkeypatch, tmp_path, body: Any = b"{}") -> Gateway:
    if shutil.which("jq") is None:
        pytest.skip("jq is a startup precondition of the otel-demo-ext worker")
    ca_file = tmp_path / "synthetic-ca.pem"
    ca_file.write_text("synthetic ca placeholder\n")
    profile = select_profile(ext_env(str(ca_file)))
    lease = Lease(
        incident_id=uuid4(),
        run_id=uuid4(),
        owner=uuid4(),
        epoch=1,
        control_generation=3,
        global_suspension_generation=1,
        target_suspension_generation=2,
    )
    clock = FakeClock(start=NOW)
    source = SourceDouble(body)
    evidence = RecordingSink()
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: source)
    store = _Store(lease)
    factory = profile.executor_factory(store, evidence, clock)
    run_input = profile.face(clock).input_for(
        run_id=str(lease.run_id),
        question="Why is checkout degraded?",
        target_id=TARGET_ID,
        deadline=NOW + timedelta(minutes=10),
        model_requests=8,
    )
    return Gateway(factory(lease, run_input), source, evidence, store)


def everything_seen(outcome, evidence: RecordingSink) -> str:
    """Every byte a tool call left for the model or the evidence store."""
    parts = [json.dumps(outcome.model_view, default=str, sort_keys=True)]
    parts.append(repr(outcome))
    parts.append(repr(getattr(evidence, "records", evidence.__dict__)))
    return "\n".join(parts)
