"""Structure contract with the pinned official evaluator (ITBench-Evaluations@14f026fc).

Runs only when ``ITBENCH_EVALUATIONS_DIR`` points at a checkout of the pin with
its own ``.venv`` (outside the repo; CI skips).  Three synthetic outputs (no
report, entities, propagation chain) written by ``answer.write_trial`` go
through the evaluator's own loader and judge-prompt builder.  No judge call and
no model request: the OpenAI client is constructed but never used.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts.itbench_lite import answer, official_eval

EVALUATOR = os.environ.get(official_eval.EVALUATOR_DIR_ENV)

pytestmark = pytest.mark.skipif(
    not EVALUATOR,
    reason=f"{official_eval.EVALUATOR_DIR_ENV} not set (evaluator lives outside the repo)",
)

VOCAB = ["alpha", "beta", "gamma"]

PROBE = r"""
import json, sys
from itbench_evaluations.loader import load_agent_outputs_sync
from itbench_evaluations.agent import LAAJAgent
from itbench_evaluations.prompts import main as p_main, entity_correctness as p_ent, semantic_grouping as p_sem

root, incident = sys.argv[1], sys.argv[2]
outputs, bad = load_agent_outputs_sync(root, incident)
gt = {"id": incident, "groups": [{"id": "x", "kind": "Service", "filter": ["alpha"], "root_cause": True}],
      "aliases": [], "propagations": []}
agent = LAAJAgent(model="unused", domain="sre")
round_trip = []
for trial in outputs:
    prompt = agent._build_user_prompt(gt, trial["output"], None, ["ROOT_CAUSE_ENTITY"])
    block = prompt.split("## Generated Response:\n```json\n", 1)[1].split("\n```", 1)[0]
    round_trip.append(json.loads(block) == trial["output"])
print(json.dumps({
    "bad_runs": bad,
    "trials": [{"trial": t["trial"], "keys": sorted(t["output"]),
                "entities": len(t["output"]["entities"]),
                "root_causes": sum(1 for e in t["output"]["entities"] if e["contributing_factor"] is True),
                "propagations": len(t["output"]["propagations"])} for t in outputs],
    "prompt_round_trip": round_trip,
    "judge_reads": {
        "name": "`name` field" in p_main.LAAJ_SYSTEM_PROMPT,
        "contributing_factor": "contributing_factor" in p_ent.ROOT_CAUSE_ENTITY_PROMPT,
        "propagations": "`propagations` array from the `Generated Response`" in p_sem.SEMANTIC_GROUPING_PROMPT,
    },
}))
"""


def _claim(text):
    return {
        "kind": "fact",
        "text": text,
        "evidence_ids": ["ev-1"],
        "target_refs": ["t"],
        "time_scope_ref": "w",
    }


def _report(summary, claims):
    return {
        "schema_version": "m0-report-v2",
        "assessment_status": "completed",
        "conclusion": "supported",
        "summary": summary,
        "claims": claims,
        "gaps": [],
        "next_steps": [],
    }


def test_synthetic_outputs_load_and_parse_in_pinned_evaluator(tmp_path):
    directory = Path(EVALUATOR).expanduser()
    official_eval.check_pinned(directory)
    evidence = [
        {
            "evidence_id": "ev-1",
            "operation": {"tool": "traces_search", "query": '{"service":"alpha"}'},
        }
    ]
    cases = {
        1: answer.convert(None, [], VOCAB),
        2: answer.convert(
            _report(
                "alpha is failing; beta sees errors.",
                [_claim("alpha 5xx"), _claim("beta latency")],
            ),
            evidence,
            VOCAB,
        ),
        3: answer.convert(
            _report("gamma -> alpha -> beta.", [_claim("alpha 5xx")]), evidence, VOCAB
        ),
    }
    outputs = tmp_path / "outputs"
    for trial, conversion in cases.items():
        answer.write_trial(outputs, "Scenario-901", trial, conversion)
    (
        outputs / "Scenario-901" / "4"
    ).mkdir()  # a trial with no file: the evaluator calls it a bad run

    work = tmp_path / "work"
    work.mkdir()
    completed = subprocess.run(
        [
            str(official_eval.evaluator_python(directory)),
            "-c",
            PROBE,
            str(outputs),
            "901",
        ],
        cwd=work,
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")},
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    print(json.dumps(result, indent=2))

    keys = ["alerts_explained", "entities", "propagations"]
    assert result["bad_runs"] == 1
    assert result["trials"] == [
        {"trial": 1, "keys": keys, "entities": 0, "root_causes": 0, "propagations": 0},
        {"trial": 2, "keys": keys, "entities": 2, "root_causes": 1, "propagations": 0},
        {"trial": 3, "keys": keys, "entities": 3, "root_causes": 1, "propagations": 2},
    ]
    assert result["prompt_round_trip"] == [True, True, True]
    assert result["judge_reads"] == {
        "name": True,
        "contributing_factor": True,
        "propagations": True,
    }
