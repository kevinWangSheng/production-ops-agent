"""Whitelisted trace upload/readback seam; injected local backend only in this batch."""

import copy
from dataclasses import dataclass
from typing import Protocol

from scripts.m0.contracts import RunContext
from scripts.m0.protocol import ProtocolError, no_network, trace_dto


class TraceBackend(Protocol):
    def upload(self, trace_id: str, payload: dict) -> None: ...
    def read(self, trace_id: str) -> dict: ...


@dataclass(frozen=True)
class TraceResult:
    status: str
    code: str


class TraceAdapter:
    def __init__(self, backend: TraceBackend):
        self.backend = backend

    def export(self, run: RunContext, raw: dict) -> TraceResult:
        try:
            dto = trace_dto(raw)
            if dto["run_id"] != str(run.run_id):
                raise ProtocolError("TRACE_OWNER_INVALID")
            dto["experiment_id"] = str(run.experiment_id)
            dto["adapter"] = "m0-synthetic-stream-v1"
        except Exception:
            return TraceResult("failed", "TRACE_INPUT_INVALID")
        try:
            with no_network():
                self.backend.upload(str(run.run_id), copy.deepcopy(dto))
        except Exception:
            return TraceResult("failed", "TRACE_UPLOAD_FAILED")
        try:
            with no_network():
                returned = self.backend.read(str(run.run_id))
        except Exception:
            return TraceResult("failed", "TRACE_READ_FAILED")
        # Exact types, fields and attribution: even unknown nested fields fail closed.
        if (
            type(returned) is not dict
            or returned != dto
            or any(type(returned[k]) is not type(v) for k, v in dto.items())
        ):
            return TraceResult("failed", "TRACE_READ_INVALID")
        return TraceResult("completed", "TRACE_VERIFIED_SYNTHETIC")
