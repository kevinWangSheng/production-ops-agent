"""Implementer's unit checks for ``opspilot.tools.kafka_admin`` beyond the
independent contract tests: the one re-run on ``_DESTROY_BROKER`` seen in CI
(PR #748) and the group listing that reports broker errors."""

from __future__ import annotations

import concurrent.futures
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from opspilot.tools.executor import TransportRequest, TransportUnavailable
from opspilot.tools.kafka_admin import (
    BROKERS_TOOL,
    CONSUMERS_TOOL,
    KafkaConfig,
    KafkaTransport,
)
from opspilot.tools.outcomes import Window

ck = pytest.importorskip("confluent_kafka")

END = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def _request(tool: str) -> TransportRequest:
    return TransportRequest(
        operation_id="op",
        source="otel-demo",
        verb="list",
        endpoint="http://127.0.0.1:1",
        selector={"kafka_bootstrap": "localhost:9092"},
        params={},
        window=Window(END - timedelta(hours=1), END),
        timeout_seconds=5.0,
        max_result_bytes=2 * 1024 * 1024,
        credential_ref="otel-demo",
        tool=tool,
    )


def _failed(name: str) -> concurrent.futures.Future[Any]:
    future: concurrent.futures.Future[Any] = concurrent.futures.Future()
    error = ck.KafkaError(getattr(ck.KafkaError, name), name)
    future.set_exception(ck.KafkaException(error))
    return future


def _done(value: Any) -> concurrent.futures.Future[Any]:
    future: concurrent.futures.Future[Any] = concurrent.futures.Future()
    future.set_result(value)
    return future


class _Client:
    def __init__(self, answers: list[concurrent.futures.Future[Any]]) -> None:
        self.answers = answers
        self.calls = 0

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def describe_cluster(self, **kwargs: Any) -> concurrent.futures.Future[Any]:
        self.calls += 1
        return self.answers.pop(0)

    def list_consumer_groups(self, **kwargs: Any) -> concurrent.futures.Future[Any]:
        self.calls += 1
        return self.answers.pop(0)


_CLUSTER = SimpleNamespace(
    cluster_id="c",
    controller=SimpleNamespace(id=1),
    nodes=[SimpleNamespace(id=1, host="localhost", port=9092, rack=None)],
)


def test_a_retired_broker_handle_is_read_once_more():
    client = _Client([_failed("_DESTROY_BROKER"), _done(_CLUSTER)])
    transport = KafkaTransport(
        KafkaConfig("localhost:9092"), client_factory=lambda conf: client
    )
    response = transport.fetch(_request(BROKERS_TOOL))
    assert client.calls == 2
    assert response.source_status is None and b'"id":1' in response.body


def test_a_second_retired_handle_is_unavailable():
    client = _Client([_failed("_DESTROY_BROKER"), _failed("_DESTROY_BROKER")])
    transport = KafkaTransport(
        KafkaConfig("localhost:9092"), client_factory=lambda conf: client
    )
    with pytest.raises(TransportUnavailable):
        transport.fetch(_request(BROKERS_TOOL))
    assert client.calls == 2


def test_a_listing_with_only_broker_errors_is_not_no_groups():
    error = ck.KafkaException(ck.KafkaError(ck.KafkaError._TRANSPORT, "broker down"))
    client = _Client([_done(SimpleNamespace(valid=[], errors=[error]))])
    transport = KafkaTransport(
        KafkaConfig("localhost:9092"), client_factory=lambda conf: client
    )
    with pytest.raises(TransportUnavailable):
        transport.fetch(_request(CONSUMERS_TOOL))


def test_a_partial_listing_is_incomplete_and_names_the_error():
    error = ck.KafkaException(
        ck.KafkaError(ck.KafkaError.COORDINATOR_NOT_AVAILABLE, "loading")
    )
    group = SimpleNamespace(
        group_id="g", state=None, type=None, is_simple_consumer_group=False
    )
    client = _Client([_done(SimpleNamespace(valid=[group], errors=[error]))])
    transport = KafkaTransport(
        KafkaConfig("localhost:9092"), client_factory=lambda conf: client
    )
    response = transport.fetch(_request(CONSUMERS_TOOL))
    assert response.incomplete is True
    assert "COORDINATOR_NOT_AVAILABLE" in str(response.view_fields)


def test_a_failed_login_reported_to_error_cb_is_forbidden_not_a_timeout():
    hung: concurrent.futures.Future[Any] = concurrent.futures.Future()
    client = _Client([hung])
    confs: list[dict[str, Any]] = []

    def factory(conf: dict[str, Any]) -> _Client:
        confs.append(conf)
        return client

    def poll(timeout: float) -> int:
        error = ck.KafkaError(ck.KafkaError._AUTHENTICATION, "bad login")
        confs[0]["error_cb"](error)
        return 0

    client.poll = poll  # type: ignore[attr-defined]
    transport = KafkaTransport(KafkaConfig("localhost:9092"), client_factory=factory)
    response = transport.fetch(_request(BROKERS_TOOL))
    assert response.source_status == "KAFKA_FORBIDDEN"
    assert "SASL_AUTHENTICATION_FAILED" in str(response.view_fields)


class _GroupClient:
    """One group (``g``) committed on four partitions of ``t``; partition 0
    reads normally, the others fail with ``errors[p]`` in ``list_offsets``."""

    def __init__(self, errors: dict[int, str]) -> None:
        self.errors = errors

    def __enter__(self) -> _GroupClient:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def poll(self, timeout: float) -> int:
        return 0

    def describe_consumer_groups(self, ids: list[str], **kwargs: Any) -> Any:
        group = SimpleNamespace(
            members=[],
            state=ck.ConsumerGroupState.EMPTY,
            type=ck.ConsumerGroupType.CLASSIC,
            coordinator=SimpleNamespace(id=1),
            partition_assignor="",
        )
        return {gid: _done(group) for gid in ids}

    def list_consumer_group_offsets(self, requests: list[Any], **kwargs: Any) -> Any:
        tps = [ck.TopicPartition("t", p, 10) for p in range(4)]
        return {
            r.group_id: _done(SimpleNamespace(topic_partitions=tps)) for r in requests
        }

    def list_consumer_groups(self, **kwargs: Any) -> Any:
        group = SimpleNamespace(group_id="g")
        return _done(SimpleNamespace(valid=[group], errors=[]))

    def describe_topics(self, topics: Any, **kwargs: Any) -> Any:
        parts = [SimpleNamespace(id=p) for p in range(4)]
        return {"t": _done(SimpleNamespace(partitions=parts))}

    def list_offsets(self, request: dict[Any, Any], **kwargs: Any) -> Any:
        out = {}
        for tp, spec in request.items():
            name = self.errors.get(tp.partition)
            offset = 0 if "earliest" in type(spec).__name__.lower() else 30
            out[tp] = _failed(name) if name else _done(SimpleNamespace(offset=offset))
        return out


PARTITION_ERRORS = {
    1: "LEADER_NOT_AVAILABLE",
    2: "NOT_LEADER_FOR_PARTITION",
    3: "UNKNOWN_TOPIC_OR_PART",
}


def _group_request(tool: str, params: dict[str, Any]) -> TransportRequest:
    from dataclasses import replace

    return replace(_request(tool), params=params)


def test_partition_errors_leave_the_other_partitions_standing():
    import json

    transport = KafkaTransport(
        KafkaConfig("localhost:9092"),
        client_factory=lambda conf: _GroupClient(PARTITION_ERRORS),
    )
    response = transport.fetch(
        _group_request("describe_consumer_group", {"group_id": "g"})
    )
    assert response.source_status is None, response.view_fields
    (group,) = json.loads(response.body)["results"]
    rows = {r["partition"]: r for r in group["partitions"]}
    assert rows[0]["lag"] == 20 and rows[0]["flags"] == []
    for p in PARTITION_ERRORS:
        assert rows[p]["end_offset"] is None and rows[p]["lag"] is None
        assert rows[p]["flags"] == ["end_offset_unavailable"]
    assert group["lag_by_topic"] == {"t": 20}
    notes = str(response.view_fields)
    for name in PARTITION_ERRORS.values():
        assert name in notes, notes


def test_find_counts_only_the_readable_partitions():
    import json

    transport = KafkaTransport(
        KafkaConfig("localhost:9092"),
        client_factory=lambda conf: _GroupClient(PARTITION_ERRORS),
    )
    response = transport.fetch(
        _group_request("find_consumer_groups_by_topic", {"topic_name": "t"})
    )
    assert response.source_status is None, response.view_fields
    (row,) = json.loads(response.body)["results"]
    assert row["total_lag"] == 20
    assert "LEADER_NOT_AVAILABLE" in str(response.view_fields)


def test_a_partition_authorization_failure_still_ends_the_call():
    transport = KafkaTransport(
        KafkaConfig("localhost:9092"),
        client_factory=lambda conf: _GroupClient({1: "TOPIC_AUTHORIZATION_FAILED"}),
    )
    response = transport.fetch(
        _group_request("describe_consumer_group", {"group_id": "g"})
    )
    assert response.source_status == "KAFKA_FORBIDDEN"
