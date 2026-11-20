"""Implementer's real-broker checks for C3 (#567), beside the independent
author's ``test_tool_expansion_c3_kafka.py`` on the same CI broker.

Only ``impl-`` topics and groups (at most a few groups, so the author's
200-group scan still reaches ``order-*``). What this file adds: the
auto-create guard with a fresh misspelt name through every by-name tool on
the anonymous listener, the lag of a small known group, and two
observations recorded for the task record (what each tool needs beyond the
Describe ACLs, and the cost of one short-lived client per call). Runs only
in the CI job ``kafka-backend`` (``OPSPILOT_KAFKA_BACKEND=1``).
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import time
import uuid
import warnings
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from opspilot.tools import ToolRequest
from opspilot.tools.outcomes import Window
from opspilot.tools.profiles import PROFILE_ENV, select_profile
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import TARGET_ID, FakeStore, _lease

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_KAFKA_BACKEND") != "1",
    reason="real Kafka runs only in the CI kafka-backend job",
)

SASL = os.environ.get("KAFKA_IT_SASL_BOOTSTRAP", "localhost:19092")
NOAUTH = os.environ.get("KAFKA_IT_NOAUTH_BOOTSTRAP", "localhost:19094")
ADMIN = os.environ.get("KAFKA_IT_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("KAFKA_IT_ADMIN_PASSWORD", "admin-pass")
READER = os.environ.get("KAFKA_IT_READONLY_USER", "opspilot")
READER_PASSWORD = os.environ.get("KAFKA_IT_READONLY_PASSWORD", "opspilot-pass")
PROBE_OUT = os.environ.get("KAFKA_IT_PROBE_OUT")

TOPIC = "impl-orders"
ENDS = {0: 50, 1: 30}
COMMITTED = {0: 10, 1: 30}  # lag 40 + 0
GROUP = "impl-stopped"

NOW = datetime.now(timezone.utc).replace(microsecond=0)
FRAME = Window(NOW - timedelta(hours=6), NOW)
_INDEX = itertools.count()


def _observe(text: str) -> None:
    warnings.warn(text, stacklevel=2)
    if PROBE_OUT:
        with open(PROBE_OUT, "a", encoding="utf-8") as out:
            out.write(json.dumps({"observation": f"impl: {text}"}) + "\n")


def _admin():
    from confluent_kafka.admin import AdminClient

    return AdminClient(
        {
            "bootstrap.servers": SASL,
            "security.protocol": "SASL_PLAINTEXT",
            "sasl.mechanism": "PLAIN",
            "sasl.username": ADMIN,
            "sasl.password": ADMIN_PASSWORD,
        }
    )


def _topics(admin) -> set[str]:
    return set(admin.list_topics(timeout=30).topics)


@pytest.fixture(scope="module")
def admin():
    from confluent_kafka import ConsumerGroupTopicPartitions, Producer, TopicPartition
    from confluent_kafka.admin import (
        AclBinding,
        AclOperation,
        AclPermissionType,
        NewTopic,
        ResourcePatternType,
        ResourceType,
    )

    client = _admin()
    deadline = time.monotonic() + 120
    while True:
        try:
            client.list_topics(timeout=5)
            break
        except Exception:
            assert time.monotonic() < deadline, f"Kafka at {SASL} not ready"
            time.sleep(2)
    grants = [
        (ResourceType.TOPIC, "*", AclOperation.DESCRIBE),
        (ResourceType.TOPIC, "*", AclOperation.DESCRIBE_CONFIGS),
        (ResourceType.GROUP, "*", AclOperation.DESCRIBE),
        (ResourceType.BROKER, "kafka-cluster", AclOperation.DESCRIBE),
        (ResourceType.BROKER, "kafka-cluster", AclOperation.DESCRIBE_CONFIGS),
    ]
    acls = [
        AclBinding(
            kind,
            name,
            ResourcePatternType.LITERAL,
            f"User:{READER}",
            "*",
            operation,
            AclPermissionType.ALLOW,
        )
        for kind, name, operation in grants
    ]
    for future in client.create_acls(acls).values():
        future.result(timeout=60)
    if TOPIC not in _topics(client):
        for future in client.create_topics([NewTopic(TOPIC, 2, 1)]).values():
            future.result(timeout=60)
    deadline = time.monotonic() + 60
    while TOPIC not in _topics(client):
        assert time.monotonic() < deadline
        time.sleep(1)
    producer = Producer(
        {
            "bootstrap.servers": SASL,
            "security.protocol": "SASL_PLAINTEXT",
            "sasl.mechanism": "PLAIN",
            "sasl.username": ADMIN,
            "sasl.password": ADMIN_PASSWORD,
        }
    )
    from confluent_kafka.admin import OffsetSpec

    request = {TopicPartition(TOPIC, p): OffsetSpec.latest() for p in ENDS}
    have = {
        tp.partition: f.result(timeout=30).offset
        for tp, f in client.list_offsets(request).items()
    }
    for partition, count in ENDS.items():
        assert have[partition] in (0, count), have
        for n in range(have[partition], count):
            producer.produce(TOPIC, value=b"impl record %d" % n, partition=partition)
    assert producer.flush(60) == 0
    offsets = ConsumerGroupTopicPartitions(
        GROUP, [TopicPartition(TOPIC, p, o) for p, o in COMMITTED.items()]
    )
    for future in client.alter_consumer_group_offsets([offsets]).values():
        future.result(timeout=60)
    return client


@pytest.fixture(scope="module")
def k8s_ca(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("c3-impl") / "synthetic-ca.pem"
    path.write_text("synthetic ca placeholder\n")
    return str(path)


def _executor(k8s_ca: str, bootstrap: str, **kafka: str):
    if shutil.which("jq") is None or shutil.which("python3") is None:
        pytest.skip("the extension worker needs jq and python3 on PATH")
    env = {
        PROFILE_ENV: "otel-demo-ext",
        "OPSPILOT_K8S_API_URL": "https://127.0.0.1:16443",
        "OPSPILOT_K8S_TOKEN": "k8s-ro-aaaa567",
        "OPSPILOT_K8S_CA_FILE": k8s_ca,
        "OPSPILOT_K8S_NAMESPACE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_KAFKA_BOOTSTRAP_SERVERS": bootstrap,
        **kafka,
    }
    clock = FakeClock(start=NOW)
    profile = select_profile(env)
    lease = _lease()
    deadline = NOW + timedelta(minutes=10)
    factory = profile.executor_factory(
        FakeStore(lease, deadline=deadline), RecordingSink(), clock
    )
    return factory(
        lease,
        profile.face(clock).input_for(
            run_id=str(lease.run_id),
            question="impl check",
            target_id=TARGET_ID,
            deadline=deadline,
            model_requests=12,
        ),
    )


@pytest.fixture(scope="module")
def reader(k8s_ca):
    return _executor(
        k8s_ca,
        SASL,
        OPSPILOT_KAFKA_SECURITY_PROTOCOL="SASL_PLAINTEXT",
        OPSPILOT_KAFKA_USERNAME=READER,
        OPSPILOT_KAFKA_PASSWORD=READER_PASSWORD,
    )


@pytest.fixture(scope="module")
def anonymous(k8s_ca):
    return _executor(k8s_ca, NOAUTH)


def _run(executor, tool: str, params: Mapping[str, object] | None = None):
    return executor.execute(
        ToolRequest(
            step_id="c3-impl",
            tool_index=next(_INDEX),
            tool_name=tool,
            target_ref=TARGET_ID,
            params=dict(params or {}),
            window=FRAME.as_json(),
        )
    )


def _view(outcome) -> Mapping[str, Any]:
    assert outcome.model_view is not None
    return outcome.model_view


def test_the_impl_group_lag_is_exact(admin, reader):
    outcome = _run(reader, "describe_consumer_group", {"group_id": GROUP})
    assert outcome.status == "ok", _view(outcome)
    (group,) = _view(outcome)["content"]
    assert group["lag_by_topic"] == {TOPIC: 40}
    rows = {r["partition"]: r for r in group["partitions"]}
    assert (rows[0]["committed_offset"], rows[0]["end_offset"], rows[0]["lag"]) == (
        10,
        50,
        40,
    )
    assert rows[1]["lag"] == 0
    found = _run(reader, "find_consumer_groups_by_topic", {"topic_name": TOPIC})
    assert found.status == "ok", _view(found)
    by = {r["group_id"]: r for r in _view(found)["content"]}
    assert by[GROUP]["total_lag"] == 40


def test_a_misspelt_name_creates_nothing_on_the_anonymous_listener(admin, anonymous):
    missing = f"impl-missing-{uuid.uuid4().hex[:8]}"
    before = _topics(admin)
    calls = [
        ("describe_topic", {"topic_name": missing}),
        ("describe_topic", {"topic_name": missing, "fetch_configuration": True}),
        ("find_consumer_groups_by_topic", {"topic_name": missing}),
        ("describe_configs", {"resource_type": "topic", "resource_name": missing}),
    ]
    for tool, params in calls:
        outcome = _run(anonymous, tool, params)
        assert outcome.status == "no_data", (tool, _view(outcome))
        assert "TOPIC_NOT_FOUND" in json.dumps(_view(outcome).get("notes") or [])
    time.sleep(3)  # an auto-created topic would show within this
    after = _topics(admin)
    assert missing not in after
    assert after - before <= {t for t in after if t.startswith("autocreate-probe-")}


def test_observe_reads_beyond_describe_and_client_cost(admin, reader):
    params = {
        "list_kafka_consumers": {},
        "describe_consumer_group": {"group_id": GROUP},
        "list_topics": {},
        "describe_topic": {"topic_name": TOPIC, "fetch_configuration": True},
        "find_consumer_groups_by_topic": {"topic_name": TOPIC},
        "list_brokers": {},
        "describe_configs": {"resource_type": "broker", "resource_name": "1"},
        "cluster_overview": {},
    }
    results = {}
    seconds = []
    for tool, args in params.items():
        started = time.monotonic()
        outcome = _run(reader, tool, args)
        seconds.append(time.monotonic() - started)
        results[tool] = f"{outcome.status}/{outcome.reason}"
    _observe(
        "with Describe-only ACLs: "
        + ", ".join(f"{tool}={status}" for tool, status in results.items())
    )
    started = time.monotonic()
    for _ in range(5):
        _run(reader, "list_brokers")
    per_call = (time.monotonic() - started) / 5
    _observe(
        f"list_brokers with a new client per call: {per_call:.3f} s per call "
        f"(eight tools {min(seconds):.3f}-{max(seconds):.3f} s)"
    )
    assert all(value.startswith("ok/") for value in results.values()), results


def test_observe_group_visibility_admin_and_reader(admin):
    """What the admin and the Describe-only reader see of ``impl-stopped``
    through the raw admin API (an observation for the task record)."""
    from confluent_kafka import ConsumerGroupTopicPartitions
    from confluent_kafka.admin import AdminClient

    reader_client = AdminClient(
        {
            "bootstrap.servers": SASL,
            "security.protocol": "SASL_PLAINTEXT",
            "sasl.mechanism": "PLAIN",
            "sasl.username": READER,
            "sasl.password": READER_PASSWORD,
        }
    )
    for label, client in (("admin", admin), ("reader", reader_client)):
        report: dict[str, Any] = {}
        try:
            listed = client.list_consumer_groups(request_timeout=20).result(timeout=25)
            report["listed"] = sorted(g.group_id for g in listed.valid)[:20]
            report["list_errors"] = [str(e) for e in listed.errors][:5]
        except Exception as error:
            report["list_error"] = str(error)[:300]
        try:
            (desc,) = [
                f.result(timeout=25)
                for f in client.describe_consumer_groups(
                    [GROUP], request_timeout=20
                ).values()
            ]
            report["describe"] = f"{desc.state} {desc.type} members={len(desc.members)}"
        except Exception as error:
            report["describe_error"] = str(error)[:300]
        try:
            (offsets,) = [
                f.result(timeout=25)
                for f in client.list_consumer_group_offsets(
                    [ConsumerGroupTopicPartitions(GROUP)], request_timeout=20
                ).values()
            ]
            report["offsets"] = [
                (tp.topic, tp.partition, tp.offset, str(tp.error))
                for tp in offsets.topic_partitions
            ]
        except Exception as error:
            report["offsets_error"] = str(error)[:300]
        _observe(f"group visibility {label}: {json.dumps(report, default=str)}")
