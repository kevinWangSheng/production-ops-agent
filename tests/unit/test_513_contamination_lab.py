"""#513: contamination is recorded per lab, on both systems, with a
sensitivity row; scoring, voids and validity do not change.

Preregistered rules r3 (``evals/preregistered/f1-closure-paired-rules.json``)
record ``predecessor`` and ``contamination`` per slot, never void on them,
and report "a sensitivity row that excludes flagged slots". Each lab has its
own telemetry stores, so only an earlier fault on the same lab can leak into
a window. Synthetic fixtures on the public dev-v2 dataset only.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts import eval_compare_scoring as compare
from scripts import eval_dev_round
from scripts.holmes_baseline import dataset as ds
from tests.test_s7_holmes_dataset_impl import DATASET, fake_output, write_attempt
from tests.unit.test_432_impl import (
    NOISE_NEG,
    NOISE_POS,
    PI_POS,
    _attempt_item,
    _iso,
    _loaded,
)

LAB_A, LAB_B = "/srv/eval/out/lab-0", "/srv/eval/out/lab-1"
HOLMES_SID = "dv2-2b214b"  # question entry, Holmes applicable


def _message() -> str:
    return str(_loaded()[PI_POS][1].injectors[1].params["message"])


def _span_row(minute: int) -> dict[str, Any]:
    return {
        "service": "payment",
        "operation": "grpc.oteldemo.PaymentService/Charge",
        "start_us": int(
            datetime(2026, 11, 2, 12, minute, tzinfo=UTC).timestamp() * 1e6
        ),
        "error": True,
    }


def _leaky_views() -> list[dict[str, Any]]:
    return [
        {"evidence_id": "a", "tool": "traces_search", "span_view": True,
         "window": None, "rows": [_span_row(5)], "text": f"... {_message()} ..."},
    ]  # fmt: skip


def _pair(prior_lab: str | None, later_lab: str | None) -> list[dict[str, Any]]:
    prior = _attempt_item(PI_POS, "prior", 1, 15, effective=2, restored=10)
    later = _attempt_item(
        NOISE_NEG, "later", 20, 30, effective=21, restored=28, views=_leaky_views()
    )
    return [{**prior, "lab": prior_lab}, {**later, "lab": later_lab}]


# -- OpsPilot: the predecessor is on the same lab ----------------------------------


def test_fault_on_another_lab_is_neither_predecessor_nor_contamination():
    result = eval_dev_round.isolation(_pair(LAB_B, LAB_A), _loaded())
    assert result["later"] == {
        "predecessor": None,
        "contamination": [],
        "metrics_overlap": [],
    }


def test_fault_on_the_same_lab_flags_as_before():
    result = eval_dev_round.isolation(_pair(LAB_A, LAB_A), _loaded())
    assert result["later"]["predecessor"]["attempt_id"] == "prior"
    assert result["later"]["contamination"] == [
        f"PRIOR_FAULT_SPAN:{PI_POS}",
        f"PRIOR_INJECTION_TEXT:{PI_POS}",
    ]


@pytest.mark.parametrize("labs", [(None, LAB_A), (LAB_A, None), (None, None)])
def test_unknown_lab_is_compared_with_every_lab(labs):
    result = eval_dev_round.isolation(_pair(*labs), _loaded())
    assert result["later"]["predecessor"]["attempt_id"] == "prior"
    assert result["later"]["contamination"]


def test_nearest_same_lab_fault_is_the_predecessor_even_if_another_lab_ended_later():
    near_other = {
        **_attempt_item(NOISE_POS, "other-lab", 3, 19, effective=4, restored=18),
        "lab": LAB_B,
    }
    items = [*_pair(LAB_A, LAB_A), near_other]
    pred = eval_dev_round.isolation(items, _loaded())["later"]["predecessor"]
    assert pred["attempt_id"] == "prior"
    assert pred["gap_minutes"] == 10.0


def test_context_faults_from_other_rounds_count_on_the_same_lab_only():
    prior, later = _pair(LAB_A, LAB_A)
    entry = eval_dev_round.fault_entry(prior, _loaded()[PI_POS][1])
    assert entry is not None
    flagged = eval_dev_round.isolation([later], _loaded(), [entry])
    assert flagged["later"]["predecessor"]["attempt_id"] == "prior"
    assert flagged["later"]["contamination"]
    elsewhere = eval_dev_round.isolation([later], _loaded(), [{**entry, "lab": LAB_B}])
    assert elsewhere["later"]["predecessor"] is None
    assert elsewhere["later"]["contamination"] == []


def test_lab_is_the_rounds_own_output_root():
    command = ["--mode", "live", "--output-root", LAB_A + "/", "--pair", "x"]
    assert eval_dev_round.lab_of({"command": command}) == LAB_A
    assert eval_dev_round.lab_of({"command": ["--mode", "live"]}) is None
    assert eval_dev_round.lab_of({}) is None


# -- lifecycle timeline shared with HolmesGPT ---------------------------------------


def _ts(minute: int) -> float:
    return datetime(2026, 11, 2, 12, minute, tzinfo=UTC).timestamp()


def _round(
    root: Path, round_id: str, sid: str, attempt_id: str, lab: str, start: int
) -> Path:
    """A lifecycle.json as eval_live writes it (the fields the timeline reads)."""
    directory = root / "rounds" / round_id
    directory.mkdir(parents=True)
    lifecycle = {
        "schema": "opspilot-eval-live-lifecycle-1",
        "round": round_id,
        "command": ["--mode", "live", "--round", round_id, "--output-root", lab],
        "slots": [
            {
                "attempt_id": attempt_id,
                "scenario_id": sid,
                "status": "ran",
                "fault": {
                    "effective_at": _iso(start + 1),
                    "normal_confirmed_at": _iso(start + 9),
                },
                "timings": {"started_at": _iso(start), "ended_at": _iso(start + 14)},
            }
        ],
    }
    (directory / "lifecycle.json").write_text(json.dumps(lifecycle))
    return directory


def test_lifecycle_faults_carry_the_lab_and_skip_alert_only_rounds(tmp_path):
    rounds = [
        _round(tmp_path, "w1", PI_POS, "op-1", LAB_A, 1),
        _round(tmp_path, "w2", "dv2-9f0fd3", "op-2", LAB_B, 1),  # alert only
    ]
    faults, labs = eval_dev_round.lifecycle_faults(rounds, _loaded())
    assert labs == {"w1": LAB_A, "w2": LAB_B}
    assert [(f["attempt_id"], f["lab"]) for f in faults] == [("op-1", LAB_A)]


def test_timeline_dataset_with_a_shared_scenario_id_is_a_conflict():
    with pytest.raises(eval_dev_round.DevRoundError) as err:
        eval_dev_round.timeline_scenarios(_loaded(), [DATASET])
    assert err.value.code.startswith("TIMELINE_SCENARIO_CONFLICT:")


# -- HolmesGPT: same rule, written into its round summary ---------------------------


def _jaeger_data(minute: int) -> str:
    trace = {
        "traceID": "t1",
        "spans": [
            {
                "operationName": "grpc.oteldemo.PaymentService/Charge",
                "processID": "p1",
                "startTime": int(_ts(minute) * 1e6),
                "tags": [{"key": "error", "type": "bool", "value": True}],
            }
        ],
        "processes": {"p1": {"serviceName": "payment"}},
    }
    return json.dumps({"status_code": 200, "body": {"data": [trace]}})


def _holmes_output(*, span_minute: int | None = 5, text: str = "") -> dict[str, Any]:
    calls = [
        {
            "tool_name": "execute_prometheus_range_query",
            "result": {
                "status": "success",
                "data": "{}",
                "params": {"start": _iso(0), "end": _iso(30)},
            },
        }
    ]
    if span_minute is not None:
        calls.append(
            {
                "tool_name": "jaeger_request",
                "result": {"status": "success", "data": _jaeger_data(span_minute)},
            }
        )
    if text:
        calls.append(
            {
                "tool_name": "jaeger_request",
                "result": {"status": "success", "data": f'["{text}"]'},
            }
        )
    return fake_output(tool_calls=calls)


def _holmes(tmp_path: Path, prior_lab: str, output: dict[str, Any]) -> Path:
    attempts = tmp_path / "attempts"
    write_attempt(attempts, HOLMES_SID, "h1", _ts(22), output=output)
    path = attempts / "h1" / ds.SUMMARY_FILE
    summary = json.loads(path.read_text())
    summary["round"] = "w-holmes"
    path.write_text(json.dumps(summary))
    _round(tmp_path, "w-prior", PI_POS, "op-prior", prior_lab, 1)
    _round(tmp_path, "w-holmes", HOLMES_SID, "op-h", LAB_A, 20)
    return attempts


def _ran(doc: dict[str, Any]) -> dict[str, Any]:
    (slot,) = [s for s in doc["slots"] if s["status"] == "ran"]
    return slot


def _rounds(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "rounds").iterdir())


def test_holmes_slot_gets_the_opspilot_rule_on_the_same_lab(tmp_path):
    attempts = _holmes(tmp_path, LAB_A, _holmes_output(text=_message()))
    slot = _ran(ds.summarize(DATASET, attempts, n=3, round_dirs=_rounds(tmp_path)))
    assert slot["lab"] == LAB_A
    assert slot["predecessor"]["attempt_id"] == "op-prior"
    assert slot["contamination"] == [
        f"PRIOR_FAULT_SPAN:{PI_POS}",
        f"PRIOR_INJECTION_TEXT:{PI_POS}",
    ]
    assert slot["metrics_overlap"] == [PI_POS]


def test_holmes_fault_on_another_lab_does_not_count(tmp_path):
    attempts = _holmes(tmp_path, LAB_B, _holmes_output(text=_message()))
    slot = _ran(ds.summarize(DATASET, attempts, n=3, round_dirs=_rounds(tmp_path)))
    assert slot["predecessor"] is None
    assert slot["contamination"] == []
    assert slot["metrics_overlap"] == []


def test_holmes_span_outside_the_prior_fault_interval_does_not_count(tmp_path):
    attempts = _holmes(tmp_path, LAB_A, _holmes_output(span_minute=25))
    slot = _ran(ds.summarize(DATASET, attempts, n=3, round_dirs=_rounds(tmp_path)))
    assert slot["contamination"] == []


def test_holmes_without_round_dirs_records_nothing_rather_than_clean(tmp_path):
    attempts = _holmes(tmp_path, LAB_A, _holmes_output(text=_message()))
    slot = _ran(ds.summarize(DATASET, attempts, n=3))
    assert slot["contamination"] is None
    assert slot["lab"] is None and slot["predecessor"] is None


def test_holmes_round_without_a_lifecycle_fails_closed(tmp_path):
    attempts = _holmes(tmp_path, LAB_A, _holmes_output())
    only_prior = [tmp_path / "rounds" / "w-prior"]
    with pytest.raises(ds.DatasetError) as err:
        ds.summarize(DATASET, attempts, n=3, round_dirs=only_prior)
    assert err.value.code == "ROUND_LIFECYCLE_MISSING:w-holmes"


def test_holmes_scores_are_unchanged_by_the_contamination_record(tmp_path):
    attempts = _holmes(tmp_path, LAB_A, _holmes_output(text=_message()))
    plain = ds.summarize(DATASET, attempts, n=3)
    recorded = ds.summarize(DATASET, attempts, n=3, round_dirs=_rounds(tmp_path))
    fields = {"lab", "predecessor", "contamination", "metrics_overlap"}

    def strip(doc: dict[str, Any]) -> dict[str, Any]:
        slots = [{k: v for k, v in s.items() if k not in fields} for s in doc["slots"]]
        return {**doc, "slots": slots}

    assert strip(plain) == strip(recorded)


def test_holmes_views_read_full_jaeger_traces_and_keep_projections_as_text():
    projected = '[{"traceID": "x", "errs": ["Charge"]}]'
    output = {
        "tool_calls": [
            {"tool_name": "jaeger_request", "result": {"data": _jaeger_data(5)}},
            {"tool_name": "jaeger_request", "result": {"data": projected}},
        ]
    }
    full, proj = ds.holmes_views(output)
    assert full["rows"] == [
        {
            "service": "payment",
            "operation": "grpc.oteldemo.PaymentService/Charge",
            "start_us": int(_ts(5) * 1e6),
            "error": True,
        }
    ]
    assert proj["rows"] == [] and proj["text"] == projected


# -- comparison: one sensitivity row, main numbers unchanged ------------------------


def _item(attempt_id: str, system: str, sid: str, window: str, flags, ok=True):
    return {
        "attempt_id": attempt_id,
        "system": system,
        "scenario_id": sid,
        "slot": 1,
        "judge_applicable": False,
        "verdicts": {},
        "checks": {"final_state": ok},
        "window": window,
        "contamination": flags,
    }


ITEMS = [
    _item("op-1", "opspilot", "s1", "w1", ["PRIOR_FAULT_SPAN:x"], ok=False),
    _item("h-1", "holmes", "s1", "w1", []),
    _item("op-2", "opspilot", "s2", "w2", []),
    _item("h-2", "holmes", "s2", "w2", None),
    _item("op-3", "opspilot", "s3", "w3", [], ok=False),
]


def test_sensitivity_drops_flagged_windows_for_both_systems():
    row = compare.contamination_sensitivity(ITEMS, {}, k=3)
    assert row["flagged_attempts"] == ["op-1"]
    assert row["flagged_windows"] == ["w1"]
    assert row["excluded"] == {"opspilot": 1, "holmes": 1}
    assert row["unrecorded"] == ["h-2"]
    assert row["systems"]["opspilot"]["n_attempts"] == 2
    assert row["systems"]["opspilot"]["passed"] == 1
    assert row["systems"]["holmes"]["n_attempts"] == 1


def test_sensitivity_reports_a_system_whose_every_slot_was_excluded():
    items = [
        _item("op-1", "opspilot", "s1", "w1", ["PRIOR_FAULT_SPAN:x"]),
        _item("h-1", "holmes", "s1", "w1", []),
    ]
    row = compare.contamination_sensitivity(items, {}, k=3)
    empty = {
        "n_attempts": 0,
        "passed": 0,
        "pass_rate": None,
        "pass_rate_interval": None,
    }
    assert row["systems"] == {"opspilot": empty, "holmes": empty}
    assert row["comparison"]["systems"] == {"opspilot": empty, "holmes": empty}


def test_sensitivity_ignores_voided_attempts():
    row = compare.contamination_sensitivity(ITEMS, {}, k=3, voided={"op-1"})
    assert row["flagged_windows"] == []
    assert row["excluded"] == {"holmes": 0, "opspilot": 0}


def test_summarize_output_keeps_every_main_number(tmp_path):
    items = tmp_path / "items.json"
    items.write_text(
        json.dumps(
            {
                "schema": compare.ITEMS_SCHEMA,
                "k": 3,
                "judge_config": {},
                "voided": [],
                "attempts": ITEMS,
            }
        )
    )
    out = tmp_path / "summary.json"
    compare.run_summarize(
        argparse.Namespace(items=str(items), rulings=None, out=str(out))
    )
    doc = json.loads(out.read_text())
    plain = compare.summarize(ITEMS, {}, k=3)
    for key, value in plain.items():
        assert doc[key] == value, key
    assert doc["contamination_sensitivity"]["flagged_windows"] == ["w1"]


def test_holmes_relative_range_start_is_resolved_against_its_end():
    absolute = ds._range_window({"start": "-600", "end": _iso(10)})
    assert absolute == {"start": _iso(0), "end": _iso(10)}
    unresolved = ds._range_window({"start": "-600", "end": None})
    assert unresolved == {"start": "-600", "end": None}
    iso = ds._range_window({"start": _iso(1), "end": _iso(2)})
    assert iso == {"start": _iso(1), "end": _iso(2)}
