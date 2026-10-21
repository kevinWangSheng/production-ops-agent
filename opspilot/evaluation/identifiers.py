"""Calibration metadata identifier rules shared by the judge and calibration flow.

One definition so the judge-output boundary and the calibration metadata policy
cannot diverge (judge-adapters-6). No imports from evaluation modules: both
``judge`` and ``calibration_flow`` import this without a cycle.
"""

from __future__ import annotations

import re
from typing import Any

IDENTIFIER = re.compile(r"[A-Za-z0-9._:/@-]{1,256}")
# PR-A freezes HTTP routes, templated routes and names such as "orders publish".
OPERATION = re.compile(r"[A-Za-z0-9._:/@(){}\[\]-]+(?: [A-Za-z0-9._:/@(){}\[\]-]+)*")
MAX_OPERATION_LENGTH = 256


def is_identifier(value: Any) -> bool:
    return type(value) is str and IDENTIFIER.fullmatch(value) is not None


def is_operation_name(value: Any) -> bool:
    return (
        type(value) is str
        and len(value) <= MAX_OPERATION_LENGTH
        and OPERATION.fullmatch(value) is not None
    )
