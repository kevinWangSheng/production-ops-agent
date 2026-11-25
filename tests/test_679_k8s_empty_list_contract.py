"""Independent contract tests for #679: an empty K8s list that was read in
full is an ``ok`` answer ("no objects matching these conditions at query
time"), not ``no_data``.

Written from ruling B4-U5 = a (docs/tasks/2026-11-22-next-batches.md) and the
B4 pre-review rows U5, B4-13, B4-14 and 必答类别 rows 1/3
(docs/tasks/2026-11-22-next-batches-b4-prereview.md), without reading an
implementation. Exercised through the public path: the ``otel-demo-ext``
worker's executor against the loopback HTTPS fake API server of the B4
contract tests (whose default answer to any other list path is a 200 with
zero items and no ``continue``).

Rules covered:

* jq / count / tabular: API 200, pagination read to the end, zero objects,
  kind is not events -> ``ok`` with ``total_items_processed: 0`` and the
  queried kind, namespace and selectors; count says ``count: 0``.
* A jq expression that filters a non-empty list to nothing stays ``no_data``;
  events with zero items stay ``no_data``; an interrupted pagination or a
  failed page is never ``ok``.
* Other tools' empty results stay ``no_data`` (the shared executor path).
* The report rule is unchanged and an ``ok`` empty list can support a fact
  such as "no NetworkPolicy in namespace X"; the base profile's prompt and
  tool revisions do not move.

Expectations chosen where the contract leaves a shape open (each also marked
``# CHOSEN:``):

* The queried selectors appear in the view's ``scope`` as ``label_selector``
  and ``field_selector`` for all three tools, as ``kubernetes_tabular_query``
  already does (the contract says the result carries them).
* An ok empty list has ``content == []`` (no placeholder row).
* The description wording is matched loosely: "no objects matching ... at
  query time".
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from opspilot.investigation.context import delivered_view
from opspilot.investigation.reports import (
    REPORT_CONTRACT,
    ReportV2,
    unsupported_citations,
)
from opspilot.tools import TransportResponse
from opspilot.tools.profiles import PROFILE_ENV, select_profile
from tests.m1_tool_support import FakeClock, body, build, request
from tests.test_696_managed_fields_summary_contract import ConfigMapApi, configmap
from tests.test_m1_otel_demo_contract import NOW, TARGET_ID
from tests.test_tool_expansion_b4_contract import (  # noqa: F401 -- fixtures
    LIST,
    SWITCH,
    Worker,
    _clean_environment,
    _env,
    _json,
    _view,
    history,
    status_body,
    tls_pair,
)

JQ = "kubernetes_jq_query"
COUNT = "kubernetes_count"
TABULAR = "kubernetes_tabular_query"
LIST_TOOLS = (JQ, COUNT, TABULAR)
NS = "shop"
RUN_ID = "b4-run"
METRICS = "/apis/metrics.k8s.io/v1beta1/"
NP_PATH = re.compile(
    r"/apis/networking\.k8s\.io/v1/(?:namespaces/([^/]+)/)?networkpolicies"
)

# Pinned on origin/main 067d2c18 with the minimal base env below; #679 must
# not move them (B4-1, U5 a: the report rule sentence is unchanged).
BASE_PROMPT_REVISION = "prompt-replay-candidate-f3c3018f981b"
BASE_TOOL_SCHEMA_REVISION = "otel-demo-0c468be85e4d"
REPORT_CONTRACT_SHA256 = (
    "373f5030175c5a34999d6e9a72ba9f7af3a9197ffa8f2d990d0761184fc67b55"
)


class PagedApi(ConfigMapApi):
    """ConfigMaps as in #696, plus scripted NetworkPolicy pages: ``np_pages``
    is served in order (one per request to the NetworkPolicy path); each
    entry is ``(status, body)``."""

    def __init__(self) -> None:
        super().__init__()
        self.np_pages: list[tuple[int, bytes]] = []
        self._np_served = 0

    def answer(self, index: int, record: dict[str, Any]) -> tuple[int, bytes]:
        if record["method"] == "GET" and NP_PATH.fullmatch(record["path"]):
            if self.np_pages:
                page = self.np_pages[min(self._np_served, len(self.np_pages) - 1)]
                self._np_served += 1
                return page
        if record["method"] == "GET" and record["path"].startswith(METRICS):
            return 200, _json(
                {
                    "kind": "PodMetricsList",
                    "apiVersion": "metrics.k8s.io/v1beta1",
                    "metadata": {},
                    "items": [],
                }
            )
        return super().answer(index, record)

    def np_requests(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if NP_PATH.fullmatch(r["path"])]


def np_page(items: list[dict[str, Any]], token: str | None = None) -> bytes:
    metadata: dict[str, Any] = {"resourceVersion": "679"}
    if token is not None:
        metadata["continue"] = token
    return _json(
        {
            "kind": "NetworkPolicyList",
            "apiVersion": "networking.k8s.io/v1",
            "metadata": metadata,
            "items": items,
        }
    )


@pytest.fixture
def api(tls_pair) -> Iterator[PagedApi]:  # noqa: F811
    fake = PagedApi()
    fake.start(tls_pair)
    yield fake
    fake.stop()


def _worker(api: PagedApi, **env: str | None) -> Worker:
    return Worker(_env(api, **{SWITCH: None, **env}))


def _params(tool: str, kind: str, namespace: str | None, **extra: str) -> dict:
    params: dict[str, object] = {"kind": kind, **extra}
    if namespace is not None:
        params["namespace"] = namespace
    if tool == TABULAR:
        params["columns"] = "NAME:.metadata.name"
    else:
        params["jq_expr"] = ".items[] | .metadata.name"
    return params


def _call(api: PagedApi, tool: str, params: Mapping[str, object]):
    return _worker(api).call(tool, params)


def _status(outcome) -> tuple[str, Any]:
    return outcome.status, outcome.reason


# -- 1. empty list read in full -> ok ----------------------------------------------


@pytest.mark.parametrize("namespace", [NS, None], ids=["namespaced", "cluster"])
@pytest.mark.parametrize("tool", LIST_TOOLS)
def test_zero_objects_read_in_full_is_ok_with_zero_processed(api, tool, namespace):
    outcome = _call(api, tool, _params(tool, "networkpolicies", namespace))
    view = _view(outcome)
    assert _status(outcome) == ("ok", None), view
    assert view["citable_as_fact"] is True
    assert outcome.evidence.view["citable_as_fact"] is True
    assert view["total_items_processed"] == 0
    assert view["content"] == []  # CHOSEN: no placeholder row
    assert view["scope"]["kind"] == "networkpolicies"
    assert view["scope"]["namespace"] == namespace
    assert len(api.np_requests()) == 1


@pytest.mark.parametrize("tool", LIST_TOOLS)
def test_ok_empty_list_carries_the_queried_selectors(api, tool):
    params = _params(
        tool,
        "networkpolicies",
        NS,
        label_selector="app=checkout",
        field_selector="metadata.name=deny-all",
    )
    outcome = _call(api, tool, params)
    view = _view(outcome)
    assert _status(outcome) == ("ok", None), view
    assert view["total_items_processed"] == 0
    # CHOSEN: in ``scope``, as kubernetes_tabular_query already reports them.
    assert view["scope"]["label_selector"] == "app=checkout", view["scope"]
    assert view["scope"]["field_selector"] == "metadata.name=deny-all", view["scope"]
    [sent] = api.np_requests()
    assert sent["query"]["labelSelector"] == ["app=checkout"]
    assert sent["query"]["fieldSelector"] == ["metadata.name=deny-all"]


def test_count_of_zero_objects_is_count_zero_and_ok(api):
    outcome = _call(api, COUNT, _params(COUNT, "networkpolicies", NS))
    assert _status(outcome) == ("ok", None), _view(outcome)
    assert json.loads(outcome.evidence.raw)["count"] == 0
    assert _view(outcome)["matches_found"] == 0


@pytest.mark.parametrize("tool", LIST_TOOLS)
def test_empty_pages_followed_to_the_end_are_ok(api, tool):
    api.np_pages = [(200, np_page([], token="np+1/=")), (200, np_page([]))]
    outcome = _call(api, tool, _params(tool, "networkpolicies", NS))
    view = _view(outcome)
    assert _status(outcome) == ("ok", None), view
    assert view["total_items_processed"] == 0
    assert view["pages_read"] == 2
    sent = api.np_requests()
    assert len(sent) == 2 and sent[1]["query"]["continue"] == ["np+1/="]


# -- 2. what stays not ok ----------------------------------------------------------


@pytest.mark.parametrize("tool", (JQ, COUNT))
def test_jq_filtering_a_non_empty_list_to_nothing_stays_no_data(api, tool):
    api.configmaps = [configmap("flags", None), configmap("other", None)]
    params = {
        "kind": "configmaps",
        "namespace": NS,
        "jq_expr": '.items[] | select(.metadata.name == "absent")',
    }
    outcome = _call(api, tool, params)
    view = _view(outcome)
    assert _status(outcome) == ("no_data", "NO_DATA"), view
    assert view["citable_as_fact"] is False
    assert view["total_items_processed"] == 2


@pytest.mark.parametrize("namespace", [NS, None], ids=["namespaced", "cluster"])
@pytest.mark.parametrize("tool", LIST_TOOLS)
def test_zero_events_stay_no_data(api, tool, namespace):
    outcome = _call(api, tool, _params(tool, "events", namespace))
    view = _view(outcome)
    assert _status(outcome) == ("no_data", "NO_DATA"), view
    assert view["citable_as_fact"] is False


@pytest.mark.parametrize("tool", LIST_TOOLS)
@pytest.mark.parametrize(
    "second",
    [
        (500, status_body(500, "InternalError", "etcd unavailable")),
        (
            410,
            status_body(410, "Expired", "the provided continue parameter is too old"),
        ),
    ],
    ids=["500", "410-expired"],
)
def test_an_interrupted_pagination_is_never_ok(api, tool, second):
    api.np_pages = [(200, np_page([], token="np+1/=")), second]
    outcome = _call(api, tool, _params(tool, "networkpolicies", NS))
    view = _view(outcome)
    assert outcome.status != "ok", view
    assert view.get("citable_as_fact") is not True
    assert len(api.np_requests()) == 2


@pytest.mark.parametrize("tool", LIST_TOOLS)
def test_a_refused_first_page_is_never_ok(api, tool):
    api.np_pages = [(403, status_body(403, "Forbidden", "forbidden"))]
    outcome = _call(api, tool, _params(tool, "networkpolicies", NS))
    assert outcome.status != "ok", _view(outcome)
    assert _view(outcome).get("citable_as_fact") is not True


# -- 3. other tools' empty results stay no_data (shared executor path) -----------


def test_a_generic_transport_with_an_empty_body_stays_no_data():
    executor, transport, _, _ = build()
    transport.response = TransportResponse(body=body([]))
    outcome = executor.execute(request())
    assert _status(outcome) == ("no_data", "NO_DATA"), outcome.model_view
    assert outcome.model_view["citable_as_fact"] is False


def test_helm_list_of_an_empty_namespace_stays_no_data(api):
    api.secrets = history("shop", "b4-a", 2)
    outcome = Worker(_env(api)).call(LIST, {"namespace": "b4-none"})
    assert _status(outcome) == ("no_data", "NO_DATA"), _view(outcome)


def test_kubectl_top_pods_with_no_pods_stays_no_data(api):
    worker = _worker(api, OPSPILOT_K8S_LIVE_METRICS="1")
    outcome = worker.call("kubectl_top_pods", {"namespace": "nothing-here"})
    assert _status(outcome) == ("no_data", "NO_DATA"), _view(outcome)


# -- 4. descriptions, report rule and base revisions -------------------------------


def _ext_env(api: PagedApi | None = None) -> dict[str, str]:
    return _env(api, **{SWITCH: None})


@pytest.mark.parametrize("tool", LIST_TOOLS)
def test_descriptions_say_an_empty_list_proves_only_absence_at_query_time(tool):
    face = select_profile(_ext_env()).face(FakeClock(start=NOW))
    [schema] = [s for s in face.tool_schemas if s["function"]["name"] == tool]
    text = " ".join(schema["function"]["description"].split()).lower()
    # CHOSEN: loose match of "no objects matching these conditions at query time".
    assert re.search(r"no objects match\w*[^.]{0,60}at query time", text), text


def test_base_profile_prompt_and_tool_revisions_do_not_move():
    env = {
        PROFILE_ENV: "otel-demo",
        "OPSPILOT_OTEL_PROMETHEUS_URL": "http://127.0.0.1:19090",
        "OPSPILOT_OTEL_JAEGER_URL": "http://127.0.0.1:16686/jaeger/ui",
    }
    versions = select_profile(env).versions()
    assert versions["prompt_revision"] == BASE_PROMPT_REVISION
    assert versions["tool_schema_revision"] == BASE_TOOL_SCHEMA_REVISION
    assert hashlib.sha256(REPORT_CONTRACT.encode()).hexdigest() == (
        REPORT_CONTRACT_SHA256
    )


def _delivered(env: Mapping[str, str], outcome):
    context = select_profile(env).face(FakeClock(start=NOW)).evidence_context(RUN_ID)
    policy_ids = [p["id"] for p in context["time_policies"]]
    view = delivered_view(
        dict(outcome.evidence.view),
        evidence_context=context,
        authorized_targets=frozenset({TARGET_ID}),
    )
    assert view is not None
    return view, policy_ids


def _fact_report(text: str, evidence_id: str, time_scope_ref: str) -> ReportV2:
    return ReportV2.model_validate(
        {
            "schema_version": "m0-report-v2",
            "assessment_status": "completed",
            "conclusion": "supported",
            "summary": text,
            "claims": [
                {
                    "kind": "fact",
                    "text": text,
                    "evidence_ids": [evidence_id],
                    "target_refs": [TARGET_ID],
                    "time_scope_ref": time_scope_ref,
                }
            ],
            "gaps": [],
        }
    )


def _unsupported(env, outcome, text: str):
    view, policy_ids = _delivered(env, outcome)
    bound = sorted(view.time_scope_refs & set(policy_ids)) or policy_ids
    report = _fact_report(text, view.evidence_id, bound[0])
    return view, unsupported_citations(
        report,
        views=[view],
        authorized_targets=frozenset({TARGET_ID}),
        time_policy_ids=policy_ids,
    )


def test_a_fact_citing_the_ok_empty_list_passes_the_citation_check(api):
    env = _ext_env(api)
    outcome = Worker(env).call(JQ, _params(JQ, "networkpolicies", NS))
    assert _status(outcome) == ("ok", None), _view(outcome)
    view, problems = _unsupported(
        env, outcome, f"there is no NetworkPolicy in namespace {NS} at query time"
    )
    assert view.citable_as_fact is True
    assert not problems, problems


def test_a_fact_citing_a_jq_filtered_empty_result_is_still_rejected(api):
    """Negative control: the check still bites on the no_data case."""
    api.configmaps = [configmap("flags", None)]
    env = _ext_env(api)
    outcome = Worker(env).call(
        JQ,
        {
            "kind": "configmaps",
            "namespace": NS,
            "jq_expr": '.items[] | select(.metadata.name == "absent")',
        },
    )
    assert _status(outcome) == ("no_data", "NO_DATA"), _view(outcome)
    view, problems = _unsupported(env, outcome, "configmap absent does not exist")
    assert view.citable_as_fact is False
    assert problems
