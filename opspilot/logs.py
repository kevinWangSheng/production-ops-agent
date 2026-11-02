"""Process logging for the three long-running entry points (F8 structured logs).

``configure()`` is called once at the start of the worker, the Observer and
the web process. It gives the root logger one stderr handler at INFO in the
format ``OPSPILOT_LOG_FORMAT`` selects:

- ``json`` (default; also when unset or empty): one JSON object per line,
  keys in this order: ``ts level logger msg version revision [kv] [exc]
  [stack]``. ``msg`` is the message exactly as the text format renders it;
  ``kv`` holds the call site's ``name=%s`` arguments (see ``_kv``); ``exc``
  and ``stack`` are the full traceback and stack text. Newlines and quotes
  are JSON-escaped, so every record is exactly one line.
- ``text``: the previous ``%(asctime)s %(name)s %(levelname)s %(message)s``.

Any other value refuses to start (``SystemExit``) before any logging state
changes. ``version``/``revision`` are process constants read here once, with
the same validation as ``opspilot_build_info`` on ``/metrics``.

Nothing is redacted (F8L-Q2): what keeps credentials and provider text out of
the logs is the call sites, which log codes, ids and exception types only.
Uncaught exceptions (main thread and other threads) are logged through the
same handler as one ERROR record with the full traceback (Q5); a deliberate
``SystemExit("...")`` refusal never reaches the hook and stays plain text.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, TextIO, cast

from opspilot import __version__, health

__all__ = ["FORMAT_ENV", "JsonFormatter", "configure"]

FORMAT_ENV = "OPSPILOT_LOG_FORMAT"
TEXT_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"
UNCAUGHT_LOGGER = "opspilot.uncaught"
_REFUSAL = f"{FORMAT_ENV} must be json or text"

# One printf conversion: %[(name)][flags][width][.precision][length]type.
_CONVERSION = re.compile(
    r"%(?P<mapping>\([^)]*\))?[#0\- +]*(?P<width>\*|\d+)?"
    r"(?:\.(?P<precision>\*|\d*))?[hlL]?(?P<type>[diouxXeEfFgGcrsa%])"
)
_KEY_BEFORE = re.compile(r"(?:^|(?<=[^A-Za-z0-9_]))([A-Za-z_][A-Za-z0-9_]*)=\Z")


def _kv(msg: object, args: object) -> dict[str, Any] | None:
    """The ``name=%...`` arguments of a printf-style call, by position.

    ``None`` (no ``kv`` key) when the message is not a format string with a
    positional tuple of exactly the arguments it consumes, when it uses
    ``%(name)s`` mapping keys, or when a conversion cannot be parsed. Values
    come from the arguments, never from the rendered text."""
    if not isinstance(msg, str) or not isinstance(args, tuple):
        return None
    found: dict[str, Any] = {}
    consumed = 0
    literal_start = 0
    position = 0
    while True:
        percent = msg.find("%", position)
        if percent < 0:
            break
        match = _CONVERSION.match(msg, percent)
        if match is None:
            return None
        position = match.end()
        if match["type"] == "%":
            if match.end() - percent != 2:
                return None
            continue
        if match["mapping"] is not None:
            return None
        consumed += (match["width"] == "*") + (match["precision"] == "*")
        key = _KEY_BEFORE.search(msg[literal_start:percent])
        if key is not None and consumed < len(args):
            found[key.group(1)] = _value(args[consumed])
        consumed += 1
        literal_start = position
    if consumed != len(args):
        return None
    return found or None


def _value(value: object) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float) and math.isfinite(value):
        return float(value)
    return str(value)


def _timestamp(created: float) -> str:
    moment = datetime.fromtimestamp(created, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


class JsonFormatter(logging.Formatter):
    """One JSON object per record; see the module docstring for the keys."""

    def __init__(self, *, version: str, revision: str) -> None:
        super().__init__()
        self._version = version
        self._revision = revision

    def format(self, record: logging.LogRecord) -> str:
        """Total: whatever the record holds, exactly one line comes out."""
        try:
            return _dumps(self._line(record))
        except Exception as exc:  # noqa: BLE001 - a dropped record is worse
            return _dumps(self._fallback(record, exc))

    def _line(self, record: logging.LogRecord) -> dict[str, Any]:
        kv: dict[str, Any] | None
        try:
            message = record.getMessage()
            kv = _kv(record.msg, record.args)
        except (TypeError, ValueError, KeyError):
            # A call site whose arguments do not fit its format string: keep
            # the line (and the one-line shape) with the raw format string.
            message, kv = str(record.msg), None
        line: dict[str, Any] = {
            "ts": _timestamp(record.created),
            "level": record.levelname,
            "logger": record.name,
            "msg": message,
            "version": self._version,
            "revision": self._revision,
        }
        if kv:
            line["kv"] = kv
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            line["exc"] = record.exc_text
        if record.stack_info:
            line["stack"] = self.formatStack(record.stack_info)
        return line

    def _fallback(self, record: logging.LogRecord, exc: Exception) -> dict[str, Any]:
        """The fixed keys only, from parts that cannot fail, plus which
        exception stopped the full line (``format_error``)."""
        try:
            ts = _timestamp(record.created)
        except Exception:  # noqa: BLE001 - a bad ``created``: the time now
            ts = _timestamp(time.time())
        return {
            "ts": ts,
            "level": _safe_text(getattr(record, "levelname", None)),
            "logger": _safe_text(getattr(record, "name", None)),
            "msg": _safe_repr(getattr(record, "msg", None)),
            "version": self._version,
            "revision": self._revision,
            "format_error": type(exc).__name__,
        }


def _safe_text(value: object) -> str:
    return value if isinstance(value, str) else _safe_repr(value)


def _safe_repr(value: object) -> str:
    try:
        return repr(value)
    except Exception:  # noqa: BLE001
        return f"<unrepresentable {type(value).__name__}>"


def _dumps(line: dict[str, Any]) -> str:
    """Readable UTF-8 when possible; a string holding a lone surrogate
    (which no UTF-8 stream can write) is escaped as ``\\udcXX`` instead,
    still valid JSON and still one line."""
    text = json.dumps(line, ensure_ascii=False, default=str)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(line, ensure_ascii=True, default=str)
    return text


class _Handler(logging.StreamHandler[TextIO]):
    """The handler ``configure()`` owns. With no stream given it writes to
    whatever ``sys.stderr`` is at emit time (like ``logging.lastResort``)."""

    def __init__(self, stream: TextIO | None) -> None:
        super().__init__(stream if stream is not None else sys.stderr)
        self._fixed = stream is not None

    @property
    def stream(self) -> TextIO:
        return self._stream if self._fixed else sys.stderr

    @stream.setter
    def stream(self, value: TextIO) -> None:
        self._stream = value


def _log_uncaught(
    thread: str,
    exc_type: type[BaseException],
    exc: BaseException | None,
    tb: TracebackType | None,
) -> None:
    logging.getLogger(UNCAUGHT_LOGGER).error(
        "uncaught exception thread=%s",
        thread,
        exc_info=cast(Any, (exc_type, exc, tb)),
    )


def _excepthook(
    exc_type: type[BaseException],
    exc: BaseException,
    tb: TracebackType | None,
) -> None:
    try:
        _log_uncaught(threading.current_thread().name, exc_type, exc, tb)
    except Exception:  # noqa: BLE001 - never lose the traceback itself
        sys.__excepthook__(exc_type, exc, tb)


def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
    if issubclass(args.exc_type, SystemExit):
        return  # the stdlib default ignores it silently too
    name = args.thread.name if args.thread is not None else "unknown"
    try:
        _log_uncaught(name, args.exc_type, args.exc_value, args.exc_traceback)
    except Exception:  # noqa: BLE001 - never lose the traceback itself
        threading.__excepthook__(args)


def configure(
    env: Mapping[str, str] | None = None, *, stream: TextIO | None = None
) -> None:
    """Install the process logging; idempotent (replaces only the handler a
    previous call installed). ``SystemExit`` on an unknown format, before
    anything changes."""
    env = os.environ if env is None else env
    chosen = env.get(FORMAT_ENV) or "json"
    if chosen not in ("json", "text"):
        raise SystemExit(_REFUSAL)
    formatter: logging.Formatter
    if chosen == "json":
        formatter = JsonFormatter(version=__version__, revision=health._revision(env))
    else:
        formatter = logging.Formatter(TEXT_FORMAT)
    handler = _Handler(stream)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    for existing in list(root.handlers):
        if isinstance(existing, _Handler):
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
