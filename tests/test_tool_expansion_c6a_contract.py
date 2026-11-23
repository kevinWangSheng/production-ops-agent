"""Independent contract tests for tool expansion C6a (#570): the read-only SQL
tools ``database_sql_query``, ``database_sql_list_tables`` and
``database_sql_describe_table`` for PostgreSQL, MySQL and MariaDB.

Written from the C6 task record (docs/tasks/2026-11-20-tool-expansion-c6.md,
「行为（公开接口，C6a）」), decision table r8 R8-16/R8-25 and r11 (C6-U1=a,
C6-0 ... C6-22 defaults), PRODUCT-CONSTRAINTS line 18 and the C6 pre-review
(C6-8 business-database isolation), without reading the implementation.
Red until C6a lands; collection must stay green.

Every tool call goes through the public path: build the ``otel-demo-ext``
profile from an environment mapping, call the tool through the executor, and
assert on the returned view, the refusal code, or the exact statements the
recording driver (``tests/c6a_sql_support.py``, through the documented
``SqlTransport(config, connector=...)`` seam) received. No database, no
container, no model. The statement check (C6-3) is in
``tests/test_tool_expansion_c6a_statement_contract.py``; the real-database
half (R8-25) is ``tests/integration/test_tool_expansion_c6a_sql.py``.

Pinned values (``PINNED``, ``BASE_PINNED``) were computed on origin/main
``9e95ed67`` + interface commit ``4fae3400`` (before any C6 code) with
``_surface_sha256`` below: without ``OPSPILOT_SQL_URL`` all 64 extension
backend/switch sets, ``otel-demo`` and ``fixture`` must stay byte for byte.

Assumptions beyond the record are marked ``# ASSUMPTION:``. All synthetic
credentials are low-entropy placeholders.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import re
import subprocess
import time
import uuid
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

import pytest

from opspilot import tracing
from opspilot.tools.profiles import PROFILE_ENV, ToolProfile, select_profile
from opspilot.tools.registry import canonical
from opspilot.tracing import check_lab_target
from tests.c6a_sql_support import (
    CANARY,
    CATALOG_MARK,
    DESCRIBE,
    EXT_PROFILE,
    K8S_API,
    K8S_TOKEN,
    LIST_TABLES,
    MARIADB_URL,
    MARIADB_VERSION,
    MYSQL_URL,
    PG_URL,
    PROM_PASSWORD,
    QUERY,
    SQL_PASSWORD,
    SQL_TOOLS,
    SQL_USERNAME,
    FakeSql,
    call,
    forbidden,
    install,
    kind,
    make_tools,
    notes,
    number_in,
    refused_before_request,
    require_jq,
    rows,
    sql_env,
    view,
    wait_until,
    worker,
)
from tests.m1_tool_support import FakeClock

PIN_NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
RUN_ID = "c6a-run"

BACKEND_URLS = {
    "OPSPILOT_ES_URL": "http://127.0.0.1:9200",
    "OPSPILOT_LOKI_URL": "http://127.0.0.1:3100",
    "OPSPILOT_VICTORIALOGS_URL": "http://127.0.0.1:9428",
}
SWITCHES = (
    "OPSPILOT_K8S_LIVE_METRICS",
    "OPSPILOT_KNOWLEDGE_TOOL",
    "OPSPILOT_TODO_WRITE",
)

# (elasticsearch, loki, victorialogs, live_metrics, knowledge, todo):
#     (tool_schema_revision, surface sha256) on main before C6.
PINNED: dict[tuple[bool, ...], tuple[str, str]] = {
    (False, False, False, False, False, False): (
        "otel-demo-ext-743e0a7a01f0",
        "3b493f35b66f30983cf700df7ea0a98d6f7d3488da3372f8a6e0c0f1acf16c01",
    ),
    (False, False, False, False, False, True): (
        "otel-demo-ext-ed1f47a0b313",
        "38ed7be7d90fba2aad33a06f239539a29a912bfbb4a90e5f83d297166cd64d75",
    ),
    (False, False, False, False, True, False): (
        "otel-demo-ext-9f73b76863f1",
        "e7bb6d87623ac995754ffdfff48699592d8d858eb21bbe77375219a402883c5f",
    ),
    (False, False, False, False, True, True): (
        "otel-demo-ext-414691912286",
        "dd8db3e274a902085f1fae28e2d2e2c4874a7e81bcc0712a04a2f5f0e6aa1da9",
    ),
    (False, False, False, True, False, False): (
        "otel-demo-ext-9fa5e3263dd6",
        "7839ade396d4bfee5263ea90429e2197a053a498b6c9bd91919cf197dc19efa0",
    ),
    (False, False, False, True, False, True): (
        "otel-demo-ext-b976e92a3aea",
        "93c529f545ad26c5d29c76c5b886ab7b5f47d935a0be972b2e29bc51ded2e1dc",
    ),
    (False, False, False, True, True, False): (
        "otel-demo-ext-f4112963d783",
        "baadeb652844388887ee5d3eda0c05301a983f2b686844cf464e3fac645ec37f",
    ),
    (False, False, False, True, True, True): (
        "otel-demo-ext-103fdeea127f",
        "33ab9ccefb3e9ce1a2e27c647afa3d32fd22e731412a8fecda6fc279f6ea8297",
    ),
    (False, False, True, False, False, False): (
        "otel-demo-ext-9b9496d3da62",
        "f293eff1e416b23fc9b7adfdeda6619535781ff0123385bd93d4794fde38cef7",
    ),
    (False, False, True, False, False, True): (
        "otel-demo-ext-b05a7715b390",
        "4c0ca1ed04d0c7167cfaf00806f88362879a4b3e4ee4d9ae2432ed085b962644",
    ),
    (False, False, True, False, True, False): (
        "otel-demo-ext-093854ede4b3",
        "43ef43e3c872af1bd01d4276ad85ee382aa6725ba41a2fdb99d7230d51061aba",
    ),
    (False, False, True, False, True, True): (
        "otel-demo-ext-fc413319fe3f",
        "c2acb2ba13bd6a33f2a5eedda239ec0cbeab722376997bd811b2ea6eb0ca5208",
    ),
    (False, False, True, True, False, False): (
        "otel-demo-ext-59236ca5501c",
        "89e5dae9bba501ecaffeee4684cdc314620c94e8b282fbcdb947140a93983dca",
    ),
    (False, False, True, True, False, True): (
        "otel-demo-ext-1c32b0f80b9c",
        "611c7387a5a2588b2968b1fe14ba5912863258f413d6e69a3449da1eef0624ec",
    ),
    (False, False, True, True, True, False): (
        "otel-demo-ext-fb8a28cf7b36",
        "3d16e1d2a4de92cedf9a5f20af6c33d69c830c1f0a05fd72ed6ff8ec0faa0011",
    ),
    (False, False, True, True, True, True): (
        "otel-demo-ext-bc2f6ee8257f",
        "fa77281880fe8db3d05fcd1b1141490ac57cfea94838dabc70077f50f8f40acf",
    ),
    (False, True, False, False, False, False): (
        "otel-demo-ext-e903661eda1e",
        "a6b15441dd1934d3ada5d1d3e828f175827510afdf36371a811d2776ac9192ec",
    ),
    (False, True, False, False, False, True): (
        "otel-demo-ext-7a41956210ba",
        "93f1403e992cc0d1841a50d3acfe450b8b75c979c6d5954a010dfc78f3005132",
    ),
    (False, True, False, False, True, False): (
        "otel-demo-ext-6038a559298f",
        "5b16bdd8cc7b5d5e55fb1ce025c3bd196611f9c725f912cf3c110a072e10b12a",
    ),
    (False, True, False, False, True, True): (
        "otel-demo-ext-3ec34547734a",
        "0acc71eae1483f81e41cffb28b5e45c662cbbdbd6a738d6c44bf516271b97d7d",
    ),
    (False, True, False, True, False, False): (
        "otel-demo-ext-5c484f6e8670",
        "98cd50b50b1f6650cd25e40407cca4f65337f0d1c3306e3de050332f9eb780f9",
    ),
    (False, True, False, True, False, True): (
        "otel-demo-ext-0d5375f35bba",
        "c770870b3a0be90bddbf1ab5a3d1b72118c31d2c74c0b0410637eca2fe550049",
    ),
    (False, True, False, True, True, False): (
        "otel-demo-ext-aecca6736c31",
        "2cc06085574fb8e6781564de4a5a42d40058ec6d3e241b2a2bfbd00c93c40768",
    ),
    (False, True, False, True, True, True): (
        "otel-demo-ext-8833068590ca",
        "af99885c73fedb3fcd561fc16faa77a9d2f5770fcd19cf54182c1a7b81df2742",
    ),
    (False, True, True, False, False, False): (
        "otel-demo-ext-f7c7b39f72db",
        "cd1168c8b96dcd675059d9036fa927797bfd8161edecf4fa39540a70ae62c4fa",
    ),
    (False, True, True, False, False, True): (
        "otel-demo-ext-6dff700738b4",
        "99aef0590a3c23e6454c1a3f5ff7b4211c1662af18cfae5c02126c7341bb78f6",
    ),
    (False, True, True, False, True, False): (
        "otel-demo-ext-45a9a5a60108",
        "c617e504f754c528e2e21ae2c7b1b115d5e71eab5152fc8ea6e692f34eeed17c",
    ),
    (False, True, True, False, True, True): (
        "otel-demo-ext-d1d8cb85aeb1",
        "6013d2610090ec3179ead66b7ed7d7c4828fb32f071b7b27cf2c360d9c6faf57",
    ),
    (False, True, True, True, False, False): (
        "otel-demo-ext-8ff76ed62045",
        "e50b64a8377f3214e8cac1d659bce3219964cb82e84c695a7971cd9684811dd4",
    ),
    (False, True, True, True, False, True): (
        "otel-demo-ext-b932b0968ee5",
        "1570fa113778872dc8ecacecd88c075cbeacfcfc613c8f6d73c72400d479b75e",
    ),
    (False, True, True, True, True, False): (
        "otel-demo-ext-1faec0a4fdad",
        "863c71c3bc7287c969041c6e4466a4c196944d286917e459424a11d0c2ee6401",
    ),
    (False, True, True, True, True, True): (
        "otel-demo-ext-77d1776b724c",
        "0805fa0f0aaf92690796c76516ff833d5e5ac8638d5c76882d1088d1066fcb28",
    ),
    (True, False, False, False, False, False): (
        "otel-demo-ext-546a670f3486",
        "7533401273967ce7ea5903ba582a0cb8819d7d95e82896d32aa1b763716b7c50",
    ),
    (True, False, False, False, False, True): (
        "otel-demo-ext-95eb34f6aaa0",
        "20762a3b201ff48047cb85edec352852c4bf2aad7d6d34b153f65bfc00cc80cf",
    ),
    (True, False, False, False, True, False): (
        "otel-demo-ext-3d4509d847ff",
        "63bd3f5edbc06ad8e9f5a693f668130632e2d843f2f6b7068deb6ea4b632fbe5",
    ),
    (True, False, False, False, True, True): (
        "otel-demo-ext-31a323835b84",
        "75e834478d64550b0ecae0664841b82794acde26fbb93a410e583b41951ec6b8",
    ),
    (True, False, False, True, False, False): (
        "otel-demo-ext-2449583d502a",
        "6271dd4e62f324685362d387ed81e97c6035a16d5403c03921acde62c8d00fac",
    ),
    (True, False, False, True, False, True): (
        "otel-demo-ext-86fbdb62a20d",
        "2b13eb37b886b101cc239d9f0ee9589b0c2e003d62cbd83371098e01fdbff7af",
    ),
    (True, False, False, True, True, False): (
        "otel-demo-ext-6ae71c493e1c",
        "ee5df7952efe9ae662530cf808afc1f598b2003d106e5f13edcf374d20c48fe8",
    ),
    (True, False, False, True, True, True): (
        "otel-demo-ext-04886f64e0d2",
        "b504fda5187cc813c16dbc10e306bc5be6981e2f99b1eb93bbb3c6680c1b2242",
    ),
    (True, False, True, False, False, False): (
        "otel-demo-ext-e3c4b23f16ca",
        "3d761c375c0cbb3974f336d1f3d6abb3f637f72153004ac3991c67dad5e226fd",
    ),
    (True, False, True, False, False, True): (
        "otel-demo-ext-2be983904fcc",
        "0b752d677788690e977f9b31be98cca91ecf8bdb0424c99253b8f639e7470d68",
    ),
    (True, False, True, False, True, False): (
        "otel-demo-ext-019252640b9e",
        "00b7098b72e888ca5fcbed13f6e9e9b6bb5cccd99be70a366b11aa6dd8a4fb6b",
    ),
    (True, False, True, False, True, True): (
        "otel-demo-ext-3cf3a0999757",
        "64d8335f83790c9e8035de09faf0511a621fb881319d31db7c5996051c710342",
    ),
    (True, False, True, True, False, False): (
        "otel-demo-ext-da18bd68e496",
        "7debeee0074d075094f4a97b7c16260539cb0531045af66e0462e70d8a2206bf",
    ),
    (True, False, True, True, False, True): (
        "otel-demo-ext-11fb96b923f0",
        "32b3579b5939071fe0aac6ee727123fdf50cc67f4ab42df63aea9ffb5f83ba74",
    ),
    (True, False, True, True, True, False): (
        "otel-demo-ext-70b8a4454594",
        "db6e5c51955dbe670bd97f3ba6fe6149c29467ddc0038ef37598991f2fc1bac2",
    ),
    (True, False, True, True, True, True): (
        "otel-demo-ext-a748bf3645d8",
        "c0fd99de38dd2f680c216d59f9494e4ad51bcaf74d3c9d4e7d12e46ff054a20b",
    ),
    (True, True, False, False, False, False): (
        "otel-demo-ext-24903a469f6e",
        "dd8328efe57f4e79f73b5b434b0cfb45ca1eb43e2803162f02a87ffc6272add8",
    ),
    (True, True, False, False, False, True): (
        "otel-demo-ext-5cdd6052b151",
        "71476c9f2aa3912d221a814e9cd0716d647723bd5317ee49761743423e5e088c",
    ),
    (True, True, False, False, True, False): (
        "otel-demo-ext-6aed5fe8b25c",
        "9b7681dfec4d86de3c5f2ec5f1a9e92fc46c39bc90c472e5be99751205192442",
    ),
    (True, True, False, False, True, True): (
        "otel-demo-ext-7bf8fbda949d",
        "c31e90568dd8a09c9484e3214dc3a25fcb654cb4f1a45a7e6624ed1d2c7af648",
    ),
    (True, True, False, True, False, False): (
        "otel-demo-ext-47039e8cf424",
        "4d9762cd8237d46d064e139333573b7ae37a1ae8f6257ddec00ddb577cd60e0c",
    ),
    (True, True, False, True, False, True): (
        "otel-demo-ext-af30a98ade0e",
        "097d023e3db9a753015cf90e3f9ed6da7297a499f455279d1a62db4ad6ed2b6f",
    ),
    (True, True, False, True, True, False): (
        "otel-demo-ext-8cae393d3676",
        "94fb99271d48be849062aafde0cbdb1a0f9c1360de360eb17d828203fcc35f7a",
    ),
    (True, True, False, True, True, True): (
        "otel-demo-ext-b9768302b0bb",
        "39c5e70e14f3aac6e541046b3b74ca44dcb545b7052f7a7dee8ae29eec5dbade",
    ),
    (True, True, True, False, False, False): (
        "otel-demo-ext-d23e2326b211",
        "d04177abc8c2ebd134821005fef2ccadbb854338d6d1aefead7a34426c392262",
    ),
    (True, True, True, False, False, True): (
        "otel-demo-ext-846c66965d33",
        "2ff85492af8c0a95508764118b5ac3447f683e12c2a3711b30a9946272b5c18a",
    ),
    (True, True, True, False, True, False): (
        "otel-demo-ext-fc9a744bd938",
        "480d0b14221aef5ebf3cf177cce6bac780c8578fa1c9ee627262be9542976632",
    ),
    (True, True, True, False, True, True): (
        "otel-demo-ext-5ed5c62cfbe5",
        "f69e92a6d3ac0424e0943866a52d875d576b9b4f9e00a9685166dd450604afba",
    ),
    (True, True, True, True, False, False): (
        "otel-demo-ext-87fa944a04d4",
        "831aef0545b9a04a4c9f39902c396be1cafb811e9f890f5baae44a291e4f6e57",
    ),
    (True, True, True, True, False, True): (
        "otel-demo-ext-b12e5c2d2614",
        "038ac3ed6348f57c4c26646467b8248986370bb18bf27beaf5be16506b7ca73b",
    ),
    (True, True, True, True, True, False): (
        "otel-demo-ext-5f921d39a856",
        "a5a15e6f454921836b2320aeec719f20fba16b8bf9f930f912cd51393da2958c",
    ),
    (True, True, True, True, True, True): (
        "otel-demo-ext-7769864cfe8c",
        "70acdd174eee206e18ff044ba3377482b29423b76c685e7a4712ab67d8202a2f",
    ),
}
BASE_PINNED = {
    "otel-demo": (
        "otel-demo-a9d431ec0dfb",
        "a070a1ed434d1523035bf9141cf91b12358c7d88141523e37ea4391cf07abcfc",
    ),
    "fixture": (
        "fixture-5",
        "d0e306ef25f0312bdc8c16450e44a1adf463503104ff5ab7fb0cc5351f512645",
    ),
}


@pytest.fixture(autouse=True)
def _clean_environment(tmp_path, monkeypatch):
    # Configuration comes from the mapping each test passes; nothing leaks in
    # from the developer's shell or a CI job's OpsPilot DSNs.
    import os

    for name in list(os.environ):
        if name.startswith("OPSPILOT_SQL") or re.fullmatch(
            r"OPSPILOT_[A-Z0-9_]*DSN", name
        ):
            monkeypatch.delenv(name, raising=False)
    for name in ("PGHOST", "PGPORT", "PGHOSTADDR", "PGSERVICE", "PGDATABASE"):
        monkeypatch.delenv(name, raising=False)
    ca_file = tmp_path / "synthetic-k8s-ca.pem"
    ca_file.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("C6A_TEST_K8S_CA_FILE", str(ca_file))


def _flags_env(flags: tuple[bool, ...], **extra: str) -> dict[str, str]:
    env = {PROFILE_ENV: EXT_PROFILE}
    for on, name in zip(flags[:3], BACKEND_URLS):
        if on:
            env[name] = BACKEND_URLS[name]
    for on, name in zip(flags[3:], SWITCHES):
        if on:
            env[name] = "1"
    env.update(extra)
    return env


def _face(env: Mapping[str, str]):
    return select_profile(env).face(FakeClock(start=PIN_NOW))


def _tool_names(env: Mapping[str, str]) -> list[str]:
    return [s["function"]["name"] for s in _face(env).tool_schemas]


def _available(env: Mapping[str, str]) -> Mapping[str, Any]:
    return _face(env).evidence_context(RUN_ID)["data_sources"]["available"]


def _surface_sha256(profile: ToolProfile) -> str:
    face = profile.face(FakeClock(start=PIN_NOW))
    blob = {
        "tool_schemas": [dict(item) for item in face.tool_schemas],
        "variant_id": face.variant_id,
        "evidence_context": dict(face.evidence_context(RUN_ID)),
        "versions": profile.versions(),
    }
    return hashlib.sha256(canonical(blob).encode()).hexdigest()


def _revision(env: Mapping[str, str]) -> str:
    return select_profile(env).versions()["tool_schema_revision"]


def _schemas(env: Mapping[str, str]) -> dict[str, Mapping[str, Any]]:
    return {s["function"]["name"]: s["function"] for s in _face(env).tool_schemas}


# == surfaces and revisions (C6-13) ==============================================


@pytest.mark.parametrize("flags", sorted(PINNED))
def test_without_a_sql_url_every_extension_set_is_byte_identical(flags):
    revision, surface = PINNED[flags]
    env = _flags_env(flags)
    assert _revision(env) == revision
    assert _surface_sha256(select_profile(env)) == surface
    # SQL credentials, TLS or CA alone never change what the web records.
    loose = _flags_env(
        flags,
        OPSPILOT_SQL_USERNAME=SQL_USERNAME,
        OPSPILOT_SQL_PASSWORD=SQL_PASSWORD,
    )
    assert _surface_sha256(select_profile(loose)) == surface
    assert not set(SQL_TOOLS) & set(_tool_names(env))
    assert "sql" not in _available(env)


@pytest.mark.parametrize("profile", sorted(BASE_PINNED))
@pytest.mark.parametrize(
    "url", [PG_URL, MYSQL_URL, "clickhouse+http://127.0.0.1:8123/x"]
)
def test_other_profiles_ignore_every_sql_setting(profile, url):
    revision, surface = BASE_PINNED[profile]
    env = {
        PROFILE_ENV: profile,
        "OPSPILOT_SQL_URL": url,
        "OPSPILOT_SQL_USERNAME": SQL_USERNAME,
        "OPSPILOT_SQL_PASSWORD": SQL_PASSWORD,
    }
    assert _revision(env) == revision
    assert _surface_sha256(select_profile(env)) == surface


def test_revision_function_and_constants_keep_existing_values():
    from opspilot.tools import otel_demo_ext

    for flags, (revision, _) in PINNED.items():
        es, loki, vlogs, live, knowledge, todo = flags
        kwargs = dict(
            elasticsearch=es,
            loki=loki,
            victorialogs=vlogs,
            live_metrics=live,
            knowledge=knowledge,
            todo=todo,
        )
        assert otel_demo_ext.tool_schema_revision(**kwargs) == revision
        assert otel_demo_ext.tool_schema_revision(**kwargs, sql=None) == revision
    assert otel_demo_ext.TOOL_SCHEMA_REVISION == PINNED[(False,) * 6][0]


@pytest.mark.parametrize(
    ("url", "dialect"),
    [
        (PG_URL, "postgresql"),
        ("postgres://127.0.0.1:15432/c6tdb", "postgresql"),
        (MYSQL_URL, "mysql"),
        (MARIADB_URL, "mysql"),
    ],
)
def test_sql_url_appends_three_tools_last_and_one_revision_per_dialect(url, dialect):
    from opspilot.tools import otel_demo_ext

    without = {PROFILE_ENV: EXT_PROFILE}
    web = {PROFILE_ENV: EXT_PROFILE, "OPSPILOT_SQL_URL": url}
    old_names, new_names = _tool_names(without), _tool_names(web)
    assert new_names[: len(old_names)] == old_names
    assert set(new_names[len(old_names) :]) == set(SQL_TOOLS)
    assert len(new_names) == len(old_names) + 3
    available = _available(web)
    assert available["sql"] == sorted(SQL_TOOLS)
    assert {k: v for k, v in available.items() if k != "sql"} == _available(without)
    revision = _revision(web)
    assert revision.startswith("otel-demo-ext-")
    assert revision not in {pin[0] for pin in PINNED.values()}
    assert revision == otel_demo_ext.tool_schema_revision(sql=dialect)
    constant = {
        "postgresql": otel_demo_ext.SQL_POSTGRESQL_TOOL_SCHEMA_REVISION,
        "mysql": otel_demo_ext.SQL_MYSQL_TOOL_SCHEMA_REVISION,
    }[dialect]
    assert revision == constant
    # The web (address only) and the worker (address and credential) agree.
    assert select_profile(sql_env(url)).versions() == select_profile(web).versions()
    assert _surface_sha256(select_profile(sql_env(url))) == _surface_sha256(
        select_profile(web)
    )


def test_the_two_dialects_have_different_revisions_and_names_constant():
    from opspilot.tools import otel_demo_ext

    assert (
        otel_demo_ext.SQL_POSTGRESQL_TOOL_SCHEMA_REVISION
        != otel_demo_ext.SQL_MYSQL_TOOL_SCHEMA_REVISION
    )
    # A web on one dialect and a worker on another record different versions,
    # so the Run is blocked on claim (C3 §5, as B1/C5).
    pg = select_profile({PROFILE_ENV: EXT_PROFILE, "OPSPILOT_SQL_URL": PG_URL})
    my = select_profile({PROFILE_ENV: EXT_PROFILE, "OPSPILOT_SQL_URL": MYSQL_URL})
    assert (
        pg.versions()["tool_schema_revision"] != my.versions()["tool_schema_revision"]
    )
    names = tuple(otel_demo_ext.SQL_TOOL_NAMES)
    # ASSUMPTION: SQL_TOOL_NAMES is either the three tool names or, like
    # LOKI_TOOL_NAMES, every registration name of the SQL set.
    assert set(SQL_TOOLS) <= set(names)
    extra = set(names) - set(SQL_TOOLS)
    assert not extra or extra == set(otel_demo_ext.TOOL_NAMES)


def test_every_backend_set_with_each_dialect_has_its_own_revision():
    from opspilot.tools import otel_demo_ext

    revisions = set()
    for flags in PINNED:
        es, loki, vlogs, live, knowledge, todo = flags
        for url, dialect in (
            (None, None),
            (PG_URL, "postgresql"),
            (MYSQL_URL, "mysql"),
        ):
            extra = {"OPSPILOT_SQL_URL": url} if url else {}
            env = _flags_env(flags, **extra)
            revision = _revision(env)
            assert revision == otel_demo_ext.tool_schema_revision(
                elasticsearch=es,
                loki=loki,
                victorialogs=vlogs,
                live_metrics=live,
                knowledge=knowledge,
                todo=todo,
                sql=dialect,
            )
            revisions.add(revision)
            if url:
                names = _tool_names(env)
                assert set(names[-3:]) == set(SQL_TOOLS), names[-5:]
    assert len(revisions) == 64 * 3


def test_the_sql_url_changes_nothing_else_in_the_face():
    without = {PROFILE_ENV: EXT_PROFILE}
    web = {PROFILE_ENV: EXT_PROFILE, "OPSPILOT_SQL_URL": PG_URL}
    assert set(SQL_TOOLS) <= set(_tool_names(web))
    old_context = dict(_face(without).evidence_context(RUN_ID))
    new_context = dict(_face(web).evidence_context(RUN_ID))
    old_context.pop("data_sources")
    new_context.pop("data_sources")
    assert new_context == old_context
    assert _face(web).variant_id == _face(without).variant_id
    old, new = select_profile(without).versions(), select_profile(web).versions()
    # C6-15: only tool descriptions change, never the investigation prompt.
    assert new["prompt_revision"] == old["prompt_revision"]
    assert new["context_policy_revision"] == old["context_policy_revision"]


# == schemas and registrations ===================================================


def test_schemas_take_exactly_the_documented_parameters():
    schemas = _schemas({PROFILE_ENV: EXT_PROFILE, "OPSPILOT_SQL_URL": PG_URL})
    query = schemas[QUERY]["parameters"]
    assert set(query["properties"]) == {"sql", "limit"}
    assert query["required"] == ["sql"]
    assert query["properties"]["sql"]["type"] == "string"
    assert query["properties"]["limit"]["type"] == "integer"
    tables = schemas[LIST_TABLES]["parameters"]
    assert set(tables["properties"]) == {"schema", "include_views"}
    assert not tables.get("required")
    assert tables["properties"]["schema"]["type"] == "string"
    assert tables["properties"]["include_views"]["type"] == "boolean"
    describe = schemas[DESCRIBE]["parameters"]
    assert set(describe["properties"]) == {"table_name", "schema"}
    assert describe["required"] == ["table_name"]
    # C6-17 ⑫: the model never picks a database, a connection or an address.
    flat = json.dumps([schemas[t]["parameters"] for t in SQL_TOOLS]).lower()
    for word in ("database", "dsn", "connection", "host", "url", "password"):
        assert f'"{word}"' not in flat, word


@pytest.mark.parametrize(
    ("url", "dialect_words"),
    [(PG_URL, ("postgres",)), (MYSQL_URL, ("mysql", "mariadb"))],
)
def test_query_description_names_dialect_rules_and_no_write_mode(url, dialect_words):
    schemas = _schemas({PROFILE_ENV: EXT_PROFILE, "OPSPILOT_SQL_URL": url})
    text = schemas[QUERY]["description"]
    lower = text.lower()
    assert any(word in lower for word in dialect_words), text
    assert "replace(" in lower or "replace()" in lower, text  # C6-15
    assert "limit" in lower, text
    assert "select" in lower, text
    # The upstream "writes can be enabled by configuration" is gone (C6-15).
    for marker in ("allow_write", "enable write", "write mode", "read_only: false"):
        assert marker not in lower, marker


def test_registrations_are_read_only_otel_demo_tools():
    from opspilot.tools import otel_demo, otel_demo_ext

    # ASSUMPTION: the private registry seam takes the dialect like
    # tool_schema_revision does, as ``_registrations(knowledge=)`` did for C2b.
    for dialect in ("postgresql", "mysql"):
        regs = {r.name: r for r in otel_demo_ext._registrations(sql=dialect)}
        assert set(SQL_TOOLS) <= set(regs)
        assert regs[QUERY].verb == "query"
        assert regs[LIST_TABLES].verb == "get"
        assert regs[DESCRIBE].verb == "get"
        for name in SQL_TOOLS:
            reg = regs[name]
            assert reg.read_only is True
            assert reg.result_path == ("results",)
            assert reg.may_contain_secrets is True
            assert reg.source == otel_demo.SOURCE
    assert not set(SQL_TOOLS) & {r.name for r in otel_demo_ext._registrations()}


def test_unconfigured_extension_worker_has_no_sql_tool(monkeypatch):
    driver = FakeSql("postgresql")
    install(monkeypatch, driver)
    executor, _ = worker(sql_env(None))
    outcome = executor.execute(call(QUERY, {"sql": "SELECT 1"}))
    assert outcome.status in ("error", "denied")
    assert driver.connections == [] and driver.sent == []


# == statement sequences (C6-4) ===================================================


def _pg_sets(kinds: list[str]) -> set[str]:
    return set(kinds[1:4])


PG_SETS = {
    "set:statement_timeout",
    "set:lock_timeout",
    "set:idle_in_transaction_session_timeout",
}


def test_postgres_query_runs_inside_a_read_only_transaction_and_rolls_back(
    monkeypatch,
):
    tools = make_tools(monkeypatch, "postgresql")
    sql = "SELECT id, name FROM orders WHERE id < 3"
    tools.driver.result = (["id", "name"], [(1, "a"), (2, "b")])
    outcome = tools.query(sql)
    assert outcome.status == "ok", view(outcome)
    [connection] = tools.driver.connections
    kinds = tools.driver.kinds(connection)
    assert kinds[0] == "begin", kinds
    assert _pg_sets(kinds) == PG_SETS, kinds
    assert kinds[4:] == ["check", "model", "rollback"], kinds
    # The tool sends the model's SQL verbatim and does not rewrite it.
    [model] = [s for s in connection.sent if s.statement == sql]
    assert model.method == "query"
    assert model.params == ()
    assert connection.closed >= 1
    assert connection.sent[-1].statement.strip().upper().startswith("ROLLBACK")
    for item in connection.sent:
        if kind(item.statement, sql) == "check":
            assert "/* opspilot:account-check */" in item.statement


@pytest.mark.parametrize(
    ("version", "timeout_kind"),
    [(MARIADB_VERSION, "set:max_statement_time"), ("8.0.40", "set:max_execution_time")],
)
def test_mysql_and_mariadb_sequence(monkeypatch, version, timeout_kind):
    tools = make_tools(monkeypatch, "mysql", version=version)
    sql = "SELECT id FROM orders"
    outcome = tools.query(sql)
    assert outcome.status == "ok", view(outcome)
    [connection] = tools.driver.connections
    kinds = tools.driver.kinds(connection)
    assert kinds == [
        "version",
        timeout_kind,
        "set:lock_wait_timeout",
        "check",
        "begin",
        "model",
        "rollback",
    ], kinds
    check = next(s for s in connection.sent if kind(s.statement) == "check")
    assert "/* opspilot:account-check */" in check.statement
    assert re.search(r"\bSHOW\s+GRANTS\b", check.statement, re.I), check.statement
    begin = next(s for s in connection.sent if kind(s.statement) == "begin")
    assert re.fullmatch(
        r"\s*START\s+TRANSACTION\s+READ\s+ONLY\s*;?\s*", begin.statement, re.I
    ), begin.statement
    assert connection.closed >= 1


def test_mariadb_url_with_a_mysql_server_uses_the_mysql_timeout(monkeypatch):
    # The URL picks the dialect; VERSION() picks the timeout syntax.
    tools = make_tools(monkeypatch, "mysql", url=MARIADB_URL, version="8.0.40")
    assert tools.query("SELECT 1 AS n").status == "ok"
    assert "set:max_execution_time" in tools.driver.kinds()
    assert "set:max_statement_time" not in tools.driver.kinds()


@pytest.mark.parametrize("dialect", ["postgresql", "mysql"])
def test_each_call_opens_its_own_connection_and_closes_it(monkeypatch, dialect):
    tools = make_tools(monkeypatch, dialect)
    for n in range(3):
        assert tools.query(f"SELECT {n} AS n").status == "ok"
    assert len(tools.driver.connections) == 3
    for connection in tools.driver.connections:
        assert connection.closed >= 1
        kinds = tools.driver.kinds(connection)
        assert kinds.count("check") == 1  # C6-U1: every connection
        assert kinds.count("model") == 1


@pytest.mark.parametrize("dialect", ["postgresql", "mysql"])
@pytest.mark.parametrize(
    ("tool", "params"),
    [
        (LIST_TABLES, {}),
        (LIST_TABLES, {"schema": "c6tschemaq", "include_views": False}),
        (DESCRIBE, {"table_name": "c6ttableq", "schema": "c6tschemaq"}),
        (DESCRIBE, {"table_name": "c6tschemaq.c6ttableq"}),
    ],
)
def test_catalog_tools_send_only_marked_bound_queries_inside_the_transaction(
    monkeypatch, dialect, tool, params
):
    tools = make_tools(monkeypatch, dialect)
    outcome = tools.run(tool, params)
    assert outcome.status in ("ok", "no_data", "error"), view(outcome)
    [connection] = tools.driver.connections
    kinds = tools.driver.kinds(connection)
    assert "catalog" in kinds, kinds
    first, last = kinds.index("catalog"), len(kinds) - 1 - kinds[::-1].index("catalog")
    assert kinds.index("begin") < first and kinds.index("check") < first
    assert kinds[last + 1 :] == ["rollback"], kinds
    assert set(kinds[first : last + 1]) == {"catalog"}, kinds
    catalog = [s for s in connection.sent if kind(s.statement) == "catalog"]
    for item in catalog:
        assert CATALOG_MARK in item.statement
        # Identifiers are bound, never spliced into the statement text.
        assert "c6tschemaq" not in item.statement
        assert "c6ttableq" not in item.statement
    bound = json.dumps([list(map(str, s.params)) for s in catalog])
    if "schema" in params or "." in str(params.get("table_name", "")):
        assert "c6tschemaq" in bound, bound
    if tool == DESCRIBE:
        assert "c6ttableq" in bound, bound
        # schema.table was split: no parameter is the dotted name.
        assert "c6tschemaq.c6ttableq" not in bound


@pytest.mark.parametrize("dialect", ["postgresql", "mysql"])
def test_describe_of_a_missing_table_is_table_not_found(monkeypatch, dialect):
    tools = make_tools(monkeypatch, dialect)
    outcome = tools.run(DESCRIBE, {"table_name": "nope"})
    assert (outcome.status, outcome.reason) == ("error", "INVALID_PARAMS"), view(
        outcome
    )
    assert notes(outcome)[0].startswith("TABLE_NOT_FOUND"), view(outcome)


@pytest.mark.parametrize("dialect", ["postgresql", "mysql"])
@pytest.mark.parametrize(
    ("tool", "params"),
    [
        (LIST_TABLES, {"schema": "a b"}),
        (LIST_TABLES, {"schema": "x';DROP TABLE t;--"}),
        (LIST_TABLES, {"schema": '"public"'}),
        (LIST_TABLES, {"schema": "`db`"}),
        (LIST_TABLES, {"schema": "s" * 129}),
        (LIST_TABLES, {"schema": ""}),
        (DESCRIBE, {"table_name": "t/*x*/"}),
        (DESCRIBE, {"table_name": "t\x00"}),
        (DESCRIBE, {"table_name": "t(1)"}),
        (DESCRIBE, {"table_name": "t" * 129}),
        (DESCRIBE, {"table_name": ""}),
        (DESCRIBE, {"table_name": "ok", "schema": "pg catalog"}),
        (DESCRIBE, {"table_name": "tablé"}),
    ],
)
def test_bad_identifiers_are_refused_before_any_statement(
    monkeypatch, dialect, tool, params
):
    tools = make_tools(monkeypatch, dialect)
    outcome = tools.run(tool, params)
    refused_before_request(outcome, tools.driver, "INVALID_IDENTIFIER")


@pytest.mark.parametrize("dialect", ["postgresql", "mysql"])
def test_identifiers_with_dollar_dash_and_digits_are_accepted(monkeypatch, dialect):
    tools = make_tools(monkeypatch, dialect)
    name = "app-v2$" + "t" * 121
    assert len(name) == 128
    outcome = tools.run(DESCRIBE, {"table_name": name, "schema": "s_1"})
    assert not any(n.startswith("INVALID_IDENTIFIER") for n in notes(outcome)), view(
        outcome
    )
    assert "catalog" in tools.driver.kinds()


@pytest.mark.parametrize("dialect", ["postgresql", "mysql"])
@pytest.mark.parametrize(
    "extra",
    [
        {"database": "other"},
        {"dsn": "postgresql://u:p@127.0.0.1/x"},
        {"connection": "admin"},
        {"start": "2026-11-20T00:00:00Z", "end": "2026-11-20T01:00:00Z"},
        {"params": [1]},
    ],
)
def test_parameters_outside_the_schema_never_reach_the_database(
    monkeypatch, dialect, extra
):
    tools = make_tools(monkeypatch, dialect)
    outcome = tools.run(QUERY, {"sql": "SELECT 1", **extra})
    assert outcome.status in ("error", "denied"), view(outcome)
    assert tools.driver.sent == []


# == C6-U1: the account check refuses a non-read-only account =====================


@pytest.mark.parametrize(
    "problems",
    [
        ["rolsuper"],
        ["rolcreaterole"],
        ["pg_write_all_data"],
        ["table_write"],
        ["sequence_usage", "schema_create"],
        ["database_owner"],
    ],
)
@pytest.mark.parametrize("tool", SQL_TOOLS)
def test_postgres_account_problems_refuse_without_the_model_statement(
    monkeypatch, problems, tool
):
    # ASSUMPTION: the check answers one problem code per row; any row other
    # than ``opspilot_schema`` is an account problem (codes are the
    # implementation's choice).
    tools = make_tools(monkeypatch, "postgresql")
    tools.driver.problems = problems
    params = {
        QUERY: {"sql": "SELECT 1 AS marker_c6t"},
        LIST_TABLES: {},
        DESCRIBE: {"table_name": "orders"},
    }[tool]
    outcome = tools.run(tool, params)
    forbidden(outcome, "SQL_ACCOUNT_NOT_READ_ONLY")
    kinds = tools.driver.kinds()
    assert "check" in kinds
    assert "model" not in kinds and "catalog" not in kinds, kinds
    after = kinds[kinds.index("check") + 1 :]
    assert set(after) <= {"rollback"}, kinds
    assert all(c.closed >= 1 for c in tools.driver.connections)


def test_postgres_opspilot_schema_is_targets_opspilot_db(monkeypatch):
    tools = make_tools(monkeypatch, "postgresql")
    tools.driver.problems = ["opspilot_schema"]
    outcome = tools.query("SELECT 1 AS marker_c6t")
    forbidden(outcome, "SQL_TARGETS_OPSPILOT_DB")
    assert "model" not in tools.driver.kinds()


@pytest.mark.parametrize(
    "line",
    [
        "GRANT INSERT ON `c6tdb`.* TO `c6t_ro`@`%`",
        "GRANT SELECT, INSERT, UPDATE, DELETE ON `c6tdb`.* TO `c6t_ro`@`%`",
        "GRANT ALL PRIVILEGES ON *.* TO `root`@`%` WITH GRANT OPTION",
        "GRANT ALL PRIVILEGES ON `c6tdb`.* TO `c6t_ro`@`%`",
        "GRANT SELECT ON `c6tdb`.* TO `c6t_ro`@`%` WITH GRANT OPTION",
        "GRANT PROXY ON ``@`` TO `root`@`%` WITH GRANT OPTION",
        "GRANT `c6t_writer`@`%` TO `c6t_ro`@`%`",
        "GRANT `c6t_writer` TO `c6t_ro`@`%`",
        "GRANT CREATE TEMPORARY TABLES ON `c6tdb`.* TO `c6t_ro`@`%`",
        "GRANT FILE ON *.* TO `c6t_ro`@`%`",
        "GRANT SUPER ON *.* TO `c6t_ro`@`%`",
        "GRANT SYSTEM_VARIABLES_ADMIN ON *.* TO `c6t_ro`@`%`",
        "this is not a grant",
    ],
)
@pytest.mark.parametrize("tool", SQL_TOOLS)
def test_mysql_grant_problems_refuse_without_the_model_statement(
    monkeypatch, line, tool
):
    tools = make_tools(monkeypatch, "mysql")
    tools.driver.grants = [*tools.driver.grants, line]
    params = {
        QUERY: {"sql": "SELECT 1 AS marker_c6t"},
        LIST_TABLES: {},
        DESCRIBE: {"table_name": "orders"},
    }[tool]
    outcome = tools.run(tool, params)
    forbidden(outcome, "SQL_ACCOUNT_NOT_READ_ONLY")
    kinds = tools.driver.kinds()
    assert "model" not in kinds and "catalog" not in kinds, kinds
    assert kinds.index("check") < len(kinds)
    after = kinds[kinds.index("check") + 1 :]
    assert set(after) <= {"rollback"}, kinds


def test_mariadb_password_hash_in_a_grant_line_is_never_echoed(monkeypatch):
    digest = "*" + "A" * 40
    tools = make_tools(monkeypatch, "mysql", version=MARIADB_VERSION)
    tools.driver.grants = [
        f"GRANT INSERT ON *.* TO `c6t_ro`@`%` IDENTIFIED BY PASSWORD '{digest}'",
    ]
    outcome = tools.query("SELECT 1")
    forbidden(outcome, "SQL_ACCOUNT_NOT_READ_ONLY")
    text = json.dumps(view(outcome), ensure_ascii=False)
    assert digest not in text
    assert "IDENTIFIED BY" not in text
    assert "INSERT" in text.upper()  # the privilege name is reported


def test_mysql_read_only_account_with_mariadb_hash_line_passes(monkeypatch):
    digest = "*" + "B" * 40
    tools = make_tools(monkeypatch, "mysql", version=MARIADB_VERSION)
    tools.driver.grants = [
        f"GRANT USAGE ON *.* TO `c6t_ro`@`%` IDENTIFIED BY PASSWORD '{digest}'",
        "GRANT SELECT, SHOW VIEW ON `c6tdb`.* TO `c6t_ro`@`%`",
    ]
    outcome = tools.query("SELECT 1")
    assert outcome.status == "ok", view(outcome)
    assert digest not in json.dumps(view(outcome))


GOOD_LINES = [
    "GRANT USAGE ON *.* TO `c6t_ro`@`%`",
    "GRANT SELECT, SHOW VIEW ON `testdb`.* TO `opspilot_ro`@`%`",
    "GRANT SELECT ON `performance_schema`.* TO `opspilot_ro`@`%`",
    "GRANT PROCESS ON *.* TO `opspilot_ro`@`%`",
    "GRANT PROCESS, REPLICATION CLIENT ON *.* TO 'opspilot_ro'@'localhost'",
    "GRANT SELECT (`id`, `name`) ON `testdb`.`orders` TO `opspilot_ro`@`%`",
    "GRANT SHOW DATABASES ON *.* TO `opspilot_ro`@`%`",
    "GRANT BINLOG MONITOR ON *.* TO `opspilot_ro`@`%`",
    "GRANT SLAVE MONITOR ON *.* TO `opspilot_ro`@`%`",
    "GRANT REPLICA MONITOR ON *.* TO `opspilot_ro`@`%`",
    "GRANT USAGE ON *.* TO `opspilot_ro`@`%` IDENTIFIED BY PASSWORD '*"
    + "C" * 40
    + "'",
]

BAD_LINES = [
    "GRANT INSERT ON `testdb`.* TO `u`@`%`",
    "GRANT UPDATE ON `testdb`.* TO `u`@`%`",
    "GRANT DELETE ON `testdb`.* TO `u`@`%`",
    "GRANT CREATE ON `testdb`.* TO `u`@`%`",
    "GRANT DROP ON `testdb`.* TO `u`@`%`",
    "GRANT ALTER ON `testdb`.* TO `u`@`%`",
    "GRANT INDEX ON `testdb`.* TO `u`@`%`",
    "GRANT REFERENCES ON `testdb`.* TO `u`@`%`",
    "GRANT CREATE TEMPORARY TABLES ON `testdb`.* TO `u`@`%`",
    "GRANT LOCK TABLES ON `testdb`.* TO `u`@`%`",
    "GRANT EXECUTE ON `testdb`.* TO `u`@`%`",
    "GRANT CREATE VIEW ON `testdb`.* TO `u`@`%`",
    "GRANT CREATE ROUTINE, ALTER ROUTINE ON `testdb`.* TO `u`@`%`",
    "GRANT EVENT, TRIGGER ON `testdb`.* TO `u`@`%`",
    "GRANT SELECT, INSERT ON `testdb`.`orders` TO `u`@`%`",
    "GRANT SELECT (`id`), UPDATE (`note`) ON `testdb`.`orders` TO `u`@`%`",
    "GRANT FILE ON *.* TO `u`@`%`",
    "GRANT SUPER ON *.* TO `u`@`%`",
    "GRANT RELOAD, SHUTDOWN ON *.* TO `u`@`%`",
    "GRANT CREATE USER ON *.* TO `u`@`%`",
    "GRANT PROCESS ON *.* TO `u`@`%` WITH GRANT OPTION",
    "GRANT SELECT ON `testdb`.* TO `u`@`%` WITH GRANT OPTION",
    "GRANT ALL PRIVILEGES ON *.* TO `root`@`%` WITH GRANT OPTION",
    "GRANT ALL PRIVILEGES ON `testdb`.* TO `u`@`%`",
    "GRANT ALL ON `testdb`.* TO `u`@`%`",
    "GRANT PROXY ON ``@`` TO `root`@`%` WITH GRANT OPTION",
    "GRANT PROXY ON `other`@`%` TO `u`@`%`",
    "GRANT `writer`@`%` TO `u`@`%`",
    "GRANT `writer` TO `u`@`%`",
    "GRANT `r1`@`%`,`r2`@`%` TO `u`@`%`",
    "GRANT BACKUP_ADMIN,BINLOG_ADMIN,CONNECTION_ADMIN ON *.* TO `root`@`%`",
    "GRANT SET_USER_ID ON *.* TO `u`@`%`",
    "GRANT SYSTEM_VARIABLES_ADMIN ON *.* TO `u`@`%`",
    "REVOKE INSERT ON `testdb`.* FROM `u`@`%`",
    "GRANT SELECT ON `testdb`.* TO `u`@`%`; GRANT INSERT ON `testdb`.* TO `u`@`%`",
    "",
    "garbage",
]


@pytest.mark.parametrize("line", GOOD_LINES)
def test_session_problem_accepts_whitelisted_mysql_grants(line):
    from opspilot.tools import sql_query

    assert sql_query.session_problem("mysql", [(line,)]) is None
    assert sql_query.session_problem("mysql", [(g,) for g in GOOD_LINES]) is None


@pytest.mark.parametrize("line", BAD_LINES)
def test_session_problem_refuses_every_other_mysql_grant_line(line):
    from opspilot.tools import sql_query

    assert sql_query.session_problem("mysql", [(line,)]) is not None
    mixed = [(g,) for g in GOOD_LINES[:3]] + [(line,)] + [(GOOD_LINES[3],)]
    assert sql_query.session_problem("mysql", mixed) is not None


def test_session_problem_on_postgres_rows():
    from opspilot.tools import sql_query

    assert sql_query.session_problem("postgresql", []) is None
    assert sql_query.session_problem("postgresql", [("rolsuper",)]) is not None
    assert sql_query.session_problem("postgresql", [("opspilot_schema",)]) is not None


# == C6-8: isolation from OpsPilot's own databases ================================


def _with_dsn(monkeypatch, env: dict[str, str], **dsns: str) -> dict[str, str]:
    """DSNs in both the mapping and the process environment.

    # ASSUMPTION: the worker may read the OpsPilot DSNs from the mapping it is
    # given or from ``os.environ``; the tests set both.
    """
    for name, value in dsns.items():
        monkeypatch.setenv(name, value)
    return {**env, **dsns}


@pytest.mark.parametrize(
    ("sql_url", "dsns"),
    [
        (
            "postgresql://127.0.0.1:5432/app",
            {"OPSPILOT_WORKER_DSN": "postgresql://opspilot:x@localhost/opspilot"},
        ),
        (
            "postgresql://localhost/app",
            {"OPSPILOT_DSN": "host=127.0.0.1 port=5432 dbname=opspilot user=o"},
        ),
        (
            "postgres://127.0.0.1:5433/app",
            {"OPSPILOT_WEB_DSN": "postgresql://o@[::1]:5433/opspilot"},
        ),
        (
            "postgresql://127.0.0.2:5432/app",
            {"OPSPILOT_WORKER_DSN": "postgresql://o@localhost:5432/opspilot"},
        ),
        (
            "postgresql://localhost:5432/app",
            {"OPSPILOT_WORKER_DSN": "host=/var/run/postgresql dbname=opspilot"},
        ),
        (
            "postgresql://127.0.0.1/app",
            {"OPSPILOT_WORKER_DSN": "dbname=opspilot user=o"},
        ),
        (
            "mysql://127.0.0.1:5432/app",
            {"OPSPILOT_WORKER_DSN": "postgresql://o@127.0.0.1:5432/opspilot"},
        ),
        (
            "postgresql://127.0.0.1:5432/app",
            {
                "OPSPILOT_WORKER_DSN": "postgresql://o@127.0.0.1:6543/opspilot",
                "OPSPILOT_KNOWLEDGE_ADMIN_DSN": "postgresql://o@127.0.0.1/opspilot",
            },
        ),
    ],
)
def test_worker_refuses_a_sql_url_on_an_opspilot_database(monkeypatch, sql_url, dsns):
    driver = FakeSql("mysql" if sql_url.startswith("mysql") else "postgresql")
    install(monkeypatch, driver)
    env = _with_dsn(monkeypatch, sql_env(sql_url), **dsns)
    with pytest.raises(SystemExit, match="SQL_TARGETS_OPSPILOT_DB"):
        worker(env)
    assert driver.connections == []  # decided from configuration alone


def test_pghost_and_pgport_fill_a_dsn_without_host_or_port(monkeypatch):
    driver = FakeSql("postgresql")
    install(monkeypatch, driver)
    monkeypatch.setenv("PGHOST", "localhost")
    monkeypatch.setenv("PGPORT", "6544")
    env = _with_dsn(
        monkeypatch,
        sql_env("postgresql://127.0.0.1:6544/app", PGHOST="localhost", PGPORT="6544"),
        OPSPILOT_WORKER_DSN="dbname=opspilot user=o",
    )
    with pytest.raises(SystemExit, match="SQL_TARGETS_OPSPILOT_DB"):
        worker(env)


@pytest.mark.parametrize(
    ("sql_url", "dsns"),
    [
        (
            "postgresql://127.0.0.1:15432/app",
            {"OPSPILOT_WORKER_DSN": "postgresql://o@127.0.0.1:5432/opspilot"},
        ),
        (
            "mysql://127.0.0.1/app",
            {"OPSPILOT_WORKER_DSN": "postgresql://o@127.0.0.1/opspilot"},
        ),
        (
            "postgresql://127.0.0.1:5432/app",
            # Neither name ends in DSN: not an OpsPilot connection string.
            {
                "OPSPILOT_DSN_BACKUP": "postgresql://o@127.0.0.1:5432/x",
                "OPSPILOT_REPLICA_DATABASE_URL": "postgresql://o@127.0.0.1:5432/x",
            },
        ),
    ],
)
def test_worker_starts_when_no_opspilot_dsn_shares_the_address(
    monkeypatch, sql_url, dsns
):
    driver = FakeSql("mysql" if sql_url.startswith("mysql") else "postgresql")
    install(monkeypatch, driver)
    executor, _ = worker(_with_dsn(monkeypatch, sql_env(sql_url), **dsns))
    assert executor is not None


def test_credentials_come_only_from_the_sql_ro_slot(monkeypatch):
    driver = FakeSql("postgresql")
    install(monkeypatch, driver)
    dsn_user, dsn_password = "c6tdsnuser", "c6t-dsn-" + "bbbb570"
    env = _with_dsn(
        monkeypatch,
        sql_env("postgresql://127.0.0.1:15432/app"),
        OPSPILOT_WORKER_DSN=f"postgresql://{dsn_user}:{dsn_password}@127.0.0.1:5432/o",
    )
    executor, _ = worker(env)
    driver.model = "SELECT 1"
    outcome = executor.execute(call(QUERY, {"sql": "SELECT 1"}))
    assert outcome.status == "ok", view(outcome)
    [config] = driver.configs
    flat = repr(config)
    if dataclasses.is_dataclass(config):
        flat += json.dumps(dataclasses.asdict(config), default=str)
    elif hasattr(config, "__dict__"):
        flat += json.dumps(vars(config), default=str)
    assert dsn_user not in flat and dsn_password not in flat
    assert SQL_USERNAME in flat


def test_no_fallback_to_another_slot_when_sql_has_no_credential(monkeypatch):
    env = sql_env(
        PG_URL,
        OPSPILOT_SQL_USERNAME="",
        OPSPILOT_SQL_PASSWORD="",
        OPSPILOT_ES_URL="http://127.0.0.1:9",
        OPSPILOT_ES_API_KEY="es-ro-cccccccc570",
        OPSPILOT_OTEL_TOKEN="gggggggg570",
    )
    with pytest.raises(SystemExit, match="SQL_CONFIG_MISSING"):
        worker(env)


# == timeouts (C6-4 / 超时) ========================================================


def test_postgres_server_timeouts_are_the_remaining_budget_minus_two_seconds(
    monkeypatch,
):
    tools = make_tools(monkeypatch, "postgresql")
    assert tools.query("SELECT 1").status == "ok"
    [timeout] = tools.driver.timeouts
    assert 25 <= timeout <= 30, timeout
    sets = {
        kind(s.statement): number_in(s.statement, s.params)
        for s in tools.driver.sent
        if kind(s.statement).startswith("set:")
    }
    assert set(sets) == PG_SETS
    assert 24_000 <= sets["set:statement_timeout"] <= 28_000, sets
    for value in sets.values():
        assert 1_000 <= value <= 28_000, sets


def test_postgres_server_timeout_is_at_least_one_second(monkeypatch):
    tools = make_tools(monkeypatch, "postgresql", request_timeout_seconds=2.5)
    assert tools.query("SELECT 1").status == "ok"
    [timeout] = tools.driver.timeouts
    assert 0 < timeout <= 2.5
    [statement] = [
        s for s in tools.driver.sent if kind(s.statement) == "set:statement_timeout"
    ]
    assert number_in(statement.statement, statement.params) == 1_000


@pytest.mark.parametrize(
    ("version", "name", "scale"),
    [
        ("8.0.40", "set:max_execution_time", 1000),
        (MARIADB_VERSION, "set:max_statement_time", 1),
    ],
)
def test_mysql_server_timeouts(monkeypatch, version, name, scale):
    tools = make_tools(monkeypatch, "mysql", version=version)
    assert tools.query("SELECT 1").status == "ok"
    [timeout] = tools.driver.timeouts
    assert 25 <= timeout <= 30, timeout
    sets = {
        kind(s.statement): number_in(s.statement, s.params)
        for s in tools.driver.sent
        if kind(s.statement).startswith("set:")
    }
    assert 24 * scale <= sets[name] <= 28 * scale, sets
    assert 1 <= sets["set:lock_wait_timeout"] <= 28, sets


def test_postgres_watchdog_cancels_a_query_past_the_deadline(monkeypatch):
    tools = make_tools(monkeypatch, "postgresql", request_timeout_seconds=2.0)
    tools.driver.block_seconds = 15
    started = time.monotonic()
    outcome = tools.query("SELECT count(*) FROM big_a, big_b")
    elapsed = time.monotonic() - started
    assert (outcome.status, outcome.reason) == ("timeout", "TOOL_TIMEOUT"), view(
        outcome
    )
    assert elapsed < 8, elapsed
    [connection] = tools.driver.connections
    assert wait_until(lambda: connection.cancel_calls >= 1)
    assert wait_until(lambda: connection.closed >= 1)


def test_mysql_read_timeout_kills_the_query_from_another_connection(monkeypatch):
    pytest.importorskip("pymysql")
    tools = make_tools(monkeypatch, "mysql", request_timeout_seconds=2.0)
    tools.driver.block_seconds = 15
    sql = "SELECT COUNT(*) FROM big_a, big_b"
    started = time.monotonic()
    outcome = tools.query(sql)
    elapsed = time.monotonic() - started
    assert (outcome.status, outcome.reason) == ("timeout", "TOOL_TIMEOUT"), view(
        outcome
    )
    assert elapsed < 8, elapsed
    first = tools.driver.connections[0]

    def killed() -> bool:
        if first.cancel_calls:
            return True
        for other in tools.driver.connections[1:]:
            for item in other.sent:
                if re.fullmatch(
                    r"\s*KILL\s+QUERY\s+4242\s*;?\s*", item.statement, re.I
                ):
                    return True
        return False

    assert wait_until(killed), [c.sent for c in tools.driver.connections]
    for other in tools.driver.connections[1:]:
        assert sql not in [s.statement for s in other.sent]


def _pg_error(code: str, message: str):
    import psycopg.errors

    return psycopg.errors.lookup(code)(message)


def _mysql_error(code: int, message: str):
    pymysql = pytest.importorskip("pymysql")
    cls = pymysql.err.error_map.get(code, pymysql.err.OperationalError)
    return cls(code, message)


@pytest.mark.parametrize("code", ["57014"])
def test_postgres_statement_timeout_is_tool_timeout(monkeypatch, code):
    tools = make_tools(monkeypatch, "postgresql")
    tools.driver.errors["model"] = _pg_error(
        code, "canceling statement due to statement timeout"
    )
    outcome = tools.query("SELECT 1")
    assert (outcome.status, outcome.reason) == ("timeout", "TOOL_TIMEOUT"), view(
        outcome
    )


@pytest.mark.parametrize(
    ("version", "code"), [("8.0.40", 3024), (MARIADB_VERSION, 1969)]
)
def test_mysql_statement_timeout_is_tool_timeout(monkeypatch, version, code):
    tools = make_tools(monkeypatch, "mysql", version=version)
    tools.driver.errors["model"] = _mysql_error(code, "Query execution was interrupted")
    outcome = tools.query("SELECT 1")
    assert (outcome.status, outcome.reason) == ("timeout", "TOOL_TIMEOUT"), view(
        outcome
    )


# == projection, limits, sizes ======================================================


def test_rows_are_column_value_maps_in_result_order_with_duplicate_suffixes(
    monkeypatch,
):
    tools = make_tools(monkeypatch, "postgresql")
    tools.driver.result = (
        ["id", "name", "id", "total", "id"],
        [(1, "a", 10, 5, 100), (2, "b", 20, 6, 200)],
    )
    outcome = tools.query("SELECT o.id, o.name, c.id, o.total, x.id FROM o, c, x")
    assert outcome.status == "ok", view(outcome)
    got = rows(outcome)
    expected_keys = ["id", "name", "id#2", "total", "id#3"]
    for row in got:
        assert [k for k in row if k != "_truncated_fields"] == expected_keys
    assert got[0]["id"] == 1 and got[0]["id#2"] == 10 and got[0]["id#3"] == 100
    assert got[1]["name"] == "b"
    v = view(outcome)
    assert v["dialect"] == "postgresql"
    assert v["columns"] in (expected_keys, ["id", "name", "id", "total", "id"])
    assert v["limit"] == 100
    assert v["incomplete"] is False


def test_values_convert_to_json(monkeypatch):
    moment = datetime(2026, 11, 20, 11, 59, 30, 250000, tzinfo=timezone.utc)
    ident = uuid.UUID("12345678-1234-5678-1234-567812345678")
    tools = make_tools(monkeypatch, "postgresql")
    columns = [
        "n", "b", "i", "f", "dec", "u", "inf", "nan", "ts", "d", "td",
        "raw", "doc", "arr", "ip",
    ]  # fmt: skip
    values = (
        None, True, 7, 1.5, Decimal("12.30"), ident, math.inf, math.nan, moment,
        date(2026, 11, 20), timedelta(seconds=90, microseconds=500000),
        b"\x01\xff", {"a": 1, "b": [1, 2]}, [1, 2], IPv4Address("10.0.0.1"),
    )  # fmt: skip
    tools.driver.result = (columns, [values])
    outcome = tools.query("SELECT * FROM typed")
    assert outcome.status == "ok", view(outcome)
    [row] = rows(outcome)
    assert row["n"] is None and row["b"] is True and row["i"] == 7 and row["f"] == 1.5
    assert row["dec"] == "12.30"
    assert row["u"] == str(ident)
    assert isinstance(row["inf"], str) and "inf" in row["inf"].lower()
    assert isinstance(row["nan"], str) and "nan" in row["nan"].lower()
    assert datetime.fromisoformat(row["ts"].replace("Z", "+00:00")) == moment
    assert row["d"] == "2026-11-20"
    assert isinstance(row["td"], str) and float(row["td"]) == 90.5
    assert row["raw"] == "0x01ff"
    assert isinstance(row["doc"], str) and json.loads(row["doc"]) == {
        "a": 1,
        "b": [1, 2],
    }
    assert " " not in row["doc"]
    assert row["arr"] == "[1,2]"
    assert row["ip"] == "10.0.0.1"
    json.dumps(view(outcome), allow_nan=False)  # the whole view is strict JSON


def test_long_strings_are_cut_to_2000_chars_and_marked(monkeypatch):
    tools = make_tools(monkeypatch, "mysql")
    tools.driver.result = (
        ["id", "body", "short"],
        [(1, "x" * 5000, "ok"), (2, "y", "ok")],
    )
    outcome = tools.query("SELECT id, body, short FROM docs")
    assert outcome.status == "ok", view(outcome)
    first, second = rows(outcome)
    assert first["body"].startswith("x" * 1000) and len(first["body"]) <= 2000 + 64
    assert "x" * 2001 not in json.dumps(view(outcome))
    assert first["_truncated_fields"] == ["body"]
    assert not second.get("_truncated_fields")
    assert view(outcome)["long_values_cut"]


@pytest.mark.parametrize("dialect", ["postgresql", "mysql"])
def test_password_and_token_columns_are_redacted(monkeypatch, dialect):
    tools = make_tools(monkeypatch, dialect)
    tools.driver.result = (
        ["id", "password", "api_token"],
        [(1, f"pw-{CANARY}", f"tok-{CANARY}")],
    )
    outcome = tools.query("SELECT id, password, api_token FROM users")
    assert outcome.status == "ok", view(outcome)
    text = json.dumps(view(outcome))
    assert CANARY not in text
    assert "REDACTED" in text
    assert rows(outcome)[0]["id"] == 1


@pytest.mark.parametrize("limit", [1, 3, 500])
def test_reaching_the_limit_marks_incomplete(monkeypatch, limit):
    tools = make_tools(monkeypatch, "postgresql")
    tools.driver.result = (["n"], [(n,) for n in range(limit + 5)])
    sql = "SELECT n FROM generate_series(1, 1000) n"
    outcome = tools.query(sql, limit=limit)
    assert outcome.status == "ok", view(outcome)
    assert [r["n"] for r in rows(outcome)] == list(range(limit))
    v = view(outcome)
    assert v["incomplete"] is True
    assert "limit" in str(v["incomplete_reason"]).lower()
    assert v["limit"] == limit
    [model] = [s for s in tools.driver.sent if s.statement == sql]
    assert model.max_rows in (limit, limit + 1)  # never an unbounded read


def test_exactly_limit_rows_are_complete_and_default_limit_is_100(monkeypatch):
    tools = make_tools(monkeypatch, "mysql")
    tools.driver.result = (["n"], [(n,) for n in range(100)])
    sql = "SELECT n FROM numbers"
    outcome = tools.query(sql)
    assert outcome.status == "ok", view(outcome)
    assert len(rows(outcome)) == 100
    assert view(outcome)["incomplete"] is False
    assert view(outcome)["limit"] == 100
    [model] = [s for s in tools.driver.sent if s.statement == sql]
    assert model.max_rows in (100, 101)


@pytest.mark.parametrize("limit", [0, 501, -1, 10_000])
def test_limit_outside_1_to_500_is_refused_before_any_statement(monkeypatch, limit):
    tools = make_tools(monkeypatch, "postgresql")
    outcome = tools.query("SELECT 1", limit=limit)
    refused_before_request(outcome, tools.driver, "INVALID_LIMIT")


@pytest.mark.parametrize("limit", ["10", 1.5, True, None, [5]])
def test_limit_of_the_wrong_type_is_refused(monkeypatch, limit):
    tools = make_tools(monkeypatch, "postgresql")
    outcome = tools.run(QUERY, {"sql": "SELECT 1", "limit": limit})
    assert outcome.status in ("error", "denied"), view(outcome)
    assert tools.driver.sent == []


def test_raw_rows_over_2_mib_are_refused_whole_even_if_the_cut_view_is_small(
    monkeypatch,
):
    tools = make_tools(monkeypatch, "postgresql")
    tools.driver.result = (["blob"], [("z" * (3 * 1024 * 1024),)])
    outcome = tools.query("SELECT blob FROM big", limit=1)
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE"), view(
        outcome
    )
    assert view(outcome)["content"] is None


def test_many_rows_over_2_mib_are_refused_whole(monkeypatch):
    tools = make_tools(monkeypatch, "mysql")
    tools.driver.result = (["id", "body"], [(n, "q" * 5000) for n in range(500)])
    outcome = tools.query("SELECT id, body FROM big", limit=500)
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE"), view(
        outcome
    )
    assert view(outcome)["content"] is None


def test_view_over_25k_tokens_is_refused_whole_with_a_narrowing_hint(monkeypatch):
    words = " ".join(f"word{n} checkout latency spike" for n in range(60))
    assert len(words) < 2000
    tools = make_tools(monkeypatch, "postgresql")
    tools.driver.result = (["id", "note"], [(n, words) for n in range(500)])
    outcome = tools.query("SELECT id, note FROM notes", limit=500)
    assert (outcome.status, outcome.reason) == ("error", "RESULT_TOO_LARGE"), view(
        outcome
    )
    v = view(outcome)
    assert v["content"] is None
    hint = json.dumps(
        {k: val for k, val in v.items() if k not in ("query", "sql")}
    ).lower()
    assert "limit" in hint


@pytest.mark.parametrize("dialect", ["postgresql", "mysql"])
def test_zero_rows_are_no_data(monkeypatch, dialect):
    tools = make_tools(monkeypatch, dialect)
    tools.driver.result = (["n"], [])
    outcome = tools.query("SELECT n FROM empty_table")
    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA"), view(outcome)


def test_current_state_times_and_cannot_prove(monkeypatch):
    tools = make_tools(monkeypatch, "postgresql")
    first = tools.query("SELECT 1 AS n")
    tools.driver.result = (["m"], [("x",)])
    second = tools.query("SELECT 'x' AS m")
    for outcome in (first, second):
        v = view(outcome)
        assert outcome.status == "ok", v
        assert v["source_start_at"] is None and v["source_end_at"] is None
        record = outcome.evidence
        assert record.source_start_at is None and record.source_end_at is None
        # notes[0] carries the query time.
        assert re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d", notes(outcome)[0]), notes(
            outcome
        )
        assert v["source"] == "otel-demo"
    a, b = view(first)["cannot_prove"], view(second)["cannot_prove"]
    assert isinstance(a, str) and a and a == b


# == error classification (C6-14) ===================================================


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        ("42601", "INVALID_PARAMS"),
        ("42P01", "INVALID_PARAMS"),
        ("42703", "INVALID_PARAMS"),
        ("22012", "INVALID_PARAMS"),
        ("22P02", "INVALID_PARAMS"),
        ("42501", "SOURCE_FORBIDDEN"),
        ("25006", "SOURCE_FORBIDDEN"),
        ("08006", "SOURCE_UNAVAILABLE"),
        ("57P01", "SOURCE_UNAVAILABLE"),
        ("53300", "SOURCE_UNAVAILABLE"),
        ("XX000", "SOURCE_ERROR"),
    ],
)
def test_postgres_errors_on_the_model_statement(monkeypatch, code, reason):
    tools = make_tools(monkeypatch, "postgresql")
    message = f"server said something password={CANARY} " + "w" * 3000
    tools.driver.errors["model"] = _pg_error(code, message)
    outcome = tools.query("SELECT 1")
    assert (outcome.status, outcome.reason) == ("error", reason), view(outcome)
    text = json.dumps(view(outcome))
    assert CANARY not in text
    assert "w" * 600 not in text  # echo cut to 500 chars
    if reason == "INVALID_PARAMS":
        assert "server said something" in text  # echoed, marked untrusted
    if reason == "SOURCE_FORBIDDEN" and code == "42501":
        assert "server said something" not in text  # not echoed
    if code == "25006":
        assert "SESSION_READ_ONLY" in " ".join(notes(outcome))
    assert len(tools.driver.connections) == 1  # no retry
    assert tools.driver.kinds()[-1] == "rollback" or tools.driver.connections[0].closed


@pytest.mark.parametrize(
    ("code", "reason"),
    [
        (1064, "INVALID_PARAMS"),
        (1146, "INVALID_PARAMS"),
        (1054, "INVALID_PARAMS"),
        (1052, "INVALID_PARAMS"),
        (1305, "INVALID_PARAMS"),
        (1142, "SOURCE_FORBIDDEN"),
        (1143, "SOURCE_FORBIDDEN"),
        (1044, "SOURCE_FORBIDDEN"),
        (1227, "SOURCE_FORBIDDEN"),
        (1792, "SOURCE_FORBIDDEN"),
        (2006, "SOURCE_UNAVAILABLE"),
        (2013, "SOURCE_UNAVAILABLE"),
    ],
)
def test_mysql_errors_on_the_model_statement(monkeypatch, code, reason):
    tools = make_tools(monkeypatch, "mysql")
    tools.driver.errors["model"] = _mysql_error(
        code, f"server said something password={CANARY} " + "w" * 3000
    )
    outcome = tools.query("SELECT 1")
    assert (outcome.status, outcome.reason) == ("error", reason), view(outcome)
    text = json.dumps(view(outcome))
    assert CANARY not in text
    assert "w" * 600 not in text
    if code == 1792:
        assert "SESSION_READ_ONLY" in " ".join(notes(outcome))
    if reason == "SOURCE_FORBIDDEN" and code != 1792:
        assert "server said something" not in text
    assert len(tools.driver.connections) == 1


def test_bad_password_at_connect_is_forbidden_and_not_echoed(monkeypatch):
    tools = make_tools(monkeypatch, "postgresql")
    tools.driver.connect_error = _pg_error(
        "28P01", f'password authentication failed for user "{SQL_USERNAME}" {CANARY}'
    )
    outcome = tools.query("SELECT 1")
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN"), view(
        outcome
    )
    assert CANARY not in json.dumps(view(outcome))
    assert SQL_PASSWORD not in json.dumps(view(outcome))
    assert len(tools.driver.configs) == 1  # no retry


def test_mysql_bad_password_and_unreachable_server(monkeypatch):
    tools = make_tools(monkeypatch, "mysql")
    tools.driver.connect_error = _mysql_error(1045, "Access denied for user")
    outcome = tools.query("SELECT 1")
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_FORBIDDEN"), view(
        outcome
    )
    tools.driver.connect_error = _mysql_error(2003, "Can't connect to MySQL server")
    outcome = tools.query("SELECT 1")
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_UNAVAILABLE"), view(
        outcome
    )


def test_postgres_unreachable_server_is_source_unavailable(monkeypatch):
    import psycopg

    tools = make_tools(monkeypatch, "postgresql")
    tools.driver.connect_error = psycopg.OperationalError(
        'connection failed: connection to server at "127.0.0.1", port 15432 failed: '
        "Connection refused"
    )
    outcome = tools.query("SELECT 1")
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_UNAVAILABLE"), view(
        outcome
    )


@pytest.mark.parametrize("dialect", ["postgresql", "mysql"])
def test_a_read_only_transaction_that_cannot_start_is_source_error(
    monkeypatch, dialect
):
    tools = make_tools(monkeypatch, dialect)
    if dialect == "postgresql":
        tools.driver.errors["begin"] = _pg_error("XX000", "internal error")
    else:
        tools.driver.errors["begin"] = _mysql_error(1105, "Unknown error")
    outcome = tools.query("SELECT 1")
    assert (outcome.status, outcome.reason) == ("error", "SOURCE_ERROR"), view(outcome)
    assert "model" not in tools.driver.kinds()
    assert all(c.closed >= 1 for c in tools.driver.connections)


def test_a_failing_account_check_never_runs_the_model_statement(monkeypatch):
    tools = make_tools(monkeypatch, "postgresql")
    tools.driver.errors["check"] = _pg_error("XX000", "internal error")
    outcome = tools.query("SELECT 1")
    assert outcome.status == "error", view(outcome)
    assert "model" not in tools.driver.kinds()


CA_SENTINEL = "<minted-ca>"


def _mint_ca(tmp_path: Path) -> str:
    import shutil

    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is needed to mint the synthetic CA")
    cert, key = tmp_path / "sql-ca.pem", tmp_path / "sql-ca-key.pem"
    subprocess.run(
        [
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "2",
            "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return str(cert)


# == configuration and startup codes (C6-7, C6-9) ===================================


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"OPSPILOT_SQL_USERNAME": ""}, "SQL_CONFIG_MISSING"),
        ({"OPSPILOT_SQL_PASSWORD": ""}, "SQL_CONFIG_MISSING"),
        (
            {"OPSPILOT_SQL_URL": "postgresql://u:p@127.0.0.1:5432/db"},
            "SQL_CONFIG_INVALID",
        ),
        (
            {"OPSPILOT_SQL_URL": "postgresql://u@127.0.0.1:5432/db"},
            "SQL_CONFIG_INVALID",
        ),
        (
            {"OPSPILOT_SQL_URL": "postgresql://127.0.0.1/db?sslmode=disable"},
            "SQL_CONFIG_INVALID",
        ),
        ({"OPSPILOT_SQL_URL": "postgresql://127.0.0.1/db#x"}, "SQL_CONFIG_INVALID"),
        ({"OPSPILOT_SQL_URL": "postgresql://127.0.0.1:5432"}, "SQL_CONFIG_INVALID"),
        ({"OPSPILOT_SQL_URL": "postgresql://127.0.0.1:5432/"}, "SQL_CONFIG_INVALID"),
        ({"OPSPILOT_SQL_URL": "postgresql://127.0.0.1:5432/a/b"}, "SQL_CONFIG_INVALID"),
        ({"OPSPILOT_SQL_URL": "mysql://127.0.0.1:3306/db?ssl=0"}, "SQL_CONFIG_INVALID"),
        # clickhouse+http:// and mssql:// were refused here until C6b opened
        # them (tests/test_tool_expansion_c6b_contract.py).
        (
            {"OPSPILOT_SQL_URL": "postgresql+psycopg://127.0.0.1/db"},
            "SQL_CONFIG_INVALID",
        ),
        ({"OPSPILOT_SQL_URL": "mongodb://127.0.0.1:27017/db"}, "SQL_CONFIG_INVALID"),
        ({"OPSPILOT_SQL_URL": "sqlite:///tmp/x.db"}, "SQL_CONFIG_INVALID"),
        ({"OPSPILOT_SQL_TLS": "prefer"}, "SQL_CONFIG_INVALID"),
        ({"OPSPILOT_SQL_TLS": "verify-ca"}, "SQL_CONFIG_INVALID"),
        ({"OPSPILOT_SQL_TLS": "on"}, "SQL_CONFIG_INVALID"),
        (
            {
                "OPSPILOT_SQL_TLS": "disable",
                "OPSPILOT_SQL_CA_FILE": "/run/secrets/ca.pem",
            },
            "SQL_CONFIG_INVALID",
        ),
        # Settings without the address are never silently ignored.
        ({"OPSPILOT_SQL_URL": ""}, "SQL_CONFIG_INVALID"),
        (
            {
                "OPSPILOT_SQL_URL": "",
                "OPSPILOT_SQL_USERNAME": "",
                "OPSPILOT_SQL_PASSWORD": "",
                "OPSPILOT_SQL_TLS": "require",
            },
            "SQL_CONFIG_INVALID",
        ),
        (
            {
                "OPSPILOT_SQL_URL": "",
                "OPSPILOT_SQL_USERNAME": "",
                "OPSPILOT_SQL_PASSWORD": "",
                "OPSPILOT_SQL_CA_FILE": "/run/secrets/ca.pem",
            },
            "SQL_CONFIG_INVALID",
        ),
    ],
)
def test_worker_refuses_to_start_with_bad_sql_configuration(overrides, code):
    require_jq()
    with pytest.raises(SystemExit, match=code):
        worker(sql_env(PG_URL, **overrides))


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"OPSPILOT_SQL_TLS": "verify-full"},
        {"OPSPILOT_SQL_TLS": "verify-full", "OPSPILOT_SQL_CA_FILE": CA_SENTINEL},
        {"OPSPILOT_SQL_TLS": "require"},
        {"OPSPILOT_SQL_TLS": "disable"},
        {"OPSPILOT_SQL_URL": "postgres://db.c6t.svc:5432/app"},
        {"OPSPILOT_SQL_URL": "mariadb://127.0.0.1/app"},
    ],
)
def test_worker_starts_with_valid_sql_configuration(monkeypatch, tmp_path, overrides):
    # Lead ruling (as Loki/VictoriaLogs): the CA file must exist and be
    # readable; a freshly minted one stands in for /run/secrets.
    if overrides.get("OPSPILOT_SQL_CA_FILE") == CA_SENTINEL:
        overrides = {**overrides, "OPSPILOT_SQL_CA_FILE": _mint_ca(tmp_path)}
    driver = FakeSql("postgresql")
    install(monkeypatch, driver)
    executor, _ = worker(sql_env(PG_URL, **overrides))
    assert executor is not None
    assert driver.connections == []  # no connection at startup


@pytest.mark.parametrize("tls", ["verify-full", "require"])
def test_a_missing_ca_file_is_config_invalid(tmp_path, tls):
    require_jq()
    missing = str(tmp_path / "no-such-ca.pem")
    with pytest.raises(SystemExit, match="SQL_CONFIG_INVALID"):
        worker(sql_env(PG_URL, OPSPILOT_SQL_TLS=tls, OPSPILOT_SQL_CA_FILE=missing))


@pytest.mark.parametrize(
    "url",
    [
        "clickhouse://127.0.0.1:8123/default",  # C6b opened clickhouse+http
        "mssql+pyodbc://127.0.0.1:1433/db",  # C6b opened mssql and mssql+pytds
        "oracle://127.0.0.1/db",
        "127.0.0.1:5432/db",
    ],
)
def test_web_refuses_an_unknown_prefix(url):
    with pytest.raises(SystemExit, match="SQL_CONFIG_INVALID"):
        profile = select_profile({PROFILE_ENV: EXT_PROFILE, "OPSPILOT_SQL_URL": url})
        profile.versions()
        profile.face(FakeClock(start=PIN_NOW))


class _TrackingEnv(Mapping[str, str]):
    def __init__(self, values: Mapping[str, str]):
        self._values = dict(values)
        self.read: set[str] = set()
        self.iterated = False

    def __getitem__(self, key: str) -> str:
        self.read.add(key)
        return self._values[key]

    def __iter__(self):
        self.iterated = True
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def test_web_side_profile_reads_no_sql_credential():
    env = _TrackingEnv(
        {
            PROFILE_ENV: EXT_PROFILE,
            "OPSPILOT_SQL_URL": PG_URL,
            "OPSPILOT_SQL_USERNAME": SQL_USERNAME,
            "OPSPILOT_SQL_PASSWORD": SQL_PASSWORD,
            "OPSPILOT_SQL_CA_FILE": "/nonexistent/sql-ca.pem",
            "OPSPILOT_WORKER_DSN": "postgresql://o@127.0.0.1:15432/x",
        }
    )
    profile = select_profile(env)
    face = profile.face(FakeClock(start=PIN_NOW))
    face.evidence_context(RUN_ID)
    profile.versions()
    assert set(SQL_TOOLS) <= {s["function"]["name"] for s in face.tool_schemas}
    assert not env.read & {
        "OPSPILOT_SQL_USERNAME",
        "OPSPILOT_SQL_PASSWORD",
        "OPSPILOT_SQL_CA_FILE",
    }
    assert not env.iterated


# == lab gate ===================================================================


def test_lab_gate_requires_a_lab_sql_host():
    env = {
        "OPSPILOT_TRACE": "lab",
        PROFILE_ENV: EXT_PROFILE,
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686",
        "OPSPILOT_K8S_API_URL": K8S_API,
        "LANGSMITH_PROJECT": "opspilot-lab-c6a",
        "LANGSMITH_API_KEY": "lsv2-aaaaaaaa570",
        "LANGSMITH_ENDPOINT": "https://api.smith.langchain.com",
    }
    assert check_lab_target(env) == ()
    for lab_url in (
        "postgresql://127.0.0.1:10217/testdb",
        "postgres://localhost/testdb",
        "mysql://127.0.0.1:10218/testdb",
        "mariadb://localhost:3306/testdb",
        "postgresql://postgres.c6t.svc:5432/testdb",
    ):
        assert check_lab_target({**env, "OPSPILOT_SQL_URL": lab_url}) == (), lab_url
    for remote in (
        "postgresql://db.example.invalid:5432/testdb",
        "mysql://10.0.0.5:3306/testdb",
        "postgresql://127.attacker.example/testdb",
    ):
        assert "SQL_NOT_LAB" in check_lab_target({**env, "OPSPILOT_SQL_URL": remote}), (
            remote
        )
    assert "SQL_NOT_LAB" not in check_lab_target(
        {**env, "OPSPILOT_ES_URL": "https://logs.example.invalid:9200"}
    )


# == deploy entrypoint and trace ===================================================


def test_entrypoint_imports_sql_settings_from_the_worker_env_file(tmp_path):
    entrypoint = Path(__file__).resolve().parents[1] / "deploy" / "docker-entrypoint.sh"
    worker_env = tmp_path / "worker_env"
    worker_env.write_text(
        "OPSPILOT_SQL_URL=postgresql://127.0.0.1:10217/testdb\n"
        f"OPSPILOT_SQL_USERNAME={SQL_USERNAME}\n"
        f"OPSPILOT_SQL_PASSWORD='{SQL_PASSWORD}'\n"
        "OPSPILOT_SQL_TLS=disable\n"
        "OPSPILOT_SQL_CA_FILE=/run/secrets/sql-ca.pem\n"
        "OPSPILOT_SQLX=not-a-sql-setting\n"
        "OPSPILOT_SQL_$(touch pwned)=x\n"
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
    assert env["OPSPILOT_SQL_URL"] == "postgresql://127.0.0.1:10217/testdb"
    assert env["OPSPILOT_SQL_USERNAME"] == SQL_USERNAME
    assert env["OPSPILOT_SQL_PASSWORD"] == SQL_PASSWORD
    assert env["OPSPILOT_SQL_TLS"] == "disable"
    assert env["OPSPILOT_SQL_CA_FILE"] == "/run/secrets/sql-ca.pem"
    assert "OPSPILOT_SQLX" not in env
    assert not (tmp_path / "pwned").exists()


def test_credentials_never_appear_in_views_notes_or_trace(monkeypatch):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from tests.test_tracing import lab_env

    exporter = InMemorySpanExporter()
    tracer = tracing.configure(
        lab_env(
            OPSPILOT_TOOL_PROFILE=EXT_PROFILE,
            OPSPILOT_K8S_API_URL=K8S_API,
            OPSPILOT_SQL_URL=PG_URL,
            OPSPILOT_OTEL_PROMETHEUS_URL="http://127.0.0.1:19090",
            OPSPILOT_OTEL_JAEGER_URL="http://127.0.0.1:16686",
        ),
        exporter=exporter,
    )
    try:
        outcomes = []
        tools = make_tools(monkeypatch, "postgresql")
        with tracer.run(incident_id="c6a-incident", run_id=RUN_ID, versions={}):
            outcomes.append(tools.query("SELECT 1 AS n"))
            tools.driver.errors["model"] = _pg_error("42601", "syntax error at or near")
            outcomes.append(tools.query("SELECT FROM WHERE"))
            tools.driver.errors.clear()
            tools.driver.connect_error = _pg_error(
                "28P01", "password authentication failed"
            )
            outcomes.append(tools.query("SELECT 2 AS n"))
            tools.driver.connect_error = None
            tools.driver.problems = ["rolsuper"]
            outcomes.append(tools.query("SELECT 3 AS n"))
        tracer.shutdown()
        spans = exporter.get_finished_spans()
    finally:
        tracing.reset()
    assert any(QUERY in span.name for span in spans)
    exported = json.dumps([dict(span.attributes or {}) for span in spans], default=str)
    rendered = json.dumps([o.model_view for o in outcomes], default=str)
    for secret in (SQL_PASSWORD, K8S_TOKEN, PROM_PASSWORD):
        assert secret not in exported
        assert secret not in rendered
