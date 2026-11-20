"""Independent contract tests for tool expansion B4 (#563): Helm release
history -- ``helm_list``, ``helm_history`` and ``helm_status`` -- and the
OpsPilot gateway that narrows a Secret-listing identity to Helm records.

Written from the B4 task record (docs/tasks/2026-11-20-tool-expansion-b4.md,
"行为（公开接口）"), decision table R8-18 and r11 (B4C4-U1=a, the corrected
R8-18 wording: the identity can read every Secret's content) and the B4/C4
pre-review (B4-5 ... B4-13, contract tests 1-8), without reading the
implementation. Red until the implementation lands; collection stays green.

Two seams are exercised:

* the gateway: the pure function
  ``kubernetes_core.secret_request_problem(url, accept, *, helm_page)`` and
  ``KubernetesClient(..., helm_releases=).get(..., helm_page=)``, which raises
  ``KubernetesRequestRefused`` without calling its opener (a spy here);
* the tools: through the public path -- build ``otel-demo-ext`` from an env
  mapping, call the tools through the worker's executor, and assert on the
  view, evidence, logs, spans and what a loopback HTTPS fake API server
  received. No container, no cluster, no model.

Baseline note: C6a (SQL, #570/#746) and C3 (Kafka, #567) are not on main yet.
The interface puts the three Helm tools after every data-source backend
(including those two once merged) and before ``fetch_skill`` / ``TodoWrite``.
So nothing here pins a revision or surface hash: the switch-off surface is
compared with the same profile built without the switch, and the switch-on
surface with the switch-off one plus exactly the documented delta.

Seams assumed beyond the interface (each also marked ``# ASSUMPTION:``):

* An invalid switch value or a non-lab cluster address ends the start with
  ``SystemExit`` whose text contains the code, as every other startup code.
* ``otel_demo_ext._registrations(helm=True)`` and
  ``otel_demo_ext.tool_schema_revision(helm=True)`` take ``helm`` as a
  keyword (the interface names ``tool_schema_revision(..., helm=)``).
* ``KubernetesRequestRefused`` carries its code in ``str()`` or ``.code``.
* A pre-request refusal surfaces as the tool's error class with
  ``source_contact: none``; rows are the view's ``content``.

Synthetic credentials and canaries are low-entropy placeholders.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
import os
import random
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

import pytest

from opspilot.tools import ToolRequest
from opspilot.tools.outcomes import Window
from opspilot.tools.profiles import PROFILE_ENV, ToolProfile, select_profile
from opspilot.tools.registry import canonical
from tests.m1_tool_support import FakeClock, RecordingSink
from tests.test_m1_otel_demo_contract import NOW, TARGET_ID, FakeStore, _lease

REPO = Path(__file__).resolve().parents[1]
EXT_PROFILE = "otel-demo-ext"
SWITCH = "OPSPILOT_HELM_RELEASES"
LIST = "helm_list"
HISTORY = "helm_history"
STATUS = "helm_status"
HELM_TOOLS = (LIST, HISTORY, STATUS)
REFUSED = "RESOURCE_NOT_ALLOWED"
HELM_TYPE = "helm.sh/release.v1"
ROW_FIELDS = {
    "name",
    "namespace",
    "revision",
    "status",
    "chart",
    "chart_version",
    "app_version",
    "first_deployed",
    "last_deployed",
    "deleted",
    "description",
    "rollback_revision",
}

K8S_TOKEN = "k8s-ro-aaaaaaaa563"
PROM_PASSWORD = "bbbbbbbb563"
RUN_ID = "b4-run"
MIB = 1024 * 1024
API = "https://127.0.0.1:6443"

FRAME = Window(NOW - timedelta(hours=6), NOW)
FORBIDDEN_NOTE = (
    "This cluster's read-only identity has no permission to read Helm release records."
)
_FIRST_NOTE = re.compile(
    r"Helm release records as stored now \(queried at (\S+)\); revisions "
    r"outside the authorized window are listed too\."
)
_RFC3339_Z = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


# ======================================================================================
# 1. The gateway: secret_request_problem and KubernetesClient.get
# ======================================================================================

FS = ("fieldSelector", "type=helm.sh/release.v1")
LS_ALL = ("labelSelector", "owner=helm")
LS_SHOP = ("labelSelector", "owner=helm,name=shop")
LIMIT = ("limit", "5")
PAGE_MARK = "aaaa+bbbb/cccc_dddd-eeee=="


def _q(*pairs: tuple[str, str]) -> str:
    return urlencode(list(pairs))


LIST_Q = _q(FS, LS_ALL, LIMIT)
SHOP_Q = _q(FS, LS_SHOP, LIMIT)
CLUSTER = f"{API}/api/v1/secrets"
NS = f"{API}/api/v1/namespaces/b4-a/secrets"

LEGAL = {
    "cluster-list": f"{CLUSTER}?{LIST_Q}",
    "namespace-list": f"{NS}?{LIST_Q}",
    "named": f"{NS}?{SHOP_Q}",
    "cluster-continue": f"{CLUSTER}?{_q(FS, LS_ALL, LIMIT, ('continue', PAGE_MARK))}",
    "named-continue": f"{NS}?{_q(FS, LS_SHOP, LIMIT, ('continue', 'abc'))}",
    "continue-4096": f"{NS}?{_q(FS, LS_SHOP, LIMIT, ('continue', 'a' * 4096))}",
    "dotted-release": f"{NS}?{_q(FS, ('labelSelector', 'owner=helm,name=my.app-1'), LIMIT)}",
    "53-char-release": f"{NS}?{_q(FS, ('labelSelector', 'owner=helm,name=' + 'r' * 53), LIMIT)}",
    # A namespace that happens to be called ``secrets``.
    "namespace-named-secrets": f"{API}/api/v1/namespaces/secrets/secrets?{LIST_Q}",
}


def _ns_url(query: str) -> str:
    return f"{NS}?{query}" if query is not None else NS


_PATHS = {
    # resource position case and percent-encoding
    "path-Secrets": f"{API}/api/v1/Secrets?{LIST_Q}",
    "path-SECRETS": f"{API}/api/v1/SECRETS?{LIST_Q}",
    "ns-path-SeCrEtS": f"{API}/api/v1/namespaces/b4-a/SeCrEtS?{LIST_Q}",
    "path-%73ecrets": f"{API}/api/v1/%73ecrets?{LIST_Q}",
    "path-secret%73": f"{API}/api/v1/secret%73?{LIST_Q}",
    "ns-path-%53ecrets": f"{API}/api/v1/namespaces/b4-a/%53ecrets?{LIST_Q}",
    "ns-encoded": f"{API}/api/v1/namespaces/%62%34-a/secrets?{LIST_Q}",
    # empty, dot and dot-dot segments
    "leading-double-slash": f"{API}//api/v1/secrets?{LIST_Q}",
    "double-slash-api": f"{API}/api//v1/secrets?{LIST_Q}",
    "double-slash-resource": f"{API}/api/v1//secrets?{LIST_Q}",
    "double-slash-namespace": f"{API}/api/v1/namespaces//secrets?{LIST_Q}",
    "double-slash-ns-resource": f"{API}/api/v1/namespaces/b4-a//secrets?{LIST_Q}",
    "dot-segment": f"{API}/api/v1/./secrets?{LIST_Q}",
    "dotdot-segment": f"{API}/api/v1/pods/../secrets?{LIST_Q}",
    "dotdot-namespace": f"{API}/api/v1/namespaces/b4-a/../secrets?{LIST_Q}",
    "dotdot-encoded": f"{API}/api/v1/pods/%2E%2E/secrets?{LIST_Q}",
    "dotdot-encoded-lower": f"{API}/api/v1/pods/%2e%2e/secrets?{LIST_Q}",
    "dot-encoded": f"{API}/api/v1/%2E/secrets?{LIST_Q}",
    "encoded-slash-namespace": f"{API}/api/v1/namespaces/b4-a%2Fsecrets?{LIST_Q}",
    "encoded-slash-resource": f"{API}/api/v1/secrets%2Fplain-canary?{LIST_Q}",
    "encoded-slash-lower": f"{API}/api/v1/namespaces/b4-a/secrets%2fx?{LIST_Q}",
    # trailing slash, names and subresources
    "trailing-slash": f"{API}/api/v1/secrets/?{LIST_Q}",
    "trailing-slash-ns": f"{API}/api/v1/namespaces/b4-a/secrets/?{LIST_Q}",
    "named-secret": f"{API}/api/v1/namespaces/b4-a/secrets/sh.helm.release.v1.shop.v1",
    "named-secret-query": f"{API}/api/v1/namespaces/b4-a/secrets/plain-canary?{LIST_Q}",
    "named-secret-subresource": (
        f"{API}/api/v1/namespaces/b4-a/secrets/sh.helm.release.v1.shop.v1/status"
    ),
    "cluster-named-secret": f"{API}/api/v1/secrets/plain-canary",
    # namespace that is not a DNS label
    "ns-upper": f"{API}/api/v1/namespaces/B4-A/secrets?{LIST_Q}",
    "ns-underscore": f"{API}/api/v1/namespaces/b4_a/secrets?{LIST_Q}",
    "ns-leading-dash": f"{API}/api/v1/namespaces/-b4/secrets?{LIST_Q}",
    "ns-64-chars": f"{API}/api/v1/namespaces/{'n' * 64}/secrets?{LIST_Q}",
    # other API group paths
    "apis-core": f"{API}/apis/core/v1/secrets?{LIST_Q}",
    "apis-namespaced": f"{API}/apis/apps/v1/namespaces/b4-a/secrets?{LIST_Q}",
    # watch and proxy resource positions
    "legacy-watch": f"{API}/api/v1/watch/secrets?{LIST_Q}",
    "legacy-watch-ns": f"{API}/api/v1/watch/namespaces/b4-a/secrets?{LIST_Q}",
    "legacy-WATCH": f"{API}/api/v1/WATCH/secrets?{LIST_Q}",
    "legacy-%77atch": f"{API}/api/v1/%77atch/secrets?{LIST_Q}",
    "apis-watch": f"{API}/apis/apps/v1/watch/namespaces/b4-a/secrets?{LIST_Q}",
    "legacy-proxy": f"{API}/api/v1/proxy/namespaces/b4-a/secrets?{LIST_Q}",
    # URL parts
    "fragment": f"{NS}?{LIST_Q}#x",
    "userinfo": f"https://u:p@127.0.0.1:6443/api/v1/namespaces/b4-a/secrets?{LIST_Q}",
    "userinfo-user": f"https://u@127.0.0.1:6443/api/v1/secrets?{LIST_Q}",
}

_QUERIES = {
    # keys missing
    "no-query": NS,
    "empty-query": f"{NS}?",
    "no-fieldSelector": _ns_url(_q(LS_ALL, LIMIT)),
    "no-labelSelector": _ns_url(_q(FS, LIMIT)),
    "no-limit": _ns_url(_q(FS, LS_ALL)),
    # repeated keys, both orders
    "fs-twice-valid-first": _ns_url(
        _q(FS, ("fieldSelector", "type=Opaque"), LS_ALL, LIMIT)
    ),
    "fs-twice-valid-last": _ns_url(
        _q(("fieldSelector", "type=Opaque"), FS, LS_ALL, LIMIT)
    ),
    "fs-twice-identical": _ns_url(_q(FS, FS, LS_ALL, LIMIT)),
    "ls-twice": _ns_url(_q(FS, LS_ALL, ("labelSelector", "owner=helm"), LIMIT)),
    "limit-twice": _ns_url(_q(FS, LS_ALL, LIMIT, LIMIT)),
    "continue-twice": _ns_url(
        _q(FS, LS_ALL, LIMIT, ("continue", "a"), ("continue", "b"))
    ),
    # fieldSelector values
    "fs-not-equal": _ns_url(
        _q(("fieldSelector", "type!=helm.sh/release.v1"), LS_ALL, LIMIT)
    ),
    "fs-double-equal": _ns_url(
        _q(("fieldSelector", "type==helm.sh/release.v1"), LS_ALL, LIMIT)
    ),
    "fs-extra-condition": _ns_url(
        _q(
            ("fieldSelector", "type=helm.sh/release.v1,metadata.name=plain-canary"),
            LS_ALL,
            LIMIT,
        )
    ),
    "fs-extra-type": _ns_url(
        _q(("fieldSelector", "type=helm.sh/release.v1,type!=Opaque"), LS_ALL, LIMIT)
    ),
    "fs-opaque": _ns_url(_q(("fieldSelector", "type=Opaque"), LS_ALL, LIMIT)),
    "fs-empty": _ns_url(_q(("fieldSelector", ""), LS_ALL, LIMIT)),
    "fs-value-case": _ns_url(
        _q(("fieldSelector", "type=Helm.sh/release.v1"), LS_ALL, LIMIT)
    ),
    "fs-key-case": _ns_url(
        _q(("fieldSelector", "Type=helm.sh/release.v1"), LS_ALL, LIMIT)
    ),
    "fs-leading-space": _ns_url(
        _q(("fieldSelector", " type=helm.sh/release.v1"), LS_ALL, LIMIT)
    ),
    "fs-trailing-space": _ns_url(
        _q(("fieldSelector", "type=helm.sh/release.v1 "), LS_ALL, LIMIT)
    ),
    "fs-metadata-only": _ns_url(
        _q(("fieldSelector", "metadata.namespace=b4-a"), LS_ALL, LIMIT)
    ),
    # key case and key encoding
    "key-FieldSelector": _ns_url(_q(("FieldSelector", FS[1]), LS_ALL, LIMIT)),
    "key-fieldselector": _ns_url(_q(("fieldselector", FS[1]), LS_ALL, LIMIT)),
    "key-LabelSelector": _ns_url(_q(FS, ("LabelSelector", "owner=helm"), LIMIT)),
    "key-Limit": _ns_url(_q(FS, LS_ALL, ("Limit", "5"))),
    "key-Continue": _ns_url(_q(FS, LS_ALL, LIMIT, ("Continue", "abc"))),
    "key-field%53elector": _ns_url(LIST_Q.replace("fieldSelector", "field%53elector")),
    "key-%66ieldSelector": _ns_url(LIST_Q.replace("fieldSelector", "%66ieldSelector")),
    "key-label%53elector": _ns_url(LIST_Q.replace("labelSelector", "label%53elector")),
    # extra keys
    "watch-true": _ns_url(_q(FS, LS_ALL, LIMIT, ("watch", "true"))),
    "watch-1": _ns_url(_q(FS, LS_ALL, LIMIT, ("watch", "1"))),
    "watch-first": _ns_url(_q(("watch", "true"), FS, LS_ALL, LIMIT)),
    "resourceVersion-0": _ns_url(_q(FS, LS_ALL, LIMIT, ("resourceVersion", "0"))),
    "resourceVersionMatch": _ns_url(
        _q(
            FS,
            LS_ALL,
            LIMIT,
            ("resourceVersion", "0"),
            ("resourceVersionMatch", "NotOlderThan"),
        )
    ),
    "timeoutSeconds": _ns_url(_q(FS, LS_ALL, LIMIT, ("timeoutSeconds", "1"))),
    "allowWatchBookmarks": _ns_url(
        _q(FS, LS_ALL, LIMIT, ("allowWatchBookmarks", "true"))
    ),
    "sendInitialEvents": _ns_url(_q(FS, LS_ALL, LIMIT, ("sendInitialEvents", "true"))),
    "pretty": _ns_url(_q(FS, LS_ALL, LIMIT, ("pretty", "true"))),
    "as-Table": _ns_url(_q(FS, LS_ALL, LIMIT, ("as", "Table"))),
    "includeObject": _ns_url(_q(FS, LS_ALL, LIMIT, ("includeObject", "Object"))),
    # ';' separators
    "semicolon-extra": _ns_url(LIST_Q + ";watch=true"),
    "semicolon-separators": _ns_url(LIST_Q.replace("&", ";")),
    # limit values
    "limit-500": _ns_url(_q(FS, LS_ALL, ("limit", "500"))),
    "limit-0": _ns_url(_q(FS, LS_ALL, ("limit", "0"))),
    "limit-05": _ns_url(_q(FS, LS_ALL, ("limit", "05"))),
    "limit-6": _ns_url(_q(FS, LS_ALL, ("limit", "6"))),
    "limit-empty": _ns_url(_q(FS, LS_ALL, ("limit", ""))),
    "limit-space": _ns_url(_q(FS, LS_ALL, ("limit", "5 "))),
    "limit-%35": _ns_url(LIST_Q.replace("limit=5", "limit=%35")),
    # labelSelector values
    "ls-extra-term": _ns_url(
        _q(FS, ("labelSelector", "owner=helm,name=shop,status=deployed"), LIMIT)
    ),
    "ls-swapped": _ns_url(_q(FS, ("labelSelector", "name=shop,owner=helm"), LIMIT)),
    "ls-name-upper": _ns_url(_q(FS, ("labelSelector", "owner=helm,name=Shop"), LIMIT)),
    "ls-name-empty": _ns_url(_q(FS, ("labelSelector", "owner=helm,name="), LIMIT)),
    "ls-name-not-equal": _ns_url(
        _q(FS, ("labelSelector", "owner=helm,name!=shop"), LIMIT)
    ),
    "ls-set-based": _ns_url(_q(FS, ("labelSelector", "owner in (helm)"), LIMIT)),
    "ls-name-underscore": _ns_url(
        _q(FS, ("labelSelector", "owner=helm,name=a_b"), LIMIT)
    ),
    "ls-name-54": _ns_url(
        _q(FS, ("labelSelector", "owner=helm,name=" + "r" * 54), LIMIT)
    ),
    "ls-name-double-dot": _ns_url(
        _q(FS, ("labelSelector", "owner=helm,name=a..b"), LIMIT)
    ),
    "ls-trailing-comma": _ns_url(_q(FS, ("labelSelector", "owner=helm,"), LIMIT)),
    "ls-double-equal": _ns_url(_q(FS, ("labelSelector", "owner==helm"), LIMIT)),
    "ls-owner-case": _ns_url(_q(FS, ("labelSelector", "owner=Helm"), LIMIT)),
    "ls-empty": _ns_url(_q(FS, ("labelSelector", ""), LIMIT)),
    "ls-existence": _ns_url(_q(FS, ("labelSelector", "owner=helm,!name"), LIMIT)),
    "ls-two-names": _ns_url(
        _q(FS, ("labelSelector", "owner=helm,name=shop,name=cart"), LIMIT)
    ),
    "ls-space": _ns_url(_q(FS, ("labelSelector", "owner=helm, name=shop"), LIMIT)),
    "ls-other-owner": _ns_url(_q(FS, ("labelSelector", "owner=helm,owner=x"), LIMIT)),
    # continue values
    "continue-empty": _ns_url(_q(FS, LS_ALL, LIMIT, ("continue", ""))),
    "continue-space": _ns_url(_q(FS, LS_ALL, LIMIT, ("continue", "a b"))),
    "continue-ampersand": _ns_url(_q(FS, LS_ALL, LIMIT, ("continue", "x&watch=true"))),
    "continue-dot": _ns_url(_q(FS, LS_ALL, LIMIT, ("continue", "a.b"))),
    "continue-nul": _ns_url(_q(FS, LS_ALL, LIMIT, ("continue", "a\x00"))),
    "continue-4097": _ns_url(_q(FS, LS_ALL, LIMIT, ("continue", "a" * 4097))),
    # the raw string differs from its re-encoding
    "unencoded-values": _ns_url(
        "fieldSelector=type=helm.sh/release.v1&labelSelector=owner=helm&limit=5"
    ),
    "lowercase-hex": _ns_url(LIST_Q.replace("%3D", "%3d").replace("%2F", "%2f")),
    "over-encoded-dots": _ns_url(LIST_Q.replace("helm.sh", "helm%2Esh")),
    "over-encoded-letters": _ns_url(LIST_Q.replace("owner", "%6Fwner")),
    "key-order-label-first": _ns_url(_q(LS_ALL, FS, LIMIT)),
    "key-order-limit-first": _ns_url(_q(LIMIT, FS, LS_ALL)),
    "key-order-continue-early": _ns_url(_q(FS, LS_ALL, ("continue", "abc"), LIMIT)),
    "raw-plus-in-continue": _ns_url(_q(FS, LS_ALL, LIMIT) + "&continue=tok+5/="),
    "space-as-%20": _ns_url(_q(FS, LS_ALL, LIMIT) + "&continue=a%20b"),
    "trailing-ampersand": _ns_url(LIST_Q + "&"),
    "double-ampersand": _ns_url(LIST_Q.replace("&", "&&", 1)),
    "bare-key": _ns_url(LIST_Q + "&watch"),
    "bad-percent": _ns_url(LIST_Q + "&continue=%zz"),
}

_ACCEPTS = {
    "accept-table": "application/json;as=Table;v=v1;g=meta.k8s.io",
    "accept-table-fallback": (
        "application/json;as=Table;g=meta.k8s.io;v=v1, application/json"
    ),
    "accept-partial-metadata": (
        "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"
    ),
    "accept-protobuf": "application/vnd.kubernetes.protobuf",
    "accept-protobuf-fallback": "application/vnd.kubernetes.protobuf, application/json",
    "accept-json-any": "application/json, */*",
    "accept-any": "*/*",
    "accept-case": "Application/JSON",
    "accept-trailing-space": "application/json ",
    "accept-leading-space": " application/json",
    "accept-yaml": "application/yaml",
    "accept-empty": "",
    "accept-watch-stream": "application/json;stream=watch",
    "accept-charset": "application/json; charset=utf-8",
}

#: (url, accept, helm_page) for every request the gateway must refuse.
BYPASSES: dict[str, tuple[str, str, bool]] = {
    **{k: (v, "application/json", True) for k, v in _PATHS.items()},
    **{k: (v, "application/json", True) for k, v in _QUERIES.items()},
    **{k: (LEGAL["named"], v, True) for k, v in _ACCEPTS.items()},
    # the exact legal shapes, but not from the Helm transport
    **{f"not-helm-page-{k}": (v, "application/json", False) for k, v in LEGAL.items()},
}

#: Ordinary requests of the existing tools: never Secrets, so not narrowed --
#: including names and namespaces that happen to be called ``secrets``.
ORDINARY = {
    "namespace-named-secrets": f"{API}/api/v1/namespaces/secrets/pods?limit=500",
    "configmap-named-secrets": f"{API}/api/v1/namespaces/b4-a/configmaps/secrets",
    "pod-named-secrets-log": f"{API}/api/v1/namespaces/b4-a/pods/secrets/log?container=app",
    "apps-namespace-secrets": f"{API}/apis/apps/v1/namespaces/secrets/deployments?limit=500",
    "namespace-named-watch": f"{API}/api/v1/namespaces/watch/pods?limit=500",
    "pod-named-proxy": f"{API}/api/v1/namespaces/b4-a/pods/proxy",
    "nodes-continue": f"{API}/api/v1/nodes?{urlencode([('limit', '500'), ('continue', 'x')])}",
    "metrics-pods": f"{API}/apis/metrics.k8s.io/v1beta1/pods",
    "events-field-selector": (
        f"{API}/api/v1/namespaces/b4-a/events?"
        + urlencode([("limit", "500"), ("fieldSelector", "type!=Normal")])
    ),
}

#: Refused for every request, Secret or not (B4-5 steps 1 and 2).
ALWAYS_REFUSED = {
    "watch-pods": f"{API}/api/v1/watch/pods",
    "watch-apps": f"{API}/apis/apps/v1/watch/deployments",
    "watch-namespaced": f"{API}/api/v1/namespaces/b4-a/watch",
    "proxy-nodes": f"{API}/api/v1/proxy/nodes/n1",
    "double-slash-pods": f"{API}/api/v1//pods",
    "dotdot-to-secrets": f"{API}/api/v1/namespaces/b4-a/pods/../secrets",
    "dot-segment-pods": f"{API}/api/v1/namespaces/b4-a/./pods",
    "encoded-slash-pod-name": f"{API}/api/v1/namespaces/b4-a/pods/a%2Fb/log",
}


def _problem(url: str, accept: str = "application/json", *, helm_page: bool = True):
    from opspilot.tools.kubernetes_core import secret_request_problem

    return secret_request_problem(url, accept, helm_page=helm_page)


def test_the_bypass_table_is_large_and_distinct():
    # Guard the table itself: every case is a different request.
    assert len(BYPASSES) >= 150
    assert len(set(BYPASSES.values())) == len(BYPASSES)
    legal = {(url, "application/json", True) for url in LEGAL.values()}
    assert not legal & set(BYPASSES.values())


@pytest.mark.parametrize("name", sorted(LEGAL))
def test_the_two_legal_shapes_and_continue_pages_pass(name):
    assert _problem(LEGAL[name]) is None


@pytest.mark.parametrize("name", sorted(BYPASSES))
def test_every_bypass_is_refused_by_the_pure_function(name):
    url, accept, helm_page = BYPASSES[name]
    assert _problem(url, accept, helm_page=helm_page) == REFUSED


@pytest.mark.parametrize("name", sorted(ORDINARY))
@pytest.mark.parametrize("helm_page", [False, True])
def test_ordinary_requests_are_not_narrowed(name, helm_page):
    # Names or namespaces that happen to be ``secrets`` keep working (I-11).
    assert _problem(ORDINARY[name], helm_page=helm_page) is None
    assert (
        _problem(
            ORDINARY[name],
            "application/json;as=Table;v=v1;g=meta.k8s.io",
            helm_page=helm_page,
        )
        is None
    )


@pytest.mark.parametrize("name", sorted(ALWAYS_REFUSED))
@pytest.mark.parametrize("helm_page", [False, True])
def test_watch_proxy_and_unsafe_segments_are_refused_for_every_request(name, helm_page):
    assert _problem(ALWAYS_REFUSED[name], helm_page=helm_page) == REFUSED


# -- the client: refusal raises before the opener runs ---------------------------------


class _SpyResponse:
    status = 200

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size: int = -1) -> bytes:
        body, self._body = self._body, b""
        return body if size < 0 else body[:size]


class SpyOpener:
    """Records every open; answers an empty list."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def open(self, request, timeout=None):
        self.calls.append(request.full_url)
        return _SpyResponse(b'{"kind":"SecretList","apiVersion":"v1","items":[]}')


def _client(opener: SpyOpener, **kwargs):
    from opspilot.tools.kubernetes_core import CREDENTIAL_REF, KubernetesClient

    return KubernetesClient(
        credentials={CREDENTIAL_REF: K8S_TOKEN}, opener=opener, **kwargs
    )


def _get(client, url: str, accept: str = "application/json", **kwargs):
    return client.get(
        url, deadline=time.monotonic() + 5, max_bytes=MIB, accept=accept, **kwargs
    )


def _refusal_code(error: BaseException) -> str:
    # ASSUMPTION: the code is in ``.code`` or ``str()``.
    return getattr(error, "code", None) or str(error)


def test_refusal_is_its_own_exception_not_a_transport_error():
    from opspilot.tools.executor import TransportError
    from opspilot.tools.kubernetes_core import KubernetesRequestRefused

    assert not issubclass(KubernetesRequestRefused, TransportError)


@pytest.mark.parametrize("name", sorted(BYPASSES))
def test_every_bypass_raises_in_the_client_and_never_opens(name):
    from opspilot.tools.kubernetes_core import KubernetesRequestRefused

    url, accept, helm_page = BYPASSES[name]
    opener = SpyOpener()
    client = _client(opener, helm_releases=True)
    with pytest.raises(KubernetesRequestRefused) as caught:
        _get(client, url, accept, helm_page=helm_page)
    assert REFUSED in _refusal_code(caught.value)
    assert opener.calls == []


@pytest.mark.parametrize("name", sorted(LEGAL))
def test_a_legal_helm_page_is_sent_once(name):
    opener = SpyOpener()
    fetched = _get(_client(opener, helm_releases=True), LEGAL[name], helm_page=True)
    assert fetched.status == 200
    assert opener.calls == [LEGAL[name]]


@pytest.mark.parametrize("helm_page", [False, True])
@pytest.mark.parametrize(
    "kwargs", [{}, {"helm_releases": False}], ids=["default", "off"]
)
def test_with_the_switch_off_even_legal_shapes_are_refused(kwargs, helm_page):
    from opspilot.tools.kubernetes_core import KubernetesRequestRefused

    for url in LEGAL.values():
        opener = SpyOpener()
        with pytest.raises(KubernetesRequestRefused):
            _get(_client(opener, **kwargs), url, helm_page=helm_page)
        assert opener.calls == [], url


@pytest.mark.parametrize("helm_releases", [False, True])
def test_the_client_still_sends_ordinary_requests_and_refuses_watch(helm_releases):
    from opspilot.tools.kubernetes_core import KubernetesRequestRefused

    for url in ORDINARY.values():
        opener = SpyOpener()
        _get(_client(opener, helm_releases=helm_releases), url)
        assert opener.calls == [url]
    for url in ALWAYS_REFUSED.values():
        opener = SpyOpener()
        with pytest.raises(KubernetesRequestRefused):
            _get(_client(opener, helm_releases=helm_releases), url)
        assert opener.calls == [], url


# ======================================================================================
# 2. Synthetic Helm records (Secret objects as the API server returns them)
# ======================================================================================

T0 = datetime(2026, 11, 20, 9, 0, 0, tzinfo=timezone.utc)


def go_time(moment: datetime, *, helm4: bool = False) -> str:
    """Go's RFC3339Nano as Helm writes it: Helm 3 in a local offset with
    nanoseconds, Helm 4 in UTC."""
    if helm4:
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.5Z")
    local = moment.astimezone(timezone(timedelta(hours=-8)))
    return local.strftime("%Y-%m-%dT%H:%M:%S.123456789-08:00")


def _z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def release(
    name: str,
    namespace: str,
    version: int,
    *,
    status: str = "deployed",
    chart: str = "b4-fixture",
    chart_version: str = "0.1.0",
    app_version: str | None = "1.0.0",
    first: datetime = T0,
    last: datetime | None = None,
    deleted: object = "",
    description: str = "Install complete",
    rollback_revision: object = None,
    helm4: bool = False,
) -> dict[str, Any]:
    last = last or first + timedelta(minutes=version)
    metadata: dict[str, Any] = {
        "name": chart,
        "version": chart_version,
        "apiVersion": "v2",
        "description": "A synthetic chart",
        "type": "application",
    }
    if app_version is not None:
        metadata["appVersion"] = app_version
    info: dict[str, Any] = {
        "first_deployed": go_time(first, helm4=helm4),
        "last_deployed": go_time(last, helm4=helm4),
        "deleted": deleted,
        "description": description,
        "status": status,
        "notes": "Thank you for installing.",
    }
    if rollback_revision is not None:
        info["rollback_revision"] = rollback_revision
    body: dict[str, Any] = {
        "name": name,
        "info": info,
        "chart": {
            "metadata": metadata,
            "lock": None,
            "templates": [
                {"name": "templates/configmap.yaml", "data": _b64(b"kind: ConfigMap\n")}
            ],
            "values": {"replicas": 1},
            "schema": None,
            "files": [],
        },
        "config": {},
        "manifest": "---\n# Source: b4-fixture/templates/configmap.yaml\n",
        "hooks": [],
        "version": version,
        "namespace": namespace,
    }
    if helm4:
        info["resources"] = {"v1/ConfigMap": [{"kind": "ConfigMap"}]}
        body["labels"] = {}
        body["apply_method"] = "csa"
    return body


def encode(body: object, *, gzipped: bool = True) -> str:
    """``data.release``: K8s base64 of Helm's base64 of (gzip of) the JSON."""
    raw = body if isinstance(body, bytes) else _json(body)
    if gzipped:
        raw = gzip.compress(raw)
    return _b64(_b64(raw).encode())


def secret(
    body: Mapping[str, Any] | None,
    *,
    namespace: str | None = None,
    name: str | None = None,
    version: object = None,
    gzipped: bool = True,
    data: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
    extra_labels: Mapping[str, str] | None = None,
    annotations: Mapping[str, str] | None = None,
    secret_type: str = HELM_TYPE,
    release_data: str | None = None,
) -> dict[str, Any]:
    """A Secret as the API server returns it; defaults follow ``body``."""
    body = body or {}
    rel_name = name or body.get("name", "x")
    rel_version = version if version is not None else body.get("version", 1)
    ns = namespace or body.get("namespace", "b4-a")
    status = body.get("info", {}).get("status", "deployed") if body else "deployed"
    item_labels = (
        labels
        if labels is not None
        else {
            "name": str(rel_name),
            "owner": "helm",
            "status": str(status),
            "version": str(rel_version),
            "modifiedAt": "1763629200",
        }
    )
    item_labels = {**item_labels, **(extra_labels or {})}
    payload = (
        data
        if data is not None
        else {
            "release": release_data
            if release_data is not None
            else encode(body, gzipped=gzipped)
        }
    )
    metadata: dict[str, Any] = {
        "name": f"sh.helm.release.v1.{rel_name}.v{rel_version}",
        "namespace": ns,
        "uid": "00000000-0000-0000-0000-000000000563",
        "resourceVersion": "563",
        "creationTimestamp": _z(T0),
        "labels": item_labels,
        "managedFields": [{"manager": "helm", "operation": "Update"}],
    }
    if annotations:
        metadata["annotations"] = dict(annotations)
    return {
        "kind": "Secret",
        "apiVersion": "v1",
        "metadata": metadata,
        "type": secret_type,
        "data": payload,
    }


def history(name: str, namespace: str, count: int, **kwargs) -> list[dict[str, Any]]:
    """``count`` revisions, the last deployed and the others superseded."""
    return [
        secret(
            release(
                name,
                namespace,
                v,
                status="deployed" if v == count else "superseded",
                description="Install complete" if v == 1 else "Upgrade complete",
                **kwargs,
            )
        )
        for v in range(1, count + 1)
    ]


def status_body(code: int, reason: str, message: str) -> bytes:
    return _json(
        {
            "kind": "Status",
            "apiVersion": "v1",
            "metadata": {},
            "status": "Failure",
            "message": message,
            "reason": reason,
            "code": code,
        }
    )


# -- the fake API server ---------------------------------------------------------------

_SECRETS_PATH = re.compile(r"/api/v1/(?:namespaces/([^/]+)/)?secrets")


class FakeApiServer:
    """A loopback HTTPS API server listing Secrets the way kube-apiserver does
    (type field selector, equality label selector, ``limit`` and
    ``continue``), recording every request."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.secrets: list[dict[str, Any]] = []
        #: request ordinal -> (status, body) / raw 200 body.
        self.fail_at: dict[int, tuple[int, bytes]] = {}
        self.raw_at: dict[int, bytes] = {}
        self.delay = 0.0
        self._server: ThreadingHTTPServer | None = None
        self.url = ""
        self.ca: Path | None = None
        self._lock = threading.Lock()

    def _list(self, namespace: str | None, query: Mapping[str, str]) -> bytes:
        items = [
            s
            for s in self.secrets
            if namespace is None or s["metadata"]["namespace"] == namespace
        ]
        selector = query.get("fieldSelector")
        if selector:
            key, _, value = selector.partition("=")
            assert key == "type", selector
            items = [s for s in items if s["type"] == value]
        labels = query.get("labelSelector")
        if labels:
            for term in labels.split(","):
                key, _, value = term.partition("=")
                items = [s for s in items if s["metadata"]["labels"].get(key) == value]
        limit = int(query.get("limit") or 0) or max(len(items), 1)
        token = query.get("continue") or ""
        offset = int(token[2:-2]) if token else 0
        page = items[offset : offset + limit]
        following = offset + limit
        metadata: dict[str, Any] = {"resourceVersion": "563"}
        if following < len(items):
            metadata["continue"] = f"c+{following}/="
            metadata["remainingItemCount"] = len(items) - following
        return _json(
            {
                "kind": "SecretList",
                "apiVersion": "v1",
                "metadata": metadata,
                "items": page,
            }
        )

    def answer(self, index: int, record: dict[str, Any]) -> tuple[int, bytes]:
        if self.delay:
            time.sleep(self.delay)
        if record["method"] != "GET":
            return 405, status_body(405, "MethodNotAllowed", "not allowed")
        if index in self.fail_at:
            return self.fail_at[index]
        if index in self.raw_at:
            return 200, self.raw_at[index]
        query = {k: v[-1] for k, v in record["query"].items()}
        match = _SECRETS_PATH.fullmatch(record["path"])
        if match:
            return 200, self._list(match[1], query)
        if record["path"].startswith(("/api/v1/", "/apis/")):
            return 200, _json(
                {"kind": "List", "apiVersion": "v1", "metadata": {}, "items": []}
            )
        return 404, status_body(404, "NotFound", "the server could not find it")

    def start(self, tls: tuple[Path, Path]) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                return None

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                parts = urlsplit(self.path)
                record = {
                    "method": self.command,
                    "raw_path": parts.path,
                    "path": unquote(parts.path),
                    "raw_query": parts.query,
                    "query": parse_qs(parts.query, keep_blank_values=True),
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": body,
                }
                with fake._lock:
                    index = len(fake.requests)
                    fake.requests.append(record)
                status, payload = fake.answer(index, record)
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except OSError:
                    pass

            do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = _serve

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        server.block_on_close = False
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(tls[0]), str(tls[1]))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        self._server = server
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        ).start()
        self.url = f"https://127.0.0.1:{server.server_address[1]}"
        self.ca = tls[0]

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    def secret_requests(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if _SECRETS_PATH.fullmatch(r["path"])]


@pytest.fixture(scope="module")
def tls_pair(tmp_path_factory) -> tuple[Path, Path]:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("openssl is needed to mint the synthetic API server CA")
    folder = tmp_path_factory.mktemp("b4-tls")
    cert, key = folder / "apiserver.pem", folder / "apiserver-key.pem"
    subprocess.run(
        [
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "2",
            "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return cert, key


@pytest.fixture
def api(tls_pair) -> Iterator[FakeApiServer]:
    fake = FakeApiServer()
    fake.start(tls_pair)
    yield fake
    fake.stop()


OTHER_SWITCHES = (
    SWITCH,
    "OPSPILOT_ES_URL",
    "OPSPILOT_LOKI_URL",
    "OPSPILOT_VICTORIALOGS_URL",
    "OPSPILOT_K8S_LIVE_METRICS",
    "OPSPILOT_KNOWLEDGE_TOOL",
    "OPSPILOT_TODO_WRITE",
    "OPSPILOT_TRACE",
)


@pytest.fixture(autouse=True)
def _clean_environment(tmp_path, monkeypatch):
    for name in OTHER_SWITCHES:
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    placeholder = tmp_path / "synthetic-ca-placeholder.pem"
    placeholder.write_text("synthetic ca placeholder\n")
    monkeypatch.setenv("B4_TEST_PLACEHOLDER_CA", str(placeholder))


def _env(api: FakeApiServer | None = None, **overrides: str | None) -> dict[str, str]:
    """A worker environment with the switch on, pointed at ``api``."""
    values: dict[str, str | None] = {
        PROFILE_ENV: EXT_PROFILE,
        SWITCH: "1",
        "OPSPILOT_K8S_API_URL": api.url
        if api is not None
        else "https://127.0.0.1:16443",
        "OPSPILOT_K8S_TOKEN": K8S_TOKEN,
        "OPSPILOT_K8S_CA_FILE": str(api.ca)
        if api is not None and api.ca
        else os.environ["B4_TEST_PLACEHOLDER_CA"],
        "OPSPILOT_K8S_NAMESPACE": "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
        "OPSPILOT_OTEL_PROMETHEUS_USERNAME": "investigator",
        "OPSPILOT_OTEL_PROMETHEUS_PASSWORD": PROM_PASSWORD,
    }
    values.update(overrides)
    return {k: v for k, v in values.items() if v is not None}


def _require_tools() -> None:
    if shutil.which("jq") is None or shutil.which("python3") is None:
        pytest.skip("the extension worker needs jq and python3 on PATH")


def _input(profile: ToolProfile, lease, clock: FakeClock, deadline: datetime):
    return profile.face(clock).input_for(
        run_id=str(lease.run_id),
        question="When was the last release in b4-a?",
        target_id=TARGET_ID,
        deadline=deadline,
        model_requests=12,
    )


class Worker:
    def __init__(self, env: Mapping[str, str], *, deadline_seconds: float = 600):
        _require_tools()
        clock = FakeClock(start=NOW)
        profile = select_profile(env)
        assert isinstance(profile, ToolProfile)
        if env.get(SWITCH) == "1":
            # Positive control: refusal tests are vacuous without the tools.
            names = {s["function"]["name"] for s in profile.face(clock).tool_schemas}
            assert set(HELM_TOOLS) <= names
        lease = _lease()
        deadline = NOW + timedelta(seconds=deadline_seconds)
        self.store = FakeStore(lease, deadline=deadline)
        self.sink = RecordingSink()
        factory = profile.executor_factory(self.store, self.sink, clock)
        self.executor = factory(lease, _input(profile, lease, clock, deadline))
        self._index = 0

    def call(self, tool: str, params: Mapping[str, object] | None = None):
        request = ToolRequest(
            step_id="b4-step",
            tool_index=self._index,
            tool_name=tool,
            target_ref=TARGET_ID,
            params=dict(params or {}),
            window=FRAME.as_json(),
        )
        self._index += 1
        return self.executor.execute(request)


def _run(
    api: FakeApiServer, tool: str, params: Mapping[str, object] | None = None, **env
):
    return Worker(_env(api, **env)).call(tool, params)


def _view(outcome) -> Mapping[str, Any]:
    assert outcome.model_view is not None
    return outcome.model_view


def _text(outcome) -> str:
    return json.dumps(_view(outcome), ensure_ascii=False, sort_keys=True)


def _rows(outcome) -> list[Mapping[str, Any]]:
    assert outcome.status == "ok", _view(outcome)
    content = _view(outcome)["content"]
    assert isinstance(content, list), _view(outcome)
    return content


def _notes(outcome) -> list[str]:
    notes = _view(outcome).get("notes") or []
    assert isinstance(notes, list), _view(outcome)
    return [str(n) for n in notes]


def _failed(outcome, reason: str) -> None:
    assert (outcome.status, outcome.reason) == ("error", reason), _view(outcome)
    assert _view(outcome).get("content") is None, _view(outcome)


def _expected_query(label: str, token: str | None = None) -> str:
    pairs = [FS, ("labelSelector", label), LIMIT]
    if token is not None:
        pairs.append(("continue", token))
    return urlencode(pairs)


# ======================================================================================
# 3. Requests the tools send
# ======================================================================================


def _assert_plain_get(record: Mapping[str, Any]) -> None:
    assert record["method"] == "GET", record
    assert record["body"] == b""
    assert record["headers"].get("authorization") == f"Bearer {K8S_TOKEN}"
    assert record["headers"].get("accept") == "application/json"
    assert PROM_PASSWORD not in json.dumps(record["headers"])


def _passes_gateway(api: FakeApiServer, record: Mapping[str, Any]) -> bool:
    url = f"{API}{record['raw_path']}?{record['raw_query']}"
    return _problem(url, record["headers"].get("accept", "")) is None


@pytest.mark.parametrize(
    ("tool", "params", "path", "label"),
    [
        (LIST, {}, "/api/v1/secrets", "owner=helm"),
        (LIST, {"namespace": "b4-a"}, "/api/v1/namespaces/b4-a/secrets", "owner=helm"),
        (
            HISTORY,
            {"release_name": "shop", "namespace": "b4-a"},
            "/api/v1/namespaces/b4-a/secrets",
            "owner=helm,name=shop",
        ),
        (
            STATUS,
            {"release_name": "shop", "namespace": "b4-a"},
            "/api/v1/namespaces/b4-a/secrets",
            "owner=helm,name=shop",
        ),
    ],
)
def test_each_tool_sends_exactly_the_documented_list_pages(
    api, tool, params, path, label
):
    api.secrets = history("shop", "b4-a", 12)  # three pages of five
    outcome = _run(api, tool, params)
    assert outcome.status == "ok", _view(outcome)
    assert [r["raw_path"] for r in api.requests] == [path] * 3
    expected = [
        _expected_query(label),
        _expected_query(label, "c+5/="),
        _expected_query(label, "c+10/="),
    ]
    assert [r["raw_query"] for r in api.requests] == expected
    for record in api.requests:
        _assert_plain_get(record)
        assert _passes_gateway(api, record)


def test_helm_list_without_namespace_spans_the_cluster(api):
    api.secrets = [
        *history("shop", "b4-a", 2),
        *history("cart", "b4-b", 1, helm4=True),
    ]
    outcome = _run(api, LIST)
    assert [(r["namespace"], r["name"]) for r in _rows(outcome)] == [
        ("b4-a", "shop"),
        ("b4-b", "cart"),
    ]
    assert _view(outcome)["scope"] == {"namespace": None, "all_namespaces": True}
    outcome = _run(api, LIST, {"namespace": "b4-b"})
    assert [r["name"] for r in _rows(outcome)] == ["cart"]
    assert _view(outcome)["scope"] == {"namespace": "b4-b", "all_namespaces": False}


def test_other_secrets_are_never_read_even_when_present(api):
    api.secrets = [
        *history("shop", "b4-a", 1),
        secret(None, name="plain-canary", secret_type="Opaque",
               data={"value": _b64(b"CANARY-B4-OPAQUE")}, labels={}),
    ]  # fmt: skip
    outcome = _run(api, LIST)
    assert [r["name"] for r in _rows(outcome)] == ["shop"]
    assert "CANARY-B4-OPAQUE" not in _text(outcome)
    assert all(r["query"]["fieldSelector"] == [FS[1]] for r in api.requests)


INVALID_NAMESPACES = ["B4-A", "-b4", "b4-", "b4_a", "n" * 64, "b4/../kube-system",
                      "b4 a", "b4%2Fx", "secrets/x", "", 7]  # fmt: skip
INVALID_RELEASES = [
    "Shop",
    "shop_1",
    "-shop",
    "shop-",
    "a..b",
    ".shop",
    "shop.",
    "r" * 54,
    "shop,owner=x",
    "shop,name=cart",
    "shop&watch=true",
    "shop name",
    "shop%2C",
    "shop!",
    "",
    7,
    None,
]


@pytest.mark.parametrize("namespace", INVALID_NAMESPACES)
@pytest.mark.parametrize("tool", HELM_TOOLS)
def test_a_namespace_that_is_not_a_dns_label_is_refused_before_any_request(
    api, tool, namespace
):
    params: dict[str, object] = {"namespace": namespace}
    if tool != LIST:
        params["release_name"] = "shop"
    outcome = _run(api, tool, params)
    assert outcome.status in ("error", "denied"), _view(outcome)
    if outcome.status == "error":
        assert outcome.reason == "INVALID_PARAMS", _view(outcome)
        assert _view(outcome).get("source_contact") == "none"
    assert api.requests == []


@pytest.mark.parametrize("release_name", INVALID_RELEASES)
@pytest.mark.parametrize("tool", (HISTORY, STATUS))
def test_a_release_name_outside_helms_rule_is_refused_before_any_request(
    api, tool, release_name
):
    outcome = _run(api, tool, {"release_name": release_name, "namespace": "b4-a"})
    assert outcome.status in ("error", "denied"), _view(outcome)
    if outcome.status == "error":
        assert outcome.reason == "INVALID_PARAMS", _view(outcome)
        assert _view(outcome).get("source_contact") == "none"
    assert api.requests == []


@pytest.mark.parametrize(
    "release_name", ["a", "r" * 53, "my.app-1", "a1.b2.c3", "0shop"]
)
def test_release_names_within_helms_rule_are_sent_in_the_label_selector(
    api, release_name
):
    api.secrets = history(release_name, "b4-a", 1)
    rows = _rows(_run(api, STATUS, {"release_name": release_name, "namespace": "b4-a"}))
    assert [r["name"] for r in rows] == [release_name]
    assert api.requests[0]["query"]["labelSelector"] == [
        f"owner=helm,name={release_name}"
    ]


@pytest.mark.parametrize(
    ("tool", "params"),
    [
        (HISTORY, {"release_name": "shop"}),
        (HISTORY, {"namespace": "b4-a"}),
        (STATUS, {"release_name": "shop"}),
        (STATUS, {}),
        (HISTORY, {"release_name": "shop", "namespace": "b4-a", "max": 3}),
        (STATUS, {"release_name": "shop", "namespace": "b4-a", "revision": 2}),
        (LIST, {"label_selector": "owner=helm"}),
        (LIST, {"field_selector": "type=Opaque"}),
        (LIST, {"path": "/api/v1/secrets"}),
        (LIST, {"release_name": "shop"}),
        (LIST, {"start": "2026-11-20T00:00:00Z"}),
        (HISTORY, {"release_name": "shop", "namespace": "b4-a", "output": "values"}),
    ],
)
def test_parameters_outside_the_schema_never_reach_the_cluster(api, tool, params):
    outcome = _run(api, tool, params)
    assert outcome.status in ("error", "denied"), _view(outcome)
    assert api.requests == []


def _schemas() -> dict[str, Mapping[str, Any]]:
    return {
        s["function"]["name"]: s["function"]
        for s in _face({PROFILE_ENV: EXT_PROFILE, SWITCH: "1"}).tool_schemas
    }


def test_schemas_expose_only_the_documented_parameters():
    schemas = _schemas()
    listing = schemas[LIST]["parameters"]
    assert set(listing.get("properties") or {}) == {"namespace"}
    assert not listing.get("required")
    for name in (HISTORY, STATUS):
        params = schemas[name]["parameters"]
        assert set(params.get("properties") or {}) == {"release_name", "namespace"}
        assert set(params.get("required") or ()) == {"release_name", "namespace"}
    assert not {
        "helm_values",
        "helm_manifest",
        "helm_hooks",
        "helm_notes",
        "helm_chart",
    } & set(schemas)


def test_descriptions_say_what_is_and_is_not_given():
    schemas = _schemas()
    for name in HELM_TOOLS:
        text = json.dumps(schemas[name], ensure_ascii=False).lower()
        for withheld in ("values", "manifest", "hooks", "notes"):
            assert withheld in text, (name, withheld)
        assert "replicaset" in text, name  # changes outside Helm: look there
        assert "event" in text, name
        assert "window" in text, name  # not cut to the incident frame


@pytest.mark.parametrize("namespace", ["secrets", "watch", "proxy"])
def test_existing_tools_still_reach_namespaces_named_like_resources(api, namespace):
    outcome = _run(
        api,
        "kubernetes_jq_query",
        {"kind": "pods", "namespace": namespace, "jq_expr": ".items[]"},
    )
    assert outcome.status in ("ok", "no_data"), _view(outcome)
    assert [r["raw_path"] for r in api.requests] == [
        f"/api/v1/namespaces/{namespace}/pods"
    ]


def test_a_pod_called_secrets_is_still_reachable_by_its_log_tool(api):
    _run(api, "fetch_pod_logs", {"pod_name": "secrets", "namespace": "b4-a"})
    assert any(
        r["raw_path"].startswith("/api/v1/namespaces/b4-a/pods/secrets")
        for r in api.requests
    ), [r["raw_path"] for r in api.requests]


@pytest.mark.parametrize("tool", ["kubernetes_jq_query", "kubernetes_count"])
@pytest.mark.parametrize("kind", ["secrets", "Secret", "SECRETS"])
def test_with_the_switch_on_object_tools_still_never_read_secrets(api, tool, kind):
    outcome = _run(api, tool, {"kind": kind, "jq_expr": ".items[]"})
    assert (outcome.status, outcome.reason) == ("error", REFUSED), _view(outcome)
    assert api.requests == []


def test_tabular_tool_still_never_reads_secrets(api):
    outcome = _run(
        api,
        "kubernetes_tabular_query",
        {"kind": "secrets", "columns": "NAME:.metadata.name"},
    )
    assert (outcome.status, outcome.reason) == ("error", REFUSED), _view(outcome)
    assert api.requests == []


@pytest.mark.parametrize("off", [None, "", "0"])
def test_with_the_switch_off_the_tools_do_not_exist(api, off):
    api.secrets = history("shop", "b4-a", 1)
    worker = Worker(_env(api, **{SWITCH: off}))
    for tool, params in (
        (LIST, {}),
        (HISTORY, {"release_name": "shop", "namespace": "b4-a"}),
        (STATUS, {"release_name": "shop", "namespace": "b4-a"}),
    ):
        outcome = worker.call(tool, params)
        assert outcome.status in ("error", "denied"), _view(outcome)
    assert api.requests == []


# ======================================================================================
# 4. Rows: the twelve fields, order, latest revision, time and rollback
# ======================================================================================


def test_rows_project_exactly_the_twelve_documented_fields(api):
    first = datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)
    last = datetime(2026, 11, 20, 8, 30, 15, tzinfo=timezone.utc)
    api.secrets = [
        secret(
            release(
                "shop",
                "b4-a",
                2,
                chart_version="0.2.0",
                app_version="1.1.0",
                first=first,
                last=last,
                description="Upgrade complete",
            )
        )
    ]
    rows = _rows(_run(api, HISTORY, {"release_name": "shop", "namespace": "b4-a"}))
    assert rows == [
        {
            "name": "shop",
            "namespace": "b4-a",
            "revision": 2,
            "status": "deployed",
            "chart": "b4-fixture",
            "chart_version": "0.2.0",
            "app_version": "1.1.0",
            "first_deployed": "2026-11-01T10:00:00Z",
            "last_deployed": "2026-11-20T08:30:15Z",
            "deleted": None,
            "description": "Upgrade complete",
            "rollback_revision": None,
        }
    ]


@pytest.mark.parametrize("helm4", [False, True], ids=["helm3", "helm4"])
def test_both_helm_generations_decode(api, helm4):
    api.secrets = history("cart", "b4-b", 3, helm4=helm4)
    rows = _rows(_run(api, HISTORY, {"release_name": "cart", "namespace": "b4-b"}))
    assert [r["revision"] for r in rows] == [1, 2, 3]
    assert [r["status"] for r in rows] == ["superseded", "superseded", "deployed"]
    for row in rows:
        assert set(row) == ROW_FIELDS
        assert _RFC3339_Z.fullmatch(row["first_deployed"]), row
        assert _RFC3339_Z.fullmatch(row["last_deployed"]), row


def test_history_is_every_kept_revision_in_ascending_order(api):
    secrets = history("shop", "b4-a", 7)
    random.Random(563).shuffle(secrets)
    api.secrets = secrets + history("cart", "b4-a", 2)
    outcome = _run(api, HISTORY, {"release_name": "shop", "namespace": "b4-a"})
    assert [r["revision"] for r in _rows(outcome)] == list(range(1, 8))
    assert {r["name"] for r in _rows(outcome)} == {"shop"}
    assert _view(outcome)["backend_revisions_returned"] == 7


def test_status_is_one_row_the_highest_revision_whatever_its_status(api):
    api.secrets = [
        secret(release("shop", "b4-a", 3, status="superseded")),
        secret(
            release("shop", "b4-a", 10, status="failed", description="Upgrade failed")
        ),
        secret(release("shop", "b4-a", 9, status="deployed")),
    ]
    outcome = _run(api, STATUS, {"release_name": "shop", "namespace": "b4-a"})
    rows = _rows(outcome)
    assert [(r["revision"], r["status"]) for r in rows] == [(10, "failed")]
    assert _view(outcome)["backend_releases_returned"] == 1


def test_list_is_the_latest_revision_per_release_sorted_by_namespace_then_name(api):
    api.secrets = [
        secret(release("zeta", "b4-a", 2, status="pending-upgrade")),
        secret(release("zeta", "b4-a", 1, status="deployed")),
        secret(release("alpha", "b4-b", 5, status="uninstalling")),
        secret(release("alpha", "b4-a", 1, status="uninstalled", deleted=go_time(T0))),
        secret(release("beta", "b4-a", 4, status="failed")),
        secret(release("beta", "b4-a", 3, status="deployed")),
    ]
    random.Random(1).shuffle(api.secrets)
    outcome = _run(api, LIST)
    rows = _rows(outcome)
    assert [(r["namespace"], r["name"], r["revision"], r["status"]) for r in rows] == [
        ("b4-a", "alpha", 1, "uninstalled"),
        ("b4-a", "beta", 4, "failed"),
        ("b4-a", "zeta", 2, "pending-upgrade"),
        ("b4-b", "alpha", 5, "uninstalling"),
    ]
    assert _view(outcome)["backend_releases_returned"] == 4
    assert rows[0]["deleted"] == _z(T0)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-11-20T01:59:58.123456789-08:00", "2026-11-20T09:59:58Z"),
        ("2026-11-20T09:59:58.999999999Z", "2026-11-20T09:59:58Z"),
        ("2026-11-20T09:59:58Z", "2026-11-20T09:59:58Z"),
        ("2026-11-20T15:29:58.5+05:30", "2026-11-20T09:59:58Z"),
    ],
)
def test_times_are_utc_rfc3339_to_the_second(api, value, expected):
    body = release("shop", "b4-a", 1)
    body["info"]["last_deployed"] = value
    body["info"]["deleted"] = value
    api.secrets = [secret(body)]
    (row,) = _rows(_run(api, STATUS, {"release_name": "shop", "namespace": "b4-a"}))
    assert row["last_deployed"] == expected
    assert row["deleted"] == expected


@pytest.mark.parametrize("zero", ["", "0001-01-01T00:00:00Z", None, "missing"])
def test_zero_or_missing_times_are_null(api, zero):
    body = release("shop", "b4-a", 1)
    for key in ("first_deployed", "deleted"):
        if zero == "missing":
            del body["info"][key]
        else:
            body["info"][key] = zero
    api.secrets = [secret(body)]
    (row,) = _rows(_run(api, STATUS, {"release_name": "shop", "namespace": "b4-a"}))
    assert row["first_deployed"] is None and row["deleted"] is None


def test_a_missing_app_version_is_null(api):
    api.secrets = [secret(release("shop", "b4-a", 1, app_version=None))]
    (row,) = _rows(_run(api, STATUS, {"release_name": "shop", "namespace": "b4-a"}))
    assert row["app_version"] is None


@pytest.mark.parametrize(
    ("field", "description", "expected"),
    [
        (None, "Rollback to 1", 1),
        (None, "Rollback to 12", 12),
        (3, "Rollback to 1", 3),
        (0, "Rollback to 2", 2),
        (-1, "Rollback to 2", 2),
        ("3", "Rollback to 2", 2),
        (None, "Rollback to 2 ", None),
        (None, "rollback to 2", None),
        (None, "Rollback to x", None),
        (None, "Upgrade complete", None),
        (None, "Rollback to 1 failed: timeout", None),
        (0, "Upgrade complete", None),
        (4, "Upgrade complete", 4),
    ],
)
def test_rollback_revision_from_the_field_then_the_exact_description(
    api, field, description, expected
):
    api.secrets = [
        secret(
            release("shop", "b4-a", 5, description=description, rollback_revision=field)
        )
    ]
    (row,) = _rows(_run(api, STATUS, {"release_name": "shop", "namespace": "b4-a"}))
    assert row["rollback_revision"] == expected


def test_description_is_cut_to_2000_characters_and_redacted(api):
    from opspilot.redaction import redact_credentials

    long_text = "x" * 5000
    leaking = 'Upgrade "shop" failed: password=abcdabcd563 rejected'
    api.secrets = [
        secret(release("shop", "b4-a", 1, description=long_text)),
        secret(release("cart", "b4-a", 1, description=leaking)),
    ]
    rows = {r["name"]: r for r in _rows(_run(api, LIST, {"namespace": "b4-a"}))}
    assert rows["shop"]["description"] == "x" * 2000
    assert "abcdabcd563" not in rows["cart"]["description"]
    assert rows["cart"]["description"] == redact_credentials(leaking)


def test_dropped_counts_are_present_and_zero_on_clean_answers(api):
    api.secrets = history("shop", "b4-a", 2)
    for tool, params in (
        (LIST, {}),
        (HISTORY, {"release_name": "shop", "namespace": "b4-a"}),
        (STATUS, {"release_name": "shop", "namespace": "b4-a"}),
    ):
        view = _view(_run(api, tool, params))
        assert view["dropped"] == {"malformed": 0, "too_large": 0}, tool


# -- time: current-state class (B4C4-U1=a) ---------------------------------------------


@pytest.mark.parametrize("tool", HELM_TOOLS)
def test_source_times_are_empty_and_history_is_not_cut_to_the_frame(api, tool):
    long_ago = NOW - timedelta(days=40)
    api.secrets = [
        secret(release("shop", "b4-a", 1, first=long_ago, last=long_ago)),
        secret(
            release("shop", "b4-a", 2, first=long_ago, last=NOW + timedelta(hours=2))
        ),
    ]
    api.secrets[0]["metadata"]["labels"]["status"] = "superseded"
    worker = Worker(_env(api))
    params = {} if tool == LIST else {"release_name": "shop", "namespace": "b4-a"}
    outcome = worker.call(tool, params)
    view = _view(outcome)
    assert outcome.status == "ok", view
    assert view["source_start_at"] is None
    assert view["source_end_at"] is None
    assert view["data_as_of"] is None
    record = outcome.evidence
    assert record.source_start_at is None and record.source_end_at is None
    assert record.data_as_of is None
    if tool == HISTORY:
        # Outside the six-hour frame on both sides, still listed.
        assert [r["revision"] for r in _rows(outcome)] == [1, 2]
    notes = _notes(outcome)
    assert notes and _FIRST_NOTE.fullmatch(notes[0]), notes
    cannot = str(view.get("cannot_prove") or "").lower()
    assert "window" in cannot, cannot  # outside entries listed; no proof of absence
    assert "kubectl" in cannot, cannot  # changes outside Helm are not recorded
    assert "pod" in cannot, cannot  # not Pod rollout time / not Pod health
    assert "deployed" in cannot, cannot


# ======================================================================================
# 5. Decoding: two base64 layers, gzip only on its magic, 8 MiB, bad entries
# ======================================================================================


def test_helm_layer_without_gzip_decodes(api):
    api.secrets = [secret(release("shop", "b4-a", 1), gzipped=False)]
    (row,) = _rows(_run(api, STATUS, {"release_name": "shop", "namespace": "b4-a"}))
    assert row["revision"] == 1


def _good() -> dict[str, Any]:
    return secret(release("good", "b4-junk", 1))


def _bad_body(mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    body = release("bad", "b4-junk", 1)
    body["config"] = {"secret": "CANARY-B4-BADENTRY"}
    mutate(body)
    return body


def _pop(*path: str) -> Callable[[dict[str, Any]], None]:
    def mutate(body: dict[str, Any]) -> None:
        target = body
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]

    return mutate


def _set(value: object, *path: str) -> Callable[[dict[str, Any]], None]:
    def mutate(body: dict[str, Any]) -> None:
        target = body
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


_BAD_JSON = b'{"name":"bad","version":1,"CANARY-B4-BADENTRY":'
MALFORMED: dict[str, dict[str, Any]] = {
    "k8s-base64-invalid": secret(None, name="bad", release_data="!!CANARY-B4-BADENTRY!!"),
    "k8s-base64-unpadded": secret(None, name="bad", release_data=encode(release("bad", "b4-junk", 1))[:-1]),
    "helm-base64-invalid": secret(None, name="bad", release_data=_b64(b"!!not base64!!")),
    "helm-base64-newline": secret(
        None, name="bad",
        release_data=_b64(_b64(gzip.compress(_json(release("bad", "b4-junk", 1)))).encode()[:8] + b"\n"
                          + _b64(gzip.compress(_json(release("bad", "b4-junk", 1)))).encode()[8:]),
    ),
    "invalid-json": secret(None, name="bad", release_data=encode(_BAD_JSON)),
    "invalid-json-plain": secret(None, name="bad", release_data=encode(_BAD_JSON, gzipped=False)),
    "json-array": secret(None, name="bad", release_data=encode(b"[1,2]")),
    "json-string": secret(None, name="bad", release_data=encode(b'"CANARY-B4-BADENTRY"')),
    "not-utf8": secret(None, name="bad", release_data=encode(b'{"name":"\xff\xfe"}')),
    "gzip-magic-other-method": secret(
        None, name="bad", release_data=_b64(_b64(b"\x1f\x8b\x09" + gzip.compress(b"{}")[3:]).encode())
    ),
    "gzip-corrupt": secret(
        None, name="bad", release_data=_b64(_b64(gzip.compress(_json(release("bad", "b4-junk", 1)))[:30]).encode())
    ),
    "missing-release-key": secret(None, name="bad", data={"other": _b64(b"CANARY-B4-BADENTRY")}),
    "no-data": {**secret(None, name="bad"), "data": None},
    "missing-name": secret(_bad_body(_pop("name")), name="bad", namespace="b4-junk"),
    "version-string": secret(_bad_body(_set("1", "version")), name="bad", namespace="b4-junk"),
    "version-float": secret(_bad_body(_set(1.5, "version")), name="bad", namespace="b4-junk"),
    "version-missing": secret(_bad_body(_pop("version")), name="bad", version=1, namespace="b4-junk"),
    "status-upper": secret(_bad_body(_set("DEPLOYED", "info", "status"))),
    "status-punctuation": secret(_bad_body(_set("deployed!", "info", "status"))),
    "status-33-chars": secret(_bad_body(_set("a" * 33, "info", "status"))),
    "status-empty": secret(_bad_body(_set("", "info", "status"))),
    "status-missing": secret(_bad_body(_pop("info", "status"))),
    "time-unparseable": secret(_bad_body(_set("yesterday", "info", "last_deployed"))),
    "time-number": secret(_bad_body(_set(1763629200, "info", "first_deployed"))),
    "name-label-mismatch": secret(
        _bad_body(lambda b: None),
        labels={"name": "other", "owner": "helm", "version": "1", "status": "deployed"},
    ),
    "version-label-mismatch": secret(
        _bad_body(lambda b: None),
        labels={"name": "bad", "owner": "helm", "version": "2", "status": "deployed"},
    ),
    "namespace-mismatch": secret(_bad_body(_set("kube-system", "namespace")), namespace="b4-junk"),
}  # fmt: skip


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_a_bad_entry_is_dropped_counted_and_never_echoed(api, case, caplog):
    caplog.set_level(logging.DEBUG)
    bad = json.loads(json.dumps(MALFORMED[case]))
    bad["metadata"]["namespace"] = "b4-junk"
    api.secrets = [_good(), bad]
    worker = Worker(_env(api))
    outcome = worker.call(LIST, {"namespace": "b4-junk"})
    assert [r["name"] for r in _rows(outcome)] == ["good"]
    view = _view(outcome)
    assert view["dropped"] == {"malformed": 1, "too_large": 0}, view
    assert any("1" in n and "malformed" in n.lower() for n in _notes(outcome)), _notes(
        outcome
    )
    blob = (bad.get("data") or {}).get("release") or ""
    outputs = (
        _text(outcome),
        outcome.evidence.raw.decode("utf-8", "replace"),
        repr(worker.sink.records),
        caplog.text,
        repr([r.args for r in caplog.records]),
    )
    for text in outputs:
        assert "CANARY-B4-BADENTRY" not in text
        if blob:
            assert blob not in text


#: Records without ``owner=helm``: the request's ``labelSelector=owner=helm``
#: keeps the API server from returning them at all (the real one filters the
#: same way), so they are neither rows nor counted as malformed. The tool's
#: own owner-label check is a second guard the public path cannot reach.
NOT_HELM_OWNED = {
    "owner-label-not-helm": {
        "name": "bad",
        "owner": "tiller",
        "version": "1",
        "status": "deployed",
    },
    "owner-label-missing": {"name": "bad", "version": "1", "status": "deployed"},
}


@pytest.mark.parametrize("case", sorted(NOT_HELM_OWNED))
def test_records_not_owned_by_helm_are_never_returned_or_counted(api, case):
    item = secret(release("bad", "b4-junk", 1), labels=NOT_HELM_OWNED[case])
    api.secrets = [_good(), item]
    outcome = _run(api, LIST, {"namespace": "b4-junk"})
    assert [r["name"] for r in _rows(outcome)] == ["good"]
    assert _view(outcome)["dropped"] == {"malformed": 0, "too_large": 0}
    assert all(r["query"]["labelSelector"] == ["owner=helm"] for r in api.requests)


def test_an_entry_that_inflates_past_8_mib_is_too_large_and_one_just_under_decodes(api):
    under = release("under", "b4-junk", 1)
    under["config"] = {"pad": "u" * (8 * MIB - 64 * 1024)}
    over = release("over", "b4-junk", 1)
    over["config"] = {"pad": "o" * (8 * MIB + 64 * 1024)}
    zeros = secret(None, name="zeros", namespace="b4-junk",
                   release_data=_b64(_b64(gzip.compress(b"\0" * (32 * MIB))).encode()))  # fmt: skip
    api.secrets = [secret(under), secret(over), zeros]
    outcome = _run(api, LIST, {"namespace": "b4-junk"})
    assert [r["name"] for r in _rows(outcome)] == ["under"]
    assert _view(outcome)["dropped"] == {"malformed": 0, "too_large": 2}
    assert any(
        "too_large" in n or "8 MiB" in n or "large" in n.lower()
        for n in _notes(outcome)
    )


def test_only_dropped_entries_is_no_data(api):
    api.secrets = [
        {**MALFORMED["k8s-base64-invalid"], "metadata": {**MALFORMED["k8s-base64-invalid"]["metadata"], "namespace": "b4-junk"}}
    ]  # fmt: skip
    outcome = _run(api, LIST, {"namespace": "b4-junk"})
    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA"), _view(outcome)


MALFORMED_PAGES = {
    "not-json": b"<html>proxy</html>",
    "json-array": b"[]",
    "json-null": b"null",
    "no-items": _json({"kind": "SecretList", "apiVersion": "v1", "metadata": {}}),
    "items-object": _json({"kind": "SecretList", "items": {}}),
    "items-string": _json({"kind": "SecretList", "items": "CANARY-B4-PAGE"}),
}


@pytest.mark.parametrize("case", sorted(MALFORMED_PAGES))
def test_a_page_that_is_not_a_secret_list_fails_the_whole_call(api, case):
    api.secrets = history("shop", "b4-a", 1)
    api.raw_at[0] = MALFORMED_PAGES[case]
    outcome = _run(api, LIST)
    _failed(outcome, "MALFORMED_RESULT")
    assert "CANARY-B4-PAGE" not in _text(outcome)
    assert "proxy" not in _text(outcome)


# ======================================================================================
# 6. Nothing but the twelve fields ever leaves the tool
# ======================================================================================

CANARIES = {
    "config": "CANARY-B4-VALUES",
    "set": "CANARY-B4-SET",
    "chart-values": "CANARY-B4-CHARTVALUES",
    "template": "CANARY-B4-TEMPLATE",
    "file": "CANARY-B4-FILE",
    "schema": "CANARY-B4-SCHEMA",
    "chart-metadata": "CANARY-B4-CHARTMETA",
    "chart-lock": "CANARY-B4-LOCK",
    "chart-dependency": "CANARY-B4-DEPENDENCY",
    "manifest": "CANARY-B4-MANIFEST",
    "hook": "CANARY-B4-HOOK",
    "hook-name": "CANARY-B4-HOOKNAME",
    "notes": "CANARY-B4-NOTES",
    "resources": "CANARY-B4-RESOURCES",
    "release-label": "CANARY-B4-LABEL",
    "apply-method": "CANARY-B4-APPLY",
    "unknown-field": "CANARY-B4-EXTRA",
    "unknown-info-field": "CANARY-B4-INFOEXTRA",
    "annotation": "CANARY-B4-ANNOTATION",
    "secret-label": "CANARY-B4-SECRETLABEL",
    "data-key": "CANARY-B4-DATAKEY",
    "bad-entry": "CANARY-B4-BADENTRY",
}


def _canary_release(name: str, version: int, *, description: str = "Upgrade complete"):
    body = release(name, "b4-a", version, helm4=True, description=description,
                   status="deployed" if version == 2 else "superseded")  # fmt: skip
    c = CANARIES
    body["config"] = {"image": {"tag": c["config"]}, "fromSet": c["set"]}
    chart = body["chart"]
    chart["values"] = {"default": c["chart-values"]}
    chart["templates"] = [
        {"name": "templates/cm.yaml", "data": _b64(f"# {c['template']}\n".encode())}
    ]
    chart["files"] = [{"name": "files/readme.txt", "data": _b64(c["file"].encode())}]
    chart["schema"] = _b64(json.dumps({"title": c["schema"]}).encode())
    chart["lock"] = {"digest": c["chart-lock"], "dependencies": []}
    chart["metadata"]["description"] = c["chart-metadata"]
    chart["metadata"]["annotations"] = {"note": c["chart-metadata"]}
    chart["metadata"]["dependencies"] = [
        {"name": c["chart-dependency"], "version": "1.0.0"}
    ]
    body["manifest"] = f"---\nkind: ConfigMap\ndata:\n  k: {c['manifest']}\n"
    body["hooks"] = [
        {
            "name": c["hook-name"],
            "kind": "ConfigMap",
            "path": "templates/hook.yaml",
            "manifest": f"data:\n  k: {c['hook']}\n",
            "events": ["pre-install"],
            "last_run": {"started_at": "", "completed_at": "", "phase": ""},
        }
    ]
    body["info"]["notes"] = c["notes"]
    body["info"]["resources"] = {"v1/ConfigMap": [{"data": {"k": c["resources"]}}]}
    body["info"]["mystery"] = c["unknown-info-field"]
    body["labels"] = {"team": c["release-label"]}
    body["apply_method"] = c["apply-method"]
    body["extra"] = c["unknown-field"]
    return secret(
        body,
        extra_labels={"team": c["secret-label"]},
        annotations={
            "kubectl.kubernetes.io/last-applied-configuration": json.dumps(
                {"metadata": {"annotations": {"x": c["annotation"]}}}
            ),
            "canary": c["annotation"],
        },
    )


def _canary_secrets() -> list[dict[str, Any]]:
    items = [_canary_release("shop", 1), _canary_release("shop", 2)]
    for item in items:
        item["data"]["extra"] = _b64(CANARIES["data-key"].encode())
    bad = release("shop", "b4-a", 3, status="DEPLOYED")  # malformed: dropped
    bad["config"] = {"secret": CANARIES["bad-entry"]}
    items.append(secret(bad))
    return items


def _forms(value: str) -> list[str]:
    raw = value.encode()
    return [value, _b64(raw), _b64(f"# {value}\n".encode()), _b64(raw).rstrip("=")]


def _collect_spans(run: Callable[[], list[Any]], api: FakeApiServer):
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from opspilot import tracing
    from tests.test_tracing import lab_env

    exporter = InMemorySpanExporter()
    tracer = tracing.configure(
        lab_env(
            OPSPILOT_TOOL_PROFILE=EXT_PROFILE,
            OPSPILOT_K8S_API_URL=api.url,
            OPSPILOT_OTEL_PROMETHEUS_URL="http://127.0.0.1:19090",
            OPSPILOT_OTEL_JAEGER_URL="http://127.0.0.1:16686",
            **{SWITCH: "1"},
        ),
        exporter=exporter,
    )
    try:
        with tracer.run(incident_id="b4-incident", run_id=RUN_ID, versions={}):
            results = run()
        tracer.shutdown()
        spans = exporter.get_finished_spans()
    finally:
        tracing.reset()
    exported = json.dumps(
        [
            {
                "name": span.name,
                "attributes": dict(span.attributes or {}),
                "events": [
                    {"name": e.name, "attributes": dict(e.attributes or {})}
                    for e in span.events
                ],
            }
            for span in spans
        ],
        default=str,
    )
    return results, spans, exported


def test_no_withheld_field_reaches_any_output(api, caplog):
    caplog.set_level(logging.DEBUG)
    api.secrets = _canary_secrets()
    worker = Worker(_env(api))

    def run():
        return [
            worker.call(LIST, {}),
            worker.call(LIST, {"namespace": "b4-a"}),
            worker.call(HISTORY, {"release_name": "shop", "namespace": "b4-a"}),
            worker.call(STATUS, {"release_name": "shop", "namespace": "b4-a"}),
        ]

    outcomes, spans, exported = _collect_spans(run, api)
    assert any(HISTORY in span.name or HISTORY in json.dumps(dict(span.attributes or {}), default=str)
               for span in spans), [s.name for s in spans]  # fmt: skip
    for outcome in outcomes:
        assert outcome.status == "ok", _view(outcome)
        for row in _rows(outcome):
            assert set(row) == ROW_FIELDS
    assert [r["revision"] for r in _rows(outcomes[2])] == [1, 2]
    assert _view(outcomes[2])["dropped"] == {"malformed": 1, "too_large": 0}
    blobs = [s["data"]["release"] for s in api.secrets]
    outputs = {
        "views": json.dumps([_view(o) for o in outcomes], default=str),
        "evidence-raw": "\n".join(
            o.evidence.raw.decode("utf-8", "replace") for o in outcomes
        ),
        "evidence-view": json.dumps(
            [dict(o.evidence.view) for o in outcomes], default=str
        ),
        "sink": repr(worker.sink.records),
        "ledger": repr(worker.store.charges),
        "spans": exported,
        "logs": caplog.text + repr([r.args for r in caplog.records]),
    }
    leaks = [
        (where, place)
        for where, text in outputs.items()
        for place, value in CANARIES.items()
        if any(form in text for form in _forms(value))
    ]
    assert leaks == []
    for where, text in outputs.items():
        for blob in blobs:
            assert blob not in text, where
        assert "last-applied-configuration" not in text, where
        assert "managedFields" not in text, where


def test_an_oversized_view_saves_no_withheld_field_either(api, caplog):
    # Forty releases with long dense descriptions: the projection is small,
    # the view is over 25k tokens, so the executor keeps the body as raw.
    caplog.set_level(logging.DEBUG)
    secrets = []
    for n in range(40):
        dense = hashlib.sha256(str(n).encode()).hexdigest() * 32  # 2048 chars
        item = _canary_release(f"rel-{n:02d}", 2, description=dense[:2000])
        secrets.append(item)
    api.secrets = secrets
    worker = Worker(_env(api))
    outcome = worker.call(LIST, {"namespace": "b4-a"})
    _failed(outcome, "RESULT_TOO_LARGE")
    text = _text(outcome)
    assert "namespace" in text or "release_name" in text
    outputs = [
        text,
        repr(worker.sink.records),
        repr([getattr(r, "raw", b"") for r in worker.sink.records]),
        caplog.text,
    ]
    for output in outputs:
        for value in CANARIES.values():
            assert all(form not in output for form in _forms(value)), value


# ======================================================================================
# 7. Size, time and error classes
# ======================================================================================


def _padded(name: str, version: int, size: int) -> dict[str, Any]:
    item = secret(release(name, "b4-a", version, status="superseded"))
    item["data"]["pad"] = "A" * size  # another data key: read, never decoded
    return item


def test_raw_reads_over_16_mib_fail_the_whole_call(api):
    api.secrets = [_padded("big", v, MIB) for v in range(1, 15)]  # ~14 MiB
    ok = _run(api, STATUS, {"release_name": "big", "namespace": "b4-a"})
    assert [r["revision"] for r in _rows(ok)] == [14]
    api.requests.clear()
    api.secrets = [_padded("big", v, MIB) for v in range(1, 18)]  # ~17 MiB
    outcome = _run(api, STATUS, {"release_name": "big", "namespace": "b4-a"})
    _failed(outcome, "RESULT_TOO_LARGE")
    text = _text(outcome)
    assert "namespace" in text or "release_name" in text
    assert len(api.requests) <= 4  # stops once the budget is spent


def test_at_most_200_pages_are_read(api):
    api.secrets = [
        secret(release("many", "b4-a", v, status="superseded")) for v in range(1, 1001)
    ]
    ok = _run(api, STATUS, {"release_name": "many", "namespace": "b4-a"})
    assert [r["revision"] for r in _rows(ok)] == [1000]
    assert len(api.requests) == 200
    api.requests.clear()
    api.secrets.append(secret(release("many", "b4-a", 1001)))
    outcome = _run(api, STATUS, {"release_name": "many", "namespace": "b4-a"})
    assert outcome.status == "error", _view(outcome)
    assert outcome.reason == "RESULT_TOO_LARGE", _view(outcome)
    assert len(api.requests) <= 201


def test_a_read_past_the_call_deadline_is_a_timeout(api):
    api.secrets = history("shop", "b4-a", 12)
    api.delay = 4.0
    worker = Worker(_env(api), deadline_seconds=2)
    started = time.monotonic()
    outcome = worker.call(HISTORY, {"release_name": "shop", "namespace": "b4-a"})
    assert (outcome.status, outcome.reason) == ("timeout", "TOOL_TIMEOUT"), _view(
        outcome
    )
    assert time.monotonic() - started < 3.9


def test_registrations_are_bounded_reads_that_may_contain_secrets():
    from opspilot.tools import otel_demo_ext

    # ASSUMPTION: the private registry seam takes ``helm`` as a keyword.
    regs = {r.name: r for r in otel_demo_ext._registrations(helm=True)}
    assert set(HELM_TOOLS) <= set(regs)
    assert not set(HELM_TOOLS) & {r.name for r in otel_demo_ext._registrations()}
    for name in HELM_TOOLS:
        reg = regs[name]
        assert reg.read_only is True, name
        assert reg.verb == "list", name
        assert reg.source == "otel-demo", name
        assert reg.result_path == ("results",), name
        assert reg.may_contain_secrets is True, name
        assert reg.max_result_bytes == 2 * MIB, name
        assert reg.max_view_tokens == 25_000, name
        assert 0 < reg.request_timeout_seconds <= 30, name


def _fail(api: FakeApiServer, index: int, status: int, reason: str) -> None:
    api.fail_at[index] = (status, status_body(status, reason, f"synthetic {reason}"))


STATUS_CASES = [
    (400, "BadRequest", "INVALID_PARAMS"),
    (404, "NotFound", "INVALID_PARAMS"),
    (422, "Invalid", "INVALID_PARAMS"),
    (401, "Unauthorized", "SOURCE_FORBIDDEN"),
    (403, "Forbidden", "SOURCE_FORBIDDEN"),
    (410, "Expired", "SOURCE_ERROR"),
    (429, "TooManyRequests", "SOURCE_UNAVAILABLE"),
    (500, "InternalError", "SOURCE_UNAVAILABLE"),
    (503, "ServiceUnavailable", "SOURCE_UNAVAILABLE"),
]


@pytest.mark.parametrize(("status", "k8s_reason", "reason"), STATUS_CASES)
@pytest.mark.parametrize("tool", HELM_TOOLS)
def test_statuses_map_to_the_helm_error_table_with_one_request(
    api, tool, status, k8s_reason, reason
):
    _fail(api, 0, status, k8s_reason)
    params = {} if tool == LIST else {"release_name": "shop", "namespace": "b4-a"}
    outcome = _run(api, tool, params)
    _failed(outcome, reason)
    assert len(api.requests) == 1  # no retry
    if status in (401, 403):
        assert FORBIDDEN_NOTE in _text(outcome), _view(outcome)


def test_an_expired_continue_fails_the_whole_call_without_partial_rows(api):
    api.secrets = history("shop", "b4-a", 12)
    _fail(api, 1, 410, "Expired")
    outcome = _run(api, HISTORY, {"release_name": "shop", "namespace": "b4-a"})
    _failed(outcome, "SOURCE_ERROR")
    assert len(api.requests) == 2


def test_an_unreachable_api_server_is_source_unavailable(api):
    env = _env(api)
    api.stop()
    _failed(Worker(env).call(LIST), "SOURCE_UNAVAILABLE")


def test_an_untrusted_certificate_reads_nothing(api):
    api.secrets = history("shop", "b4-a", 1)
    env = _env(api, OPSPILOT_K8S_CA_FILE=os.environ["B4_TEST_PLACEHOLDER_CA"])
    outcome = Worker(env).call(LIST)
    assert outcome.status == "error", _view(outcome)
    assert api.requests == []


@pytest.mark.parametrize(
    ("tool", "params"),
    [
        (HISTORY, {"release_name": "nope", "namespace": "b4-a"}),
        (STATUS, {"release_name": "nope", "namespace": "b4-a"}),
        (HISTORY, {"release_name": "shop", "namespace": "b4-none"}),
        (STATUS, {"release_name": "shop", "namespace": "b4-none"}),
    ],
)
def test_a_missing_release_or_namespace_is_no_data_naming_what_was_asked(
    api, tool, params
):
    api.secrets = history("shop", "b4-a", 2)
    outcome = _run(api, tool, params)
    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA"), _view(outcome)
    notes = " ".join(_notes(outcome))
    assert params["release_name"] in notes and params["namespace"] in notes, notes


def test_an_empty_namespace_list_is_no_data(api):
    api.secrets = history("shop", "b4-a", 2)
    outcome = _run(api, LIST, {"namespace": "b4-none"})
    assert (outcome.status, outcome.reason) == ("no_data", "NO_DATA"), _view(outcome)


# ======================================================================================
# 8. The switch, the lab gate, revisions and the source list
# ======================================================================================


def _face(env: Mapping[str, str]):
    return select_profile(env).face(FakeClock(start=NOW))


def _tool_names(env: Mapping[str, str]) -> list[str]:
    return [s["function"]["name"] for s in _face(env).tool_schemas]


def _surface(env: Mapping[str, str]) -> dict[str, Any]:
    profile = select_profile(env)
    face = profile.face(FakeClock(start=NOW))
    return {
        "tool_schemas": [dict(item) for item in face.tool_schemas],
        "variant_id": face.variant_id,
        "evidence_context": json.loads(canonical(dict(face.evidence_context(RUN_ID)))),
        "versions": profile.versions(),
    }


FLAGS = {
    "es": ("OPSPILOT_ES_URL", "http://127.0.0.1:9200"),
    "loki": ("OPSPILOT_LOKI_URL", "http://127.0.0.1:3100"),
    "vlogs": ("OPSPILOT_VICTORIALOGS_URL", "http://127.0.0.1:9428"),
    "live": ("OPSPILOT_K8S_LIVE_METRICS", "1"),
    "knowledge": ("OPSPILOT_KNOWLEDGE_TOOL", "1"),
    "todo": ("OPSPILOT_TODO_WRITE", "1"),
}
COMBOS = [
    (),
    ("es",),
    ("loki", "vlogs"),
    ("live",),
    ("knowledge",),
    ("todo",),
    ("knowledge", "todo"),
    ("es", "loki", "vlogs", "live", "knowledge", "todo"),
]
#: Tools that stay after Helm's in the array (B4-2).
TAIL_TOOLS = ("fetch_skill", "TodoWrite")


def _combo_env(combo: tuple[str, ...], switch: str | None) -> dict[str, str]:
    env = {PROFILE_ENV: EXT_PROFILE}
    for flag in combo:
        name, value = FLAGS[flag]
        env[name] = value
    if switch is not None:
        env[SWITCH] = switch
    return env


@pytest.mark.parametrize("combo", COMBOS, ids=lambda c: "+".join(c) or "none")
def test_switch_off_spellings_leave_every_surface_byte_identical(combo):
    reference = canonical(_surface(_combo_env(combo, None)))
    for off in ("", "0"):
        assert canonical(_surface(_combo_env(combo, off))) == reference, off
    assert not set(HELM_TOOLS) & set(_tool_names(_combo_env(combo, None)))


@pytest.mark.parametrize("combo", COMBOS, ids=lambda c: "+".join(c) or "none")
def test_switch_on_adds_exactly_the_three_tools_and_the_change_history_source(combo):
    off_env, on_env = _combo_env(combo, None), _combo_env(combo, "1")
    off, on = _surface(off_env), _surface(on_env)
    off_names = [s["function"]["name"] for s in off["tool_schemas"]]
    on_names = [s["function"]["name"] for s in on["tool_schemas"]]
    tail = [n for n in off_names if n in TAIL_TOOLS]
    head = [n for n in off_names if n not in TAIL_TOOLS]
    assert off_names == head + tail  # the tail is last today
    assert on_names == head + list(HELM_TOOLS) + tail
    # Every other tool schema is byte for byte the same.
    kept = [s for s in on["tool_schemas"] if s["function"]["name"] not in HELM_TOOLS]
    assert canonical(kept) == canonical(off["tool_schemas"])
    assert on["variant_id"] == off["variant_id"]
    # Versions: only the tool schema revision moves.
    assert set(on["versions"]) == set(off["versions"])
    for key, value in off["versions"].items():
        if key == "tool_schema_revision":
            assert on["versions"][key] != value
        else:
            assert on["versions"][key] == value, key
    # data_sources: change_history moves from not integrated to available.
    # The surface is canonical JSON (keys sorted), so the source keys are
    # compared as a set; their insertion order is not observable here.
    off_sources = off["evidence_context"]["data_sources"]
    on_sources = on["evidence_context"]["data_sources"]
    off_keys = list(off_sources["available"])
    assert "change_history" not in off_keys
    assert set(on_sources["available"]) == set(off_keys) | {"change_history"}
    assert on_sources["available"]["change_history"] == [HISTORY, LIST, STATUS]
    for key in off_keys:
        assert on_sources["available"][key] == off_sources["available"][key], key
    assert "change_history" in off_sources["not_integrated"]
    assert on_sources["not_integrated"] == [
        n for n in off_sources["not_integrated"] if n != "change_history"
    ]
    assert on_sources["instruction"] == off_sources["instruction"]
    rest_on = {k: v for k, v in on["evidence_context"].items() if k != "data_sources"}
    rest_off = {k: v for k, v in off["evidence_context"].items() if k != "data_sources"}
    assert rest_on == rest_off


def test_each_backend_set_has_its_own_helm_revision():
    from opspilot.tools import otel_demo_ext

    revisions = set()
    for combo in COMBOS:
        for switch in (None, "1"):
            revisions.add(
                select_profile(_combo_env(combo, switch)).versions()[
                    "tool_schema_revision"
                ]
            )
    assert len(revisions) == 2 * len(COMBOS)
    on = select_profile({PROFILE_ENV: EXT_PROFILE, SWITCH: "1"}).versions()
    # ASSUMPTION: ``helm`` is a keyword of tool_schema_revision.
    assert (
        otel_demo_ext.HELM_TOOL_SCHEMA_REVISION
        == on["tool_schema_revision"]
        == otel_demo_ext.tool_schema_revision(helm=True)
    )
    assert (
        otel_demo_ext.tool_schema_revision(helm=False)
        == otel_demo_ext.TOOL_SCHEMA_REVISION
    )
    assert set(HELM_TOOLS) <= set(otel_demo_ext.HELM_TOOL_NAMES)


@pytest.mark.parametrize("value", ["1", "garbage", "true"])
def test_the_switch_is_read_only_in_the_extension_profile(value):
    for profile in ("otel-demo", None):
        base_env = {PROFILE_ENV: profile} if profile else {}
        reference = canonical(_surface(base_env))
        assert canonical(_surface({**base_env, SWITCH: value})) == reference


INVALID_SWITCH = [
    "true",
    "yes",
    "on",
    "2",
    "01",
    " 1",
    "1 ",
    "TRUE",
    "-1",
    "1.0",
    "off",
]


def _web_start(env: Mapping[str, str]) -> None:
    profile = select_profile(env)
    profile.versions()
    profile.face(FakeClock(start=NOW)).tool_schemas  # noqa: B018


def _worker_start(env: Mapping[str, str]) -> None:
    profile = select_profile(env)
    clock = FakeClock(start=NOW)
    lease = _lease()
    deadline = NOW + timedelta(minutes=10)
    factory = profile.executor_factory(
        FakeStore(lease, deadline=deadline), RecordingSink(), clock
    )
    factory(lease, _input(profile, lease, clock, deadline))


@pytest.mark.parametrize("value", INVALID_SWITCH)
def test_an_invalid_switch_value_fails_the_web_start(value):
    # ASSUMPTION: the same SystemExit the other startup codes end with.
    with pytest.raises(SystemExit, match="HELM_RELEASES_INVALID"):
        _web_start({PROFILE_ENV: EXT_PROFILE, SWITCH: value})


@pytest.mark.parametrize("value", ["true", "2", " 1"])
def test_an_invalid_switch_value_fails_the_worker_start(value):
    _require_tools()
    with pytest.raises(SystemExit, match="HELM_RELEASES_INVALID"):
        _worker_start(_env(None, **{SWITCH: value}))


NOT_LAB = [
    "https://k8s.example.invalid:6443",
    "https://10.0.0.1:6443",
    "https://127.0.0.1.nip.io:6443",
    "https://kubernetes.default.svc.example.com",
]
LAB = [
    "https://127.0.0.1:16443",
    "https://localhost:6443",
    "https://[::1]:6443",
    "https://kubernetes.default.svc",
]


@pytest.mark.parametrize("trace", [None, "off", "lab"])
@pytest.mark.parametrize("url", NOT_LAB)
def test_a_non_lab_cluster_with_the_switch_on_stops_the_worker(url, trace):
    _require_tools()
    env = _env(None, OPSPILOT_K8S_API_URL=url, OPSPILOT_TRACE=trace)
    with pytest.raises(SystemExit, match="HELM_NOT_LAB"):
        _worker_start(env)


@pytest.mark.parametrize("url", NOT_LAB[:1])
def test_the_lab_gate_is_only_for_the_switch_and_only_in_the_worker(url):
    _require_tools()
    _worker_start(_env(None, OPSPILOT_K8S_API_URL=url, **{SWITCH: None}))
    # The web never reads the cluster address.
    _web_start(_env(None, OPSPILOT_K8S_API_URL=url))


@pytest.mark.parametrize("url", LAB)
def test_lab_cluster_addresses_start_the_worker(url):
    _require_tools()
    _worker_start(_env(None, OPSPILOT_K8S_API_URL=url))


def test_web_on_and_worker_off_record_different_versions():
    on = select_profile({PROFILE_ENV: EXT_PROFILE, SWITCH: "1"}).versions()
    off = select_profile({PROFILE_ENV: EXT_PROFILE}).versions()
    assert on != off
    assert select_profile(_env(None)).versions() == on


def test_entrypoint_passes_the_switch_to_the_worker(tmp_path):
    entrypoint = REPO / "deploy" / "docker-entrypoint.sh"
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


# -- identity layer and CI fixtures (B4-4, R8-25) ---------------------------------------

#: Pinned on origin/main 882bd49c: the base manifest does not change (B4-4).
BASE_RBAC_SHA256 = "ddd4299d538ecfeb48ed272bd2f7d6306c54778f8c188ae09366a4b4370fdea0"


def test_the_base_identity_is_unchanged_and_the_helm_overlay_is_list_only():
    import yaml

    base = REPO / "scripts" / "kind_lab" / "opspilot-readonly-rbac.yaml"
    assert hashlib.sha256(base.read_bytes()).hexdigest() == BASE_RBAC_SHA256
    overlay = REPO / "scripts" / "kind_lab" / "opspilot-readonly-rbac-helm.yaml"
    documents = [d for d in yaml.safe_load_all(overlay.read_text()) if d]
    kinds = {d["kind"]: d for d in documents}
    assert set(kinds) == {"ClusterRole", "ClusterRoleBinding"}
    role, binding = kinds["ClusterRole"], kinds["ClusterRoleBinding"]
    assert role["metadata"]["name"] == "opspilot-investigator-helm-releases"
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["secrets"], "verbs": ["list"]}
    ]
    assert binding["metadata"]["name"] == "opspilot-investigator-helm-releases"
    assert binding["roleRef"]["kind"] == "ClusterRole"
    assert binding["roleRef"]["name"] == "opspilot-investigator-helm-releases"
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": "opspilot-investigator",
            "namespace": "opspilot-readonly",
        }
    ]


def test_the_audit_policy_never_records_request_or_response_bodies():
    import yaml

    policy = yaml.safe_load(
        (REPO / "tests" / "fixtures" / "b4_helm" / "audit-policy.yaml").read_text()
    )
    levels = {rule["level"] for rule in policy["rules"]}
    assert levels <= {"None", "Metadata"}
    secret_rules = [
        rule
        for rule in policy["rules"]
        if any(
            "secrets" in (r.get("resources") or []) for r in rule.get("resources") or []
        )
    ]
    assert secret_rules and all(rule["level"] == "Metadata" for rule in secret_rules)


def test_the_canary_list_names_every_planted_canary():
    path = REPO / "tests" / "fixtures" / "b4_helm" / "canaries.txt"
    listed = {line.strip() for line in path.read_text().splitlines() if line.strip()}
    assert {
        "CANARY-B4-VALUES",
        "CANARY-B4-SET",
        "CANARY-B4-MANIFEST",
        "CANARY-B4-HOOK",
        "CANARY-B4-NOTES",
        "CANARY-B4-TEMPLATE",
        "CANARY-B4-FILE",
        "CANARY-B4-LABEL",
        "CANARY-B4-ANNOTATION",
        "CANARY-B4-OPAQUE",
    } <= listed


def test_the_authz_probe_has_the_helm_overlay_mode():
    probe = REPO / "scripts" / "k8s_authz_probe.py"
    result = subprocess.run(
        [sys.executable, str(probe), "--help"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
    )
    assert result.returncode == 0, result.stderr
    assert "--expect-helm-overlay" in result.stdout
