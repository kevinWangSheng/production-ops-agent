# Flash normal-1 单次实验合同

冻结于 2026-09-09，执行前记录。沿用 [M0-01 任务](../../tasks/2026-09-08-m0-01-preflight.md)、M0 §1/2/6/7、C3 §5/11/12 与 SPEC 当前门槛；仅验证兼容链路，F1/F7/F8/F14 全部验收状态不变。

## 授权与范围

用户本轮明确：“授权: 涉及费用问题直接授权. 本轮不会产生多大费用,无需人审核”。此为本轮新的费用授权，覆盖下列有界单次实验；不复用原 Pro 的批准文件、实验身份、已占用 2 CNY，不授权自动合并 PR。

- 费用上限新增 **2.00 CNY**；原 2.00 CNY 未核账继续保留。新批准引用、experiment UUID、run UUID 独立生成；固定 v2 合同、当前代码/锁/fixture digest、实际 runtime、现有账户身份及 key 指纹，0600 私有文件；凭据仅留在主仓库 `.env`。
- 绝对截止 **2026-09-09T15:00:00+00:00**（洛杉矶 08:00）；单次有效 HTTP 运行期限 420 秒，模型请求各最多 180 秒，其他 HTTP 各最多 10 秒；数据库和关闭另受既有有界超时约束。
- 唯一用例为现有 `tests/fixtures/m0/protocol-v1.json` normal，执行一次。直接 `deepseek-v4-flash`，thinking enabled/high；接受响应报告相同 alias，不宣称固定模型权重。
- 至多 2 次非流式模型 POST，每次 `max_tokens=4096`，请求 16 KiB、响应 128 KiB；固定一次本地 `read_fixture`，只准 `m0-target-a`。
- 现有 US 默认 workspace/default 项目；不新建、不启用付费套餐/自动化。live 内最多 1 次项目 GET、1 次 trace POST、3 次 trace GET（仅 404 可继续）；无自动模型重试、重复上传、重定向、环境代理或自动 tracing。
- live 外只读前置核对：1 次模型目录、1 次余额、1 次现有项目元数据、1 次账户 settings；执行后至多 1 次余额读取。必要故障诊断可再有至多 3 次同 Run trace GET 和 2 次账户/项目元数据 GET，仍在绝对截止前，不增加模型或上传次数。UI 仅检查套餐/用量及现有项目自动化配置。

## 费用依据与前提

当日读取 [DeepSeek 官方人民币价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)：Flash 峰值 cache-miss 输入 3 元/M、输出 9 元/M，空闲减半。每请求按 32768 输入 tokens（体积限制下含结构裕量的保守工程假设）、4096 输出估算，两轮上界 `(32768*3+4096*9)*2/1e6=0.270336 CNY`。不是供应商硬账单上限证明。平台预留 0.10 CNY，余量覆盖估算不确定性；若发现前提不成立则停止。

[LangSmith 官方价格](https://www.langchain.com/pricing) Developer 包含每月 5000 base traces；本轮首次页面读取显示旧值 0/5000；刷新后浏览器扩展读取超时，改用原生浏览器 UI 确认当前 Developer Free、1/5000，显示 Add card to remove trace limit。只读 API 的模型目录、CNY 余额、项目及 settings 均 HTTP 200，项目 UUID/name/default tenant 与预期匹配，不把延迟用量当最终账单。一次 base trace 预期在免费额度内；不开 evaluator、automation 或订阅。费用估算、账户余额变化与可归属实际账单分别报告；未知费用不释放占用。

## 执行版本与数据库

复用 `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01`，从干净 `ddf39dd` 快进已合并 main `55539bc3262260840c2f6a92201c1478c8443255`，新分支 `chore/m0-01-flash-live`。运行源码/锁不改；CPython 3.12.13，实际锁依赖闭包校验通过；OpenAI 3.10.0、HTTPX2 2.12.0、LangSmith 0.12.2，全部依赖以 uv.lock 为准。

独立 PostgreSQL 17.9（Homebrew）位于本 worktree 的 `tmp/m0-b/postgres`，loopback 55431，database `m0_budget` / user `m0_lab`。现有 `public.m0_live_once` 与 `public.m0_live_diagnostics` 已可查询；这是专属实验实例内既有 schema，不是生产隔离证明，无新增 schema/迁移。系统 PostgreSQL 另行运行，不触碰。执行前保存既有行哈希，结束后逐行核对；保留数据库、旧证据和批准文件。

## 完成判据及失败处置

必须同时满足：首轮真实 Flash 返回有效 `read_fixture(target=m0-target-a)`；工具结果配对续接；第二轮 JSON 精确返回 target `m0-target-a` / evidence_id `m0-evidence-a`；PostgreSQL 提交 business completed、业务/outbox 和固定安全诊断；LangSmith 白名单 DTO 上传且正确项目回读一致，终态 TRACE_VERIFIED。

采用现有 CLI：`.venv/bin/python -m scripts.m0 live --env-file /Users/shenghuikevin/dev/AI/production-ops-agent/.env --approval-file <本轮新0600批准文件绝对路径>`。不存储/公开 provider reasoning；它仅同进程同 Run 回传同供应商。业务失败不取得上传权；trace 失败保留业务已提交事实，继续限额内只读诊断，原失败不改写，不重复 live。

收尾保存 CLI、DB 安全投影、时间、次数、usage、版本/digest、费用边界及原实验不变证明；独立审查证据后提交 PR，等待最新 CI 和已触发 review，处理发现，不自动合并。停止专属 PG，保留数据和私有证据。一次 normal 成功不代表完整 M0、故障恢复、正式 eval 或产品验收。

执行前补记：原生 UI 已确认 default 项目 Automations 为 No automations found，Evaluators 表没有数据行；模型目录、余额、项目及 settings 四次只读 GET 均 200。独立 Agent `flash_review` 静态审查无阻塞，独立 66 项离线 live 测试通过。新身份及版本见 [版本清单](flash-version.json)。
