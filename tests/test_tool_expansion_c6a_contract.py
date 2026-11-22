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
        "otel-demo-ext-c63f058afb18",
        "46f34b6e1ecf4c36cddc9b3b57f437f5b07dfe6cd7add88c936d33e0abd04ba1",
    ),
    (False, False, False, False, False, True): (
        "otel-demo-ext-f5e4ed53f220",
        "b556353cf444a5f9f499a825f05b116f0e08ce7c32376a1c709f3c7f111f1307",
    ),
    (False, False, False, False, True, False): (
        "otel-demo-ext-b478179bc212",
        "0dd01cedf27d9628fcf902a21d25f47aae4b667cc051b5a60a243b551470eeba",
    ),
    (False, False, False, False, True, True): (
        "otel-demo-ext-d106c02180ba",
        "516e3a586a17fa8be419f0a584ba429a48ce3e2f55c3c82a50a3341b307692c4",
    ),
    (False, False, False, True, False, False): (
        "otel-demo-ext-4730c27997c9",
        "42a8de274da6a095513bf9632c9eaa1dea0a6d0c4cbb7246d1d30987746c7181",
    ),
    (False, False, False, True, False, True): (
        "otel-demo-ext-3a2608526746",
        "a01553478a404fb660ad0809b922f7c672d597d7d825f5f90e36de08ae2bc351",
    ),
    (False, False, False, True, True, False): (
        "otel-demo-ext-2ccc8623a304",
        "05713c29d9166827ad08ee53796907189cccfffcebe046f448e6b803f5c75b9f",
    ),
    (False, False, False, True, True, True): (
        "otel-demo-ext-e26e19873a37",
        "87511b2377fdd6d83ea1a88837d1110bee08077af8e0a1d535c4491c824e6333",
    ),
    (False, False, True, False, False, False): (
        "otel-demo-ext-840df01f3bce",
        "43f33bf040746b4297461340389011b10be3dfe6d365989686c7d5ec69339ab8",
    ),
    (False, False, True, False, False, True): (
        "otel-demo-ext-596626e3a941",
        "2b80107e703d0a3484e88bf94c4a7331cada198b547d72dd690bb0945700ffae",
    ),
    (False, False, True, False, True, False): (
        "otel-demo-ext-08aeabe7db49",
        "0f52f3bc044ebff6d364bf26e56db847aa59518d8a9eecd4aa999c3fc9fc7f8e",
    ),
    (False, False, True, False, True, True): (
        "otel-demo-ext-5cecdf3e5356",
        "d04cedc1cd01ddd05ca785807dc6f7e300a3f253f9f5a7b1dba01f8166a78550",
    ),
    (False, False, True, True, False, False): (
        "otel-demo-ext-3eec17c97bae",
        "e4bbeb1a4ebf20478d0705f7fe21f542354489a4c539cdc340b61f54608c415f",
    ),
    (False, False, True, True, False, True): (
        "otel-demo-ext-0e2dcad7fb3b",
        "03eaef62108b20b7f7c4e157d31537304f1cf963a3ccc0c22ac55282a3c0cb77",
    ),
    (False, False, True, True, True, False): (
        "otel-demo-ext-8c481b12121d",
        "29977633fabeb3cf501c2ea7bfde5e44b4cd3c7ba1a5522b85db1b4501875501",
    ),
    (False, False, True, True, True, True): (
        "otel-demo-ext-870e7cd605ee",
        "b77b37ca501f0cb56006e6dc89ed50b1d57a53b10efa82320b18240921b845cc",
    ),
    (False, True, False, False, False, False): (
        "otel-demo-ext-dd942a2f1292",
        "db5d2a478912fb21373b0eed433b6bbfc851e971a64e50f2314ed84d8e933e22",
    ),
    (False, True, False, False, False, True): (
        "otel-demo-ext-85be06c0b7ff",
        "e05a42bc008d39db361bbc4f1e694d4ddb018363f980bfaa04c0c5e658bc0444",
    ),
    (False, True, False, False, True, False): (
        "otel-demo-ext-0a9474cb2a38",
        "42c0ac0bdb381e17db6376b3372945495d43e04265d1bf98eb071ba3457bc32f",
    ),
    (False, True, False, False, True, True): (
        "otel-demo-ext-e869cf409b4c",
        "1cb27161f881a0d45df5362e2ae3841a2e7cd6a16b0a760c571bf28b30c9d2e6",
    ),
    (False, True, False, True, False, False): (
        "otel-demo-ext-4994814c7688",
        "a4d33c87ee980e69f4b8549f7b95857bc30c7ec2f2cdc2b2294b87a1ffc032c7",
    ),
    (False, True, False, True, False, True): (
        "otel-demo-ext-0d723a8e7280",
        "abdc3f7f0e6fdee3384696be0b0b374d7b7cd3dca7f362a02dfa1d806f29c259",
    ),
    (False, True, False, True, True, False): (
        "otel-demo-ext-2e03b6d5942f",
        "a3883ae2b0dc708291033fcc592d4c2679d3c53bce99469b797e4873a512f55a",
    ),
    (False, True, False, True, True, True): (
        "otel-demo-ext-c503aa921af1",
        "9e64b6ce4fb1a31465833d274c3b5945cfd62c6ff2b738e309271090a0b4fb06",
    ),
    (False, True, True, False, False, False): (
        "otel-demo-ext-cbd3b6aaa6b8",
        "68f1f62d281b5984cef6940b16052f5223b7ab34ca2057f46252c714ebc2bf41",
    ),
    (False, True, True, False, False, True): (
        "otel-demo-ext-39b272e9fe31",
        "ee51c27e82494975f83674484aaaf038649b728f17485316712409efed652655",
    ),
    (False, True, True, False, True, False): (
        "otel-demo-ext-f6a19a986de2",
        "fb51d034c19d3bbc594c4ab2ecfc4e6668b8e4ebcae77880faec3a5b2dae68ff",
    ),
    (False, True, True, False, True, True): (
        "otel-demo-ext-dfdfb26794bd",
        "af70d6ede723f85ccb01d71b683578626adcd9fb315c69c1a99f9bac8c030ed0",
    ),
    (False, True, True, True, False, False): (
        "otel-demo-ext-61aad69191fd",
        "344c6146e488c29ba31147fe8a137e890d7b8acdd12ec172c142b34064c6e62b",
    ),
    (False, True, True, True, False, True): (
        "otel-demo-ext-4d54816ee82f",
        "729028ca6dd81c0a7013b5fbf4577027fbf7b5b2ecd44422e7eebe9ee8a304f2",
    ),
    (False, True, True, True, True, False): (
        "otel-demo-ext-887025871d38",
        "905bc91f895bf578a539f0ec9bb7886f82a16647ebf7e42a92cdddb87fb205b8",
    ),
    (False, True, True, True, True, True): (
        "otel-demo-ext-847af8287827",
        "fd5fcf409452a9971dc7ea0a7f90cb13511a192a0ad1e94197cc667ce5e3fe6d",
    ),
    (True, False, False, False, False, False): (
        "otel-demo-ext-4f7faf728706",
        "97aa05da3739c97a5ab47647387760a68d0ffd34aee64da6f36387b72a810f5b",
    ),
    (True, False, False, False, False, True): (
        "otel-demo-ext-54395c6ad55a",
        "1cb3aea3493a2f472fe38ce3a6f7c4c9ba6c432070b4f9aa30722f2181f368ab",
    ),
    (True, False, False, False, True, False): (
        "otel-demo-ext-963351391eff",
        "3cf8f1855a927060ef473a4d7fa01cc2a94a7386136b56ed6c0b89ca12b293e1",
    ),
    (True, False, False, False, True, True): (
        "otel-demo-ext-7aa202c9d9f6",
        "e7ee18900c0bb5a113defcd7d6b7528e4cbdc0e8ef1b0eb906616eb11ead0d81",
    ),
    (True, False, False, True, False, False): (
        "otel-demo-ext-f49f281c318a",
        "501f950e3b51cbf7950e175d324d76bf16857e4b89db9465c4fdc8d8d68e336b",
    ),
    (True, False, False, True, False, True): (
        "otel-demo-ext-1595c5775240",
        "1cee441f831daceabf3f4def18424de66bc94aef6067d8e9b345171588fce4d4",
    ),
    (True, False, False, True, True, False): (
        "otel-demo-ext-1c5703eee208",
        "12df736f7e32f6c1cd470213b19d60cd761f2be82ad4dc64594874b2ce50f529",
    ),
    (True, False, False, True, True, True): (
        "otel-demo-ext-f88a23d4cec4",
        "428152269b2e10ae485458285ef9920238966707dfd083861957a0a0860caa92",
    ),
    (True, False, True, False, False, False): (
        "otel-demo-ext-381e74cfe8d0",
        "fb71c70a1a8cb93abfb36dcc8ff65811732fec161511c78a849364b1878b9f05",
    ),
    (True, False, True, False, False, True): (
        "otel-demo-ext-2b742c1a2380",
        "5947bf3df6f87611b75eb688c7fd08da91dd7eec4b360e41ba592375d37bc650",
    ),
    (True, False, True, False, True, False): (
        "otel-demo-ext-357ef95fb165",
        "30a30b1a7555344665fea59fd145ab711d905bd79aa425d0e13e7a2ee9597998",
    ),
    (True, False, True, False, True, True): (
        "otel-demo-ext-54d7fc1f5d63",
        "4d09c1ed4708a2ba444b05160798c66a9843705d16b31961529c31619f65290e",
    ),
    (True, False, True, True, False, False): (
        "otel-demo-ext-884953bd455e",
        "78e1bf2bb42b7b517a61a3c855c807f3be25f109779b14686caad5d400db39ef",
    ),
    (True, False, True, True, False, True): (
        "otel-demo-ext-0c362dc85592",
        "d98c49f95345292629a63df72a8e6149ff2e2642a07f1a70518f7ca443519152",
    ),
    (True, False, True, True, True, False): (
        "otel-demo-ext-304259988d53",
        "99ad66c94508a4bbc3aeec6eefad413ff78bb15434f5e2738730b450f79def0a",
    ),
    (True, False, True, True, True, True): (
        "otel-demo-ext-97785c2abd49",
        "9a10f56f108f5e109adae4093de4db3b474930fbb57eaabdec288515e8c8d14c",
    ),
    (True, True, False, False, False, False): (
        "otel-demo-ext-f11fc2ce4d18",
        "41d5eea74a65a5c34478cbfa268ca59c7fee5b13e6962c1846dae4dec1a7e578",
    ),
    (True, True, False, False, False, True): (
        "otel-demo-ext-b62634d96bd3",
        "663dacc29b5faf46aef448e33b19efb49f5e98234961f83f54bbdd9f3b9d4b65",
    ),
    (True, True, False, False, True, False): (
        "otel-demo-ext-76f1393a7ecc",
        "5e87e364d0f4bd194d1a8896ff88c36b1848b45e2ffab415ae5c42e4b7a2fa25",
    ),
    (True, True, False, False, True, True): (
        "otel-demo-ext-f269e268cafc",
        "e8af304001d24b74dfc3f14acb9e69c1ac548e9187c9056bd0c7e36febfcfbb8",
    ),
    (True, True, False, True, False, False): (
        "otel-demo-ext-ebbaaaa09494",
        "e3af16ff56d5d8cceabd52e7d6e712a6c24f9af18b6efe9c162c619583f3ac03",
    ),
    (True, True, False, True, False, True): (
        "otel-demo-ext-be0074f762d0",
        "f8ae7ab3f1ac0e70da02c7696110a5fe869bc481bc3903027238e2a224fb832f",
    ),
    (True, True, False, True, True, False): (
        "otel-demo-ext-0c77ddf8c44c",
        "f0a840b0d322b967493c898ad6175142d9b87774baa2b822be934379ff554c0e",
    ),
    (True, True, False, True, True, True): (
        "otel-demo-ext-12eee7829dad",
        "a1d98de6864f03101f4d5ff223b3991265ba65263b2c870267f981868a278649",
    ),
    (True, True, True, False, False, False): (
        "otel-demo-ext-da924862e562",
        "c0792ef4c64d2203c881745ae00e26fcc2ef391ad03861b48434189165bab9d2",
    ),
    (True, True, True, False, False, True): (
        "otel-demo-ext-5d45a802901c",
        "c46fd5ca33fec094aa72649b508d5f73660d75e115641a4b3c43f19fa9f57943",
    ),
    (True, True, True, False, True, False): (
        "otel-demo-ext-a5fba6d19b27",
        "8a8d578665ef85fc4887ca860bc3db1caa50d2c1f81f4cba5e17b6583f9d900d",
    ),
    (True, True, True, False, True, True): (
        "otel-demo-ext-3e0ad5dfaa86",
        "990776302731d10ddd49ddb6e33f57f26dde874a47e814d5053d1695950d3498",
    ),
    (True, True, True, True, False, False): (
        "otel-demo-ext-263512312688",
        "54e4812e17a8a27dbad36f5721ba8b2362932ff7dce9e91caa01505598628f93",
    ),
    (True, True, True, True, False, True): (
        "otel-demo-ext-f3fc5256acae",
        "efbd20e367cd0b493e6abf6a253fbd86d4c8d5390b236f3cb2ccad5213c53c62",
    ),
    (True, True, True, True, True, False): (
        "otel-demo-ext-0eed81430d65",
        "d872059f77b94f0c4619f54395e987ca1519b6f647dcb3f3b336b6559987abce",
    ),
    (True, True, True, True, True, True): (
        "otel-demo-ext-98bf11a1173f",
        "336bf60b8ea5c994c9a8feec78f503b07903a411f5a19576ebcf61a872347a16",
    ),
}
BASE_PINNED = {
    "otel-demo": (
        "otel-demo-a3a95a0f888e",
        "af9cd88eaa10ec43d961169b2c0d1c9ba95ebd29fdf3acde5962854c602e459e",
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
