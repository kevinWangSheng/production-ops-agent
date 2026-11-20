"""Deterministic report-to-text conversion for the judge (#666 H-9, H-12, H-13).

* The report text is :func:`opspilot.evaluation.binary_judge.project_opspilot_report`.
* ``include_tool_calls`` questions get upstream's tool-call block appended
  (``tests/llm/utils/property_manager.py:212-231`` at a0009195), one entry per
  call of the Run in call order, refused calls included, nothing truncated.
* A Run without a usable report is not sent to the judge: ``text`` is None and
  ``reason`` is ``REPORT_REJECTED_BY_PRODUCT`` (H-12, #645).
* When the evaluation shifted a fixed-time question's data, ISO 8601 date-times
  and ``YYYY-MM-DD`` dates that fall inside the shifted data range ± 1 hour are
  shifted back (H-9). Times outside that range are left alone; a time of day
  written without a date is not touched (known limitation, disclosed).
* English month-name dates (``August 2, 2025``, ``Nov 19 2026 at 13:45 UTC``;
  #666 PR-C, the C5 Loki question texts use this form) are read the same way
  and rewritten in their own style.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

from opspilot.evaluation.binary_judge import project_opspilot_report
from opspilot.investigation.reports import ReportV2

REPORT_REJECTED_BY_PRODUCT = "REPORT_REJECTED_BY_PRODUCT"
#: H-9: the deshift range is the shifted data range widened by this much.
DESHIFT_MARGIN = timedelta(hours=1)

# --------------------------------------------------------------------------
# Tool-call block


@dataclass(frozen=True)
class ToolCall:
    """One tool call of the Run, as the evidence store keeps it.

    ``output`` is the stored (redacted) view the model saw; a refused call
    has ``refused_code`` (and usually ``refused_reason``) instead.
    """

    tool: str
    params: Mapping[str, Any]
    output: Any | None = None
    refused_code: str | None = None
    refused_reason: str | None = None


def _canonical(value: object) -> str:
    # Same canonical form as the ITBench conversion (answer.py ``_canonical``).
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def describe(call: ToolCall) -> str:
    """Upstream's ``tc.description``: ``<tool>(<canonical JSON params>)``."""
    return f"{call.tool}({_canonical(dict(call.params))})"


def output_text(call: ToolCall) -> str | None:
    """The call's ``Output:`` body; None when there is nothing to show."""
    if call.refused_code is not None:
        reason = f": {call.refused_reason}" if call.refused_reason else ""
        return f"REFUSED {call.refused_code}{reason}"
    if call.output is None:
        return None
    if isinstance(call.output, str):
        return call.output
    return json.dumps(
        call.output, ensure_ascii=False, sort_keys=True, indent=2, default=str
    )


def format_tool_calls(calls: Sequence[ToolCall]) -> str:
    """Upstream's ``# Tool Calls`` block, verbatim layout; empty for no calls
    (upstream appends nothing when ``result.tool_calls`` is empty)."""
    if not calls:
        return ""
    text = "\n\n# Tool Calls\n\n"
    for i, call in enumerate(calls, 1):
        text += f"* Tool #{i}: {describe(call)}\n"
        body = output_text(call)
        if body:
            # Not truncated: upstream gives the judge the complete output.
            indented = "\n".join(f"  {line}" for line in body.split("\n"))
            text += f"Output:\n{indented}\n"
        text += "---\n"
    return text


# --------------------------------------------------------------------------
# Deshift


@dataclass(frozen=True)
class Shift:
    """The whole-second offset added to a question's data, and the shifted
    data's earliest and latest time (naive values are read as UTC)."""

    seconds: int
    shifted_min: datetime
    shifted_max: datetime


# A date, optionally followed by a time (T or space), optional seconds and
# fraction, optional zone. A numeric offset is read only after seconds, so
# "10:00-11:00" is two times of day, not a time with offset -11:00. Not
# preceded by a digit, ``.`` or ``:`` and not followed by a digit, so parts of
# longer numbers are not read as dates. Dotted dates (index names such as
# ``app-185-…-2024.01.05``) never match.
_STAMP = re.compile(
    r"(?<![\d.:])"
    r"(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})"
    r"(?:(?P<sep>[Tt ])(?P<h>\d{2}):(?P<mi>\d{2})"
    r"(?::(?P<s>\d{2})(?P<frac>[.,]\d{1,9})?)?"
    r"(?P<zone>[Zz]|(?(s)[+-](?:0\d|1[0-4])(?::?[0-5]\d)?(?![:\d-])|(?!)))?(?!\d))?"
    r"(?!\d)"
)


_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)  # fmt: skip
_MONTH_NUMBER = (
    {m: i + 1 for i, m in enumerate(_MONTHS)}
    | {m[:3]: i + 1 for i, m in enumerate(_MONTHS)}
    | {"sept": 9}
)
# ``<Month|Mon>[.] <D>[st|nd|rd|th][,] <YYYY>``, optionally followed by a time
# of day (``at``/``around``/``,``/space, ``H:MM[:SS]``, optional ``UTC``/``GMT``
# /``Z``). A time without a zone is read as UTC (the question texts say UTC).
_MONTH_DATE = re.compile(
    r"(?<![A-Za-z])"
    r"(?P<mon>(?i:january|february|march|april|may|june|july|august|september"
    r"|october|november|december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec))"
    r"(?P<dot>\.)?(?P<sp1>\s)(?P<d>\d{1,2})(?P<ord>st|nd|rd|th)?(?P<comma>,)?"
    r"(?P<sp2>\s)(?P<y>\d{4})(?![\d])"
    r"(?:(?P<conn>,?\s+(?:at\s+|around\s+)?|,\s*)"
    r"(?P<h>\d{1,2}):(?P<mi>[0-5]\d)(?::(?P<s>[0-5]\d))?"
    r"(?P<zone>\s?(?:UTC|GMT)|Z)?(?![\d:]))?"
)


def _ordinal(day: int) -> str:
    if 10 <= day % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


def _month_name(original: str, month: int) -> str:
    full = _MONTHS[month - 1]
    if (len(original) > 3 or original.lower() == "may") and original.lower() != "sept":
        name = full
    elif original.lower() == "sept":
        name = "sept" if month == 9 else full[:3]
    else:
        name = full[:3]
    if original.isupper():
        return name.upper()
    if original[0].isupper():
        return name.capitalize()
    return name


def month_date_parts(m: re.Match[str]) -> tuple[date, datetime | None]:
    """The date of a :data:`_MONTH_DATE` match and, when a time follows, the
    UTC instant. Raises ValueError for an impossible date or time."""
    if not m["mon"][0].isupper():
        raise ValueError("month name not capitalised")  # "it may 3, 2026"
    month = _MONTH_NUMBER[m["mon"].lower()]
    day = date(int(m["y"]), month, int(m["d"]))
    if m["h"] is None:
        return day, None
    moment = datetime(
        day.year, day.month, day.day, int(m["h"]), int(m["mi"]), int(m["s"] or 0),
        tzinfo=UTC,
    )  # fmt: skip
    return day, moment


def format_month_date(m: re.Match[str], day: date, moment: datetime | None) -> str:
    """``day`` (and ``moment``'s time of day) in the match's own style."""
    out = _month_name(m["mon"], day.month) + (m["dot"] or "") + m["sp1"]
    out += str(day.day) + (_ordinal(day.day) if m["ord"] else "")
    out += (m["comma"] or "") + m["sp2"] + f"{day.year:04d}"
    if m["h"] is None or moment is None:
        return out
    hour = f"{moment.hour:02d}" if len(m["h"]) == 2 else str(moment.hour)
    out += m["conn"] + f"{hour}:{moment.minute:02d}"
    if m["s"] is not None:
        out += f":{moment.second:02d}"
    return out + (m["zone"] or "")


def _deshift_month(
    m: re.Match[str], shift: Shift, low: datetime, high: datetime
) -> str | None:
    try:
        day, moment = month_date_parts(m)
    except ValueError:
        return None
    if moment is None:
        original = _deshift_date(day, shift, low, high)
        return None if original is None else format_month_date(m, original, None)
    if not low <= moment <= high:
        return None
    step = (shift.seconds // 60) * 60 if m["s"] is None else shift.seconds
    back = moment - timedelta(seconds=step)
    return format_month_date(m, back.date(), back)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _zone(text: str | None) -> timezone | None:
    if not text:
        return None
    if text in {"Z", "z"}:
        return UTC
    sign = 1 if text[0] == "+" else -1
    digits = text[1:].replace(":", "")
    hours = int(digits[:2])
    minutes = int(digits[2:4]) if len(digits) > 2 else 0
    if hours > 23 or minutes > 59:
        raise ValueError("bad zone")
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def _deshift_date(
    day: date, shift: Shift, low: datetime, high: datetime
) -> date | None:
    """The original date of a shifted ``YYYY-MM-DD``, or None out of range.

    The part of that day inside the range is taken; its midpoint, shifted
    back, gives the original date. A sub-day remainder of the offset thus
    follows the data instead of a fixed day count.
    """
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    end = start + timedelta(days=1)
    lo, hi = max(start, low), min(end, high)
    if lo > hi or (lo == hi and lo == end):
        return None
    middle = lo + (hi - lo) / 2
    return (middle - timedelta(seconds=shift.seconds)).date()


def _deshift_stamp(
    m: re.Match[str], shift: Shift, low: datetime, high: datetime
) -> str | None:
    """The match shifted back in its own format, or None when out of range or
    not a valid time."""
    try:
        day = date(int(m["y"]), int(m["mo"]), int(m["d"]))
        if m["h"] is None:
            original = _deshift_date(day, shift, low, high)
            return None if original is None else original.isoformat()
        zone = _zone(m["zone"])
        second = int(m["s"]) if m["s"] is not None else 0
        local = datetime(
            day.year,
            day.month,
            day.day,
            int(m["h"]),
            int(m["mi"]),
            second,
            tzinfo=zone or UTC,
        )
    except ValueError:
        return None
    # Fractions are kept as written: the offset is whole seconds, and the
    # range check at second precision is unaffected by them.
    if not low <= _utc(local) <= high:
        return None
    if m["s"] is None:
        # Minute precision: a writer that cut seconds off the shifted time is
        # undone exactly by the offset's whole minutes.
        back = local - timedelta(seconds=(shift.seconds // 60) * 60)
    else:
        back = local - timedelta(seconds=shift.seconds)
    out = back.strftime("%Y-%m-%d") + m["sep"] + back.strftime("%H:%M")
    if m["s"] is not None:
        out += back.strftime(":%S") + (m["frac"] or "")
    return out + (m["zone"] or "")


def deshift(text: str, shift: Shift | None) -> tuple[str, int]:
    """Undo the evaluation's offset on dates and times in the data range.

    Returns the new text and how many dates or date-times were rewritten.
    """
    if shift is None:
        return text, 0
    low = _utc(shift.shifted_min) - DESHIFT_MARGIN
    high = _utc(shift.shifted_max) + DESHIFT_MARGIN
    count = 0

    def replace(m: re.Match[str]) -> str:
        nonlocal count
        new = _deshift_stamp(m, shift, low, high)
        if new is None:
            return m.group(0)
        count += 1
        return new

    def replace_month(m: re.Match[str]) -> str:
        nonlocal count
        new = _deshift_month(m, shift, low, high)
        if new is None:
            return m.group(0)
        count += 1
        return new

    text = _STAMP.sub(replace, text)
    return _MONTH_DATE.sub(replace_month, text), count


# --------------------------------------------------------------------------
# Report to text


@dataclass(frozen=True)
class ConvertedText:
    text: str | None
    deshift_count: int
    with_tool_calls: bool
    reason: str | None


def report_to_text(
    report: ReportV2 | None,
    calls: Sequence[ToolCall] | None,
    shift: Shift | None,
) -> ConvertedText:
    """What the judge reads for one trial.

    ``report`` is None when the Run has no usable report
    (``run.outcome.report_available`` not true). ``calls`` is None unless the
    question sets ``include_tool_calls``.
    """
    if report is None:
        return ConvertedText(None, 0, False, REPORT_REJECTED_BY_PRODUCT)
    text = project_opspilot_report(report)
    with_calls = bool(calls)
    if calls:
        text += format_tool_calls(calls)
    text, count = deshift(text, shift)
    return ConvertedText(text, count, with_calls, None)
