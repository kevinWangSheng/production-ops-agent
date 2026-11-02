"""F14 step 3, #2356: freeze the OpsPilot trace rerun (2026-11-02).

The four OpsPilot Runs of the #2356 pairing were rerun with
``OPSPILOT_TRACE=lab`` (user-approved, over the Q8 cap). This copies each
Run's frozen summary next to the original ones under
``upstream-2356-failed-source/opspilot/<run_id>/summary.json``, re-derives the
S7 fields with the same ``issues.opspilot_run`` the original freeze used,
reads the LangSmith root run back (child-span counts by run type), and
recomputes Q7 with the Holmes side of the original record (Holmes was not
rerun). The result goes to ``upstream-2356-failed-source/opspilot-trace-rerun.json``;
``record.json`` (the original Q7 decision) is not touched.

    python -m scripts.holmes_baseline.opspilot_trace_rerun <out-dir>...

Each ``<out-dir>`` is one ``M1_ACCEPTANCE_OUT`` named ``<group>-<n>`` holding
``<run_id>/summary.json``, ``first-request.json`` and its versions sidecar.
The LangSmith key is read in-process from ``OPSPILOT_ENV_FILE``; nothing is
printed but the derived fields.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from scripts.holmes_baseline import issues

ROOT = Path(__file__).resolve().parents[2]
PAIRED = ROOT / issues.EVIDENCE_PREFIX / issues.PAIRED
INDEX = PAIRED / "opspilot-trace-rerun.json"
LABEL = "trace rerun 2026-11-02 (OPSPILOT_TRACE=lab)"


def _client() -> Any:
    from scripts.lab_evidence import langsmith_client
    from scripts.m1_live_flash_loop import load_langsmith_env

    env_file = os.environ.get("OPSPILOT_ENV_FILE")
    if env_file:
        load_langsmith_env(Path(env_file))
    return langsmith_client()


def child_spans(
    trace: dict[str, Any], client_factory: Callable[[], Any] = _client
) -> dict[str, Any]:
    """Run-type counts under the summary's LangSmith root run."""
    root_id, project = trace.get("langsmith_run_id"), trace.get("project")
    if not root_id or not project:
        return {"read_back": "no_root", "kinds": {}}
    try:
        client = client_factory()
        root = client.read_run(root_id)
        kinds: dict[str, int] = {}
        for child in client.list_runs(project_name=project, trace_id=root.trace_id):
            if str(child.id) == str(root.id):
                continue
            kind = str(getattr(child, "run_type", "unknown"))
            kinds[kind] = kinds.get(kind, 0) + 1
        return {
            "read_back": "found",
            "root_status": getattr(root, "status", None),
            "kinds": kinds,
        }
    except Exception as exc:  # noqa: BLE001 - recorded, never raised
        return {"read_back": f"error:{type(exc).__name__}", "kinds": {}}


def freeze(out_dirs: Sequence[Path]) -> dict[str, Any]:
    record = json.loads((PAIRED / "record.json").read_text())
    groups: dict[str, list[dict[str, Any]]] = {"prometheus": [], "jaeger": []}
    for out in out_dirs:
        group = out.name.split("-", 1)[0]
        (summary_path,) = list(out.glob("*/summary.json"))
        summary = json.loads(summary_path.read_text())
        rel = f"opspilot/{summary['run_id']}/summary.json"
        (PAIRED / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(summary_path, PAIRED / rel)
        first = out / "first-request.json"
        sidecar = first.with_name(first.name + ".versions.json")
        entry = issues.opspilot_run(
            PAIRED / rel,
            rel,
            first.read_bytes(),
            group,
            json.loads(sidecar.read_text()) if sidecar.exists() else None,
        )
        trace = summary.get("trace") or {}
        entry["trace"] = {
            "project": trace.get("project"),
            "read_back": trace.get("read_back"),
            "url": trace.get("url"),
            "langsmith_run_id": trace.get("langsmith_run_id"),
            "child_spans": child_spans(trace),
        }
        counts = summary.get("counts") or {}
        entry["model_requests"] = counts.get("http_count")
        groups[group].append(entry)
    side = {
        "groups": {g: {"n": len(rs), "runs": rs} for g, rs in groups.items()},
        # Holmes was not rerun: Q7 keeps the original Holmes side
        "holmes": record["opspilot_side"]["holmes"],
    }
    index = {
        "label": LABEL,
        "original_q7": record["opspilot_side"]["q7"],
        "groups": side["groups"],
        "q7_recomputed": issues.q7(side),
    }
    index["q7_matches_original"] = {
        k: index["q7_recomputed"][k] == index["original_q7"][k]
        for k in ("input_gap", "report_gap", "n1_triggered")
    }
    return index


def main(argv: list[str] | None = None) -> int:
    dirs = [Path(a) for a in (sys.argv[1:] if argv is None else argv)]
    index = freeze(dirs)
    INDEX.write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {
                "q7_recomputed": index["q7_recomputed"],
                "matches": index["q7_matches_original"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
