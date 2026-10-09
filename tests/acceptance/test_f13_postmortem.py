"""F13 第 1–3 步独立外部验收（合同 r6）；不验收 M2，也不改 passes。

确定性模型和测试驱动模拟审核不代表真实模型或真人审阅。
基线投影是 NotImplementedError 接口桩：保留失败，不 xfail、不弱化断言。
"""

import copy
import hashlib
import json
import os
from dataclasses import replace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from opspilot import schema, tracing
from opspilot.acceptance import postmortem_outcome, postmortem_records
from opspilot.investigation.loop import ModelError
from opspilot.knowledge import KnowledgeStore
from opspilot.knowledge.contract import CODE_SECTIONS
from opspilot.knowledge.jobs import GenerationStore
from opspilot.persistence import DurableStore
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
)
from tests.f13_acceptance_support import (
    HUMAN_NOTE,
    CountingModel,
    Harness,
    normalize,
    output,
    raw_records,
    scenario,
)
from tests.m1_web_support import ORIGIN, UI_PASSWORD, UI_USER

pytestmark = pytest.mark.skipif(
    os.environ.get("M1_DURABLE_POSTGRES") != "1", reason="需要显式启用临时 PG 验收"
)


@pytest.fixture(scope="module")
def dsn():
    """自建自删隔离临时库；迁移只使用产品既有 schema。"""
    admin = os.environ["OPSPILOT_LAB_DSN"]
    name = f"opspilot_f13_accept_{uuid4().hex[:12]}"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    scratch = make_conninfo(admin, dbname=name)
    try:
        schema.migrate(scratch, pg_dump=os.environ.get("OPSPILOT_PG_DUMP", "pg_dump"))
        yield scratch
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
def h(dsn):
    """真实持久化与 ASGI 工作台；不启动会领取调查 Run 的完整 worker。"""
    durable = DurableStore(dsn)
    knowledge = KnowledgeStore(dsn)
    jobs = GenerationStore(dsn)
    try:
        durable.install()
        knowledge.install()
        jobs.install()
        events = DurableEventLog(durable)
        events.install()
        evidence = DurableEvidenceStore(durable)
        evidence.install()
        ledger = DurableWebLedger(durable)
        ledger.install()
        bench = Workbench(
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
                event_tokens={},
                auth_revision="f13-acceptance",
                allowed_origins=frozenset({ORIGIN}),
            )
        )
        app = create_app(
            bench,
            auth,
            DurableClock(durable),
            review=PostmortemReview(knowledge=knowledge, events=events),
        )
        yield Harness(app, knowledge, jobs, durable, dsn, events)
    finally:
        jobs.close()
        knowledge.close()
        durable.close()


def project(h, incident, model, monkeypatch):
    """R12：投影全程禁止遥测入口，并核对独立模型计数原样不变。"""
    before = len(model.calls)

    def forbidden(*args, **kwargs):
        pytest.fail("外部复盘投影调用了遥测入口")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(tracing, "tracer", forbidden)
            result = postmortem_outcome(
                scenario(incident), postmortem_records(h.knowledge, incident)
            )
    finally:
        assert len(model.calls) == before
    assert result.model_requests == ()
    assert result.unknown_reasons == ()
    assert result.subject_id == str(incident)
    assert result.scenario_id == "f13-independent"
    return result


def assert_records(h, incident, result, model):
    """D11/R8–R10：逐字段复制持久记录；完整计数仅用于少于十次的场景。"""
    records = raw_records(h, incident)
    view = records.view
    assert result.generation_status == view["status"]
    assert result.schedule == view["schedule"]
    assert result.recent_attempts == tuple(view["attempts"])
    assert sum(a["model_requests"] for a in result.recent_attempts) == len(model.calls)
    pm = view["postmortem"]
    assert result.postmortem_id == (None if pm is None else pm["postmortem_id"])
    assert result.postmortem_generation == (None if pm is None else pm["generation"])
    assert result.latest_version == (None if pm is None else pm["latest_version"])
    versions = [] if pm is None else pm["versions"]
    assert len(result.versions) == len(versions)
    for actual, row in zip(result.versions, versions, strict=True):
        for field in (
            "version",
            "revises_version",
            "state",
            "stale_reason",
            "content_sha256",
            "generated_by",
            "generated_by_kind",
            "created_at",
        ):
            assert getattr(actual, field) == row[field]
        assert actual.created_at.tzinfo is not None
        assert actual.watermark == {
            key: row[key]
            for key in (
                "incident_control_generation",
                "observation_generation",
                "observation_session_id",
                "observation_ending_id",
                "run_count",
                "last_run_id",
                "input_watermark",
                "evidence_snapshot_sha256",
            )
        }
        document = json.loads(row["content"])
        assert actual.sections == {key: document[key] for key in CODE_SECTIONS}
        assert actual.evidence_catalog == tuple(document["evidence_catalog"])
        assert actual.generation_record == document["generation"]
        assert actual.validation == document["validation"]
        assert actual.proposals == tuple(row["proposals"])
        assert len(actual.conclusions) == len(row["conclusions"])
        for c, persisted in zip(actual.conclusions, row["conclusions"], strict=True):
            for field in (
                "conclusion_key",
                "ordinal",
                "section",
                "body",
                "author",
                "certainty",
                "citations_valid",
                "dispute_state",
            ):
                assert getattr(c, field) == persisted[field]
            assert c.evidence_refs == tuple(persisted["evidence_refs"])
            assert c.disputes == tuple(persisted["disputes"])
        assert actual.review_actions == tuple(
            a for a in result.review_actions if a.version == actual.version
        )
    for projected, rows in (
        (result.review_actions, records.postmortem_audit),
        (
            result.knowledge_actions,
            tuple(a for audit in records.entry_audit.values() for a in audit),
        ),
    ):
        assert {a.event_id for a in projected} == {a["event_id"] for a in rows}
        by_id = {a["event_id"]: a for a in rows}
        for actual in projected:
            for field in actual.__dataclass_fields__:
                assert getattr(actual, field) == by_id[actual.event_id][field]
    expected = {(r["entry_id"], r["revision"]) for r in records.published}
    assert {(k.entry_id, k.revision) for k in result.knowledge} == expected
    for k in result.knowledge:
        row = next(
            r
            for r in records.entries[k.entry_id]["revisions"]
            if r["revision"] == k.revision
        )
        for field in k.__dataclass_fields__:
            if field in {"retrievable", "freshness", "tags"}:
                continue
            source_field = "event_id" if field == "approval_event_id" else field
            assert getattr(k, field) == row[source_field]
        assert k.tags == tuple(row["tags"])
        active = records.active[k.entry_id]
        assert k.retrievable == (
            active is not None and active["revision"] == k.revision
        )
        assert k.freshness == (active["freshness"] if k.retrievable else None)


def assert_pages(h, incident, result):
    """D34/R14：HTML 单元格对应投影字段，代际变化由调用者重读。"""
    page = h.page(f"/incidents/{incident}")
    assert page.text("postmortem-status") == result.generation_status
    if result.postmortem_id:
        assert page.text("postmortem-generation") == str(result.postmortem_generation)
        assert page.text("postmortem-latest") == f"version {result.latest_version}"
    schedule = result.schedule
    text = page.text("postmortem-schedule")
    assert f"consecutive failures {schedule['consecutive_failures']}" in text
    assert ("generating now" in text) == schedule["lease_active"]
    assert ("regeneration pending" in text) == bool(
        schedule["pending_regeneration_version"]
    )
    for key in ("pending_regeneration_version", "last_error_code", "next_attempt_at"):
        if schedule[key] is not None:
            assert str(schedule[key]) in text
    rows = page.rows.get("postmortem-attempts", [])
    assert len(rows) == len(result.recent_attempts)
    for cells, a in zip(rows, result.recent_attempts, strict=True):
        assert cells == [
            str(a["attempt_id"]),
            a["status"],
            a["error_code"] or "",
            str(a["version"] or "")
            + (f" revises {a['revises_version']}" if a["revises_version"] else ""),
            f"{a['model_requests']} / {a['max_model_requests']}",
            f"{a['started_at']} → {a['finished_at'] or ''}".strip(),
        ]
    version_rows = page.rows.get("postmortem-versions", [])
    assert len(version_rows) == len(result.versions)
    versions_by_number = {v.version: v for v in result.versions}
    for cells in version_rows:
        v = versions_by_number[int(cells[0])]
        assert cells[1] == normalize(v.state + " " + (v.stale_reason or ""))
        assert cells[2] == str(v.revises_version or "")
        assert cells[4] == str(sum(len(c.disputes) for c in v.conclusions))
        assert cells[6] == f"{v.created_at} by {v.generated_by} ({v.generated_by_kind})"
        detail = h.page(f"/postmortems/{result.postmortem_id}/versions/{v.version}")
        assert detail.text("version-state") == v.state
        assert detail.text("postmortem-generation") == str(result.postmortem_generation)
        for key, label in (
            ("incident_control_generation", "control generation"),
            ("observation_generation", "observation generation"),
            ("run_count", "runs"),
            ("input_watermark", "input"),
            ("evidence_snapshot_sha256", "evidence snapshot"),
        ):
            assert f"{label} {v.watermark[key]}" in normalize(detail.all_text)
        for c in v.conclusions:
            body = detail.text(f"conclusion-{c.conclusion_key}")
            for value in (c.body, c.author, c.certainty):
                assert normalize(value) in body
            assert ("citations invalid" in body) == (not c.citations_valid)
            assert ("disputed" in body) == (c.dispute_state == "disputed")
            for ref in c.evidence_refs:
                for key in ("evidence_id", "scope", "window_start", "window_end"):
                    assert normalize(ref[key]) in body
                assert ("not citable as fact" in body) == (
                    not all(r["citable_as_fact"] for r in c.evidence_refs)
                )
                assert (
                    f"/incidents/{incident}/evidence/{ref['evidence_id']}"
                    in detail.links
                )
            for dispute in c.disputes:
                disputed = detail.text("disputes")
                for key in (
                    "conclusion_key",
                    "reason",
                    "raised_by",
                    "raised_by_kind",
                    "raised_at",
                ):
                    assert normalize(dispute[key]) in disputed
    for entry in {k.entry_id for k in result.knowledge}:
        detail = h.page(f"/knowledge/{entry}")
        history = h.knowledge.knowledge_history(entry)
        assert detail.text("entry-generation") == str(history["generation"])
        active = [k for k in result.knowledge if k.entry_id == entry and k.retrievable]
        if active:
            assert detail.text("active-revision") == str(active[0].revision)
        else:
            assert detail.text("no-active") == "no active revision"
        revisions = [k for k in result.knowledge if k.entry_id == entry]
        assert len(detail.rows["revisions"]) == len(revisions)
        revisions_by_number = {k.revision: k for k in revisions}
        for cells in detail.rows["revisions"]:
            k = revisions_by_number[int(cells[0])]
            assert cells[:3] == [
                str(k.revision),
                k.state,
                normalize(k.name + " " + " ".join(k.tags)),
            ]
            assert (
                cells[3]
                == f"version {k.source_version} proposal {k.source_proposal_key}"
            )
            assert (
                f"/postmortems/{k.source_postmortem_id}/versions/{k.source_version}"
                in detail.links
            )
            assert (
                cells[4] == f"{k.approved_at} by {k.approved_by} ({k.approved_by_kind})"
            )
            assert cells[5] == str(k.supersedes_revision or "")
            assert cells[6] == (
                f"revoked {k.revoked_at} by {k.revoked_by} ({k.revoked_by_kind}): {k.revoked_reason}"
                if k.revoked_at
                else ""
            )
            assert cells[7] == normalize(k.content + "sha256 " + k.content_sha256)


def checked(h, incident, model, monkeypatch):
    """稳定代际边界内读取页面和投影；移动则重新读取，不要求跨对象原子。"""
    for _ in range(3):
        before = h.generations(incident)
        result = project(h, incident, model, monkeypatch)
        # 先采集断言结果，代际移动时不把混合读取误判为页面缺陷。
        error = None
        try:
            assert_records(h, incident, result, model)
            assert_pages(h, incident, result)
        except AssertionError as exc:
            error = exc
        if before == h.generations(incident):
            if error:
                raise error
            return result
    pytest.fail("页面比较期间代际持续变化，无法建立 D34 稳定边界")


def test_draft_contains_stimulus_and_bound_evidence(h, monkeypatch):
    """F13-1/2；D2/D11/D15/D26/R8/R9/R12/R14：实际内容、水位、引用且生成不发布。"""
    incident, ev, session, ending = h.seed()
    model = CountingModel(output(ev))
    view = h.generate(incident, model)
    assert view["status"] == "under_review"
    assert (
        h.knowledge.knowledge_from_postmortem(view["postmortem"]["postmortem_id"]) == []
    )
    result = checked(h, incident, model, monkeypatch)
    [version] = result.versions
    assert version.watermark["observation_session_id"] == session
    assert version.watermark["observation_ending_id"] == ending
    assert version.watermark["incident_control_generation"] == int(
        h.page(f"/incidents/{incident}").text("control-generation")
    )
    assert version.watermark["observation_generation"] == 1
    assert version.watermark["run_count"] == 1
    assert str(incident) in json.dumps(version.sections["incident"])
    assert version.sections["timeline"] and version.sections["runs"]
    assert HUMAN_NOTE in json.dumps(
        version.sections["human_actions"], ensure_ascii=False
    )
    assert "follow_up" in json.dumps(version.sections["human_actions"])
    recovery = json.dumps(version.sections["recovery"], ensure_ascii=False)
    assert "recovery_confirmed" in recovery
    assert "transition" in recovery
    recovery_refs = [
        ref for ref in version.evidence_catalog if ref["kind"] == "recovery"
    ]
    assert recovery_refs
    assert any(ref["scope"] == "recovery:error_ratio" for ref in recovery_refs)
    assert all(ref["citable_as_fact"] for ref in recovery_refs)
    assert all(":" in ref["evidence_id"] for ref in recovery_refs)
    conclusions = {c.conclusion_key: c for c in version.conclusions}
    for statement in output(ev)["narrative_sections"] + output(ev)["conclusions"]:
        assert conclusions[statement["key"]].body == statement["body"]
    finding = conclusions["impact"]
    assert finding.certainty == "supported" and finding.citations_valid
    catalog_ref = next(
        ref for ref in version.evidence_catalog if ref["evidence_id"] == ev
    )
    assert catalog_ref["kind"] == "investigation"
    assert catalog_ref["citable_as_fact"] is True
    assert catalog_ref["window_start"] == "2026-10-09T12:00:00Z"
    assert catalog_ref["window_end"] == "2026-10-09T12:05:00Z"
    expected_ref = {
        key: catalog_ref[key]
        for key in (
            "evidence_id",
            "scope",
            "window_start",
            "window_end",
            "citable_as_fact",
        )
    }
    assert finding.evidence_refs == (expected_ref,)
    assert conclusions["uncertainty"].certainty == "uncertain"
    assert "缺少" in conclusions["uncertainty"].body
    assert result.knowledge == ()
    assert result.recent_attempts[0]["status"] == "succeeded"
    assert all(a.action not in {"approve", "publish"} for a in result.review_actions)


@pytest.mark.parametrize("failure", ["provider", "output", "citations"])
def test_generation_failure_and_citation_failure_remain_distinct(
    h, monkeypatch, failure
):
    """F13-1/2；D2/D15/D22/D26/R9/R10：尝试失败与引用失败不同，均不可读取知识。"""
    incident, ev, _, _ = h.seed()
    model = CountingModel(
        output(ev, citation_failure=True)
        if failure == "citations"
        else ({"invalid": True} if failure == "output" else output(ev)),
        error=ModelError("MODEL_UNAVAILABLE") if failure == "provider" else None,
    )
    view = h.generate(incident, model)
    assert view["status"] == (
        "citations_failed" if failure == "citations" else "generation_failed"
    )
    assert len(model.calls) == (1 if failure == "provider" else 2)
    if failure == "citations":
        before = raw_records(h, incident)
        refusal = h.review(incident)
        assert refusal.status == 422
        assert refusal.json()["code"] == "ILLEGAL_TRANSITION"
        assert raw_records(h, incident) == before
    result = checked(h, incident, model, monkeypatch)
    assert result.knowledge == ()
    assert result.schedule["consecutive_failures"] == 1
    attempt = result.recent_attempts[0]
    if failure == "citations":
        assert attempt["status"] == "succeeded"
        assert result.versions[0].state == "draft"
        assert not result.versions[0].validation["citations_valid"]
        failed = [c for c in result.versions[0].conclusions if not c.citations_valid]
        assert failed and all(c.certainty == "uncertain" for c in failed)
    else:
        assert attempt["status"] == "failed"
        assert attempt["error_code"] == (
            "MODEL_UNAVAILABLE" if failure == "provider" else "OUTPUT_INVALID"
        )
        assert result.versions == ()


@pytest.mark.parametrize("state", ["disputed", "returned", "rejected", "stale"])
def test_untrusted_versions_refuse_approval_without_mutation(h, monkeypatch, state):
    """F13-2/3；D3/D5/D9/D16/D33/R10：HTTP 拒绝不改变代际、内容、审计或知识。"""
    incident, ev, _, _ = h.seed()
    model = CountingModel(output(ev, disputed=state == "disputed"))
    h.generate(incident, model)
    if state in {"returned", "rejected"}:
        response = h.review(
            incident,
            "return" if state == "returned" else "reject",
            reason="需要重新核对证据",
        )
        assert response.status == 200, response.text
    elif state == "stale":
        response = h.post(
            f"/incidents/{incident}/control",
            {
                "action": "follow_up",
                "expected_generation": h.page(f"/incidents/{incident}").text(
                    "control-generation"
                ),
                "idempotency_key": uuid4().hex,
                "text": "新增人工线索改变水位",
            },
        )
        assert response.status == 200, response.text
    before = raw_records(h, incident)
    refusal = h.review(incident)
    expected_status = 422
    expected_code = "DISPUTED" if state == "disputed" else "ILLEGAL_TRANSITION"
    assert refusal.status == expected_status
    assert refusal.json()["code"] == expected_code
    assert raw_records(h, incident) == before
    result = checked(h, incident, model, monkeypatch)
    assert result.generation_status == (
        "under_review" if state == "disputed" else state
    )
    assert result.knowledge == ()
    if state == "disputed":
        assert any(
            c.disputes and c.dispute_state == "disputed"
            for c in result.versions[0].conclusions
        )
    if state == "returned":
        assert result.schedule["pending_regeneration_version"] == 1
    if state == "stale":
        assert result.versions[0].stale_reason


def test_return_regenerates_new_read_only_version_with_reason(h, monkeypatch):
    """F13-1/2/3；D5/D14/D15：HTTP 退回后由 worker 产生 revises_version，原版保留。"""
    incident, ev, _, _ = h.seed()
    model = CountingModel(output(ev, disputed=True))
    h.generate(incident, model)
    old = raw_records(h, incident).view["postmortem"]["versions"][0]
    response = h.review(incident, "return", reason="补充对照窗口")
    assert response.status == 200, response.text
    # 模型争议仍在；退回不能清除，新的模型刺激在新版本里重新提出内容。
    model.payload = output(ev)
    h.generate(incident, model)
    result = checked(h, incident, model, monkeypatch)
    first, second = result.versions
    assert first.state == "returned" and first.content_sha256 == old["content_sha256"]
    assert any(c.disputes for c in first.conclusions)
    assert second.version == 2 and second.revises_version == 1
    assert second.state == "under_review"
    assert second.generation_record["return_reason"] == "补充对照窗口"
    assert result.knowledge == ()


def test_http_approval_records_reviewer_provenance_and_idempotency(h, monkeypatch):
    """F13-3；D5/D6/D9/D33/R8：测试驱动模拟审核，身份取认证主体、来源/哈希/时间保留。"""
    incident, ev, _, _ = h.seed()
    model = CountingModel(output(ev))
    view = h.generate(incident, model)["postmortem"]
    path = f"/postmortems/{view['postmortem_id']}/versions/1/review"
    fields = {
        "action": "approve",
        "expected_generation": str(view["generation"]),
        "idempotency_key": uuid4().hex,
        "actor_id": "forged-worker",
        "principal_kind": "worker",
    }
    before = raw_records(h, incident)
    assert h.post(path, fields, authenticated=False).status == 401
    assert raw_records(h, incident) == before
    response = h.post(path, fields)
    assert response.status == 200, response.text
    approved = raw_records(h, incident)
    replay = h.post(path, fields)
    assert replay.status == 200 and replay.json()["replayed"]
    assert raw_records(h, incident) == approved
    stale = h.post(path, {**fields, "idempotency_key": uuid4().hex})
    assert stale.status == 409
    assert raw_records(h, incident) == approved
    result = checked(h, incident, model, monkeypatch)
    assert result.generation_status == "approved"
    [revision] = result.knowledge
    assert revision.retrievable and revision.state == "active"
    assert revision.source_postmortem_id == result.postmortem_id
    assert (
        revision.source_version == 1
        and revision.source_proposal_key == "checkout-errors"
    )
    assert (revision.approved_by, revision.approved_by_kind) == (UI_USER, "basic_auth")
    assert revision.approved_at.tzinfo is not None
    assert (
        revision.content_sha256 == hashlib.sha256(revision.content.encode()).hexdigest()
    )
    approval = next(a for a in result.review_actions if a.action == "approve")
    publish = next(
        a
        for a in result.knowledge_actions
        if a.action in {"publish", "supersede"} and a.revision == revision.revision
    )
    assert publish.event_id == revision.approval_event_id
    assert (publish.actor_id, publish.principal_kind) == (UI_USER, "basic_auth")
    assert publish.object_id == revision.entry_id
    assert publish.revision == revision.revision
    entry_history = h.knowledge.knowledge_history(revision.entry_id)
    assert publish.resulting_generation == entry_history["generation"]
    assert publish.recorded_at.tzinfo is not None
    assert revision.approved_at >= result.versions[0].created_at
    assert (approval.actor_id, approval.principal_kind) == (
        publish.actor_id,
        publish.principal_kind,
    )


def test_http_supersede_and_revoke_preserve_immutable_history(h, monkeypatch):
    """F13-3；D3/D5/D25/D33/R15：合法人工输入驱动替换；撤销墓碑不删历史。"""
    incident, ev, _, _ = h.seed()
    model = CountingModel(output(ev))
    h.generate(incident, model)
    assert h.review(incident).status == 200
    before = raw_records(h, incident)
    [entry] = before.published
    entry_id = entry["entry_id"]
    response = h.post(
        f"/incidents/{incident}/control",
        {
            "action": "follow_up",
            "expected_generation": h.page(f"/incidents/{incident}").text(
                "control-generation"
            ),
            "idempotency_key": uuid4().hex,
            "text": "补充人工确认：请复核 checkout 错误窗口",
        },
    )
    assert response.status == 200, response.text
    view = h.generate(incident, model)["postmortem"]
    assert view["versions"][-1]["proposals"][0]["supersedes_entry_id"] == entry_id, (
        "worker 没有产生合法替换目标；不补造版本"
    )
    response = h.review(
        incident,
        "supersede",
        **{f"entry_generation:{entry_id}": str(before.entries[entry_id]["generation"])},
    )
    assert response.status == 200, response.text
    replaced = raw_records(h, incident)
    assert [r["state"] for r in replaced.entries[entry_id]["revisions"]] == [
        "superseded",
        "active",
    ]
    assert replaced.active[entry_id]["revision"] == 2
    # 在调用桩前完成全部 HTTP 链路，使场景搭建问题不会被桩掩盖。
    reason = "新证据推翻该条目的适用性"
    response = h.post(
        f"/knowledge/{entry_id}/revoke",
        {
            "revision": "2",
            "expected_generation": str(replaced.entries[entry_id]["generation"]),
            "idempotency_key": uuid4().hex,
            "reason": reason,
        },
    )
    assert response.status == 200, response.text
    assert h.knowledge.active_revision(entry_id) is None
    result = checked(h, incident, model, monkeypatch)
    old, revoked = result.knowledge
    assert (old.revision, old.state, old.retrievable) == (1, "superseded", False)
    assert (revoked.revision, revoked.state, revoked.retrievable) == (
        2,
        "revoked",
        False,
    )
    assert revoked.supersedes_revision == 1
    assert (revoked.revoked_reason, revoked.revoked_by, revoked.revoked_by_kind) == (
        reason,
        UI_USER,
        "basic_auth",
    )
    assert revoked.revoked_at.tzinfo is not None
    assert old.content == before.entries[entry_id]["revisions"][0]["content"]
    assert revoked.content == replaced.entries[entry_id]["revisions"][1]["content"]
    assert any(a.action == "supersede" for a in result.knowledge_actions)
    assert any(
        a.action == "revoke" and a.reason == reason for a in result.knowledge_actions
    )


def test_missing_record_is_not_generated(h, monkeypatch):
    """F13-1；D11/R10/R13：已存在事故但无复盘记录，明确 not_generated。"""
    incident, _, _, _ = h.seed(evidence=False, readings=False)
    model = CountingModel()
    result = checked(h, incident, model, monkeypatch)
    assert result.generation_status == "not_generated"
    assert result.versions == result.knowledge == result.review_actions == ()
    assert result.recent_attempts == ()


@pytest.mark.parametrize(
    "component",
    [
        "incident",
        "view",
        "attempt",
        "version",
        "conclusion",
        "proposal",
        "dispute",
        "audit",
        "knowledge",
    ],
)
def test_subject_mismatch_projects_no_partial_records(h, monkeypatch, component):
    """F13-1/2/3；D11/R13：外事故或嵌套主体错绑只能 unknown，不能投影部分记录。"""
    incident, ev, _, _ = h.seed()
    model = CountingModel(output(ev, disputed=component == "dispute"))
    h.generate(incident, model)
    if component == "knowledge":
        assert h.review(incident).status == 200
    records = copy.deepcopy(raw_records(h, incident))
    other = uuid4()
    if component == "incident":
        records = replace(records, incident_id=other)
    elif component == "view":
        records.view["incident_id"] = other
    elif component == "attempt":
        records.view["attempts"][0]["incident_id"] = other
    elif component == "knowledge":
        entry = next(iter(records.entries.values()))
        entry["revisions"][0]["source_postmortem_id"] = other
    elif component == "audit":
        records.postmortem_audit[0]["object_id"] = other
    else:
        version = records.view["postmortem"]["versions"][0]
        row = (
            version
            if component == "version"
            else (
                version["proposals"][0]
                if component == "proposal"
                else version["conclusions"][0]
            )
        )
        if component == "dispute":
            row = next(c for c in version["conclusions"] if c["disputes"])["disputes"][
                0
            ]
        row["postmortem_id"] = other
    before = len(model.calls)
    with monkeypatch.context() as patch:
        patch.setattr(tracing, "tracer", lambda: pytest.fail("错绑投影调用遥测"))
        try:
            result = postmortem_outcome(scenario(incident), records)
        finally:
            assert len(model.calls) == before
    assert result.generation_status == "unknown"
    assert result.unknown_reasons == ("SUBJECT_MISMATCH",)
    assert result.subject_id == str(incident)
    assert (
        result.postmortem_id
        is result.postmortem_generation
        is result.latest_version
        is result.schedule
        is None
    )
    assert (
        result.versions
        == result.recent_attempts
        == result.review_actions
        == result.knowledge
        == result.knowledge_actions
        == result.model_requests
        == ()
    )
