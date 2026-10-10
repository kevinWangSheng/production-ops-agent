"""F13 r6 独立场景辅助：临时库、真实工作台和自有计数模型。"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from uuid import UUID, uuid4

import psycopg

from opspilot.acceptance import IncidentScenario, PostmortemRecords
from opspilot.investigation.loop import ModelReply
from opspilot.knowledge.worker import PostmortemWorker
from tests.m1_web_support import basic, call, post_form, same_origin

T0 = datetime(2026, 10, 9, 12, 5, 7, 123456, tzinfo=timezone.utc)
START = (T0 - timedelta(minutes=5)).isoformat()
END = T0.isoformat()
HUMAN_NOTE = "人工核查：checkout 错误率升高，根因尚未确认"


class CountingModel:
    """独立计数，不读取产品请求计数，也不发送模型或遥测请求。"""

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def complete(self, request):
        self.calls.append(request)
        if self.error:
            raise self.error
        return ModelReply(
            json.dumps(self.payload, ensure_ascii=False),
            None,
            (),
            "stop",
            "deepseek-flash",
            {"prompt_tokens": 30, "completion_tokens": 20},
            {},
        )


def output(evidence_id, *, disputed=False, citation_failure=False):
    """测试作者冻结的最小合法模型输出。"""
    return {
        "narrative_sections": [
            {
                "key": "impact",
                "section": "impact_summary",
                "body": "checkout 错误影响请求；受影响用户数量未知。",
                "evidence_ids": [
                    "unknown-evidence" if citation_failure else evidence_id
                ],
            },
        ],
        "conclusions": [
            {
                "key": "uncertainty",
                "section": "hypotheses",
                "claim": "hypothesis",
                "body": "部署可能相关；缺少变更记录，根因未确认。",
                "evidence_ids": [evidence_id],
            }
        ],
        "proposals": [
            {
                "key": "checkout-errors",
                "name": "checkout 错误核查",
                "tags": ["checkout"],
                "symptoms": ["错误率异常"],
                "checks": ["核对错误窗口"],
                "evidence_ids": [evidence_id],
            }
        ],
        "disputes": [
            {"conclusion_key": "uncertainty", "reason": "对照窗口不足，结论有争议"}
        ]
        if disputed
        else [],
    }


@dataclass
class Harness:
    app: object
    knowledge: object
    jobs: object
    durable: object
    dsn: str
    events: object

    def seed(self, *, evidence=True, readings=True):
        """只造上游事故/观察证据；不写复盘、知识或生成调度表。"""
        incident = seed_upstream(self.dsn, ended_at=T0)
        response = self.post(
            f"/incidents/{incident}/control",
            {
                "action": "follow_up",
                "expected_generation": self.page(f"/incidents/{incident}").text(
                    "control-generation"
                ),
                "idempotency_key": uuid4().hex,
                "text": HUMAN_NOTE,
            },
        )
        assert response.status == 200, response.text
        with psycopg.connect(self.dsn) as conn:
            session = conn.execute(
                "SELECT session_id FROM opspilot_observation_sessions WHERE incident_id=%s",
                (incident,),
            ).fetchone()[0]
            ending = conn.execute(
                "SELECT ending_id FROM opspilot_observation_endings WHERE incident_id=%s",
                (incident,),
            ).fetchone()[0]
        ev = f"ev-{incident}" if evidence else None
        return incident, ev, session, ending

    def post(self, path, fields, *, authenticated=True):
        return post_form(
            self.app,
            path,
            fields,
            headers={
                **(basic() if authenticated else {}),
                **same_origin(),
                "accept": "application/json",
            },
        )

    def page(self, path):
        response = call(self.app, "GET", path, headers=basic())
        assert response.status == 200, response.text
        return Page(response.text)

    def generate(self, incident, model):
        before = len(model.calls)
        result = PostmortemWorker(
            knowledge=self.knowledge, jobs=self.jobs, model=model, events=self.events
        ).generate(incident)
        assert result is not None
        view = self.knowledge.incident_postmortem(incident)
        assert view["attempts"][0]["model_requests"] == len(model.calls) - before
        return view

    def review(self, incident, action="approve", *, reason=None, **extra):
        view = self.knowledge.incident_postmortem(incident)["postmortem"]
        fields = {
            "action": action,
            "expected_generation": str(view["generation"]),
            "idempotency_key": uuid4().hex,
            **extra,
        }
        if reason:
            fields["reason"] = reason
        return self.post(
            f"/postmortems/{view['postmortem_id']}/versions/{view['latest_version']}/review",
            fields,
        )

    def generations(self, incident):
        view = self.knowledge.incident_postmortem(incident)["postmortem"]
        if view is None:
            return None, {}
        return view["generation"], {
            r["entry_id"]: self.knowledge.knowledge_history(r["entry_id"])["generation"]
            for r in self.knowledge.knowledge_from_postmortem(view["postmortem_id"])
        }


def scenario(incident):
    return IncidentScenario("f13-independent", "F13", "1–3", "incident", str(incident))


def raw_records(h, incident):
    """按公开读接口组装错绑刺激；正常验收始终调用产品 postmortem_records。"""
    view = h.knowledge.incident_postmortem(incident)
    pm = view["postmortem"]
    published = (
        ()
        if pm is None
        else tuple(h.knowledge.knowledge_from_postmortem(pm["postmortem_id"]))
    )
    ids = {r["entry_id"] for r in published}
    return PostmortemRecords(
        incident,
        view,
        published,
        {i: h.knowledge.knowledge_history(i) for i in ids},
        {i: h.knowledge.active_revision(i) for i in ids},
        ()
        if pm is None
        else tuple(h.knowledge.audit_trail("postmortem", pm["postmortem_id"])),
        {i: tuple(h.knowledge.audit_trail("knowledge_entry", i)) for i in ids},
    )


class Page(HTMLParser):
    """保留单元格与元素文本，按字段比较真实 HTML，避免整页关键词匹配。"""

    def __init__(self, html):
        super().__init__()
        self.stack = []
        self.texts = {}
        self.rows = {}
        self.row_links = {}
        self.links = set()
        self.all_text = ""
        self.table = None
        self.row = None
        self.cell = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        ident = attrs.get("id")
        if ident:
            self.texts[ident] = ""
        if tag == "table":
            self.table = ident
            self.rows.setdefault(ident, [])
        if tag == "tr":
            self.row = []
            self.row_hrefs = []
        if tag == "td":
            self.cell = ""
        if tag == "a":
            self.links.add(attrs.get("href"))
            if self.row is not None:
                self.row_hrefs.append(attrs.get("href"))
        if tag == "br":
            self.handle_data(" ")
        if tag not in {"input", "br", "hr", "img", "meta", "link"}:
            self.stack.append((tag, ident))

    def handle_data(self, data):
        self.all_text += data
        for _, ident in self.stack:
            if ident:
                self.texts[ident] += data
        if self.cell is not None:
            self.cell += data

    def handle_endtag(self, tag):
        if tag == "td":
            self.row.append(normalize(self.cell))
            self.cell = None
        if tag == "tr" and self.row:
            self.rows[self.table].append(self.row)
            self.row_links.setdefault(self.table, []).append(self.row_hrefs)
        if tag == "table":
            self.table = None
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def text(self, ident):
        return normalize(self.texts[ident])


def normalize(value):
    return " ".join(str(value).split())


def seed_upstream(
    dsn: str, *, ended_at: datetime = T0, authorized: bool = False
) -> UUID:
    """插入无调查结论、Run 仍排队的事故及已结束观察，允许合法人工追问。

    只构造上游记录；后续控制、复盘生成和审核全部通过产品公开入口。
    """
    incident_id, target_id, run_id = uuid4(), uuid4(), uuid4()
    session_id = uuid4()
    with psycopg.connect(dsn) as conn:
        conn.execute(
            "INSERT INTO opspilot_targets(target_id, resource_uid) VALUES (%s, %s)",
            (target_id, f"uid-{target_id}"),
        )
        conn.execute(
            "INSERT INTO opspilot_target_suspensions(target_id) VALUES (%s)",
            (target_id,),
        )
        conn.execute(
            "INSERT INTO opspilot_incidents(incident_id,intake_key,state,lifecycle,target_id,observation_generation) VALUES (%s,%s,'investigating','resolved',%s,1)",
            (incident_id, f"key-{incident_id}", target_id),
        )
        conn.execute(
            "INSERT INTO opspilot_runs(run_id, incident_id, state, control_generation, "
            "budget_limit, deadline, versions) "
            "VALUES (%s, %s, 'queued', 0, 1, %s, '{}')",
            (run_id, incident_id, datetime.now(timezone.utc) + timedelta(hours=1)),
        )
        conn.execute(
            "UPDATE opspilot_incidents SET current_run_id=%s WHERE incident_id=%s",
            (run_id, incident_id),
        )
        conn.execute(
            "INSERT INTO opspilot_evidence(evidence_id, run_id, subject_id, status, adopted, "
            "raw, raw_sha256, view, view_sha256, projection_revision, observed_at, committed) "
            "VALUES (%s, %s, %s, 'ok', true, '\\x00', %s, %s, %s, 'p1', %s, true)",
            (
                f"ev-{incident_id}",
                str(run_id),
                str(incident_id),
                "0" * 64,
                json.dumps(
                    {
                        "evidence_id": f"ev-{incident_id}",
                        "status": "ok",
                        "adopted": True,
                        "citable_as_fact": True,
                        "tool": "metrics_query",
                        "target_id": "checkout",
                        "scope": "checkout",
                        "window_start": "2026-10-09T12:00:00Z",
                        "window_end": "2026-10-09T12:05:00Z",
                        "window": {
                            "start": "2026-10-09T12:00:00Z",
                            "end": "2026-10-09T12:05:00Z",
                        },
                        "content": [{"t": T0.isoformat(), "v": 0.4}],
                        "query": {
                            "expr": "rate(errors[5m])",
                        },
                    }
                ),
                "1" * 64,
                T0,
            ),
        )
        conn.execute(
            "INSERT INTO opspilot_observation_sessions(session_id, incident_id, purpose, "
            "target_id, target, subject_control_generation, observation_generation, "
            "authorized_by, state, ended_reason, authorized_global_generation, "
            "authorized_target_generation, deadline_at, max_samples, "
            "sample_interval_seconds, sustained_window_seconds) "
            "VALUES (%s, %s, 'incident_recovery', %s, %s, 0, 1, 'op', %s, %s, 0, 0, %s, "
            "5, 60, 60)",
            (
                session_id,
                incident_id,
                target_id,
                json.dumps(
                    {
                        "integration_id": "f13-lab",
                        "cluster_uid": "f13-cluster",
                        "namespace": "default",
                        "resource_uid": f"uid-{target_id}",
                        "revision": "fixture-v1",
                    }
                ),
                "authorized" if authorized else "completed",
                None if authorized else "recovery_confirmed",
                T0 + timedelta(hours=1),
            ),
        )
        for sequence in (1, 2):
            sample_id = uuid4()
            start = ended_at - timedelta(minutes=10 - sequence)
            conn.execute(
                "INSERT INTO opspilot_observation_samples(sample_id, session_id, job_id, "
                "sequence, epoch, window_start, window_end, outcome, required_signals_present, "
                "subject_control_generation, observation_generation, disposition, reason, "
                "confirms_health, health_basis, subject_lifecycle, incident_control_generation, "
                "incident_observation_generation, scope_suspended, global_generation, "
                "target_generation, within_deadline, lease_valid, lease_stamps_match, "
                "readings_consistent, transition) VALUES (%s,%s,%s,%s,1,%s,%s,'healthy',true,"
                "0,1,'adopted','adopted',true,'confirmed','observing_recovery',0,1,false,0,0,"
                "true,true,true,true,%s)",
                (
                    sample_id,
                    session_id,
                    uuid4(),
                    sequence,
                    start,
                    start + timedelta(minutes=5),
                    "recovery_confirmed" if sequence == 2 else None,
                ),
            )
            for signal in ("error_ratio", "request_rate_per_second"):
                conn.execute(
                    "INSERT INTO opspilot_observation_signal_readings(sample_id, signal_name, "
                    "status, value, sample_count, query, window_start, window_end, source) "
                    "VALUES (%s, %s, 'ok', 0.01, 5, 'q', %s, %s, 'prometheus')",
                    (sample_id, signal, start, start + timedelta(minutes=5)),
                )
        if not authorized:
            conn.execute(
                "INSERT INTO opspilot_observation_endings(ending_id, session_id, incident_id, "
                "ended_reason, transition, sample_id, lifecycle_before, lifecycle_after, "
                "recorded_at) VALUES (%s, %s, %s, 'recovery_confirmed', "
                "'recovery_confirmed', %s, 'observing_recovery', 'resolved', %s)",
                (uuid4(), session_id, incident_id, sample_id, ended_at),
            )
    return incident_id
