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
        "otel-demo-ext-f94273f95547",
        "05ca2e1517ec52b7949f79f2fc32e5e74219f2bb03b5363671bdcbbad471b280",
    ),
    (False, False, False, False, False, True): (
        "otel-demo-ext-32377aee10c3",
        "96e36535c746246b0937a5a51c3ff2bd3017e2713b7e5b8f9d70b0bf01200f5e",
    ),
    (False, False, False, False, True, False): (
        "otel-demo-ext-a32741d1cbda",
        "19a6595a907375ad8c5d8c070ac0278446981765d125c3cd2e94ce3bd3dd8d1e",
    ),
    (False, False, False, False, True, True): (
        "otel-demo-ext-62647c33f00a",
        "93d875200e1eb1fd24f924119165d12ee9e848f05dc0a081ced6b4cdaac8a4a7",
    ),
    (False, False, False, True, False, False): (
        "otel-demo-ext-d63dc71f41c2",
        "39d598f84122307f47257016107d448db61686eb26e14a6c5eac7346cdcc407e",
    ),
    (False, False, False, True, False, True): (
        "otel-demo-ext-279059cd4400",
        "0a4fd6d9f92896ac6bbe9994bbe21b4af1488007b99427817fc5d398da8523b2",
    ),
    (False, False, False, True, True, False): (
        "otel-demo-ext-030e68084357",
        "a19bda974ba0e61a905bf0ea93e4019c9d3fe2e4a675db100051375a1dd5b51c",
    ),
    (False, False, False, True, True, True): (
        "otel-demo-ext-1ca88b51717a",
        "f09e0baa07eb64a6a92fafffcfa8696500270d84877ec6594923820a07ad2a1a",
    ),
    (False, False, True, False, False, False): (
        "otel-demo-ext-778042c7b8bd",
        "6a47ae80424f5c23a38c308f245c692c4b288477badc1f0abf42b1e40eb2ed7c",
    ),
    (False, False, True, False, False, True): (
        "otel-demo-ext-b064210d5f36",
        "ab08dab5466c183e92d827798c6eb00331963fd0c37c08da24cb04061528f31a",
    ),
    (False, False, True, False, True, False): (
        "otel-demo-ext-7e2a424e0189",
        "b02c5809cfe90d717338f267121f9b0b82a41a891260091a3487d844f50977df",
    ),
    (False, False, True, False, True, True): (
        "otel-demo-ext-a42d61c66228",
        "4b3995d60020a51ad17685b296162291542e37590c8b39cf32d01f5f033c845a",
    ),
    (False, False, True, True, False, False): (
        "otel-demo-ext-c6618148999c",
        "ac8bd66a4bdac51092ec461fb50f377a6e7ae692c9b5a7b4cf72f5cf5ea663e1",
    ),
    (False, False, True, True, False, True): (
        "otel-demo-ext-4a382900214f",
        "45a9b3bed3dce26c37c96b43345de4e634a286c607ac83b017c874d1ef905510",
    ),
    (False, False, True, True, True, False): (
        "otel-demo-ext-957f06d98771",
        "46db6833dab8ce4974628779fa364179b97ff0de250cc91708c892abcb253190",
    ),
    (False, False, True, True, True, True): (
        "otel-demo-ext-050743466abd",
        "09da05bc5c2565716b9e5d240ed5ac2a2241c2906573cd9a9e87503208ecefb4",
    ),
    (False, True, False, False, False, False): (
        "otel-demo-ext-8e517e935ae7",
        "95b02412aedd4f462f4d08e7f4afe215200585c57eef15442d4aeb97e8633cda",
    ),
    (False, True, False, False, False, True): (
        "otel-demo-ext-5af98f201752",
        "c02fc6def034151f4cb7b5e6e2309ef8e43c4fd5e8aca9bb9c35e4c2d2a2c747",
    ),
    (False, True, False, False, True, False): (
        "otel-demo-ext-2eb0e5988eb0",
        "09819caf4a1578c5c0a9189ebc69cca4349d738c9b4902fcd8d021fbcd659b9b",
    ),
    (False, True, False, False, True, True): (
        "otel-demo-ext-51e58adef8ad",
        "cdcf954d714eb2340e7e93fcc7b8ab6e1c44cb69475abe11efdada63952de90b",
    ),
    (False, True, False, True, False, False): (
        "otel-demo-ext-dcde6afdd1ed",
        "58f79d4d143bbf08437a8ec68351862359aeea5af6631483bd57efcf877fcad6",
    ),
    (False, True, False, True, False, True): (
        "otel-demo-ext-1693a2368316",
        "f25a2a6d1ca8c02f339dc9a4151c183e2baeb10939559c85429025bb5eb92550",
    ),
    (False, True, False, True, True, False): (
        "otel-demo-ext-8ceefd1f68ce",
        "d1ebe7e4b76dd0153ea7ccc06075a3c4a546aef9205db38cf1e8d10f803cef7f",
    ),
    (False, True, False, True, True, True): (
        "otel-demo-ext-f43daf9b832f",
        "0e861c671a857831dcaabd479f95c3c317400f840b41248e481a0da59ca996cc",
    ),
    (False, True, True, False, False, False): (
        "otel-demo-ext-050fe627e880",
        "66e1148b14edeefa9bf021ad6e1e1a525477a9ec456c5ff2c103194917261949",
    ),
    (False, True, True, False, False, True): (
        "otel-demo-ext-4f4a1b074925",
        "6dae7c04ea1a97158938866d856a3321f287f4db4d356cdd7eb7eea3adc6ac3f",
    ),
    (False, True, True, False, True, False): (
        "otel-demo-ext-102a5e7b7184",
        "790cb44a74fea914e289b3c38b6530869c9786c59925d000c1d0301984b4f473",
    ),
    (False, True, True, False, True, True): (
        "otel-demo-ext-81baae5be513",
        "641e78228ed4a79f9e8df786696826bb594dc95d93f75b0482381bc52d90387b",
    ),
    (False, True, True, True, False, False): (
        "otel-demo-ext-f9e322e2cef9",
        "8a14a2859920f72527dbeead4321d41ffa5ec290e3481dbd7eaa4052877b9449",
    ),
    (False, True, True, True, False, True): (
        "otel-demo-ext-536792bcc6fa",
        "7e22bdf7d9b80c05d71eb96df3ad010fead1cac937dee74166a65e0309483e4d",
    ),
    (False, True, True, True, True, False): (
        "otel-demo-ext-8ebc58b941b5",
        "bf797bc417edd388c3f31328e64d804975eb86884eb89f61e5488bdb9ac2cb62",
    ),
    (False, True, True, True, True, True): (
        "otel-demo-ext-c2a39bfa7a72",
        "93d80f2cb298122a846791c3fac4b4d92cfc574aa8ebab1e42edfbcb7a3fa2af",
    ),
    (True, False, False, False, False, False): (
        "otel-demo-ext-328399ccd3ae",
        "c3a23597872e17b8b46363b93767ccdde9eaae17b2691e95e9c56d955884634d",
    ),
    (True, False, False, False, False, True): (
        "otel-demo-ext-65f3bf5c9cba",
        "06199ac75aaefac5fd2313d53d1eda140f95451f2ce8469f90945a60819b89d0",
    ),
    (True, False, False, False, True, False): (
        "otel-demo-ext-e0daf25410fd",
        "06f8ce4b85b069b581bd48906b0552644d6d6ba50aab2a73a5addda4b500a443",
    ),
    (True, False, False, False, True, True): (
        "otel-demo-ext-d9c3e7e87a4d",
        "6549f7d2701e07ae4e2f15119c8ad22fe820094789b46a26bc2ddc4d7d52ec20",
    ),
    (True, False, False, True, False, False): (
        "otel-demo-ext-965275c42fd8",
        "72ded204ed02f982d8c3874d918c7dcce569214140742e0612c5e75a4c5e1c8a",
    ),
    (True, False, False, True, False, True): (
        "otel-demo-ext-4693399f178b",
        "ac67525b1e4bfa09eba259dcb5d84df022e109bc8fd83517d9f5ef727d6d9483",
    ),
    (True, False, False, True, True, False): (
        "otel-demo-ext-70e5e3ca5e9f",
        "9471e59e127f9fcf53d262406dc69057c78075477bc99d87837aa451609ad407",
    ),
    (True, False, False, True, True, True): (
        "otel-demo-ext-2088510b9b21",
        "f2f8bffa9b0facf3af46a046352dffe262ad374e6a82cd7803c2621f75a9af96",
    ),
    (True, False, True, False, False, False): (
        "otel-demo-ext-bd3d9abe6275",
        "fa252c3d844cb20723a2419a448e45d847b92bbcaa8cf2d604ea0484d7c24c2d",
    ),
    (True, False, True, False, False, True): (
        "otel-demo-ext-9ec84babf3e4",
        "75e0d2622340a958cdb2e9c377d1d2f32896d56eeaef71b117048faa87a08a0e",
    ),
    (True, False, True, False, True, False): (
        "otel-demo-ext-d076c831ff96",
        "5a89e0b7090982ffb968d104cdc2d95a1e31ee386390d89bfbba5aeb9baba44d",
    ),
    (True, False, True, False, True, True): (
        "otel-demo-ext-e027e7a27520",
        "a099f579b0b32035f3bf16ddcdda46d3868eddb0eac5d25c67e49cf748c2ac44",
    ),
    (True, False, True, True, False, False): (
        "otel-demo-ext-b1fd386619a8",
        "61e0d90aa403c94bf5e8c22c71df74bfbd84506b422b7f383c83be7bb37c650b",
    ),
    (True, False, True, True, False, True): (
        "otel-demo-ext-589ee23252c3",
        "11ae43f06a274abc2dd3f56ebb64c9aa772ab7507b8d678d7a3ee58a2dc2e486",
    ),
    (True, False, True, True, True, False): (
        "otel-demo-ext-93c30f593adf",
        "ceef11678083ec08c95e23dccac6b196f894a8ffd62f408f117e8f4959b1eeaa",
    ),
    (True, False, True, True, True, True): (
        "otel-demo-ext-c68724ff2619",
        "e6547c6ec2652ab43c3e62b9483275861b544f341f20455247a2e18ca69e067b",
    ),
    (True, True, False, False, False, False): (
        "otel-demo-ext-f6cde82c67c8",
        "04844e664e6a70d964aa18fd65782cec89452e7fbec82d8f19739698756a5584",
    ),
    (True, True, False, False, False, True): (
        "otel-demo-ext-b2a63cdf18d0",
        "153c9d61fc569dad950e2f228902621848075965890990f1849be9120c1eba23",
    ),
    (True, True, False, False, True, False): (
        "otel-demo-ext-14f45222e3d7",
        "4f8958f6cac14cee79c5c2c1668a6bd9eb3269eb4db78d9bbe68fe91c35cd766",
    ),
    (True, True, False, False, True, True): (
        "otel-demo-ext-0bd8ba03f7db",
        "dd16c3ec85519d2052f90b81fd6f35167d41f8951e25d97691dd87d7fc99b83c",
    ),
    (True, True, False, True, False, False): (
        "otel-demo-ext-d9b634288e7b",
        "bf8255666a8ca2dc0258fe28b592cf15f292e5940bb336ee1884d8e79c4488cd",
    ),
    (True, True, False, True, False, True): (
        "otel-demo-ext-ca59642e87d6",
        "e8415578dc817da7705953576c12c755b0fa40e01b44f3b008af8fbf2fc23ca2",
    ),
    (True, True, False, True, True, False): (
        "otel-demo-ext-e917b31f551d",
        "701811f84ebdf77887cecd12626161f3e8603d7785ae46dfc8c2691295fa9e4d",
    ),
    (True, True, False, True, True, True): (
        "otel-demo-ext-38c8890415cf",
        "4a369172a4e5d57e20d6f9733f1ac16b32f679b2f7e62e77224f36e947bc5f61",
    ),
    (True, True, True, False, False, False): (
        "otel-demo-ext-2f1df6abd15b",
        "165bc342a99838c0860a7b85e2a600d0c0805287cdd2e8d6e2d3ce42975a812f",
    ),
    (True, True, True, False, False, True): (
        "otel-demo-ext-e3a36a21a19b",
        "4b7dde7c4fd5a40b361825b90b9f3ba2fc66a9f8c930375ddae47a6645f88fe8",
    ),
    (True, True, True, False, True, False): (
        "otel-demo-ext-a7194d74ca0b",
        "6272618cadf4875837170070391f56f6d7816377d48a1b0d1c6ecfb921dd13b3",
    ),
    (True, True, True, False, True, True): (
        "otel-demo-ext-a39212949b8a",
        "75463cd8b7b9c90ab8ff37d17e9a56a193552f2ceef198a475fb3020d13a37dd",
    ),
    (True, True, True, True, False, False): (
        "otel-demo-ext-1fd1ab30617d",
        "855d65c2f99122a2bb1caa12cc03730cb7578ea0562dc896a0767e9de22e4df4",
    ),
    (True, True, True, True, False, True): (
        "otel-demo-ext-a05949c50e56",
        "0ba5f9bb7246fa4e4e9493a668aa33a6002558d06588263fb837485d6466cca3",
    ),
    (True, True, True, True, True, False): (
        "otel-demo-ext-6debe49c2855",
        "ee29d11f3ff010566bdde1d9b23fd27ca209a129509d34b1cdb8e779cd5f89be",
    ),
    (True, True, True, True, True, True): (
        "otel-demo-ext-17161933d365",
        "446b1ccf65b8f5e80c166a297b4c401a9cf7d520b556d5eec66f29da176b1a2f",
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
