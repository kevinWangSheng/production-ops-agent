# M0 当前 final 指针独立复验

日期：2026-09-10。独立验证者未参与实现；范围为 PR #16 评论 [3976875093](https://github.com/kevinWangSheng/production-ops-agent/pull/16#discussion_r3976875093) 的发布后人工控制失效问题，不扩展产品范围。

## 实验合同

依据 SPEC 证据/人工控制/运行预算/实施门槛，以及 C3 人工控制与结果所有权、PostgreSQL 业务恢复权威合同。父执行者已启动并核对原专属 PG 55431；本验证只创建随机合成实验，保留记录，不触碰旧真实行、不自行起停，不读取 `.env` 或实际私有协议。模型/trace/外部业务请求为 0。

基线 `6eab0a4b139a130afde98f4105c39e1d15c145a8`。步骤：独立复现 publish 后 cancel/correct 仍 published=true；等待实现稳定哈希，再验证当前 final 清空、历史 report payload/hash/generation/时间保持、旧 fence 无法发布或覆盖、新 Run 合法接续，以及已有 send grant 门槛不回退。仅真实 PG + 合成响应机制验证，不是新真实模型实验或产品验收。失败保留并报告，不增加调用。

## 基线与复验

独立读取 GitHub 原评论：`gh api repos/kevinWangSheng/production-ops-agent/pulls/comments/3976875093 --jq '{body,html_url,commit_id}'`。

基线真实 PG 独立复现（无 HTTP）：

- 实验 `6744a485-8e7e-4c6c-be1d-66cfe0450812`：新 Run/claim -> dispatch 合成响应 -> commit_response -> publish -> control(cancel)。结果 state=cancelled、generation=1、published=true。
- 实验 `2bb9f655-f755-48f1-b9da-605901f19917`：同链路 control(correct)。结果 state=waiting_human、generation=1、published=true。

因此确认原 P1：新人工控制已取代结论，但当前指针仍被 summary 暴露为已发布。

## 稳定修复独立复验

结论：该有界修复独立通过，未发现范围内未解决问题。测试前后哈希一致：

- `scripts/m0/step_store.py` SHA-256 `9e865752d0d4acfc0dcd7c41e81175e944cbc99b0f87d199cbd5cd52c1aed599`。
- `tests/integration/test_m0_step_store_postgres.py` SHA-256 `35062d86556434e506ed37e954f6ee8283ffec7fa9de30dd041db86b165cbf19`。

独立运行 `M0_STEP_POSTGRES=1 .venv/bin/python -m pytest tests/integration/test_m0_step_store_postgres.py -q`：**26 passed in 7.63s**，包括已发布后 cancel/correct 的回归、send grant 未领取拒绝/写入故障原子回滚/进程退出/最新 attempt、控制及租约栅栏等既有检查。

另行独立探针（下附）使用 prepared + claimed 的合成响应路径，并验证：

1. 发布与同候选重发在控制前可幂等成功。
2. cancel/correct 后 final=NULL，published=false，状态分别为 cancelled/waiting_human，generation=1。
3. 原报告 candidate、SHA-256、generation=0、published_at 均不变。
4. 旧 fence 既不能重发原候选，也不能写入不同候选或响应；旧 expected_generation 的人工操作返回 CONTROL_CONFLICT。
5. 显式新 Run 使用 generation=2，可完成新的响应提交和发布；旧报告仍逐字段相同，实验累计占用仍为 20，未因新 Run 重置。

cancel 实验 `2def2a68-0e1f-4e32-8431-0c863108f25e`、correct 实验 `3b20e1da-b494-448b-86a0-dc3d7ff05fc8`，均 PASS。合成历史 candidate 哈希均为 `531cbd6d2054b7fe3b2d7ba0e38a80f00602181d64c0193df7fc1d5d542c7720`。

静态差异只有 control 的条件 UPDATE 增加 `final=NULL`；沿用主体 advisory lock 和事务，使控制 generation/state/当前指针一起更新，未修改历史 m0_v3_report 或扩大执行权。publish 的既有当前 fence 和终态重发门槛未变。该修改符合 C3 人工控制优先和历史保留合同。

## 边界

这是随机合成记录上的实际 PostgreSQL 机制验证；无模型、trace 或外部业务发送，未读/改旧真实记录，未启动/停止 PG，未改实现或提交。PG 检查完成后已通知父执行者可协调停止。

该补丁作用于今后的成功 control 调用，不是旧数据迁移；不声称已追溯清除旧实验留下的 stale final。真实模型、完整调查质量、其他 bridge/schema 工件、全生命周期验收不在本独立审查范围，SPEC gate 和产品 passes 不变。

## 独立探针

在专属 PG 已获授权启动且身份已核对时，将脚本保存到临时文件，在任务工作区运行 `PYTHONPATH=. .venv/bin/python <临时文件>`。下文仅做 Ruff 格式化后留存，不改变探针行为。

```python
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from scripts.m0.budget import PostgresBudget
from scripts.m0.contracts import BudgetError, RunContext
from scripts.m0.postgres_lab import DSN
from scripts.m0.step_store import StepStore, digest

V = {"state": "v3", "provider": "deepseek", "tool": "independent-fixture-v1"}
for action in ["cancel", "correct"]:
    l = PostgresBudget(DSN)
    s = StepStore(l)
    r = RunContext(
        uuid4(), uuid4(), "deepseek", datetime.now(timezone.utc) + timedelta(minutes=4)
    )
    l.initialize(r.experiment_id, 100, r.deadline)
    subject = s.accept(r, "final-pointer-independent", {"synthetic": True}, V)
    f = s.claim(subject, r, uuid4(), V)
    candidate = {"synthetic": True, "claim": "old"}
    rid = uuid4()
    step = s.prepare_request(f, "final", 0, {}, rid, 10, 4)
    with s.send_guard(f, rid, input_hash=digest({})) as (release, check):
        check()
        release()
    assert s.commit_response(
        f, step, {"role": "assistant", "content": json.dumps(candidate)}, request_id=rid
    )
    assert s.publish(f, candidate, step=step)
    assert s.publish(f, candidate, step=step)

    def history():
        with l._transaction() as c:
            return c.execute(
                "SELECT candidate,generation,published_at FROM m0_v3_report WHERE run_id=%s",
                (r.run_id,),
            ).fetchone()

    before = history()
    h = digest(before["candidate"])
    assert s.control(subject, 0, action, payload={"synthetic_human": True}) == 1
    assert s.summary(subject)["state"]["published"] is False
    with l._transaction() as c:
        current = c.execute(
            "SELECT final,state,generation FROM m0_v3_subject WHERE id=%s", (subject,)
        ).fetchone()
    assert current == {
        "final": None,
        "state": "cancelled" if action == "cancel" else "waiting_human",
        "generation": 1,
    }
    assert history() == before and digest(history()["candidate"]) == h
    for old_candidate in [candidate, {"synthetic": True, "claim": "overwrite"}]:
        assert s.publish(f, old_candidate, step=step) is False
    assert (
        s.commit_response(
            f, step, {"role": "assistant", "content": "{}"}, request_id=rid
        )
        is False
    )
    try:
        s.control(subject, 0, "cancel")
    except BudgetError as e:
        assert str(e) == "CONTROL_CONFLICT"
    else:
        raise AssertionError("stale human generation accepted")
    fresh = replace(r, run_id=uuid4())
    assert s.new_run(subject, 1, fresh, {"synthetic": "new"}, V) == 2
    current_f = s.claim(subject, fresh, uuid4(), V)
    assert not s.publish(f, candidate, step=step)
    new_rid = uuid4()
    new_step, _ = s.dispatch(current_f, "final", 0, {}, new_rid, 10, 4, lambda: None)
    new_candidate = {"synthetic": True, "claim": "new"}
    assert s.commit_response(
        current_f,
        new_step,
        {"role": "assistant", "content": json.dumps(new_candidate)},
        request_id=new_rid,
    )
    assert s.publish(current_f, new_candidate, step=new_step)
    assert s.summary(subject)["state"]["published"] is True
    assert history() == before
    assert l.snapshot(r.experiment_id)["reserved"] == 20
    print(
        {
            "action": action,
            "experiment": str(r.experiment_id),
            "historical_payload_hash": h,
            "historical_generation": before["generation"],
            "history_timestamp_unchanged": True,
            "old_fence_denied": True,
            "new_run_published": True,
            "provider_http": 0,
        }
    )
```
