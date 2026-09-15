"""Shared scalars, fixed error codes and the deterministic transition primitive.

Every persistent type in this package is frozen, strict and rejects unknown
fields, so a credential, a model-supplied attribute or a coerced value cannot
enter a domain object by accident.
"""

from collections.abc import Mapping
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ErrorCode = Literal[
    "INVALID_INPUT",
    "ILLEGAL_TRANSITION",
    "CONTROL_CONFLICT",
    "SUSPENDED",
    "STALE_RESULT",
]
_CODES = frozenset(get_args(ErrorCode))


class DomainError(Exception):
    """Only the fixed codes above may leave the domain boundary."""

    def __init__(self, code: ErrorCode, detail: str = "") -> None:
        if code not in _CODES:
            raise ValueError(f"unknown domain error code {code!r}")
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


Text = Annotated[str, Field(min_length=1)]
Count = Annotated[int, Field(ge=0)]
Positive = Annotated[int, Field(gt=0)]


class DTO(BaseModel):
    #: ``hide_input_in_errors`` keeps the rejected value out of the raised
    #: ``ValidationError``. Forbidding a credential field is not enough on its
    #: own: the default message quotes the input, so a caller that logs the
    #: rejection would export the very secret the field rejected.
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, hide_input_in_errors=True
    )


def sanitized_errors(exc: ValidationError) -> list[dict[str, object]]:
    """Render a validation failure without the value that was rejected.

    ``hide_input_in_errors`` only reaches ``str(exc)``. ``exc.errors()`` and
    ``exc.json()`` still carry the rejected input, and those are the paths a
    structured logger takes, so a refused credential would be exported by the
    very failure that refused it. This is the only sanctioned way to render a
    ``ValidationError`` in product code; ``tests/test_architecture.py`` keeps
    the raw accessors out of ``opspilot/``.
    """

    return [
        {"type": error["type"], "loc": error["loc"], "msg": error["msg"]}
        for error in exc.errors()
    ]


class StateMachine:
    """A ``(state, trigger) -> state`` table.

    Only listed pairs are legal. An unlisted pair is rejected, never inferred,
    and a state with no triggers is terminal.
    """

    def __init__(self, name: str, edges: Mapping[str, Mapping[str, str]]) -> None:
        self.name = name
        self._edges: dict[str, dict[str, str]] = {
            state: dict(triggers) for state, triggers in edges.items()
        }
        for state, triggers in self._edges.items():
            for trigger, target in triggers.items():
                if target not in self._edges:
                    raise ValueError(f"{name}: {state}/{trigger} -> unknown {target!r}")

    @property
    def states(self) -> frozenset[str]:
        return frozenset(self._edges)

    def triggers(self, state: str) -> frozenset[str]:
        return frozenset(self._require(state))

    def terminal(self, state: str) -> bool:
        return not self._require(state)

    def fire(self, state: str, trigger: str) -> str:
        """Return the next state, or raise ``ILLEGAL_TRANSITION``."""
        allowed = self._require(state)
        if not isinstance(trigger, str) or trigger not in allowed:
            raise DomainError(
                "ILLEGAL_TRANSITION", f"{self.name}: {state} -[{trigger}]-> ?"
            )
        return allowed[trigger]

    def _require(self, state: str) -> dict[str, str]:
        if not isinstance(state, str) or state not in self._edges:
            raise DomainError("INVALID_INPUT", f"{self.name}: unknown state {state!r}")
        return self._edges[state]
