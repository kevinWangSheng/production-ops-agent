"""Existing Holmes CLI/parser path with synthetic bundles and a fake pipe only."""

import base64
import copy
import hashlib
import json
import os
import runpy
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import dotenv  # noqa: E402

from scripts.m0_environment import holmes_baseline as wrapper  # noqa: E402
from scripts.m0_environment.round02 import save  # noqa: E402

wrapper.UPSTREAM = Path(os.environ["HOLMES_TEST_UPSTREAM"])
clock_case = runpy.run_path(str(ROOT / "tests/test_m0_initial_evidence.py"))[
    "clock_case"
]
metadata = (
    ROOT / "docs/evidence/m0-real-environment/round-02-provider-models.json"
).read_bytes()


def run_case(raw_state, mutation, accepted):
    with tempfile.TemporaryDirectory() as directory:
        task = Path(directory)
        source = task / "source"
        source.mkdir()

        def raw_change(raw):
            for field in ("operation_started_at", "collection_completed_at"):
                if raw_state == "missing":
                    raw.pop(field)
                elif raw_state == "null":
                    raw[field] = None

        def timing_change(timing):
            if raw_state != "known":
                timing["operation_started_at"] = None
                timing["collection_completed_at"] = None

        entry, context, scope = clock_case(source, raw_change, timing_change)
        bundle = source / "verified-bundle.json"
        save(
            bundle,
            {
                "schema_version": "m0-initial-evidence-v1",
                "projection_context": context.model_dump(mode="json"),
                "entries": [entry],
            },
        )
        scope_path = task / "scope.json"
        registry = task / "registry.json"
        save(registry, context.registry)
        scope.update(
            metrics_scope="integration",
            control_generation=0,
            deployment_registry_file=str(registry),
        )
        save(scope_path, scope)
        question = task / "question.json"
        save(question, {"run_id": "echo", "request": "Review preserved evidence."})
        override = {
            "producer-e1": {
                "view_hash": wrapper.canonical_hash(
                    json.loads((source / "view.json").read_text())
                ),
                "timing": copy.deepcopy(entry["timing"]),
            }
        }
        if mutation == "invent":
            override["producer-e1"]["timing"]["collection_completed_at"] = (
                "2026-09-10T01:01:03Z"
            )
        elif mutation == "erase":
            override["producer-e1"]["timing"]["collection_completed_at"] = None
        elif mutation == "equivalent":
            override["producer-e1"]["timing"]["collection_completed_at"] = (
                "2026-09-10T02:01:02+01:00"
            )
        elif mutation == "source-bounds":
            override["producer-e1"]["timing"].update(
                source_time_basis="unknown",
                source_start_at="2026-09-10T01:00:30Z",
                source_end_at="2026-09-10T01:00:30Z",
            )
        elif mutation == "unknown-id":
            override["other-id"] = copy.deepcopy(override["producer-e1"])
        override_path = task / "override.json"
        save(override_path, override)
        source_hashes = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in source.glob("*.json")
        }
        meta = task / "docs/evidence/m0-real-environment/round-02-provider-models.json"
        meta.parent.mkdir(parents=True)
        meta.write_bytes(metadata)
        (task / "tmp/m0-environment").mkdir(parents=True)

        def fake_child(command, data, wall_seconds):
            packet = json.loads(data)
            assert "api.deepseek.com" in packet["url"]
            report = {
                "schema_version": "m0-report-v2",
                "assessment_status": "incomplete",
                "conclusion": "inconclusive",
                "summary": "No qualified current facts.",
                "claims": [],
                "gaps": ["No time policy supplied."],
                "next_steps": [],
            }
            response = {
                "id": "fake",
                "object": "chat.completion",
                "created": 1789002060,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(report),
                            "reasoning_content": "synthetic-private",
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "prompt_cache_hit_tokens": 0,
                    "prompt_cache_miss_tokens": 100,
                },
            }
            return json.dumps(
                {
                    "status": 200,
                    "headers": {},
                    "body": base64.b64encode(json.dumps(response).encode()).decode(),
                    "complete": True,
                }
            ).encode()

        argv = [
            "wrapper",
            "--run-id",
            "echo",
            "--phase",
            "report",
            "--max-steps",
            "1",
            "--scope-file",
            str(scope_path),
            "--question-file",
            str(question),
            "--initial-evidence-manifest",
            str(bundle),
        ]
        if mutation != "no-flag":
            argv.extend(["--initial-timings-file", str(override_path)])
        rejected = False
        with (
            patch.object(wrapper, "ROOT", task),
            patch.object(wrapper, "run_child", side_effect=fake_child) as transport,
            patch.object(
                dotenv, "dotenv_values", return_value={"DEEPSEEK_API_KEY": "synthetic"}
            ) as credentials,
            patch.object(sys, "argv", argv),
        ):
            try:
                wrapper.main()
            except ValueError:
                rejected = True
        assert rejected is not accepted, (raw_state, mutation, "override gate verdict")
        if not accepted:
            assert transport.call_count == credentials.call_count == 0
        else:
            assert transport.call_count == credentials.call_count == 1
            folder = task / "tmp/m0-environment/holmes-runs/echo"
            recorded = json.loads((folder / "evidence-timings.json").read_text())[
                "producer-e1"
            ]["timing"]
            from scripts.m0.outcomes_v4 import Timing

            original = Timing.model_validate_json(
                json.dumps(entry["timing"])
            ).model_dump(mode="json")
            assert recorded == original, (
                "matching echo must never replace the verified timing"
            )
        assert source_hashes == {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in source.glob("*.json")
        }
        print(
            json.dumps(
                {
                    "raw_state": raw_state,
                    "override": mutation,
                    "accepted_echo": accepted,
                    "fake_transports": transport.call_count,
                    "credential_reads": credentials.call_count,
                    "real_http": 0,
                    "original_bundle_unchanged": True,
                }
            )
        )


for state, mutation, accepted in [
    ("missing", "invent", False),
    ("null", "invent", False),
    ("known", "invent", False),
    ("known", "erase", False),
    ("missing", "match", True),
    ("null", "match", True),
    ("known", "match", True),
    ("known", "equivalent", True),
    ("known", "source-bounds", False),
    ("known", "unknown-id", False),
    ("known", "no-flag", True),
]:
    run_case(state, mutation, accepted)
