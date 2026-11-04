"""Offline acceptance tests for the 72h soak summarizer (F8 step 4).

Contract: soak pre-review r0, S-R1..S-R18
(``~/.agent/handoffs/opspilot-prereview-soak-2026-11-04.md``). These tests
were written by an independent author from that contract only, without the
implementation branch. They pin the *external* behaviour of the summarizer:
raw soak data in, ``summary.json`` out. No network, no product code.

Entry point (the implementation must provide exactly this)
-----------------------------------------------------------

Module ``scripts.soak.summarize``:

* ``summarize(raw_dir: pathlib.Path) -> dict`` returns the summary.
* ``main(argv: list[str] | None = None) -> int``; CLI
  ``python -m scripts.soak.summarize RAW_DIR --out SUMMARY_JSON``.
  Exit 0 when the summary was written (also when step 4 is *not* achieved),
  exit 2 without writing anything when ``plan.json``, ``events.jsonl`` or
  ``meta.json`` is missing.
* Output is deterministic: same raw dir -> equal JSON.

All timestamps are ISO-8601 UTC strings (``...Z``). Durations are seconds.

Raw directory layout (input)
----------------------------

``plan.json`` (frozen before T0, S-R6)::

    {"product_alertname": "CheckoutPlaceOrderErrorRatioHigh",
     "expected_deployments": ["checkout", ...],
     "model_price_cny_per_mtok": {"input": float, "output": float},
     "injections": [{"id": "E01", "category": "episode"|"noise"|"agent_fault",
                     "name": str, "planned_start": ts,
                     "planned_duration_s": int}]}

``events.jsonl`` (scheduler log, da-server clock), one object per line with
``ts`` and ``type``:

* ``soak_start`` / ``soak_end``
* ``injection_on`` / ``injection_off`` (``injection_id``)
* ``injection_skipped`` (``injection_id``, ``reason``)
* ``env_interruption_start`` / ``env_interruption_end`` (``cause``)
* ``env_action`` (``action``, ``reason``)   -- S-R13 environment-only actions
* ``tunnel_down`` / ``tunnel_up``
* ``clock_check`` (``host``, ``offset_s``)  -- offset vs da-server, S-R5
* ``rental_lost``                            -- host lost/reinstalled, S-R4

``meta.json`` (written after the run)::

    {"snapshot_at": ts,          # when the PG exports below were taken
     "rental": {"hourly_usd": float, "powered_on_at": ts,
                "powered_off_at": ts},
     "balance_readings": [{"ts": ts, "balance_cny": float}]}   # 0..n

``alerts.jsonl``: lab Alertmanager firings ``{alertname, fingerprint,
starts_at}`` (rental clock).

``deliveries.jsonl`` (PG export): ``{delivery_id, fingerprint, received_at,
outcome: "run"|"handoff"|"attached"|null, outcome_at: ts|null,
run_id: str|null, incident_id: str|null}``.

``runs.jsonl`` (PG export): ``{run_id, incident_id, state, created_at,
started_at|null, finished_at|null, wall_clock_limit_s, input_tokens,
output_tokens}``; ``state`` is the product run execution state.

``sessions.jsonl`` (PG export, recovery observation): ``{session_id,
incident_id, started_at, ended_at|null, interval_s, max_duration_s,
transition|null}``.

CSV files (header row, ISO ``ts`` column):

* ``samples.csv``: ``ts,session_id,signal_status`` (``ok|missing|stale``)
* ``readyz.csv``: ``ts,role,status`` -- local probe on da-server, HTTP code,
  ``0`` = no response (S-R7 primary)
* ``prom_up.csv``: ``ts,role,up`` -- lab Prometheus ``up{job="opspilot"}``
  per scrape, through the tunnel (S-R7 secondary, S-R8b)
* ``ksm.csv``: ``ts,deployment,pods`` -- pods per expected Deployment from
  the KSM owner mapping only (S-R8c)
* ``docker_stats.csv``: ``ts,host,group,container,cpu_pct,mem_bytes``;
  ``group`` is ``opspilot|lab|shared`` (``shared`` = da-server shared
  services, S-R10)
* ``host_stats.csv``: ``ts,host,mem_available_bytes,load1,disk_free_bytes``
* ``sizes.csv``: ``ts,host,kind,bytes`` (``kind`` e.g. ``pg_database``,
  ``log_volume``; start and end readings)

Summary (output) -- keys pinned by these tests
----------------------------------------------

* ``production_proof``: always ``false``.
* ``plan_sha256``: sha256 hex of the ``plan.json`` bytes.
* ``window``: ``start``, ``end``, ``covered_s``, ``interruption_s``,
  ``extension_s``, ``required_s``, ``coverage_ok``, ``rental_lost``.
  Rule (S-R4): interruptions are not deducted; if total environment
  interruption > 4h, required = 72h + min(interruption, 12h), else 72h.
* ``clock``: ``tolerance_s`` (2), ``max_abs_offset_s``, ``over_1s`` (list of
  ``{ts, host, offset_s}`` with |offset| > 1).
* ``injections``: list of ``{id, category, executed, skipped_reason,
  disposition}``. ``executed`` = has ``injection_on``. ``disposition`` only
  for executed episodes, else ``null``: ``run|handoff|attached|none``.
  Linking: window W = [on, next executed episode's on, or soak_end); firing =
  earliest alert with ``product_alertname`` and ``starts_at`` in W; delivery
  = earliest delivery with the same fingerprint and
  ``received_at >= firing - 2s`` and ``< W end``; disposition = its
  ``outcome`` (null outcome or no firing/delivery -> ``none``).

Every metric section (``availability``, ``missing_observations``,
``stuck_work``, ``resources``, ``cost``) has ``published`` (bool) and
``sources`` (raw file names it used). If one of its raw files is absent:
``published: false`` and ``missing_sources`` lists them. Required files:

* availability: readyz.csv, prom_up.csv, alerts.jsonl, deliveries.jsonl
* missing_observations: sessions.jsonl, samples.csv, prom_up.csv, ksm.csv
* stuck_work: runs.jsonl, sessions.jsonl, alerts.jsonl, deliveries.jsonl
* resources: docker_stats.csv, host_stats.csv, sizes.csv
* cost: runs.jsonl

Section contents:

* ``availability.readyz[role]`` / ``availability.prometheus_up[role]``:
  ``{ticks, ok, ratio}`` (ok = status 200 / up 1).
  ``availability.tunnel``: ``{disconnects, disconnected_s}``.
  ``availability.end_to_end``: per executed episode ``{injection_id,
  outcome, outcome_after_s, within_10min}``; within_10min = outcome is
  run/handoff and ``outcome_at - firing <= 600``.
* ``missing_observations.recovery``: ``{expected, recorded, missing,
  signal_missing, stale}`` totals; expected per session =
  floor((ended_at or snapshot_at - started_at) / interval_s), missing =
  max(0, expected - recorded).
  ``missing_observations.scrape_gaps``: ``{role, start, end,
  missed_scrapes}`` for runs of >= 2 consecutive ``up=0`` rows per role.
  ``missing_observations.lab_liveness``: ``source`` =
  ``"kube_state_metrics_owner_mapping"``, ``series`` = the three KSM series,
  ``limitation`` (non-empty text about stale/zombie series), ``ticks``
  (distinct ksm ts), ``absent`` = ``{ts, deployment}`` where an expected
  deployment has pods 0 or no row at that tick.
* ``stuck_work``: each of ``non_terminal_runs`` (state queued/running),
  ``waiting_on_human_runs`` (waiting_human/paused/blocked -- reported, not
  stuck), ``over_wall_clock_runs`` ((finished_at or snapshot_at) -
  started_at > wall_clock_limit_s), ``long_queued_runs`` ((started_at or
  snapshot_at) - created_at > 600), ``overdue_recovery_sessions`` ((ended_at
  or snapshot_at) - started_at > max_duration_s + 300),
  ``deliveries_without_outcome``, ``unaccounted_injections`` (executed
  episodes with disposition none) is ``{count, ids}`` (ids sorted).
* ``resources.containers`` (groups opspilot and lab) and
  ``resources.shared_services`` (group shared): lists of ``{host, group,
  container, cpu_pct, mem_bytes}``; ``resources.hosts[host]`` has
  ``mem_available_bytes``, ``load1``, ``disk_free_bytes``. Every stats object
  is ``{min, p50, p95, max}`` using nearest-rank percentiles.
  ``resources.sizes``: ``{host, kind, start_bytes, end_bytes, delta_bytes}``.
* ``cost.model``: ``{runs, input_tokens, output_tokens, token_cost_cny}``;
  ``cost.balance_delta_cny`` = first - last reading, ``null`` with < 2;
  ``cost.rental``: ``{hours, hourly_usd, estimated_usd}``;
  ``cost.langsmith_usd`` = 0.
* ``environment``: ``actions`` (``{ts, action, reason}``), ``interruptions``.
* ``step4``: ``{achieved, unmet}`` (S-R12). Achieved iff ``unmet`` is empty.
  Unmet codes: ``coverage``, ``rental_lost``, ``injection_unexplained:<id>``
  (planned, not executed, no skip reason), ``injection_unaccounted:<id>``,
  ``section_unpublished:<section>``. Bad numbers alone never make it unmet.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts.soak import summarize as soak

REPO = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 11, 10, 0, 0, 0, tzinfo=UTC)
H = 3600
ROLES = ("web", "worker", "observer")
DEPLOYMENTS = ("checkout", "payment", "frontend")
ALERT = "CheckoutPlaceOrderErrorRatioHigh"


def ts(offset_s: float) -> str:
    moment = T0 + timedelta(seconds=offset_s)
    text = moment.strftime("%Y-%m-%dT%H:%M:%S")
    if moment.microsecond:
        text += f".{moment.microsecond:06d}"
    return text + "Z"


CSV_HEADERS = {
    "samples.csv": ["ts", "session_id", "signal_status"],
    "readyz.csv": ["ts", "role", "status"],
    "prom_up.csv": ["ts", "role", "up"],
    "ksm.csv": ["ts", "deployment", "pods"],
    "docker_stats.csv": ["ts", "host", "group", "container", "cpu_pct", "mem_bytes"],
    "host_stats.csv": ["ts", "host", "mem_available_bytes", "load1", "disk_free_bytes"],
    "sizes.csv": ["ts", "host", "kind", "bytes"],
}


def healthy() -> dict[str, Any]:
    """A complete, healthy 72h soak: two episodes, one noise, one agent fault."""
    e1_on, e2_on = 1 * H, 4 * H
    f1, f2 = e1_on + 180, e2_on + 200
    data: dict[str, Any] = {
        "plan.json": {
            "product_alertname": ALERT,
            "expected_deployments": list(DEPLOYMENTS),
            "model_price_cny_per_mtok": {"input": 2.0, "output": 3.0},
            "injections": [
                {
                    "id": "E01",
                    "category": "episode",
                    "name": "paymentFailure",
                    "planned_start": ts(e1_on),
                    "planned_duration_s": 900,
                },
                {
                    "id": "N01",
                    "category": "noise",
                    "name": "adManualGc",
                    "planned_start": ts(2 * H),
                    "planned_duration_s": 900,
                },
                {
                    "id": "E02",
                    "category": "episode",
                    "name": "paymentFailure",
                    "planned_start": ts(e2_on),
                    "planned_duration_s": 900,
                },
                {
                    "id": "A1",
                    "category": "agent_fault",
                    "name": "kill_worker",
                    "planned_start": ts(30 * H),
                    "planned_duration_s": 0,
                },
            ],
        },
        "meta.json": {
            "snapshot_at": ts(72 * H + 31 * 60),
            "rental": {
                "hourly_usd": 0.25,
                "powered_on_at": ts(-4 * H),
                "powered_off_at": ts(76 * H),
            },
            "balance_readings": [
                {"ts": ts(-60), "balance_cny": 100.0},
                {"ts": ts(72 * H + 60), "balance_cny": 98.5},
            ],
        },
        "events.jsonl": [
            {"ts": ts(0), "type": "soak_start"},
            {"ts": ts(0), "type": "clock_check", "host": "rental", "offset_s": 0.2},
            {"ts": ts(e1_on), "type": "injection_on", "injection_id": "E01"},
            {"ts": ts(e1_on + 900), "type": "injection_off", "injection_id": "E01"},
            {"ts": ts(2 * H), "type": "injection_on", "injection_id": "N01"},
            {"ts": ts(2 * H + 900), "type": "injection_off", "injection_id": "N01"},
            {"ts": ts(e2_on), "type": "injection_on", "injection_id": "E02"},
            {"ts": ts(e2_on + 900), "type": "injection_off", "injection_id": "E02"},
            {"ts": ts(30 * H), "type": "injection_on", "injection_id": "A1"},
            {
                "ts": ts(72 * H),
                "type": "clock_check",
                "host": "rental",
                "offset_s": -0.4,
            },
            {"ts": ts(72 * H), "type": "soak_end"},
        ],
        "alerts.jsonl": [
            {"alertname": ALERT, "fingerprint": "fpA", "starts_at": ts(f1)},
            {"alertname": ALERT, "fingerprint": "fpA", "starts_at": ts(f2)},
        ],
        "deliveries.jsonl": [
            {
                "delivery_id": "D1",
                "fingerprint": "fpA",
                "received_at": ts(f1 + 3),
                "outcome": "run",
                "outcome_at": ts(f1 + 300),
                "run_id": "R1",
                "incident_id": "I1",
            },
            {
                "delivery_id": "D2",
                "fingerprint": "fpA",
                "received_at": ts(f2 + 3),
                "outcome": "run",
                "outcome_at": ts(f2 + 120),
                "run_id": "R2",
                "incident_id": "I2",
            },
        ],
        "runs.jsonl": [
            {
                "run_id": "R1",
                "incident_id": "I1",
                "state": "completed",
                "created_at": ts(f1 + 300),
                "started_at": ts(f1 + 305),
                "finished_at": ts(f1 + 900),
                "wall_clock_limit_s": 1800,
                "input_tokens": 100_000,
                "output_tokens": 10_000,
            },
            {
                "run_id": "R2",
                "incident_id": "I2",
                "state": "completed",
                "created_at": ts(f2 + 120),
                "started_at": ts(f2 + 125),
                "finished_at": ts(f2 + 700),
                "wall_clock_limit_s": 1800,
                "input_tokens": 200_000,
                "output_tokens": 20_000,
            },
        ],
        "sessions.jsonl": [
            {
                "session_id": "S1",
                "incident_id": "I1",
                "started_at": ts(e1_on + 1200),
                "ended_at": ts(e1_on + 1200 + 900),
                "interval_s": 60,
                "max_duration_s": 3600,
                "transition": "recovery_confirmed",
            },
        ],
        "samples.csv": [[ts(e1_on + 1200 + 60 * i), "S1", "ok"] for i in range(1, 16)],
        "readyz.csv": [
            [ts(t), role, 200] for t in (0, 24 * H, 48 * H, 72 * H) for role in ROLES
        ],
        "prom_up.csv": [
            [ts(t), role, 1] for t in (0, 24 * H, 48 * H, 72 * H) for role in ROLES
        ],
        "ksm.csv": [[ts(t), dep, 1] for t in (0, 36 * H) for dep in DEPLOYMENTS],
        "docker_stats.csv": [
            [ts(60 * i), "da-server", "opspilot", "opspilot-worker", i, i * 1000]
            for i in range(1, 21)
        ]
        + [
            [ts(60 * i), "da-server", "shared", "sub2api", 5, 7000] for i in range(1, 4)
        ],
        "host_stats.csv": [
            [ts(60 * i), "da-server", 6_000_000_000 - i, 0.5, 60_000_000_000]
            for i in range(1, 5)
        ],
        "sizes.csv": [
            [ts(0), "da-server", "pg_database", 10_000_000],
            [ts(72 * H), "da-server", "pg_database", 14_000_000],
        ],
    }
    return data


def write(raw: Path, data: dict[str, Any]) -> Path:
    raw.mkdir(parents=True, exist_ok=True)
    for name, content in data.items():
        if content is None:
            continue
        path = raw / name
        if name.endswith(".json"):
            path.write_text(json.dumps(content, indent=2), encoding="utf-8")
        elif name.endswith(".jsonl"):
            # The scheduler log is append-only, so it is in time order.
            if name == "events.jsonl":
                content = sorted(content, key=lambda e: e["ts"])
            path.write_text(
                "".join(json.dumps(r) + "\n" for r in content), encoding="utf-8"
            )
        else:
            with path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(CSV_HEADERS[name])
                writer.writerows(content)
    return raw


def run(tmp_path: Path, data: dict[str, Any]) -> dict[str, Any]:
    return soak.summarize(write(tmp_path / "raw", data))


@pytest.fixture
def data() -> dict[str, Any]:
    return healthy()


def by_id(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in summary["injections"]}


# --- entry point, determinism, framing -------------------------------------


def test_healthy_soak_is_achieved_and_never_claims_production_proof(tmp_path, data):
    summary = run(tmp_path, data)
    assert summary["step4"] == {"achieved": True, "unmet": []}
    assert summary["production_proof"] is False
    for section in (
        "availability",
        "missing_observations",
        "stuck_work",
        "resources",
        "cost",
    ):
        assert summary[section]["published"] is True, section
        assert summary[section]["sources"], section


def test_plan_hash_is_sha256_of_frozen_plan_bytes(tmp_path, data):
    raw = write(tmp_path / "raw", data)
    expected = hashlib.sha256((raw / "plan.json").read_bytes()).hexdigest()
    assert soak.summarize(raw)["plan_sha256"] == expected


def test_cli_writes_summary_json_and_is_deterministic(tmp_path, data):
    raw = write(tmp_path / "raw", data)
    out1, out2 = tmp_path / "a.json", tmp_path / "b.json"
    assert soak.main([str(raw), "--out", str(out1)]) == 0
    assert soak.main([str(raw), "--out", str(out2)]) == 0
    first = json.loads(out1.read_text(encoding="utf-8"))
    assert first == json.loads(out2.read_text(encoding="utf-8"))
    assert first == soak.summarize(raw)


def test_module_runs_as_python_dash_m(tmp_path, data):
    raw = write(tmp_path / "raw", data)
    out = tmp_path / "summary.json"
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.soak.summarize", str(raw), "--out", str(out)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(out.read_text(encoding="utf-8"))["step4"]["achieved"] is True


@pytest.mark.parametrize("core", ["plan.json", "events.jsonl", "meta.json"])
def test_missing_core_input_exits_2_without_writing(tmp_path, data, core):
    data[core] = None
    raw = write(tmp_path / "raw", data)
    out = tmp_path / "summary.json"
    assert soak.main([str(raw), "--out", str(out)]) == 2
    assert not out.exists()


def test_missing_raw_file_unpublishes_only_its_sections_and_fails_step4(tmp_path, data):
    data["ksm.csv"] = None
    summary = run(tmp_path, data)
    section = summary["missing_observations"]
    assert section["published"] is False
    assert section["missing_sources"] == ["ksm.csv"]
    assert summary["availability"]["published"] is True
    assert summary["step4"]["achieved"] is False
    assert summary["step4"]["unmet"] == ["section_unpublished:missing_observations"]


def test_bad_numbers_do_not_affect_achieved(tmp_path, data):
    data["readyz.csv"] = [[ts(t), role, 503] for t in (0, H) for role in ROLES]
    data["runs.jsonl"][0]["state"] = "failed"
    summary = run(tmp_path, data)
    assert summary["availability"]["readyz"]["web"]["ratio"] == 0
    assert summary["step4"]["achieved"] is True


# --- S-R4 window and S-R5 clock --------------------------------------------


def _interrupt(data: dict[str, Any], start_s: int, hours: float) -> None:
    data["events.jsonl"] += [
        {
            "ts": ts(start_s),
            "type": "env_interruption_start",
            "cause": "provider maintenance",
        },
        {
            "ts": ts(start_s + hours * H),
            "type": "env_interruption_end",
            "cause": "provider maintenance",
        },
    ]


def _end_at(data: dict[str, Any], end_s: int) -> None:
    for event in data["events.jsonl"]:
        if event["type"] == "soak_end":
            event["ts"] = ts(end_s)


def test_window_72h_without_interruptions(tmp_path, data):
    window = run(tmp_path, data)["window"]
    assert window["covered_s"] == 72 * H
    assert window["required_s"] == 72 * H
    assert window["coverage_ok"] is True
    assert window["start"] == ts(0) and window["end"] == ts(72 * H)


def test_short_run_fails_coverage(tmp_path, data):
    _end_at(data, 70 * H)
    summary = run(tmp_path, data)
    assert summary["window"]["coverage_ok"] is False
    assert "coverage" in summary["step4"]["unmet"]


def test_interruptions_up_to_4h_are_not_deducted_and_not_extended(tmp_path, data):
    _interrupt(data, 10 * H, 3)
    window = run(tmp_path, data)["window"]
    assert window["interruption_s"] == 3 * H
    assert window["extension_s"] == 0
    assert window["coverage_ok"] is True


def test_interruptions_over_4h_extend_end_by_same_length(tmp_path, data):
    _interrupt(data, 10 * H, 2)
    _interrupt(data, 20 * H, 3)
    _end_at(data, 74 * H)
    window = run(tmp_path, data)["window"]
    assert window["interruption_s"] == 5 * H
    assert window["extension_s"] == 5 * H
    assert window["required_s"] == 77 * H
    assert window["coverage_ok"] is False

    data2 = healthy()
    _interrupt(data2, 10 * H, 5)
    _end_at(data2, 77 * H)
    assert run(tmp_path / "b", data2)["window"]["coverage_ok"] is True


def test_extension_is_capped_at_12h(tmp_path, data):
    _interrupt(data, 10 * H, 15)
    _end_at(data, 84 * H)
    window = run(tmp_path, data)["window"]
    assert window["extension_s"] == 12 * H
    assert window["required_s"] == 84 * H
    assert window["coverage_ok"] is True


def test_rental_lost_voids_the_run(tmp_path, data):
    data["events.jsonl"].insert(3, {"ts": ts(40 * H), "type": "rental_lost"})
    summary = run(tmp_path, data)
    assert summary["window"]["rental_lost"] is True
    assert "rental_lost" in summary["step4"]["unmet"]


def test_clock_offsets_over_1s_are_listed(tmp_path, data):
    data["events.jsonl"].append(
        {"ts": ts(6 * H), "type": "clock_check", "host": "rental", "offset_s": -1.5}
    )
    clock = run(tmp_path, data)["clock"]
    assert clock["tolerance_s"] == 2
    assert clock["max_abs_offset_s"] == pytest.approx(1.5)
    assert clock["over_1s"] == [{"ts": ts(6 * H), "host": "rental", "offset_s": -1.5}]


# --- S-R6 injections and S-R9 dispositions ---------------------------------


def test_every_executed_episode_has_a_disposition(tmp_path, data):
    injections = by_id(run(tmp_path, data))
    assert injections["E01"]["executed"] is True
    assert injections["E01"]["disposition"] == "run"
    assert injections["E02"]["disposition"] == "run"
    # Noise and agent faults need no disposition (noise must not start a Run).
    assert injections["N01"]["disposition"] is None
    assert injections["A1"]["executed"] is True
    assert injections["A1"]["disposition"] is None


@pytest.mark.parametrize("outcome", ["handoff", "attached"])
def test_handoff_and_attach_are_valid_dispositions(tmp_path, data, outcome):
    data["deliveries.jsonl"][1].update(outcome=outcome, run_id=None)
    summary = run(tmp_path, data)
    assert by_id(summary)["E02"]["disposition"] == outcome
    assert summary["stuck_work"]["unaccounted_injections"] == {"count": 0, "ids": []}
    assert summary["step4"]["achieved"] is True


def test_episode_without_alert_is_unaccounted(tmp_path, data):
    data["alerts.jsonl"] = data["alerts.jsonl"][:1]
    data["deliveries.jsonl"] = data["deliveries.jsonl"][:1]
    summary = run(tmp_path, data)
    assert by_id(summary)["E02"]["disposition"] == "none"
    assert summary["stuck_work"]["unaccounted_injections"] == {
        "count": 1,
        "ids": ["E02"],
    }
    assert summary["step4"]["unmet"] == ["injection_unaccounted:E02"]


def test_delivery_with_null_outcome_is_unaccounted_and_listed(tmp_path, data):
    data["deliveries.jsonl"][0].update(outcome=None, outcome_at=None, run_id=None)
    summary = run(tmp_path, data)
    assert by_id(summary)["E01"]["disposition"] == "none"
    assert summary["stuck_work"]["deliveries_without_outcome"] == {
        "count": 1,
        "ids": ["D1"],
    }
    assert "injection_unaccounted:E01" in summary["step4"]["unmet"]


def test_cross_host_match_tolerates_2s_skew(tmp_path, data):
    firing = 1 * H + 180
    data["deliveries.jsonl"][0]["received_at"] = ts(firing - 1.5)
    assert by_id(run(tmp_path, data))["E01"]["disposition"] == "run"


def test_delivery_beyond_skew_tolerance_is_not_matched(tmp_path, data):
    firing = 1 * H + 180
    data["deliveries.jsonl"][0]["received_at"] = ts(firing - 5)
    assert by_id(run(tmp_path, data))["E01"]["disposition"] == "none"


def test_delivery_for_other_fingerprint_is_not_matched(tmp_path, data):
    data["deliveries.jsonl"][0]["fingerprint"] = "fpOther"
    assert by_id(run(tmp_path, data))["E01"]["disposition"] == "none"


def test_first_delivery_wins_over_later_repeats(tmp_path, data):
    firing = 1 * H + 180
    data["deliveries.jsonl"].append(
        {
            "delivery_id": "D1b",
            "fingerprint": "fpA",
            "received_at": ts(firing + 400),
            "outcome": "attached",
            "outcome_at": ts(firing + 400),
            "run_id": None,
            "incident_id": "I1",
        }
    )
    assert by_id(run(tmp_path, data))["E01"]["disposition"] == "run"


def test_skipped_injection_with_reason_is_reported_not_unmet(tmp_path, data):
    data["events.jsonl"] = [
        e for e in data["events.jsonl"] if e.get("injection_id") != "A1"
    ] + [
        {
            "ts": ts(30 * H),
            "type": "injection_skipped",
            "injection_id": "A1",
            "reason": "worker idle, no in-flight run",
        }
    ]
    summary = run(tmp_path, data)
    a1 = by_id(summary)["A1"]
    assert a1["executed"] is False
    assert a1["skipped_reason"] == "worker idle, no in-flight run"
    assert summary["step4"]["achieved"] is True


def test_planned_injection_neither_executed_nor_skipped_fails_step4(tmp_path, data):
    data["events.jsonl"] = [
        e for e in data["events.jsonl"] if e.get("injection_id") != "N01"
    ]
    summary = run(tmp_path, data)
    assert by_id(summary)["N01"]["executed"] is False
    assert summary["step4"]["unmet"] == ["injection_unexplained:N01"]


# --- S-R7 availability -----------------------------------------------------


def test_readyz_availability_per_role(tmp_path, data):
    data["readyz.csv"][0][2] = 0  # web at T0: no response
    data["readyz.csv"][4][2] = 503  # worker at 24h
    avail = run(tmp_path, data)["availability"]
    assert avail["readyz"]["web"] == {"ticks": 4, "ok": 3, "ratio": 0.75}
    assert avail["readyz"]["worker"] == {"ticks": 4, "ok": 3, "ratio": 0.75}
    assert avail["readyz"]["observer"] == {"ticks": 4, "ok": 4, "ratio": 1.0}


def test_prometheus_up_and_tunnel_disconnects_reported_separately(tmp_path, data):
    data["prom_up.csv"][3][2] = 0  # web at 24h
    data["events.jsonl"] += [
        {"ts": ts(24 * H - 60), "type": "tunnel_down"},
        {"ts": ts(24 * H + 540), "type": "tunnel_up"},
        {"ts": ts(50 * H), "type": "tunnel_down"},
        {"ts": ts(50 * H + 60), "type": "tunnel_up"},
    ]
    avail = run(tmp_path, data)["availability"]
    assert avail["prometheus_up"]["web"]["ratio"] == 0.75
    assert avail["readyz"]["web"]["ratio"] == 1.0
    assert avail["tunnel"] == {"disconnects": 2, "disconnected_s": 660}


def test_end_to_end_within_10_minutes_of_firing(tmp_path, data):
    data["deliveries.jsonl"][1].update(
        outcome="handoff", outcome_at=ts(4 * H + 200 + 700), run_id=None
    )
    e2e = {
        row["injection_id"]: row
        for row in run(tmp_path, data)["availability"]["end_to_end"]
    }
    assert e2e["E01"]["outcome"] == "run"
    assert e2e["E01"]["outcome_after_s"] == 300
    assert e2e["E01"]["within_10min"] is True
    assert e2e["E02"]["outcome_after_s"] == 700
    assert e2e["E02"]["within_10min"] is False
    assert set(e2e) == {"E01", "E02"}


def test_attached_does_not_count_as_end_to_end_success(tmp_path, data):
    data["deliveries.jsonl"][1].update(outcome="attached", outcome_at=ts(4 * H + 210))
    e2e = {
        row["injection_id"]: row
        for row in run(tmp_path, data)["availability"]["end_to_end"]
    }
    assert e2e["E02"]["within_10min"] is False


# --- S-R8 missing observations ---------------------------------------------


def test_recovery_samples_expected_vs_recorded(tmp_path, data):
    # 900 s at 60 s -> 15 expected; drop 2, mark 1 missing-signal, 1 stale.
    rows = data["samples.csv"][:13]
    rows[0][2] = "missing"
    rows[1][2] = "stale"
    data["samples.csv"] = rows
    recovery = run(tmp_path, data)["missing_observations"]["recovery"]
    assert recovery == {
        "expected": 15,
        "recorded": 13,
        "missing": 2,
        "signal_missing": 1,
        "stale": 1,
    }


def test_open_session_counts_expected_until_snapshot(tmp_path, data):
    session = data["sessions.jsonl"][0]
    session["ended_at"] = None
    session["transition"] = None
    snapshot = 72 * H + 31 * 60
    start = 1 * H + 1200
    recovery = run(tmp_path, data)["missing_observations"]["recovery"]
    assert recovery["expected"] == math.floor((snapshot - start) / 60)
    assert recovery["recorded"] == 15


def test_scrape_gaps_need_two_consecutive_misses(tmp_path, data):
    data["prom_up.csv"] = [
        [ts(15 * i), "web", 0 if i in (2, 3, 4, 8) else 1] for i in range(10)
    ] + [[ts(15 * i), "worker", 1] for i in range(10)]
    gaps = run(tmp_path, data)["missing_observations"]["scrape_gaps"]
    assert gaps == [
        {"role": "web", "start": ts(30), "end": ts(60), "missed_scrapes": 3}
    ]


def test_lab_liveness_uses_only_ksm_owner_mapping(tmp_path, data):
    data["ksm.csv"] = [
        [ts(0), "checkout", 1],
        [ts(0), "payment", 0],
        [ts(0), "frontend", 2],
        [ts(60), "checkout", 1],
        [ts(60), "payment", 1],  # frontend has no row
    ]
    liveness = run(tmp_path, data)["missing_observations"]["lab_liveness"]
    assert liveness["source"] == "kube_state_metrics_owner_mapping"
    assert set(liveness["series"]) == {
        "kube_pod_owner",
        "kube_replicaset_owner",
        "kube_pod_container_info",
    }
    assert liveness["limitation"].strip()
    assert liveness["ticks"] == 2
    assert liveness["absent"] == [
        {"ts": ts(0), "deployment": "payment"},
        {"ts": ts(60), "deployment": "frontend"},
    ]


# --- S-R9 stuck work -------------------------------------------------------


def _extra_run(
    run_id: str,
    state: str,
    created: int,
    started: int | None,
    finished: int | None,
    limit: int = 1800,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "incident_id": "I9",
        "state": state,
        "created_at": ts(created),
        "started_at": None if started is None else ts(started),
        "finished_at": None if finished is None else ts(finished),
        "wall_clock_limit_s": limit,
        "input_tokens": 0,
        "output_tokens": 0,
    }


def test_healthy_soak_has_no_stuck_work(tmp_path, data):
    stuck = run(tmp_path, data)["stuck_work"]
    for key in (
        "non_terminal_runs",
        "waiting_on_human_runs",
        "over_wall_clock_runs",
        "long_queued_runs",
        "overdue_recovery_sessions",
        "deliveries_without_outcome",
        "unaccounted_injections",
    ):
        assert stuck[key] == {"count": 0, "ids": []}, key


def test_stuck_runs_are_classified(tmp_path, data):
    data["runs.jsonl"] += [
        _extra_run("R3", "running", 10 * H, 10 * H + 10, None),  # never finished
        _extra_run("R4", "queued", 70 * H, None, None),  # queued > 10 min
        _extra_run("R5", "completed", 20 * H, 20 * H + 30, 21 * H),  # over 1800 s
        _extra_run(
            "R6", "completed", 30 * H, 30 * H + 700, 30 * H + 900
        ),  # waited 700 s
        _extra_run("R7", "blocked", 40 * H, 40 * H + 5, 40 * H + 60),
        _extra_run("R8", "waiting_human", 41 * H, 41 * H + 5, None, limit=10**9),
    ]
    stuck = run(tmp_path, data)["stuck_work"]
    assert stuck["non_terminal_runs"] == {"count": 2, "ids": ["R3", "R4"]}
    assert stuck["waiting_on_human_runs"] == {"count": 2, "ids": ["R7", "R8"]}
    assert stuck["over_wall_clock_runs"] == {"count": 2, "ids": ["R3", "R5"]}
    assert stuck["long_queued_runs"] == {"count": 2, "ids": ["R4", "R6"]}


def test_overdue_recovery_sessions(tmp_path, data):
    data["sessions.jsonl"] += [
        {
            "session_id": "S2",
            "incident_id": "I2",
            "started_at": ts(5 * H),
            "ended_at": ts(5 * H + 3600 + 301),
            "interval_s": 60,
            "max_duration_s": 3600,
            "transition": "observation_ended_unconfirmed",
        },
        {
            "session_id": "S3",
            "incident_id": "I3",
            "started_at": ts(8 * H),
            "ended_at": ts(8 * H + 3600 + 300),
            "interval_s": 60,
            "max_duration_s": 3600,
            "transition": "observation_ended_unconfirmed",
        },
        {
            "session_id": "S4",
            "incident_id": "I4",
            "started_at": ts(60 * H),
            "ended_at": None,
            "interval_s": 60,
            "max_duration_s": 3600,
            "transition": None,
        },
    ]
    stuck = run(tmp_path, data)["stuck_work"]
    assert stuck["overdue_recovery_sessions"] == {"count": 2, "ids": ["S2", "S4"]}


# --- S-R10 resources -------------------------------------------------------


def test_resource_percentiles_use_nearest_rank(tmp_path, data):
    res = run(tmp_path, data)["resources"]
    (worker,) = [c for c in res["containers"] if c["container"] == "opspilot-worker"]
    assert worker["host"] == "da-server" and worker["group"] == "opspilot"
    assert worker["cpu_pct"] == {"min": 1, "p50": 10, "p95": 19, "max": 20}
    assert worker["mem_bytes"] == {
        "min": 1000,
        "p50": 10000,
        "p95": 19000,
        "max": 20000,
    }


def test_shared_services_listed_separately(tmp_path, data):
    res = run(tmp_path, data)["resources"]
    assert all(c["container"] != "sub2api" for c in res["containers"])
    (shared,) = res["shared_services"]
    assert shared["container"] == "sub2api"
    assert shared["mem_bytes"]["max"] == 7000


def test_host_stats_and_size_deltas(tmp_path, data):
    res = run(tmp_path, data)["resources"]
    host = res["hosts"]["da-server"]
    assert host["mem_available_bytes"]["min"] == 6_000_000_000 - 4
    assert host["disk_free_bytes"]["max"] == 60_000_000_000
    assert res["sizes"] == [
        {
            "host": "da-server",
            "kind": "pg_database",
            "start_bytes": 10_000_000,
            "end_bytes": 14_000_000,
            "delta_bytes": 4_000_000,
        }
    ]


# --- S-R11 cost ------------------------------------------------------------


def test_cost_from_tokens_rental_and_balance(tmp_path, data):
    cost = run(tmp_path, data)["cost"]
    assert cost["model"]["runs"] == 2
    assert cost["model"]["input_tokens"] == 300_000
    assert cost["model"]["output_tokens"] == 30_000
    # 0.3 Mtok * 2.0 + 0.03 Mtok * 3.0
    assert cost["model"]["token_cost_cny"] == pytest.approx(0.69)
    assert cost["balance_delta_cny"] == pytest.approx(1.5)
    assert cost["rental"]["hours"] == pytest.approx(80)
    assert cost["rental"]["hourly_usd"] == pytest.approx(0.25)
    assert cost["rental"]["estimated_usd"] == pytest.approx(20)
    assert cost["langsmith_usd"] == 0


def test_balance_delta_is_null_without_two_readings(tmp_path, data):
    data["meta.json"]["balance_readings"] = [{"ts": ts(0), "balance_cny": 100.0}]
    assert run(tmp_path, data)["cost"]["balance_delta_cny"] is None


# --- S-R13 environment actions ---------------------------------------------


def test_environment_actions_and_interruptions_are_published(tmp_path, data):
    data["events.jsonl"].append(
        {
            "ts": ts(12 * H),
            "type": "env_action",
            "action": "rollout restart otel-collector",
            "reason": "frontend span rate 0 for 10 min",
        }
    )
    _interrupt(data, 15 * H, 1)
    env = run(tmp_path, data)["environment"]
    assert env["actions"] == [
        {
            "ts": ts(12 * H),
            "action": "rollout restart otel-collector",
            "reason": "frontend span rate 0 for 10 min",
        }
    ]
    assert len(env["interruptions"]) == 1


def test_input_is_not_mutated(tmp_path, data):
    original = copy.deepcopy(data)
    raw = write(tmp_path / "raw", data)
    before = {p.name: p.read_bytes() for p in raw.iterdir()}
    soak.summarize(raw)
    assert {p.name: p.read_bytes() for p in raw.iterdir()} == before
    assert data == original
