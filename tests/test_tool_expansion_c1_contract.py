"""Independent contract tests for tool expansion C1 (#565): the investigation
task list tool ``TodoWrite``.

Written from the C1 task record (docs/tasks/2026-11-20-tool-expansion-c1.md,
"公开接口" and "合同测试要覆盖的行为"), the decision table r11 (C1-U1=a: the
investigation loop handles it, not the read-only gateway; no evidence id; a
report cannot cite it; pause and cancel apply as usual) and the C1
pre-review, without reading the implementation. Red until it lands;
collection must stay green.

Baseline note: C2b (PR #741: ``knowledge=``, the knowledge tool and its
prompt-revision key) and #722 (PR #725: ``OPSPILOT_CLUSTER_NAME`` and the
``replay-candidate-k8s-cluster`` variant) are not on main at the time of
writing (origin/main a8cf46d9 + interface 725a70c6). The interface places
``TodoWrite`` after the knowledge tool and composes the ``-todo`` suffix after
``-cluster``. So nothing here pins a value main does not have yet: the
switch-off surface is compared with the pins the B3 contract file keeps for
the same backend sets (whoever changes main maintains them), and every
switch-on property is relative ("``TodoWrite`` is the last tool", "off is
byte-identical to not passing ``todo``"). Combinations with the knowledge
switch or the cluster name skip only while the base lacks C2b / #722 (the
guard reads the base, never the face under test).

Seams assumed beyond the interface (each also marked ``# ASSUMPTION:``):

* ``todo_write_view`` takes the already parsed arguments (a mapping), as the
  executor's ``ToolRequest.params`` does.
* ``counts`` always carries all four status keys, zeros included.
* ``TODO_GUIDANCE`` is the segment text (a ``str``) or a segment with a
  ``text`` attribute.

Loop tests drive the real ``InvestigationLoop`` with a scripted model, the
in-memory step store and the repository's executor doubles. No network, no
PostgreSQL, no container; the clock is fixed.
"""

from __future__ import annotations

import inspect
import json
import subprocess
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from opspilot.investigation.context import initial_messages
from opspilot.investigation.loop import InvestigationLoop, InvestigationRequest
from opspilot.investigation.store import MemoryStepStore
from opspilot.tools import TransportResponse
from opspilot.tools.profiles import PROFILE_ENV, ToolProfile, select_profile
from opspilot.tools.registry import canonical
from tests.m1_investigation_support import (
    TOOL_SCHEMAS as METRICS_SCHEMAS,
)
from tests.m1_investigation_support import (
    ScriptedModel,
    reply,
    report_json,
    tool_call,
)
from tests.m1_tool_support import (
    NOW,
    WINDOW_START,
    FakeClock,
    RecordingLedger,
    body,
    build,
    historical_window_context,
)

ROOT = Path(__file__).resolve().parents[1]
EXT_PROFILE = "otel-demo-ext"
SWITCH = "OPSPILOT_TODO_WRITE"
TOOL = "TodoWrite"
METRICS = "metrics.range_query"
METRICS_ARGS = '{"expr":"rate(http_errors[5m])"}'
RUN_ID = "b3-run"  # the run id the B3 surface pins were computed with
KNOWLEDGE_SWITCH = "OPSPILOT_KNOWLEDGE_TOOL"  # C2b
CLUSTER_ENV = "OPSPILOT_CLUSTER_NAME"  # #722
OFF_VALUES = [None, "", "0"]
INVALID_VALUES = ["on", "true", "yes", "2", "01", " 1", "1 ", "TRUE", "-1", "1.0"]

#: Pre-review C1-8, the description the interface adopts verbatim.
DESCRIPTION = (
    "Save this investigation's task list to break a multi-step investigation "
    "into concrete sub-tasks and track their status. Always send the COMPLETE "
    "list, not only the tasks that changed; each call replaces the previous "
    "list. The list is shown to the operator as progress. It is not evidence: "
    "it has no evidence_id and cannot be cited. A list that breaks the limits "
    "(50 tasks, 500 characters per task, unique ids) is refused whole with "
    "INVALID_TODOS and the previous list stays."
)
STATUSES = ("pending", "in_progress", "completed", "failed")
# The same value, joined so the literal is not a secret-scanner hit.
SECRET = "-".join(("hunter2", "c1", "565"))


@pytest.fixture(autouse=True)
def _clean_switches(monkeypatch):
    # The loop must decide from the Run input alone: no switch in the process
    # environment unless a test sets one on purpose.
    for name in (SWITCH, KNOWLEDGE_SWITCH, CLUSTER_ENV):
        monkeypatch.delenv(name, raising=False)


def _todo():
    from opspilot.investigation import todo

    return todo


def _ext():
    from opspilot.tools import otel_demo_ext

    return otel_demo_ext


def _items(*statuses: str, prefix: str = "t") -> list[dict[str, str]]:
    return [
        {"id": f"{prefix}{i}", "content": f"Check step {i}", "status": status}
        for i, status in enumerate(statuses, start=1)
    ]


def _args(todos: object) -> dict[str, object]:
    return {"todos": todos}


def _view(arguments: object, operation_id: str = "op-c1-0") -> Mapping[str, Any]:
    # ASSUMPTION: the parsed arguments, as ToolRequest.params carries them.
    return _todo().todo_write_view(operation_id, arguments)


# -- 1. schema, switch and the pure view ------------------------------------------


def test_module_constants():
    todo = _todo()
    assert todo.TOOL_NAME == TOOL
    assert todo.ENV == SWITCH


def test_schema_follows_upstream_fields_and_status_enum():
    schema = _todo().TODO_WRITE_SCHEMA
    assert schema["type"] == "function"
    function = schema["function"]
    assert function["name"] == TOOL
    assert function["description"] == DESCRIPTION
    params = function["parameters"]
    assert params["type"] == "object"
    assert set(params["properties"]) == {"todos"}
    assert params["required"] == ["todos"]
    todos = params["properties"]["todos"]
    assert todos["type"] == "array"
    item = todos["items"]
    assert item["type"] == "object"
    assert set(item["properties"]) == {"id", "content", "status"}
    assert sorted(item["required"]) == ["content", "id", "status"]
    assert item["properties"]["id"]["type"] == "string"
    assert item["properties"]["content"]["type"] == "string"
    assert list(item["properties"]["status"]["enum"]) == list(STATUSES)
    assert "priority" not in json.dumps(schema)  # upstream prose only, not schema
    assert "subsequent prompts" not in function["description"]


@pytest.mark.parametrize("value", OFF_VALUES)
def test_switch_off_values(value):
    env = {} if value is None else {SWITCH: value}
    assert _todo().todo_write_enabled(env) is False


def test_switch_value_1_is_on():
    assert _todo().todo_write_enabled({SWITCH: "1"}) is True


@pytest.mark.parametrize("value", INVALID_VALUES)
def test_an_invalid_switch_value_is_a_startup_error(value):
    with pytest.raises(SystemExit, match="TODO_WRITE_INVALID"):
        _todo().todo_write_enabled({SWITCH: value})


def test_an_accepted_list_returns_the_documented_view():
    todos = _items("completed", "in_progress", "pending")
    view = _view(_args(todos), "op-a")
    assert dict(view) == {
        "operation_id": "op-a",
        "tool": TOOL,
        "status": "ok",
        "adopted": False,
        "citable_as_fact": False,
        "todos": todos,  # the model's order, not re-sorted by status
        # ASSUMPTION: all four keys, zeros included.
        "counts": {"pending": 1, "in_progress": 1, "completed": 1, "failed": 0},
        "message": (
            "Task list saved: 3 tasks. This replaces the previous list. It is "
            "your plan, not evidence; it has no evidence_id."
        ),
    }


def test_the_view_carries_no_evidence_fields():
    for view in (_view(_args(_items("pending"))), _view(_args("not a list"))):
        for key in ("evidence_id", "source", "target_id", "window"):
            assert key not in view, (key, view)


def test_every_status_is_counted():
    view = _view(_args(_items(*STATUSES, "completed")))
    assert view["counts"] == {
        "pending": 1,
        "in_progress": 1,
        "completed": 2,
        "failed": 1,
    }


def test_the_view_is_a_pure_function_of_its_arguments():
    args = _args(_items("pending", "completed"))
    first = canonical(dict(_view(args, "op-p")))
    again = canonical(dict(_view(json.loads(json.dumps(args)), "op-p")))
    assert first == again  # no timestamp, no counter
    assert _view(args, "op-q")["operation_id"] == "op-q"


def _limits_ok() -> list[object]:
    return [
        _args(_items(*["pending"] * 50)),
        _args([{"id": "x" * 64, "content": "c" * 500, "status": "pending"}]),
        # Ten 500-character multi-byte tasks: about 15 KiB of canonical JSON.
        _args(
            [
                {"id": f"m{i}", "content": "中" * 500, "status": "pending"}
                for i in range(10)
            ]
        ),
    ]


@pytest.mark.parametrize(
    "arguments", _limits_ok(), ids=["50-tasks", "max-fields", "15KiB"]
)
def test_lists_at_the_limits_are_accepted(arguments):
    view = _view(arguments)
    assert view["status"] == "ok", view
    assert len(view["todos"]) == len(arguments["todos"])


def _over(**item: object) -> dict[str, object]:
    base = {"id": "t1", "content": "Check checkout", "status": "pending"}
    base.update(item)
    return _args([{k: v for k, v in base.items() if v is not _MISSING}])


_MISSING = object()

INVALID_ARGUMENTS = {
    "51-tasks": _args(_items(*["pending"] * 51)),
    "duplicate-id": _args(
        [
            {"id": "dup", "content": "a", "status": "pending"},
            {"id": "dup", "content": "b", "status": "completed"},
        ]
    ),
    "content-501": _over(content="c" * 501),
    "content-empty": _over(content=""),
    "content-control-char": _over(content="bell\x07here"),
    "id-65": _over(id="x" * 65),
    "id-empty": _over(id=""),
    "id-not-string": _over(id=7),
    "content-not-string": _over(content=["a"]),
    "status-unknown": _over(status="done"),
    "status-priority-word": _over(status="high"),
    "missing-id": _over(id=_MISSING),
    "missing-content": _over(content=_MISSING),
    "missing-status": _over(status=_MISSING),
    "item-not-object": _args(["Check checkout"]),
    "todos-not-array": _args({"id": "t1", "content": "a", "status": "pending"}),
    "todos-string": _args("t1: check checkout"),
    "todos-missing": {},
    "arguments-not-object": ["t1"],
    # 22 multi-byte tasks of 500 characters: every task is valid, the whole
    # canonical JSON is over 32 KiB.
    "over-32KiB": _args(
        [{"id": f"m{i}", "content": "中" * 500, "status": "pending"} for i in range(22)]
    ),
}


@pytest.mark.parametrize("name", sorted(INVALID_ARGUMENTS))
def test_an_invalid_list_is_refused_whole(name):
    arguments = INVALID_ARGUMENTS[name]
    view = _view(arguments, "op-bad")
    assert view["status"] == "denied", view
    assert view["reason"] == "INVALID_TODOS"
    assert view["operation_id"] == "op-bad"
    assert view["tool"] == TOOL
    assert view["adopted"] is False and view["citable_as_fact"] is False
    assert "todos" not in view and "counts" not in view
    assert isinstance(view["message"], str) and view["message"]
    assert "evidence_id" not in view


def test_a_refusal_does_not_echo_the_content():
    marker = "do-not-echo-c1-marker"
    view = _view(_over(content=marker + "x" * 600))
    assert view["reason"] == "INVALID_TODOS"
    assert marker not in json.dumps(dict(view))


def test_content_is_redacted_before_it_is_stored_or_returned():
    view = _view(
        _args(
            [
                {
                    "id": "t1",
                    "content": f"Rotate the DSN with password={SECRET} later",
                    "status": "pending",
                }
            ]
        )
    )
    assert view["status"] == "ok"
    text = json.dumps(dict(view))
    assert SECRET not in text
    assert "REDACTED_CREDENTIAL" in view["todos"][0]["content"]
    assert view["todos"][0]["content"].startswith("Rotate the DSN with ")


# -- 4. the model-visible surface and the revisions --------------------------------


def _face(env: Mapping[str, str]):
    return select_profile(env).face(FakeClock(start=NOW))


def _names(env: Mapping[str, str]) -> list[str]:
    return [s["function"]["name"] for s in _face(env).tool_schemas]


def _system(env: Mapping[str, str]) -> str:
    face = _face(env)
    run_input = face.input_for(
        run_id=RUN_ID,
        question="Why is checkout degraded?",
        target_id="otel-demo",
        deadline=NOW + timedelta(minutes=10),
        model_requests=12,
    )
    return initial_messages(run_input, evidence_context=None)[1]


def _input(env: Mapping[str, str]):
    return _face(env).input_for(
        run_id=RUN_ID,
        question="Why is checkout degraded?",
        target_id="otel-demo",
        deadline=NOW + timedelta(minutes=10),
        model_requests=12,
    )


def _b3():
    from tests import test_tool_expansion_b3_contract as b3

    return b3


def _backend_env(es: bool, loki: bool, vlogs: bool) -> dict[str, str]:
    return _b3()._backend_env(es, loki, vlogs)


def _surface(profile: ToolProfile) -> str:
    return _b3()._surface_sha256(profile)


def _with(env: Mapping[str, str], value: str | None) -> dict[str, str]:
    out = dict(env)
    if value is not None:
        out[SWITCH] = value
    return out


@pytest.mark.parametrize("value", OFF_VALUES)
def test_switch_off_keeps_every_extension_backend_set_at_its_pin(value):
    # The B3 contract keeps the switch-off pins of the eight backend sets;
    # with the C1 switch off they must hold unchanged (C1 §4).
    pinned = _b3().PINNED
    for backends, (revision, surface) in pinned.items():
        profile = select_profile(_with(_backend_env(*backends), value))
        assert profile.versions()["tool_schema_revision"] == revision, backends
        assert _surface(profile) == surface, backends
        assert TOOL not in _names(_with(_backend_env(*backends), value))


@pytest.mark.parametrize("value", OFF_VALUES)
def test_switch_off_keeps_the_system_bytes_versions_and_input(value):
    base = {PROFILE_ENV: EXT_PROFILE}
    off = _with(base, value)
    assert _system(off) == _system(base)
    assert select_profile(off).versions() == select_profile(base).versions()
    assert _input(off).as_json() == _input(base).as_json()
    assert _face(off).variant_id == _face(base).variant_id


@pytest.mark.parametrize("value", ["1", "0", "", "garbage", "on"])
def test_other_profiles_never_read_the_switch(value):
    b3 = _b3()
    otel = select_profile({PROFILE_ENV: "otel-demo", SWITCH: value})
    assert otel.versions()["tool_schema_revision"] == b3.BASE_TOOL_SCHEMA_REVISION
    assert _surface(otel) == b3.BASE_SURFACE_SHA256
    assert _system({PROFILE_ENV: "otel-demo", SWITCH: value}) == _system(
        {PROFILE_ENV: "otel-demo"}
    )
    fixture = select_profile({SWITCH: value})
    assert fixture.versions() == select_profile({}).versions()
    assert TOOL not in [
        s["function"]["name"] for s in fixture.face(FakeClock(start=NOW)).tool_schemas
    ]


@pytest.mark.parametrize(
    "backends",
    [
        (False, False, False),
        (True, False, False),
        (False, True, True),
        (True, True, True),
    ],
)
def test_switch_on_appends_todowrite_last(backends):
    off = _backend_env(*backends)
    on = _with(off, "1")
    old, new = _face(off).tool_schemas, _face(on).tool_schemas
    assert [dict(s) for s in new[:-1]] == [dict(s) for s in old]
    assert dict(new[-1]) == dict(_todo().TODO_WRITE_SCHEMA)
    # Not a gateway tool: the source list the model reads is unchanged.
    assert (
        _face(on).evidence_context(RUN_ID)["data_sources"]
        == _face(off).evidence_context(RUN_ID)["data_sources"]
    )


def test_switch_on_changes_tool_schema_and_prompt_revisions_only():
    base = {PROFILE_ENV: EXT_PROFILE}
    off, on = (
        select_profile(base).versions(),
        select_profile(_with(base, "1")).versions(),
    )
    assert set(on) == set(off)
    assert on["tool_schema_revision"] != off["tool_schema_revision"]
    assert on["tool_schema_revision"].startswith("otel-demo-ext-")
    assert on["prompt_revision"] != off["prompt_revision"]
    assert on["context_policy_revision"] == off["context_policy_revision"]
    for key in set(off) - {"tool_schema_revision", "prompt_revision"}:
        assert on[key] == off[key], key


def test_switch_on_keeps_the_input_version_and_question_bytes():
    base = {PROFILE_ENV: EXT_PROFILE}
    off, on = _input(base), _input(_with(base, "1"))
    assert on.version == off.version
    assert on.question == off.question
    assert set(on.as_json()) == set(off.as_json())
    assert dict(on.scope_facts) == dict(off.scope_facts)  # no new fact key
    assert on.tool_schemas[-1]["function"]["name"] == TOOL
    assert on.variant_id == off.variant_id + "-todo"


def test_switch_on_variant_and_guidance_position():
    from opspilot.instructions import discipline
    from opspilot.investigation.context import REPORT_CONTRACT

    base = {PROFILE_ENV: EXT_PROFILE}
    assert _face(base).variant_id == "replay-candidate-k8s"
    assert _face(_with(base, "1")).variant_id == "replay-candidate-k8s-todo"
    assert "replay-candidate-k8s-todo" in discipline.VARIANTS
    off, on = _system(base), _system(_with(base, "1"))
    tail = " " + REPORT_CONTRACT
    assert off.endswith(tail) and on.endswith(tail)
    head = off[: -len(tail)]
    assert on.startswith(head)  # K8s guidance and everything before it unchanged
    inserted = on[len(head) : -len(tail)]
    guidance = discipline.TODO_GUIDANCE
    # ASSUMPTION: a str, or a segment carrying ``text``.
    guidance_text = guidance if isinstance(guidance, str) else guidance.text
    assert inserted == guidance_text
    assert TOOL in inserted and "not evidence" in inserted
    assert "evidence_id" in inserted


def test_ext_keyword_functions_agree_with_the_profile():
    ext = _ext()
    on = select_profile({PROFILE_ENV: EXT_PROFILE, SWITCH: "1"})
    assert (
        ext.TODO_TOOL_SCHEMA_REVISION
        == on.versions()["tool_schema_revision"]
        == ext.tool_schema_revision(todo=True)
    )
    assert ext.otel_demo_ext_versions(todo=True) == on.versions()
    assert ext.otel_demo_ext_versions(todo=False) == ext.otel_demo_ext_versions()
    assert ext.tool_schema_revision(todo=False) == ext.TOOL_SCHEMA_REVISION
    assert [dict(s) for s in ext._tool_schemas(todo=False)] == [
        dict(s) for s in ext._tool_schemas()
    ]
    assert ext._tool_schemas(todo=True)[-1]["function"]["name"] == TOOL
    face = ext.otel_demo_ext_face(FakeClock(start=NOW), todo=True)
    assert [dict(s) for s in face.tool_schemas] == [
        dict(s) for s in on.face(FakeClock(start=NOW)).tool_schemas
    ]
    for fn in (
        ext._tool_schemas,
        ext.tool_schema_revision,
        ext.otel_demo_ext_versions,
        ext.otel_demo_ext_face,
        ext.otel_demo_ext_executor_factory,
    ):
        assert inspect.signature(fn).parameters["todo"].default is False, fn
    # Not a registered gateway tool: the registrations take no switch.
    assert "todo" not in inspect.signature(ext._registrations).parameters
    assert TOOL not in {item.name for item in ext._registrations()}


def test_switch_with_live_metrics_gives_four_distinct_revisions():
    revisions = set()
    for live in ("0", "1"):
        for todo in ("0", "1"):
            env = {
                PROFILE_ENV: EXT_PROFILE,
                "OPSPILOT_K8S_LIVE_METRICS": live,
                SWITCH: todo,
            }
            versions = select_profile(env).versions()
            revisions.add(
                (versions["tool_schema_revision"], versions["prompt_revision"])
            )
            names = _names(env)
            assert (names[-1] == TOOL) is (todo == "1")
    assert len({r for r, _ in revisions}) == 4


def _has_knowledge_base() -> bool:
    return "knowledge" in inspect.signature(_ext()._tool_schemas).parameters


def test_switch_with_knowledge_tool_combinations():
    if not _has_knowledge_base():
        pytest.skip("base without C2b (PR #741): no knowledge switch yet")
    surfaces, prompts = set(), set()
    base_names = _names({PROFILE_ENV: EXT_PROFILE, KNOWLEDGE_SWITCH: "1"})
    for knowledge in ("0", "1"):
        for todo in ("0", "1"):
            env = {PROFILE_ENV: EXT_PROFILE, KNOWLEDGE_SWITCH: knowledge, SWITCH: todo}
            versions = select_profile(env).versions()
            surfaces.add(versions["tool_schema_revision"])
            prompts.add(versions["prompt_revision"])
    assert len(surfaces) == 4 and len(prompts) >= 2
    both = _names({PROFILE_ENV: EXT_PROFILE, KNOWLEDGE_SWITCH: "1", SWITCH: "1"})
    assert both[:-1] == base_names and both[-1] == TOOL  # after the knowledge tool


def _has_cluster_base() -> bool:
    from opspilot.instructions import discipline

    return "replay-candidate-k8s-cluster" in discipline.VARIANTS


def test_switch_with_cluster_name_combinations():
    if not _has_cluster_base():
        pytest.skip("base without #722 (PR #725): no cluster variant yet")
    from opspilot.instructions import discipline
    from opspilot.investigation.context import REPORT_CONTRACT

    seen = {}
    for cluster in (None, "kind-lab"):
        for todo in ("0", "1"):
            env = {PROFILE_ENV: EXT_PROFILE, SWITCH: todo}
            if cluster is not None:
                env[CLUSTER_ENV] = cluster
            seen[(cluster, todo)] = (
                _face(env).variant_id,
                select_profile(env).versions()["prompt_revision"],
                _system(env),
            )
    assert seen[("kind-lab", "1")][0] == "replay-candidate-k8s-cluster-todo"
    assert seen[(None, "1")][0] == "replay-candidate-k8s-todo"
    assert len({v[1] for v in seen.values()}) == 4
    assert len({v[2] for v in seen.values()}) == 4
    # Cluster segment first, then the task-list guidance, then the contract.
    tail = " " + REPORT_CONTRACT
    guidance = discipline.TODO_GUIDANCE
    guidance_text = guidance if isinstance(guidance, str) else guidance.text
    cluster_off = seen[("kind-lab", "0")][2]
    cluster_on = seen[("kind-lab", "1")][2]
    assert cluster_on == cluster_off[: -len(tail)] + guidance_text + tail


def test_web_on_and_worker_off_record_different_versions():
    # A Run recorded by a web with the switch is blocked on claim by a worker
    # without it (C3 §5): the versions differ.
    on = select_profile({PROFILE_ENV: EXT_PROFILE, SWITCH: "1"}).versions()
    off = select_profile({PROFILE_ENV: EXT_PROFILE}).versions()
    assert on != off


def test_an_invalid_switch_value_fails_the_web_start():
    for value in ("on", "true", " 1"):
        with pytest.raises(SystemExit, match="TODO_WRITE_INVALID"):
            profile = select_profile({PROFILE_ENV: EXT_PROFILE, SWITCH: value})
            profile.versions()
            profile.face(FakeClock(start=NOW)).tool_schemas  # noqa: B018


def _worker_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    placeholder = tmp_path / "synthetic-ca-placeholder.pem"
    placeholder.write_text("synthetic ca placeholder\n")
    return {
        PROFILE_ENV: EXT_PROFILE,
        "OPSPILOT_K8S_API_URL": "https://127.0.0.1:16443",
        "OPSPILOT_K8S_TOKEN": "k8s-ro-aaaaaaaa565",
        "OPSPILOT_K8S_CA_FILE": str(placeholder),
        "OPSPILOT_K8S_NAMESPACE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_OTEL_PROMETHEUS_USERNAME": "investigator",
        "OPSPILOT_OTEL_PROMETHEUS_PASSWORD": "bbbbbbbb565",
        **extra,
    }


def _worker_executor(env: Mapping[str, str]):
    import shutil

    from tests.m1_tool_support import RecordingSink
    from tests.test_m1_otel_demo_contract import FakeStore, _lease

    if shutil.which("jq") is None or shutil.which("python3") is None:
        pytest.skip("the extension worker needs jq and python3 on PATH")
    clock = FakeClock(start=NOW)
    profile = select_profile(env)
    lease = _lease()
    deadline = NOW + timedelta(minutes=10)
    factory = profile.executor_factory(
        FakeStore(lease, deadline=deadline), RecordingSink(), clock
    )
    run_input = profile.face(clock).input_for(
        run_id=str(lease.run_id),
        question="Why is checkout degraded?",
        target_id="otel-demo",
        deadline=deadline,
        model_requests=12,
    )
    return factory(lease, run_input), run_input


def test_an_invalid_switch_value_fails_the_worker_start(tmp_path):
    with pytest.raises(SystemExit, match="TODO_WRITE_INVALID"):
        _worker_executor(_worker_env(tmp_path, **{SWITCH: "on"}))


def test_the_worker_gateway_never_registers_todowrite(tmp_path):
    from opspilot.tools import ToolRequest

    executor, run_input = _worker_executor(_worker_env(tmp_path, **{SWITCH: "1"}))
    assert run_input.tool_schemas[-1]["function"]["name"] == TOOL
    outcome = executor.execute(
        ToolRequest(
            step_id="c1-step",
            tool_index=0,
            tool_name=TOOL,
            target_ref="otel-demo",
            params=_args(_items("pending")),
            window=executor.scope.window.as_json(),
        )
    )
    assert (outcome.status, outcome.reason) == ("denied", "TOOL_NOT_REGISTERED")


def test_entrypoint_passes_the_switch_to_the_worker(tmp_path):
    entrypoint = ROOT / "deploy" / "docker-entrypoint.sh"
    worker_env = tmp_path / "worker_env"
    worker_env.write_text(f"{SWITCH}=1\n")
    result = subprocess.run(
        ["sh", str(entrypoint), "env"],
        env={"PATH": "/usr/bin:/bin", "OPSPILOT_ENV_FILE": str(worker_env)},
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    env = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert env[SWITCH] == "1"


def test_no_migration_is_added_for_the_task_list():
    versions = ROOT / "opspilot" / "migrations" / "versions"
    for path in versions.glob("*.py"):
        text = path.read_text()
        assert TOOL not in text and "todo" not in text.lower(), path.name


# -- 3/5/6/7. the investigation loop ----------------------------------------------


class CountingExecutor:
    """The real executor, counting what reaches it."""

    def __init__(self, inner):
        self._inner = inner
        self.requests: list[Any] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def execute(self, request):
        self.requests.append(request)
        return self._inner.execute(request)


def _schemas(todo: bool = True) -> tuple[Mapping[str, Any], ...]:
    if not todo:
        return tuple(METRICS_SCHEMAS)
    return (*METRICS_SCHEMAS, _todo().TODO_WRITE_SCHEMA)


class Harness:
    def __init__(
        self,
        replies,
        *,
        parallelism=1,
        model_requests=2,
        todo=True,
        store_cls=MemoryStepStore,
    ):
        self.ledger = RecordingLedger()
        executor, self.transport, self.sink, self.clock = build(ledger=self.ledger)
        self.transport.response = TransportResponse(
            body=body([{"metric": "checkout", "value": 3}]), data_as_of=WINDOW_START
        )
        self.executor = CountingExecutor(executor)
        run_id = executor.scope.run_id
        self.store = store_cls(
            budget_limit=10,
            deadline=executor.scope.deadline,
            clock=self.clock,
            run_id=run_id,
        )
        self.model = ScriptedModel(replies)
        self.loop = InvestigationLoop(
            model=self.model,
            executor=self.executor,
            store=self.store,
            clock=self.clock,
            tool_parallelism=parallelism,
        )
        self.request = InvestigationRequest(
            run_id=run_id,
            question="Why is checkout erroring?",
            scope=executor.scope,
            tool_schemas=_schemas(todo),
            model_requests=model_requests,
            evidence_context=historical_window_context(run_id),
        )

    def run(self):
        return self.loop.run(self.request)

    def results(self) -> list[list[Mapping[str, Any]]]:
        """Each committed step's tool results, in step order."""
        snapshot = self.store.snapshot()
        return [
            [item["result"] for item in step["tool_results"]]
            for step in snapshot["steps"]
        ]


def _todo_call(todos, call_id="call-todo"):
    return tool_call(call_id=call_id, name=TOOL, arguments=json.dumps(_args(todos)))


def _metrics_call(call_id="call-metrics"):
    return tool_call(call_id=call_id, name=METRICS, arguments=METRICS_ARGS)


def _tools(*calls):
    return reply(tool_calls=list(calls), finish="tool_calls")


def _tool_messages(call) -> list[dict[str, Any]]:
    return [json.loads(m["content"]) for m in call.messages if m.get("role") == "tool"]


def _cite_metrics(call):
    """Final report citing the metrics evidence (never the task list)."""
    eids = [v["evidence_id"] for v in _tool_messages(call) if v.get("evidence_id")]
    return reply(content=report_json(evidence_id=eids[-1] if eids else "missing"))


def test_a_todowrite_call_never_reaches_the_gateway():
    todos = _items("in_progress", "pending")
    h = Harness([_tools(_todo_call(todos)), _cite_metrics], model_requests=2)
    outcome = h.run()
    assert h.executor.requests == []
    assert h.transport.requests == []
    assert h.sink.records == []  # no evidence row
    assert h.ledger.charges == []  # no charge row
    [first, *_] = h.results()
    assert len(first) == 1
    stored = first[0]
    assert stored["tool"] == TOOL and stored["status"] == "ok"
    assert stored["todos"] == todos
    assert "evidence_id" not in stored
    assert outcome.evidence_ids == ()
    # The model's next request carried the same view as the tool result.
    sent = _tool_messages(h.model.calls[1])
    assert sent == [dict(stored)]


def test_a_todowrite_beside_a_query_leaves_the_query_unchanged():
    todos = _items("in_progress")
    h = Harness(
        [_tools(_metrics_call(), _todo_call(todos)), _cite_metrics], model_requests=2
    )
    outcome = h.run()
    assert [r.tool_name for r in h.executor.requests] == [METRICS]
    first = h.results()[0]
    assert [r["tool"] for r in first] == [METRICS, TOOL]
    assert len(h.sink.records) == 1
    # The ledger sees only the query's operation (reserve and settle).
    assert {op for op, _ in h.ledger.charges} == {first[0]["operation_id"]}
    assert first[0]["status"] == "ok" and first[0]["evidence_id"]
    assert outcome.execution == "completed", outcome.handoff_reasons
    assert outcome.report is not None
    assert list(outcome.evidence_ids) == [first[0]["evidence_id"]]


@pytest.mark.parametrize("parallelism", [1, 2, 4])
def test_ordinals_and_the_later_list_win_in_one_round(parallelism):
    early, late = _items("pending", "pending"), _items("completed", "in_progress")
    h = Harness(
        [
            _tools(
                _todo_call(early, "c-0"),
                _metrics_call("c-1"),
                _todo_call(late, "c-2"),
            ),
            _cite_metrics,
        ],
        parallelism=parallelism,
        model_requests=2,
    )
    outcome = h.run()
    first = h.results()[0]
    assert [r["tool"] for r in first] == [TOOL, METRICS, TOOL]
    assert first[0]["todos"] == early and first[2]["todos"] == late
    assert first[1]["evidence_id"]
    assert len(h.executor.requests) == 1  # only the query went to the gateway
    assert outcome.execution == "completed", outcome.handoff_reasons
    # The tool messages reach the model in call order, paired by id.
    call = h.model.calls[1]
    ids = [m["tool_call_id"] for m in call.messages if m.get("role") == "tool"]
    assert ids == ["c-0", "c-1", "c-2"]


def test_a_refused_list_is_committed_as_a_refusal_and_the_run_goes_on():
    good = _items("in_progress", "pending")
    bad = _items(*["pending"] * 51)
    h = Harness(
        [
            _tools(_todo_call(good, "c-0")),
            _tools(_todo_call(bad, "c-1"), _metrics_call("c-2")),
            _cite_metrics,
        ],
        model_requests=3,
    )
    outcome = h.run()
    steps = h.results()
    assert steps[0][0]["status"] == "ok"
    refused = steps[1][0]
    assert (refused["status"], refused["reason"]) == ("denied", "INVALID_TODOS")
    assert "todos" not in refused
    assert outcome.execution == "completed", outcome.handoff_reasons


def test_dispatch_follows_the_run_input_not_the_process_environment(monkeypatch):
    monkeypatch.setenv(SWITCH, "0")
    h = Harness([_tools(_todo_call(_items("pending"))), _cite_metrics])
    h.run()
    assert h.executor.requests == []
    assert h.results()[0][0]["status"] == "ok"


def test_without_the_schema_a_todowrite_call_goes_to_the_gateway(monkeypatch):
    # Switch off: the model writing the name anyway is an unregistered tool.
    monkeypatch.setenv(SWITCH, "1")
    h = Harness([_tools(_todo_call(_items("pending"))), _cite_metrics], todo=False)
    h.run()
    assert [r.tool_name for r in h.executor.requests] == [TOOL]
    stored = h.results()[0][0]
    assert (stored["status"], stored["reason"]) == ("denied", "TOOL_NOT_REGISTERED")
    assert "todos" not in stored


def test_a_report_citing_the_task_list_is_refused():
    h = Harness(
        [
            _tools(_todo_call(_items("completed"), "c-0"), _metrics_call("c-1")),
            lambda call: reply(
                content=report_json(evidence_id=_tool_messages(call)[0]["operation_id"])
            ),
        ],
        model_requests=2,
    )
    outcome = h.run()
    assert outcome.report is None
    assert outcome.handoff is True
    todo_op = h.results()[0][0]["operation_id"]
    assert todo_op not in outcome.evidence_ids


def test_a_report_citing_a_made_up_id_is_refused():
    h = Harness(
        [
            _tools(_todo_call(_items("completed"))),
            lambda call: reply(content=report_json(evidence_id="ev-made-up-565")),
        ],
        model_requests=2,
    )
    outcome = h.run()
    assert outcome.report is None and outcome.handoff is True


def test_task_list_rounds_count_as_model_requests_with_no_extra_cap():
    many = [_todo_call(_items("pending"), f"c-{i}") for i in range(30)]
    h = Harness(
        [
            _tools(*many),
            _tools(_todo_call(_items("completed"), "d-0"), _metrics_call("d-1")),
            _cite_metrics,
        ],
        model_requests=3,
    )
    outcome = h.run()
    assert len(h.model.calls) == 3
    assert outcome.model_requests_used == 3
    first = h.results()[0]
    assert len(first) == 30 and all(r["status"] == "ok" for r in first)
    assert outcome.execution == "completed", outcome.handoff_reasons


def test_the_final_round_offers_no_tools():
    h = Harness([_tools(_todo_call(_items("pending"))), _cite_metrics])
    h.run()
    assert TOOL in [t["function"]["name"] for t in h.model.calls[0].tools]
    assert h.model.calls[-1].tools is None


class _PauseAfterStep(MemoryStepStore):
    """A human pauses right after the plan is committed."""

    def commit_step(self, logical_key, response):
        step_id = super().commit_step(logical_key, response)
        if response.get("assistant", {}).get("tool_calls"):
            self.deny_control()
        return step_id


def test_a_pause_before_the_commit_stops_the_task_list():
    h = Harness(
        [_tools(_todo_call(_items("pending"))), _cite_metrics],
        store_cls=_PauseAfterStep,
    )
    outcome = h.run()
    assert h.results()[0] == []  # the list was never committed
    assert outcome.handoff is True
    assert "CONTROL_DENIED" in outcome.handoff_reasons
    assert len(h.model.calls) == 1


def test_a_rebuilt_transcript_carries_the_committed_list():
    from opspilot.investigation.context import rebuild_transcript

    todos = _items("in_progress", "pending")
    h = Harness([_tools(_todo_call(todos)), _cite_metrics])
    h.run()
    snapshot = h.store.snapshot()
    # The step after the tool round is the conclusion; keep only the tool
    # round, as a worker killed before the final request would leave it.
    snapshot["steps"] = snapshot["steps"][:1]
    snapshot["conclusion"] = None
    snapshot["state"] = snapshot["run"]["state"] = "running"
    transcript = rebuild_transcript(
        snapshot,
        run_id=h.request.run_id,
        authorized_targets=h.request.scope.target_ids,
        input=h.request.as_input(),
    )
    tools = [
        json.loads(m["content"]) for m in transcript.messages if m.get("role") == "tool"
    ]
    assert [t.get("todos") for t in tools] == [todos]


# -- trace: one tool span, redacted --------------------------------------------------


def test_a_todowrite_call_gets_one_redacted_tool_span():
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from opspilot import tracing
    from tests.test_tracing import lab_env

    exporter = InMemorySpanExporter()
    tracer = tracing.configure(lab_env(), exporter=exporter)
    try:
        content = f"Retry with password={SECRET}"
        h = Harness(
            [
                _tools(
                    _todo_call([{"id": "t1", "content": content, "status": "pending"}])
                ),
                _cite_metrics,
            ]
        )
        with tracer.run(
            incident_id=uuid4(),
            run_id=uuid4(),
            versions={"prompt_revision": "p", "tool_schema_revision": "t"},
        ) as span:
            h.run()
            span.settle("published", None)
        tracer.shutdown()
        spans = exporter.get_finished_spans()
        names = [s.name for s in spans]
        assert sum(1 for n in names if n.endswith(TOOL)) == 1, names
        text = json.dumps([dict(s.attributes) for s in spans], default=str)
        assert SECRET not in text
    finally:
        tracing.reset()
