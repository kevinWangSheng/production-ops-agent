"""Kafka read-only tools (C3, #567): topics, consumer groups and their lag,
brokers and configuration -- never message content.

The upstream HolmesGPT ``kafka/admin`` toolset (``holmes/plugins/toolsets/
kafka.py`` at ``a0009195``) without ``consume_messages`` and
``list_kafka_clusters``, and without the ``kafka_cluster_name`` parameter:
one deployment reads one cluster, configured by the operator. The public
interface is ``docs/tasks/2026-11-20-tool-expansion-c3.md``; the decisions
are r11 C3-U1 = a, C3-U2 = a and C3-1 to C3-14.

Read-only by construction (C3-3):

* The only ``confluent_kafka`` object is the admin client, and the only
  methods called on it are the eight reads ``list_topics`` (never with a
  topic: a metadata request naming a topic is what makes a broker auto-create
  it), ``describe_topics``, ``describe_cluster``, ``list_consumer_groups``,
  ``describe_consumer_groups``, ``list_consumer_group_offsets``,
  ``list_offsets`` and ``describe_configs``. None of them returns a message.
  Upstream computes lag with a ``Consumer`` that joins the target group; here
  committed offsets come from ``list_consumer_group_offsets`` and the
  partition bounds from ``list_offsets``. The ruff ban list and an AST
  contract test hold the line.
* The client configuration fixes ``allow.auto.create.topics=false`` and a
  topic named by the model is looked up with ``describe_topics`` only.
* One short-lived client per call (C3-9), created by an injectable factory;
  ``confluent_kafka`` is imported lazily, so the web never loads librdkafka.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from opspilot.tools import otel_demo
from opspilot.tools.executor import (
    TransportError,
    TransportRequest,
    TransportResponse,
    TransportResultTooLarge,
    TransportTimeout,
    TransportUnavailable,
)
from opspilot.tools.registry import (
    MAX_VIEW_TOKENS,
    ParameterSpec,
    ToolContractError,
    ToolDescription,
    ToolRegistration,
    canonical,
)

__all__ = [
    "BOOTSTRAP_ENV",
    "CREDENTIAL_REF",
    "ENDPOINT_SELECTOR",
    "KafkaConfig",
    "KafkaTransport",
    "TOOL_NAMES",
    "TOOL_SCHEMAS",
    "kafka_enabled",
    "kafka_registrations",
]

CONSUMERS_TOOL = "list_kafka_consumers"
GROUP_TOOL = "describe_consumer_group"
TOPICS_TOOL = "list_topics"
TOPIC_TOOL = "describe_topic"
FIND_TOOL = "find_consumer_groups_by_topic"
BROKERS_TOOL = "list_brokers"
CONFIGS_TOOL = "describe_configs"
OVERVIEW_TOOL = "cluster_overview"
TOOL_NAMES: tuple[str, ...] = (
    CONSUMERS_TOOL,
    GROUP_TOOL,
    TOPICS_TOOL,
    TOPIC_TOOL,
    FIND_TOOL,
    BROKERS_TOOL,
    CONFIGS_TOOL,
    OVERVIEW_TOOL,
)

#: C3-4: the transport's own credential slot.
CREDENTIAL_REF = "kafka-ro"
#: C3-8: where the bootstrap address sits in the registered target's selector.
ENDPOINT_SELECTOR = "kafka_bootstrap"

BOOTSTRAP_ENV = "OPSPILOT_KAFKA_BOOTSTRAP_SERVERS"
PROTOCOL_ENV = "OPSPILOT_KAFKA_SECURITY_PROTOCOL"
MECHANISM_ENV = "OPSPILOT_KAFKA_SASL_MECHANISM"
USERNAME_ENV = "OPSPILOT_KAFKA_USERNAME"
PASSWORD_ENV = "OPSPILOT_KAFKA_PASSWORD"
CA_FILE_ENV = "OPSPILOT_KAFKA_CA_FILE"
_OTHER_ENVS = (PROTOCOL_ENV, MECHANISM_ENV, USERNAME_ENV, PASSWORD_ENV, CA_FILE_ENV)

PROTOCOLS = ("PLAINTEXT", "SSL", "SASL_PLAINTEXT", "SASL_SSL")
MECHANISMS = ("PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512")
MAX_BOOTSTRAP_SERVERS = 10

REQUEST_TIMEOUT_SECONDS = 30.0
MAX_RESULT_BYTES = 2 * 1024 * 1024
#: Row caps per tool (lead ruling 2026-11-20): set from the measured tokens
#: per row so that a full page still fits the 25k-token view.
MAX_ROWS = 1000
MAX_GROUP_ROWS = 800
MAX_PARTITION_ROWS = 500
MAX_SCANNED_GROUPS = 200
MAX_VALUE_CHARS = 2000
MAX_GROUP_CHARS = 255
MAX_NOTE_CHARS = 500
CONNECTION_SETUP_TIMEOUT_MS = 10000
CLIENT_ID = "opspilot-ro"

_TOPIC = re.compile(r"[A-Za-z0-9._-]{1,249}")
_BROKER_ID = re.compile(r"[0-9]{1,10}")
_HOST_PORT = re.compile(r"(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+):([0-9]{1,5})")

_LOG = logging.getLogger("opspilot.tools.kafka")

ClientFactory = Callable[[dict[str, Any]], Any]


# -- configuration (C3-4) --------------------------------------------------------


def kafka_enabled(env: Mapping[str, str]) -> bool:
    """B1-U2 for Kafka: whether the extended profile shows the eight tools.
    Only the address is read -- the web never reads a credential."""
    return bool(env.get(BOOTSTRAP_ENV))


def _clean_bootstrap(value: str) -> str:
    invalid = ToolContractError("KAFKA_CONFIG_INVALID")
    entries = value.split(",")
    if not entries or len(entries) > MAX_BOOTSTRAP_SERVERS:
        raise invalid
    for entry in entries:
        match = _HOST_PORT.fullmatch(entry)
        if match is None or not 1 <= int(match.group(2)) <= 65535:
            raise invalid
    return value


def _printable(value: str) -> bool:
    return bool(value) and value.isprintable()


@dataclass(frozen=True)
class KafkaConfig:
    """The worker's ``kafka-ro`` connection (C3-4): bootstrap address, security
    protocol and, for ``SASL_*``, the mechanism with a user name and password;
    a CA file for TLS. Anonymous ``PLAINTEXT`` is valid (C3-U1 = a)."""

    bootstrap_servers: str
    security_protocol: str = "PLAINTEXT"
    sasl_mechanism: str | None = None
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    ca_file: str | None = None

    def __post_init__(self) -> None:
        invalid = ToolContractError("KAFKA_CONFIG_INVALID")
        if not isinstance(self.bootstrap_servers, str) or not self.bootstrap_servers:
            raise invalid
        _clean_bootstrap(self.bootstrap_servers)
        if self.security_protocol not in PROTOCOLS:
            raise invalid
        sasl = self.security_protocol.startswith("SASL_")
        tls = self.security_protocol in ("SSL", "SASL_SSL")
        if sasl:
            if self.sasl_mechanism is None:
                object.__setattr__(self, "sasl_mechanism", "PLAIN")
            elif self.sasl_mechanism not in MECHANISMS:
                raise invalid
            if self.username is None or self.password is None:
                raise ToolContractError("KAFKA_CONFIG_MISSING")
            if not _printable(self.username) or not _printable(self.password):
                raise invalid
        elif (
            self.sasl_mechanism is not None
            or self.username is not None
            or self.password is not None
        ):
            raise invalid
        if self.ca_file is not None:
            if not tls:
                raise invalid
            path = Path(self.ca_file)
            if not path.is_file() or not os.access(path, os.R_OK):
                raise invalid

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> KafkaConfig | None:
        """``OPSPILOT_KAFKA_*`` (C3-4). No address: ``None`` -- unless another
        of the variables is set, which is ``KAFKA_CONFIG_INVALID`` so a
        setting is never silently ignored."""
        bootstrap = env.get(BOOTSTRAP_ENV) or ""
        values = {name: env.get(name) or None for name in _OTHER_ENVS}
        if not bootstrap:
            if any(values.values()):
                raise ToolContractError("KAFKA_CONFIG_INVALID")
            return None
        return cls(
            bootstrap_servers=bootstrap,
            security_protocol=values[PROTOCOL_ENV] or "PLAINTEXT",
            sasl_mechanism=values[MECHANISM_ENV],
            username=values[USERNAME_ENV],
            password=values[PASSWORD_ENV],
            ca_file=values[CA_FILE_ENV],
        )

    def client_conf(self, bootstrap: str, timeout_seconds: float) -> dict[str, Any]:
        """The admin client's configuration; the read-only settings are fixed
        here, never taken from the environment (C3-3, C3-9)."""
        conf: dict[str, Any] = {
            "bootstrap.servers": bootstrap,
            "client.id": CLIENT_ID,
            "allow.auto.create.topics": "false",
            "socket.connection.setup.timeout.ms": str(CONNECTION_SETUP_TIMEOUT_MS),
            "socket.timeout.ms": str(max(10, int(timeout_seconds * 1000))),
            "security.protocol": self.security_protocol,
            "logger": _LOG,
        }
        if self.security_protocol.startswith("SASL_"):
            conf["sasl.mechanism"] = self.sasl_mechanism
            conf["sasl.username"] = self.username
            conf["sasl.password"] = self.password
        if self.ca_file is not None:
            conf["ssl.ca.location"] = self.ca_file
        return conf


def _admin_client(conf: dict[str, Any]) -> Any:
    from confluent_kafka.admin import AdminClient

    return AdminClient(conf)


# -- errors (C3-10) ----------------------------------------------------------------

_UNAVAILABLE = frozenset(
    {
        "_TRANSPORT",
        "_ALL_BROKERS_DOWN",
        "_RESOLVE",
        "NOT_COORDINATOR",
        "COORDINATOR_NOT_AVAILABLE",
        "COORDINATOR_LOAD_IN_PROGRESS",
    }
)
_TIMED_OUT = frozenset({"_TIMED_OUT", "REQUEST_TIMED_OUT"})
_FORBIDDEN_EXTRA = frozenset({"SASL_AUTHENTICATION_FAILED", "_AUTHENTICATION"})
_TOPIC_MISSING = frozenset({"UNKNOWN_TOPIC_OR_PART", "UNKNOWN_TOPIC_ID"})
_GROUP_MISSING = frozenset({"GROUP_ID_NOT_FOUND"})

FORBIDDEN_CODE = "KAFKA_FORBIDDEN"
INVALID_CODE = "KAFKA_INVALID_REQUEST"
ERROR_CODE = "KAFKA_ERROR"


class _Missing(Exception):
    """A topic or group the cluster does not have: a fact, not a failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _HandleGone(TransportUnavailable):
    """``_DESTROY_BROKER``: librdkafka retired the broker handle a request
    was waiting on -- the bootstrap handle is replaced once the first
    metadata names the brokers, which a request sent by a brand-new client
    can race (CI, PR #748). Nothing reached a broker's state; the call's
    reads are re-run once on the same client (C3 implementation decision)."""


class _Refusal(Exception):
    def __init__(self, response: TransportResponse, kafka_name: str = "") -> None:
        super().__init__()
        self.response = response
        self.kafka_name = kafka_name


def _error_name(error: object) -> tuple[str, str]:
    """The ``KafkaError`` name and description inside ``error`` (a
    ``KafkaException``, or a bare ``KafkaError``)."""
    args = getattr(error, "args", None)
    inner: object = args[0] if isinstance(args, tuple) and args else error
    name_of = getattr(inner, "name", None)
    text_of = getattr(inner, "str", None)
    name = str(name_of()) if callable(name_of) else ""
    text = str(text_of()) if callable(text_of) else ""
    return name, text


def _note_text(text: str) -> str:
    text = "".join(ch if ch.isprintable() else " " for ch in text)
    return text[:MAX_NOTE_CHARS]


def _classify(error: object, missing: str | None) -> BaseException:
    """What a Kafka failure becomes: a transport exception, a fixed-code
    response wrapped in ``_Refusal``, or ``_Missing``."""
    name, text = _error_name(error)
    if missing is not None and name in _TOPIC_MISSING:
        return _Missing("TOPIC_NOT_FOUND" if missing == "topic" else missing)
    if name in _GROUP_MISSING:
        return _Missing("GROUP_NOT_FOUND")
    if name == "_DESTROY_BROKER":
        return _HandleGone(name)
    if name in _UNAVAILABLE:
        return TransportUnavailable(name)
    if name in _TIMED_OUT:
        return TransportTimeout(name)
    if name.endswith("_AUTHORIZATION_FAILED") or name in _FORBIDDEN_EXTRA:
        return _forbidden(name)
    if name.startswith("INVALID_"):
        return _Refusal(
            TransportResponse(
                body=canonical({"error": INVALID_CODE}).encode(),
                source_status=INVALID_CODE,
                view_fields={"notes": [f"{INVALID_CODE}: {name}: {_note_text(text)}"]},
            ),
            name,
        )
    label = name or type(error).__name__
    if not name:
        _LOG.warning("kafka tool failure: %s", type(error).__name__)
    return _Refusal(
        TransportResponse(
            body=canonical({"error": ERROR_CODE}).encode(),
            source_status=ERROR_CODE,
            view_fields={"notes": [f"{ERROR_CODE}: {label}: {_note_text(text)}"]},
        ),
        name,
    )


_LOGIN_CHECK_SECONDS = 0.5


def _forbidden(name: str) -> _Refusal:
    return _Refusal(
        TransportResponse(
            body=canonical({"error": FORBIDDEN_CODE}).encode(),
            source_status=FORBIDDEN_CODE,
            view_fields={
                "notes": [
                    f"{FORBIDDEN_CODE}: the broker refused this read for the "
                    f"configured identity ({name})."
                ]
            },
        )
    )


def _refused(code: str, text: str) -> TransportResponse:
    """A fixed-code refusal decided before anything was sent; ``notes`` carry
    the code and what to change (B1-6)."""
    return TransportResponse(
        body=canonical({"error": code}).encode(),
        source_status=code,
        sent=False,
        view_fields={"notes": [f"{code}: {text}"]},
    )


# -- projection helpers -------------------------------------------------------------


def _enum_name(value: object) -> str | None:
    if value is None:
        return None
    name = getattr(value, "name", None)
    return str(name if name is not None else value).rsplit(".", 1)[-1]


def _node_id(node: object) -> int | None:
    if node is None:
        return None
    value = getattr(node, "id", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _cut(value: Any, counter: list[int]) -> Any:
    """Every string value cut to ``MAX_VALUE_CHARS`` (C3-8)."""
    if isinstance(value, str):
        if len(value) > MAX_VALUE_CHARS:
            counter[0] += 1
            return value[:MAX_VALUE_CHARS]
        return value
    if isinstance(value, list):
        return [_cut(item, counter) for item in value]
    if isinstance(value, dict):
        return {key: _cut(item, counter) for key, item in value.items()}
    return value


def _offset(value: object) -> int | None:
    """A real offset, or ``None`` (``OFFSET_INVALID`` -1001 and every other
    negative value mean "nothing committed")."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


@dataclass
class _Call:
    """One tool call: the client, the deadline and the request kwargs."""

    client: Any
    deadline: float
    monotonic: Callable[[], float]
    #: Error names librdkafka reported through ``error_cb`` (not per request).
    seen: list[str] = field(default_factory=list)
    #: Partition-level errors the call answered around (shown in notes).
    partition_errors: dict[str, int] = field(default_factory=dict)

    def refused_login(self) -> bool:
        """Whether the client reported a failed login: librdkafka keeps
        retrying and never fails the request itself (CI, PR #748), so a
        wrong password would otherwise end as a timeout."""
        return any(name in _FORBIDDEN_EXTRA for name in self.seen)

    def remaining(self) -> float:
        left = self.deadline - self.monotonic()
        if left <= 0:
            raise TransportTimeout("DEADLINE")
        return left

    def kwargs(self) -> dict[str, float]:
        return {"request_timeout": self.remaining()}

    def result(self, future: Any, missing: str | None = None) -> Any:
        try:
            while True:
                # Short waits so a failed login ends the call at once.
                wait = min(_LOGIN_CHECK_SECONDS, self.remaining())
                try:
                    return future.result(timeout=wait)
                except concurrent.futures.TimeoutError:
                    self.client.poll(0)
                    if self.refused_login():
                        raise _forbidden("SASL_AUTHENTICATION_FAILED") from None
        except (TransportError, _Missing, _Refusal):
            raise
        except Exception as error:
            raise _classify(error, missing) from None


# -- lag (C3-7) ---------------------------------------------------------------------


def _lag_rows(
    call: _Call,
    group_id: str,
    assigned: Iterable[tuple[str, int]],
) -> list[dict[str, Any]]:
    """One row per partition of (assigned ∪ committed), sorted by topic and
    partition, with committed, end and log-start offsets, the lag and its
    flags. The commit metadata string is never read into a row."""
    from confluent_kafka import ConsumerGroupTopicPartitions

    futures = call.client.list_consumer_group_offsets(
        [ConsumerGroupTopicPartitions(group_id)], **call.kwargs()
    )
    committed: dict[tuple[str, int], int | None] = {}
    for future in futures.values():
        answer = call.result(future, missing=None)
        for tp in getattr(answer, "topic_partitions", None) or []:
            committed[(str(tp.topic), int(tp.partition))] = _offset(tp.offset)
    keys = sorted(set(assigned) | set(committed))
    if not keys:
        return []
    ends = _bounds(call, keys, "latest")
    starts = _bounds(call, keys, "earliest")
    rows: list[dict[str, Any]] = []
    for topic, partition in keys:
        done = committed.get((topic, partition))
        end = ends.get((topic, partition))
        start = starts.get((topic, partition))
        flags: list[str] = []
        lag: int | None
        if done is None:
            lag = None
            flags.append("no_committed_offset")
        elif end is None:
            lag = None
            flags.append("end_offset_unavailable")
        elif done > end:
            lag = 0
            flags.append("committed_ahead_of_end")
        else:
            lag = end - done
            if start is not None and done < start:
                flags.append("committed_below_log_start")
        rows.append(
            {
                "topic": topic,
                "partition": partition,
                "committed_offset": done,
                "end_offset": end,
                "log_start_offset": start,
                "lag": lag,
                "flags": flags,
            }
        )
    return rows


def _bounds(
    call: _Call, keys: Sequence[tuple[str, int]], kind: str
) -> dict[tuple[str, int], int | None]:
    """Each partition's end (``latest``, high watermark, read_uncommitted) or
    log start (``earliest``) offset; ``None`` where the partition is gone."""
    from confluent_kafka import IsolationLevel, TopicPartition
    from confluent_kafka.admin import OffsetSpec  # type: ignore[attr-defined]

    spec = (
        OffsetSpec.latest()  # type: ignore[no-untyped-call]
        if kind == "latest"
        else OffsetSpec.earliest()  # type: ignore[no-untyped-call]
    )
    isolation = IsolationLevel.READ_UNCOMMITTED
    request = {TopicPartition(topic, partition): spec for topic, partition in keys}
    futures = call.client.list_offsets(
        request, isolation_level=isolation, **call.kwargs()
    )
    out: dict[tuple[str, int], int | None] = {}
    for tp, future in futures.items():
        key = (str(tp.topic), int(tp.partition))
        answer, error = _partition_result(call, future)
        if error is not None:
            # One offline or deleted partition must not hide the others
            # (review, PR #748): its offset is unknown, the rest stand.
            out[key] = None
            call.partition_errors[error] = call.partition_errors.get(error, 0) + 1
            continue
        out[key] = _offset(getattr(answer, "offset", None))
    return out


def _partition_result(call: _Call, future: Any) -> tuple[Any, str | None]:
    """One partition's answer, or the Kafka error name it failed with.

    Call-level failures still end the call: a timeout or unreachable cluster,
    an authorization or login failure, and a retired broker handle (re-run
    once by ``fetch``). Every other error belongs to this partition only
    (``LEADER_NOT_AVAILABLE``, ``NOT_LEADER_FOR_PARTITION``,
    ``UNKNOWN_TOPIC_OR_PART`` ...)."""
    try:
        return call.result(future, missing="topic"), None
    except _Missing:
        return None, "UNKNOWN_TOPIC_OR_PART"
    except _Refusal as refusal:
        if refusal.response.source_status == FORBIDDEN_CODE:
            raise
        return None, refusal.kafka_name or "UNKNOWN"


def _lag_by_topic(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for row in rows:
        lag = row["lag"]
        if isinstance(lag, int):
            totals[row["topic"]] = totals.get(row["topic"], 0) + lag
    return dict(sorted(totals.items()))


def _assignment(member: object) -> list[tuple[str, int]]:
    assignment = getattr(member, "assignment", None)
    partitions = getattr(assignment, "topic_partitions", None) or []
    return [(str(tp.topic), int(tp.partition)) for tp in partitions]


def _describe_group(call: _Call, group_id: str) -> Any:
    futures = call.client.describe_consumer_groups([group_id], **call.kwargs())
    (future,) = futures.values()
    description = call.result(future, missing=None)
    if _enum_name(getattr(description, "state", None)) == "DEAD":
        raise _Missing("GROUP_NOT_FOUND")
    return description


# -- the transport ------------------------------------------------------------------


@dataclass
class _Answer:
    rows: list[dict[str, Any]]
    unit: str
    returned: int
    incomplete: bool = False
    notes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


class KafkaTransport:
    """Serves the eight tools with one short-lived admin client per call.

    ``client_factory(conf)`` returns the admin client (a context manager);
    the default builds ``confluent_kafka.admin.AdminClient``. The bootstrap
    address comes from the request's selector, the security settings from
    ``config``.
    """

    def __init__(
        self,
        config: KafkaConfig,
        *,
        client_factory: ClientFactory | None = None,
        request_timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._config = config
        self._factory = client_factory or _admin_client
        self._timeout = request_timeout_seconds
        self._monotonic = monotonic or time.monotonic

    def fetch(self, request: TransportRequest) -> TransportResponse:
        if request.tool not in TOOL_NAMES:
            raise TransportError("TOOL_NOT_SUPPORTED")
        refusal = _check_params(request.tool, request.params)
        if refusal is not None:
            return refusal
        bootstrap = request.selector.get(ENDPOINT_SELECTOR, "")
        if not bootstrap:
            raise TransportError("KAFKA_ENDPOINT_MISSING")
        budget = min(self._timeout, request.timeout_seconds)
        deadline = self._monotonic() + budget
        conf = self._config.client_conf(bootstrap, budget)
        seen: list[str] = []
        conf["error_cb"] = lambda error: seen.append(_error_name(error)[0])
        try:
            with self._factory(conf) as client:
                call = _Call(client, deadline, self._monotonic, seen)
                try:
                    try:
                        answer = _HANDLERS[request.tool](call, request.params)
                    except _HandleGone:
                        # Once only, within the same deadline; read-only reads.
                        call.partition_errors.clear()
                        answer = _HANDLERS[request.tool](call, request.params)
                except TransportTimeout:
                    client.poll(0)
                    if call.refused_login():
                        raise _forbidden("SASL_AUTHENTICATION_FAILED") from None
                    raise
                if call.partition_errors:
                    answer.notes.append(
                        "Offsets of some partitions could not be read ("
                        + ", ".join(
                            f"{name} x{count}"
                            for name, count in sorted(call.partition_errors.items())
                        )
                        + "); their end or start offset is null and their lag "
                        "unknown (end_offset_unavailable)."
                    )
        except _Missing as missing:
            answer = _Answer(
                rows=[],
                unit=_ROW_UNITS[request.tool],
                returned=0,
                notes=[f"{missing.code}: {_MISSING_TEXT[missing.code]}"],
            )
        except _Refusal as refusal_:
            return refusal_.response
        except (TransportError, concurrent.futures.TimeoutError) as error:
            if isinstance(error, concurrent.futures.TimeoutError):
                raise TransportTimeout("TIMEOUT") from None
            raise
        except Exception as error:
            classified = _classify(error, None)
            if isinstance(classified, _Refusal):
                return classified.response
            if isinstance(classified, _Missing):
                answer = _Answer(
                    rows=[],
                    unit=_ROW_UNITS[request.tool],
                    returned=0,
                    notes=[f"{classified.code}: {_MISSING_TEXT[classified.code]}"],
                )
            else:
                raise classified from None
        counter = [0]
        rows = _cut(answer.rows, counter)
        notes = list(answer.notes)
        if counter[0]:
            notes.append(
                f"{counter[0]} string values were cut to {MAX_VALUE_CHARS} characters."
            )
        body = canonical({"results": rows}).encode("utf-8")
        if len(body) > request.max_result_bytes:
            raise TransportResultTooLarge(str(request.max_result_bytes))
        return TransportResponse(
            body=body,
            row_unit=answer.unit,
            backend_rows_returned=answer.returned,
            incomplete=answer.incomplete,
            view_fields={
                **answer.extra,
                "notes": notes,
                "cannot_prove": _CANNOT_PROVE[request.tool],
            },
        )


_MISSING_TEXT: Mapping[str, str] = {
    "TOPIC_NOT_FOUND": (
        "the cluster has no topic of this name (check the spelling with "
        "list_topics); nothing was created."
    ),
    "GROUP_NOT_FOUND": (
        "the cluster has no consumer group of this name (check it with "
        "list_kafka_consumers)."
    ),
    "BROKER_NOT_FOUND": "the cluster has no broker with this ID (see list_brokers).",
}


def _check_params(tool: str, params: Mapping[str, object]) -> TransportResponse | None:
    """C3-8: names checked before any client exists."""
    if tool in (TOPIC_TOOL, FIND_TOOL):
        name = params.get("topic_name")
        if not isinstance(name, str) or _TOPIC.fullmatch(name) is None:
            return _refused("INVALID_TOPIC", _TOPIC_RULE)
    if tool == GROUP_TOOL:
        group = params.get("group_id")
        if (
            not isinstance(group, str)
            or not _printable(group)
            or len(group) > MAX_GROUP_CHARS
        ):
            return _refused("INVALID_GROUP", _GROUP_RULE)
    if tool == CONFIGS_TOOL:
        kind = params.get("resource_type")
        name = params.get("resource_name")
        if kind == "topic":
            if not isinstance(name, str) or _TOPIC.fullmatch(name) is None:
                return _refused("INVALID_TOPIC", _TOPIC_RULE)
        elif kind == "broker":
            if not isinstance(name, str) or _BROKER_ID.fullmatch(name) is None:
                return _refused("INVALID_RESOURCE", _RESOURCE_RULE)
        else:
            return _refused("INVALID_RESOURCE", _RESOURCE_RULE)
    return None


_TOPIC_RULE = "a topic name is 1 to 249 of A-Z a-z 0-9 . _ -."
_GROUP_RULE = "a group ID is 1 to 255 printable characters."
_RESOURCE_RULE = (
    "resource_type is topic or broker; a broker's resource_name is its "
    "numeric ID (see list_brokers)."
)


# -- the eight tools ----------------------------------------------------------------


def _list_consumers(call: _Call, params: Mapping[str, object]) -> _Answer:
    future = call.client.list_consumer_groups(**call.kwargs())
    result = call.result(future)
    listings = sorted(
        getattr(result, "valid", None) or [], key=lambda item: str(item.group_id)
    )
    rows = [
        {
            "group_id": str(item.group_id),
            "state": _enum_name(getattr(item, "state", None)),
            "type": _enum_name(getattr(item, "type", None)),
            "is_simple": bool(getattr(item, "is_simple_consumer_group", False)),
        }
        for item in listings
    ]
    notes, incomplete = _listing_errors(result, bool(rows))
    answer = _capped(rows, "groups", notes, MAX_GROUP_ROWS)
    answer.incomplete = answer.incomplete or incomplete
    return answer


def _listing_errors(result: object, any_valid: bool) -> tuple[list[str], bool]:
    """``list_consumer_groups`` reports a broker it could not list in
    ``errors`` beside the groups it did list. With no group listed at all the
    first error is the answer (an empty list would claim "no groups");
    otherwise the listing is incomplete and the notes say so."""
    errors = list(getattr(result, "errors", None) or [])
    if not errors:
        return [], False
    if not any_valid:
        raise _classify(errors[0], None)
    names = sorted({_error_name(error)[0] or "UNKNOWN" for error in errors})
    return [
        f"{len(errors)} brokers did not answer the group listing "
        f"({', '.join(names)}); groups they coordinate are missing."
    ], True


def _capped(
    rows: list[dict[str, Any]],
    unit: str,
    notes: list[str] | None = None,
    limit: int = MAX_ROWS,
) -> _Answer:
    notes = list(notes or [])
    incomplete = len(rows) > limit
    if incomplete:
        notes.append(f"{len(rows)} {unit}; the first {limit} by name are shown.")
    return _Answer(
        rows=rows[:limit],
        unit=unit,
        returned=len(rows),
        incomplete=incomplete,
        notes=notes,
    )


def _describe_consumer_group(call: _Call, params: Mapping[str, object]) -> _Answer:
    group_id = str(params["group_id"])
    include = params.get("include_offsets", True)
    description = _describe_group(call, group_id)
    members_raw = list(getattr(description, "members", None) or [])
    members = []
    assigned: list[tuple[str, int]] = []
    for member in members_raw:
        partitions = _assignment(member)
        assigned.extend(partitions)
        members.append(
            {
                "member_id": getattr(member, "member_id", None),
                "client_id": getattr(member, "client_id", None),
                "host": getattr(member, "host", None),
                "group_instance_id": getattr(member, "group_instance_id", None),
                "assignment": [
                    {"topic": topic, "partition": partition}
                    for topic, partition in sorted(partitions)
                ],
            }
        )
    members.sort(key=lambda item: str(item["member_id"]))
    rows: list[dict[str, Any]] = []
    if include is not False:
        rows = _lag_rows(call, group_id, assigned)
    notes: list[str] = []
    incomplete = len(rows) > MAX_PARTITION_ROWS
    if incomplete:
        notes.append(
            f"{len(rows)} partition rows; the first {MAX_PARTITION_ROWS} by topic "
            "and partition are shown. lag_by_topic counts every partition read."
        )
    group = {
        "group_id": group_id,
        "state": _enum_name(getattr(description, "state", None)),
        "type": _enum_name(getattr(description, "type", None)),
        "coordinator": _node_id(getattr(description, "coordinator", None)),
        "partition_assignor": getattr(description, "partition_assignor", None),
        "members": members,
        "partitions": rows[:MAX_PARTITION_ROWS],
        "lag_by_topic": _lag_by_topic(rows),
    }
    return _Answer(
        rows=[group], unit="groups", returned=1, incomplete=incomplete, notes=notes
    )


def _list_topics(call: _Call, params: Mapping[str, object]) -> _Answer:
    metadata = _metadata(call)
    topics = getattr(metadata, "topics", None) or {}
    rows = [
        {
            "topic": str(name),
            "partitions": len(getattr(topic, "partitions", None) or {}),
            "internal": _internal(str(name), topic),
        }
        for name, topic in sorted(topics.items(), key=lambda item: str(item[0]))
    ]
    return _capped(rows, "topics")


def _internal(name: str, topic: object) -> bool:
    flag = getattr(topic, "is_internal", None)
    if isinstance(flag, bool):
        return flag
    # Cluster metadata does not carry the flag; Kafka's own internal topics
    # are these two.
    return name in ("__consumer_offsets", "__transaction_state")


def _metadata(call: _Call) -> Any:
    # Never with a topic: a metadata request naming one can auto-create it.
    try:
        return call.client.list_topics(timeout=call.remaining())
    except (TransportError, _Missing, _Refusal):
        raise
    except Exception as error:
        raise _classify(error, None) from None


def _describe_topics(call: _Call, name: str) -> Any:
    from confluent_kafka import TopicCollection

    futures = call.client.describe_topics(TopicCollection([name]), **call.kwargs())
    (future,) = futures.values()
    return call.result(future, missing="topic")


def _describe_topic(call: _Call, params: Mapping[str, object]) -> _Answer:
    name = str(params["topic_name"])
    description = _describe_topics(call, name)
    partitions = []
    for info in sorted(
        getattr(description, "partitions", None) or [], key=lambda item: int(item.id)
    ):
        leader = _node_id(getattr(info, "leader", None))
        replicas = [_node_id(node) for node in getattr(info, "replicas", None) or []]
        isr = [_node_id(node) for node in getattr(info, "isr", None) or []]
        partitions.append(
            {
                "partition": int(info.id),
                "leader": leader,
                "replicas": replicas,
                "isr": isr,
                "under_replicated": len(isr) < len(replicas),
                "offline": leader is None or leader < 0,
            }
        )
    notes: list[str] = []
    incomplete = len(partitions) > MAX_PARTITION_ROWS
    if incomplete:
        notes.append(
            f"{len(partitions)} partitions; the first {MAX_PARTITION_ROWS} are shown."
        )
    row: dict[str, Any] = {
        "topic": name,
        "topic_id": str(getattr(description, "topic_id", "") or "") or None,
        "internal": bool(getattr(description, "is_internal", False)),
        "partitions": partitions[:MAX_PARTITION_ROWS],
    }
    if params.get("fetch_configuration") is True:
        row["configs"] = _configs(call, "topic", name)
    return _Answer(
        rows=[row], unit="topics", returned=1, incomplete=incomplete, notes=notes
    )


def _configs(call: _Call, kind: str, name: str) -> list[dict[str, Any]]:
    from confluent_kafka.admin import (  # type: ignore[attr-defined]
        ConfigResource,
        ResourceType,
    )

    restype = ResourceType.TOPIC if kind == "topic" else ResourceType.BROKER
    futures = call.client.describe_configs(
        [ConfigResource(restype, name)], **call.kwargs()
    )
    (future,) = futures.values()
    missing = "topic" if kind == "topic" else "BROKER_NOT_FOUND"
    entries = call.result(future, missing=missing) or {}
    rows = []
    for key in sorted(entries, key=str):
        entry = entries[key]
        sensitive = bool(getattr(entry, "is_sensitive", False))
        value = getattr(entry, "value", None)
        rows.append(
            {
                "name": str(getattr(entry, "name", key)),
                "value": None if sensitive or value is None else str(value),
                "source": _enum_name(getattr(entry, "source", None)),
                "is_default": bool(getattr(entry, "is_default", False)),
                "is_read_only": bool(getattr(entry, "is_read_only", False)),
                "is_sensitive": sensitive,
            }
        )
    return rows


def _find_groups(call: _Call, params: Mapping[str, object]) -> _Answer:
    topic = str(params["topic_name"])
    # Confirm the topic first (C3-3): a missing one scans no group.
    description = _describe_topics(call, topic)
    partitions = sorted(
        int(info.id) for info in getattr(description, "partitions", None) or []
    )
    listed = call.result(call.client.list_consumer_groups(**call.kwargs()))
    names = sorted(
        {str(item.group_id) for item in getattr(listed, "valid", None) or []}
    )
    listing_notes, listing_incomplete = _listing_errors(listed, bool(names))
    scanned = names[:MAX_SCANNED_GROUPS]
    rows: list[dict[str, Any]] = []
    ends: dict[tuple[str, int], int | None] | None = None
    if scanned:
        futures = call.client.describe_consumer_groups(scanned, **call.kwargs())
        descriptions: dict[str, Any] = {}
        for group_id, future in futures.items():
            try:
                descriptions[str(group_id)] = call.result(future)
            except _Missing:
                continue
        for group_id in scanned:
            description = descriptions.get(group_id)
            if description is None:
                continue
            if _enum_name(getattr(description, "state", None)) == "DEAD":
                continue
            members = list(getattr(description, "members", None) or [])
            assigned = sorted(
                {
                    key
                    for member in members
                    for key in _assignment(member)
                    if key[0] == topic
                }
            )
            committed = _committed(call, group_id, topic)
            if not assigned and not committed:
                continue
            if ends is None:
                ends = _bounds(call, [(topic, p) for p in partitions], "latest")
            total = 0
            for key in sorted(set(assigned) | set(committed)):
                done = committed.get(key)
                end = ends.get(key)
                if done is not None and end is not None:
                    total += max(0, end - done)
            rows.append(
                {
                    "group_id": group_id,
                    "state": _enum_name(getattr(description, "state", None)),
                    "member_count": len(members),
                    "assigned_partitions": len(assigned),
                    "committed_partitions": len(committed),
                    "total_lag": total,
                }
            )
    incomplete = len(names) > len(scanned) or listing_incomplete
    notes: list[str] = list(listing_notes)
    if len(names) > len(scanned):
        notes.append(
            f"Scanned {len(scanned)} of {len(names)} consumer groups (the first "
            f"{MAX_SCANNED_GROUPS} by name); groups after them were not checked."
        )
    return _Answer(
        rows=rows,
        unit="groups",
        returned=len(rows),
        incomplete=incomplete,
        notes=notes,
        extra={"groups_scanned": len(scanned), "groups_total": len(names)},
    )


def _committed(call: _Call, group_id: str, topic: str) -> dict[tuple[str, int], int]:
    from confluent_kafka import ConsumerGroupTopicPartitions

    futures = call.client.list_consumer_group_offsets(
        [ConsumerGroupTopicPartitions(group_id)], **call.kwargs()
    )
    out: dict[tuple[str, int], int] = {}
    for future in futures.values():
        try:
            answer = call.result(future)
        except _Missing:
            continue
        for tp in getattr(answer, "topic_partitions", None) or []:
            offset = _offset(tp.offset)
            if str(tp.topic) == topic and offset is not None:
                out[(topic, int(tp.partition))] = offset
    return out


def _cluster(call: _Call) -> Any:
    return call.result(call.client.describe_cluster(**call.kwargs()))


def _list_brokers(call: _Call, params: Mapping[str, object]) -> _Answer:
    cluster = _cluster(call)
    controller = _node_id(getattr(cluster, "controller", None))
    rows = [
        {
            "id": _node_id(node),
            "host": getattr(node, "host", None),
            "port": getattr(node, "port", None),
            "rack": getattr(node, "rack", None),
            "is_controller": _node_id(node) == controller,
        }
        for node in sorted(
            getattr(cluster, "nodes", None) or [], key=lambda n: _node_id(n) or 0
        )
    ]
    answer = _capped(rows, "brokers")
    answer.extra = {"cluster_id": getattr(cluster, "cluster_id", None)}
    return answer


def _describe_configs(call: _Call, params: Mapping[str, object]) -> _Answer:
    kind = str(params["resource_type"])
    rows = _configs(call, kind, str(params["resource_name"]))
    return _capped(rows, "configs")


def _cluster_overview(call: _Call, params: Mapping[str, object]) -> _Answer:
    cluster = _cluster(call)
    metadata = _metadata(call)
    live = {_node_id(node) for node in getattr(cluster, "nodes", None) or []}
    partitions = under = offline = 0
    offline_brokers: set[int] = set()
    topics = getattr(metadata, "topics", None) or {}
    for topic in topics.values():
        for info in (getattr(topic, "partitions", None) or {}).values():
            partitions += 1
            replicas = list(getattr(info, "replicas", None) or [])
            isrs = list(getattr(info, "isrs", None) or [])
            if len(isrs) < len(replicas):
                under += 1
            if getattr(info, "leader", -1) == -1:
                offline += 1
            offline_brokers.update(r for r in replicas if r not in live)
    row = {
        "cluster_id": getattr(cluster, "cluster_id", None),
        "controller_id": _node_id(getattr(cluster, "controller", None)),
        "broker_count": len(live),
        "topic_count": len(topics),
        "partition_count": partitions,
        "under_replicated_partitions": under,
        "offline_partitions": offline,
        "offline_broker_ids": sorted(offline_brokers),
    }
    return _Answer(rows=[row], unit="summary", returned=1)


_HANDLERS: Mapping[str, Callable[[_Call, Mapping[str, object]], _Answer]] = {
    CONSUMERS_TOOL: _list_consumers,
    GROUP_TOOL: _describe_consumer_group,
    TOPICS_TOOL: _list_topics,
    TOPIC_TOOL: _describe_topic,
    FIND_TOOL: _find_groups,
    BROKERS_TOOL: _list_brokers,
    CONFIGS_TOOL: _describe_configs,
    OVERVIEW_TOOL: _cluster_overview,
}

_ROW_UNITS: Mapping[str, str] = {
    CONSUMERS_TOOL: "groups",
    GROUP_TOOL: "groups",
    TOPICS_TOOL: "topics",
    TOPIC_TOOL: "topics",
    FIND_TOOL: "groups",
    BROKERS_TOOL: "brokers",
    CONFIGS_TOOL: "configs",
    OVERVIEW_TOOL: "summary",
}

_VERBS: Mapping[str, str] = {
    CONSUMERS_TOOL: "list",
    GROUP_TOOL: "get",
    TOPICS_TOOL: "list",
    TOPIC_TOOL: "get",
    FIND_TOOL: "get",
    BROKERS_TOOL: "list",
    CONFIGS_TOOL: "get",
    OVERVIEW_TOOL: "get",
}


def overflow_summary(
    tool: str, rows: Sequence[object], query: Mapping[str, object]
) -> dict[str, object]:
    """What an over-limit refusal says about its rows: the row unit."""
    del rows, query
    return {"row_unit": _ROW_UNITS[tool]}


# -- model-visible face (C3-11: tool descriptions only) --------------------------------

_STATE = (
    "Current cluster state read at query time (observed_at), not the state "
    "during the incident window. Never message content."
)
_LAG = (
    "Lag is in messages: the partition's end offset (high watermark) minus "
    "the group's committed offset, read in two requests that are not one "
    "snapshot."
)

_CANNOT_PROVE_BASE = (
    "This is the cluster's state at query time, not during the incident "
    "window. It never shows message content, so it cannot say what a message "
    "contained or why consumers fell behind."
)
_CANNOT_PROVE_LAG = (
    _CANNOT_PROVE_BASE
    + " Lag is measured now, not during the incident window; committed and "
    "end offsets are read in separate requests, not one instant."
)
_CANNOT_PROVE: Mapping[str, str] = {
    name: (_CANNOT_PROVE_LAG if name in (GROUP_TOOL, FIND_TOOL) else _CANNOT_PROVE_BASE)
    for name in TOOL_NAMES
}

_SCHEMA_TEXT: Mapping[str, str] = {
    CONSUMERS_TOOL: "Lists all Kafka consumer groups in the cluster: group_id, "
    "state, type, is_simple; sorted by group_id, at most 800.",
    GROUP_TOOL: "Describes one Kafka consumer group: state, coordinator, "
    "members (client_id, host, assignment) and, with include_offsets (default "
    "true), per-partition committed offset, end offset, log start offset, lag "
    "and flags (no_committed_offset, committed_ahead_of_end, "
    "committed_below_log_start, end_offset_unavailable), plus lag_by_topic. " + _LAG,
    TOPICS_TOOL: "Lists all Kafka topics in the cluster with their partition "
    "count and whether they are internal; sorted by name, at most 1000.",
    TOPIC_TOOL: "Describes one Kafka topic: per partition the leader, replicas, "
    "in-sync replicas, under_replicated and offline; with fetch_configuration "
    "also the topic configuration (sensitive values removed). A topic that "
    "does not exist is reported, never created.",
    FIND_TOOL: "Finds the consumer groups consuming from one Kafka topic "
    "(members assigned to it or offsets committed on it), each with its total "
    "lag on this topic; scans at most 200 groups by name. " + _LAG,
    BROKERS_TOOL: "Lists the Kafka brokers in the cluster: id, host, port, "
    "rack and which one is the controller.",
    CONFIGS_TOOL: "Describes the configuration of one Kafka topic or broker: "
    "name, value, source, is_default, is_read_only, is_sensitive (a sensitive "
    "value is never returned).",
    OVERVIEW_TOOL: "A health summary of the Kafka cluster: broker, topic and "
    "partition counts, under-replicated and offline partitions, offline "
    "broker IDs and the controller.",
}

_PARAMETERS: Mapping[str, Mapping[str, ParameterSpec]] = {
    CONSUMERS_TOOL: {},
    GROUP_TOOL: {
        "group_id": ParameterSpec(
            "string", required=True, description="The ID of the consumer group."
        ),
        "include_offsets": ParameterSpec(
            "boolean",
            description="Include committed offsets and lag per partition "
            "(default true).",
        ),
    },
    TOPICS_TOOL: {},
    TOPIC_TOOL: {
        "topic_name": ParameterSpec(
            "string", required=True, description="The name of the topic."
        ),
        "fetch_configuration": ParameterSpec(
            "boolean", description="Also fetch the topic configuration (default false)."
        ),
    },
    FIND_TOOL: {
        "topic_name": ParameterSpec(
            "string", required=True, description="The name of the topic."
        ),
    },
    BROKERS_TOOL: {},
    CONFIGS_TOOL: {
        "resource_type": ParameterSpec(
            "string", required=True, description="topic or broker."
        ),
        "resource_name": ParameterSpec(
            "string",
            required=True,
            description="The topic name, or the broker's numeric ID.",
        ),
    },
    OVERVIEW_TOOL: {},
}


def _schema(name: str) -> Mapping[str, Any]:
    properties: dict[str, Any] = {}
    for key, spec in _PARAMETERS[name].items():
        prop: dict[str, Any] = {"type": spec.kind, "description": spec.description}
        if name == CONFIGS_TOOL and key == "resource_type":
            prop["enum"] = ["topic", "broker"]
        properties[key] = prop
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": _SCHEMA_TEXT[name] + " " + _STATE,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": [k for k, s in _PARAMETERS[name].items() if s.required],
            },
        },
    }


TOOL_SCHEMAS: tuple[Mapping[str, Any], ...] = tuple(_schema(n) for n in TOOL_NAMES)

_RETURNS: Mapping[str, str] = {
    CONSUMERS_TOOL: "Rows under results, one per consumer group: group_id, "
    "state, type, is_simple; sorted by group_id. The view adds notes and "
    "cannot_prove.",
    GROUP_TOOL: "One row under results: group_id, state, type, coordinator "
    "(broker ID), partition_assignor, members (member_id, client_id, host, "
    "group_instance_id, assignment), partitions (topic, partition, "
    "committed_offset, end_offset, log_start_offset, lag, flags) and "
    "lag_by_topic. Lag is end_offset - committed_offset in messages; null "
    "without a committed offset (flag no_committed_offset); 0 when the commit "
    "is past the end (committed_ahead_of_end); committed_below_log_start when "
    "retention already deleted messages the group had not read; "
    "end_offset_unavailable (lag null) when the partition's offsets could not "
    "be read, e.g. it has no leader.",
    TOPICS_TOOL: "Rows under results, one per topic: topic, partitions (count), "
    "internal; sorted by name.",
    TOPIC_TOOL: "One row under results: topic, topic_id, internal, partitions "
    "(partition, leader, replicas, isr as broker IDs, under_replicated, "
    "offline) and, with fetch_configuration, configs.",
    FIND_TOOL: "Rows under results, one per group consuming the topic: "
    "group_id, state, member_count, assigned_partitions, committed_partitions "
    "(counts on this topic), total_lag (messages on this topic). The view "
    "adds groups_scanned and groups_total.",
    BROKERS_TOOL: "Rows under results, one per broker: id, host, port, rack, "
    "is_controller; sorted by id. The view adds cluster_id.",
    CONFIGS_TOOL: "Rows under results, one per configuration entry: name, "
    "value (null when sensitive), source, is_default, is_read_only, "
    "is_sensitive; sorted by name.",
    OVERVIEW_TOOL: "One row under results: cluster_id, controller_id, "
    "broker_count, topic_count, partition_count, under_replicated_partitions "
    "(fewer in-sync replicas than replicas), offline_partitions (no leader), "
    "offline_broker_ids (replica brokers that are not live).",
}

_WINDOW_FORMAT = (
    "The authorized absolute window is appended as {window}. Kafka state is "
    "read at query time whatever the window says; there is no source time."
)
_VALUES_FORMAT = (
    "The authorized target for this Run is appended as {values}; the cluster "
    "is the operator's, never a parameter. A topic name is 1 to 249 of A-Z "
    "a-z 0-9 . _ - (INVALID_TOPIC), a group ID 1 to 255 printable characters "
    "(INVALID_GROUP), a broker resource its numeric ID (INVALID_RESOURCE); "
    "refused before anything is sent."
)
_LIMITS = (
    "At most 1000 topics, 800 groups and 500 partition rows, sorted, the "
    "rest marked incomplete; find_consumer_groups_by_topic scans at most 200 "
    "groups. String values cut to 2000 characters. At most 2 MiB and 25,000 "
    "tokens, refused whole above that (RESULT_TOO_LARGE). 30 s per call; "
    "no retry, except one re-run when the client retires a broker connection "
    "(_DESTROY_BROKER). A missing topic or group is no_data (TOPIC_NOT_FOUND, "
    "GROUP_NOT_FOUND); an authorization failure SOURCE_FORBIDDEN; an "
    "unreachable cluster SOURCE_UNAVAILABLE."
)

_ERROR_CLASSES: Mapping[str, str] = {
    "INVALID_TOPIC": "INVALID_PARAMS",
    "INVALID_GROUP": "INVALID_PARAMS",
    "INVALID_RESOURCE": "INVALID_PARAMS",
    INVALID_CODE: "INVALID_PARAMS",
    FORBIDDEN_CODE: "SOURCE_FORBIDDEN",
    ERROR_CODE: "SOURCE_ERROR",
}


def kafka_registrations(*, max_window_seconds: int) -> tuple[ToolRegistration, ...]:
    """The eight registrations (``source`` is the lab target's;
    ``may_contain_secrets=True``, C3-8)."""
    return tuple(
        ToolRegistration(
            name=name,
            version="v1",
            source=otel_demo.SOURCE,
            verb=_VERBS[name],
            may_contain_secrets=True,
            result_path=("results",),
            request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            max_result_bytes=MAX_RESULT_BYTES,
            max_view_tokens=MAX_VIEW_TOKENS,
            max_window_seconds=max_window_seconds,
            error_classes=dict(_ERROR_CLASSES),
            description=ToolDescription(
                returns=_RETURNS[name],
                window_format=_WINDOW_FORMAT,
                values_format=_VALUES_FORMAT,
                limits=_LIMITS,
                cannot_prove=_CANNOT_PROVE[name],
            ),
            parameters=dict(_PARAMETERS[name]),
        )
        for name in TOOL_NAMES
    )
