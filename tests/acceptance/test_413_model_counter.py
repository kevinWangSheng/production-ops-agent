"""Acceptance tests for the worker model-call counter seam."""

from __future__ import annotations

import importlib

import pytest


def _health():
    try:
        return importlib.import_module("opspilot.health")
    except ModuleNotFoundError as exc:
        pytest.fail(f"#413 public health module is missing: {exc}")


def test_model_call_counter_passthrough_categories_and_failure_reset():
    health = _health()
    from opspilot.investigation import ModelError

    class Client:
        def complete(self, *args, **kwargs):
            return (args, kwargs)

    client = health.ModelCallCounter(Client())
    request = object()
    result = client.complete(request)
    assert result == ((request,), {})
    assert client.snapshot() == {
        "calls": {"ok": 1},
        "consecutive_failures": 0,
        "last_category": "ok",
    }

    class Failing:
        def __init__(self):
            self.fail = True

        def complete(self, *args, **kwargs):
            if self.fail:
                raise ModelError("MODEL_UNAVAILABLE")
            return "success"

    wrapped = Failing()
    counter = health.ModelCallCounter(wrapped)
    with pytest.raises(ModelError) as raised:
        counter.complete("x")
    assert raised.value.code == "MODEL_UNAVAILABLE"
    with pytest.raises(ModelError):
        counter.complete("x")
    assert counter.snapshot()["consecutive_failures"] == 2
    wrapped.fail = False
    assert counter.complete("x") == "success"
    snapshot = counter.snapshot()
    assert snapshot["calls"] == {"MODEL_UNAVAILABLE": 2, "ok": 1}
    assert snapshot["consecutive_failures"] == 0
    assert snapshot["last_category"] == "ok"
