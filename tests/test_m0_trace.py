import copy
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from scripts.m0.contracts import RunContext
from scripts.m0.trace_adapter import TraceAdapter


class Backend:
    def __init__(self, fault=None):
        self.fault, self.payload, self.uploads = fault, None, 0

    def upload(self, trace_id, payload):
        self.uploads += 1
        if self.fault == "upload":
            raise OSError("SYNTHETIC_SECRET")
        self.payload = copy.deepcopy(payload)

    def read(self, trace_id):
        if self.fault == "read":
            raise OSError("SYNTHETIC_SECRET")
        data = copy.deepcopy(self.payload)
        if self.fault == "owner":
            data["run_id"] = str(uuid4())
        if self.fault == "pollution":
            data["metadata"] = {"reasoning_content": "SYNTHETIC_PRIVATE"}
        if self.fault == "type":
            data["attempt"] = True
        return data


def test_whitelist_upload_readback_and_retry():
    run = RunContext(
        uuid4(), uuid4(), "deepseek", datetime(2026, 9, 9, tzinfo=timezone.utc)
    )
    raw = {
        "run_id": str(run.run_id),
        "request_count": 2,
        "status": "completed",
        "api_key": "SYNTHETIC_SECRET",
        "reasoning_content": "SYNTHETIC_PRIVATE",
    }
    backend = Backend("upload")
    adapter = TraceAdapter(backend)
    assert adapter.export(run, raw).code == "TRACE_UPLOAD_FAILED"
    backend.fault = None
    assert adapter.export(run, raw).code == "TRACE_VERIFIED_SYNTHETIC"
    assert backend.uploads == 2
    assert backend.payload["experiment_id"] == str(run.experiment_id)
    assert (
        "api_key" not in backend.payload and "reasoning_content" not in backend.payload
    )
    assert raw["api_key"] == "SYNTHETIC_SECRET"


@pytest.mark.parametrize(
    "fault,code",
    [
        ("upload", "TRACE_UPLOAD_FAILED"),
        ("read", "TRACE_READ_FAILED"),
        ("owner", "TRACE_READ_INVALID"),
        ("pollution", "TRACE_READ_INVALID"),
        ("type", "TRACE_READ_INVALID"),
    ],
)
def test_trace_failures_are_fixed_and_separate(fault, code):
    run = RunContext(
        uuid4(), uuid4(), "deepseek", datetime(2026, 9, 9, tzinfo=timezone.utc)
    )
    assert (
        TraceAdapter(Backend(fault))
        .export(
            run, {"run_id": str(run.run_id), "request_count": 1, "status": "completed"}
        )
        .code
        == code
    )


def test_wrong_input_owner_does_not_upload():
    run = RunContext(
        uuid4(), uuid4(), "deepseek", datetime(2026, 9, 9, tzinfo=timezone.utc)
    )
    backend = Backend()
    assert (
        TraceAdapter(backend)
        .export(
            run, {"run_id": str(uuid4()), "request_count": 1, "status": "completed"}
        )
        .code
        == "TRACE_INPUT_INVALID"
    )
    assert backend.uploads == 0


def test_backend_cannot_open_network():
    import socket

    class NetworkBackend(Backend):
        def upload(self, trace_id, payload):
            socket.getaddrinfo("example.com", 443)

    run = RunContext(
        uuid4(), uuid4(), "deepseek", datetime(2026, 9, 9, tzinfo=timezone.utc)
    )
    assert (
        TraceAdapter(NetworkBackend())
        .export(
            run, {"run_id": str(run.run_id), "request_count": 1, "status": "completed"}
        )
        .code
        == "TRACE_UPLOAD_FAILED"
    )
