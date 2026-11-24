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
        "otel-demo-ext-e00b0e7d95c9",
        "07b940679146450e2206d0338363bd256ec8a75ae387406d3631f48cbe81b4a6",
    ),
    (False, False, False, False, False, True): (
        "otel-demo-ext-9c52f9946923",
        "f216b6d090f054c8372139c0f71c199149a686b5df4bd1a35ec6ab82a476fff5",
    ),
    (False, False, False, False, True, False): (
        "otel-demo-ext-419874664bc1",
        "5b89037f0130698dbc13c4a380d65e8709771bfdf7be460a70c489dd3819e9ca",
    ),
    (False, False, False, False, True, True): (
        "otel-demo-ext-40b24bcf6b09",
        "16c17e9330b9430a305427ac9a57316bc4fe4bc98206cbda4fd600cca0e58244",
    ),
    (False, False, False, True, False, False): (
        "otel-demo-ext-e97777ad121f",
        "c7b23beb445f219705da92695b476484afed30dfeea238d2953bb893da8355b1",
    ),
    (False, False, False, True, False, True): (
        "otel-demo-ext-9657d25ca730",
        "f6bbadb93e4c642a593cde4f2ea24ba88ddc221f8800006e2b546f4ce9ac329a",
    ),
    (False, False, False, True, True, False): (
        "otel-demo-ext-1f9ad1079177",
        "2ce01e0a9b3c177369a9f0ee3785091b4b324fe0f9a7dcf4f41b5fd85dbf043e",
    ),
    (False, False, False, True, True, True): (
        "otel-demo-ext-efd2ae89e068",
        "5cd4339f10b55850870e3fa2becb6013764b73be279cbde96232103e9fd3a0eb",
    ),
    (False, False, True, False, False, False): (
        "otel-demo-ext-281d18acd17f",
        "ebb2800bdc4e401f25a1d8fb3f6bc1aadf83c28c4f0ab5d58ac41b41029beaae",
    ),
    (False, False, True, False, False, True): (
        "otel-demo-ext-22310b2a789f",
        "5c0d2ed69af9f06b6cf9d43fcfe75c031226cc4808f0917ba0bcb22b9a63a78c",
    ),
    (False, False, True, False, True, False): (
        "otel-demo-ext-65675b4546dc",
        "25c024c1630305b9d4493937b6551a74416e05d19a03a6549d37be4e8b55fd23",
    ),
    (False, False, True, False, True, True): (
        "otel-demo-ext-c8cc70e2722d",
        "06fc47d8d6ed466ddaeff4d1febda25616cc18999ef6df880e5117a20d008090",
    ),
    (False, False, True, True, False, False): (
        "otel-demo-ext-49cece9bd751",
        "583388b96af86003f9bad96977500247a1b7aca17eff1647f03d1200342c1bd8",
    ),
    (False, False, True, True, False, True): (
        "otel-demo-ext-8923ba5cb744",
        "f46f679be558ee69dd2d9466a251ab6ab05ac47396da1b330c9802051105c387",
    ),
    (False, False, True, True, True, False): (
        "otel-demo-ext-773fafaedb30",
        "5f76108374462bdc1734ccb6d60b27b10d7f4bae7624c2b9c41072eb600d94a9",
    ),
    (False, False, True, True, True, True): (
        "otel-demo-ext-c410d72ab377",
        "654c192ab430d306f140bd0379dd87bf959eab2e17e3a1c3e69cf65346dfc702",
    ),
    (False, True, False, False, False, False): (
        "otel-demo-ext-855bfed29802",
        "d9bd02c2b3a9fde4d8a7352a195148d8388b359e7617474e68b71a199120fb28",
    ),
    (False, True, False, False, False, True): (
        "otel-demo-ext-1f5129041052",
        "af02591f8159ef3fc45ef13a1e89a3a3163cf43a92eb2a619935cea14bd036ac",
    ),
    (False, True, False, False, True, False): (
        "otel-demo-ext-827bb4639269",
        "896f4b5dc1715823d30d53dd04edd7ad850e0d40b9cf9191f788e6b0478353c2",
    ),
    (False, True, False, False, True, True): (
        "otel-demo-ext-b898debd63a6",
        "f2960bf16a69c47ce20efc2ed0494910b3437ee2bdd37c7cc679d78d7c7d44ab",
    ),
    (False, True, False, True, False, False): (
        "otel-demo-ext-3f77ca58dec3",
        "5d0019432762a8e580caec91ca43d376fc2a77a225ba3fa1db26d508f7c2d4f9",
    ),
    (False, True, False, True, False, True): (
        "otel-demo-ext-3e82fdcbd38d",
        "5d9bc5155b57c5a5b035bcae2a70d9a4c11da3ac99289b9523de89f7b4c6447e",
    ),
    (False, True, False, True, True, False): (
        "otel-demo-ext-e2f004fe3872",
        "b6d5da3fb328b1621a18fb9d457d25adaaec3a499adaf34f4897fa8e39003273",
    ),
    (False, True, False, True, True, True): (
        "otel-demo-ext-fce649ab6525",
        "0ff6691e4cb7cf7f2bb7f701773d32b2b37c7d22bb5792791a99c85f4964977d",
    ),
    (False, True, True, False, False, False): (
        "otel-demo-ext-4ed1ed0ec118",
        "6d1ba82d46cb1866414c139d8802ea8c0103e7b29dfd574b2273499da36c8015",
    ),
    (False, True, True, False, False, True): (
        "otel-demo-ext-d697694875b6",
        "f14b02507ab97b9cbd4f92f1d16af7f0d7c0acbbebfd049f36ed7b4707dfe804",
    ),
    (False, True, True, False, True, False): (
        "otel-demo-ext-3be1f2030f9b",
        "014f979bfb0aa796a11f5115f03d93ee3768ccac6a5bbc60156290713e4b1cc6",
    ),
    (False, True, True, False, True, True): (
        "otel-demo-ext-5749ec4ab4fe",
        "bd7f30e1009c86c6cd5497a2eb6cde2547d9403a69cfabea5369ff08ed61747f",
    ),
    (False, True, True, True, False, False): (
        "otel-demo-ext-a153e6e7254f",
        "1f337774b99392265f84f153f4a6d7db1a7449107b10dc589f2b4c542d67736b",
    ),
    (False, True, True, True, False, True): (
        "otel-demo-ext-336eed44bf3e",
        "28852fed603a63c75f3d73b9798f66917bbc5da67923f542be01b90a2ccec3bb",
    ),
    (False, True, True, True, True, False): (
        "otel-demo-ext-54c8f04e73ed",
        "8747d951feb0f5e1d21301944f178543295a787e5374aa39e75756b05b79cfdb",
    ),
    (False, True, True, True, True, True): (
        "otel-demo-ext-97b268cdc93a",
        "d2c352b649fad7ec99d1d65a7dca9f75f9779ae61b59332258b6a8bc16b6e960",
    ),
    (True, False, False, False, False, False): (
        "otel-demo-ext-bd3bc6f91e49",
        "0e31d8c72a32a1af7843ae7fd8b25b1340cd4029e193fc8257c95120f7a378fb",
    ),
    (True, False, False, False, False, True): (
        "otel-demo-ext-bdd79468e8ee",
        "932724f1b862ed52a19023091628c29b861cdf7dcfe05d3c09a516020213bd93",
    ),
    (True, False, False, False, True, False): (
        "otel-demo-ext-7e7758ef3417",
        "3003d53024950fee05f6a11e4856ce3ada7dcefd3fa4b78c12b38a577ed5a357",
    ),
    (True, False, False, False, True, True): (
        "otel-demo-ext-494abb5db4d9",
        "76be803a8a49570470c776a903b8973248d718f83d04f57bea2a86b534648e44",
    ),
    (True, False, False, True, False, False): (
        "otel-demo-ext-b0a0a9603dc7",
        "966e2a67ecdfb54d029d68e2b737488e7670d42b418925af2540b23f393185a6",
    ),
    (True, False, False, True, False, True): (
        "otel-demo-ext-85860083d005",
        "9f1ab2f3003626473f5d450c917337bb41ecbc1a57546c6d887862f6c9f34835",
    ),
    (True, False, False, True, True, False): (
        "otel-demo-ext-21a1ce81e50b",
        "f847b604cf5d897797084674b341a46b12fc0bf2825ca8b6c4f25ae0fd22bd4a",
    ),
    (True, False, False, True, True, True): (
        "otel-demo-ext-4c07888c011c",
        "9c7c611e92a26027daee75d2d447e7e1a5b7d3f6227ebf1a5bffa8a26e43493d",
    ),
    (True, False, True, False, False, False): (
        "otel-demo-ext-6452516290e8",
        "d75716801dca8c707bb0d2344a06cb618fc073c6d378ce07a1e79d39da63bad4",
    ),
    (True, False, True, False, False, True): (
        "otel-demo-ext-250907dc15fd",
        "3dd3ce6a708f1bf16c6c24f0c266add7b6fb949b9519506ee770292575487da1",
    ),
    (True, False, True, False, True, False): (
        "otel-demo-ext-94bdc8197f2a",
        "accf34d89340592c71be9f8fe381dc4da9818fd7187499867e62ef082f38bf18",
    ),
    (True, False, True, False, True, True): (
        "otel-demo-ext-ebc824e7c54c",
        "eef5919bf80d1bc6a3df293eb82ce585ed07177d97defe4f32d9e028fff6aef3",
    ),
    (True, False, True, True, False, False): (
        "otel-demo-ext-8b1bab736a76",
        "eab52638f4a2d2c68daee46f88a13816f73b2cd7a2d20029e76bc21dbf0eac42",
    ),
    (True, False, True, True, False, True): (
        "otel-demo-ext-e62b89a23526",
        "2278271beac740dba10054661ad7596c9fccd5dae7a9b6514bef17fb7c4d4221",
    ),
    (True, False, True, True, True, False): (
        "otel-demo-ext-1a861d866693",
        "d19cbbf0cf1e926a438572e6d81f5ddecb4733bff626147ea4f56780ae91c280",
    ),
    (True, False, True, True, True, True): (
        "otel-demo-ext-ed8f198fe4dc",
        "dc4833bf2c3ad6b23b734ae6cb30305c9f3a375d08e941a90978e9b721f69d41",
    ),
    (True, True, False, False, False, False): (
        "otel-demo-ext-b2f074df8640",
        "3f49cd8809df18aad07aa725a5cd591db7852a2352eb8ea5c4864d6e1132234a",
    ),
    (True, True, False, False, False, True): (
        "otel-demo-ext-36d3f6285fb8",
        "d5ff8201a064eede23e4a1b593ffc6c902109a8a4e6449a3c14a99e39c532c9e",
    ),
    (True, True, False, False, True, False): (
        "otel-demo-ext-803a7e5faca0",
        "d2bbdde7e28e4982429f852ede61cd1516789139e84e32d53882123f1c7d6fa1",
    ),
    (True, True, False, False, True, True): (
        "otel-demo-ext-3f628e0a742e",
        "7f87087a69e206596b9045071ecb23c06d38b342e741f8a028d743b4628ceb0e",
    ),
    (True, True, False, True, False, False): (
        "otel-demo-ext-6feea6e4abe2",
        "6bedb21f9a2fd5f0b341ff7dddc8803d4312f4d502e9f38c3860525923f402af",
    ),
    (True, True, False, True, False, True): (
        "otel-demo-ext-932ec294c540",
        "b079185aa0d3979b60dca2f41c416de23161963907d9af99fbdd1082c85a88a2",
    ),
    (True, True, False, True, True, False): (
        "otel-demo-ext-31fc86f32c20",
        "ba166789743e29ee2bc259947ced71ca6f48e01e3629400bb1a5699f25d6e7f2",
    ),
    (True, True, False, True, True, True): (
        "otel-demo-ext-e6dd035508c5",
        "8d3d7f603a28d9a18201c658805872f1ee991a692c9897ee8b85e4a0c162e2b3",
    ),
    (True, True, True, False, False, False): (
        "otel-demo-ext-27b7b844fa1f",
        "6463581e9e91b8b6c10630e7d191ea8c92f8a8cacda6731f3f38d8887b08d1d8",
    ),
    (True, True, True, False, False, True): (
        "otel-demo-ext-45d5f694de1d",
        "accd07d48d83b30314f7d8e8c9e562778607ce463feb0d59342615542b7243ba",
    ),
    (True, True, True, False, True, False): (
        "otel-demo-ext-061707745e8a",
        "40a26062a87fcd77aad5a9dff41207eddf66877dc3194063743f04122cca1c88",
    ),
    (True, True, True, False, True, True): (
        "otel-demo-ext-e3ac93815787",
        "39524cec9b889eb38e16ba19fdd4aba11a4804341c2cbd54eaaee8d6d28f8cb6",
    ),
    (True, True, True, True, False, False): (
        "otel-demo-ext-24db655aa725",
        "f41ae53c564f091941b465485f42b27fc9deb7db78429428bf6c1712803f47d4",
    ),
    (True, True, True, True, False, True): (
        "otel-demo-ext-cfc9d4912d8f",
        "b30afbf9c2276229fe4a6309f4307ff24c8b6e63ece81bdab9548c10c5116ff8",
    ),
    (True, True, True, True, True, False): (
        "otel-demo-ext-4bd928337576",
        "67b00d51e71a42a863a3fb34e8de9a6b0b4cb9d02f7d7facf06e53b6d845cc00",
    ),
    (True, True, True, True, True, True): (
        "otel-demo-ext-285691e55a36",
        "32cc62a63bcb080bfa2f2e1343302b4f0c89c81ff38f7c6a6ae0cf0d5f40697e",
    ),
}
BASE_PINNED = {
    "otel-demo": (
        "otel-demo-75925b8f2e3d",
        "72396af53db774d115ab953ea5a2cedc0b526b7f0591e6dbbd94965ccfc6033f",
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
