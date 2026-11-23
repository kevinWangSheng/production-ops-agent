"""Independent contract tests for tool expansion C3 (#567): the eight Kafka
read-only tools ``list_kafka_consumers``, ``describe_consumer_group``,
``list_topics``, ``describe_topic``, ``find_consumer_groups_by_topic``,
``list_brokers``, ``describe_configs`` and ``cluster_overview``.

Written from the C3 task record (docs/tasks/2026-11-20-tool-expansion-c3.md,
the public interface), decision table r8 (R8-20, R8-25) and r11 (C3-U1 = a,
C3-U2 = a, C3-1 ... C3-14) and the C3 pre-review, without reading the
implementation. Red until the implementation lands; collection stays green
(``opspilot.tools.kafka_admin`` is imported inside the tests that need it).

Every behavioural test goes through the public path: build the
``otel-demo-ext`` profile from an environment mapping, call a tool through
the worker's executor, and assert on the returned view, the refusal code,
what the injected admin-client factory received (the configuration and
every method call), or what the evidence and trace carry. No broker, no
model. The read-only guarantees are also checked statically (AST scan of
``opspilot/`` and the ruff ban list).

Baseline (C6a / #570 not merged when this was written, main ``d28b4631``):

* The pinned revisions below are the extension's existing values on
  ``d28b4631``. C6a must keep them byte for byte too (its own C5-14 rule);
  everything that depends on where C6a's SQL tools sit is asserted
  relatively instead -- the eight Kafka tools are one block, removing it gives
  the face without Kafka, and with no knowledge/task-list switch the block is
  last.

Seams assumed beyond the interface (each also marked ``# ASSUMPTION:``):

* The worker builds its ``KafkaTransport`` through the class
  ``opspilot.tools.kafka_admin.KafkaTransport`` with ``client_factory`` left
  at its default; the tests wrap ``KafkaTransport.__init__`` to pass the fake
  factory (the injection point the interface names) and wrap
  ``KafkaTransport.fetch`` (the ``ReadOnlyTransport`` protocol) only to see
  the ``TransportRequest`` selector.
* ``select_profile(env)`` reads ``OPSPILOT_KAFKA_BOOTSTRAP_SERVERS`` from the
  mapping it is given; the worker reads every ``OPSPILOT_KAFKA_*`` from the
  same mapping, and a bad configuration ends worker start with
  ``SystemExit`` whose text contains the startup code (as B1/C5).
* Pre-request refusals are ``INVALID_PARAMS`` with the code at the start of
  the first ``notes`` entry (interface: "码放 notes 开头").
* ``no_data`` answers carry ``TOPIC_NOT_FOUND`` / ``GROUP_NOT_FOUND`` in
  ``notes``; truncation is reported as ``incomplete: true`` in the view.
* The view's ``content`` is the ``results`` rows; the executor's
  ``backend_<row_unit>_returned`` field names the row unit.
* Group state and type are projected as their names (``EMPTY``, ``STABLE``,
  ``CLASSIC`` ...); the tests compare case-insensitively on the last dotted
  part, so ``"Empty"`` or ``"ConsumerGroupState.EMPTY"`` also pass.
* ``assigned_partitions`` / ``committed_partitions`` may be counts or lists;
  the tests compare their sizes.

The fake admin client returns the real ``confluent_kafka`` model types when
the package is importable (CI, after C3-1 adds it), so ``isinstance``,
enum comparisons and ``except KafkaException`` in the product behave as
against the real client; without the package it falls back to look-alikes
(only for collection -- the product module needs the package anyway). The
product never sees ``confluent_kafka`` from this file except through those
returned objects.

All synthetic credentials and canaries are low-entropy placeholders.
"""

from __future__ import annotations

import ast
import concurrent.futures
import enum
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opspilot import tracing
from opspilot.tools import ToolRequest
from opspilot.tools.outcomes import Window
from opspilot.tools.profiles import PROFILE_ENV, ToolProfile, select_profile
from opspilot.tools.registry import canonical
from opspilot.tracing import check_lab_target
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import NOW, TARGET_ID, FakeStore, _lease

ROOT = Path(__file__).resolve().parents[1]
EXT_PROFILE = "otel-demo-ext"
MIB = 1024 * 1024

CONSUMERS = "list_kafka_consumers"
GROUP = "describe_consumer_group"
TOPICS = "list_topics"
TOPIC = "describe_topic"
FIND = "find_consumer_groups_by_topic"
BROKERS = "list_brokers"
CONFIGS = "describe_configs"
OVERVIEW = "cluster_overview"
TOOLS = (CONSUMERS, GROUP, TOPICS, TOPIC, FIND, BROKERS, CONFIGS, OVERVIEW)

READ_METHODS = frozenset(
    {
        "list_topics",
        "describe_topics",
        "describe_cluster",
        "list_consumer_groups",
        "describe_consumer_groups",
        "list_consumer_group_offsets",
        "list_offsets",
        "describe_configs",
    }
)
# What each tool may call (interface table); ``describe_topic`` and
# ``describe_consumer_group`` also by their optional flags.
ALLOWED_CALLS = {
    CONSUMERS: {"list_consumer_groups"},
    GROUP: {"describe_consumer_groups", "list_consumer_group_offsets", "list_offsets"},
    TOPICS: {"list_topics"},
    TOPIC: {"describe_topics", "describe_configs"},
    FIND: {
        "describe_topics",
        "list_consumer_groups",
        "describe_consumer_groups",
        "list_consumer_group_offsets",
        "list_offsets",
    },
    BROKERS: {"describe_cluster"},
    CONFIGS: {"describe_configs"},
    OVERVIEW: {"describe_cluster", "list_topics"},
}
MINIMAL: dict[str, dict[str, object]] = {
    CONSUMERS: {},
    GROUP: {"group_id": "order-processor"},
    TOPICS: {},
    TOPIC: {"topic_name": "orders"},
    FIND: {"topic_name": "orders"},
    BROKERS: {},
    CONFIGS: {"resource_type": "topic", "resource_name": "orders"},
    OVERVIEW: {},
}
ROW_UNIT = {
    CONSUMERS: "groups",
    GROUP: "groups",
    TOPICS: "topics",
    TOPIC: "topics",
    FIND: "groups",
    BROKERS: "brokers",
    CONFIGS: "configs",
    OVERVIEW: "summary",
}
VERB = {
    CONSUMERS: "list",
    GROUP: "get",
    TOPICS: "list",
    TOPIC: "get",
    FIND: "get",
    BROKERS: "list",
    CONFIGS: "get",
    OVERVIEW: "get",
}
LAG_ROW_KEYS = {
    "topic",
    "partition",
    "committed_offset",
    "end_offset",
    "log_start_offset",
    "lag",
    "flags",
}

BOOTSTRAP = "localhost:9092"
KAFKA_USERNAME = "opspilot"
KAFKA_PASSWORD = "kafka-pw-aaaa567"
ES_API_KEY = "es-ro-bbbb567"
LOKI_BEARER = "loki-ro-cccc567"
VL_BEARER = "vlogs-ro-dddd567"
PROM_USERNAME = "investigator"
PROM_PASSWORD = "prom-pw-eeee567"
K8S_TOKEN = "k8s-ro-ffff567"
K8S_API = "https://127.0.0.1:16443"
CANARY = "gggg567"
META_SENTINEL = "commit-meta-hhhh567"
RUN_ID = "c3-run"

KAFKA_ENV_NAMES = (
    "OPSPILOT_KAFKA_BOOTSTRAP_SERVERS",
    "OPSPILOT_KAFKA_SECURITY_PROTOCOL",
    "OPSPILOT_KAFKA_SASL_MECHANISM",
    "OPSPILOT_KAFKA_USERNAME",
    "OPSPILOT_KAFKA_PASSWORD",
    "OPSPILOT_KAFKA_CA_FILE",
)
KAFKA_CREDENTIAL_NAMES = (
    "OPSPILOT_KAFKA_SASL_MECHANISM",
    "OPSPILOT_KAFKA_USERNAME",
    "OPSPILOT_KAFKA_PASSWORD",
    "OPSPILOT_KAFKA_CA_FILE",
)
OTHER_BACKEND_ENV = (
    "OPSPILOT_ES_URL",
    "OPSPILOT_ES_API_KEY",
    "OPSPILOT_LOKI_URL",
    "OPSPILOT_LOKI_BEARER_TOKEN",
    "OPSPILOT_VICTORIALOGS_URL",
    "OPSPILOT_VICTORIALOGS_BEARER_TOKEN",
    "OPSPILOT_K8S_LIVE_METRICS",
    "OPSPILOT_KNOWLEDGE_TOOL",
    "OPSPILOT_TODO_WRITE",
)

# -- pinned surfaces (main d28b4631, before any C3 code) ---------------------------

BASE_TOOL_SCHEMA_REVISION = "otel-demo-74b3ea2ac593"
SWITCHES = {
    "elasticsearch": ("OPSPILOT_ES_URL", "http://127.0.0.1:9200"),
    "loki": ("OPSPILOT_LOKI_URL", "http://127.0.0.1:3100"),
    "victorialogs": ("OPSPILOT_VICTORIALOGS_URL", "http://127.0.0.1:9428"),
    "live_metrics": ("OPSPILOT_K8S_LIVE_METRICS", "1"),
    "knowledge": ("OPSPILOT_KNOWLEDGE_TOOL", "1"),
    "todo": ("OPSPILOT_TODO_WRITE", "1"),
}
ALL = tuple(SWITCHES)
PINNED = {
    (): "otel-demo-ext-dd66f68d7e78",
    ("elasticsearch",): "otel-demo-ext-3a621c50d364",
    ("loki",): "otel-demo-ext-f972b2da8df7",
    ("victorialogs",): "otel-demo-ext-98bf8bd3c019",
    ("live_metrics",): "otel-demo-ext-9af003c87a85",
    ("knowledge",): "otel-demo-ext-fdb5c78a5aa8",
    ("todo",): "otel-demo-ext-a0d2215188b0",
    ALL: "otel-demo-ext-60c8fb695f3d",
}
BACKENDS_ONLY = ("elasticsearch", "loki", "victorialogs", "live_metrics")


# -- the admin-client model types (real when importable) ---------------------------


def _ck() -> Any:
    try:
        import confluent_kafka
        import confluent_kafka.admin  # noqa: F401
    except ImportError:
        return None
    return confluent_kafka


class _LocalState(enum.Enum):
    UNKNOWN = 0
    PREPARING_REBALANCING = 1
    COMPLETING_REBALANCING = 2
    STABLE = 3
    DEAD = 4
    EMPTY = 5


class _LocalType(enum.Enum):
    UNKNOWN = 0
    CONSUMER = 1
    CLASSIC = 2


class _LocalSource(enum.Enum):
    UNKNOWN_CONFIG = 0
    DYNAMIC_TOPIC_CONFIG = 1
    DYNAMIC_BROKER_CONFIG = 2
    STATIC_BROKER_CONFIG = 4
    DEFAULT_CONFIG = 5


class _LocalError:
    def __init__(self, name: str, reason: str) -> None:
        self._name, self._reason = name, reason

    def code(self) -> int:
        return -abs(hash(self._name)) % 1000

    def name(self) -> str:
        return self._name

    def str(self) -> str:
        return self._reason

    def __str__(self) -> str:
        return f'KafkaError{{code={self._name},str="{self._reason}"}}'


class _LocalKafkaException(Exception):
    pass


class _LocalUuid:
    def __str__(self) -> str:
        return "AAAAAAAAAAAAAAAAAAAAAQ"


class Models:
    """Builds the objects the admin client returns."""

    def __init__(self) -> None:
        self.ck = _ck()

    # enums -------------------------------------------------------------------
    def state(self, name: str) -> Any:
        if self.ck:
            return getattr(self.ck.ConsumerGroupState, name)
        return getattr(_LocalState, name)

    def group_type(self, name: str) -> Any:
        if self.ck:
            return getattr(self.ck.ConsumerGroupType, name)
        return getattr(_LocalType, name)

    def source(self, name: str) -> Any:
        if self.ck:
            return getattr(self.ck.admin.ConfigSource, name)
        return getattr(_LocalSource, name)

    # values ------------------------------------------------------------------
    def node(self, id: int, host: str = "localhost", port: int = 9092, rack=None):
        if self.ck:
            return self.ck.Node(id=id, host=host, port=port, rack=rack)
        return SimpleNamespace(
            id=id, id_string=str(id), host=host, port=port, rack=rack
        )

    def tp(self, topic: str, partition: int, offset: int = -1001, metadata=None):
        if self.ck:
            # The real constructor wants a str; "" is "no metadata".
            return self.ck.TopicPartition(topic, partition, offset, metadata or "")
        return SimpleNamespace(
            topic=topic, partition=partition, offset=offset, metadata=metadata,
            leader_epoch=None, error=None,
        )  # fmt: skip

    def error(self, name: str, reason: str = "") -> Exception:
        if self.ck:
            code = getattr(self.ck.KafkaError, name)
            return self.ck.KafkaException(self.ck.KafkaError(code, reason or name))
        return _LocalKafkaException(_LocalError(name, reason or name))

    def uuid(self):
        if self.ck:
            return self.ck.Uuid(1, 2)
        return _LocalUuid()

    def partition_info(self, id, leader, replicas, isr):
        args = dict(id=id, leader=leader, replicas=replicas, isr=isr)
        if self.ck:
            return self.ck.TopicPartitionInfo(**args)
        return SimpleNamespace(**args)

    def topic_description(self, name, partitions, internal=False):
        args = dict(
            name=name, topic_id=self.uuid(), is_internal=internal, partitions=partitions
        )
        if self.ck:
            return self.ck.admin.TopicDescription(**args)
        return SimpleNamespace(authorized_operations=None, **args)

    def group_listing(self, group_id, state, gtype, simple):
        args = dict(
            group_id=group_id,
            is_simple_consumer_group=simple,
            state=self.state(state),
            type=self.group_type(gtype),
        )
        if self.ck:
            return self.ck.admin.ConsumerGroupListing(**args)
        return SimpleNamespace(**args)

    def list_groups_result(self, listings):
        if self.ck:
            return self.ck.admin.ListConsumerGroupsResult(valid=listings, errors=[])
        return SimpleNamespace(valid=listings, errors=[])

    def member(self, member_id, client_id, host, instance, tps):
        if self.ck:
            assignment = self.ck.admin.MemberAssignment(tps)
            return self.ck.admin.MemberDescription(
                member_id=member_id, client_id=client_id, host=host,
                assignment=assignment, group_instance_id=instance,
            )  # fmt: skip
        return SimpleNamespace(
            member_id=member_id, client_id=client_id, host=host,
            assignment=SimpleNamespace(topic_partitions=tps),
            group_instance_id=instance, target_assignment=None,
        )  # fmt: skip

    def group_description(
        self, group_id, simple, members, assignor, state, coordinator, gtype
    ):
        args = dict(
            group_id=group_id, is_simple_consumer_group=simple, members=members,
            partition_assignor=assignor, state=self.state(state),
            coordinator=coordinator, type=self.group_type(gtype),
        )  # fmt: skip
        if self.ck:
            return self.ck.admin.ConsumerGroupDescription(**args)
        return SimpleNamespace(authorized_operations=None, **args)

    def cgtp(self, group_id, tps):
        if self.ck:
            return self.ck.ConsumerGroupTopicPartitions(group_id, tps)
        return SimpleNamespace(group_id=group_id, topic_partitions=tps)

    def cluster_result(self, controller, nodes, cluster_id):
        args = dict(controller=controller, nodes=nodes, cluster_id=cluster_id)
        if self.ck:
            return self.ck.admin.DescribeClusterResult(**args)
        return SimpleNamespace(authorized_operations=None, **args)

    def offsets_result(self, offset):
        if self.ck:
            return self.ck.admin.ListOffsetsResultInfo(offset, -1, -1)
        return SimpleNamespace(offset=offset, timestamp=-1, leader_epoch=None)

    def config_entry(self, name, value, *, sensitive, default, read_only, source):
        args = dict(
            name=name, value=value, source=self.source(source),
            is_read_only=read_only, is_default=default, is_sensitive=sensitive,
        )  # fmt: skip
        if self.ck:
            return self.ck.admin.ConfigEntry(**args)
        return SimpleNamespace(is_synonym=False, synonyms={}, **args)

    def metadata(self, cluster_id, controller_id, brokers, topics):
        if self.ck:
            admin = self.ck.admin
            meta = admin.ClusterMetadata()
            meta.cluster_id, meta.controller_id = cluster_id, controller_id
            for bid, (host, port, _rack) in brokers.items():
                b = admin.BrokerMetadata()
                b.id, b.host, b.port = bid, host, port
                meta.brokers[bid] = b
            for name, spec in topics.items():
                t = admin.TopicMetadata()
                t.topic, t.error = name, None
                for pid, (leader, replicas, isr) in spec["partitions"].items():
                    p = admin.PartitionMetadata()
                    p.id, p.leader, p.replicas, p.isrs, p.error = (
                        pid, leader, list(replicas), list(isr), None,
                    )  # fmt: skip
                    t.partitions[pid] = p
                meta.topics[name] = t
            return meta
        return SimpleNamespace(
            cluster_id=cluster_id,
            controller_id=controller_id,
            brokers={
                bid: SimpleNamespace(id=bid, host=h, port=p)
                for bid, (h, p, _r) in brokers.items()
            },
            topics={
                name: SimpleNamespace(
                    topic=name,
                    error=None,
                    partitions={
                        pid: SimpleNamespace(
                            id=pid,
                            leader=lead,
                            replicas=list(rep),
                            isrs=list(isr),
                            error=None,
                        )  # fmt: skip
                        for pid, (lead, rep, isr) in spec["partitions"].items()
                    },
                )
                for name, spec in topics.items()
            },
        )


def _name(value: object) -> str:
    """An enum-ish value's bare upper-case name."""
    return str(getattr(value, "name", value)).rsplit(".", 1)[-1].upper()


def _size(value: object) -> int:
    if isinstance(value, (list, tuple, set)):
        return len(value)
    assert isinstance(value, int) and not isinstance(value, bool), value
    return value


# -- the fake cluster and its admin client ------------------------------------------


class _Future(concurrent.futures.Future):
    def __init__(self, log: list) -> None:
        super().__init__()
        self._log = log

    def result(self, timeout=None):
        self._log.append(timeout)
        return super().result(timeout)


class FakeKafka:
    """A fake cluster behind an admin-client factory that records every
    configuration it is handed and every method call on every client."""

    def __init__(self) -> None:
        self.m = Models()
        self.configs: list[dict[str, Any]] = []
        self.calls: list[tuple[str, tuple, dict]] = []
        self.other: list[str] = []  # any non-read method touched
        self.entered = 0
        self.exited = 0
        self.result_timeouts: list[object] = []
        self.requests: list[Any] = []  # TransportRequest seen by fetch
        self.cluster_id = "c3-cluster-id"
        self.controller = 2
        self.brokers: dict[int, tuple[str, int, str | None]] = {
            1: ("broker-1.kafka.svc", 9092, "rack-a"),
            2: ("broker-2.kafka.svc", 9092, None),
            3: ("broker-3.kafka.svc", 9092, "rack-c"),
        }
        self.topics: dict[str, dict[str, Any]] = {}
        self.ends: dict[tuple[str, int], tuple[int, int]] = {}  # (start, end)
        self.groups: dict[str, dict[str, Any]] = {}
        self.configs_by_resource: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.fail: dict[str, Exception] = {}
        self.hang: set[str] = set()
        self.missing_group_error = False  # GROUP_ID_NOT_FOUND instead of DEAD

    # data helpers ------------------------------------------------------------
    def topic(self, name, partitions, *, internal=False, ends=None, replicas=(1, 2, 3)):
        self.topics[name] = {
            "internal": internal,
            "partitions": {
                p: (replicas[0], tuple(replicas), tuple(replicas))
                for p in range(partitions)
            },
        }
        for p in range(partitions):
            self.ends[(name, p)] = (0, (ends or {}).get(p, 0))

    def group(
        self, gid, *, state="EMPTY", gtype="CLASSIC", simple=False, members=(),
        offsets=None, assignor="range",
    ):  # fmt: skip
        self.groups[gid] = {
            "state": state, "type": gtype, "simple": simple,
            "members": list(members), "offsets": dict(offsets or {}),
            "assignor": assignor,
        }  # fmt: skip

    def config(self, restype, name, entries):
        self.configs_by_resource[(restype, name)] = entries

    # the factory ---------------------------------------------------------------
    def factory(self, conf):
        self.configs.append(dict(conf))
        return _Client(self)

    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def future(self, method: str, value=None, error: Exception | None = None):
        fut = _Future(self.result_timeouts)
        if method in self.hang:
            # Never answered; a safety net keeps a missing timeout from
            # hanging the suite forever.
            timer = threading.Timer(
                12.0, lambda: fut.done() or fut.set_exception(RuntimeError("hung"))
            )
            timer.daemon = True
            timer.start()
            return fut
        error = self.fail.get(method, error)
        if error is not None:
            fut.set_exception(error)
        else:
            fut.set_result(value)
        return fut

    def node(self, bid: int):
        host, port, rack = self.brokers[bid]
        return self.m.node(bid, host, port, rack)


def _restype(resource: Any) -> str:
    return _name(getattr(resource, "restype", "")).lower()


def _spec_kind(spec: Any) -> str:
    name = type(spec).__name__.lower()
    if "latest" in name:
        return "latest"
    if "earliest" in name:
        return "earliest"
    value = getattr(spec, "_value", None)
    return {-1: "latest", -2: "earliest"}.get(value, repr(spec))


class _Client:
    _ALLOWED_EXTRA = {"poll", "close"}

    def __init__(self, fake: FakeKafka) -> None:
        self._fake = fake

    def __enter__(self):
        self._fake.entered += 1
        return self

    def __exit__(self, *exc):
        self._fake.exited += 1
        return False

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        fake = self.__dict__["_fake"]
        if name in self._ALLOWED_EXTRA:
            return lambda *a, **k: 0
        fake.other.append(name)
        raise AttributeError(f"fake admin client refuses {name}")

    def _record(self, name, args, kwargs):
        self._fake.calls.append((name, args, dict(kwargs)))

    # the eight reads -----------------------------------------------------------
    def list_topics(self, *args, **kwargs):
        self._record("list_topics", args, kwargs)
        f = self._fake
        if "list_topics" in f.fail:
            raise f.fail["list_topics"]
        return f.m.metadata(f.cluster_id, f.controller, f.brokers, f.topics)

    def describe_topics(self, topics, **kwargs):
        self._record("describe_topics", (topics,), kwargs)
        f = self._fake
        out = {}
        for name in list(topics.topic_names):
            spec = f.topics.get(name)
            if spec is None:
                out[name] = f.future(
                    "describe_topics", error=f.m.error("UNKNOWN_TOPIC_OR_PART")
                )
                continue
            alive = set(f.brokers)
            partitions = [
                f.m.partition_info(
                    pid,
                    f.node(leader) if leader in alive else None,
                    [f.node(r) if r in alive else f.m.node(r, "", -1) for r in reps],
                    [f.node(r) for r in isr if r in alive],
                )
                for pid, (leader, reps, isr) in sorted(spec["partitions"].items())
            ]
            out[name] = f.future(
                "describe_topics",
                f.m.topic_description(name, partitions, spec["internal"]),
            )
        return out

    def describe_cluster(self, **kwargs):
        self._record("describe_cluster", (), kwargs)
        f = self._fake
        nodes = [f.node(b) for b in sorted(f.brokers)]
        return f.future(
            "describe_cluster",
            f.m.cluster_result(f.node(f.controller), nodes, f.cluster_id),
        )

    def list_consumer_groups(self, **kwargs):
        self._record("list_consumer_groups", (), kwargs)
        f = self._fake
        listings = [
            f.m.group_listing(gid, g["state"], g["type"], g["simple"])
            for gid, g in f.groups.items()
        ]
        return f.future("list_consumer_groups", f.m.list_groups_result(listings))

    def describe_consumer_groups(self, group_ids, **kwargs):
        self._record("describe_consumer_groups", (list(group_ids),), kwargs)
        f = self._fake
        out = {}
        for gid in group_ids:
            g = f.groups.get(gid)
            if g is None:
                if f.missing_group_error:
                    out[gid] = f.future(
                        "describe_consumer_groups",
                        error=f.m.error("GROUP_ID_NOT_FOUND"),
                    )
                else:
                    desc = f.m.group_description(
                        gid, False, [], "", "DEAD", f.node(f.controller), "UNKNOWN"
                    )
                    out[gid] = f.future("describe_consumer_groups", desc)
                continue
            members = [
                f.m.member(mid, cid, host, inst, [f.m.tp(t, p) for t, p in tps])
                for mid, cid, host, inst, tps in g["members"]
            ]
            desc = f.m.group_description(
                gid, g["simple"], members, g["assignor"], g["state"],
                f.node(f.controller), g["type"],
            )  # fmt: skip
            out[gid] = f.future("describe_consumer_groups", desc)
        return out

    def list_consumer_group_offsets(self, requests, **kwargs):
        self._record("list_consumer_group_offsets", (list(requests),), kwargs)
        f = self._fake
        out = {}
        for request in requests:
            gid = request.group_id
            committed = f.groups.get(gid, {}).get("offsets", {})
            wanted = request.topic_partitions
            if wanted:
                keys = [(tp.topic, tp.partition) for tp in wanted]
            else:
                keys = sorted(committed)
            tps = []
            for key in keys:
                offset, meta = committed.get(key, (-1001, None))
                tps.append(f.m.tp(key[0], key[1], offset, meta))
            out[gid] = f.future("list_consumer_group_offsets", f.m.cgtp(gid, tps))
        return out

    def list_offsets(self, topic_partition_offsets, **kwargs):
        items = list(topic_partition_offsets.items())
        self._record(
            "list_offsets",
            ([(tp.topic, tp.partition, _spec_kind(spec)) for tp, spec in items],),
            kwargs,
        )
        f = self._fake
        out = {}
        for tp, spec in items:
            bounds = f.ends.get((tp.topic, tp.partition))
            if bounds is None:
                out[tp] = f.future(
                    "list_offsets", error=f.m.error("UNKNOWN_TOPIC_OR_PART")
                )
                continue
            value = bounds[0] if _spec_kind(spec) == "earliest" else bounds[1]
            out[tp] = f.future("list_offsets", f.m.offsets_result(value))
        return out

    def describe_configs(self, resources, **kwargs):
        resources = list(resources)
        self._record(
            "describe_configs",
            ([(_restype(r), str(r.name)) for r in resources],),
            kwargs,
        )
        f = self._fake
        out = {}
        for resource in resources:
            key = (_restype(resource), str(resource.name))
            entries = f.configs_by_resource.get(key)
            if entries is None:
                out[resource] = f.future(
                    "describe_configs", error=f.m.error("UNKNOWN_TOPIC_OR_PART")
                )
                continue
            value = {
                e["name"]: f.m.config_entry(
                    e["name"],
                    e["value"],
                    sensitive=e.get("sensitive", False),
                    default=e.get("default", False),
                    read_only=e.get("read_only", False),
                    source=e.get("source", "DYNAMIC_TOPIC_CONFIG"),
                )  # fmt: skip
                for e in entries
            }
            out[resource] = f.future("describe_configs", value)
        return out


def _standard(fake: FakeKafka) -> FakeKafka:
    """orders (3 partitions, 1000/800/600), payments (1, 5 records), the
    internal offsets topic; the stopped ``order-processor`` (400/800/100,
    lag 1100, no members) and the caught-up ``order-audit`` (one member)."""
    fake.topic("orders", 3, ends={0: 1000, 1: 800, 2: 600})
    fake.topic("payments", 1, ends={0: 5})
    fake.topic("__consumer_offsets", 2, internal=True)
    fake.group(
        "order-processor",
        state="EMPTY",
        offsets={
            ("orders", 0): (400, META_SENTINEL),
            ("orders", 1): (800, META_SENTINEL),
            ("orders", 2): (100, META_SENTINEL),
        },
    )
    fake.group(
        "order-audit",
        state="STABLE",
        members=[
            (
                "audit-1-aaaa",
                "audit-client",
                "/10.0.0.7",
                None,
                [("orders", 0), ("orders", 1), ("orders", 2)],
            )
        ],
        offsets={
            ("orders", p): (end, None) for p, end in ((0, 1000), (1, 800), (2, 600))
        },
    )
    fake.config(
        "topic",
        "orders",
        [
            {"name": "retention.ms", "value": "86400123"},
            {"name": "cleanup.policy", "value": "delete", "default": True,
             "source": "DEFAULT_CONFIG"},
            {"name": "sasl.jaas.config", "value": f"password={CANARY}", "sensitive": True},
        ],
    )  # fmt: skip
    fake.config(
        "broker",
        "1",
        [
            {"name": "num.network.threads", "value": "3", "read_only": True,
             "source": "STATIC_BROKER_CONFIG"},
            {"name": "ssl.keystore.password", "value": CANARY, "sensitive": True,
             "source": "STATIC_BROKER_CONFIG"},
        ],
    )  # fmt: skip
    return fake


# -- fixtures and the public path ---------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_environment(tmp_path, monkeypatch):
    for name in KAFKA_ENV_NAMES + OTHER_BACKEND_ENV:
        monkeypatch.delenv(name, raising=False)
    ca_file = tmp_path / "synthetic-k8s-ca.pem"
    ca_file.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("C3_TEST_K8S_CA_FILE", str(ca_file))
    kafka_ca = tmp_path / "synthetic-kafka-ca.pem"
    kafka_ca.write_text("synthetic kafka ca placeholder\n")
    monkeypatch.setenv("C3_TEST_KAFKA_CA_FILE", str(kafka_ca))


@pytest.fixture
def kafka(monkeypatch) -> Iterator[FakeKafka]:
    from opspilot.tools import kafka_admin

    fake = _standard(FakeKafka())
    transport = kafka_admin.KafkaTransport
    original_init = transport.__init__
    original_fetch = transport.fetch

    # ASSUMPTION: the worker constructs ``KafkaTransport`` with the default
    # ``client_factory``; the fake factory is passed in its place.
    def init(self, *args, client_factory=None, **kwargs):
        original_init(
            self,
            *args,
            client_factory=fake.factory if client_factory is None else client_factory,
            **kwargs,
        )

    def fetch(self, request):
        fake.requests.append(request)
        return original_fetch(self, request)

    monkeypatch.setattr(transport, "__init__", init)
    monkeypatch.setattr(transport, "fetch", fetch)
    yield fake


def _base_env() -> dict[str, str]:
    return {
        PROFILE_ENV: EXT_PROFILE,
        "OPSPILOT_K8S_API_URL": K8S_API,
        "OPSPILOT_K8S_TOKEN": K8S_TOKEN,
        "OPSPILOT_K8S_CA_FILE": os.environ["C3_TEST_K8S_CA_FILE"],
        "OPSPILOT_K8S_NAMESPACE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_OTEL_PROMETHEUS_USERNAME": PROM_USERNAME,
        "OPSPILOT_OTEL_PROMETHEUS_PASSWORD": PROM_PASSWORD,
    }


def _env(bootstrap: str | None = BOOTSTRAP, **overrides: str) -> dict[str, str]:
    """A worker environment; anonymous PLAINTEXT unless overridden."""
    values = _base_env()
    if bootstrap is not None:
        values["OPSPILOT_KAFKA_BOOTSTRAP_SERVERS"] = bootstrap
    values.update(overrides)
    return {k: v for k, v in values.items() if v != ""}


def _sasl_env(**overrides: str) -> dict[str, str]:
    settings = {
        "OPSPILOT_KAFKA_SECURITY_PROTOCOL": "SASL_PLAINTEXT",
        "OPSPILOT_KAFKA_USERNAME": KAFKA_USERNAME,
        "OPSPILOT_KAFKA_PASSWORD": KAFKA_PASSWORD,
    }
    settings.update(overrides)
    return _env(**settings)


def _require_jq() -> None:
    if shutil.which("jq") is None or shutil.which("python3") is None:
        pytest.skip("the extension worker needs jq and python3 on PATH")


def _worker(env: Mapping[str, str], *, deadline_seconds: float = 600):
    _require_jq()
    clock = FakeClock(start=NOW)
    profile = select_profile(env)
    assert isinstance(profile, ToolProfile)
    if env.get("OPSPILOT_KAFKA_BOOTSTRAP_SERVERS"):
        # Positive control: refusal tests are vacuous without the tools.
        names = {s["function"]["name"] for s in profile.face(clock).tool_schemas}
        assert set(TOOLS) <= names, names
    lease = _lease()
    deadline = NOW + timedelta(seconds=deadline_seconds)
    store = FakeStore(lease, deadline=deadline)
    sink = RecordingSink()
    factory = profile.executor_factory(store, sink, clock)
    model_input = profile.face(clock).input_for(
        run_id=str(lease.run_id),
        question="What is the consumer lag on the orders topic?",
        target_id=TARGET_ID,
        deadline=deadline,
        model_requests=12,
    )
    return factory(lease, model_input), sink


FRAME = Window(NOW - timedelta(hours=6), NOW)


def _call(tool: str, params: dict[str, object] | None = None, *, index: int = 0):
    return ToolRequest(
        step_id="c3-step",
        tool_index=index,
        tool_name=tool,
        target_ref=TARGET_ID,
        params=dict(MINIMAL[tool] if params is None else params),
        window=FRAME.as_json(),
    )


def _run(fake: FakeKafka, tool: str, params=None, *, env=None):
    executor, _ = _worker(env or _env())
    return executor.execute(_call(tool, params))


def _view(outcome) -> Mapping[str, Any]:
    assert outcome.model_view is not None
    return outcome.model_view


def _text(outcome) -> str:
    return json.dumps(_view(outcome), ensure_ascii=False, sort_keys=True, default=str)


def _meta(outcome) -> str:
    """The view without its rows: where notes and markers live."""
    return json.dumps(
        {k: v for k, v in _view(outcome).items() if k != "content"},
        ensure_ascii=False,
        default=str,
    )


def _rows(outcome) -> list[Mapping[str, Any]]:
    content = _view(outcome)["content"]
    assert isinstance(content, list), _view(outcome)
    return content


def _one(outcome) -> Mapping[str, Any]:
    rows = _rows(outcome)
    assert len(rows) == 1, rows
    return rows[0]


def _keys(row: Mapping[str, Any]) -> set[str]:
    """A row's projected keys; ``_``-prefixed bookkeeping (e.g. a truncation
    list) is not a projected field."""
    return {k for k in row if not k.startswith("_")}


def _ok(outcome) -> None:
    assert outcome.status == "ok", (outcome.status, outcome.reason, _view(outcome))


def _refused_with(outcome, code: str) -> None:
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), _view(
        outcome
    )
    notes = _view(outcome).get("notes") or []
    assert notes and str(notes[0]).startswith(code), _view(outcome)


def _no_data_with(outcome, code: str) -> None:
    assert outcome.status == "no_data", (outcome.status, outcome.reason, _view(outcome))
    assert code in json.dumps(_view(outcome).get("notes") or [], default=str), _view(
        outcome
    )


def _face(env: Mapping[str, str]):
    return select_profile(env).face(FakeClock(start=NOW))


def _tool_names(env: Mapping[str, str]) -> list[str]:
    return [s["function"]["name"] for s in _face(env).tool_schemas]


def _available(env: Mapping[str, str]) -> Mapping[str, Any]:
    return _face(env).evidence_context(RUN_ID)["data_sources"]["available"]


def _surface_sha256(profile: ToolProfile) -> str:
    face = profile.face(FakeClock(start=NOW))
    blob = {
        "tool_schemas": [dict(item) for item in face.tool_schemas],
        "variant_id": face.variant_id,
        "evidence_context": dict(face.evidence_context(RUN_ID)),
        "versions": profile.versions(),
    }
    return hashlib.sha256(canonical(blob).encode()).hexdigest()


def _web(switches: tuple[str, ...] = (), *, kafka: bool = False) -> dict[str, str]:
    env = {PROFILE_ENV: EXT_PROFILE}
    for name in switches:
        key, value = SWITCHES[name]
        env[key] = value
    if kafka:
        env["OPSPILOT_KAFKA_BOOTSTRAP_SERVERS"] = BOOTSTRAP
    return env


def _flags(switches: tuple[str, ...]) -> dict[str, bool]:
    return {name: name in switches for name in SWITCHES}


# == ⑩ visibility: byte-stable surfaces, the tools only with the address ===========


def test_otel_demo_profile_ignores_kafka_settings():
    plain = select_profile({PROFILE_ENV: "otel-demo"})
    assert plain.versions()["tool_schema_revision"] == BASE_TOOL_SCHEMA_REVISION
    with_kafka = select_profile(
        {
            PROFILE_ENV: "otel-demo",
            "OPSPILOT_KAFKA_BOOTSTRAP_SERVERS": BOOTSTRAP,
            "OPSPILOT_KAFKA_SECURITY_PROTOCOL": "SASL_PLAINTEXT",
        }
    )
    assert _surface_sha256(with_kafka) == _surface_sha256(plain)
    assert not set(TOOLS) & {
        s["function"]["name"]
        for s in with_kafka.face(FakeClock(start=NOW)).tool_schemas
    }


@pytest.mark.parametrize(
    "switches", sorted(PINNED), ids=lambda s: "+".join(s) or "none"
)
def test_existing_extension_revisions_are_unchanged(switches):
    from opspilot.tools.otel_demo_ext import tool_schema_revision

    revision = PINNED[switches]
    assert select_profile(_web(switches)).versions()["tool_schema_revision"] == revision
    assert tool_schema_revision(**_flags(switches)) == revision
    assert tool_schema_revision(**_flags(switches), kafka=False) == revision


@pytest.mark.parametrize(
    "switches", sorted(PINNED), ids=lambda s: "+".join(s) or "none"
)
def test_kafka_settings_without_the_address_change_no_surface(switches):
    plain = _surface_sha256(select_profile(_web(switches)))
    # The web looks at the address only (B1-U2): any other OPSPILOT_KAFKA_*
    # leaves every byte it records unchanged.
    for extra in (
        {"OPSPILOT_KAFKA_SECURITY_PROTOCOL": "SASL_SSL"},
        {"OPSPILOT_KAFKA_SASL_MECHANISM": "SCRAM-SHA-512"},
        {
            "OPSPILOT_KAFKA_USERNAME": KAFKA_USERNAME,
            "OPSPILOT_KAFKA_PASSWORD": KAFKA_PASSWORD,
        },
        {"OPSPILOT_KAFKA_CA_FILE": "/run/secrets/kafka-ca.pem"},
    ):
        assert _surface_sha256(select_profile({**_web(switches), **extra})) == plain, (
            extra
        )
    assert not set(TOOLS) & set(_tool_names(_web(switches)))
    assert "kafka" not in _available(_web(switches))


@pytest.mark.parametrize(
    "switches", sorted(PINNED), ids=lambda s: "+".join(s) or "none"
)
def test_the_address_adds_exactly_the_eight_tools_as_one_block_and_one_source(switches):
    from opspilot.tools import kafka_admin

    old = _tool_names(_web(switches))
    new = _tool_names(_web(switches, kafka=True))
    positions = [i for i, name in enumerate(new) if name in TOOLS]
    assert len(positions) == 8 and positions == list(
        range(positions[0], positions[0] + 8)
    ), new
    assert sorted(new[positions[0] : positions[0] + 8]) == sorted(TOOLS)
    assert [n for n in new if n not in TOOLS] == old
    if not {"knowledge", "todo"} & set(switches):
        # After every backend in the fixed order (C3-12).
        assert positions[-1] == len(new) - 1
    assert sorted(kafka_admin.TOOL_NAMES) == sorted(TOOLS)
    available = _available(_web(switches, kafka=True))
    assert available["kafka"] == sorted(TOOLS)
    assert {k: v for k, v in available.items() if k != "kafka"} == dict(
        _available(_web(switches))
    )
    revision = select_profile(_web(switches, kafka=True)).versions()[
        "tool_schema_revision"
    ]
    assert revision.startswith("otel-demo-ext-")
    assert revision not in set(PINNED.values())


def test_kafka_revisions_follow_the_flag_and_the_new_constants():
    from opspilot.tools.otel_demo_ext import (
        KAFKA_TOOL_NAMES,
        KAFKA_TOOL_SCHEMA_REVISION,
        TOOL_NAMES,
        tool_schema_revision,
    )

    seen = set()
    for switches in sorted(PINNED):
        revision = select_profile(_web(switches, kafka=True)).versions()[
            "tool_schema_revision"
        ]
        assert revision == tool_schema_revision(**_flags(switches), kafka=True)
        seen.add(revision)
    assert len(seen) == len(PINNED)
    assert KAFKA_TOOL_SCHEMA_REVISION == tool_schema_revision(kafka=True)
    assert (
        select_profile(_web(kafka=True)).versions()["tool_schema_revision"]
        == KAFKA_TOOL_SCHEMA_REVISION
    )
    # Like the other *_TOOL_NAMES: the registered names with only Kafka set.
    names = list(KAFKA_TOOL_NAMES)
    assert len(names) == len(set(names))
    assert set(names) == set(TOOL_NAMES) | set(TOOLS)
    # Mapping order is moot.
    full = _web(BACKENDS_ONLY, kafka=True)
    assert _surface_sha256(select_profile(dict(reversed(full.items())))) == (
        _surface_sha256(select_profile(full))
    )


def test_web_and_worker_agree_on_the_kafka_face_and_disagreement_is_visible():
    web = _web(kafka=True)
    worker = _sasl_env()
    assert select_profile(web).versions() == select_profile(worker).versions()
    assert _surface_sha256(select_profile(web)) == _surface_sha256(
        select_profile(worker)
    )
    # A web without the address records other versions than a worker with
    # it, which is what the claim-time check compares (as B1/C5).
    assert select_profile(_web()).versions() != select_profile(worker).versions()


def test_schemas_expose_the_upstream_parameters_without_a_cluster_name():
    schemas = {
        s["function"]["name"]: s["function"]
        for s in _face(_web(kafka=True)).tool_schemas
    }
    expected = {
        CONSUMERS: ({}, []),
        GROUP: ({"group_id": "string", "include_offsets": "boolean"}, ["group_id"]),
        TOPICS: ({}, []),
        TOPIC: (
            {"topic_name": "string", "fetch_configuration": "boolean"},
            ["topic_name"],
        ),
        FIND: ({"topic_name": "string"}, ["topic_name"]),
        BROKERS: ({}, []),
        CONFIGS: (
            {"resource_type": "string", "resource_name": "string"},
            ["resource_name", "resource_type"],
        ),
        OVERVIEW: ({}, []),
    }
    for tool, (properties, required) in expected.items():
        params = schemas[tool]["parameters"]
        props = params.get("properties") or {}
        assert set(props) == set(properties), tool
        assert "kafka_cluster_name" not in json.dumps(schemas[tool]), tool
        assert sorted(params.get("required") or []) == required, tool
        for name, kind in properties.items():
            assert props[name]["type"] == kind, (tool, name)
    resource_type = schemas[CONFIGS]["parameters"]["properties"]["resource_type"]
    if "enum" in resource_type:
        assert sorted(resource_type["enum"]) == ["broker", "topic"]


def test_registrations_are_bounded_reads_that_may_contain_secrets():
    from opspilot.tools import kafka_admin

    assert kafka_admin.ENDPOINT_SELECTOR == "kafka_bootstrap"
    if hasattr(kafka_admin, "CREDENTIAL_REF"):
        assert kafka_admin.CREDENTIAL_REF == "kafka-ro"
    regs = {
        r.name: r for r in kafka_admin.kafka_registrations(max_window_seconds=86400)
    }
    assert set(regs) == set(TOOLS)
    for name, reg in regs.items():
        assert reg.read_only is True, name
        assert reg.verb == VERB[name], name
        assert reg.source == "otel-demo", name
        assert reg.result_path == ("results",), name
        assert reg.may_contain_secrets is True, name
        assert reg.max_result_bytes == 2 * MIB, name
        assert reg.max_view_tokens == 25_000, name
        assert 0 < reg.request_timeout_seconds <= 30, name
        # C3-11: current state, not the incident window; no message content;
        # committed and end offsets are not one snapshot.
        cannot = reg.description.cannot_prove.lower()
        assert "window" in cannot and "message" in cannot, (name, cannot)
    for name in (GROUP, FIND):
        assert "lag" in regs[name].description.cannot_prove.lower(), name


def test_unconfigured_extension_worker_has_no_kafka_tools(kafka):
    executor, _ = _worker(_env(None))
    for index, tool in enumerate(TOOLS):
        outcome = executor.execute(_call(tool, index=index))
        assert outcome.status in ("error", "denied"), (tool, _view(outcome))
    assert kafka.configs == [] and kafka.calls == []


# == ① the static read-only guarantee ================================================

ALLOWED_IMPORT_MODULES = {"confluent_kafka", "confluent_kafka.admin"}
ALLOWED_IMPORT_NAMES = {
    "AdminClient",
    "KafkaError",
    "KafkaException",
    "TopicPartition",
    "TopicCollection",
    "ConsumerGroupTopicPartitions",
    "OffsetSpec",
    "ConfigResource",
    "ResourceType",
    "IsolationLevel",
}
FORBIDDEN_IDENTIFIERS = {
    "Consumer",
    "Producer",
    "DeserializingConsumer",
    "SerializingProducer",
    "ShareConsumer",
    "DeserializingShareConsumer",
    "AIOConsumer",
    "AIOProducer",
}
# Every admin-client method that is not one of the eight reads, and the
# consumer/producer verbs; none may be called in the Kafka module.
FORBIDDEN_METHODS = {
    "create_topics", "delete_topics", "create_partitions", "alter_configs",
    "incremental_alter_configs", "create_acls", "delete_acls", "describe_acls",
    "alter_consumer_group_offsets", "delete_consumer_groups", "delete_records",
    "elect_leaders", "alter_user_scram_credentials",
    "describe_user_scram_credentials", "set_sasl_credentials", "list_groups",
    "consume", "produce", "produce_batch", "subscribe", "assign",
    "incremental_assign", "commit", "flush", "seek", "store_offsets",
    "init_transactions", "begin_transaction", "send_offsets_to_transaction",
    "commit_transaction",
}  # fmt: skip
KAFKA_MODULE = ROOT / "opspilot" / "tools" / "kafka_admin.py"


def _product_files() -> list[Path]:
    return sorted(p for p in (ROOT / "opspilot").rglob("*.py") if p.is_file())


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_only_the_kafka_module_imports_confluent_kafka_and_only_read_types():
    assert KAFKA_MODULE.is_file()
    importers = set()
    for path in _product_files():
        tree = _tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("confluent_kafka"), (
                        f"{path}: plain import {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "confluent_kafka"
            ):
                importers.add(path)
                assert node.module in ALLOWED_IMPORT_MODULES, (path, node.module)
                names = {alias.name for alias in node.names}
                assert names <= ALLOWED_IMPORT_NAMES, (
                    path,
                    names - ALLOWED_IMPORT_NAMES,
                )
            elif isinstance(node, ast.Call):
                # No importlib / __import__ detour around the rules above.
                func = node.func
                called = getattr(func, "attr", None) or getattr(func, "id", None)
                if called in {"import_module", "__import__"}:
                    for arg in node.args:
                        assert not (
                            isinstance(arg, ast.Constant)
                            and str(arg.value).startswith("confluent_kafka")
                        ), (path, node.lineno)
    assert importers == {KAFKA_MODULE}


def test_no_consumer_or_producer_object_exists_in_product_code():
    for path in _product_files():
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Name):
                assert node.id not in FORBIDDEN_IDENTIFIERS, (
                    path,
                    node.lineno,
                    node.id,
                )
            elif isinstance(node, ast.Attribute):
                assert node.attr not in FORBIDDEN_IDENTIFIERS, (path, node.lineno)
                assert node.attr != "aio", (path, node.lineno)
            elif isinstance(node, ast.alias):
                assert (node.asname or node.name).split(".")[-1] not in (
                    FORBIDDEN_IDENTIFIERS
                ), (path, node.name)


def test_the_kafka_module_calls_only_the_eight_reads():
    called = set()
    for node in ast.walk(_tree(KAFKA_MODULE)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            assert attr not in FORBIDDEN_METHODS, (node.lineno, attr)
            called.add(attr)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in {"getattr", "__import__"} or not any(
                isinstance(a, ast.Constant) and a.value in FORBIDDEN_METHODS
                for a in node.args
            ), node.lineno
    # Positive control: the reads are really there.
    assert {"list_topics", "describe_topics", "list_offsets"} <= called


def test_list_topics_is_never_called_with_a_topic():
    calls = 0
    for node in ast.walk(_tree(KAFKA_MODULE)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "list_topics"
        ):
            calls += 1
            assert not node.args, f"list_topics with a positional at {node.lineno}"
            assert all(k.arg != "topic" and k.arg is not None for k in node.keywords), (
                node.lineno
            )
    assert calls >= 1


def test_ruff_bans_consumers_and_producers_in_product_code():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    banned = config["tool"]["ruff"]["lint"]["flake8-tidy-imports"]["banned-api"]
    for name in (
        "confluent_kafka.Consumer",
        "confluent_kafka.Producer",
        "confluent_kafka.DeserializingConsumer",
        "confluent_kafka.SerializingProducer",
        "confluent_kafka.aio",
    ):
        assert name in banned, name
    # The existing bans stay.
    assert "datetime.datetime.now" in banned


def test_the_web_side_face_does_not_load_librdkafka():
    script = (
        "import json, sys\n"
        "from opspilot.tools.profiles import select_profile\n"
        "from tests.m1_tool_support import FakeClock\n"
        "from tests.test_m1_otel_demo_contract import NOW\n"
        "p = select_profile({'OPSPILOT_TOOL_PROFILE': 'otel-demo-ext',"
        f" 'OPSPILOT_KAFKA_BOOTSTRAP_SERVERS': {BOOTSTRAP!r}}})\n"
        "face = p.face(FakeClock(start=NOW))\n"
        "face.evidence_context('r'); p.versions()\n"
        "names = {s['function']['name'] for s in face.tool_schemas}\n"
        "print(json.dumps({'kafka_tools': 'list_brokers' in names,"
        " 'loaded': sorted(m for m in sys.modules if m.startswith('confluent_kafka'))}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report == {"kafka_tools": True, "loaded": []}


# == ② client configuration, by-name lookups, one short client per call ==============


def _conf_value(conf: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in conf:
            return conf[key]
    return None


def test_each_call_builds_one_short_lived_client_with_the_fixed_settings(kafka):
    executor, _ = _worker(_env("localhost:9092,127.0.0.1:9093"))
    for index, tool in enumerate(TOOLS):
        before = len(kafka.configs)
        executor.execute(_call(tool, index=index))
        assert len(kafka.configs) == before + 1, tool  # a new client per call
    assert kafka.entered == kafka.exited == len(TOOLS)  # ``with`` closes each
    for conf in kafka.configs:
        assert conf["bootstrap.servers"] == "localhost:9092,127.0.0.1:9093"
        assert str(conf["client.id"]) == "opspilot-ro"
        assert str(conf["allow.auto.create.topics"]).lower() == "false"
        assert str(conf["socket.connection.setup.timeout.ms"]) == "10000"
        assert str(_conf_value(conf, "security.protocol") or "PLAINTEXT").upper() == (
            "PLAINTEXT"
        )
        assert not {k for k in conf if k.startswith(("sasl.", "ssl."))}, conf
        assert "group.id" not in conf
    assert kafka.other == []


@pytest.mark.parametrize("tool", TOOLS)
def test_each_tool_uses_only_its_reads_with_a_bounded_timeout(kafka, tool):
    params = dict(MINIMAL[tool])
    if tool == TOPIC:
        params["fetch_configuration"] = True
    outcome = _run(kafka, tool, params)
    _ok(outcome)
    names = set(kafka.names())
    assert names and names <= ALLOWED_CALLS[tool], names
    assert names <= READ_METHODS
    assert kafka.other == []
    for name, _args, kwargs in kafka.calls:
        timeout = kwargs.get("request_timeout", kwargs.get("timeout"))
        assert isinstance(timeout, (int, float)) and 0 < timeout <= 30, (name, kwargs)
    for timeout in kafka.result_timeouts:
        assert isinstance(timeout, (int, float)) and 0 < timeout <= 30, timeout


@pytest.mark.parametrize("tool", (TOPICS, OVERVIEW))
def test_list_topics_is_called_without_a_topic(kafka, tool):
    _ok(_run(kafka, tool))
    calls = [(a, k) for n, a, k in kafka.calls if n == "list_topics"]
    assert calls
    for args, kwargs in calls:
        assert args == () and "topic" not in kwargs, (args, kwargs)


def test_a_misspelled_topic_is_looked_up_only_by_describe_topics(kafka):
    executor, _ = _worker(_env())
    _no_data_with(
        executor.execute(_call(TOPIC, {"topic_name": "ordres"})), "TOPIC_NOT_FOUND"
    )
    assert kafka.names() == ["describe_topics"]
    _no_data_with(
        executor.execute(_call(FIND, {"topic_name": "ordres"}, index=1)),
        "TOPIC_NOT_FOUND",
    )
    # find_consumer_groups_by_topic confirms the topic first and scans no group.
    assert kafka.names() == ["describe_topics", "describe_topics"]
    for _, args, _ in kafka.calls:
        assert list(args[0].topic_names) == ["ordres"]
    assert "ordres" not in kafka.topics


def test_the_selector_carries_the_bootstrap_address_and_no_secret(kafka):
    outcome = _run(kafka, BROKERS, env=_sasl_env())
    _ok(outcome)
    assert kafka.requests
    selector = dict(kafka.requests[-1].selector)
    assert selector["kafka_bootstrap"] == BOOTSTRAP
    assert KAFKA_PASSWORD not in json.dumps(selector)


# == ③ lag: the four flags, lag_by_topic, total_lag, no commit metadata ==============


def _lag_cluster(fake: FakeKafka) -> None:
    fake.topic("audit", 2)
    fake.ends[("audit", 0)] = (0, 30)
    fake.ends[("audit", 1)] = (10, 20)
    fake.group(
        "lagging",
        state="STABLE",
        members=[
            (
                "m-1",
                "lag-client",
                "/10.0.0.9",
                "instance-1",
                [("orders", 0), ("orders", 1), ("orders", 2), ("payments", 0)],
            )
        ],
        offsets={
            ("orders", 0): (400, META_SENTINEL),
            ("orders", 1): (800, None),
            ("orders", 2): (100, None),
            # Not assigned, but committed: still part of the partition set.
            ("audit", 0): (50, META_SENTINEL),
            ("audit", 1): (5, None),
        },
    )


def _by_partition(rows) -> dict[tuple[str, int], Mapping[str, Any]]:
    return {(row["topic"], row["partition"]): row for row in rows}


def test_lag_rows_carry_the_four_flag_cases_and_per_topic_totals(kafka):
    _lag_cluster(kafka)
    outcome = _run(kafka, GROUP, {"group_id": "lagging"})
    _ok(outcome)
    group = _one(outcome)
    assert _keys(group) == {
        "group_id", "state", "type", "coordinator", "partition_assignor",
        "members", "partitions", "lag_by_topic",
    }  # fmt: skip
    rows = _by_partition(group["partitions"])
    assert set(rows) == {
        ("orders", 0), ("orders", 1), ("orders", 2), ("payments", 0),
        ("audit", 0), ("audit", 1),
    }  # fmt: skip
    for row in rows.values():
        assert _keys(row) == LAG_ROW_KEYS, row
    assert (rows["orders", 0]["committed_offset"], rows["orders", 0]["end_offset"]) == (
        400,
        1000,
    )
    assert [rows["orders", p]["lag"] for p in range(3)] == [600, 0, 500]
    assert all(rows["orders", p]["flags"] == [] for p in range(3))
    # Assigned, never committed.
    assert rows["payments", 0]["lag"] is None
    assert rows["payments", 0]["committed_offset"] is None
    assert rows["payments", 0]["flags"] == ["no_committed_offset"]
    # Committed past the end: lag 0, flagged.
    assert rows["audit", 0]["lag"] == 0
    assert rows["audit", 0]["flags"] == ["committed_ahead_of_end"]
    # Committed below the log start: lag still end - committed, flagged.
    assert rows["audit", 1]["log_start_offset"] == 10
    assert rows["audit", 1]["lag"] == 15
    assert rows["audit", 1]["flags"] == ["committed_below_log_start"]
    assert group["lag_by_topic"]["orders"] == 1100
    assert group["lag_by_topic"]["audit"] == 15
    assert group["lag_by_topic"].get("payments", 0) == 0
    # Member projection.
    (member,) = group["members"]
    assert _keys(member) == {
        "member_id", "client_id", "host", "group_instance_id", "assignment",
    }  # fmt: skip
    assert (member["client_id"], member["host"], member["group_instance_id"]) == (
        "lag-client",
        "/10.0.0.9",
        "instance-1",
    )
    assert sorted((a["topic"], a["partition"]) for a in member["assignment"]) == [
        ("orders", 0), ("orders", 1), ("orders", 2), ("payments", 0),
    ]  # fmt: skip
    assert _name(group["state"]) == "STABLE"
    assert group["partition_assignor"] == "range"
    # The commit metadata string is never projected.
    assert META_SENTINEL not in _text(outcome)
    assert META_SENTINEL.encode() not in (outcome.evidence.raw or b"")


def test_offsets_are_latest_and_earliest_of_the_partition_union(kafka):
    _lag_cluster(kafka)
    _ok(_run(kafka, GROUP, {"group_id": "lagging"}))
    asked: dict[str, set[tuple[str, int]]] = {"latest": set(), "earliest": set()}
    for name, args, kwargs in kafka.calls:
        if name != "list_offsets":
            continue
        for topic, partition, kind in args[0]:
            assert kind in asked, kind
            asked[kind].add((topic, partition))
        level = kwargs.get("isolation_level")
        if level is not None:
            assert "UNCOMMITTED" in _name(level), level
    union = {
        ("orders", 0), ("orders", 1), ("orders", 2), ("payments", 0),
        ("audit", 0), ("audit", 1),
    }  # fmt: skip
    assert asked == {"latest": union, "earliest": union}
    offsets_calls = [a for n, a, _ in kafka.calls if n == "list_consumer_group_offsets"]
    assert offsets_calls
    for (requests,) in offsets_calls:
        assert [r.group_id for r in requests] == ["lagging"]  # one group per call


def test_include_offsets_defaults_to_true_and_false_skips_the_offset_reads(kafka):
    executor, _ = _worker(_env())
    _ok(executor.execute(_call(GROUP, {"group_id": "order-processor"})))
    assert {"list_consumer_group_offsets", "list_offsets"} <= set(kafka.names())
    kafka.calls.clear()
    outcome = executor.execute(
        _call(GROUP, {"group_id": "order-processor", "include_offsets": False}, index=1)
    )
    _ok(outcome)
    assert kafka.names() == ["describe_consumer_groups"]
    assert not _one(outcome).get("partitions")
    assert "lag" not in json.dumps(_one(outcome).get("lag_by_topic") or {})


def test_the_stopped_group_reads_1100(kafka):
    outcome = _run(kafka, GROUP, {"group_id": "order-processor"})
    _ok(outcome)
    group = _one(outcome)
    assert group["group_id"] == "order-processor"
    assert _name(group["state"]) == "EMPTY"
    assert group["members"] == []
    assert group["lag_by_topic"] == {"orders": 1100}
    assert sorted(
        (r["partition"], r["committed_offset"], r["end_offset"], r["lag"])
        for r in group["partitions"]
    ) == [(0, 400, 1000, 600), (1, 800, 800, 0), (2, 100, 600, 500)]


def test_a_negative_committed_offset_is_no_committed_offset(kafka):
    # OFFSET_INVALID (-1001) is how the protocol says "nothing committed".
    kafka.group(
        "half",
        state="STABLE",
        members=[("m", "c", "/h", None, [("orders", 0), ("orders", 1)])],
        offsets={("orders", 0): (-1001, None), ("orders", 1): (700, None)},
    )
    outcome = _run(kafka, GROUP, {"group_id": "half"})
    _ok(outcome)
    rows = _by_partition(_one(outcome)["partitions"])
    assert rows["orders", 0]["lag"] is None
    assert rows["orders", 0]["flags"] == ["no_committed_offset"]
    assert rows["orders", 1]["lag"] == 100
    assert _one(outcome)["lag_by_topic"]["orders"] == 100


def test_find_groups_by_topic_returns_each_group_with_its_total_lag(kafka):
    _lag_cluster(kafka)
    kafka.group("payments-only", offsets={("payments", 0): (5, None)})
    kafka.group("idle", state="EMPTY")
    outcome = _run(kafka, FIND, {"topic_name": "orders"})
    _ok(outcome)
    rows = _rows(outcome)
    assert [r["group_id"] for r in rows] == [
        "lagging",
        "order-audit",
        "order-processor",
    ]
    for row in rows:
        assert _keys(row) == {
            "group_id", "state", "member_count", "assigned_partitions",
            "committed_partitions", "total_lag",
        }, row  # fmt: skip
    by = {r["group_id"]: r for r in rows}
    assert by["order-processor"]["total_lag"] == 1100
    assert by["order-processor"]["member_count"] == 0
    assert _size(by["order-processor"]["assigned_partitions"]) == 0
    assert _size(by["order-processor"]["committed_partitions"]) == 3
    assert by["order-audit"]["total_lag"] == 0
    assert by["order-audit"]["member_count"] == 1
    assert _size(by["order-audit"]["assigned_partitions"]) == 3
    # Only this topic's lag: orders 600 + 0 + 500, not audit's 15.
    assert by["lagging"]["total_lag"] == 1100
    assert _size(by["lagging"]["assigned_partitions"]) == 3
    assert META_SENTINEL not in _text(outcome)
    # The topic is confirmed before any group is listed.
    names = kafka.names()
    assert names[0] == "describe_topics"
    assert names.index("list_consumer_groups") > 0


@pytest.mark.parametrize("error", [False, True], ids=["dead", "group-id-not-found"])
def test_an_unknown_group_is_no_data_group_not_found(kafka, error):
    kafka.missing_group_error = error
    outcome = _run(kafka, GROUP, {"group_id": "no-such-group"})
    _no_data_with(outcome, "GROUP_NOT_FOUND")


# == projections of the other tools ==================================================


def test_list_kafka_consumers_rows_are_sorted_groups(kafka):
    kafka.group("a-simple", state="EMPTY", simple=True, gtype="CLASSIC")
    kafka.group("z-new", state="STABLE", gtype="CONSUMER")
    outcome = _run(kafka, CONSUMERS)
    _ok(outcome)
    rows = _rows(outcome)
    assert [r["group_id"] for r in rows] == [
        "a-simple", "order-audit", "order-processor", "z-new",
    ]  # fmt: skip
    for row in rows:
        assert _keys(row) == {"group_id", "state", "type", "is_simple"}, row
    by = {r["group_id"]: r for r in rows}
    assert by["a-simple"]["is_simple"] is True
    assert by["order-audit"]["is_simple"] is False
    assert _name(by["z-new"]["type"]) == "CONSUMER"
    assert _name(by["order-audit"]["state"]) == "STABLE"


def test_list_topics_rows_are_sorted_and_keep_internal_topics(kafka):
    outcome = _run(kafka, TOPICS)
    _ok(outcome)
    rows = _rows(outcome)
    assert [r["topic"] for r in rows] == ["__consumer_offsets", "orders", "payments"]
    for row in rows:
        assert _keys(row) == {"topic", "partitions", "internal"}, row
    by = {r["topic"]: r for r in rows}
    assert by["__consumer_offsets"]["internal"] is True
    assert by["orders"]["internal"] is False
    assert _size(by["orders"]["partitions"]) == 3


def test_describe_topic_rows_mark_under_replicated_and_offline_partitions(kafka):
    kafka.topics["orders"]["partitions"] = {
        0: (1, (1, 2, 3), (1, 2, 3)),
        1: (2, (2, 3, 1), (2, 3)),  # one replica out of sync
        2: (9, (9, 4), ()),  # leader and replicas on brokers that are gone
    }
    outcome = _run(kafka, TOPIC)
    _ok(outcome)
    row = _one(outcome)
    assert _keys(row) - {"configs"} == {"topic", "topic_id", "internal", "partitions"}
    assert row["topic"] == "orders" and row["internal"] is False
    assert row["topic_id"]
    parts = {p["partition"]: p for p in row["partitions"]}
    assert sorted(parts) == [0, 1, 2]
    for part in parts.values():
        assert _keys(part) == {
            "partition", "leader", "replicas", "isr", "under_replicated", "offline",
        }  # fmt: skip
    assert (parts[0]["leader"], parts[0]["replicas"], parts[0]["isr"]) == (
        1,
        [1, 2, 3],
        [1, 2, 3],
    )
    assert (parts[0]["under_replicated"], parts[0]["offline"]) == (False, False)
    assert parts[1]["under_replicated"] is True and parts[1]["offline"] is False
    assert parts[2]["offline"] is True
    assert parts[2]["leader"] in (None, -1)
    assert not row.get("configs")
    assert "describe_configs" not in kafka.names()  # fetch_configuration=false


def test_describe_topic_with_configuration_drops_sensitive_values(kafka):
    outcome = _run(kafka, TOPIC, {"topic_name": "orders", "fetch_configuration": True})
    _ok(outcome)
    assert ("describe_configs", [("topic", "orders")]) in [
        (n, a[0]) for n, a, _ in kafka.calls
    ]
    text = _text(outcome)
    assert "retention.ms" in text and "86400123" in text
    assert CANARY not in text
    assert CANARY.encode() not in (outcome.evidence.raw or b"")


def test_list_brokers_rows_are_sorted_and_the_view_has_the_cluster_id(kafka):
    outcome = _run(kafka, BROKERS)
    _ok(outcome)
    rows = _rows(outcome)
    assert [r["id"] for r in rows] == [1, 2, 3]
    for row in rows:
        assert _keys(row) == {"id", "host", "port", "rack", "is_controller"}, row
    assert {k: rows[0][k] for k in _keys(rows[0])} == {
        "id": 1, "host": "broker-1.kafka.svc", "port": 9092, "rack": "rack-a",
        "is_controller": False,
    }  # fmt: skip
    assert [r["is_controller"] for r in rows] == [False, True, False]
    assert rows[1]["rack"] is None
    assert "c3-cluster-id" in _meta(outcome)


@pytest.mark.parametrize(
    ("params", "requested"),
    [
        ({"resource_type": "topic", "resource_name": "orders"}, ("topic", "orders")),
        ({"resource_type": "broker", "resource_name": "1"}, ("broker", "1")),
    ],
)
def test_describe_configs_rows_are_sorted_and_sensitive_values_dropped(
    kafka, params, requested
):
    outcome = _run(kafka, CONFIGS, params)
    _ok(outcome)
    assert [(n, a[0]) for n, a, _ in kafka.calls] == [("describe_configs", [requested])]
    rows = _rows(outcome)
    assert [r["name"] for r in rows] == sorted(r["name"] for r in rows)
    for row in rows:
        assert _keys(row) == {
            "name", "value", "source", "is_default", "is_read_only", "is_sensitive",
        }, row  # fmt: skip
    sensitive = [r for r in rows if r["is_sensitive"]]
    assert len(sensitive) == 1 and sensitive[0]["value"] is None
    assert CANARY not in _text(outcome)
    assert CANARY.encode() not in (outcome.evidence.raw or b"")
    if requested[0] == "topic":
        by = {r["name"]: r for r in rows}
        assert by["retention.ms"]["value"] == "86400123"
        assert by["cleanup.policy"]["is_default"] is True
    else:
        assert {r["name"]: r for r in rows}["num.network.threads"][
            "is_read_only"
        ] is True


def test_cluster_overview_counts_follow_the_upstream_rules(kafka):
    kafka.topics.pop("__consumer_offsets")
    kafka.topics["orders"]["partitions"] = {
        0: (1, (1, 2, 3), (1, 2, 3)),
        1: (2, (2, 3), (2,)),  # under-replicated
        2: (-1, (3, 7), (3,)),  # offline, and replica 7 is no live broker
    }
    outcome = _run(kafka, OVERVIEW)
    _ok(outcome)
    row = _one(outcome)
    assert _keys(row) == {
        "cluster_id", "controller_id", "broker_count", "topic_count",
        "partition_count", "under_replicated_partitions", "offline_partitions",
        "offline_broker_ids",
    }  # fmt: skip
    assert row["cluster_id"] == "c3-cluster-id"
    assert row["controller_id"] == 2
    assert row["broker_count"] == 3
    assert row["topic_count"] == 2
    assert row["partition_count"] == 4
    assert row["under_replicated_partitions"] == 2
    assert row["offline_partitions"] == 1
    assert list(row["offline_broker_ids"]) == [7]


@pytest.mark.parametrize("tool", TOOLS)
def test_views_name_the_row_unit_and_are_current_state(kafka, tool):
    outcome = _run(kafka, tool)
    _ok(outcome)
    view = _view(outcome)
    assert f"backend_{ROW_UNIT[tool]}_returned" in view, view
    assert view.get("source_start_at") is None and view.get("source_end_at") is None
    observed = datetime.fromisoformat(str(view["observed_at"]).replace("Z", "+00:00"))
    assert abs(observed - NOW) <= timedelta(seconds=1), view["observed_at"]
    assert view.get("cannot_prove"), view


# == ④ parameters refused before any request ========================================

INVALID_TOPICS = ["", "a" * 250, "bad topic", "x/y", "a;b", "über", "a\nb", "a*"]
INVALID_GROUPS = ["", "g" * 256, "g\x00h", "g\nh", "g\x7fh", "\x1b[31m"]


@pytest.mark.parametrize("tool", (TOPIC, FIND))
@pytest.mark.parametrize("name", INVALID_TOPICS)
def test_invalid_topic_names_are_refused_without_a_client(kafka, tool, name):
    _refused_with(_run(kafka, tool, {"topic_name": name}), "INVALID_TOPIC")
    assert kafka.configs == [] and kafka.calls == []


@pytest.mark.parametrize("name", INVALID_GROUPS)
def test_invalid_group_ids_are_refused_without_a_client(kafka, name):
    _refused_with(_run(kafka, GROUP, {"group_id": name}), "INVALID_GROUP")
    assert kafka.configs == [] and kafka.calls == []


@pytest.mark.parametrize(
    "params",
    [
        {"resource_type": "group", "resource_name": "order-processor"},
        {"resource_type": "cluster", "resource_name": "x"},
        {"resource_type": "broker", "resource_name": "-1"},
        {"resource_type": "broker", "resource_name": "1.0"},
        {"resource_type": "broker", "resource_name": "b1"},
        {"resource_type": "broker", "resource_name": ""},
        {"resource_type": "broker", "resource_name": "0x1"},
    ],
)
def test_invalid_config_resources_are_refused_without_a_client(kafka, params):
    _refused_with(_run(kafka, CONFIGS, params), "INVALID_RESOURCE")
    assert kafka.configs == [] and kafka.calls == []


@pytest.mark.parametrize("name", ["", "bad topic", "a" * 250])
def test_an_invalid_topic_resource_is_refused_without_a_client(kafka, name):
    outcome = _run(kafka, CONFIGS, {"resource_type": "topic", "resource_name": name})
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), _view(
        outcome
    )
    notes = _view(outcome).get("notes") or []
    assert notes and str(notes[0]).startswith(("INVALID_TOPIC", "INVALID_RESOURCE"))
    assert kafka.configs == [] and kafka.calls == []


@pytest.mark.parametrize(
    ("tool", "params"),
    [
        (GROUP, {"group_id": "order-processor", "include_offsets": "true"}),
        (GROUP, {"group_id": "order-processor", "include_offsets": 1}),
        (TOPIC, {"topic_name": "orders", "fetch_configuration": "yes"}),
        (TOPIC, {"topic_name": "orders", "fetch_configuration": 0}),
    ],
)
def test_booleans_of_the_wrong_type_are_refused_without_a_client(kafka, tool, params):
    outcome = _run(kafka, tool, params)
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), _view(
        outcome
    )
    assert kafka.configs == [] and kafka.calls == []


@pytest.mark.parametrize(
    ("tool", "params"),
    [
        (GROUP, {}),
        (TOPIC, {}),
        (FIND, {}),
        (CONFIGS, {"resource_type": "topic"}),
        (CONFIGS, {"resource_name": "orders"}),
        (GROUP, {"group_id": 7}),
        (TOPIC, {"topic_name": ["orders"]}),
        (BROKERS, {"kafka_cluster_name": "prod"}),
        (TOPICS, {"topic": "orders"}),
        (FIND, {"topic_name": "orders", "kafka_cluster_name": "prod"}),
    ],
)
def test_missing_mistyped_or_extra_parameters_are_refused_without_a_client(
    kafka, tool, params
):
    outcome = _run(kafka, tool, params)
    assert outcome.status in ("error", "denied"), _view(outcome)
    assert kafka.configs == [] and kafka.calls == []


def test_boundary_names_are_accepted_and_sent(kafka):
    executor, _ = _worker(_env())
    long_topic = "t." + "a" * 247
    _no_data_with(
        executor.execute(_call(TOPIC, {"topic_name": long_topic})), "TOPIC_NOT_FOUND"
    )
    assert list(kafka.calls[-1][1][0].topic_names) == [long_topic]
    for index, group in enumerate(("g" * 255, "order processor (blue)", "订单-组")):
        outcome = executor.execute(_call(GROUP, {"group_id": group}, index=1 + index))
        _no_data_with(outcome, "GROUP_NOT_FOUND")
        described = [a[0] for n, a, _ in kafka.calls if n == "describe_consumer_groups"]
        assert described[-1] == [group]
    outcome = executor.execute(
        _call(CONFIGS, {"resource_type": "broker", "resource_name": "0"}, index=9)
    )
    assert kafka.calls[-1][1][0] == [("broker", "0")]
    assert outcome.status in ("no_data", "ok", "error"), _view(outcome)


# == ⑤ caps and incomplete markers ===================================================


def test_list_topics_keeps_the_first_1000_by_name_and_is_incomplete(kafka):
    for n in range(1200):
        kafka.topic(f"t-{n:04d}", 1)
    outcome = _run(kafka, TOPICS)
    _ok(outcome)
    rows = _rows(outcome)
    assert len(rows) == 1000
    names = sorted(
        ["__consumer_offsets", "orders", "payments"]
        + [f"t-{n:04d}" for n in range(1200)]
    )
    assert [r["topic"] for r in rows] == names[:1000]
    assert _view(outcome)["incomplete"] is True


# Lead revision of C3-8 (2026-11-20): per-tool caps so a full page fits the
# 25k-token view -- list_kafka_consumers 800 rows, the partition rows of
# describe_topic and describe_consumer_group 500, list_topics 1000, the
# 200-group scan unchanged.
CONSUMERS_CAP = 800
PARTITION_ROWS_CAP = 500


def test_list_kafka_consumers_keeps_the_first_800_by_name(kafka):
    for n in range(1100):
        kafka.group(f"g-{n:04d}")
    outcome = _run(kafka, CONSUMERS)
    _ok(outcome)
    rows = _rows(outcome)
    assert len(rows) == CONSUMERS_CAP
    assert [r["group_id"] for r in rows] == [f"g-{n:04d}" for n in range(CONSUMERS_CAP)]
    assert _view(outcome)["incomplete"] is True


def test_exactly_800_groups_are_complete(kafka):
    kafka.groups.clear()
    for n in range(CONSUMERS_CAP):
        kafka.group(f"g-{n:04d}")
    outcome = _run(kafka, CONSUMERS)
    _ok(outcome)
    assert len(_rows(outcome)) == CONSUMERS_CAP
    assert _view(outcome)["incomplete"] is False


def test_exactly_1000_topics_are_complete(kafka):
    kafka.topics.clear()
    for n in range(1000):
        kafka.topic(f"t-{n:04d}", 1)
    outcome = _run(kafka, TOPICS)
    _ok(outcome)
    assert len(_rows(outcome)) == 1000
    assert _view(outcome)["incomplete"] is False


def test_describe_topic_keeps_500_partition_rows(kafka):
    kafka.topic("wide", 1200)
    outcome = _run(kafka, TOPIC, {"topic_name": "wide"})
    _ok(outcome)
    parts = _one(outcome)["partitions"]
    assert [p["partition"] for p in parts] == list(range(PARTITION_ROWS_CAP))
    assert _view(outcome)["incomplete"] is True


def test_describe_topic_with_exactly_500_partitions_is_complete(kafka):
    kafka.topic("wide", PARTITION_ROWS_CAP)
    outcome = _run(kafka, TOPIC, {"topic_name": "wide"})
    _ok(outcome)
    assert len(_one(outcome)["partitions"]) == PARTITION_ROWS_CAP
    assert _view(outcome)["incomplete"] is False


def _wide_group(kafka: FakeKafka, partitions: int) -> None:
    kafka.topic("wide", partitions, ends={p: 10 for p in range(partitions)})
    kafka.group(
        "wide-group",
        state="EMPTY",
        offsets={("wide", p): (4, None) for p in range(partitions)},
    )


def test_describe_consumer_group_keeps_500_partition_rows(kafka):
    _wide_group(kafka, 1200)
    outcome = _run(kafka, GROUP, {"group_id": "wide-group"})
    _ok(outcome)
    rows = _one(outcome)["partitions"]
    assert len(rows) == PARTITION_ROWS_CAP
    assert all(_keys(row) == LAG_ROW_KEYS for row in rows)
    assert _view(outcome)["incomplete"] is True


def test_describe_consumer_group_with_exactly_500_partition_rows_is_complete(kafka):
    _wide_group(kafka, PARTITION_ROWS_CAP)
    outcome = _run(kafka, GROUP, {"group_id": "wide-group"})
    _ok(outcome)
    assert len(_one(outcome)["partitions"]) == PARTITION_ROWS_CAP
    assert _view(outcome)["incomplete"] is False


def test_find_scans_the_first_200_groups_by_name_and_says_how_many(kafka):
    kafka.groups.clear()
    for n in range(210):
        kafka.group(f"g-{n:03d}", offsets={("orders", 0): (n, None)})
    outcome = _run(kafka, FIND, {"topic_name": "orders"})
    _ok(outcome)
    described = [
        g for n, a, _ in kafka.calls if n == "describe_consumer_groups" for g in a[0]
    ]
    assert sorted(set(described)) == [f"g-{n:03d}" for n in range(200)]
    fetched = [
        r.group_id
        for n, a, _ in kafka.calls
        if n == "list_consumer_group_offsets"
        for r in a[0]
    ]
    assert set(fetched) <= {f"g-{n:03d}" for n in range(200)}
    assert [r["group_id"] for r in _rows(outcome)] == [f"g-{n:03d}" for n in range(200)]
    assert _view(outcome)["incomplete"] is True
    meta = _meta(outcome)
    assert "200" in meta and "210" in meta, meta


def test_string_values_are_cut_to_2000_chars(kafka):
    kafka.group(
        "long",
        state="STABLE",
        members=[("m" * 3000, "c" * 3000, "/" + "h" * 2500, None, [("orders", 0)])],
        offsets={("orders", 0): (1, None)},
    )
    outcome = _run(kafka, GROUP, {"group_id": "long"})
    _ok(outcome)
    (member,) = _one(outcome)["members"]
    assert len(member["client_id"]) == 2000 and member["client_id"] == "c" * 2000
    assert len(member["member_id"]) == 2000
    assert len(member["host"]) == 2000
    kafka.config("topic", "payments", [{"name": "x.long", "value": "v" * 5000}])
    outcome = _run(
        kafka, CONFIGS, {"resource_type": "topic", "resource_name": "payments"}
    )
    _ok(outcome)
    text = _text(outcome)
    assert "v" * 2000 in text and "v" * 2001 not in text


def test_raw_result_over_2_mib_is_refused_whole(kafka):
    kafka.config(
        "topic",
        "payments",
        [{"name": f"x.{n:04d}", "value": f"{n:04d}" + "v" * 1990} for n in range(1200)],
    )
    outcome = _run(
        kafka, CONFIGS, {"resource_type": "topic", "resource_name": "payments"}
    )
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE"), _view(
        outcome
    )
    assert _view(outcome)["content"] is None


def test_view_over_25k_tokens_is_refused_whole(kafka):
    words = " ".join(f"word{n} retention segment" for n in range(60))
    assert len(words) < 2000
    kafka.config(
        "topic",
        "payments",
        [{"name": f"x.{n:03d}", "value": words} for n in range(300)],
    )
    outcome = _run(
        kafka, CONFIGS, {"resource_type": "topic", "resource_name": "payments"}
    )
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE"), _view(
        outcome
    )
    assert _view(outcome)["content"] is None


# == ⑦ error classification ==========================================================


@pytest.mark.parametrize(
    ("name", "status", "reason"),
    [
        ("_TRANSPORT", "error", "SOURCE_UNAVAILABLE"),
        ("_ALL_BROKERS_DOWN", "error", "SOURCE_UNAVAILABLE"),
        ("_RESOLVE", "error", "SOURCE_UNAVAILABLE"),
        ("NOT_COORDINATOR", "error", "SOURCE_UNAVAILABLE"),
        ("COORDINATOR_NOT_AVAILABLE", "error", "SOURCE_UNAVAILABLE"),
        ("COORDINATOR_LOAD_IN_PROGRESS", "error", "SOURCE_UNAVAILABLE"),
        ("_TIMED_OUT", "timeout", None),
        ("REQUEST_TIMED_OUT", "timeout", None),
        ("TOPIC_AUTHORIZATION_FAILED", "error", "SOURCE_FORBIDDEN"),
        ("GROUP_AUTHORIZATION_FAILED", "error", "SOURCE_FORBIDDEN"),
        ("CLUSTER_AUTHORIZATION_FAILED", "error", "SOURCE_FORBIDDEN"),
        ("SASL_AUTHENTICATION_FAILED", "error", "SOURCE_FORBIDDEN"),
        ("_AUTHENTICATION", "error", "SOURCE_FORBIDDEN"),
        ("INVALID_REQUEST", "error", "INVALID_PARAMS"),
        ("POLICY_VIOLATION", "error", "SOURCE_ERROR"),
    ],
)
def test_kafka_errors_map_to_fixed_outcomes_with_one_request(
    kafka, name, status, reason
):
    kafka.fail["describe_cluster"] = kafka.m.error(
        name, f"broker said {name} marker-iiii567"
    )
    outcome = _run(kafka, BROKERS)
    assert outcome.status == status, (name, _view(outcome))
    if reason is not None:
        assert outcome.reason == reason, (name, _view(outcome))
    assert kafka.names() == ["describe_cluster"]  # no retry
    if reason == "SOURCE_FORBIDDEN":
        assert "marker-iiii567" not in _text(outcome)  # the code only


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("_TRANSPORT", "SOURCE_UNAVAILABLE"),
        ("CLUSTER_AUTHORIZATION_FAILED", "SOURCE_FORBIDDEN"),
    ],
)
def test_a_failing_metadata_request_maps_the_same_way(kafka, name, reason):
    kafka.fail["list_topics"] = kafka.m.error(name)
    outcome = _run(kafka, TOPICS)
    assert (outcome.status, outcome.reason) == ("error", reason), _view(outcome)
    assert kafka.names() == ["list_topics"]


@pytest.mark.parametrize("name", ["UNKNOWN_TOPIC_OR_PART", "UNKNOWN_TOPIC_ID"])
def test_unknown_topic_errors_are_no_data_topic_not_found(kafka, name):
    kafka.fail["describe_topics"] = kafka.m.error(name)
    _no_data_with(_run(kafka, TOPIC), "TOPIC_NOT_FOUND")


def test_a_missing_config_topic_is_no_data_topic_not_found(kafka):
    outcome = _run(kafka, CONFIGS, {"resource_type": "topic", "resource_name": "ghost"})
    _no_data_with(outcome, "TOPIC_NOT_FOUND")


def test_other_errors_echo_a_short_redacted_name_and_description(kafka):
    detail = f"policy rejected the request password={CANARY} " + "z" * 20000
    kafka.fail["describe_cluster"] = kafka.m.error("POLICY_VIOLATION", detail)
    outcome = _run(kafka, BROKERS)
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_ERROR"), _view(outcome)
    text = _text(outcome)
    assert "POLICY_VIOLATION" in text
    assert CANARY not in text
    assert "z" * 5000 not in text


def test_a_request_past_the_call_deadline_is_a_timeout(kafka):
    kafka.hang.add("describe_cluster")
    executor, _ = _worker(_env(), deadline_seconds=2)
    started = time.monotonic()
    outcome = executor.execute(_call(BROKERS))
    assert outcome.status == "timeout", _view(outcome)
    assert time.monotonic() - started < 6
    for _name_, _args, kwargs in kafka.calls:
        timeout = kwargs.get("request_timeout")
        assert isinstance(timeout, (int, float)) and 0 < timeout <= 2.5, kwargs


# == ⑧ credentials: the kafka-ro slot, no fallback, never shown =====================


def test_sasl_settings_reach_only_the_client_configuration(kafka):
    env = _sasl_env(
        OPSPILOT_ES_URL="http://127.0.0.1:9200",
        OPSPILOT_ES_API_KEY=ES_API_KEY,
        OPSPILOT_LOKI_URL="http://127.0.0.1:3100",
        OPSPILOT_LOKI_BEARER_TOKEN=LOKI_BEARER,
    )
    _ok(_run(kafka, BROKERS, env=env))
    (conf,) = kafka.configs
    assert str(conf["security.protocol"]) == "SASL_PLAINTEXT"
    assert str(_conf_value(conf, "sasl.mechanism", "sasl.mechanisms")) == "PLAIN"
    assert str(conf["sasl.username"]) == KAFKA_USERNAME
    assert str(conf["sasl.password"]) == KAFKA_PASSWORD
    values = json.dumps({k: str(v) for k, v in conf.items()})
    for other in (ES_API_KEY, LOKI_BEARER, PROM_PASSWORD, K8S_TOKEN):
        assert other not in values


@pytest.mark.parametrize("mechanism", ["PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"])
def test_each_sasl_mechanism_is_passed(kafka, mechanism):
    _ok(_run(kafka, BROKERS, env=_sasl_env(OPSPILOT_KAFKA_SASL_MECHANISM=mechanism)))
    assert (
        str(_conf_value(kafka.configs[0], "sasl.mechanism", "sasl.mechanisms"))
        == mechanism
    )


@pytest.mark.parametrize("protocol", ["SSL", "SASL_SSL"])
def test_tls_uses_the_configured_ca(kafka, protocol):
    ca = os.environ["C3_TEST_KAFKA_CA_FILE"]
    extra = {"OPSPILOT_KAFKA_SECURITY_PROTOCOL": protocol, "OPSPILOT_KAFKA_CA_FILE": ca}
    env = _sasl_env(**extra) if protocol == "SASL_SSL" else _env(**extra)
    _ok(_run(kafka, BROKERS, env=env))
    conf = kafka.configs[0]
    assert str(conf["security.protocol"]) == protocol
    assert str(conf["ssl.ca.location"]) == ca
    assert not any(k.startswith("ssl.") and "key" in k for k in conf)  # no mTLS


def test_no_fallback_to_another_slot_without_the_kafka_password(kafka):
    _require_jq()
    env = _sasl_env(
        OPSPILOT_KAFKA_PASSWORD="",
        OPSPILOT_ES_URL="http://127.0.0.1:9200",
        OPSPILOT_ES_API_KEY=ES_API_KEY,
        OPSPILOT_VICTORIALOGS_URL="http://127.0.0.1:9428",
        OPSPILOT_VICTORIALOGS_BEARER_TOKEN=VL_BEARER,
    )
    with pytest.raises(SystemExit, match="KAFKA_CONFIG_MISSING"):
        _worker(env)
    assert kafka.configs == []


def test_the_password_never_appears_in_views_notes_evidence_or_trace(kafka):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from tests.test_tracing import lab_env

    exporter = InMemorySpanExporter()
    tracer = tracing.configure(
        lab_env(
            OPSPILOT_TOOL_PROFILE=EXT_PROFILE,
            OPSPILOT_K8S_API_URL=K8S_API,
            OPSPILOT_KAFKA_BOOTSTRAP_SERVERS=BOOTSTRAP,
            OPSPILOT_KAFKA_SECURITY_PROTOCOL="SASL_PLAINTEXT",
            OPSPILOT_KAFKA_USERNAME=KAFKA_USERNAME,
            OPSPILOT_KAFKA_PASSWORD=KAFKA_PASSWORD,
            OPSPILOT_OTEL_PROMETHEUS_URL="http://127.0.0.1:19090",
            OPSPILOT_OTEL_JAEGER_URL="http://127.0.0.1:16686",
        ),
        exporter=exporter,
    )
    outcomes: list[Any] = []
    try:
        executor, sink = _worker(_sasl_env())
        with tracer.run(incident_id="c3-incident", run_id=RUN_ID, versions={}):
            for index, tool in enumerate(TOOLS):
                outcomes.append(executor.execute(_call(tool, index=index)))
            kafka.fail["describe_cluster"] = kafka.m.error(
                "SASL_AUTHENTICATION_FAILED", f"bad password {KAFKA_PASSWORD}"
            )
            outcomes.append(executor.execute(_call(BROKERS, index=20)))
            kafka.fail["describe_cluster"] = kafka.m.error(
                "POLICY_VIOLATION", f"rejected sasl.password={KAFKA_PASSWORD}"
            )
            outcomes.append(executor.execute(_call(BROKERS, index=21)))
            outcomes.append(executor.execute(_call(GROUP, {"group_id": ""}, index=22)))
        tracer.shutdown()
        spans = exporter.get_finished_spans()
        records = list(sink.records)
    finally:
        tracing.reset()
    assert spans
    exported = json.dumps([dict(span.attributes or {}) for span in spans], default=str)
    rendered = json.dumps([o.model_view for o in outcomes], default=str)
    raw = b"".join((r.raw or b"") for r in records)
    selectors = json.dumps([dict(r.selector) for r in kafka.requests], default=str)
    assert KAFKA_PASSWORD not in exported
    assert KAFKA_PASSWORD not in rendered
    assert KAFKA_PASSWORD.encode() not in raw
    assert KAFKA_PASSWORD not in selectors


# == ⑨ startup codes, and the web does not read the credentials =====================


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        # SASL needs both halves.
        ({"OPSPILOT_KAFKA_SECURITY_PROTOCOL": "SASL_PLAINTEXT"}, "KAFKA_CONFIG_MISSING"),
        (
            {"OPSPILOT_KAFKA_SECURITY_PROTOCOL": "SASL_SSL",
             "OPSPILOT_KAFKA_USERNAME": KAFKA_USERNAME},
            "KAFKA_CONFIG_MISSING",
        ),
        (
            {"OPSPILOT_KAFKA_SECURITY_PROTOCOL": "SASL_PLAINTEXT",
             "OPSPILOT_KAFKA_PASSWORD": KAFKA_PASSWORD},
            "KAFKA_CONFIG_MISSING",
        ),
        # Address format.
        ({"OPSPILOT_KAFKA_BOOTSTRAP_SERVERS": "kafka://localhost:9092"}, "KAFKA_CONFIG_INVALID"),
        ({"OPSPILOT_KAFKA_BOOTSTRAP_SERVERS": "PLAINTEXT://localhost:9092"}, "KAFKA_CONFIG_INVALID"),
        ({"OPSPILOT_KAFKA_BOOTSTRAP_SERVERS": "localhost"}, "KAFKA_CONFIG_INVALID"),
        ({"OPSPILOT_KAFKA_BOOTSTRAP_SERVERS": "localhost:0"}, "KAFKA_CONFIG_INVALID"),
        ({"OPSPILOT_KAFKA_BOOTSTRAP_SERVERS": "localhost:65536"}, "KAFKA_CONFIG_INVALID"),
        ({"OPSPILOT_KAFKA_BOOTSTRAP_SERVERS": "localhost:port"}, "KAFKA_CONFIG_INVALID"),
        ({"OPSPILOT_KAFKA_BOOTSTRAP_SERVERS": "localhost:9092,,localhost:9093"}, "KAFKA_CONFIG_INVALID"),
        (
            {"OPSPILOT_KAFKA_BOOTSTRAP_SERVERS": ",".join(f"localhost:{9000 + n}" for n in range(11))},
            "KAFKA_CONFIG_INVALID",
        ),
        # Protocol and mechanism values.
        ({"OPSPILOT_KAFKA_SECURITY_PROTOCOL": "SASL"}, "KAFKA_CONFIG_INVALID"),
        ({"OPSPILOT_KAFKA_SECURITY_PROTOCOL": "TLS"}, "KAFKA_CONFIG_INVALID"),
        (
            {"OPSPILOT_KAFKA_SECURITY_PROTOCOL": "SASL_PLAINTEXT",
             "OPSPILOT_KAFKA_USERNAME": KAFKA_USERNAME,
             "OPSPILOT_KAFKA_PASSWORD": KAFKA_PASSWORD,
             "OPSPILOT_KAFKA_SASL_MECHANISM": "GSSAPI"},
            "KAFKA_CONFIG_INVALID",
        ),
        (
            {"OPSPILOT_KAFKA_SECURITY_PROTOCOL": "SASL_PLAINTEXT",
             "OPSPILOT_KAFKA_USERNAME": KAFKA_USERNAME,
             "OPSPILOT_KAFKA_PASSWORD": KAFKA_PASSWORD,
             "OPSPILOT_KAFKA_SASL_MECHANISM": "OAUTHBEARER"},
            "KAFKA_CONFIG_INVALID",
        ),
        # Credentials or a mechanism without SASL.
        ({"OPSPILOT_KAFKA_USERNAME": KAFKA_USERNAME}, "KAFKA_CONFIG_INVALID"),
        ({"OPSPILOT_KAFKA_PASSWORD": KAFKA_PASSWORD}, "KAFKA_CONFIG_INVALID"),
        ({"OPSPILOT_KAFKA_SASL_MECHANISM": "PLAIN"}, "KAFKA_CONFIG_INVALID"),
        (
            {"OPSPILOT_KAFKA_SECURITY_PROTOCOL": "SSL",
             "OPSPILOT_KAFKA_USERNAME": KAFKA_USERNAME,
             "OPSPILOT_KAFKA_PASSWORD": KAFKA_PASSWORD},
            "KAFKA_CONFIG_INVALID",
        ),
        # A CA file only with TLS.
        ({"OPSPILOT_KAFKA_CA_FILE": "/run/secrets/kafka-ca.pem"}, "KAFKA_CONFIG_INVALID"),
        (
            {"OPSPILOT_KAFKA_SECURITY_PROTOCOL": "SASL_PLAINTEXT",
             "OPSPILOT_KAFKA_USERNAME": KAFKA_USERNAME,
             "OPSPILOT_KAFKA_PASSWORD": KAFKA_PASSWORD,
             "OPSPILOT_KAFKA_CA_FILE": "/run/secrets/kafka-ca.pem"},
            "KAFKA_CONFIG_INVALID",
        ),
    ],
)  # fmt: skip
def test_worker_refuses_a_bad_kafka_configuration(kafka, overrides, code):
    _require_jq()
    with pytest.raises(SystemExit, match=code):
        _worker(_env(**overrides))
    assert kafka.configs == []


@pytest.mark.parametrize("name", KAFKA_ENV_NAMES[1:])
def test_settings_without_the_address_are_invalid_for_the_worker(kafka, name):
    _require_jq()
    value = {
        "OPSPILOT_KAFKA_SECURITY_PROTOCOL": "PLAINTEXT",
        "OPSPILOT_KAFKA_SASL_MECHANISM": "PLAIN",
        "OPSPILOT_KAFKA_USERNAME": KAFKA_USERNAME,
        "OPSPILOT_KAFKA_PASSWORD": KAFKA_PASSWORD,
        "OPSPILOT_KAFKA_CA_FILE": "/run/secrets/kafka-ca.pem",
    }[name]
    with pytest.raises(SystemExit, match="KAFKA_CONFIG_INVALID"):
        _worker(_env(None, **{name: value}))


@pytest.mark.parametrize(
    "env",
    [
        # Anonymous PLAINTEXT (the SREGym question, C3-U1 = a).
        {},
        {"OPSPILOT_KAFKA_SECURITY_PROTOCOL": "PLAINTEXT"},
        {"OPSPILOT_KAFKA_BOOTSTRAP_SERVERS": "127.0.0.1:1,localhost:65535"},
        {
            "OPSPILOT_KAFKA_BOOTSTRAP_SERVERS": ",".join(
                f"localhost:{9000 + n}" for n in range(10)
            )
        },
        {"OPSPILOT_KAFKA_SECURITY_PROTOCOL": "SSL"},
    ],
)
def test_valid_configurations_start_and_answer(kafka, env):
    outcome = _run(kafka, BROKERS, env=_env(**env))
    _ok(outcome)
    assert len(kafka.configs) == 1


class _TrackingEnv(Mapping[str, str]):
    def __init__(self, values: Mapping[str, str]):
        self._values = dict(values)
        self.read: set[str] = set()
        self.iterated = False

    def __getitem__(self, key: str) -> str:
        self.read.add(key)
        return self._values[key]

    def __iter__(self):
        self.iterated = True
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def test_web_side_profile_reads_no_kafka_credential():
    env = _TrackingEnv(
        _sasl_env(
            OPSPILOT_KAFKA_SECURITY_PROTOCOL="SASL_SSL",
            OPSPILOT_KAFKA_SASL_MECHANISM="SCRAM-SHA-512",
            OPSPILOT_KAFKA_CA_FILE="/nonexistent/kafka-ca.pem",
        )
    )
    profile = select_profile(env)
    face = profile.face(FakeClock(start=NOW))
    face.evidence_context(RUN_ID)
    profile.versions()
    assert set(TOOLS) <= {s["function"]["name"] for s in face.tool_schemas}
    assert not env.read & set(KAFKA_CREDENTIAL_NAMES), env.read
    assert not env.iterated


def test_startup_codes_end_the_process():
    from opspilot.tools import profiles

    assert {"KAFKA_CONFIG_MISSING", "KAFKA_CONFIG_INVALID"} <= set(
        profiles._STARTUP_CODES
    )


def test_entrypoint_imports_kafka_settings_from_the_worker_env_file(tmp_path):
    entrypoint = ROOT / "deploy" / "docker-entrypoint.sh"
    worker_env = tmp_path / "worker_env"
    worker_env.write_text(
        "OPSPILOT_KAFKA_BOOTSTRAP_SERVERS=127.0.0.1:9092\n"
        "OPSPILOT_KAFKA_SECURITY_PROTOCOL=SASL_PLAINTEXT\n"
        "OPSPILOT_KAFKA_SASL_MECHANISM=SCRAM-SHA-256\n"
        f"OPSPILOT_KAFKA_USERNAME={KAFKA_USERNAME}\n"
        f"OPSPILOT_KAFKA_PASSWORD='{KAFKA_PASSWORD}'\n"
        "OPSPILOT_KAFKA_CA_FILE=/run/secrets/kafka-ca.pem\n"
        "OPSPILOT_KAFKAX=not-a-setting\n"
        "OPSPILOT_KAFKA_$(touch pwned)=x\n"
    )
    result = subprocess.run(
        ["sh", str(entrypoint), "env"],
        env={"PATH": "/usr/bin:/bin", "OPSPILOT_ENV_FILE": str(worker_env)},
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    env = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert env["OPSPILOT_KAFKA_BOOTSTRAP_SERVERS"] == "127.0.0.1:9092"
    assert env["OPSPILOT_KAFKA_SECURITY_PROTOCOL"] == "SASL_PLAINTEXT"
    assert env["OPSPILOT_KAFKA_SASL_MECHANISM"] == "SCRAM-SHA-256"
    assert env["OPSPILOT_KAFKA_USERNAME"] == KAFKA_USERNAME
    assert env["OPSPILOT_KAFKA_PASSWORD"] == KAFKA_PASSWORD
    assert env["OPSPILOT_KAFKA_CA_FILE"] == "/run/secrets/kafka-ca.pem"
    assert "OPSPILOT_KAFKAX" not in env
    assert not (tmp_path / "pwned").exists()


# == ⑪ lab gate ======================================================================


def test_lab_gate_checks_every_bootstrap_address():
    env = {
        "OPSPILOT_TRACE": "lab",
        PROFILE_ENV: EXT_PROFILE,
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686",
        "OPSPILOT_K8S_API_URL": K8S_API,
        "LANGSMITH_PROJECT": "opspilot-lab-c3",
        "LANGSMITH_API_KEY": "lsv2-aaaaaaaa567",
        "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
    }
    assert check_lab_target(env) == ()
    for lab in (
        "localhost:9092",
        "127.0.0.1:9092",
        "127.0.0.1:9092,localhost:9094",
        "kafka.astronomy-shop.svc:9092",
    ):
        assert (
            check_lab_target({**env, "OPSPILOT_KAFKA_BOOTSTRAP_SERVERS": lab}) == ()
        ), lab
    for remote in (
        "kafka.example.invalid:9092",
        "127.0.0.1:9092,kafka.example.invalid:9092",
        "localhost:9092,10.1.2.3:9092",
        "127.attacker.example:9092",
    ):
        failures = check_lab_target({**env, "OPSPILOT_KAFKA_BOOTSTRAP_SERVERS": remote})
        assert "KAFKA_NOT_LAB" in failures, remote
    others = check_lab_target(
        {**env, "OPSPILOT_ES_URL": "https://logs.example.invalid:9200"}
    )
    assert "KAFKA_NOT_LAB" not in others
