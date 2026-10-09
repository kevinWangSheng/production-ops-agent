"""独立 HTTP 验收合同：M1-03 第 3 步，F13 步骤 2–3。

仅使用冻结接口及存储读接口；审核原语只能由 HTTP 路由触发。
本模块独立建临时 PG 库并迁移，不共享实验库的数据。
每个 test 的 docstring 标注合同条款；不验收生成调度或 M2 知识加载。
"""

import os
from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema
from opspilot.knowledge import Actor, DisputeDraft, KnowledgeStore
from opspilot.knowledge.contract import (
    EVENT_PAYLOAD_KEYS,
    REVIEW_ERROR_CLASSES,
    GenerationStatus,
    IncidentPostmortemView,
)
from opspilot.persistence import DurableStore, PoolConfig
from opspilot.web import (
    AuthConfig,
    Authenticator,
    DurableClock,
    DurableEventLog,
    DurableEvidenceStore,
    DurableIncidentStore,
    DurableWebLedger,
    PostmortemReview,
    Workbench,
    create_app,
    hash_password,
    token_digest,
)
from scripts.m0.postgres_lab import DSN
from tests.integration.test_m1_03_knowledge_store_postgres import (
    _conclusions,
    _proposal,
    _seed_incident,
)
from tests.m1_web_support import (
    EVENT_TOKEN,
    ORIGIN,
    UI_PASSWORD,
    UI_USER,
    basic,
    bearer,
    call,
    post_form,
    same_origin,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="explicit PG opt-in required"
)
POOL = PoolConfig(min_size=0, max_size=2, timeout=5.0)
WORKER = Actor("contract-worker", "worker")


class SnapshotKnowledge(KnowledgeStore):
    """仅补基线缺失的第 2 步读投影，不替代任何审核行为。"""

    def incident_postmortem(self, incident_id: UUID) -> IncidentPostmortemView:
        parent = getattr(super(), "incident_postmortem", None)
        if parent is not None:
            return parent(incident_id)
        snapshot = self.postmortem_for_incident(incident_id)
        status: GenerationStatus = "not_generated"
        if snapshot is not None:
            latest = max(snapshot["versions"], key=lambda row: row["version"])
            status = (
                "citations_failed" if latest["state"] == "draft" else latest["state"]
            )
        return {
            "incident_id": incident_id,
            "status": status,
            "postmortem": snapshot,
            "schedule": {
                "pending_regeneration_version": None,
                "lease_active": False,
                "consecutive_failures": 0,
                "next_attempt_at": None,
                "last_error_code": None,
            },
            "attempts": [],
        }


@pytest.fixture(scope="module")
def dsn() -> Iterator[str]:
    name = f"opspilot_review_contract_{uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    scratch = make_conninfo(DSN, dbname=name)
    try:
        schema.migrate(scratch, pg_dump=os.environ.get("OPSPILOT_PG_DUMP", "pg_dump"))
        yield scratch
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


@dataclass
class Harness:
    app: object
    knowledge: SnapshotKnowledge
    events: DurableEventLog
    dsn: str


@pytest.fixture
def h(dsn: str) -> Iterator[Harness]:
    durable = DurableStore(dsn, pool=POOL)
    knowledge = SnapshotKnowledge(dsn, pool=POOL)
    try:
        durable.install()
        knowledge.install()
        events = DurableEventLog(durable)
        events.install()
        evidence = DurableEvidenceStore(durable)
        evidence.install()
        ledger = DurableWebLedger(durable)
        ledger.install()
        workbench = Workbench(
            incidents=DurableIncidentStore(durable),
            events=events,
            evidence=evidence,
            ledger=ledger,
            run_versions={"state": "v1"},
            run_seconds=600,
        )
        auth = Authenticator(
            AuthConfig(
                ui_users={UI_USER: hash_password(UI_PASSWORD)},
                event_tokens={token_digest(EVENT_TOKEN): "alertmanager-contract"},
                auth_revision="review-contract",
                allowed_origins=frozenset({ORIGIN}),
            )
        )
        app = create_app(
            workbench,
            auth,
            DurableClock(durable),
            review=PostmortemReview(knowledge=knowledge, events=events),
        )
        yield Harness(app, knowledge, events, dsn)
    finally:
        knowledge.close()
        durable.close()


class Page(HTMLParser):
    """按元素 ID/表单字段/链接验收，不依赖模板空白与属性顺序。"""

    def __init__(self, text):
        super().__init__()
        self.ids = set()
        self.texts = {}
        self.links = set()
        self.inputs = {}
        self.stack = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        element_id = attrs.get("id")
        if element_id:
            self.ids.add(element_id)
            self.texts.setdefault(element_id, "")
        if tag == "a":
            self.links.add(attrs.get("href"))
        if tag == "input":
            self.inputs[attrs.get("name")] = attrs
        # Void elements have no matching end tag.
        if tag not in {"input", "br", "hr", "img", "meta", "link"}:
            self.stack.append((tag, element_id))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        for _, element_id in self.stack:
            if element_id:
                self.texts[element_id] += data

    def text(self, element_id):
        assert element_id in self.ids
        return self.texts[element_id].strip()


def _key():
    return f"review-contract-{uuid4().hex}"


def _version_path(draft):
    return f"/postmortems/{draft.object_id}/versions/{draft.version}"


def _page(h, path):
    response = call(h.app, "GET", path, headers=basic())
    assert response.status == 200, response.text
    return Page(response.text)


def _draft(h, *, proposals=None, disputed=False, citations=True):
    incident_id, watermark = _seed_incident(h.dsn)
    draft = h.knowledge.record_draft(
        incident_id,
        expected_generation=0,
        idempotency_key=_key(),
        actor=WORKER,
        watermark=watermark,
        content={
            "validation": {
                "citations_valid": citations,
                "errors": {} if citations else {"finding-1": ["UNKNOWN_EVIDENCE"]},
            },
        },
        conclusions=_conclusions(citations_valid=citations),
        proposals=[_proposal()] if proposals is None else proposals,
        disputes=[DisputeDraft("finding-1", "部署前错误率也已升高")]
        if disputed
        else [],
    )
    return incident_id, draft


def _fields(draft, action="approve", **changes):
    fields = {
        "action": action,
        "expected_generation": str(draft.generation),
        "idempotency_key": _key(),
    }
    if action in {"reject", "return"}:
        fields["reason"] = "请核查部署前的证据"
    fields.update(changes)
    return fields


def _post(h, path, fields, *, html=False, headers=None):
    return post_form(
        h.app,
        path,
        fields,
        headers=headers
        if headers is not None
        else {
            **basic(),
            **same_origin(),
            "accept": "text/html" if html else "application/json",
        },
    )


def _review(h, draft, fields=None, **kwargs):
    return _post(
        h, _version_path(draft) + "/review", fields or _fields(draft), **kwargs
    )


def _error(response, code, *, fields=()):
    classification = REVIEW_ERROR_CLASSES[code]
    assert (
        response.status
        == {
            "conflict": 409,
            "refused": 422,
            "invalid": 400,
            "not_found": 404,
            "unavailable": 503,
        }[classification]
    ), response.text
    data = response.json()
    assert data["code"] == code
    assert data["error_class"] == classification
    assert set(fields) <= set(data.get("fields", []))
    return data


def _unchanged(h, incident_id, draft):
    return (
        h.knowledge.postmortem(draft.object_id),
        h.knowledge.audit_trail("postmortem", draft.object_id),
        h.knowledge.knowledge_from_postmortem(draft.object_id),
        h.events.read_after(incident_id, 0),
    )


def _events(h, incident_id):
    events = h.events.read_after(incident_id, 0)
    for event in events:
        assert event.subject_id == incident_id
        assert event.kind in {"postmortem_reviewed", "knowledge_changed"}
        assert set(event.payload) == set(EVENT_PAYLOAD_KEYS[event.kind])
        assert UI_PASSWORD not in str(event.payload)
    return events


def _audit_actor(h, kind, object_id, action):
    rows = [
        row
        for row in h.knowledge.audit_trail(kind, object_id)
        if row["action"] == action
    ]
    assert len(rows) == 1
    assert (rows[0]["actor_id"], rows[0]["principal_kind"]) == (UI_USER, "basic_auth")
    assert UI_PASSWORD not in str(rows)
    return rows[0]


def _publish(h, *, proposals=None):
    incident_id, draft = _draft(h, proposals=proposals)
    response = _review(h, draft)
    assert response.status == 200, response.text
    return incident_id, draft, response.json()


def test_approval_provenance_audit_events_and_replay(h):
    """D3/D5/D6/D9、R2/R4/R6、F13-3：一次批准发布多个条目并保留来源。"""
    incident_id, draft = _draft(h, proposals=[_proposal("kb-1"), _proposal("kb-2")])
    page = _page(h, _version_path(draft))
    assert page.text("version-state") == "under_review"
    assert {"form-approve", "form-return", "form-reject"} <= page.ids
    assert "form-supersede" not in page.ids
    assert (
        _page(h, f"/incidents/{incident_id}").text("postmortem-status")
        == "under_review"
    )
    assert h.knowledge.knowledge_from_postmortem(draft.object_id) == []
    fields = _fields(draft, actor_id="mallory", actor="model", principal_kind="worker")
    response = _review(h, draft, fields)
    assert response.status == 200, response.text
    data = response.json()
    assert set(data) == {
        "action",
        "postmortem_id",
        "incident_id",
        "version",
        "state",
        "generation",
        "replayed",
        "published",
    }
    assert (data["action"], data["state"], data["replayed"]) == (
        "approve",
        "approved",
        False,
    )
    assert data["postmortem_id"] == str(draft.object_id)
    assert data["incident_id"] == str(incident_id)
    assert data["version"] == draft.version
    assert data["generation"] == draft.generation + 1
    assert set(data["published"]) == {"kb-1", "kb-2"}
    rows = h.knowledge.knowledge_from_postmortem(draft.object_id)
    assert len(rows) == 2
    for key, published in data["published"].items():
        assert set(published) == {"entry_id", "revision", "generation", "action"}
        assert (
            published["revision"],
            published["generation"],
            published["action"],
        ) == (1, 1, "publish")
        entry_id = UUID(published["entry_id"])
        active = h.knowledge.active_revision(entry_id)
        assert (active["approved_by"], active["approved_by_kind"]) == (
            UI_USER,
            "basic_auth",
        )
        assert (
            active["source_postmortem_id"],
            active["source_version"],
            active["source_proposal_key"],
        ) == (draft.object_id, draft.version, key)
        row = next(r for r in rows if r["entry_id"] == entry_id)
        assert (
            row["revision"],
            row["source_version"],
            row["source_proposal_key"],
            row["supersedes_revision"],
            row["state"],
        ) == (1, draft.version, key, None, "active")
        assert row["name"] == active["name"]
        _audit_actor(h, "knowledge_entry", entry_id, "publish")
    _audit_actor(h, "postmortem", draft.object_id, "approve")
    events = _events(h, incident_id)
    assert sorted(e.kind for e in events) == [
        "knowledge_changed",
        "knowledge_changed",
        "postmortem_reviewed",
    ]
    reviewed = next(e.payload for e in events if e.kind == "postmortem_reviewed")
    assert reviewed == {
        "postmortem_id": str(draft.object_id),
        "version": draft.version,
        "state": "approved",
        "generation": data["generation"],
        "reason": None,
    }
    changed = [e.payload for e in events if e.kind == "knowledge_changed"]
    assert {e["entry_id"] for e in changed} == {
        p["entry_id"] for p in data["published"].values()
    }
    assert all(
        (e["revision"], e["state"], e["generation"], e["reason"])
        == (1, "active", 1, None)
        for e in changed
    )
    before = _unchanged(h, incident_id, draft)
    replay = _review(h, draft, fields)
    assert replay.status == 200
    assert replay.json() == {**data, "replayed": True}
    assert _unchanged(h, incident_id, draft) == before
    conflict = _review(h, draft, {**fields, "action": "reject", "reason": "不同请求"})
    _error(conflict, "IDEMPOTENCY_CONFLICT")
    assert _unchanged(h, incident_id, draft) == before
    page = _page(h, f"/incidents/{incident_id}")
    assert page.text("postmortem-status") == "approved"
    assert _version_path(draft) in page.links
    for published in data["published"].values():
        assert f"/knowledge/{published['entry_id']}" in page.links


@pytest.mark.parametrize("terminal", ["approved", "rejected", "returned", "stale"])
def test_only_under_review_accepts_actions_and_terminal_versions_are_read_only(
    h, terminal
):
    """D5/D9/D14、R2/R6：批准、驳回、退回、陈旧之后原版均不可再审核。"""
    incident_id, draft = _draft(h)
    if terminal == "stale":
        result = h.knowledge.mark_stale(
            draft.object_id,
            draft.version,
            reason="run_added",
            expected_generation=draft.generation,
            idempotency_key=_key(),
            actor=WORKER,
        )
        generation = result.generation
    else:
        action = {"approved": "approve", "rejected": "reject", "returned": "return"}[
            terminal
        ]
        fields = _fields(draft, action)
        response = _review(h, draft, fields)
        assert response.status == 200, response.text
        data = response.json()
        assert data["state"] == terminal
        generation = data["generation"]
        before = _unchanged(h, incident_id, draft)
        assert _review(h, draft, fields).json() == {**data, "replayed": True}
        assert _unchanged(h, incident_id, draft) == before
        _audit_actor(h, "postmortem", draft.object_id, action)
        assert (
            len([e for e in _events(h, incident_id) if e.kind == "postmortem_reviewed"])
            == 1
        )
    page = _page(h, _version_path(draft))
    assert page.text("version-state") == terminal
    assert (
        not {"form-approve", "form-supersede", "form-return", "form-reject"} & page.ids
    )
    assert _page(h, f"/incidents/{incident_id}").text("postmortem-status") == terminal
    if terminal == "stale":
        assert page.text("stale-reason") == "run_added"
    before = _unchanged(h, incident_id, draft)
    for action in ("approve", "reject", "return"):
        _error(
            _review(
                h, draft, _fields(draft, action, expected_generation=str(generation))
            ),
            "ILLEGAL_TRANSITION",
        )
        assert _unchanged(h, incident_id, draft) == before
    if terminal != "approved":
        assert h.knowledge.knowledge_from_postmortem(draft.object_id) == []


@pytest.mark.parametrize("blocked", ["dispute", "citations"])
def test_dispute_and_citation_failures_never_publish(h, blocked):
    """D3/D15/D16、R2/R3、F13-2：直接 POST 也不能绕过争议/引用失败。"""
    incident_id, draft = _draft(
        h, disputed=blocked == "dispute", citations=blocked != "citations"
    )
    page = _page(h, _version_path(draft))
    assert not {"form-approve", "form-supersede"} & page.ids
    if blocked == "dispute":
        assert "部署前错误率也已升高" in page.text("disputes")
        assert {"form-return", "form-reject"} <= page.ids
        code = "DISPUTED"
    else:
        assert page.text("version-state") == "draft"
        assert not {"form-return", "form-reject"} & page.ids
        assert "finding-1" in page.text("citation-errors")
        assert "UNKNOWN_EVIDENCE" in page.text("citation-errors")
        assert (
            _page(h, f"/incidents/{incident_id}").text("postmortem-status")
            == "citations_failed"
        )
        # A failed citation draft never entered review (D15), so D5 applies first.
        code = "ILLEGAL_TRANSITION"
    before = _unchanged(h, incident_id, draft)
    _error(_review(h, draft), code)
    assert _unchanged(h, incident_id, draft) == before
    assert h.knowledge.knowledge_from_postmortem(draft.object_id) == []
    if blocked == "dispute":
        returned = _review(h, draft, _fields(draft, "return"))
        assert returned.status == 200
        assert returned.json()["state"] == "returned"
        assert "部署前错误率也已升高" in _page(h, _version_path(draft)).text("disputes")


def test_generation_conflict_is_visible_in_json_and_html_without_writes(h):
    """D9、R3：返回当前复盘代际；HTML 拒绝就地显示，可刷新恢复。"""
    incident_id, draft = _draft(h)
    fields = _fields(draft, expected_generation=str(draft.generation - 1))
    before = _unchanged(h, incident_id, draft)
    data = _error(_review(h, draft, fields), "GENERATION_CONFLICT")
    assert data["current_generation"] == draft.generation
    html = _review(h, draft, fields, html=True)
    assert html.status == 409
    page = Page(html.text)
    assert page.text("review-error")
    assert page.text("version-state") == "under_review"
    assert _unchanged(h, incident_id, draft) == before


@pytest.mark.parametrize(
    "change,missing,field",
    [
        ({"action": "publish"}, None, "action"),
        ({"expected_generation": "-1"}, None, "expected_generation"),
        ({"expected_generation": "no"}, None, "expected_generation"),
        ({"idempotency_key": ""}, None, "idempotency_key"),
        ({}, "expected_generation", "expected_generation"),
        ({}, "idempotency_key", "idempotency_key"),
        ({"reason": "unexpected"}, None, "reason"),
        ({"action": "reject", "reason": " "}, None, "reason"),
        ({"action": "return"}, None, "reason"),
    ],
)
def test_malformed_review_commands_are_invalid_and_write_nothing(
    h, change, missing, field
):
    """D5/D9/D25、R3：ReviewCommand 字段校验。"""
    incident_id, draft = _draft(h)
    fields = _fields(draft)
    fields.update(change)
    if missing:
        fields.pop(missing)
    before = _unchanged(h, incident_id, draft)
    _error(_review(h, draft, fields), "INVALID_INPUT", fields=[field])
    assert _unchanged(h, incident_id, draft) == before


def test_supersede_requires_complete_current_entry_generations_and_preserves_history(h):
    """D3/D5/D7/D9/D25、R2/R3/R4/R6：替换两个条目，全部代际正确才发布。"""
    old_incident, old_draft, original = _publish(
        h, proposals=[_proposal("one"), _proposal("two")]
    )
    entries = {key: UUID(p["entry_id"]) for key, p in original["published"].items()}
    incident_id, draft = _draft(
        h, proposals=[_proposal(key, entry) for key, entry in entries.items()]
    )
    page = _page(h, _version_path(draft))
    assert "form-supersede" in page.ids and "form-approve" not in page.ids
    for entry in entries.values():
        control = page.inputs[f"entry_generation:{entry}"]
        assert control["type"] == "hidden" and control["value"] == "1"
    fields = _fields(
        draft,
        "supersede",
        **{f"entry_generation:{entry}": "1" for entry in entries.values()},
    )
    before = _unchanged(h, incident_id, draft)
    histories = {
        entry: h.knowledge.knowledge_history(entry) for entry in entries.values()
    }
    _error(
        _review(h, draft, {**fields, "action": "approve"}),
        "INVALID_INPUT",
        fields=["action"],
    )
    partial = dict(fields)
    partial.pop(f"entry_generation:{entries['two']}")
    _error(_review(h, draft, partial), "INVALID_INPUT")
    _error(
        _review(h, draft, {**fields, f"entry_generation:{uuid4()}": "1"}),
        "INVALID_INPUT",
    )
    for bad in ("-1", "bad"):
        _error(
            _review(h, draft, {**fields, f"entry_generation:{entries['one']}": bad}),
            "INVALID_INPUT",
        )
    stale = _error(
        _review(h, draft, {**fields, f"entry_generation:{entries['one']}": "0"}),
        "ENTRY_GENERATION_CONFLICT",
    )
    assert stale["entry_generations"][str(entries["one"])] == 1
    assert _unchanged(h, incident_id, draft) == before
    assert histories == {
        entry: h.knowledge.knowledge_history(entry) for entry in entries.values()
    }
    response = _review(h, draft, fields)
    assert response.status == 200, response.text
    data = response.json()
    assert (data["action"], data["state"], data["replayed"]) == (
        "supersede",
        "approved",
        False,
    )
    assert data["generation"] == draft.generation + 1
    for key, entry in entries.items():
        assert data["published"][key] == {
            "entry_id": str(entry),
            "revision": 2,
            "generation": 2,
            "action": "supersede",
        }
        history = h.knowledge.knowledge_history(entry)["revisions"]
        assert [
            (r["revision"], r["state"], r["supersedes_revision"]) for r in history
        ] == [(1, "superseded", None), (2, "active", 1)]
        assert (
            history[0]["content_sha256"]
            == histories[entry]["revisions"][0]["content_sha256"]
        )
        assert h.knowledge.active_revision(entry)["revision"] == 2
        _audit_actor(h, "knowledge_entry", entry, "supersede")
        detail = _page(h, f"/knowledge/{entry}")
        assert (
            "active" in " ".join(detail.texts.values())
            or "active"
            in call(h.app, "GET", f"/knowledge/{entry}", headers=basic()).text
        )
        assert "form-revoke" in detail.ids
    # This is an audit/navigation read, so it retains the superseded revision
    # with its current state. D3's active-only contract is enforced by
    # active_revision(), checked above for the new active revision.
    old_rows = h.knowledge.knowledge_from_postmortem(old_draft.object_id)
    assert [(row["revision"], row["state"]) for row in old_rows] == [
        (1, "superseded"),
        (1, "superseded"),
    ]
    rows = h.knowledge.knowledge_from_postmortem(draft.object_id)
    assert {
        (r["source_proposal_key"], r["revision"], r["state"], r["supersedes_revision"])
        for r in rows
    } == {("one", 2, "active", 1), ("two", 2, "active", 1)}
    events = _events(h, incident_id)
    assert sorted(e.kind for e in events) == [
        "knowledge_changed",
        "knowledge_changed",
        "postmortem_reviewed",
    ]
    assert len(_events(h, old_incident)) == 3
    before = _unchanged(h, incident_id, draft)
    assert _review(h, draft, fields).json() == {**data, "replayed": True}
    assert _unchanged(h, incident_id, draft) == before


def test_approve_and_supersede_page_action_names_are_not_interchangeable(h):
    """D25、R3：新条目版本不能用 supersede，拼错 action 不落库。"""
    incident_id, draft = _draft(h)
    before = _unchanged(h, incident_id, draft)
    _error(
        _review(
            h,
            draft,
            _fields(draft, "supersede", **{f"entry_generation:{uuid4()}": "0"}),
        ),
        "INVALID_INPUT",
        fields=["action"],
    )
    _error(
        _review(h, draft, _fields(draft, **{f"entry_generation:{uuid4()}": "0"})),
        "INVALID_INPUT",
    )
    assert _unchanged(h, incident_id, draft) == before


def test_revoke_requires_revision_generation_reason_and_keeps_tombstone(h):
    """D3/D5/D6/D7/D9/D25、R2/R3/R4/R6：撤销独立审核，来源事故事件与墓碑。"""
    incident_id, draft, approved = _publish(h)
    published = approved["published"]["kb-1"]
    entry = UUID(published["entry_id"])
    path = f"/knowledge/{entry}/revoke"
    fields = {
        "revision": "1",
        "expected_generation": "1",
        "idempotency_key": _key(),
        "reason": "原结论已被新证据推翻",
        "actor_id": "mallory",
        "principal_kind": "worker",
    }
    before = h.knowledge.knowledge_history(entry)
    events_before = h.events.read_after(incident_id, 0)
    audit_before = h.knowledge.audit_trail("knowledge_entry", entry)
    for field in ("revision", "expected_generation", "idempotency_key", "reason"):
        invalid = dict(fields)
        invalid.pop(field)
        _error(_post(h, path, invalid), "INVALID_INPUT", fields=[field])
    for field, value in (
        ("revision", "0"),
        ("revision", "bad"),
        ("expected_generation", "-1"),
        ("reason", " "),
    ):
        _error(
            _post(h, path, {**fields, field: value}), "INVALID_INPUT", fields=[field]
        )
    conflict = _error(
        _post(h, path, {**fields, "expected_generation": "0"}), "GENERATION_CONFLICT"
    )
    assert conflict["current_generation"] == 1
    assert h.knowledge.knowledge_history(entry) == before
    assert h.knowledge.audit_trail("knowledge_entry", entry) == audit_before
    assert h.events.read_after(incident_id, 0) == events_before
    response = _post(h, path, fields)
    assert response.status == 200, response.text
    data = response.json()
    assert data == {
        "action": "revoke",
        "entry_id": str(entry),
        "revision": 1,
        "state": "revoked",
        "generation": 2,
        "replayed": False,
    }
    assert h.knowledge.active_revision(entry) is None
    [revision] = h.knowledge.knowledge_history(entry)["revisions"]
    assert revision["state"] == "revoked"
    assert revision["revoked_reason"] == fields["reason"]
    assert (revision["revoked_by"], revision["revoked_by_kind"]) == (
        UI_USER,
        "basic_auth",
    )
    assert revision["content_sha256"] == before["revisions"][0]["content_sha256"]
    assert h.knowledge.postmortem(draft.object_id)["versions"][0]["state"] == "approved"
    _audit_actor(h, "knowledge_entry", entry, "revoke")
    events = _events(h, incident_id)
    assert len(events) == len(events_before) + 1
    assert events[-1].kind == "knowledge_changed"
    assert events[-1].payload == {
        "entry_id": str(entry),
        "revision": 1,
        "state": "revoked",
        "generation": 2,
        "reason": fields["reason"],
    }
    page_response = call(h.app, "GET", f"/knowledge/{entry}", headers=basic())
    assert page_response.status == 200
    assert "revoked" in page_response.text and fields["reason"] in page_response.text
    assert "form-revoke" not in Page(page_response.text).ids
    before_replay = h.knowledge.knowledge_history(entry)
    assert _post(h, path, fields).json() == {**data, "replayed": True}
    _error(_post(h, path, {**fields, "reason": "另一理由"}), "IDEMPOTENCY_CONFLICT")
    _error(
        _post(
            h, path, {**fields, "expected_generation": "2", "idempotency_key": _key()}
        ),
        "ILLEGAL_TRANSITION",
    )
    assert h.knowledge.knowledge_history(entry) == before_replay
    assert _events(h, incident_id) == events


@pytest.mark.parametrize("route", ["review", "revoke"])
def test_post_requires_basic_auth_and_same_origin_but_get_only_needs_auth(h, route):
    """D6、R5、C3 §9：两个写入口都防跨站；事件 token 不授予审核权限。"""
    if route == "review":
        incident_id, draft = _draft(h)
        path = _version_path(draft) + "/review"
        fields = _fields(draft)
        read_path = _version_path(draft)
    else:
        incident_id, draft, approved = _publish(h)
        entry = UUID(approved["published"]["kb-1"]["entry_id"])
        path = f"/knowledge/{entry}/revoke"
        read_path = f"/knowledge/{entry}"
        fields = {
            "revision": "1",
            "expected_generation": "1",
            "idempotency_key": _key(),
            "reason": "核查失败",
        }
    before = _unchanged(h, incident_id, draft)
    for headers in (
        {**same_origin()},
        {**basic(password="wrong"), **same_origin()},
        {**bearer(), **same_origin()},
    ):
        assert _post(h, path, fields, headers=headers).status == 401
    for headers in (basic(), {**basic(), "origin": "https://evil.example"}):
        response = _post(h, path, fields, headers=headers)
        assert response.status == 403
        assert response.json()["code"] == "ORIGIN_REJECTED"
    assert _unchanged(h, incident_id, draft) == before
    # R5 reuses the existing check: an allowed Origin is sufficient even if
    # a synthetic Fetch-Metadata header disagrees. Browsers do not emit this
    # combination for a real cross-site form, so it is not a bypass case.
    accepted = _post(
        h,
        path,
        fields,
        headers={**basic(), **same_origin(), "sec-fetch-site": "cross-site"},
    )
    assert accepted.status == 200, accepted.text
    assert call(h.app, "GET", read_path).status == 401
    assert call(h.app, "GET", read_path, headers=basic()).status == 200
    assert (
        call(h.app, "GET", f"/incidents/{incident_id}", headers=basic()).status == 200
    )


def test_html_success_redirects_to_version_and_entry(h):
    """R2/R3：HTML 成功 303 返回对应详情页。"""
    _, draft = _draft(h)
    response = _review(h, draft, html=True)
    assert response.status == 303 and response.headers["location"] == _version_path(
        draft
    )
    [published] = h.knowledge.knowledge_from_postmortem(draft.object_id)
    entry = published["entry_id"]
    fields = {
        "revision": "1",
        "expected_generation": "1",
        "idempotency_key": _key(),
        "reason": "撤销验证",
    }
    invalid = _post(
        h,
        f"/knowledge/{entry}/revoke",
        {**fields, "expected_generation": "0"},
        html=True,
    )
    assert invalid.status == 409 and Page(invalid.text).text("review-error")
    response = _post(h, f"/knowledge/{entry}/revoke", fields, html=True)
    assert (
        response.status == 303 and response.headers["location"] == f"/knowledge/{entry}"
    )


def test_missing_postmortem_is_visible_and_invalid_or_unknown_routes_are_404(h):
    """R2/R3、D11：未生成可见；缺失/格式错误 ID 与版本号不变为写请求。"""
    incident_id, _ = _seed_incident(h.dsn)
    page = _page(h, f"/incidents/{incident_id}")
    assert page.text("postmortem-status") == "not_generated"
    _, draft = _draft(h)
    missing = uuid4()
    for path in (
        f"/incidents/{missing}",
        "/incidents/not-a-uuid",
        f"/knowledge/{missing}",
        "/knowledge/not-a-uuid",
        f"/postmortems/{missing}/versions/1",
        "/postmortems/not-a-uuid/versions/1",
        *[
            f"/postmortems/{draft.object_id}/versions/{n}"
            for n in ("bad", "0", "-1", "99")
        ],
    ):
        assert call(h.app, "GET", path, headers=basic()).status == 404
    response = _post(h, f"/postmortems/{missing}/versions/1/review", _fields(draft))
    _error(response, "NOT_FOUND")
    for n in ("bad", "0", "-1", "99"):
        assert (
            _post(
                h, f"/postmortems/{draft.object_id}/versions/{n}/review", _fields(draft)
            ).status
            == 404
        )
    fields = {
        "revision": "1",
        "expected_generation": "0",
        "idempotency_key": _key(),
        "reason": "不存在",
    }
    _error(_post(h, f"/knowledge/{missing}/revoke", fields), "NOT_FOUND")
