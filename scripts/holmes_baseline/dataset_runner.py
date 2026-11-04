"""The HolmesGPT side of a paired window (S7c, #474 ``eval_live --pair``).

``paired_runner(deps).run(request)`` adapts ``dataset.run_attempt`` to the
paired schedule's ``BaselineRequest -> BaselineResult`` and never raises:

* Holmes gets its own attempt ID, derived from the paired window
  (``holmes_attempt_id``), never the OpsPilot attempt ID of the request.
* Its raw directory is ``<request.output_dir>/<holmes attempt id>``, the
  directory name being that ID (what ``dataset.summarize`` reads);
  ``output_path`` on the result names it.
* ``submit_not_after`` is only a "must start before" check (``SUBMIT_LATE``
  when setup ends after it); the Holmes wall clock stays the frozen 7200 s
  (R15).
* ``provider_error``: the Run failed on a model provider 5xx/transport
  failure that was not the injected 503 (``dataset.provider_error``).

The result type is #474's ``scripts.eval_live.BaselineResult`` when that is
importable, else the field-identical ``HolmesBaselineResult`` below.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

HOLMES_PREFIX = "holmes-"


@dataclass(frozen=True)
class HolmesBaselineResult:
    status: str
    reason: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    product_run_created: bool = False
    provider_error: bool = False
    output_sha256: str | None = None
    output_path: str | None = None


def holmes_attempt_id(round_id: str, opspilot_attempt_id: str) -> str:
    """Deterministic from the paired window (round + OpsPilot attempt), never
    equal to the OpsPilot ID; safe as a directory name."""
    digest = hashlib.sha256(f"{round_id}\0{opspilot_attempt_id}".encode()).hexdigest()
    return f"{HOLMES_PREFIX}{digest[:24]}"


def _result_type() -> Callable[..., Any]:
    try:
        from scripts.eval_live import BaselineResult  # type: ignore[attr-defined]
    except ImportError:
        return HolmesBaselineResult
    return BaselineResult  # type: ignore[no-any-return]


def _moment(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class PairedHolmes:
    def __init__(
        self,
        run_attempt: Callable[..., Mapping[str, Any]],
        *,
        kind_node: str | None = None,
        proxy_log: Callable[[float], list[str]] | None = None,
        result_type: Callable[..., Any] | None = None,
    ) -> None:
        self.run_attempt = run_attempt
        self.kind_node = kind_node
        self.proxy_log = proxy_log
        self.result_type = result_type or _result_type()

    def _result(self, **fields: Any) -> Any:
        names = {f.name for f in dataclasses.fields(self.result_type)}  # type: ignore[arg-type]
        return self.result_type(**{k: v for k, v in fields.items() if k in names})

    def run(self, request: Any) -> Any:
        attempt_id = holmes_attempt_id(str(request.round_id), str(request.attempt_id))
        directory = Path(request.output_dir) / attempt_id
        not_after = request.submit_not_after
        try:
            out = self.run_attempt(
                request.spec,
                request.answer,
                attempt_id=attempt_id,
                slot=None,
                raw_root=Path(request.output_dir),
                round_id=request.round_id,
                kind_node=self.kind_node,
                proxy_log=self.proxy_log,
                submit_not_after=None if not_after is None else not_after.timestamp(),
            )
        except Exception as exc:  # noqa: BLE001 - never raise into the schedule
            return self._result(
                status="aborted",
                reason=f"BASELINE_ERROR:{type(exc).__name__}",
                output_path=str(directory),
            )
        status = str(out.get("status"))
        reason = out.get("reason")
        if status not in ("ran", "setup_failed", "aborted"):
            status, reason = "aborted", f"BASELINE_STATUS:{status}"
        return self._result(
            status=status,
            reason=None if reason is None else str(reason),
            started_at=_moment(out.get("started_at")),
            ended_at=_moment(out.get("ended_at")),
            # setup_failed means no Holmes container started
            product_run_created=status != "setup_failed",
            provider_error=bool(out.get("provider_error")),
            output_sha256=out.get("holmes_json_sha256"),
            output_path=str(out.get("attempt_dir") or directory),
        )


def paired_runner(deps: Any = None) -> PairedHolmes:
    """The runner ``eval_live --pair`` calls. ``deps`` may carry
    ``holmes_kind_node`` and ``holmes_proxy_log`` for another lab instance;
    otherwise the ``kind_lab`` instance of this process applies."""
    from scripts.holmes_baseline.dataset import run_attempt

    return PairedHolmes(
        run_attempt,
        kind_node=getattr(deps, "holmes_kind_node", None),
        proxy_log=getattr(deps, "holmes_proxy_log", None),
    )
