"""Real-broker integration tests for tool expansion C3 (#567), R8-25 and
C3-15 ... C3-20: the eight Kafka read-only tools against a real single-node
KRaft ``apache/kafka:4.3.2`` through the public tool path. No model is
called and no question is run.

Runs only in the CI job ``kafka-backend`` (``OPSPILOT_KAFKA_BACKEND=1``),
which starts the broker; skipped everywhere else. ``confluent_kafka`` is
imported only inside the gated fixtures and tests.

The fixtures, not the product, write: the ``admin`` account creates the ACLs,
topics, records, committed offsets and deleted records; a real consumer in a
test thread keeps ``order-audit`` a live member. The product only ever holds
the ``opspilot`` account (Describe-class ACLs, no Topic READ) on the SASL
listener, or the anonymous listener for the tool-layer proof.

What the CI job must provide (ASSUMPTIONS -- the interface names the
listeners and users but not ports, variable names or passwords; defaults are
these values, each overridable by the variable of the same name):

* ``C3_KAFKA_SASL_BOOTSTRAP`` -- the ``SASL_PLAINTEXT`` listener, advertised
  and host-mapped as ``localhost:9092`` (default ``localhost:9092``).
* ``C3_KAFKA_NOAUTH_BOOTSTRAP`` -- the ``NOAUTH`` PLAINTEXT listener whose
  anonymous principal is a super user, advertised and host-mapped as
  ``localhost:9094`` (default ``localhost:9094``).
* Node id ``1`` (``KAFKA_NODE_ID=1``), ``auto.create.topics.enable=true``,
  ``StandardAuthorizer``, ``allow.everyone.if.no.acl.found=false``, super
  users ``User:admin;User:ANONYMOUS``, SASL mechanism ``PLAIN`` with JAAS
  static users ``admin``, ``opspilot`` and ``nobody``.
* ``C3_KAFKA_ADMIN_PASSWORD`` / ``C3_KAFKA_OPSPILOT_PASSWORD`` /
  ``C3_KAFKA_NOBODY_PASSWORD`` -- their passwords (defaults
  ``admin-pw-567`` / ``opspilot-pw-567`` / ``nobody-pw-567``, low entropy,
  for the gitleaks allow-list).
* ``C3_KAFKA_PROBE_OUT`` (optional) -- a file the recorded observations are
  appended to as JSON lines; they are also emitted as warnings.
* The job grants nothing itself: this module creates every ACL, topic and
  group (so a re-run on the same broker is not supported).
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import threading
import time
import warnings
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from opspilot.tools import ToolRequest
from opspilot.tools.outcomes import Window
from opspilot.tools.profiles import PROFILE_ENV, ToolProfile, select_profile
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import TARGET_ID, FakeStore, _lease

pytestmark = pytest.mark.skipif(
    os.environ.get("OPSPILOT_KAFKA_BACKEND") != "1",
    reason="real Kafka runs only in the CI kafka-backend job",
)

SASL = os.environ.get("C3_KAFKA_SASL_BOOTSTRAP", "localhost:9092")
NOAUTH = os.environ.get("C3_KAFKA_NOAUTH_BOOTSTRAP", "localhost:9094")
ADMIN_PASSWORD = os.environ.get("C3_KAFKA_ADMIN_PASSWORD", "admin-pw-567")
OPSPILOT_PASSWORD = os.environ.get("C3_KAFKA_OPSPILOT_PASSWORD", "opspilot-pw-567")
NOBODY_PASSWORD = os.environ.get("C3_KAFKA_NOBODY_PASSWORD", "nobody-pw-567")
PROBE_OUT = os.environ.get("C3_KAFKA_PROBE_OUT")

CONSUMERS = "list_kafka_consumers"
GROUP = "describe_consumer_group"
TOPICS = "list_topics"
TOPIC = "describe_topic"
FIND = "find_consumer_groups_by_topic"
BROKERS = "list_brokers"
CONFIGS = "describe_configs"
OVERVIEW = "cluster_overview"
TOOLS = (CONSUMERS, GROUP, TOPICS, TOPIC, FIND, BROKERS, CONFIGS, OVERVIEW)

# Message bodies, keys and headers carry this; no tool return may.
SENTINEL = "msg-body-sentinel-567"
META_SENTINEL = "commit-meta-sentinel-567"
ORDERS_ENDS = {0: 1000, 1: 800, 2: 600}
PROCESSOR_OFFSETS = {0: 400, 1: 800, 2: 100}  # lag 600 / 0 / 500 = 1100
PAYMENTS_RECORDS = 5
LEDGER_RECORDS = 100
LEDGER_COMMIT, LEDGER_DELETE_BEFORE = 10, 50  # committed below log start
AHEAD_COMMIT = 999  # stale's payments commit, past the end
WIDE = "wide-1200"
WIDE_PARTITIONS = 1200
BULK = [f"zz-bulk-{n:03d}" for n in range(210)]
RETENTION_MS = "86400123"
MISSING = "does-not-exist-567"
PROBE_TOPIC = "autocreate-probe-567"

NOW = datetime.now(timezone.utc).replace(microsecond=0)
FRAME = Window(NOW - timedelta(hours=6), NOW)
OUTCOMES: list[Any] = []


def _observe(text: str) -> None:
    warnings.warn(text, stacklevel=2)
    if PROBE_OUT:
        with open(PROBE_OUT, "a", encoding="utf-8") as out:
            out.write(json.dumps({"observation": text}) + "\n")


# -- engineer-side clients (fixtures only, never the product) -------------------


def _conf(user: str, password: str, bootstrap: str = SASL) -> dict[str, Any]:
    return {
        "bootstrap.servers": bootstrap,
        "security.protocol": "SASL_PLAINTEXT",
        "sasl.mechanism": "PLAIN",
        "sasl.username": user,
        "sasl.password": password,
    }


def _admin(user: str = "admin", password: str = ADMIN_PASSWORD):
    from confluent_kafka.admin import AdminClient

    return AdminClient(_conf(user, password))


def _wait_all(futures: Mapping[Any, Any], timeout: float = 60) -> list[Any]:
    return [future.result(timeout=timeout) for future in futures.values()]


def _topic_names(admin) -> set[str]:
    return set(admin.list_topics(timeout=30).topics)


def _committed(admin, group: str) -> dict[tuple[str, int], int]:
    from confluent_kafka import ConsumerGroupTopicPartitions

    (result,) = _wait_all(
        admin.list_consumer_group_offsets([ConsumerGroupTopicPartitions(group)])
    )
    return {
        (tp.topic, tp.partition): tp.offset
        for tp in result.topic_partitions
        if tp.offset >= 0
    }


def _ends(admin, topics: Mapping[str, int]) -> dict[tuple[str, int], int]:
    from confluent_kafka import TopicPartition
    from confluent_kafka.admin import OffsetSpec

    request = {
        TopicPartition(topic, p): OffsetSpec.latest()
        for topic, count in topics.items()
        for p in range(count)
    }
    futures = admin.list_offsets(request, request_timeout=30)
    return {
        (tp.topic, tp.partition): future.result(timeout=30).offset
        for tp, future in futures.items()
    }


def _set_offsets(admin, group: str, offsets: Mapping[tuple[str, int], int], meta=None):
    from confluent_kafka import ConsumerGroupTopicPartitions, TopicPartition

    request = ConsumerGroupTopicPartitions(
        group, [TopicPartition(t, p, o, meta) for (t, p), o in offsets.items()]
    )
    _wait_all(admin.alter_consumer_group_offsets([request]))


def _acls():
    from confluent_kafka.admin import (
        AclBinding,
        AclOperation,
        AclPermissionType,
        ResourcePatternType,
        ResourceType,
    )

    principal = "User:opspilot"
    grants = [
        (ResourceType.TOPIC, "*", AclOperation.DESCRIBE),
        (ResourceType.TOPIC, "*", AclOperation.DESCRIBE_CONFIGS),
        (ResourceType.GROUP, "*", AclOperation.DESCRIBE),
        (ResourceType.BROKER, "kafka-cluster", AclOperation.DESCRIBE),
        (ResourceType.BROKER, "kafka-cluster", AclOperation.DESCRIBE_CONFIGS),
    ]
    return [
        AclBinding(
            kind,
            name,
            ResourcePatternType.LITERAL,
            principal,
            "*",
            operation,
            AclPermissionType.ALLOW,
        )
        for kind, name, operation in grants
    ]


class _AuditMember(threading.Thread):
    """A real consumer that keeps ``order-audit`` Stable: it reads every
    orders and payments record, commits orders only (so payments stays
    ``no_committed_offset``), and keeps polling until stopped."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.stop = threading.Event()
        self.committed = threading.Event()
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            from confluent_kafka import Consumer, TopicPartition

            consumer = Consumer(
                {
                    **_conf("admin", ADMIN_PASSWORD),
                    "group.id": "order-audit",
                    "client.id": "audit-client",
                    "auto.offset.reset": "earliest",
                    "enable.auto.commit": False,
                }
            )
            consumer.subscribe(["orders", "payments"])
            seen = 0
            want = sum(ORDERS_ENDS.values())
            deadline = time.monotonic() + 120
            while seen < want and time.monotonic() < deadline:
                message = consumer.poll(1.0)
                if message is not None and message.error() is None:
                    if message.topic() == "orders":
                        seen += 1
            consumer.commit(
                offsets=[
                    TopicPartition("orders", p, e) for p, e in ORDERS_ENDS.items()
                ],
                asynchronous=False,
            )
            self.committed.set()
            while not self.stop.is_set():
                consumer.poll(0.5)
            consumer.close()
        except BaseException as error:  # surfaced by the fixture
            self.error = error
            self.committed.set()


@pytest.fixture(scope="module", autouse=True)
def cluster():
    from confluent_kafka import Producer
    from confluent_kafka.admin import NewTopic

    admin = _admin()
    deadline = time.monotonic() + 120
    while True:
        try:
            admin.list_topics(timeout=5)
            break
        except Exception:
            assert time.monotonic() < deadline, f"Kafka at {SASL} not ready"
            time.sleep(2)

    _wait_all(admin.create_acls(_acls()))
    _wait_all(
        admin.create_topics(
            [
                NewTopic("orders", 3, 1, config={"retention.ms": RETENTION_MS}),
                NewTopic("payments", 1, 1),
                NewTopic("ledger", 1, 1),
                NewTopic(WIDE, WIDE_PARTITIONS, 1),
            ]
        ),
        timeout=120,
    )
    producer = Producer(_conf("admin", ADMIN_PASSWORD))
    plan = [("orders", p, n) for p, n in ORDERS_ENDS.items()]
    plan += [("payments", 0, PAYMENTS_RECORDS), ("ledger", 0, LEDGER_RECORDS)]
    for topic, partition, count in plan:
        for n in range(count):
            producer.produce(
                topic,
                value=f"{SENTINEL} {topic} record {n}".encode(),
                key=f"{SENTINEL}-key-{n}".encode(),
                partition=partition,
                headers=[("note", SENTINEL.encode())],
            )
            if n % 500 == 0:
                producer.poll(0)
    assert producer.flush(60) == 0

    _set_offsets(
        admin,
        "order-processor",
        {("orders", p): o for p, o in PROCESSOR_OFFSETS.items()},
        META_SENTINEL,
    )
    _set_offsets(
        admin,
        "stale",
        {("ledger", 0): LEDGER_COMMIT, ("payments", 0): AHEAD_COMMIT},
    )
    from confluent_kafka import TopicPartition

    _wait_all(admin.delete_records([TopicPartition("ledger", 0, LEDGER_DELETE_BEFORE)]))
    for group in BULK:
        _set_offsets(admin, group, {("payments", 0): 0})

    member = _AuditMember()
    member.start()
    assert member.committed.wait(150), "order-audit never committed"
    assert member.error is None, member.error
    deadline = time.monotonic() + 60
    while True:
        (desc,) = _wait_all(admin.describe_consumer_groups(["order-audit"]))
        if desc.members and desc.state.name == "STABLE":
            break
        assert time.monotonic() < deadline, desc.state
        time.sleep(1)

    # The broker really auto-creates topics on this listener: a producer's
    # metadata request for an unknown name creates it (positive control for
    # the tool-layer proof below).
    probe = Producer({"bootstrap.servers": NOAUTH})
    probe.produce(PROBE_TOPIC, value=b"probe")
    probe.flush(30)
    deadline = time.monotonic() + 30
    while PROBE_TOPIC not in _topic_names(admin):
        assert time.monotonic() < deadline, "auto.create.topics.enable is not on"
        time.sleep(1)
    _observe(f"positive control: a producer on {NOAUTH} auto-created {PROBE_TOPIC}")

    # The opspilot ACLs are in force before any tool runs.
    reader = _admin("opspilot", OPSPILOT_PASSWORD)
    deadline = time.monotonic() + 60
    while True:
        try:
            reader.describe_cluster(request_timeout=10).result(timeout=15)
            break
        except Exception:
            assert time.monotonic() < deadline, "opspilot ACLs never applied"
            time.sleep(1)

    snapshot = _snapshot(admin)
    yield {"admin": admin, "snapshot": snapshot}
    member.stop.set()
    member.join(30)


def _snapshot(admin) -> dict[str, Any]:
    groups = ["order-processor", "order-audit", "stale", BULK[0], BULK[-1]]
    return {
        "topics": _topic_names(admin),
        "committed": {g: _committed(admin, g) for g in groups},
        "ends": _ends(admin, {"orders": 3, "payments": 1, "ledger": 1}),
    }


# -- the product path ---------------------------------------------------------------


@pytest.fixture(scope="module")
def k8s_ca(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("c3-k8s") / "synthetic-ca.pem"
    path.write_text("synthetic ca placeholder\n")
    return str(path)


def _env(k8s_ca: str, bootstrap: str = SASL, **kafka: str) -> dict[str, str]:
    return {
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


def _sasl(user: str = "opspilot", password: str = OPSPILOT_PASSWORD) -> dict[str, str]:
    return {
        "OPSPILOT_KAFKA_SECURITY_PROTOCOL": "SASL_PLAINTEXT",
        "OPSPILOT_KAFKA_SASL_MECHANISM": "PLAIN",
        "OPSPILOT_KAFKA_USERNAME": user,
        "OPSPILOT_KAFKA_PASSWORD": password,
    }


def _executor(env: Mapping[str, str]):
    if shutil.which("jq") is None or shutil.which("python3") is None:
        pytest.skip("the extension worker needs jq and python3 on PATH")
    clock = FakeClock(start=NOW)
    profile = select_profile(env)
    assert isinstance(profile, ToolProfile)
    lease = _lease()
    deadline = NOW + timedelta(minutes=10)
    factory = profile.executor_factory(
        FakeStore(lease, deadline=deadline), RecordingSink(), clock
    )
    return factory(
        lease,
        profile.face(clock).input_for(
            run_id=str(lease.run_id),
            question="What is the consumer lag on the orders topic?",
            target_id=TARGET_ID,
            deadline=deadline,
            model_requests=12,
        ),
    )


@pytest.fixture(scope="module")
def tools(k8s_ca):
    return _executor(_env(k8s_ca, **_sasl()))


@pytest.fixture(scope="module")
def anonymous(k8s_ca):
    return _executor(_env(k8s_ca, NOAUTH))


_INDEX = itertools.count()


def _run(executor, tool: str, params: dict[str, object] | None = None):
    outcome = executor.execute(
        ToolRequest(
            step_id="c3-live",
            tool_index=next(_INDEX),
            tool_name=tool,
            target_ref=TARGET_ID,
            params=dict(params or {}),
            window=FRAME.as_json(),
        )
    )
    OUTCOMES.append(outcome)
    return outcome


def _view(outcome) -> Mapping[str, Any]:
    assert outcome.model_view is not None
    return outcome.model_view


def _text(outcome) -> str:
    return json.dumps(_view(outcome), ensure_ascii=False, sort_keys=True, default=str)


def _rows(outcome) -> list[dict[str, Any]]:
    content = _view(outcome)["content"]
    assert isinstance(content, list), _view(outcome)
    return content


def _ok(outcome) -> None:
    assert outcome.status == "ok", (outcome.status, outcome.reason, _view(outcome))


def _no_data_with(outcome, code: str) -> None:
    assert outcome.status == "no_data", (outcome.status, outcome.reason, _view(outcome))
    assert code in json.dumps(_view(outcome).get("notes") or [], default=str)


def _name(value: object) -> str:
    return str(getattr(value, "name", value)).rsplit(".", 1)[-1].upper()


def _size(value: object) -> int:
    return len(value) if isinstance(value, (list, tuple)) else int(value)  # type: ignore[arg-type]


def _lag_rows(outcome) -> dict[tuple[str, int], dict[str, Any]]:
    (group,) = _rows(outcome)
    return {(r["topic"], r["partition"]): r for r in group["partitions"]}


PARAMS: dict[str, dict[str, object]] = {
    CONSUMERS: {},
    GROUP: {"group_id": "order-processor"},
    TOPICS: {},
    TOPIC: {"topic_name": "orders", "fetch_configuration": True},
    FIND: {"topic_name": "orders"},
    BROKERS: {},
    CONFIGS: {"resource_type": "topic", "resource_name": "orders"},
    OVERVIEW: {},
}


# -- the eight tools on the authenticated listener -----------------------------------


def test_every_tool_answers_with_real_data_under_the_describe_only_account(tools):
    served = []
    for tool in TOOLS:
        outcome = _run(tools, tool, PARAMS[tool])
        _ok(outcome)
        assert _rows(outcome), tool
        served.append(tool)
    broker = _run(tools, CONFIGS, {"resource_type": "broker", "resource_name": "1"})
    _ok(broker)
    _observe(
        "opspilot with Topic DESCRIBE/DESCRIBE_CONFIGS, Group DESCRIBE and Cluster "
        f"DESCRIBE/DESCRIBE_CONFIGS only (no READ) served: {', '.join(served)}, "
        "describe_configs(broker)"
    )


def test_the_stopped_group_lags_exactly_1100(tools):
    outcome = _run(tools, GROUP, {"group_id": "order-processor"})
    _ok(outcome)
    (group,) = _rows(outcome)
    assert _name(group["state"]) == "EMPTY"
    assert group["members"] == []
    rows = _lag_rows(outcome)
    assert sorted(rows) == [("orders", 0), ("orders", 1), ("orders", 2)]
    assert [rows["orders", p]["lag"] for p in range(3)] == [600, 0, 500]
    assert [rows["orders", p]["committed_offset"] for p in range(3)] == [400, 800, 100]
    assert [rows["orders", p]["end_offset"] for p in range(3)] == [1000, 800, 600]
    assert all(rows["orders", p]["flags"] == [] for p in range(3))
    assert group["lag_by_topic"] == {"orders": 1100}
    assert META_SENTINEL not in _text(outcome)


def test_the_live_group_has_members_assignment_zero_lag_and_an_uncommitted_partition(
    tools,
):
    outcome = _run(tools, GROUP, {"group_id": "order-audit"})
    _ok(outcome)
    (group,) = _rows(outcome)
    assert _name(group["state"]) == "STABLE"
    assert len(group["members"]) == 1
    (member,) = group["members"]
    assert member["client_id"] == "audit-client"
    assert member["host"]
    assigned = sorted((a["topic"], a["partition"]) for a in member["assignment"])
    assert assigned == [("orders", 0), ("orders", 1), ("orders", 2), ("payments", 0)]
    rows = _lag_rows(outcome)
    assert [rows["orders", p]["lag"] for p in range(3)] == [0, 0, 0]
    assert rows["payments", 0]["lag"] is None
    assert rows["payments", 0]["flags"] == ["no_committed_offset"]
    assert group["lag_by_topic"]["orders"] == 0
    assert group["coordinator"] not in (None, "", {})
    assert group["partition_assignor"]


def test_offsets_ahead_of_the_end_and_below_the_log_start_are_flagged(tools):
    outcome = _run(tools, GROUP, {"group_id": "stale"})
    _ok(outcome)
    rows = _lag_rows(outcome)
    ledger = rows["ledger", 0]
    assert ledger["committed_offset"] == LEDGER_COMMIT
    assert ledger["log_start_offset"] == LEDGER_DELETE_BEFORE
    assert ledger["end_offset"] == LEDGER_RECORDS
    assert ledger["lag"] == LEDGER_RECORDS - LEDGER_COMMIT
    assert ledger["flags"] == ["committed_below_log_start"]
    payments = rows["payments", 0]
    assert payments["committed_offset"] == AHEAD_COMMIT
    assert payments["end_offset"] == PAYMENTS_RECORDS
    assert payments["lag"] == 0
    assert payments["flags"] == ["committed_ahead_of_end"]


def test_find_groups_by_topic_returns_each_group_with_its_total_lag(tools, cluster):
    outcome = _run(tools, FIND, {"topic_name": "orders"})
    _ok(outcome)
    rows = {r["group_id"]: r for r in _rows(outcome)}
    assert sorted(rows) == ["order-audit", "order-processor"]
    assert rows["order-processor"]["total_lag"] == 1100
    assert rows["order-processor"]["member_count"] == 0
    assert _size(rows["order-processor"]["committed_partitions"]) == 3
    assert rows["order-audit"]["total_lag"] == 0
    assert rows["order-audit"]["member_count"] == 1
    assert _size(rows["order-audit"]["assigned_partitions"]) == 3
    # 213 groups exist; the scan stops at the first 200 by name.
    view = _view(outcome)
    assert view["incomplete"] is True
    meta = json.dumps({k: v for k, v in view.items() if k != "content"}, default=str)
    groups = len(_wait_all({0: cluster["admin"].list_consumer_groups()})[0].valid)
    assert groups >= 213
    assert "200" in meta and str(groups) in meta, meta


def test_brokers_and_overview_describe_the_single_node(tools, cluster):
    brokers = _run(tools, BROKERS)
    _ok(brokers)
    (row,) = _rows(brokers)
    assert row["id"] == 1
    assert (row["host"], row["port"]) == ("localhost", int(SASL.rsplit(":", 1)[1]))
    assert row["is_controller"] is True
    overview = _run(tools, OVERVIEW)
    _ok(overview)
    (summary,) = _rows(overview)
    assert summary["broker_count"] == 1
    assert summary["controller_id"] == 1
    assert summary["cluster_id"]
    assert summary["under_replicated_partitions"] == 0
    assert summary["offline_partitions"] == 0
    assert list(summary["offline_broker_ids"]) == []
    metadata = cluster["admin"].list_topics(timeout=30)
    internal = {name for name in metadata.topics if name.startswith("__")}
    all_parts = sum(len(t.partitions) for t in metadata.topics.values())
    user_parts = sum(
        len(t.partitions) for n, t in metadata.topics.items() if n not in internal
    )
    # Whether internal topics count is not fixed by the interface.
    assert summary["topic_count"] in (
        len(metadata.topics),
        len(metadata.topics) - len(internal),
    )
    assert summary["partition_count"] in (all_parts, user_parts)


def test_configs_read_retention_and_never_show_sensitive_values(tools):
    outcome = _run(
        tools, CONFIGS, {"resource_type": "topic", "resource_name": "orders"}
    )
    _ok(outcome)
    by = {r["name"]: r for r in _rows(outcome)}
    assert by["retention.ms"]["value"] == RETENTION_MS
    assert by["retention.ms"]["is_default"] is False
    broker = _run(tools, CONFIGS, {"resource_type": "broker", "resource_name": "1"})
    _ok(broker)
    for row in _rows(broker):
        if row["is_sensitive"]:
            assert row["value"] is None, row
    for outcome_ in (outcome, broker):
        text = _text(outcome_)
        for secret in (ADMIN_PASSWORD, OPSPILOT_PASSWORD, NOBODY_PASSWORD):
            assert secret not in text
            assert secret.encode() not in (outcome_.evidence.raw or b"")


def test_a_1200_partition_topic_keeps_1000_rows(tools):
    outcome = _run(tools, TOPIC, {"topic_name": WIDE})
    _ok(outcome)
    (row,) = _rows(outcome)
    assert [p["partition"] for p in row["partitions"]] == list(range(1000))
    assert _view(outcome)["incomplete"] is True


def test_listings_cover_the_seeded_topics_and_groups(tools):
    topics = _run(tools, TOPICS)
    _ok(topics)
    by = {r["topic"]: r for r in _rows(topics)}
    assert {"orders", "payments", "ledger", WIDE} <= set(by)
    assert _size(by["orders"]["partitions"]) == 3
    assert by["orders"]["internal"] is False
    if "__consumer_offsets" in by:
        assert by["__consumer_offsets"]["internal"] is True
    groups = _run(tools, CONSUMERS)
    _ok(groups)
    states = {r["group_id"]: _name(r["state"]) for r in _rows(groups)}
    assert states["order-processor"] == "EMPTY"
    assert states["order-audit"] == "STABLE"


# -- error classification with real responses ----------------------------------------


def test_an_account_without_acls_is_source_forbidden(k8s_ca):
    nobody = _executor(_env(k8s_ca, **_sasl("nobody", NOBODY_PASSWORD)))
    outcome = _run(nobody, BROKERS)
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN"), _view(
        outcome
    )
    outcome = _run(nobody, GROUP, {"group_id": "order-processor"})
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN"), _view(
        outcome
    )


def test_a_wrong_password_is_source_forbidden(k8s_ca):
    wrong = _executor(_env(k8s_ca, **_sasl("opspilot", "wrong-pw-567")))
    outcome = _run(wrong, BROKERS)
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN"), _view(
        outcome
    )
    assert "wrong-pw-567" not in _text(outcome)


def test_an_unreachable_port_is_unavailable_or_a_timeout(k8s_ca):
    started = time.monotonic()
    outcome = _run(_executor(_env(k8s_ca, "localhost:1")), BROKERS)
    assert outcome.status in ("error", "timeout"), _view(outcome)
    if outcome.status == "error":
        assert outcome.reason == "SOURCE_UNAVAILABLE", _view(outcome)
    assert time.monotonic() - started < 45
    _observe(f"unreachable bootstrap: {outcome.status}/{outcome.reason}")


def test_unknown_topics_and_groups_are_no_data(tools):
    _no_data_with(_run(tools, TOPIC, {"topic_name": MISSING}), "TOPIC_NOT_FOUND")
    _no_data_with(_run(tools, FIND, {"topic_name": MISSING}), "TOPIC_NOT_FOUND")
    _no_data_with(_run(tools, GROUP, {"group_id": MISSING}), "GROUP_NOT_FOUND")


# -- read-only, first layer: the tools on an anonymous super-user listener ------------


def test_looking_up_a_missing_topic_never_creates_it(anonymous, cluster):
    admin = cluster["admin"]
    before = _topic_names(admin)
    assert MISSING not in before
    _no_data_with(_run(anonymous, TOPIC, {"topic_name": MISSING}), "TOPIC_NOT_FOUND")
    outcome = _run(
        anonymous, CONFIGS, {"resource_type": "topic", "resource_name": MISSING}
    )
    assert outcome.status in ("no_data", "error"), _view(outcome)
    _no_data_with(_run(anonymous, FIND, {"topic_name": MISSING}), "TOPIC_NOT_FOUND")
    # The rest of the tools on the same listener create nothing either.
    for tool in TOOLS:
        _run(anonymous, tool, PARAMS[tool])
    time.sleep(3)
    after = _topic_names(admin)
    assert MISSING not in after
    assert after == before


# -- read-only, second layer: what the opspilot identity itself may do ---------------


def _errors_of(futures: Mapping[Any, Any]) -> list[str]:
    from confluent_kafka import KafkaException

    names = []
    for future in futures.values():
        try:
            future.result(timeout=30)
            names.append("ACCEPTED")
        except KafkaException as error:
            names.append(error.args[0].name())
    return names


def test_the_opspilot_identity_cannot_produce(cluster):
    from confluent_kafka import Producer

    errors: list[str] = []
    producer = Producer(
        {**_conf("opspilot", OPSPILOT_PASSWORD), "message.timeout.ms": 15000}
    )
    producer.produce(
        "orders",
        value=b"must-not-land-567",
        partition=0,
        on_delivery=lambda err, _msg: errors.append(err.name() if err else "DELIVERED"),
    )
    producer.flush(30)
    assert errors == ["TOPIC_AUTHORIZATION_FAILED"], errors


def test_the_opspilot_identity_cannot_read_messages(cluster):
    from confluent_kafka import Consumer, TopicPartition

    seen_values: list[bytes] = []
    errors: list[str] = []
    for group, mode in (
        ("opspilot-probe-567", "subscribe"),
        ("opspilot-assign-567", "assign"),
    ):
        consumer = Consumer(
            {
                **_conf("opspilot", OPSPILOT_PASSWORD),
                "group.id": group,
                "auto.offset.reset": "earliest",
                "enable.auto.commit": False,
                "error_cb": lambda err: errors.append(err.name()),
            }
        )
        if mode == "subscribe":
            consumer.subscribe(["orders"])
        else:
            consumer.assign([TopicPartition("orders", 0, 0)])
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            message = consumer.poll(1.0)
            if message is None:
                continue
            if message.error() is not None:
                errors.append(message.error().name())
            elif message.value() is not None:
                seen_values.append(message.value())
        consumer.close()
    assert seen_values == []
    assert any("AUTHORIZATION_FAILED" in name for name in errors), errors
    _observe(f"opspilot consumer errors: {sorted(set(errors))}")


def test_the_opspilot_identity_cannot_change_topics_configs_or_groups(cluster):
    from confluent_kafka import ConsumerGroupTopicPartitions, TopicPartition
    from confluent_kafka.admin import (
        AlterConfigOpType,
        ConfigEntry,
        ConfigResource,
        NewTopic,
        ResourceType,
    )

    reader = _admin("opspilot", OPSPILOT_PASSWORD)
    attempts = {
        "create_topics": reader.create_topics(
            [NewTopic("opspilot-made-567", 1, 1)], request_timeout=20
        ),
        "delete_topics": reader.delete_topics(["orders"], request_timeout=20),
        "incremental_alter_configs": reader.incremental_alter_configs(
            [
                ConfigResource(
                    ResourceType.TOPIC,
                    "orders",
                    incremental_configs=[
                        ConfigEntry(
                            "retention.ms",
                            "1000",
                            incremental_operation=AlterConfigOpType.SET,
                        )
                    ],
                )
            ],
            request_timeout=20,
        ),
        "delete_consumer_groups": reader.delete_consumer_groups(
            ["order-processor"], request_timeout=20
        ),
        "alter_consumer_group_offsets": reader.alter_consumer_group_offsets(
            [
                ConsumerGroupTopicPartitions(
                    "order-processor", [TopicPartition("orders", 0, 0)]
                )
            ],
            request_timeout=20,
        ),
        "delete_records": reader.delete_records(
            [TopicPartition("orders", 0, 1)], request_timeout=20
        ),
    }
    outcomes = {name: _errors_of(futures) for name, futures in attempts.items()}
    for name, errors in outcomes.items():
        assert errors and all("AUTHORIZATION_FAILED" in e for e in errors), (
            name,
            errors,
        )
    _observe(f"opspilot write attempts: {json.dumps(outcomes, sort_keys=True)}")
    admin = cluster["admin"]
    assert "opspilot-made-567" not in _topic_names(admin)
    assert "orders" in _topic_names(admin)
    (configs,) = _wait_all(
        admin.describe_configs([ConfigResource(ResourceType.TOPIC, "orders")])
    )
    assert configs["retention.ms"].value == RETENTION_MS
    assert _committed(admin, "order-processor") == {
        ("orders", p): o for p, o in PROCESSOR_OFFSETS.items()
    }


# -- last: nothing was written, and no message content ever came back ------------------


def test_zz_no_tool_wrote_anything_or_returned_message_content(cluster):
    admin = cluster["admin"]
    assert _snapshot(admin) == cluster["snapshot"]
    assert len(OUTCOMES) >= len(TOOLS)
    for outcome in OUTCOMES:
        assert SENTINEL not in _text(outcome), outcome.operation.tool
        assert META_SENTINEL not in _text(outcome), outcome.operation.tool
        raw = outcome.evidence.raw if outcome.evidence is not None else b""
        assert SENTINEL.encode() not in (raw or b""), outcome.operation.tool
        assert META_SENTINEL.encode() not in (raw or b""), outcome.operation.tool
