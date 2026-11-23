"""Independent contract tests for C2b (#566): the ``fetch_skill`` tool, its
switch ``OPSPILOT_KNOWLEDGE_TOOL``, the v7 runbook catalog in the Run input,
the catalog message and the conditional ``prompt_revision`` key.

Written from the C2 task record (docs/tasks/2026-11-19-tool-expansion-c2.md,
sections 5-6 and 「C2b 签名级细节」), decision r11 C2-U1=a / C2-U2=a and the
C2 pre-review (C2-0 .. C2-18), without reading the implementation. Red until
C2b lands; collection must stay green. The PostgreSQL half (real lookups,
the catalog the web writes, successor Runs, F13 steps 2/3 and F7 step 2 on
the new source) is ``tests/acceptance/test_566_runbook_fetch_postgres.py``.

Pinned values (``PINNED``, ``BASE_PINNED``) were computed on origin/main
``8c68d9f1`` + interface commit ``b1c5563a`` (before any C2b code) with the
helpers below (``NOW``, ``RUN_ID``, the fixed ``input_for`` arguments): with
the switch off, all 16 extension backend sets, ``otel-demo`` and
``fixture`` keep their versions (``tool_schema_revision``,
``prompt_revision``, ``context_policy_revision``), tool surface, input bytes
and initial model messages byte for byte.

No PostgreSQL, no network, no model. Assumptions beyond the interface are
marked ``# ASSUMPTION:``.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

import pytest

from opspilot.investigation.context import (
    KNOWLEDGE,
    KNOWLEDGE_BOUNDARY,
    ContextError,
    InvestigationInput,
    initial_messages,
)
from opspilot.knowledge.hints import unavailable_fact
from opspilot.tools import ToolRequest
from opspilot.tools.outcomes import Window
from opspilot.tools.profiles import PROFILE_ENV, select_profile
from opspilot.tools.registry import canonical
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import TARGET_ID, FakeStore, _lease

SWITCH = "OPSPILOT_KNOWLEDGE_TOOL"
LIVE_SWITCH = "OPSPILOT_K8S_LIVE_METRICS"
EXT = "otel-demo-ext"
TOOL = "fetch_skill"
V7 = "opspilot-investigation-input-v7"
CATALOG_KEY = "runbook_catalog"
POLICY = "opspilot-runbook-catalog-v1"
DAY = 86400

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
RETRIEVED = datetime(2026, 11, 20, 11, 59, 30, tzinfo=timezone.utc)
RUN_ID = "c2b-run"
QUESTION = "Why is checkout degraded?"
_NS = UUID("72756e62-6f6f-6b2d-6332-620000000566")

BACKEND_URLS = {
    "OPSPILOT_ES_URL": "http://127.0.0.1:9200",
    "OPSPILOT_LOKI_URL": "http://127.0.0.1:3100",
    "OPSPILOT_VICTORIALOGS_URL": "http://127.0.0.1:9428",
}

CONTEXT_POLICY = "ctx-ctx-policy-v1-a30e65ea52f4"
EXT_PROMPT_REVISION = "prompt-replay-candidate-k8s-068c56342888"
BASE_PROMPT_REVISION = "prompt-replay-candidate-f3c3018f981b"
EXT_MESSAGES_SHA256 = "9561adb973434550006a3adaf2a8ffe48ebb16e9267d3fff0d73d92f685b5ec0"
BASE_MESSAGES_SHA256 = (
    "4aa59670fc0455e02c07ad397480b018f63d8275b0653eff4636de7bfd496fb7"
)

# (elasticsearch, loki, victorialogs, live_metrics):
#     (tool_schema_revision, surface sha256, input sha256)
PINNED: dict[tuple[bool, bool, bool, bool], tuple[str, str, str]] = {
    (False, False, False, False): (
        "otel-demo-ext-b90c54386594",
        "ac39fe6525f38846a79ca6112ac82bba23590e4bb57724f471fa968b7857299d",
        "d7fe0c2c35c98d1fbda682456ba7286a948d0d5d28c67899be5b01b2b4261372",
    ),
    (False, False, False, True): (
        "otel-demo-ext-0d20c18e8654",
        "e4dbed98fae6a85f3190db6b610522f2616ede227c9edff97b158af4b5cc957e",
        "95be30ae7cda464ee96701f3e4b6da730bca15c032db412a58d7a80444da8ea1",
    ),
    (False, False, True, False): (
        "otel-demo-ext-4f224386bf21",
        "7fe5ea3b2bec1086181564a3283ae31f4d4a00fe6e5097da37b686c4f2cf9682",
        "fade49280b0ac96bb281bcce8ca34b4a920017771f92ec93848d89e821b41ad1",
    ),
    (False, False, True, True): (
        "otel-demo-ext-054b02678630",
        "193d9b72d3f96e032916e87f1e3b0350713ab08953de12242c07f6fc1562da00",
        "10bce83410f32e0c2fd387cdeea2b3b7ecf77b03ef2cbc1e9e3de6f7187a969e",
    ),
    (False, True, False, False): (
        "otel-demo-ext-06a1afb380f4",
        "89183f4cc25538fdc8acc732d5cc5409af2fee32b44c2899590dc7aa4396c92f",
        "3ac8589cd6503155fe6107468eec0723df7c930f138ba82503b96ed2f315dd69",
    ),
    (False, True, False, True): (
        "otel-demo-ext-11a106a0685b",
        "8a278823d0c81c3f91633cbf95750207a711c4e717ede7dbb09584510b06d8ed",
        "a88af49863212cd5f8c6fc2be1a02f973118cd7874fa365c041dd182ee53265b",
    ),
    (False, True, True, False): (
        "otel-demo-ext-2fe3cc8565cf",
        "4d97b14c6e3ba055674a6e83b28b4d1946ce3fa6b728443694939bed954bed81",
        "09fd5bbb9574779b4ced5ea6d5bc4279087646eacd543bc30f0fde57b9e7974c",
    ),
    (False, True, True, True): (
        "otel-demo-ext-41881b66f4ad",
        "4d16ccdb5def940db7d322f64ff899dea23bc8f7cae86495c9a96f8d8a196b2d",
        "1c892ad9cf23e1507ba1d600b7c8ed9c6c996d8cc33d5a0102e8f81ff5eb3893",
    ),
    (True, False, False, False): (
        "otel-demo-ext-5b9fc1dc2f9d",
        "8c8334ffe5605e808f9667fbf7c5fb6b5c8d22b498d4858b8e63d457222ad3e0",
        "a5a9acd71bd063148cfc827e49867fae5debaff219c939e826b17383c38fb6af",
    ),
    (True, False, False, True): (
        "otel-demo-ext-8a6002b70251",
        "7193417c7b2b431d07ddd1b63e804083feee1e37f19c75fa65dad7a6fc38cfbd",
        "60fdcf636453921d191dec3ac80443879019bc2bdbe3ee73d0706f2b5fe96772",
    ),
    (True, False, True, False): (
        "otel-demo-ext-6491e6870d8e",
        "72c3fe8ee668885962658f9097018d18cb4111df424d2bd61b9a6d2a73c3eb41",
        "50b583a1835efeec34c5590c90d09538ae3a645bfdd5ac2d0613948dd137e89b",
    ),
    (True, False, True, True): (
        "otel-demo-ext-c881d53e2a6b",
        "e13ba5a57f5e2f1a61daa698ddd338358937c9111667c50d8ab4693e8856c0fa",
        "5f01855da1e65f82bb4888158ce38d6c6c7e5dbac477122a7e6da7b13232840d",
    ),
    (True, True, False, False): (
        "otel-demo-ext-9b0915f34773",
        "e6acdb98673244403a39050465cd720e06b3663cf98bceb4f62d62ea8649c040",
        "2d3ccd880007f4870df9efdb8f051ede04492cc4e3f51918d3d8a24f0fd28740",
    ),
    (True, True, False, True): (
        "otel-demo-ext-fb3f199cf32a",
        "b5ba52fa59d425692c61f3ead0729c32cbc975e42e58f1b5d02812b692443b08",
        "0f08891195c71bc3f5b105b118642d41ac9b81d5816f42af4a233afbe56f3389",
    ),
    (True, True, True, False): (
        "otel-demo-ext-935aff9fd421",
        "76bbb8cbf0e636221e8b6360e1f9454cf658cefbfbe5e83e402f6c3b35098480",
        "7676dce5538ac465d39271629a738d501395ad8afc8b1866df935960bfc90991",
    ),
    (True, True, True, True): (
        "otel-demo-ext-12bafadea56c",
        "750ca9ebc5320be22a4692013fe71c568d00aac5e190bd8c56ddeee46ec22257",
        "d0a82a96eb3061e6055454d952bc62d96221b5068e5b93991b83d3dee8c584f1",
    ),
}
# profile: (tool_schema_revision, surface sha256, input sha256)
BASE_PINNED = {
    "fixture": (
        "fixture-5",
        "e891566cf26698a2c8f949139ead032ea5794e37c743aab61013433cef6f025f",
        "96c7053481005a5b30d702a3ec001e31e2c7020135727eee51ecb071a43888e4",
    ),
    "otel-demo": (
        "otel-demo-a9d431ec0dfb",
        "df2a393eabb14eac7ea5927d7ae9a3f948a13f6bf143810a1e809bcd6004177b",
        "ac2cae4d0900f2e7a5a9f76c1661452b3a5180bc14102c83f8471aa1c1ee9420",
    ),
}

# Upstream's forced answer frame (skills_fetcher.py:198-231) must not be
# carried over (C2-9).
UPSTREAM_FORMAT_MARKERS = ("I found a skill named", "✅", "❌")


@pytest.fixture(autouse=True)
def _clean_environment(tmp_path, monkeypatch):
    # The profile reads the mapping it is given; nothing leaks in.
    for name in (SWITCH, LIVE_SWITCH, *BACKEND_URLS):
        monkeypatch.delenv(name, raising=False)
    placeholder = tmp_path / "synthetic-ca-placeholder.pem"
    placeholder.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("C2B_TEST_PLACEHOLDER_CA", str(placeholder))


# -- helpers -------------------------------------------------------------------


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _env(
    es: bool = False,
    loki: bool = False,
    vlogs: bool = False,
    live: bool = False,
    *,
    switch: str | None = None,
    profile: str = EXT,
) -> dict[str, str]:
    env = {PROFILE_ENV: profile}
    for flag, name in zip((es, loki, vlogs), BACKEND_URLS):
        if flag:
            env[name] = BACKEND_URLS[name]
    if live:
        env[LIVE_SWITCH] = "1"
    if switch is not None:
        env[SWITCH] = switch
    return env


def _face(env: Mapping[str, str]):
    return select_profile(env).face(FakeClock(start=NOW))


def _tool_names(env: Mapping[str, str]) -> list[str]:
    return [s["function"]["name"] for s in _face(env).tool_schemas]


def _available(env: Mapping[str, str]) -> Mapping[str, Any]:
    return _face(env).evidence_context(RUN_ID)["data_sources"]["available"]


def _surface_sha256(env: Mapping[str, str]) -> str:
    profile = select_profile(env)
    face = profile.face(FakeClock(start=NOW))
    blob = {
        "tool_schemas": [dict(item) for item in face.tool_schemas],
        "variant_id": face.variant_id,
        "evidence_context": dict(face.evidence_context(RUN_ID)),
        "versions": profile.versions(),
    }
    return _sha(canonical(blob))


def _input(env: Mapping[str, str]) -> InvestigationInput:
    return _face(env).input_for(
        run_id=RUN_ID,
        question=QUESTION,
        target_id="checkout-prod",
        deadline=NOW + timedelta(minutes=10),
        model_requests=12,
    )


def _with_facts(base: InvestigationInput, **facts: Any) -> InvestigationInput:
    return replace(base, scope_facts={**base.scope_facts, **facts})


def _catalog_entry(
    name: str,
    *,
    revision: int = 1,
    age: int = 3600,
    max_age: int = DAY,
    description: str | None = None,
) -> dict[str, Any]:
    """One catalog entry, internally consistent: ``age_seconds`` is the
    whole-second distance from ``RETRIEVED`` to ``approved_at`` and
    ``freshness`` follows the F13 step 4 rule (stale when older than the
    threshold)."""
    return {
        "skill_id": name,
        "entry_id": str(uuid5(_NS, name)),
        "revision": revision,
        "description": description or f"How to investigate {name}.",
        "approved_at": (RETRIEVED - timedelta(seconds=age)).isoformat(),
        "age_seconds": age,
        "freshness": "stale" if age > max_age else "fresh",
    }


def _catalog(*entries: dict[str, Any], omitted: int = 0) -> dict[str, Any]:
    return {
        "policy_version": POLICY,
        "status": "ok",
        "retrieved_at": RETRIEVED.isoformat(),
        "max_age_seconds": DAY,
        "omitted_for_size": omitted,
        "entries": copy.deepcopy(list(entries)),
    }


def _visible(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {k: entry[k] for k in ("skill_id", "description", "revision", "freshness")}


A = _catalog_entry("checkout-errors")
B = _catalog_entry("payment-timeouts", revision=3, age=DAY + 120)
C = _catalog_entry("signup-crashloop", revision=2)


def _boundary() -> str:
    from opspilot.investigation.context import RUNBOOK_CATALOG_BOUNDARY

    return RUNBOOK_CATALOG_BOUNDARY


def _catalog_messages(messages: list[dict[str, Any]]) -> list[int]:
    boundary = _boundary()
    return [
        i
        for i, m in enumerate(messages)
        if isinstance(m.get("content"), str) and m["content"].startswith(boundary)
    ]


# -- the switch: 1 on; unset, empty, 0 off; anything else fails both starts --------


def test_switch_constants_and_parser():
    from opspilot.tools import knowledge_fetch

    assert knowledge_fetch.ENV == SWITCH
    assert knowledge_fetch.TOOL_NAME == TOOL
    assert tuple(knowledge_fetch.TOOL_NAMES) == (TOOL,)
    assert knowledge_fetch.knowledge_tool_enabled({SWITCH: "1"}) is True
    for off in ({}, {SWITCH: ""}, {SWITCH: "0"}):
        assert knowledge_fetch.knowledge_tool_enabled(off) is False, off


INVALID_SWITCH = [
    "on",
    "true",
    "yes",
    "TRUE",
    "2",
    "01",
    " 1",
    "1 ",
    "-1",
    "1.0",
    "off",
]


@pytest.mark.parametrize("value", INVALID_SWITCH)
def test_the_parser_refuses_every_other_value(value):
    from opspilot.tools import knowledge_fetch

    # ASSUMPTION: the same SystemExit the other startup switches end with
    # (``kubernetes_live_metrics.live_metrics_enabled``); any BaseException
    # carrying the code is accepted.
    with pytest.raises(BaseException, match="KNOWLEDGE_TOOL_INVALID"):
        knowledge_fetch.knowledge_tool_enabled({SWITCH: value})


def _web_start(env: Mapping[str, str]) -> None:
    profile = select_profile(env)
    profile.versions()
    profile.face(FakeClock(start=NOW)).tool_schemas  # noqa: B018


@pytest.mark.parametrize("value", INVALID_SWITCH)
def test_an_invalid_switch_value_fails_the_web_start(value):
    with pytest.raises(BaseException, match="KNOWLEDGE_TOOL_INVALID"):
        _web_start(_env(switch=value))


def _worker_env(**overrides: str) -> dict[str, str]:
    values = {
        PROFILE_ENV: EXT,
        SWITCH: "1",
        "OPSPILOT_K8S_API_URL": "https://127.0.0.1:16443",
        "OPSPILOT_K8S_TOKEN": "k8s-ro-aaaaaaaa566",
        "OPSPILOT_K8S_CA_FILE": os.environ["C2B_TEST_PLACEHOLDER_CA"],
        "OPSPILOT_K8S_NAMESPACE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
    }
    values.update(overrides)
    return values


def _require_tools() -> None:
    if shutil.which("jq") is None or shutil.which("python3") is None:
        pytest.skip("the extension worker needs jq and python3 on PATH")


@pytest.mark.parametrize("value", ["on", "true", " 1"])
def test_an_invalid_switch_value_fails_the_worker_start(value):
    _require_tools()
    with pytest.raises(BaseException, match="KNOWLEDGE_TOOL_INVALID"):
        profile = select_profile(_worker_env(**{SWITCH: value}))
        lease = _lease()
        store = FakeStore(lease, deadline=NOW + timedelta(minutes=10))
        profile.executor_factory(store, RecordingSink(), FakeClock(start=NOW))


@pytest.mark.parametrize("value", [None, "", "0"])
def test_switch_off_values_leave_the_extension_byte_identical(value):
    env = _env(switch=value)
    revision, surface, input_sha = PINNED[(False, False, False, False)]
    versions = select_profile(env).versions()
    assert versions == {
        "context_policy_revision": CONTEXT_POLICY,
        "prompt_revision": EXT_PROMPT_REVISION,
        "tool_schema_revision": revision,
    }
    assert _surface_sha256(env) == surface
    built = _input(env)
    assert built.version == "opspilot-investigation-input-v1"
    assert _sha(canonical(built.as_json())) == input_sha
    messages, _system = initial_messages(built, evidence_context=None)
    assert _sha(canonical(messages)) == EXT_MESSAGES_SHA256
    assert TOOL not in _tool_names(env)
    assert "knowledge" not in _available(env)


@pytest.mark.parametrize("flags", sorted(PINNED))
def test_switch_off_keeps_all_sixteen_backend_sets_byte_identical(flags):
    from opspilot.tools import otel_demo_ext

    revision, surface, input_sha = PINNED[flags]
    es, loki, vlogs, live = flags
    for off in (None, "", "0"):
        env = _env(*flags, switch=off)
        assert select_profile(env).versions() == {
            "context_policy_revision": CONTEXT_POLICY,
            "prompt_revision": EXT_PROMPT_REVISION,
            "tool_schema_revision": revision,
        }, off
        assert _surface_sha256(env) == surface, off
        assert _sha(canonical(_input(env).as_json())) == input_sha, off
    assert (
        otel_demo_ext.tool_schema_revision(
            elasticsearch=es,
            loki=loki,
            victorialogs=vlogs,
            live_metrics=live,
            knowledge=False,
        )
        == revision
    )
    assert otel_demo_ext.otel_demo_ext_versions(
        elasticsearch=es, loki=loki, victorialogs=vlogs, live_metrics=live
    ) == otel_demo_ext.otel_demo_ext_versions(
        elasticsearch=es,
        loki=loki,
        victorialogs=vlogs,
        live_metrics=live,
        knowledge=False,
    )


@pytest.mark.parametrize("profile", sorted(BASE_PINNED))
@pytest.mark.parametrize("value", [None, "1", "garbage"])
def test_the_switch_is_read_only_in_the_extension_profile(profile, value):
    revision, surface, input_sha = BASE_PINNED[profile]
    env = _env(switch=value, profile=profile)
    assert select_profile(env).versions() == {
        "context_policy_revision": CONTEXT_POLICY,
        "prompt_revision": BASE_PROMPT_REVISION,
        "tool_schema_revision": revision,
    }
    assert _surface_sha256(env) == surface
    built = _input(env)
    assert _sha(canonical(built.as_json())) == input_sha
    messages, _system = initial_messages(built, evidence_context=None)
    assert _sha(canonical(messages)) == BASE_MESSAGES_SHA256
    assert TOOL not in _tool_names(env)


@pytest.mark.parametrize("flags", sorted(PINNED))
def test_switch_on_appends_fetch_skill_last_and_lists_it_under_knowledge(flags):
    off = _env(*flags)
    on = _env(*flags, switch="1")
    old_names, new_names = _tool_names(off), _tool_names(on)
    # Fixed order ES -> Loki -> VictoriaLogs -> live usage -> knowledge.
    assert new_names == [*old_names, TOOL]
    old_available, new_available = _available(off), _available(on)
    assert set(new_available) == set(old_available) | {"knowledge"}
    assert new_available["knowledge"] == [TOOL]
    for key in old_available:
        assert new_available[key] == old_available[key], key
    # The face's other evidence-context fields are untouched.
    old_context = dict(_face(off).evidence_context(RUN_ID))
    new_context = dict(_face(on).evidence_context(RUN_ID))
    old_context.pop("data_sources")
    new_context.pop("data_sources")
    assert new_context == old_context
    revision = select_profile(on).versions()["tool_schema_revision"]
    assert revision.startswith("otel-demo-ext-")
    assert revision != PINNED[flags][0]


def test_every_set_with_and_without_the_switch_has_its_own_revision():
    from opspilot.tools import otel_demo_ext

    revisions = set()
    for flags in PINNED:
        es, loki, vlogs, live = flags
        for switch in ("0", "1"):
            revision = select_profile(_env(*flags, switch=switch)).versions()[
                "tool_schema_revision"
            ]
            assert revision == otel_demo_ext.tool_schema_revision(
                elasticsearch=es,
                loki=loki,
                victorialogs=vlogs,
                live_metrics=live,
                knowledge=switch == "1",
            )
            revisions.add(revision)
    assert len(revisions) == 32


def test_switch_only_constants():
    from opspilot.tools import otel_demo_ext

    on = select_profile(_env(switch="1"))
    assert (
        otel_demo_ext.KNOWLEDGE_TOOL_SCHEMA_REVISION
        == on.versions()["tool_schema_revision"]
        == otel_demo_ext.tool_schema_revision(knowledge=True)
    )
    assert tuple(otel_demo_ext.KNOWLEDGE_TOOL_NAMES) == (
        *tuple(otel_demo_ext.TOOL_NAMES),
        TOOL,
    )
    assert otel_demo_ext.TOOL_SCHEMA_REVISION == PINNED[(False,) * 4][0]


def test_web_on_and_worker_off_record_different_versions():
    # A Run recorded by a web with the switch is blocked on claim by a worker
    # without it (C3 §5): both the tool and the prompt revision differ.
    on = select_profile(_env(switch="1")).versions()
    off = select_profile(_env()).versions()
    assert on["tool_schema_revision"] != off["tool_schema_revision"]
    assert on["prompt_revision"] != off["prompt_revision"]
    assert on["context_policy_revision"] == off["context_policy_revision"]
    # The web (switch only) and the worker (switch and cluster) agree.
    assert select_profile(_worker_env()).versions() == on


def test_entrypoint_passes_the_switch_to_the_worker(tmp_path):
    entrypoint = Path(__file__).resolve().parents[1] / "deploy" / "docker-entrypoint.sh"
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


# -- the tool's face and registration ------------------------------------------------


def _schema() -> Mapping[str, Any]:
    [schema] = [
        s for s in _face(_env(switch="1")).tool_schemas if s["function"]["name"] == TOOL
    ]
    return schema


def test_the_schema_takes_one_bounded_skill_id_and_nothing_else():
    function = _schema()["function"]
    parameters = function["parameters"]
    assert set(parameters) == {"type", "properties", "required", "additionalProperties"}
    assert parameters["type"] == "object"
    assert parameters["required"] == ["skill_id"]
    assert parameters["additionalProperties"] is False
    assert set(parameters["properties"]) == {"skill_id"}
    skill_id = dict(parameters["properties"]["skill_id"])
    assert isinstance(skill_id.pop("description"), str)
    assert skill_id == {"type": "string", "minLength": 1, "maxLength": 200}


def test_the_description_lists_no_names_and_carries_no_upstream_answer_format():
    function = _schema()["function"]
    text = json.dumps(function, ensure_ascii=False)
    assert isinstance(function.get("description"), str) and function["description"]
    for marker in UPSTREAM_FORMAT_MARKERS:
        assert marker not in text, marker
    # The schema is static: the same bytes whatever is in any catalog, so it
    # cannot list the names (C2-9); every switch-on face carries the same one.
    for flags in PINNED:
        [other] = [
            s
            for s in _face(_env(*flags, switch="1")).tool_schemas
            if s["function"]["name"] == TOOL
        ]
        assert canonical(dict(other)) == canonical(dict(_schema()))


def test_the_registration_is_a_bounded_read_only_knowledge_get():
    from opspilot.tools import knowledge_fetch, otel_demo, otel_demo_ext

    reg = knowledge_fetch.fetch_skill_registration()
    assert reg.name == TOOL
    assert reg.version == "v1"
    # Lead ruling (implementation decision 11): the executor requires a
    # registration's source to equal its target's (executor.py:781), and the
    # profile's only target is the lab one; the knowledge origin is carried
    # by each row's ``kind`` and ``source`` fields (asserted on the real
    # path in tests/acceptance/test_566_runbook_fetch_postgres.py).
    assert reg.source == otel_demo.SOURCE
    assert reg.verb == "get"
    assert reg.read_only is True
    assert reg.result_path == ("results",)
    assert reg.may_contain_secrets is True
    assert reg.request_timeout_seconds == 5
    assert reg.max_result_bytes == 64 * 1024
    # The profile registers exactly that registration, last, only when on.
    # ASSUMPTION: the private registry seam mirrors tool_schema_revision, as
    # ``_registrations(live_metrics=)`` does for B3.
    on = otel_demo_ext._registrations(knowledge=True)
    assert on[-1].name == TOOL
    assert on[-1] == reg
    assert TOOL not in {r.name for r in otel_demo_ext._registrations()}


# -- fetch_skill refusals that never reach PostgreSQL ----------------------------------

#: Nothing listens here: any SQL the tool sent would fail as a source
#: outage, never as the documented pre-request codes.
DEAD_DSN = "postgresql://c2b@127.0.0.1:1/none?connect_timeout=1"
FRAME = Window(NOW - timedelta(hours=6), NOW)


class _DsnStore(FakeStore):
    """``FakeStore`` with the worker's connection string.

    # ASSUMPTION: the worker builds ``KnowledgeTransport(dsn)`` from the
    # ``DurableStore`` its executor factory is given (``store.dsn``); adapting
    # this seam is not an assertion change.
    """

    dsn = DEAD_DSN


def _executor(input_change):
    _require_tools()
    clock = FakeClock(start=NOW)
    profile = select_profile(_worker_env())
    lease = _lease()
    deadline = NOW + timedelta(minutes=10)
    store = _DsnStore(lease, deadline=deadline)
    factory = profile.executor_factory(store, RecordingSink(), clock)
    built = profile.face(clock).input_for(
        run_id=str(lease.run_id),
        question=QUESTION,
        target_id=TARGET_ID,
        deadline=deadline,
        model_requests=12,
    )
    built = _with_facts(built, **{CATALOG_KEY: _catalog(A)})
    return factory(lease, input_change(built))


def _fetch(executor, params: dict[str, Any]):
    return executor.execute(
        ToolRequest(
            step_id="c2b-step",
            tool_index=0,
            tool_name=TOOL,
            target_ref=TARGET_ID,
            params=params,
            window=FRAME.as_json(),
        )
    )


def _view(outcome) -> Mapping[str, Any]:
    assert outcome.model_view is not None
    return outcome.model_view


def _text(outcome) -> str:
    return json.dumps(_view(outcome), ensure_ascii=False, sort_keys=True)


def test_without_a_bound_target_the_tool_reports_not_found_without_sql():
    executor = _executor(lambda built: replace(built, bound_target_id=None))
    outcome = _fetch(executor, {"skill_id": "checkout-errors"})
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), _view(
        outcome
    )
    assert "KNOWLEDGE_NOT_FOUND" in _text(outcome)
    assert _view(outcome).get("content") is None


@pytest.mark.parametrize(
    "skill_id",
    ["abc\u0000def", "two\nlines", "tab\tbed", "zero​width", "bell\u0007"],
)
def test_unprintable_skill_ids_are_refused_before_any_request(skill_id):
    executor = _executor(lambda built: built)
    outcome = _fetch(executor, {"skill_id": skill_id})
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), _view(
        outcome
    )
    assert "INVALID_SKILL_ID" in _text(outcome)
    assert _view(outcome).get("content") is None


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"skill_id": ""},
        {"skill_id": "x" * 201},
        {"skill_id": 7},
        {"skill_id": "checkout-errors", "target_id": "payments-prod"},
        {"skill_id": "checkout-errors", "resource_uid": "payments-prod"},
    ],
)
def test_parameters_outside_the_schema_are_refused_before_any_request(params):
    executor = _executor(lambda built: built)
    outcome = _fetch(executor, params)
    # Schema refusals are the executor's (``denied`` or ``INVALID_PARAMS``,
    # as for every other tool); either way nothing is read.
    assert outcome.status in ("error", "denied"), _view(outcome)
    assert outcome.status != "error" or outcome.reason == "INVALID_PARAMS", _view(
        outcome
    )
    assert _view(outcome).get("content") is None


# -- input snapshot v7 -------------------------------------------------------------


def test_v7_constants():
    from opspilot.investigation import context

    assert context.INPUT_VERSION_RUNBOOK_CATALOG == V7
    assert context.RUNBOOK_CATALOG == CATALOG_KEY
    # v7 is known exactly once and ranks above v6; later inputs (v8, #722)
    # may follow it without changing what v7 means.
    known = list(context.KNOWN_INPUT_VERSIONS)
    assert known.count(V7) == 1
    assert known.index(V7) > known.index("opspilot-investigation-input-v6")
    boundary = context.RUNBOOK_CATALOG_BOUNDARY
    assert isinstance(boundary, str) and boundary
    lower = boundary.lower()
    # 第 6 节: untrusted data, not instructions, not this Run's evidence.
    for word in ("untrusted", "instruction", "evidence"):
        assert word in lower, word


def test_the_catalog_helpers_build_valid_facts():
    from opspilot.knowledge.catalog import unavailable_catalog, valid_catalog

    assert valid_catalog(_catalog(A, B, C)) is True
    assert valid_catalog(_catalog()) is True
    down = unavailable_catalog(DAY)
    assert down["policy_version"] == POLICY
    assert down["status"] == "unavailable"
    assert down["max_age_seconds"] == DAY
    assert down["entries"] == []
    assert down["omitted_for_size"] == 0
    assert valid_catalog(down) is True


def test_a_catalog_fact_makes_the_input_v7_and_round_trips():
    base = _input(_env(switch="1"))
    built = _with_facts(base, **{CATALOG_KEY: _catalog(A, B)})
    assert built.version == V7
    parsed = InvestigationInput.from_json(built.as_json())
    assert parsed.version == V7
    assert parsed.scope_facts[CATALOG_KEY] == _catalog(A, B)
    assert canonical(parsed.as_json()) == canonical(built.as_json())


def test_v7_outranks_the_knowledge_fact_and_absence_keeps_the_old_version():
    base = _input(_env(switch="1"))
    v5 = _with_facts(base, **{KNOWLEDGE: unavailable_fact(DAY)})
    assert v5.version == "opspilot-investigation-input-v5"
    both = _with_facts(v5, **{CATALOG_KEY: _catalog(A)})
    assert both.version == V7
    from opspilot.knowledge.catalog import unavailable_catalog

    down = _with_facts(base, **{CATALOG_KEY: unavailable_catalog(DAY)})
    assert down.version == V7  # the fact is there, even when unavailable


def test_a_v7_row_without_the_fact_or_a_v6_row_with_it_is_refused():
    base = _input(_env(switch="1"))
    row = _with_facts(base, **{CATALOG_KEY: _catalog(A)}).as_json()
    without = copy.deepcopy(row)
    del without["scope_facts"][CATALOG_KEY]
    with pytest.raises(ContextError, match="INPUT_INVALID"):
        InvestigationInput.from_json(without)
    mislabelled = copy.deepcopy(row)
    mislabelled["version"] = "opspilot-investigation-input-v6"
    with pytest.raises(ContextError, match="INPUT_INVALID"):
        InvestigationInput.from_json(mislabelled)


def _unsorted(fact):
    fact["entries"] = list(reversed(fact["entries"]))


def _duplicate(fact):
    fact["entries"] = [fact["entries"][0], copy.deepcopy(fact["entries"][0])]


def _too_many(fact):
    fact["entries"] = [
        _catalog_entry(f"rb-{i:03d}", description="x") for i in range(51)
    ]


def _too_big(fact):
    fact["entries"] = [
        _catalog_entry(f"rb-{i:03d}", description="d" * 300) for i in range(50)
    ]


def _wrong_freshness(fact):
    fact["entries"][1]["freshness"] = "fresh"  # B is older than the threshold


def _inconsistent_age(fact):
    fact["entries"][0]["age_seconds"] += 600


def _bad_revision(fact):
    fact["entries"][0]["revision"] = 0


def _bad_entry_id(fact):
    fact["entries"][0]["entry_id"] = "not-a-uuid"


def _bad_policy(fact):
    fact["policy_version"] = "opspilot-runbook-catalog-v0"


def _bad_status(fact):
    fact["status"] = "partial"


def _missing_key(fact):
    del fact["omitted_for_size"]


def _negative_omitted(fact):
    fact["omitted_for_size"] = -1


def _extra_entry_key(fact):
    fact["entries"][0]["content"] = "# full runbook text"


def _unavailable_with_entries(fact):
    fact["status"] = "unavailable"


TAMPERS = [
    _unsorted,
    _duplicate,
    _too_many,
    _too_big,
    _wrong_freshness,
    _inconsistent_age,
    _bad_revision,
    _bad_entry_id,
    _bad_policy,
    _bad_status,
    _missing_key,
    _negative_omitted,
    _extra_entry_key,
    _unavailable_with_entries,
]


@pytest.mark.parametrize("tamper", TAMPERS, ids=lambda f: f.__name__)
def test_a_tampered_catalog_is_refused(tamper):
    from opspilot.knowledge.catalog import valid_catalog

    fact = _catalog(A, B)
    tamper(fact)
    assert valid_catalog(fact) is False
    base = _input(_env(switch="1"))
    with pytest.raises(ContextError, match="INPUT_INVALID"):
        _with_facts(base, **{CATALOG_KEY: fact})
    row = _with_facts(base, **{CATALOG_KEY: _catalog(A, B)}).as_json()
    row["scope_facts"][CATALOG_KEY] = fact
    with pytest.raises(ContextError, match="INPUT_INVALID"):
        InvestigationInput.from_json(row)


def test_fifty_entries_within_eight_kb_are_valid():
    from opspilot.knowledge.catalog import valid_catalog

    entries = [_catalog_entry(f"rb-{i:03d}", description="short") for i in range(50)]
    assert valid_catalog(_catalog(*entries, omitted=7)) is True


# -- the catalog message --------------------------------------------------------------


def test_a_non_empty_catalog_is_one_user_message_with_the_boundary_and_the_list():
    base = _input(_env(switch="1"))
    built = _with_facts(base, **{CATALOG_KEY: _catalog(A, B, C)})
    context = {"time_policies": [], "marker": "evidence-context"}
    messages, _system = initial_messages(built, evidence_context=context)
    [index] = _catalog_messages(messages)
    message = messages[index]
    assert message["role"] == "user"
    listed = json.loads(message["content"][len(_boundary()) :])
    assert listed == [_visible(A), _visible(B), _visible(C)]
    # Only the model-visible fields: no entry_id, age or approval instant.
    assert "entry_id" not in message["content"]
    assert A["entry_id"] not in message["content"]
    # system, question, catalog, evidence context -- nothing else here.
    assert [m["role"] for m in messages] == ["system", "user", "user", "user"]
    assert index == 2
    assert messages[-1]["content"] == canonical(context)


def _knowledge_fact() -> dict[str, Any]:
    """A valid v5 fact with one entry (the F13 step 4 shape)."""
    observed = RETRIEVED - timedelta(seconds=7200)
    entry = {
        "entry_id": str(uuid5(_NS, "pm-entry")),
        "revision": 1,
        "name": "checkout payment failures",
        "tags": ["checkout"],
        "content": canonical(
            {"symptoms": ["5xx"], "evidence_refs": {"omitted": True, "count": 1}}
        ),
        "content_sha256": _sha("pm-entry"),
        "source_incident_id": str(uuid5(_NS, "pm-incident")),
        "source_postmortem_id": str(uuid5(_NS, "pm-postmortem")),
        "source_version": 1,
        "approved_by": "alice",
        "approved_at": (observed + timedelta(minutes=5)).isoformat(),
        "observation_ended_at": observed.isoformat(),
        "age_seconds": 7200,
        "freshness": "fresh",
        "truncated": False,
    }
    return {
        "policy_version": "knowledge-hint-v1",
        "status": "ok",
        "retrieved_at": RETRIEVED.isoformat(),
        "max_age_seconds": DAY,
        "omitted_for_size": 0,
        "entries": [entry],
    }


def test_the_catalog_message_sits_after_the_knowledge_hint_and_before_the_context():
    base = _input(_env(switch="1"))
    built = _with_facts(
        base, **{KNOWLEDGE: _knowledge_fact(), CATALOG_KEY: _catalog(A)}
    )
    context = {"time_policies": [], "marker": "evidence-context"}
    messages, _system = initial_messages(built, evidence_context=context)
    [knowledge_index] = [
        i
        for i, m in enumerate(messages)
        if str(m.get("content", "")).startswith(KNOWLEDGE_BOUNDARY)
    ]
    [catalog_index] = _catalog_messages(messages)
    assert catalog_index == knowledge_index + 1
    assert catalog_index == len(messages) - 2
    assert messages[-1]["content"] == canonical(context)
    # Adding the catalog changes nothing before it.
    without, _ = initial_messages(
        _with_facts(base, **{KNOWLEDGE: _knowledge_fact()}), evidence_context=context
    )
    assert messages[:catalog_index] == without[:catalog_index]
    assert messages[catalog_index + 1 :] == without[catalog_index:]


def test_an_empty_or_unavailable_catalog_sends_no_message_and_keeps_the_bytes():
    from opspilot.knowledge.catalog import unavailable_catalog

    base = _input(_env(switch="1"))
    plain, _ = initial_messages(base, evidence_context=None)
    for fact in (_catalog(), unavailable_catalog(DAY)):
        built = _with_facts(base, **{CATALOG_KEY: fact})
        assert built.version == V7
        messages, _ = initial_messages(built, evidence_context=None)
        assert not _catalog_messages(messages)
        assert canonical(messages) == canonical(plain)


def test_the_rebuilt_prefix_is_byte_identical():
    base = _input(_env(switch="1"))
    built = _with_facts(base, **{CATALOG_KEY: _catalog(A, B, C)})
    context = {"time_policies": [], "marker": "evidence-context"}
    first, system = initial_messages(built, evidence_context=context)
    again, again_system = initial_messages(
        InvestigationInput.from_json(json.loads(json.dumps(built.as_json()))),
        evidence_context=context,
    )
    assert again_system == system
    assert canonical(again) == canonical(first)


# -- prompt_revision: the catalog boundary only when the switch is on -------------------


def _loop_templates() -> dict[str, str]:
    from opspilot.investigation import loop
    from opspilot.investigation.context import (
        ALERT_CONTEXT_BOUNDARY,
        RELEASE_CONTEXT_BOUNDARY,
    )
    from opspilot.investigation.reports import REPORT_CONTRACT

    return {
        "report_contract": REPORT_CONTRACT,
        "run_coverage_template": loop.RUN_COVERAGE_TEMPLATE,
        "report_retry_template": loop.REPORT_RETRY_TEMPLATE,
        "alert_context_template": ALERT_CONTEXT_BOUNDARY,
        "knowledge_template": KNOWLEDGE_BOUNDARY,
        "release_context_template": RELEASE_CONTEXT_BOUNDARY,
    }


@pytest.mark.parametrize(
    ("variant", "pinned"),
    [
        ("replay-candidate-k8s", EXT_PROMPT_REVISION),
        ("replay-candidate", BASE_PROMPT_REVISION),
    ],
)
def test_without_the_template_the_revision_is_byte_identical(variant, pinned):
    from opspilot.instructions.discipline import prompt_revision
    from opspilot.investigation.loop import prompt_revision_versions

    templates = _loop_templates()
    assert prompt_revision(variant, **templates) == pinned
    assert (
        prompt_revision(variant, **templates, runbook_catalog_template=None) == pinned
    )
    assert prompt_revision_versions(variant) == {"prompt_revision": pinned}
    assert prompt_revision_versions(variant, knowledge_tool=False) == {
        "prompt_revision": pinned
    }


def test_the_key_is_absent_when_off_not_an_empty_string():
    from opspilot.instructions.discipline import prompt_revision

    templates = _loop_templates()
    absent = prompt_revision("replay-candidate-k8s", **templates)
    empty = prompt_revision(
        "replay-candidate-k8s", **templates, runbook_catalog_template=""
    )
    assert empty != absent


def test_with_the_switch_the_boundary_is_part_of_the_revision():
    from opspilot.instructions.discipline import prompt_revision
    from opspilot.investigation.loop import prompt_revision_versions

    templates = _loop_templates()
    expected = prompt_revision(
        "replay-candidate-k8s", **templates, runbook_catalog_template=_boundary()
    )
    assert expected != EXT_PROMPT_REVISION
    assert prompt_revision_versions("replay-candidate-k8s", knowledge_tool=True) == {
        "prompt_revision": expected
    }
    # A different boundary text is a different revision (the template, not a
    # flag, is hashed).
    assert (
        prompt_revision(
            "replay-candidate-k8s",
            **templates,
            runbook_catalog_template=_boundary() + " ",
        )
        != expected
    )
    # The profile with the switch records exactly that value.
    for flags in itertools.islice(sorted(PINNED), 0, None, 5):
        versions = select_profile(_env(*flags, switch="1")).versions()
        assert versions["prompt_revision"] == expected, flags
        assert versions["context_policy_revision"] == CONTEXT_POLICY
