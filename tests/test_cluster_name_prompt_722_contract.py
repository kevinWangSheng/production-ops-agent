"""Independent contract tests for #722: the operator-configured local cluster
name (``OPSPILOT_CLUSTER_NAME``) in the ``otel-demo-ext`` investigation
system prompt.

Written from the task record's 「公开接口」 section
(docs/tasks/2026-11-18-cluster-name-prompt-722.md), decision r10 H-U1 and the
contract prereview CN-1 ... CN-27, without reading the implementation. Red on
``main`` until the implementation lands; collection must stay green.

Pinned values (``*_PIN`` below) were computed on ``origin/main`` 23f362f2,
before the change, with this worktree's ``.venv/bin/python``: select each
profile from a mapping holding only ``OPSPILOT_TOOL_PROFILE``, build
``face.input_for(...)`` with the fixed arguments of :func:`_input`, take
``initial_messages(input, evidence_context=None)``'s system string (equal to
``render(variant, model_requests=..., report_contract=REPORT_CONTRACT)``, and
independent of ``model_requests`` 1/12/40) and hash it; the input pin is
``sha256(canonical(input.as_json()))``. The prompt pins match the task
record's 「未设时必须不变的钉值」.

No model call, no network, no PostgreSQL. Assumptions beyond the interface
are marked ``# ASSUMPTION:``.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from opspilot.instructions import VARIANTS, prompt_face_sha256, render
from opspilot.instructions.discipline import KUBERNETES_GUIDANCE
from opspilot.investigation.context import (
    AFFECTED_SERVICE,
    ContextError,
    InvestigationInput,
    evidence_context_projection,
    initial_messages,
    messages_hash,
    rebuild_transcript,
)
from opspilot.investigation.inputs import AlertAnchor
from opspilot.investigation.reports import REPORT_CONTRACT
from opspilot.persistence import PersistenceError
from opspilot.tools.otel_demo import TARGET_ID
from opspilot.tools.otel_demo_ext import otel_demo_ext_face, otel_demo_ext_versions
from opspilot.tools.profiles import PROFILE_ENV, select_profile
from opspilot.tools.registry import canonical
from opspilot.worker import Worker
from tests.m1_tool_support import FakeClock

CLUSTER_ENV = "OPSPILOT_CLUSTER_NAME"
EXT = "otel-demo-ext"
BASE_PROFILES = ("fixture", "otel-demo")

UNSET_VARIANT = "replay-candidate-k8s"
CLUSTER_VARIANT = "replay-candidate-k8s-cluster"
# Task record (2026-11-18-cluster-name-prompt-722): the cluster name is input v8;
# v7 belongs to C2b's runbook catalog, merged first.
V8 = "opspilot-investigation-input-v8"
V7_C2B = "opspilot-investigation-input-v7"

# -- pins computed on origin/main 23f362f2 (see module docstring) ------------
BASE_PROMPT_REVISION = "prompt-replay-candidate-f3c3018f981b"
BASE_SYSTEM_CHARS = 8081
BASE_SYSTEM_SHA256 = "23a7e4335cf5ffd60fa21cee4afdf8cbc8f56ca61c2dae9cf22ffbdf04d77a94"
EXT_PROMPT_REVISION = "prompt-replay-candidate-k8s-068c56342888"
EXT_SYSTEM_CHARS = 12757
EXT_SYSTEM_SHA256 = "b031c1ed2e749413c97ee73b0f55e2bcfa3def7e258233dc16e79e28fd4188d9"
EXT_TOOL_SCHEMA_REVISION = "otel-demo-ext-a4e56f2fb767"
INPUT_SHA256_PIN = {
    "fixture": "feb355dc6906cb8085766da0b6c6056041f723b0cd34bebb5e8c2bf4f75299ed",
    "otel-demo": "bbf82d6dc0f1f76af517b598a3d3381ef3ec27ea272a71f6f65b2ffefe1df559",
    EXT: "d6f13da950118a8b9bf997aff5eae19b9fbfcf5201c985f7eabfd47df3f7f785",
}

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
RUN_ID = "run-722"
QUESTION = "Why is checkout degraded?"
NAME = "kind-shop-722"
OTHER_NAME = "prod-eu-west-722"
ES_URL = "http://127.0.0.1:9200"

VALID_NAMES = (
    "a",
    "Z",
    "7",
    "x" * 128,
    "arn:aws:eks:us-east-1:000000000722:cluster/shop",
    "gke_shop-project_us-central1-a_prod",
    "reader@kind-shop",
    "kind-kind",
    "a.b.c",
    "trailing-",
    "trailing/",
    "9.9.9",
)
INVALID_NAMES = (
    " kind-shop",  # leading whitespace is refused, not trimmed
    "kind-shop ",
    "kind shop",
    "kind-shop\n",
    "\tkind",
    "   ",
    "kind`shop",
    "`kind`",
    "kind=shop",
    "KEY=value",
    "{cluster_name}",
    "kind{shop}",
    "kind}",
    "x" * 129,
    "-kind",
    ".kind",
    "_kind",
    "/kind",
    "@kind",
    ":kind",
    "kind'shop",
    'kind"shop',
    "kind,shop",
    "kind;shop",
    "kind$shop",
    "kind+shop",
    "pröd",
    "kind\u200bshop",
)


# -- helpers -------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_cluster_in_process_env(monkeypatch):
    # The profile reads the mapping it is given; the shell must not leak in.
    monkeypatch.delenv(CLUSTER_ENV, raising=False)


def _web_env(profile: str = EXT, **extra: str) -> dict[str, str]:
    return {PROFILE_ENV: profile, **extra}


def _worker_env(**extra: str) -> dict[str, str]:
    """A worker's mapping: the web's keys plus the worker-only settings."""
    return {
        PROFILE_ENV: EXT,
        "OPSPILOT_K8S_API_URL": "https://127.0.0.1:16443",
        "OPSPILOT_K8S_TOKEN": "dddddddd722",
        "OPSPILOT_K8S_CA_FILE": "/nonexistent/k8s-ca.pem",
        "OPSPILOT_K8S_NAMESPACE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        **extra,
    }


def _input(
    env: dict[str, str], *, alert: AlertAnchor | None = None
) -> InvestigationInput:
    face = select_profile(env).face(FakeClock(start=NOW))
    return face.input_for(
        run_id=RUN_ID,
        question=QUESTION,
        target_id=TARGET_ID,
        deadline=NOW + timedelta(minutes=10),
        model_requests=12,
        **({"alert": alert} if alert is not None else {}),
    )


def _system(env: dict[str, str]) -> str:
    _messages, system = initial_messages(_input(env), evidence_context=None)
    return system


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _input_sha(value: InvestigationInput) -> str:
    return hashlib.sha256(canonical(value.as_json()).encode("utf-8")).hexdigest()


def _cluster_paragraph(system: str) -> str:
    """What the set-ext system message adds to the unset one (CN-4)."""
    unset = _system(_web_env())
    tail = " " + REPORT_CONTRACT
    assert unset.endswith(tail)
    prefix = unset[: -len(tail)]
    assert prefix.endswith(KUBERNETES_GUIDANCE)
    assert system.startswith(prefix), "the unset ext prefix must be byte-identical"
    assert system.endswith(tail), "the report contract must stay last"
    return system[len(prefix) : -len(tail)]


def _code(excinfo: pytest.ExceptionInfo[BaseException]) -> str:
    error = excinfo.value
    return str(getattr(error, "code", None) or error)


# -- 1. unset: bytes and revisions unchanged ---------------------------------


@pytest.mark.parametrize("extra", [{}, {CLUSTER_ENV: ""}], ids=["absent", "empty"])
def test_unset_or_empty_leaves_every_profile_byte_identical(extra):
    for profile in BASE_PROFILES:
        env = _web_env(profile, **extra)
        selected = select_profile(env)
        assert selected.versions()["prompt_revision"] == BASE_PROMPT_REVISION
        system = _system(env)
        assert len(system) == BASE_SYSTEM_CHARS
        assert _sha(system) == BASE_SYSTEM_SHA256
        assert _input_sha(_input(env)) == INPUT_SHA256_PIN[profile]

    env = _web_env(EXT, **extra)
    selected = select_profile(env)
    face = selected.face(FakeClock(start=NOW))
    assert face.variant_id == UNSET_VARIANT
    assert face.cluster_name is None
    assert selected.versions()["prompt_revision"] == EXT_PROMPT_REVISION
    assert selected.versions()["tool_schema_revision"] == EXT_TOOL_SCHEMA_REVISION
    system = _system(env)
    assert len(system) == EXT_SYSTEM_CHARS
    assert _sha(system) == EXT_SYSTEM_SHA256

    value = _input(env)
    assert "cluster_name" not in value.scope_facts
    assert value.version == "opspilot-investigation-input-v1"
    assert "cluster_name" not in value.as_json()["scope_facts"]
    assert _input_sha(value) == INPUT_SHA256_PIN[EXT]
    # The worker's mapping agrees with the web's.
    assert select_profile(_worker_env(**extra)).versions() == selected.versions()


def test_unset_render_without_cluster_name_matches_the_pins():
    assert (
        _sha(render(UNSET_VARIANT, model_requests=12, report_contract=REPORT_CONTRACT))
        == EXT_SYSTEM_SHA256
    )
    assert (
        prompt_face_sha256(
            UNSET_VARIANT,
            model_requests=12,
            report_contract=REPORT_CONTRACT,
            cluster_name=None,
        )
        == EXT_SYSTEM_SHA256
    )
    assert (
        _sha(
            render(
                "replay-candidate",
                model_requests=12,
                report_contract=REPORT_CONTRACT,
                cluster_name=None,
            )
        )
        == BASE_SYSTEM_SHA256
    )


# -- 2. set on ext: new variant, paragraph, revision --------------------------


def test_set_on_ext_selects_the_cluster_variant_and_revision():
    selected = select_profile(_web_env(**{CLUSTER_ENV: NAME}))
    face = selected.face(FakeClock(start=NOW))
    assert face.variant_id == CLUSTER_VARIANT
    assert CLUSTER_VARIANT in VARIANTS
    assert face.cluster_name == NAME
    revision = selected.versions()["prompt_revision"]
    assert re.fullmatch(r"prompt-replay-candidate-k8s-cluster-[0-9a-f]{12}", revision)
    assert revision != EXT_PROMPT_REVISION
    assert len(revision) <= 64  # CN-18: the trace outbox IDENTIFIER bound
    # The name is L1b: never in the revision (CN-11) and never in L3.
    assert NAME not in json.dumps(selected.versions())
    assert selected.versions()["tool_schema_revision"] == EXT_TOOL_SCHEMA_REVISION
    unset = select_profile(_web_env())
    unset_face = unset.face(FakeClock(start=NOW))
    assert face.tool_schemas == unset_face.tool_schemas
    assert face.evidence_context(RUN_ID) == unset_face.evidence_context(RUN_ID)
    assert {k: v for k, v in selected.versions().items() if k != "prompt_revision"} == {
        k: v for k, v in unset.versions().items() if k != "prompt_revision"
    }


def test_paragraph_sits_between_kubernetes_guidance_and_report_contract():
    system = _system(_web_env(**{CLUSTER_ENV: NAME}))
    paragraph = _cluster_paragraph(system)
    assert paragraph.startswith(" "), "segments are joined with a leading space"
    assert paragraph.strip()
    # CN-5/CN-7: the name appears three times, each in backticks, and
    # nowhere else in the system message.
    assert paragraph.count(f"`{NAME}`") == 3
    assert system.count(NAME) == 3
    assert system.count(f"`{NAME}`") == 3


def test_paragraph_carries_the_upstream_points_in_opspilot_terms():
    # CN-5's semantic points, checked by the words any faithful wording needs
    # (no sentence regex, CN-22): only this cluster is visible to the
    # Kubernetes access; other sources may hold other clusters; verify by the
    # data's own cluster/region/environment fields; a mismatch is said first
    # in the summary, recorded as a gap and not taken as the cause.
    paragraph = _cluster_paragraph(_system(_web_env(**{CLUSTER_ENV: NAME})))
    lowered = paragraph.lower()
    for word in (
        "kubernetes",
        "cluster",
        "region",
        "environment",
        "gap",
        "summary",
        "cause",
    ):
        assert word in lowered, word
    # L1 names no tool and no command line (CN-5, A7/#676 convention).
    assert "kubectl" not in lowered
    face = select_profile(_web_env(**{CLUSTER_ENV: NAME}, OPSPILOT_ES_URL=ES_URL)).face(
        FakeClock(start=NOW)
    )
    for schema in face.tool_schemas:
        assert schema["function"]["name"] not in paragraph


def test_revision_does_not_depend_on_the_name_but_the_bytes_do():
    first = select_profile(_web_env(**{CLUSTER_ENV: NAME}))
    second = select_profile(_web_env(**{CLUSTER_ENV: OTHER_NAME}))
    assert first.versions() == second.versions()
    one = _system(_web_env(**{CLUSTER_ENV: NAME}))
    two = _system(_web_env(**{CLUSTER_ENV: OTHER_NAME}))
    assert one != two
    assert one.replace(NAME, OTHER_NAME) == two
    assert _cluster_paragraph(one).replace(NAME, OTHER_NAME) == _cluster_paragraph(two)


def test_cluster_name_combines_with_elasticsearch():
    plain_es = select_profile(_web_env(OPSPILOT_ES_URL=ES_URL))
    both = select_profile(_web_env(OPSPILOT_ES_URL=ES_URL, **{CLUSTER_ENV: NAME}))
    cluster_only = select_profile(_web_env(**{CLUSTER_ENV: NAME}))
    assert (
        both.versions()["tool_schema_revision"]
        == plain_es.versions()["tool_schema_revision"]
    )
    assert (
        both.versions()["prompt_revision"] == cluster_only.versions()["prompt_revision"]
    )
    face = both.face(FakeClock(start=NOW))
    assert face.variant_id == CLUSTER_VARIANT
    assert face.tool_schemas == plain_es.face(FakeClock(start=NOW)).tool_schemas
    assert _system(_web_env(OPSPILOT_ES_URL=ES_URL, **{CLUSTER_ENV: NAME})) == _system(
        _web_env(**{CLUSTER_ENV: NAME})
    )


def test_render_and_face_hash_with_cluster_name():
    system = _system(_web_env(**{CLUSTER_ENV: NAME}))
    rendered = render(
        CLUSTER_VARIANT,
        model_requests=12,
        report_contract=REPORT_CONTRACT,
        cluster_name=NAME,
    )
    assert rendered == system
    assert prompt_face_sha256(
        CLUSTER_VARIANT,
        model_requests=12,
        report_contract=REPORT_CONTRACT,
        cluster_name=NAME,
    ) == _sha(system)


@pytest.mark.parametrize(
    "variant, cluster_name",
    [
        (CLUSTER_VARIANT, None),
        (CLUSTER_VARIANT, ""),
        (UNSET_VARIANT, NAME),
        ("replay-candidate", NAME),
        (CLUSTER_VARIANT, "kind shop"),
        (CLUSTER_VARIANT, "kind`shop"),
        (CLUSTER_VARIANT, "{steps}"),
        (CLUSTER_VARIANT, "x" * 129),
        (CLUSTER_VARIANT, " kind"),
    ],
)
def test_render_fails_closed_on_a_missing_stray_or_malformed_name(
    variant, cluster_name
):
    for function in (render, prompt_face_sha256):
        with pytest.raises(ValueError):
            function(
                variant,
                model_requests=12,
                report_contract=REPORT_CONTRACT,
                cluster_name=cluster_name,
            )


def test_ext_profile_functions_only_look_at_whether_the_name_is_non_empty():
    clock = FakeClock(start=NOW)
    for es in (False, True):
        unset = otel_demo_ext_versions(elasticsearch=es)
        assert otel_demo_ext_versions(elasticsearch=es, cluster_name=None) == unset
        assert otel_demo_ext_versions(elasticsearch=es, cluster_name="") == unset
        named = otel_demo_ext_versions(elasticsearch=es, cluster_name=NAME)
        assert named == otel_demo_ext_versions(
            elasticsearch=es, cluster_name=OTHER_NAME
        )
        assert named["prompt_revision"] != unset["prompt_revision"]
        assert named["tool_schema_revision"] == unset["tool_schema_revision"]
        assert otel_demo_ext_face(clock, elasticsearch=es).variant_id == UNSET_VARIANT
        assert (
            otel_demo_ext_face(clock, elasticsearch=es, cluster_name="").cluster_name
            is None
        )
        face = otel_demo_ext_face(clock, elasticsearch=es, cluster_name=NAME)
        assert face.variant_id == CLUSTER_VARIANT
        assert face.cluster_name == NAME


# -- 3. base profiles ignore the variable -------------------------------------


@pytest.mark.parametrize("profile", BASE_PROFILES)
@pytest.mark.parametrize("value", [NAME, "kind shop", "x" * 200, "`{bad}`"])
def test_base_profiles_ignore_the_cluster_name_even_when_invalid(profile, value):
    env = _web_env(profile, **{CLUSTER_ENV: value})
    selected = select_profile(env)
    assert selected.versions() == select_profile(_web_env(profile)).versions()
    assert selected.versions()["prompt_revision"] == BASE_PROMPT_REVISION
    face = selected.face(FakeClock(start=NOW))
    assert face.variant_id == "replay-candidate"
    assert face.cluster_name is None
    assert _sha(_system(env)) == BASE_SYSTEM_SHA256
    value_input = _input(env)
    assert "cluster_name" not in value_input.scope_facts
    assert _input_sha(value_input) == INPUT_SHA256_PIN[profile]


# -- 4. validation at startup --------------------------------------------------


@pytest.mark.parametrize("name", INVALID_NAMES, ids=repr)
def test_invalid_name_stops_web_and_worker_at_profile_selection(name):
    for env in (
        _web_env(**{CLUSTER_ENV: name}),
        _web_env(OPSPILOT_ES_URL=ES_URL, **{CLUSTER_ENV: name}),
        _worker_env(**{CLUSTER_ENV: name}),
    ):
        with pytest.raises(SystemExit) as excinfo:
            select_profile(env)
        assert excinfo.value.code == "CLUSTER_NAME_INVALID"


@pytest.mark.parametrize("name", VALID_NAMES, ids=lambda n: n[:40])
def test_valid_names_are_accepted_verbatim(name):
    web = select_profile(_web_env(**{CLUSTER_ENV: name}))
    worker = select_profile(_worker_env(**{CLUSTER_ENV: name}))
    assert web.versions() == worker.versions()
    face = web.face(FakeClock(start=NOW))
    assert face.cluster_name == name
    assert face.variant_id == CLUSTER_VARIANT
    system = _system(_web_env(**{CLUSTER_ENV: name}))
    assert system.count(f"`{name}`") == 3
    assert _input(_web_env(**{CLUSTER_ENV: name})).scope_facts["cluster_name"] == name


# -- 5. Run input v8, rebuild, mismatches -------------------------------------


def test_set_input_records_the_name_as_v8_and_round_trips():
    from opspilot.investigation.context import CLUSTER_NAME, INPUT_VERSION_CLUSTER_NAME

    assert CLUSTER_NAME == "cluster_name"
    assert INPUT_VERSION_CLUSTER_NAME == V8
    value = _input(_web_env(**{CLUSTER_ENV: NAME}))
    assert value.scope_facts[CLUSTER_NAME] == NAME
    assert value.variant_id == CLUSTER_VARIANT
    assert value.version == V8
    # Through JSON, as PostgreSQL stores it.
    row = json.loads(json.dumps(value.as_json()))
    assert row["version"] == V8
    assert row["scope_facts"]["cluster_name"] == NAME
    again = InvestigationInput.from_json(row)
    assert again == value
    assert again.as_json() == value.as_json()
    # Everything but the variant and the fact equals the unset ext input.
    unset = _input(_web_env()).as_json()
    for key in ("question", "model_requests", "limits", "tool_schemas", "tool_face_sha256",
                "evidence_context", "bound_target_id"):  # fmt: skip
        assert row[key] == unset[key], key
    assert {
        k: v for k, v in row["scope_facts"].items() if k != "cluster_name"
    } == unset["scope_facts"]


def test_v8_outranks_and_coexists_with_earlier_facts():
    alert = AlertAnchor(
        received_at=NOW - timedelta(minutes=1),
        starts_at=NOW - timedelta(minutes=5),
        original=(NOW - timedelta(minutes=5)).isoformat(),
    )
    alerted = _input(_web_env(**{CLUSTER_ENV: NAME}), alert=alert)
    assert alerted.version == V8
    assert alerted.scope_facts["cluster_name"] == NAME
    assert "alert_starts_at" in alerted.scope_facts
    assert (
        InvestigationInput.from_json(json.loads(json.dumps(alerted.as_json())))
        == alerted
    )
    # Without the name the same alert input keeps its own version.
    assert _input(_web_env(), alert=alert).version == "opspilot-investigation-input-v3"

    value = _input(_web_env(**{CLUSTER_ENV: NAME}))
    with_service = replace(
        value,
        scope_facts={
            **value.scope_facts,
            AFFECTED_SERVICE: {"namespace": "otel-demo", "workload": "checkout"},
        },
    )
    assert with_service.version == V8
    assert (
        InvestigationInput.from_json(json.loads(json.dumps(with_service.as_json())))
        == with_service
    )
    # It also coexists with, and outranks, C2b's v7 runbook catalog fact.
    from opspilot.investigation.context import RUNBOOK_CATALOG
    from opspilot.knowledge.catalog import unavailable_catalog

    with_catalog = replace(
        value,
        scope_facts={**value.scope_facts, RUNBOOK_CATALOG: unavailable_catalog(86400)},
    )
    assert with_catalog.version == V8
    assert with_catalog.scope_facts["cluster_name"] == NAME
    assert (
        InvestigationInput.from_json(json.loads(json.dumps(with_catalog.as_json())))
        == with_catalog
    )


@pytest.mark.parametrize("bad", [*INVALID_NAMES[:6], "", None, 722, ["kind"]], ids=repr)
def test_input_with_a_malformed_name_is_invalid(bad):
    value = _input(_web_env(**{CLUSTER_ENV: NAME}))
    with pytest.raises(ContextError) as excinfo:
        replace(value, scope_facts={**value.scope_facts, "cluster_name": bad})
    assert excinfo.value.code == "INPUT_INVALID"
    row = json.loads(json.dumps(value.as_json()))
    row["scope_facts"]["cluster_name"] = bad
    with pytest.raises(ContextError) as excinfo:
        InvestigationInput.from_json(row)
    assert excinfo.value.code == "INPUT_INVALID"


def test_input_variant_and_fact_must_agree():
    named = _input(_web_env(**{CLUSTER_ENV: NAME}))
    unset = _input(_web_env())
    base = _input(_web_env("otel-demo"))
    cases = [
        # the fact without a cluster slot in the variant
        lambda: replace(named, variant_id=UNSET_VARIANT),
        lambda: replace(base, scope_facts={**base.scope_facts, "cluster_name": NAME}),
        lambda: replace(unset, scope_facts={**unset.scope_facts, "cluster_name": NAME}),
        # a cluster slot without the fact
        lambda: replace(unset, variant_id=CLUSTER_VARIANT),
        lambda: replace(
            named,
            scope_facts={
                k: v for k, v in named.scope_facts.items() if k != "cluster_name"
            },
        ),
    ]
    for build in cases:
        with pytest.raises(ContextError) as excinfo:
            build()
        assert excinfo.value.code == "INPUT_INVALID"


def test_from_json_refuses_rows_whose_version_variant_or_fact_disagree():
    named = json.loads(json.dumps(_input(_web_env(**{CLUSTER_ENV: NAME})).as_json()))
    unset = json.loads(json.dumps(_input(_web_env()).as_json()))

    def tampered(row: dict[str, Any], **changes: Any) -> dict[str, Any]:
        copy = json.loads(json.dumps(row))
        for key, value in changes.items():
            copy[key] = value
        return copy

    without_fact = tampered(named)
    del without_fact["scope_facts"]["cluster_name"]
    with_fact = tampered(unset)
    with_fact["scope_facts"]["cluster_name"] = NAME
    rows = [
        # a v1 row carrying the v8 fact
        tampered(named, version="opspilot-investigation-input-v1"),
        tampered(named, version="opspilot-investigation-input-v6"),
        tampered(named, version=V7_C2B),  # C2b's v7 does not carry the name
        tampered(named, variant_id=UNSET_VARIANT),
        tampered(unset, variant_id=CLUSTER_VARIANT),
        tampered(unset, version=V8),  # v8 row without the fact
        without_fact,
        with_fact,  # v1 row with the fact
        {**with_fact, "version": V8, "variant_id": UNSET_VARIANT},
    ]
    for row in rows:
        with pytest.raises(ContextError) as excinfo:
            InvestigationInput.from_json(row)
        assert excinfo.value.code == "INPUT_INVALID", row["version"]


def test_messages_use_the_recorded_name_not_the_process_environment(monkeypatch):
    value = _input(_web_env(**{CLUSTER_ENV: NAME}))
    row = json.loads(json.dumps(value.as_json()))
    context = evidence_context_projection(value.evidence_context, run_id=RUN_ID)
    live, system = initial_messages(value, evidence_context=context)
    assert system.count(f"`{NAME}`") == 3

    snapshot = {
        "incident_id": None,
        "state": "running",
        "control_generation": 0,
        "run": {
            "run_id": RUN_ID,
            "state": "running",
            "budget_limit": 12,
            "deadline": NOW + timedelta(minutes=10),
            "input": row,
        },
        "steps": [],
        "pending_tools": [],
        "conclusion": None,
        "inputs": [],
    }
    for environment in (OTHER_NAME, "", None, "not a valid name"):
        if environment is None:
            monkeypatch.delenv(CLUSTER_ENV, raising=False)
        else:
            monkeypatch.setenv(CLUSTER_ENV, environment)
        rebuilt = rebuild_transcript(
            snapshot, run_id=RUN_ID, authorized_targets=frozenset({TARGET_ID})
        )
        prefix = rebuilt.messages[: rebuilt.prefix_len]
        assert prefix[0] == {"role": "system", "content": system}
        assert OTHER_NAME not in prefix[0]["content"]
        # The prefix hash a committed step records is reproduced exactly.
        assert messages_hash(prefix) == messages_hash(live)
        assert rebuilt.input == value


def test_continuation_inherits_the_variant_and_name(monkeypatch):
    from opspilot.investigation.inputs import continuation_input

    value = _input(_web_env(**{CLUSTER_ENV: NAME}))
    snapshot = {
        "incident_id": None,
        "state": "running",
        "control_generation": 0,
        "run": {
            "run_id": RUN_ID,
            "state": "failed",
            "budget_limit": 12,
            "deadline": NOW + timedelta(minutes=10),
            "input": json.loads(json.dumps(value.as_json())),
        },
        "steps": [],
        "pending_tools": [],
        "conclusion": None,
        "inputs": [],
    }
    monkeypatch.setenv(CLUSTER_ENV, OTHER_NAME)
    successor = continuation_input(
        snapshot,
        new_run_id="run-722-next",
        deadline=NOW + timedelta(minutes=30),
        authorized_targets=frozenset({TARGET_ID}),
    )
    assert successor.variant_id == CLUSTER_VARIANT
    assert successor.scope_facts["cluster_name"] == NAME
    assert successor.version == V8


# -- web/worker agreement and the claim gate ----------------------------------


def test_web_and_worker_mappings_agree_when_both_set_or_both_unset():
    for extra in ({}, {CLUSTER_ENV: NAME}):
        web = select_profile(_web_env(**extra))
        worker = select_profile(_worker_env(**extra))
        assert web.versions() == worker.versions()
        web_face = web.face(FakeClock(start=NOW))
        worker_face = worker.face(FakeClock(start=NOW))
        assert web_face.variant_id == worker_face.variant_id
        assert web_face.cluster_name == worker_face.cluster_name
    assert (
        select_profile(_web_env(**{CLUSTER_ENV: NAME})).versions()
        != select_profile(_worker_env()).versions()
    )


def test_profile_reads_the_given_mapping_not_the_process_environment(monkeypatch):
    monkeypatch.setenv(CLUSTER_ENV, "not a valid name")
    selected = select_profile(_web_env())  # no error: os.environ is not read
    assert selected.versions()["prompt_revision"] == EXT_PROMPT_REVISION
    monkeypatch.setenv(CLUSTER_ENV, OTHER_NAME)
    assert select_profile(_web_env()).face(FakeClock(start=NOW)).cluster_name is None
    monkeypatch.delenv(CLUSTER_ENV)
    named = select_profile(_web_env(**{CLUSTER_ENV: NAME}))
    assert named.face(FakeClock(start=NOW)).cluster_name == NAME


class _GateStore:
    """Just enough store for ``Worker.resume``'s versions gate: the Run's
    recorded versions, and the claim the gate makes to block it."""

    run_id = UUID("00000000-0000-0000-0000-000000000722")

    def __init__(self, recorded: dict[str, str]):
        self.recorded = dict(recorded)
        self.claims: list[dict[str, str]] = []

    def recovery_metadata(self, incident_id: UUID) -> dict[str, Any]:
        return {"run_id": self.run_id, "versions": dict(self.recorded)}

    def claim(self, incident_id, run_id, owner, versions, lease_seconds):
        self.claims.append(dict(versions))
        return None


def _resume_code(
    recorded: dict[str, str], worker_versions: dict[str, str]
) -> str | None:
    store = _GateStore(recorded)
    worker = Worker.create(store, worker_versions)  # type: ignore[arg-type]
    incident = UUID("00000000-0000-0000-0000-000000007220")
    try:
        worker.resume(incident)
    except PersistenceError as error:
        if str(error) == "INCOMPATIBLE_STATE":
            assert store.claims == [worker_versions]
            return "INCOMPATIBLE_STATE"
        return None
    except Exception:  # past the gate, the fake store has nothing to recover
        return None
    return None


def test_web_set_worker_unset_or_the_reverse_is_blocked_on_claim():
    # ASSUMPTION: the web records ``profile.versions()`` and the worker
    # compares its own through the existing ``Worker.resume`` gate (C3 §5,
    # 「既有 versions 比对，不新增代码路径」).
    web_set = select_profile(_web_env(**{CLUSTER_ENV: NAME})).versions()
    web_unset = select_profile(_web_env()).versions()
    worker_set = select_profile(_worker_env(**{CLUSTER_ENV: NAME})).versions()
    worker_unset = select_profile(_worker_env()).versions()
    worker_other = select_profile(_worker_env(**{CLUSTER_ENV: OTHER_NAME})).versions()
    assert _resume_code(web_set, worker_unset) == "INCOMPATIBLE_STATE"
    assert _resume_code(web_unset, worker_set) == "INCOMPATIBLE_STATE"
    assert _resume_code(web_set, worker_set) is None
    assert _resume_code(web_unset, worker_unset) is None
    # CN-11: different names on the two sides are not blocked.
    assert _resume_code(web_set, worker_other) is None


# -- 6. deploy wiring unchanged (CN-14) ---------------------------------------


def test_entrypoint_does_not_import_the_cluster_name_from_the_worker_env_file(tmp_path):
    entrypoint = Path(__file__).resolve().parents[1] / "deploy" / "docker-entrypoint.sh"
    worker_env = tmp_path / "worker_env"
    worker_env.write_text(
        f"{CLUSTER_ENV}={NAME}\nOPSPILOT_OTEL_PROMETHEUS_URL=http://127.0.0.1:19090\n"
    )
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
    assert env["OPSPILOT_OTEL_PROMETHEUS_URL"] == "http://127.0.0.1:19090"
    assert CLUSTER_ENV not in env
