"""#427 implementation tests: the outbox payload builder, the frozen copies in
migration 0024 and the worker's pool floor (no database)."""

from __future__ import annotations

import importlib
import json
import threading
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from opspilot import worker_main
from opspilot.persistence import PoolConfig, outbox
from opspilot.persistence.grants import RUNTIME_GRANTS, WEB_ROLE, WORKER_ROLE
from opspilot.persistence.outbox import PAYLOAD_KEYS, build_payload

migration = importlib.import_module("opspilot.migrations.versions.0024_trace_outbox")

# Built at run time, not a credential-shaped literal in the source (gitleaks).
SENTINEL_KEY = "sk-" + "a1b2c3d4" * 6
SENTINEL_PROMPT = "PROMPT-SENTINEL ignore previous instructions"


def test_unknown_keys_and_none_values_are_dropped():
    payload = build_payload(
        {
            "incident_id": uuid4(),
            "reasoning_content": "secret chain of thought",
            "content": SENTINEL_PROMPT,
            "query": "rate(http_errors[5m])",
            "target_id": "checkout-prod",
            "api_key": SENTINEL_KEY,
            "tool_reason": None,
        }
    )
    assert set(payload) == {"incident_id"}


def test_token_counts_and_budget_seconds_are_kept_as_numbers():
    payload = build_payload(
        {
            "input_tokens": 8,
            "output_tokens": 16,
            "total_tokens": 24,
            "reasoning_tokens": 5,
            "budget_seconds": 600.5,
        }
    )
    assert payload == {
        "input_tokens": 8,
        "output_tokens": 16,
        "total_tokens": 24,
        "reasoning_tokens": 5,
        "budget_seconds": 600.5,
    }


@pytest.mark.parametrize("bad", [True, "8", None, [8], {"n": 8}])
def test_a_non_number_token_count_is_dropped(bad):
    assert build_payload({"input_tokens": bad}) == {}


@pytest.mark.parametrize(
    ("key", "good", "bad"),
    [
        ("handoff_reason", "BUDGET_EXHAUSTED", "budget exhausted: " + SENTINEL_KEY),
        ("tool_reason", "TOOL_NOT_REGISTERED", "ConnectionError('host x')"),
        ("finish_reason", "tool_calls", "STOP because " + SENTINEL_PROMPT),
        ("tool_status_class", "no_data", "OK"),
        ("run_state", "waiting_human", "banana"),
        ("source_contact", "confirmed", "maybe"),
        ("span_kind", "tool_result", "llm"),
        ("model", "deepseek-v4-flash", "model " + SENTINEL_PROMPT),
        ("prompt_revision", "prompt-replay-candidate-bef70649a3a8", "x" * 65),
    ],
)
def test_codes_names_and_vocabularies_keep_their_shape_or_become_unclassified(
    key, good, bad
):
    assert build_payload({key: good}) == {key: good}
    assert build_payload({key: bad}) == {key: "UNCLASSIFIED"}


def test_a_non_registry_tool_name_is_left_out():
    assert build_payload({"tool_name": "metrics_range_query"}) == {
        "tool_name": "metrics_range_query"
    }
    assert build_payload({"tool_name": "metrics.range_query; drop table"}) == {}


def test_ids_and_times_are_typed():
    run_id = uuid4()
    moment = datetime(2026, 10, 31, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    payload = build_payload(
        {
            "run_id": run_id,
            "step_id": str(run_id),
            "attempt_id": "not-a-uuid",
            "started_at": moment,
            "ended_at": datetime(2026, 10, 31),  # naive: dropped
        }
    )
    assert payload == {
        "run_id": str(run_id),
        "step_id": str(run_id),
        "started_at": "2026-10-31T04:00:00+00:00",
    }


def test_every_payload_fits_the_4096_byte_check():
    widest = {key: "A" * 64 for key in PAYLOAD_KEYS}
    widest.update(
        {key: uuid4() for key in ("incident_id", "run_id", "step_id", "attempt_id")}
    )
    widest.update(started_at=datetime.now(UTC), ended_at=datetime.now(UTC))
    widest["tool_name"] = "a" * 64
    payload = build_payload(widest)
    assert set(payload) == PAYLOAD_KEYS - {
        key
        for key in PAYLOAD_KEYS
        if key.endswith("_tokens") or key == "budget_seconds"
    }
    assert len(json.dumps(payload).encode()) < 4096


def test_the_migration_holds_frozen_copies_of_the_shapes_and_grants():
    assert migration.IDENTIFIER == outbox.IDENTIFIER.pattern
    columns = f"INSERT({migration.WRITER_COLUMNS})"
    assert RUNTIME_GRANTS[WEB_ROLE]["opspilot_trace_outbox"] == frozenset({columns})
    declared = {
        privilege.replace(" ", "")
        for role, privilege in migration.GRANTS
        if role == "opspilot_worker"
    }
    assert RUNTIME_GRANTS[WORKER_ROLE]["opspilot_trace_outbox"] == declared
    assert (
        ",".join(sorted(migration.WRITER_COLUMNS.split(",")))
        == migration.WRITER_COLUMNS
    )


@pytest.mark.parametrize(("capacity", "pool"), [("2", 4), ("1", 3), ("4", 6)])
def test_a_pool_below_capacity_plus_three_is_refused(monkeypatch, capacity, pool):
    monkeypatch.setenv("OPSPILOT_WORKER_CAPACITY", capacity)
    with pytest.raises(SystemExit, match=r"OPSPILOT_WORKER_CAPACITY\+3"):
        worker_main.worker_capacity(PoolConfig(max_size=pool))


@pytest.mark.parametrize(
    ("capacity", "pool", "ok"),
    [("2", "4", False), ("2", "5", True), ("4", "7", True), ("4", "6", False)],
)
def test_validate_pool_capacity_reads_the_environment(monkeypatch, capacity, pool, ok):
    monkeypatch.setenv("OPSPILOT_WORKER_CAPACITY", capacity)
    monkeypatch.setenv("OPSPILOT_POOL_MAX_SIZE", pool)
    if ok:
        assert worker_main.validate_pool_capacity() == int(capacity)
    else:
        with pytest.raises(SystemExit, match="POOL_MAX_SIZE"):
            worker_main.validate_pool_capacity()


def test_main_refuses_an_explicit_pool_of_capacity_plus_two_before_the_database(
    monkeypatch,
):
    monkeypatch.setenv("OPSPILOT_WORKER_DSN", "host=127.0.0.1 port=1 dbname=none")
    monkeypatch.delenv("OPSPILOT_WORKER_CAPACITY", raising=False)
    monkeypatch.setenv("OPSPILOT_POOL_MAX_SIZE", "4")

    def no_store(*_args, **_kwargs):
        raise AssertionError("opened the store")

    monkeypatch.setattr(worker_main, "DurableStore", no_store)
    with pytest.raises(SystemExit, match="OPSPILOT_POOL_MAX_SIZE=4"):
        worker_main.main([])


def test_main_takes_the_floor_when_the_pool_size_is_unset(monkeypatch):
    monkeypatch.setenv("OPSPILOT_WORKER_DSN", "host=127.0.0.1 port=1 dbname=none")
    monkeypatch.setenv("OPSPILOT_WORKER_CAPACITY", "3")
    # set first so monkeypatch restores the absence main() overwrites
    monkeypatch.setenv("OPSPILOT_POOL_MAX_SIZE", "0")
    monkeypatch.delenv("OPSPILOT_POOL_MAX_SIZE")

    class Opened(Exception):
        pass

    def opened(*_args, **_kwargs):
        raise Opened

    monkeypatch.setattr(worker_main, "DurableStore", opened)
    with pytest.raises(Opened):
        worker_main.main([])
    assert PoolConfig.from_env().max_size == 6


class _Store:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def purge(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_the_maintainer_purges_with_export_off_and_survives_a_refusal():
    from opspilot.persistence import PersistenceError

    store = _Store([PersistenceError("TIMEOUT"), 3])
    maintainer = outbox.OutboxMaintainer(store, stop=threading.Event(), interval=0)
    assert maintainer.run_once() is None
    assert maintainer.run_once() == 3
    assert store.calls == [{"export_enabled": False}] * 2


def test_the_maintainer_stops_on_the_event():
    store = _Store([0] * 100)
    stop = threading.Event()
    maintainer = outbox.OutboxMaintainer(store, stop=stop, interval=0.01)
    thread = threading.Thread(target=maintainer.run, name=outbox.THREAD_NAME)
    thread.start()
    stop.set()
    thread.join(2)
    assert not thread.is_alive()
    assert outbox.THREAD_NAME == "opspilot-outbox"
