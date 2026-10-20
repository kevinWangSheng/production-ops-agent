"""Strict JSON/DTO codec for frozen offline evaluation artifacts."""

from __future__ import annotations

import json
import math
import types
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta
from typing import (
    Any,
    Literal,
    NoReturn,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)


class StorageError(ValueError):
    """Only a fixed, credential-free code crosses the storage boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def invalid() -> NoReturn:
    raise StorageError("SCHEMA_INVALID")


def encode(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: encode(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, datetime):
        if value.utcoffset() != timedelta(0):
            return invalid()
        return value.isoformat()
    if isinstance(value, Mapping):
        if not all(type(k) is str for k in value):
            return invalid()
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, frozenset):
        return sorted(encode(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeError:
            pass
        else:
            return value
        return invalid()
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    return invalid()


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            encode(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (ValueError, TypeError, OverflowError, RecursionError):
        pass
    return invalid()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            return invalid()
        result[key] = value
    return result


def parse(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=lambda _: invalid(),
        )
        encode(value)  # Also rejects overflowed finite literals (e.g. 1e999).
        if type(value) is not dict:
            return invalid()
        return cast(dict[str, Any], value)
    except (ValueError, TypeError, OverflowError, RecursionError):
        pass
    return invalid()


def exact(value: Any, keys: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        return invalid()
    return cast(dict[str, Any], value)


def decode(annotation: Any, value: Any) -> Any:
    """Strictly reconstruct the public DTOs; never coerce bool/int/string."""
    if annotation is Any:
        return encode(value)
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, types.UnionType):
        for option in args:
            try:
                return decode(option, value)
            except StorageError:
                continue
        return invalid()
    if origin is Literal:
        if not any(type(value) is type(a) and value == a for a in args):
            return invalid()
        return value
    if annotation is type(None):
        return None if value is None else invalid()
    if annotation in (str, bool, int):
        return value if type(value) is annotation else invalid()
    if annotation is float:
        finite = False
        try:
            finite = type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            pass
        if not finite:
            return invalid()
        return float(value)
    if annotation is datetime:
        if type(value) is not str:
            return invalid()
        try:
            result = datetime.fromisoformat(value)
        except ValueError:
            result = None
        if result is None:
            return invalid()
        if result.utcoffset() != timedelta(0):
            return invalid()
        return result
    if origin in (Mapping, dict):
        if type(value) is not dict:
            return invalid()
        return {decode(args[0], k): decode(args[1], v) for k, v in value.items()}
    if origin in (tuple, list, frozenset, Sequence):
        if type(value) is not list:
            return invalid()
        if origin is tuple and args[-1:] != (Ellipsis,):
            if len(value) != len(args):
                return invalid()
            values = [decode(t, v) for t, v in zip(args, value, strict=True)]
        else:
            values = [decode(args[0], v) for v in value]
        if origin is frozenset:
            if len(set(values)) != len(values):
                return invalid()
            return frozenset(values)
        return tuple(values) if origin is tuple else values
    if isinstance(annotation, type) and is_dataclass(annotation):
        names = {f.name for f in fields(annotation)}
        # Only fields explicitly marked absent_ok may be missing (legacy artifacts);
        # they then take their declared default.
        optional = {f.name for f in fields(annotation) if f.metadata.get("absent_ok")}
        if type(value) is dict and names - optional <= set(value) <= names:
            names = set(value)
        obj = exact(value, names)
        hints = get_type_hints(annotation)
        try:
            return annotation(**{k: decode(hints[k], v) for k, v in obj.items()})
        except (ValueError, TypeError):
            pass
    return invalid()


def validated(annotation: Any, value: Any) -> Any:
    try:
        return decode(annotation, encode(value))
    except (ValueError, TypeError, OverflowError, RecursionError):
        pass
    return invalid()
