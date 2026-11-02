"""#428 LangSmith read-back script, offline: fake API responses, exit codes,
and the key never printed."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from opspilot.outbox_delivery import root_span_id_for, span_id_for
from scripts import outbox_trace_check as check

KEY = "lsv2_" + "pt_" + "abad1dea" * 4  # built at run time (gitleaks)
TRACE = "0f0f0f0f-1111-2222-3333-444444444444"


def _run(run_id, name, run_type, meta, **fields):
    data = {
        "id": str(run_id),
        "name": name,
        "run_type": run_type,
        "extra": {"metadata": meta},
        "inputs": {},
        "outputs": None,
        **fields,
    }
    return SimpleNamespace(model_dump=lambda mode=None: dict(data))


@pytest.fixture
def world(tmp_path, monkeypatch):
    run, incident = uuid4(), str(uuid4())
    rows = [
        {
            "kind": "model_step",
            "idempotency_key": f"model_step:{run}:1",
            "run_id": str(run),
        },
        {
            "kind": "run_state",
            "idempotency_key": f"run_state:{run}:1",
            "run_id": str(run),
        },
    ]
    root = _run(
        check.run_uuid(root_span_id_for(run)),
        "opspilot.run",
        "chain",
        {"incident_id": incident, "run_state": "completed"},
        total_cost=0.001,
        total_tokens=12,
        # LangSmith's trace id differs from the derived root run id here, so a
        # listing keyed by the wrong id finds nothing (bot P2 on #441)
        trace_id=TRACE,
    )
    children = [
        _run(
            check.run_uuid(span_id_for(rows[0]["idempotency_key"])),
            "opspilot.model_step",
            "llm",
            {
                "incident_id": incident,
                "ls_provider": "deepseek",
                "ls_model_name": "deepseek-v4-flash",
                "image_revision": "abc",
            },
            prompt_tokens=10,
            completion_tokens=2,
            total_tokens=12,
        ),
        _run(
            check.run_uuid(span_id_for(rows[1]["idempotency_key"])),
            "opspilot.run_state.completed",
            "chain",
            {"incident_id": incident, "run_state": "completed"},
        ),
    ]
    state = SimpleNamespace(
        root=root,
        children=children,
        root_missing=False,
        link_fails=False,
        trace_queries=[],
    )

    class Client:
        def list_runs(self, trace_id=None, **_):
            runs = [state.root, *state.children]
            if trace_id is not None:
                state.trace_queries.append(str(trace_id))
                return runs if str(trace_id) == TRACE else []
            return runs

        def read_run(self, _id):
            if state.root_missing:
                raise LookupError("404")
            return state.root

        def get_run_url(self, **_):
            if state.link_fails:
                raise RuntimeError("no url")
            return "https://smith.langchain.com/o/x/projects/p/r/y"

    monkeypatch.setattr(check, "langsmith_client", lambda env: Client())
    env_file = tmp_path / "worker_env"
    env_file.write_text(
        f"LANGSMITH_API_KEY={KEY}\nLANGSMITH_PROJECT=opspilot-lab-428\nDEEPSEEK_API_KEY=other\n"
    )
    rows_file = tmp_path / "rows.json"
    rows_file.write_text(json.dumps(rows))
    args = [
        "--env-file",
        str(env_file),
        "--rows",
        str(rows_file),
        "--incident-id",
        incident,
        "--run-id",
        str(run),
        "--image-revision",
        "abc",
    ]
    state.args = args
    return state


def _main(world, capsys, *extra):
    code = check.main([*world.args, *extra])
    out = capsys.readouterr()
    assert KEY not in out.out + out.err
    return code, json.loads(out.out)


def test_a_complete_clean_trace_passes(world, capsys):
    code, report = _main(world, capsys)
    assert code == 0 and report["failures"] == []
    # the trace is listed by the root run's own trace_id, not its run id
    assert world.trace_queries == [TRACE]
    assert report["expected"]["trace_key"] == TRACE
    assert report["expected"]["root_run_id"] != TRACE
    assert report["identity"] == {
        **report["identity"],
        "found": 2,
        "missing": 0,
        "duplicated": 0,
    }


def test_a_missing_root_fails(world, capsys):
    world.root_missing = True
    code, report = _main(world, capsys)
    assert code == 1 and "ROOT_MISSING" in report["failures"]


def test_a_missing_span_fails(world, capsys):
    world.children.pop(0)
    code, report = _main(world, capsys)
    assert code == 1 and "SPANS_MISSING" in report["failures"]


def test_a_duplicated_span_fails_unless_allowed(world, capsys):
    clone = world.children[1].model_dump()
    world.children.append(
        SimpleNamespace(model_dump=lambda mode=None: {**clone, "id": str(uuid4())})
    )
    code, report = _main(world, capsys)
    assert code == 1 and report["failures"] == ["SPANS_DUPLICATED"]
    code, report = _main(world, capsys, "--allow-duplicates")
    assert code == 0 and report["identity"]["duplicated"] == 1


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"extra": {"metadata": {"query": "up"}}}, "METADATA_OUTSIDE_ALLOWLIST"),
        ({"inputs": {"messages": ["hi"]}}, "CONTENT_PRESENT"),
        ({"outputs": {"reasoning_content": "x"}}, "FORBIDDEN_CONTENT"),
        ({"error": "x-api-key " + KEY}, "FORBIDDEN_CONTENT"),
    ],
)
def test_content_outside_the_allowlist_fails(world, capsys, change, code):
    base = world.children[1].model_dump()
    world.children[1] = SimpleNamespace(model_dump=lambda mode=None: {**base, **change})
    status, report = _main(world, capsys)
    assert status == 1 and code in report["failures"]
    assert report["content_scan"].get("api_key_literal", 0) == (
        1 if KEY in json.dumps(change) else 0
    )


def test_a_root_without_a_trace_link_fails(world, capsys):
    world.link_fails = True
    code, report = _main(world, capsys)
    assert code == 1 and report["failures"] == ["ROOT_LINK_MISSING"]
    assert report["root"]["found"] and report["root"]["link"] is None


def test_the_metadata_filter_uses_the_documented_key_value_form():
    # LangSmith trace-query-syntax "Filter by metadata key or value";
    # same form as scripts/lab_evidence.py find_root_run
    assert check._metadata_filter({"incident_id": "abc"}) == (
        'and(eq(metadata_key, "incident_id"), eq(metadata_value, "abc"))'
    )
    assert check._metadata_filter({"incident_id": "abc", "image_revision": "f00"}) == (
        'and(and(eq(metadata_key, "incident_id"), eq(metadata_value, "abc")), '
        'and(eq(metadata_key, "image_revision"), eq(metadata_value, "f00")))'
    )
    assert check._metadata_filter({"incident_id": 'a"b'}) == (
        'and(eq(metadata_key, "incident_id"), eq(metadata_value, "a\\"b"))'
    )
