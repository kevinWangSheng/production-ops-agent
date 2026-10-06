"""Experiment evidence after ADR-0006: platform trace + frozen summary.

Shared by the live experiment scripts (``m1_live_runner.py``,
``m1_live_flash_loop.py``). Three responsibilities, nothing else:

* **One LangSmith project per round.** ``configure_lab_round`` turns a round
  name into ``opspilot-lab-<round>`` (the prefix ``opspilot.tracing`` requires
  for its fail-closed check) and ``ensure_longlived_project`` creates the
  project and sets its retention to the longest tier *before* the Run, because
  a retention change applies to new traces only.
* **Frozen summary in the repository.** ``freeze`` writes the raw ledger to an
  ignored location (``OPSPILOT_LEDGER_DIR`` or ``tmp/lab-ledgers/``) and puts
  only ``summary.json`` under ``docs/evidence/``: report, verdicts, counts,
  cost, trace id with link, and the sha256 of the raw ledger bytes. Nothing
  else is written into the evidence directory.
* **Trace read-back.** ``trace_evidence`` resolves the Run's LangSmith root
  run (ingestion is asynchronous, so it polls, bounded) and records the UI
  link; a Run that is not found in time is recorded as such, never invented.

Credentials are read by the ``langsmith`` client from ``LANGSMITH_*`` in the
environment and are never written here. Development script, not product code.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opspilot.tracing import LAB_PROJECT_PREFIX, TRACE_ENV, check_lab_target

ROOT = Path(__file__).resolve().parents[1]
LEDGER_DIR_ENV = "OPSPILOT_LEDGER_DIR"
LAB_ROUND_ENV = "OPSPILOT_LAB_ROUND"
DEFAULT_LEDGER_ROOT = ROOT / "tmp/lab-ledgers"
LONGLIVED = "longlived"
SUMMARY_SCHEMA = "opspilot-lab-summary-1"
# One lowercase label: letters, digits, ``.``/``-``; LangSmith project names
# are free text, this is the project's own bound on the round name.
_ROUND = re.compile(r"^[a-z0-9][a-z0-9.-]{0,62}$")
# Root-run read-back: spans leave the batch queue within ~2s and LangSmith
# ingests them asynchronously; 30 polls at 2s is a generous upper bound.
READ_BACK_ATTEMPTS = 30
READ_BACK_DELAY_S = 2.0


# -- project per round --------------------------------------------------------


def lab_project(round_name: str) -> str:
    """``opspilot-lab-<round>``; the round name is validated, not escaped."""
    if not _ROUND.match(round_name):
        raise ValueError(
            "lab round must match [a-z0-9][a-z0-9.-]{0,62} (got %r)" % (round_name,)
        )
    return f"{LAB_PROJECT_PREFIX}{round_name}"


def configure_lab_round(env: Mapping[str, str] | Any, round_name: str) -> str:
    """Set ``LANGSMITH_PROJECT`` in ``env`` for this round; return the name."""
    project = lab_project(round_name)
    env["LANGSMITH_PROJECT"] = project
    return project


def prepare_lab_project(
    env: Mapping[str, str] | Any,
    lab_round: str | None,
    *,
    client_factory: Any,
) -> dict[str, Any] | None:
    """In lab mode: name the round's project, prove the lab target, then set
    retention. Outside lab mode returns None without touching LangSmith.

    Independent review (PR #110, A1): the project setup used to run before
    ``tracing.configure()``, so a disallowed ``LANGSMITH_ENDPOINT`` still
    received requests carrying the API key. The same ``check_lab_target`` the
    tracer applies runs here first; any failure exits with the codes and
    makes zero LangSmith calls.
    """
    if env.get(TRACE_ENV) != "lab":
        return None
    if not lab_round:
        raise SystemExit("OPSPILOT_TRACE=lab requires --lab-round")
    project = configure_lab_round(env, lab_round)
    failures = check_lab_target(env)
    if failures:
        raise SystemExit("lab target check failed: " + ",".join(failures))
    return ensure_longlived_project(client_factory(), project)


def ensure_longlived_project(client: Any, project_name: str) -> dict[str, Any]:
    """Create the project if needed and set the longest retention tier.

    ``langsmith`` 0.12.2's ``update_project`` does not expose ``trace_tier``,
    so the tier is set with ``PATCH /sessions/{id}`` and read back. Returns
    ``{"project_id", "project", "trace_tier"}``; a tier other than
    ``longlived`` after the PATCH is reported, not retried silently.
    """
    session = client.create_project(project_name, upsert=True)
    project_id = str(session.id)
    client.request_with_retries(
        "PATCH",
        f"/sessions/{project_id}",
        request_kwargs={"json": {"trace_tier": LONGLIVED}},
    )
    after = client.request_with_retries("GET", f"/sessions/{project_id}").json()
    return {
        "project": project_name,
        "project_id": project_id,
        "trace_tier": after.get("trace_tier"),
    }


# -- trace read-back ----------------------------------------------------------


def find_root_run(
    client: Any,
    *,
    project_name: str,
    otel_trace_id: str,
    attempts: int = READ_BACK_ATTEMPTS,
    delay_seconds: float = READ_BACK_DELAY_S,
    sleep: Any = time.sleep,
) -> Any | None:
    """The LangSmith root run of this OTel trace, or None.

    LangSmith assigns the OTel span's low 64 bits as the run id and keeps the
    OTel trace id in ``metadata.OTEL_TRACE_ID``. The trace id is the handle:
    every attempt is its own root span with the same business ``run_id`` and a
    renewed Run carries another one, so a ``run_id`` filter could return an
    arbitrary root (Codex review, PR #110 B2). The tracer records the last
    trace id it opened; that is the root the summary links.
    """
    for attempt in range(attempts):
        runs = list(
            client.list_runs(
                project_name=project_name,
                is_root=True,
                filter=(
                    'and(eq(metadata_key, "OTEL_TRACE_ID"), '
                    f'eq(metadata_value, "{otel_trace_id}"))'
                ),
                limit=1,
            )
        )
        if runs:
            return runs[0]
        if attempt + 1 < attempts:
            sleep(delay_seconds)
    return None


def run_url(client: Any, *, project_id: str, langsmith_run_id: str) -> str | None:
    """The UI link LangSmith builds locally; None when the tenant is unknown."""
    tenant = client._get_optional_tenant_id()
    if tenant is None:
        return None
    host = str(getattr(client, "_host_url", "https://smith.langchain.com"))
    return f"{host}/o/{tenant}/projects/p/{project_id}/r/{langsmith_run_id}?poll=true"


def trace_evidence(
    trace_record: Mapping[str, Any],
    *,
    run_id: str,
    project: Mapping[str, Any] | None,
    client_factory: Any,
    attempts: int = READ_BACK_ATTEMPTS,
    delay_seconds: float = READ_BACK_DELAY_S,
) -> dict[str, Any]:
    """The ``trace`` block of a summary: mode, ids, project, retention, link."""
    evidence: dict[str, Any] = {
        "mode": trace_record.get("mode"),
        "otel_trace_id": trace_record.get("trace_id"),
        "dropped_spans": trace_record.get("dropped_spans", 0),
        "project": None,
        "project_id": None,
        "trace_tier": None,
        "langsmith_run_id": None,
        "url": None,
        "read_back": "not_exported",
    }
    if trace_record.get("mode") != "lab" or project is None:
        return evidence
    evidence.update(
        project=project.get("project"),
        project_id=project.get("project_id"),
        trace_tier=project.get("trace_tier"),
    )
    trace_id = trace_record.get("trace_id")
    if not trace_id:
        evidence["read_back"] = "no_trace_id"
        return evidence
    # Read-back is best effort: a transient LangSmith error must not lose the
    # frozen ledger and summary that follow it (Codex review, PR #110 B3).
    try:
        client = client_factory()
        root = find_root_run(
            client,
            project_name=str(project["project"]),
            otel_trace_id=str(trace_id),
            attempts=attempts,
            delay_seconds=delay_seconds,
        )
        if root is None:
            evidence["read_back"] = "root_run_not_found"
            return evidence
        url = run_url(
            client,
            project_id=str(project["project_id"]),
            langsmith_run_id=str(root.id),
        )
    except Exception as exc:  # noqa: BLE001 - recorded, never raised past freeze
        evidence["read_back"] = f"error:{type(exc).__name__}"
        return evidence
    evidence["langsmith_run_id"] = str(root.id)
    evidence["url"] = url
    evidence["read_back"] = "found"
    return evidence


# -- frozen summary -----------------------------------------------------------


def ledger_dir(experiment: str, run_id: str) -> Path:
    """Where the raw ledger goes: ignored by git, never under docs/evidence."""
    explicit = os.environ.get(LEDGER_DIR_ENV)
    base = Path(explicit).expanduser() if explicit else DEFAULT_LEDGER_ROOT
    return base / experiment / run_id


def parse_report(content: str | None) -> Any:
    """The report as JSON when it parses, else the text; None when absent."""
    if content is None:
        return None
    try:
        return json.loads(content)
    except ValueError:
        return content


def freeze(
    ledger: Mapping[str, Any],
    *,
    experiment: str,
    run_id: str,
    evidence_dir: Path,
    summary: Mapping[str, Any],
) -> tuple[dict[str, Any], Path]:
    """Write the raw ledger outside the repo's evidence and the summary inside.

    Returns ``(summary_written, summary_path)``. The summary carries the
    ledger's sha256 and byte count; ``summary`` is the caller's frozen content
    (report, verdicts, counts, cost, trace). ``evidence_dir`` receives
    ``summary.json`` and nothing else.
    """
    raw = json.dumps(ledger, indent=2, ensure_ascii=False, default=str).encode()
    target = ledger_dir(experiment, run_id)
    target.mkdir(parents=True, exist_ok=True)
    ledger_path = target / "ledger.json"
    ledger_path.write_bytes(raw)
    frozen: dict[str, Any] = {
        "schema": SUMMARY_SCHEMA,
        "experiment": experiment,
        "run_id": run_id,
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        **summary,
        "raw_ledger": {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            # Relative inside the checkout so the public summary carries no
            # local home directory; an external OPSPILOT_LEDGER_DIR is named
            # by its last three components only.
            "path": str(ledger_path.relative_to(ROOT))
            if ledger_path.is_relative_to(ROOT)
            else str(Path(*ledger_path.parts[-3:])),
            "in_repository": False,
        },
    }
    evidence_dir.mkdir(parents=True, exist_ok=False)
    summary_path = evidence_dir / "summary.json"
    summary_path.write_text(
        json.dumps(frozen, indent=2, ensure_ascii=False, default=str) + "\n"
    )
    return frozen, summary_path


# The tracer's checks that concern the LangSmith target itself (endpoint and
# key). The Run-only codes (tool profile, Prometheus/Jaeger, LANGSMITH_PROJECT)
# are not part of this subset: a client is also needed where no Run opens.
LANGSMITH_TARGET_CODES = frozenset(
    {"LANGSMITH_ENDPOINT_NOT_ALLOWED", "LANGSMITH_KEY_MISSING"}
)
# Same canonical default as ``opspilot.tracing``; it is still validated by
# ``check_lab_target`` before use, so the two cannot drift into an open hole.
DEFAULT_LANGSMITH_ENDPOINT = "https://api.smith.langchain.com"


def langsmith_target_failures(env: Mapping[str, str] | Any) -> tuple[str, ...]:
    """Endpoint/key failures from ``opspilot.tracing.check_lab_target``."""
    return tuple(
        code
        for code in check_lab_target({**env, TRACE_ENV: "lab"})
        if code in LANGSMITH_TARGET_CODES
    )


def langsmith_client(env: Mapping[str, str] | Any | None = None) -> Any:
    """A ``langsmith.Client`` bound explicitly to the validated endpoint and key.

    Independent review (PR #110 round 2, #1): ``Client()`` with no arguments
    resolves its endpoint and key from ambient SDK configuration too
    (``LANGCHAIN_ENDPOINT``, ``LANGCHAIN_API_KEY``, ...), so with
    ``LANGSMITH_ENDPOINT`` unset the API key went wherever that pointed. The
    endpoint used here is ``LANGSMITH_ENDPOINT`` (or the canonical default),
    accepted by the tracer's own check first, and the key is the named one;
    any failure refuses before the client exists. Imported lazily so the
    experiment scripts need the SDK only in lab mode.
    """
    env = os.environ if env is None else env
    failures = langsmith_target_failures(env)
    if failures:
        raise SystemExit("langsmith target check failed: " + ",".join(failures))
    from langsmith import Client

    return Client(
        api_url=env.get("LANGSMITH_ENDPOINT") or DEFAULT_LANGSMITH_ENDPOINT,
        api_key=env["LANGSMITH_API_KEY"],
        workspace_id=env.get("LANGSMITH_WORKSPACE_ID") or None,
    )
