"""The Cloud-OpsBench tool channel's two guards (#871 U1, A2).

The evaluation tool family is a new model-input channel: benchmark snapshot
text reaches the model as tool views. Two promises hold for it:

* credentials in that text never reach the model view or the stored
  evidence -- the calls go through the product's ``ReadOnlyToolExecutor``,
  whose shared redaction covers them (killed with M67's executor edit);
* the snapshot view the benchmark's server reads never holds an answer
  file (A2; mutation M68): ``metadata.json`` is not copied, and an
  answer-named entry below the snapshot refuses the case.

The MCP server is replaced by a double at the client seam; everything from
the profile's executor factory inward is the shipped composition.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from opspilot.persistence import Lease
from opspilot.tools import ToolRequest
from scripts.cloudopsbench_eval import catalog, views
from scripts.cloudopsbench_eval import profile as cob
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.safety.gateway_support import NOW, _Store, everything_seen
from tests.safety.test_safety_tool_gateway import CANARIES, SECRET_VALUES

CANARY_TEXT = "\n".join(CANARIES.values())


class FakeMcp:
    def __init__(self, text: str, is_error: bool = False) -> None:
        self.text, self.is_error = text, is_error
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name, arguments, timeout):
        self.calls.append((name, dict(arguments)))
        return self.text, self.is_error


def _minimal_args(tool: dict) -> dict:
    """One valid value per required argument, from the benchmark schema."""
    args = {}
    schema = tool["inputSchema"]
    for name in schema.get("required") or ():
        spec = schema["properties"][name]
        kind = cob._kind(spec)
        args[name] = (
            spec["enum"][0] if "enum" in spec else 80 if kind == "integer" else "x-name"
        )
    return args


def _executor(client, system="boutique", category="runtime"):
    lease = Lease(
        incident_id=uuid4(),
        run_id=uuid4(),
        owner=uuid4(),
        epoch=1,
        control_generation=3,
        global_suspension_generation=1,
        target_suspension_generation=2,
    )
    clock = FakeClock(start=NOW)
    sink = RecordingSink()
    face = cob.face(clock, system, category)
    run_input = face.input_for(
        run_id=str(lease.run_id),
        question="q",
        target_id=cob.TARGET_ID,
        deadline=NOW + timedelta(minutes=10),
        model_requests=8,
    )
    factory = cob.executor_factory(
        _Store(lease),
        evidence=sink,
        clock=clock,
        system=system,
        category=category,
        mcp_url="http://127.0.0.1:18951/mcp",
        token_counter=lambda text: len(text) // 4,
        client=client,
    )
    window = run_input.evidence_context["time_policies"][0]["window"]
    return factory(lease, run_input), sink, window


def _call(executor, name, params, window):
    return executor.execute(
        ToolRequest(
            step_id="safety-step",
            tool_index=0,
            tool_name=name,
            target_ref=cob.TARGET_ID,
            params=params,
            window=window,
        )
    )


@pytest.mark.parametrize(
    ("system", "category"),
    [("boutique", "runtime"), ("boutique", "codedefect"), ("train-ticket", "service")],
)
def test_credentials_in_snapshot_text_never_reach_the_model_or_evidence(
    system, category
):
    for tool in catalog.benchmark_tools(system, category):
        client = FakeMcp(CANARY_TEXT)
        executor, sink, window = _executor(client, system, category)
        name = catalog.TOOL_NAMES[tool["name"]]
        outcome = _call(executor, name, _minimal_args(tool), window)
        assert client.calls == [(tool["name"], _minimal_args(tool))]
        assert outcome.status == "ok", (name, outcome.model_view)
        seen = everything_seen(outcome, sink)
        for value in SECRET_VALUES:
            assert value not in seen, (name, value)


def test_a_server_error_echoing_credentials_is_redacted():
    client = FakeMcp("Invalid tool arguments: " + CANARY_TEXT, is_error=True)
    executor, sink, window = _executor(client)
    outcome = _call(executor, "get_alerts", {}, window)
    assert outcome.status == "error"
    seen = everything_seen(outcome, sink)
    for value in SECRET_VALUES:
        assert value not in seen, value


def _case(tmp_path):
    case = tmp_path / "benchmark" / "boutique" / "runtime" / "1"
    (case / "raw_data").mkdir(parents=True)
    (case / "code" / "cartservice").mkdir(parents=True)
    (case / "tool_cache.json").write_text("{}")
    (case / "raw_data" / "alert.json").write_text("{}")
    (case / "code" / "cartservice" / "a.cs").write_text("class A {}")
    (case / "metadata.json").write_text('{"result": {"root_cause": "x"}}')
    return case


def test_the_snapshot_view_never_holds_an_answer_file(tmp_path):
    case = _case(tmp_path)
    view = tmp_path / "view"
    views.build_view(case, view)
    names = {p.relative_to(view).as_posix() for p in view.rglob("*")}
    assert names == {
        "tool_cache.json",
        "raw_data",
        "raw_data/alert.json",
        "code",
        "code/cartservice",
        "code/cartservice/a.cs",
    }


@pytest.mark.parametrize("planted", ["raw_data/milestone.json", "code/metadata.json"])
def test_an_answer_named_entry_below_the_snapshot_refuses_the_case(tmp_path, planted):
    case = _case(tmp_path)
    (case / planted).write_text("{}")
    with pytest.raises(views.ViewError) as error:
        views.build_view(case, tmp_path / "view")
    assert error.value.code == "SNAPSHOT_HAS_ANSWER_NAME"


def test_a_link_in_the_snapshot_refuses_the_case(tmp_path):
    case = _case(tmp_path)
    (case / "raw_data" / "peek.json").symlink_to(case / "metadata.json")
    with pytest.raises(views.ViewError) as error:
        views.build_view(case, tmp_path / "view")
    assert error.value.code == "SNAPSHOT_HAS_LINK"
