"""Explicit, non-executable configuration. Never render private values."""

import os
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

KEYS = frozenset(
    """DEEPSEEK_API_KEY DEEPSEEK_BASE_URL OPSPILOT_MODEL
OPSPILOT_THINKING OPSPILOT_REASONING_EFFORT LANGSMITH_API_KEY LANGSMITH_PROJECT
LANGSMITH_ENDPOINT LANGSMITH_WORKSPACE_ID LANGSMITH_TRACING
OPSPILOT_EXPERIMENT_BUDGET_CNY OPSPILOT_EXPERIMENT_DEADLINE_UTC
OPSPILOT_DATABASE_URL""".split()
)
PROFILE = {
    "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
    "OPSPILOT_MODEL": "deepseek-v4-pro",
    "OPSPILOT_THINKING": "enabled",
    "OPSPILOT_REASONING_EFFORT": "high",
    "LANGSMITH_TRACING": "false",
}


class ConfigError(Exception):
    """Only fixed error codes may leave this boundary."""


@dataclass(repr=False)
class Config:
    values: dict[str, str] = field(repr=False)

    def readiness(self):
        v = self.values
        try:
            budget = Decimal(v.get("OPSPILOT_EXPERIMENT_BUDGET_CNY", "0"))
            funded = budget.is_finite() and budget > 0
        except InvalidOperation:
            funded = False
        try:
            deadline = datetime.fromisoformat(
                v.get("OPSPILOT_EXPERIMENT_DEADLINE_UTC", "")
            )
            timely = deadline.utcoffset() is not None and deadline > datetime.now(
                timezone.utc
            )
        except ValueError:
            timely = False
        return {
            "deepseek_key_present": bool(v.get("DEEPSEEK_API_KEY")),
            "langsmith_key_present": bool(v.get("LANGSMITH_API_KEY")),
            "langsmith_endpoint_present": bool(v.get("LANGSMITH_ENDPOINT")),
            "langsmith_project_present": bool(v.get("LANGSMITH_PROJECT")),
            "workspace_present": bool(v.get("LANGSMITH_WORKSPACE_ID")),
            "positive_budget_configured": funded,
            "future_deadline_configured": timely,
            "authorization_recorded": False,
            "live_execution_enabled": False,
        }


def load_config(path: Path | None, *, environ=None) -> Config:
    env = os.environ if environ is None else environ
    values = {}
    if path is not None:
        # Explicit absolute path; reject symlinks and non-private files before reading.
        if not path.is_absolute():
            raise ConfigError("CONFIG_ABSOLUTE_PATH_REQUIRED")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd) as stream:
                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_mode & 0o077
                    or info.st_uid != os.getuid()
                    or info.st_size > 16384
                ):
                    raise ConfigError("CONFIG_PRIVATE_REGULAR_FILE_REQUIRED")
                raw = stream.read(16385)
        except (OSError, UnicodeError):
            raise ConfigError("CONFIG_UNREADABLE") from None
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not sep or key not in KEYS or key in values:
                raise ConfigError("CONFIG_SYNTAX")
            # Literal single-line values only: no shell syntax or interpolation.
            if any(c in value for c in ("$", "`", "\x00")):
                raise ConfigError("CONFIG_SYNTAX")
            if value[:1] in ('"', "'"):
                if len(value) < 2 or value[-1] != value[0]:
                    raise ConfigError("CONFIG_SYNTAX")
                value = value[1:-1]
            values[key] = value
    for key in KEYS:
        if key in env:
            if key in values and values[key] != env[key]:
                raise ConfigError("CONFIG_ENV_CONFLICT")
            if path is None:
                values[key] = env[key]
    for key, expected in PROFILE.items():
        if values.get(key, expected) != expected:
            raise ConfigError("CONFIG_PROFILE_MISMATCH")
    values = PROFILE | values
    # Reject legacy automatic tracing as well as the current switch.
    if any(
        env.get(k, "false").lower() not in ("false", "0", "")
        for k in ("LANGCHAIN_TRACING", "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING")
    ):
        raise ConfigError("AUTOMATIC_TRACING_FORBIDDEN")
    return Config(values)
