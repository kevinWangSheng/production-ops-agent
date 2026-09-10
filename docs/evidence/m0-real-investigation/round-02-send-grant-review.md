# M0 PG send grant 独立复验

日期：2026-09-10。独立验证者未参与实现，不继承实现者通过结论。

## 范围与实验合同

- 审查 PR #16 评论 [3976615675](https://github.com/kevinWangSheng/production-ops-agent/pull/16#discussion_r3976615675)，以及相邻 prepared/initiated 原子性、当前 Run/control generation/owner/epoch/lease/latest attempt 栅栏。
- 依据：SPEC 的证据真实性、人工控制、费用及实施门槛；C3 第 5/7/13 节；M0 计划第 3 工作包。只验证机制，不改变产品 passes 或 M0/M1 门槛。
- 环境：任务工作区 `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01`，父执行者已验证并启动的专属 PG `127.0.0.1:55431/m0_budget`。独立随机 experiment/subject/request；保留测试记录，不启动、停止 PG，不修改旧实验。
- 输入全部合成，模型/trace/外部业务请求为 0；不读取 `.env`、实际 reasoning 或 private wire。已有真实模型额度 20/20 不续用。
- 步骤：复现基线；审阅稳定修复；独立运行定向 PG 检查，并用可控替身验证无 grant/未 claimed 不获响应采纳、claimed 正常路径与直接 dispatch 兼容、失败原子性及栅栏。
- 判据：准备未发送的响应不能提交/发布；故障不得留下可冒充 direct dispatch 的记录；既有 control/attempt 栅栏无回退。失败保留并报告，不增加真实调用。

## 基线复现

基线 `aa485f951be319e576d5e18e12914ccd0ea84fde`；原评论已通过 `gh api repos/kevinWangSheng/production-ops-agent/pulls/comments/3976615675 --jq '{body,html_url,commit_id}'` 独立读取。

1. 实验 `3b26f226-ab8b-461c-ad1e-d1e75feb9b46`：新 Run -> accept -> claim -> prepare_request -> SQL 只查询 claimed -> commit_response（合成 assistant content `{}`）-> publish。实测 `claimed=False, accepted=True, published=True, http_calls=0`，确认原 P1。
2. 实验 `1596389a-7ce4-4ecc-80e8-57885bb596f9`：以实例级 dispatch 包装器在 dispatch 返回、prepare_request 插入 grant 之前插入确定性断点；同样提交并发布合成 `{}`。实测 `grant=None, accepted=True, published=True, http_calls=0`。该窗口不能仅靠“grant 存在时检查 claimed”消除；准备标识需要与 dispatch 原子持久化。已告知实现者及父执行者。

## 修复复验

结论：本次有界修复独立复验通过，未发现该变更范围内未解决的问题。覆盖版本为基线 `aa485f951be319e576d5e18e12914ccd0ea84fde` 上的稳定代码文件：

- `scripts/m0/step_store.py` SHA-256 `c9a1e82c1945152a94d28202fd0f1a91007d44b99fc813bef5d0870af2ac349e`。
- `tests/integration/test_m0_step_store_postgres.py` SHA-256 `421df91659793228d2b18641af1f1d22d9b46ecf0c69d45935919ca418b7e9a7`。
- 执行前后上述哈希一致；审查以此内容为准，不覆盖后续实质变更。

独立执行 `M0_STEP_POSTGRES=1 .venv/bin/python -m pytest tests/integration/test_m0_step_store_postgres.py -q`：**24 passed in 7.57s**。其中包含 grant 插入故障回滚（dispatch/grant/reserved 均为 0）、真实子进程 prepare 后退出 23 仍无法采纳、取消/发送序列化、lease、latest attempt 与正常 direct/claimed 路径。

另行编写并执行本记录末尾的独立 PG 探针：未 claimed 拒绝 commit/publish且占用保留；领取后可提交；分别错误的 Run/owner/epoch/generation 不可采纳或领取；较旧已 claimed 响应不能越过较新未 claimed attempt；实际 cancel、定向 lease 过期、新 Run 后旧响应不可采纳/发布；无 grant 的 direct dispatch 正常。全部 PASS，6 个随机实验 ID：`aaa0f3e5-cb0f-4107-b92e-ba109a55ba48`、`620fbedd-ac74-4a05-a718-bc5b668a9acc`、`b8a0b954-0dbd-4f1b-a9d0-41cac741f02d`、`f66e4e59-f512-4c49-b177-a6a302fa250d`、`4877893e-224e-45e8-a42b-01b22212b6a1`、`41371030-8949-46af-be83-a96392142318`。

静态核查：prepare 的 grant 与 step/dispatch/model_execution/预算共用一个 PostgreSQL 事务，准备路径不调用 starter；commit_response 先保留现有当前主体和执行/最新 attempt 条件，再拒绝存在而未 claimed 的 grant；send_guard 仍在同一主体 advisory lock 内一次性领取，校验输入哈希与当前 fence；publish 对未提交 response 给出固定拒绝，不再抛出 AttributeError。没有 schema 或真实传输行为变化。

### 证据边界与残余风险

`claimed=true` 只证明 transport 领取了一次性发送许可，**不证明 HTTP 已发送完成或响应确实来自供应商**。领取后、网络发起前中断仍可能保持 claimed；费用保持未知/占用的既有合同适用。本修复没有新增供应商真实性认证。direct dispatch 仍依赖受信任调用方遵守 starter 立即发起的既有合同，未把其改造成不可信调用方可用的网络证明接口。

现有旧记录不回写；旧版本两事务窗口曾留下的无 grant 记录与合法 direct dispatch 在旧 schema 下不可区分，本修复防止新 prepare 产生此窗口，不声称清洗旧记录。具体历史真实两请求兼容性另见下节。

本轮实际 PostgreSQL + 合成响应/可控发送替身，包含本地 loopback 故障检测但无 provider HTTP、无 trace 上传、无外部业务发送。未执行新的真实模型组合、soak 或完整产品验收；SPEC gate 和产品 passes 不变。

## 历史记录兼容性（只读）

按父执行者另行限定，查询历史实际组合 Run `5822fb34-c343-4085-aca5-6337ff2ad40d` 的 ID 和安全布尔值，未取出 response、input 或 reasoning 内容，也未重新提交旧 Run：

| request | step | claimed | response 存在 | execution 存在 |
|---|---|---|---|---|
| `05d69bd5-aa5c-441b-807f-47cd333e354f` | `605a54dc-c3ee-59d2-b0c3-53c1adc1d5c1` | true | true | true |
| `181b7fee-54f1-4e16-8d8b-cbdd43754165` | `9c3772d5-c630-5aa8-8eaa-ec3665bfc181` | true | true | true |

这是旧记录与 claimed 条件的静态兼容证据，不是新真实模型复验。查询仅投影 `s.id,d.request,g.claimed,s.response IS NOT NULL,e.request IS NOT NULL`，按该 Run UUID 限定。

## 独立补充探针（可复现）

将下列脚本保存到临时文件，在上述工作区以 `PYTHONPATH=. .venv/bin/python <临时文件>` 执行；仅在已授权专属 PG 存活时运行，不负责环境生命周期。

```python
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from scripts.m0.budget import PostgresBudget
from scripts.m0.contracts import BudgetError, RunContext
from scripts.m0.postgres_lab import DSN
from scripts.m0.step_store import StepStore, digest

V = {"state": "v3", "provider": "deepseek", "tool": "independent-fixture-v1"}
PAYLOAD = {"role": "assistant", "content": "{}"}
ids = []


def setup():
    l = PostgresBudget(DSN)
    s = StepStore(l)
    r = RunContext(
        uuid4(), uuid4(), "deepseek", datetime.now(timezone.utc) + timedelta(minutes=4)
    )
    l.initialize(r.experiment_id, 100, r.deadline)
    subject = s.accept(r, "send-grant-independent", {"synthetic": True}, V)
    f = s.claim(subject, r, uuid4(), V)
    ids.append(str(r.experiment_id))
    return l, s, r, subject, f


def denied(fn, code):
    try:
        fn()
    except BudgetError as e:
        assert str(e) == code, (str(e), code)
    else:
        raise AssertionError("unexpected acceptance")


def claim(s, f, rid):
    with s.send_guard(f, rid, input_hash=digest({})) as (release, check):
        check()
        release()


l, s, r, subject, f = setup()
rid = uuid4()
step = s.prepare_request(f, "unclaimed", 0, {}, rid, 10, 4)
denied(
    lambda: s.commit_response(f, step, PAYLOAD, request_id=rid),
    "MODEL_REQUEST_NOT_INITIATED",
)
denied(lambda: s.publish(f, {}, step=step), "UNCOMMITTED_CANDIDATE")
assert s.rebuild(f, step)["status"] == "retry_model"
assert l.snapshot(r.experiment_id)["reserved"] == 10
claim(s, f, rid)
for wrong in [
    replace(f, owner=uuid4()),
    replace(f, epoch=f.epoch + 1),
    replace(f, generation=f.generation + 1),
    replace(f, run=replace(r, run_id=uuid4())),
]:
    assert s.commit_response(wrong, step, PAYLOAD, request_id=rid) is False
    denied(lambda: claim(s, wrong, rid), "CONTROL_DENIED")
assert s.commit_response(f, step, PAYLOAD, request_id=rid)
assert s.publish(f, {}, step=step)
print("unclaimed + claimed + independently stale Run/owner/epoch/generation PASS")

l, s, r, subject, f = setup()
old = uuid4()
new = uuid4()
step = s.prepare_request(f, "latest", 0, {}, old, 10, 4)
claim(s, f, old)
s.prepare_request(f, "latest", 0, {}, new, 10, 4)
denied(
    lambda: s.commit_response(f, step, PAYLOAD, request_id=old), "UNKNOWN_MODEL_ATTEMPT"
)
denied(
    lambda: s.commit_response(f, step, PAYLOAD, request_id=new),
    "MODEL_REQUEST_NOT_INITIATED",
)
claim(s, f, new)
assert s.commit_response(f, step, PAYLOAD, request_id=new)
print("late claimed response cannot override newer unclaimed attempt PASS")

for action in ["cancel", "lease", "new_run"]:
    l, s, r, subject, f = setup()
    rid = uuid4()
    step = s.prepare_request(f, "control", 0, {}, rid, 10, 4)
    claim(s, f, rid)
    if action == "cancel":
        s.control(subject, 0, "cancel")
    elif action == "new_run":
        s.new_run(subject, 0, replace(r, run_id=uuid4()), {"synthetic": True}, V)
    else:
        with l._transaction() as c:
            c.execute(
                "UPDATE m0_v3_subject SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",
                (subject,),
            )
    assert s.commit_response(f, step, PAYLOAD, request_id=rid) is False
    assert s.publish(f, {}, step=step) is False
    assert l.snapshot(r.experiment_id)["reserved"] == 10
print("cancel + expired lease + actual new Run reject late claimed responses PASS")

l, s, r, subject, f = setup()
rid = uuid4()
calls = []
step, _ = s.dispatch(
    f, "direct", 0, {}, rid, 10, 4, lambda: calls.append("controlled initiation")
)
assert len(calls) == 1
with l._transaction() as c:
    assert (
        c.execute(
            "SELECT claimed FROM m0_v3_send_grant WHERE request=%s", (rid,)
        ).fetchone()
        is None
    )
assert s.commit_response(f, step, PAYLOAD, request_id=rid)
assert s.publish(f, {}, step=step)
print("direct dispatch without grant preserved PASS")
print({"synthetic_experiments": ids, "provider_http": 0, "trace_uploads": 0})
```

## 补充：适配器版本语义与旧 pending 记录

父执行者追加同组有界范围：将实际 probe 的 adapter 从 `pg-private-pipe-v2-content-normalization` 升为 `pg-private-pipe-v3-atomic-send-grant`，防止旧 pending 记录被解释为新版原子发送语义。只改版本标识，未迁移或回写旧数据，不新增平台机制。

覆盖 `scripts/m0_pg_live_probe.py` SHA-256 `1ccf6255f51f65752c86d3a7ae7eb80177b4bcdf4fe4498ed6daaf543c2fc60f`，测试前后哈希一致；此前 step_store 和 PG tests 的哈希也未变化。

独立静态核查：`execute(first)` 的 `store.accept` 与 `store.claim` 均传当前 `VERSIONS`；`execute` 接续分支同样传此字典给 claim。`StepStore.accept` 对重用 intake 的版本不一致返回 `IDENTITY_CONFLICT`；`StepStore.claim` 在主体仍可领取且租约不活跃的前提下，发现持久 versions 不匹配即持久化 blocked 并返回 `INCOMPATIBLE_STATE`。此门槛已有先前 24 项 PG 测试中的版本不兼容覆盖，不是本次新增数据库验证。

接续入口原有 code_hashes/profile_hash 检查本来已会拒绝不匹配的记录，此次语义版本升级补充一致性标识，不应说成首次引入旧代码拒绝能力。符合 C3/ADR-0003 不兼容协议状态阻塞、保留业务历史的合同；不从旧记录猜测或补造 claimed。

独立运行 `.venv/bin/python -m pytest tests/test_m0_pg_live_probe.py -q`：**10 passed in 1.63s**。另用下方离线探针实际调用 first/resume 到受控 claim 中断，观察到 accept/first claim/resume claim 三处都收到新版 adapter；均在 request 之前停止，`request_calls=0, pg_connections=0`。本补充没有启动或连接 PG。

上述是静态/离线入口证据，与前节旧真实 Run 的只读 claimed 查询分别成立：旧两请求 claimed=true 不代表旧 Run 获得新版续跑授权，也不代表新版完成了真实模型组合验证。

```python
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
import tempfile, json, time
from scripts import m0_pg_live_probe as p
from scripts.m0.contracts import BudgetError

seen = []


class Store:
    def __init__(self, ledger):
        pass

    def install(self):
        pass

    def accept(self, run, key, initial, versions):
        seen.append(("accept", versions.copy()))
        return uuid4()

    def claim(self, subject, run, owner, versions, **kw):
        seen.append(("claim", versions.copy()))
        raise BudgetError("INCOMPATIBLE_STATE")


l = SimpleNamespace(
    _transaction=lambda: nullcontext(SimpleNamespace(execute=lambda *a: None)),
    initialize=lambda *a: None,
)
with (
    tempfile.TemporaryDirectory() as d,
    patch.object(p, "StepStore", Store),
    patch.object(p, "PostgresBudget", lambda *a: l),
    patch.object(p, "code_hashes", lambda: {}),
    patch.object(p.round02, "save", lambda *a: None),
    patch.object(
        p, "request", side_effect=AssertionError("network must not be reached")
    ),
    patch.object(
        p.round02,
        "PROFILE",
        SimpleNamespace(
            deadline=time.time() + 300, provider_models_response_sha256="synthetic"
        ),
    ),
    patch.object(p.round02, "asdict", lambda *a: {}),
):
    path = Path(d) / "record.json"
    for stage in ["first", "resume"]:
        if stage == "resume":
            path.write_text(
                json.dumps(
                    {
                        "code_hashes": {},
                        "profile_hash": p.round02.canonical_hash({}),
                        "status": "first_committed",
                        "first_pid": -1,
                        "experiment_id": str(uuid4()),
                        "run_id": str(uuid4()),
                        "subject_id": str(uuid4()),
                        "deadline": time.time() + 300,
                    }
                )
            )
        try:
            p.execute(stage, path)
        except BudgetError as e:
            assert str(e) == "INCOMPATIBLE_STATE"
        else:
            raise AssertionError("must stop at claim")
assert [a for a, v in seen] == ["accept", "claim", "claim"]
assert all(v == p.VERSIONS for a, v in seen)
assert p.VERSIONS["adapter"] != "pg-private-pipe-v2-content-normalization"
print(
    {
        "first_accept_first_claim_resume_claim": seen,
        "request_calls": 0,
        "pg_connections": 0,
    }
)
```

父执行者仅按仓库Ruff规则格式化两段Python示例；格式化前后AST相同，审查结论和受审代码未改。原文保留`tmp/m002-send-grant-review-before-format.md`，SHA256 a161309423ec317cac50ad737aa00555de670cb7443d19fd80c9892284c0fbf2；初次全量检查的格式失败输出保留`tmp/m002-send-grant-final-check.txt`。
