# PR review gate 独立实现审查

日期：2026-09-09。审查者为未参与设计/实现的独立 Agent；仅修改本报告，未发布状态、修改保护、触发机器人或合并 PR。

## 结论

**P1 已在本地修复并独立复验；当前没有未处置的实现阻断发现。** 发布器现更新同一 check-run，下面保留初次审查的状态额度问题及复现历史。真实主线 Checks API 发布和保护阻断尚未验证，不能将本地结论标为部署完成。

## P1 — 无变化轮询也持续追加状态，约两天耗尽 SHA/context 上限

位置：`scripts/review_gate.py:218-219`、`:235-236`，`.github/workflows/review-gate.yml` 的每 5 分钟 schedule。

每次 evaluate 无条件先写 pending，再写最终状态。官方 [Create a commit status](https://docs.github.com/en/rest/commits/statuses#create-a-commit-status) 明确每个 repository/sha/context 最多创建 1000 条状态，超过返回 validation error。没有其他事件时，500 次正常 reconcile 即达到上限，名义频率约 41 小时 40 分钟；评论和其他 PR 事件还会使全部 open PR 被重新发布。

本地合成反例复用了真实 `evaluate` 和测试的有效证据，仅替换 API/snapshot 和有 1000 次上限的 status transport：500 次 READY 后移除机器人汇总，再次 evaluate 在初始 pending 写入处失败；证据判定已为 SUMMARY_MISSING_OR_AMBIGUOUS，但最后已发布状态仍为 success/READY。观察输出：

```json
{"successful_cycles":500,"writes":1000,"new_evidence":"SUMMARY_MISSING_OR_AMBIGUOUS","last_published_status":["success","READY"],"next_run":"API_UNAVAILABLE"}
```

若额度耗尽时最后状态为 failure/pending，则同一 SHA 后续审查和讨论全部完成也无法恢复。这会影响等待人工审核数天的普通 PR，无需恶意输入。

建议：采用能够更新同一对象的 check-run，或设计经审查的持久状态/证据去重及明确的额度处理；仅降低轮询频率不能消除硬上限。修改后覆盖超过 500 次相同证据 reconcile、证据变化和失败恢复，并重新检查 fail-closed 与发布竞争边界。

## 已执行核查

- 完整阅读 AGENTS/SPEC 及 ROADMAP、任务、开发指南和三个实现文件；此变更是开发工程门禁，不是 SPEC 排除的产品发布权限。
- `.venv/bin/python -m pytest tests/test_review_gate.py -q`：37 passed，0.02s。
- `python3 -I scripts/review_gate.py --pr 12`：真实只读 REST/GraphQL 查询返回 HEAD `70850a2357ac3236edc30d77711549d873495b8d`、`UNRESOLVED_THREADS`。没有使用 --publish。
- 只读查询 #12 bot 汇总：实际机器人 ID 199175422，实际 top-level metadata/表格结构与 parser 预期一致；该汇总绑定 `3309daf5b47ed058e50b3eb9605dab491315643c`，不能充当当前 HEAD 通过证据。
- 静态核查工作流只 checkout main、不执行 PR 文件/依赖、不插值 PR 文本入 shell，权限为 contents/read、pull-requests/read、statuses/write；发布入口检查 repository/workflow_ref。它依赖仓库写权限持有者可信，不证明不同 Actions workflow 之间身份隔离。
- 全分页线程、outdated 未解决线程、身份冒充、未知/重复汇总、HEAD/快照变化拒绝有合成测试；最后一次读与 status 写仍非事务，native conversation resolution 仍是线程重开的服务器控制。

## 证据范围与剩余验证

本报告的 1000 次上限复现是合成 transport，官方文档证明服务硬上限；未实际向 GitHub 写 1000 个状态。37 tests 是本地合成检查；#12 是当前真实 API 的拒绝路径。未执行主线受信 workflow、成功发布、必需检查绑定或真实合并阻断，不得据本报告宣称门禁已启用。

自动更新/手动复审后厂商是否更新汇总仍须真实验证；严格 parser 在厂商格式变化时会拒绝，需要明确恢复流程。bootstrap 须由用户审核合并工作流后验证真实发布，再追加 required check，不可伪造 bootstrap 成功。待最终任务文档补齐已确认部署顺序、可信边界和故障局限。

## 受检版本

HEAD：`ad99e6abd30afdbc1e57a007afaab69dd3e80203`，三个实现文件均为未提交新增。

- scripts/review_gate.py SHA256：`bcfc01bfb1321ce9d028480d192ebb614130ef0a0b12a10ba21f60561c108b1f`
- tests/test_review_gate.py SHA256：`fd819dbcf435f4c535eaf4aa02ba9e03e76864076e55a843cd7e091018105a8f`
- .github/workflows/review-gate.yml SHA256：`b9171a918e5936dc232ed5c10e3c3ead77735f2347e87283b6cebf11b9dbd80a`


## Checks API 修复复验（2026-09-09）

P1 关闭于本地实现层：检查发布端改为分页读取同 SHA、同 name、Actions app ID 15368 的唯一 check-run；首次 POST 创建，后续 PATCH 同一 id。工作流权限改为 `checks: write`。同 app/name 多个对象明确拒绝，旧 commit-status 追加路径已移除。

独立复验命令 `.venv/bin/python -m pytest tests/test_review_gate.py -q`：**40 passed，0.02s**。包含真实 #11 汇总的合成 PR 重放；不是 #11 原事故的就绪判断。

另以有状态的本地 API transport 运行真实 evaluate/status：1001 次 READY 后删除 summary，观察 failure，再恢复 summary，观察 success。共 1003 轮、1 次 POST、2005 次 PATCH，始终只有 1 个 check-run 对象，证明原历史增长反例已消除且证据变化仍能改变结论。此 transport 未模拟 GitHub 内部生命周期规则，不替代真实 API 验证。

只读核对 [Checks API 文档](https://docs.github.com/en/rest/checks/runs#update-a-check-run)：使用的 POST/PATCH、name/head_sha/external_id/status/conclusion/output 字段与权限一致；in_progress 更新没有发送 conclusion，completed 更新发送 success/failure。真实读取 #12 当前 HEAD 的 checks 返回分页对象中的 check_runs 数组，两个现有 Actions runs 的 app ID 均为 15368。首次辅助命令组合 `--slurp --jq` 被 gh 拒绝，随后改为 `--slurp | python3` 成功；实现本身未使用该不兼容组合。

首次部署的真实验证仍必须覆盖：创建 check-run；completed/success -> in_progress 时服务器确实撤销可合并状态；随后 failure 与恢复 success；跨 workflow run PATCH 同 id；fork PR 的 SHA 与 required check 关联；app 15368 绑定和 native conversation resolution。官方字段协议与合成 PATCH 测试不能证明这些服务器行为已经发生。现有仓库写权限可信边界、非事务最后读写窗口、故障不能持续实时撤绿的已声明边界保持不变。

复验版本 SHA256：

- scripts/review_gate.py：`00698e88c6ed200fa89c46ee79738e749ce1195cf3546fa3ecc31b812545fbe5`
- tests/test_review_gate.py：`f0a2955a9fa33cc3c77a10d2e61c1c546cd7c94487e66e633542024eeaee5f36`
- .github/workflows/review-gate.yml：`e5416fdabdff0376e04bd43ce9ea49486a5bea2a5a49089acfab81248d7c1754`

## 公开评论拒绝服务修复独立复验（2026-09-09）

GitHub #13 第一轮机器人指出：任意公开评论中的 `@codex review` 会被原实现视为新审查请求，即使集成不接受该用户的触发。这是初次独立审查遗漏的问题，保留此历史，不将其归为先前已覆盖。

当前修复先检查 GitHub API 返回的 `author_association`，仅 OWNER/MEMBER/COLLABORATOR 的新请求阻塞；普通贡献者、陌生评论者、未知/缺失身份不因评论文字制造持久 pending。机器人汇总身份、当前 SHA、完成状态和线程条件仍独立必需。

独立执行 `.venv/bin/python -m pytest tests/test_review_gate.py -q`：**44 passed，0.02s**。另对全部 8 个官方 association 值及 None，分别测试 `@codex review`、`@codex security review`：18 个授权/非授权判定符合预期；每例移除真实 bot 汇总后仍拒绝，18/18。仅合成输入，没有外部评论或机器人触发。

通过真实 GitHub GraphQL `__type(name:"CommentAuthorAssociation")` introspection 核对语义：OWNER 是仓库所有者；MEMBER 是所属组织成员；COLLABORATOR 是获邀协作者；CONTRIBUTOR 仅表示曾提交代码。REST 回读当前仓库 `owner.type=User`、`private=false`，因此这项关联白名单适用于当前个人公开仓库的所有者/受信协作者边界。它不是通用实时权限检查：组织成员不必拥有具体仓库写权限；若未来迁移组织或扩大协作者授权，应改为核查实际受信触发资格。该条件当前未发生，不构成此次修复阻断。

结论：所报告的“单条非受信公开 review 命令持久阻塞”已在本地实现层修复并独立复验，无新增阻断发现。公开评论仍可触发工作流，持续评论可能造成排队或快照变化拒绝；此修复不证明防止任意公开活动造成的资源消耗/拒绝服务。真实集成是否接收受信请求、汇总更新与部署保护行为仍属于前述待验证边界。

受检 SHA256：scripts/review_gate.py `2c98194b5b19204858e0079f5e096a30e77936c8e2f489aec6546c18e670cbb9`；tests/test_review_gate.py `4f588d128de3d3779d4c265077c48958449027ad7e4db10043a3f347fa80e4f4`。

## formal Code Review 入口初次独立审查（2026-09-09）

范围变化：在完全不存在真实 bot 的汇总特征时，新增只读 formal pull-request review 证据入口。它只证明 Code Review，不声称 Security Review；点赞没有完整提交绑定，始终不能通过。设计审查另行负责缩限决定，本节验证实现。

独立运行定向测试：57 passed，0.03s。真实只读查询 #13 reviews 返回 bot ID 199175422、state COMMENTED、完整 commit_id `455fae1ac42b31da15548f0ec40060f10c4a5e24`、标准 `### 💡 Codex Review` 标题，证明该输入结构实际存在；这不证明当前未提交代码已获机器人通过。新增 reviews 使用已有 REST 全分页入口，纳入两次快照摘要；旧汇总特征存在时仍进入严格汇总路径。

**新增 P1 待处置：仅检查最新请求无法排除较早 scoped review 的迟到结果。** 在正式 review 没有可信触发请求关联的情况下，08:00 `@codex review only README`、09:00 `@codex review`、10:00 一个标准 formal review 的证据被当前实现判 READY。该 review 可能是 08:00 scoped 请求的迟到回复，09:00 全量审查仍在运行。独立合成调用真实 verdict 已复现 READY；没有执行真实机器人竞态。

位置：`formal_code_review` 中 `request = max(requests, key=request_time)` 后仅检查该请求文本。建议没有可信关联时拒绝任意仍存在的受信 scoped 命令，或引入可核查的触发关联；不能凭 submitted_at 晚于最新请求推定该结果属于最新全量请求。该发现修复后须复验，先前“无未处置实现阻断”结论仅适用于先前受检版本。

### formal 入口 P1 修复复验

上述 scoped 迟到发现已关闭于本地实现层。`formal_code_review` 现检查全部仍存受信请求，任一定向 code 请求均拒绝，不再由较新的全量命令掩盖；Security 请求仍单独拒绝。

独立运行 58 tests：58 passed，0.03s。额外重放 scoped08:00→full09:00→review10:00 得 SCOPED_REVIEW_REQUEST；仅完整 code 请求得 READY；security→full 得 SECURITY_COMPLETION_MISSING。辅助重放命令首次因审查者脚本字典引号笔误未执行，修正后上述三例均通过；不是被审实现失败。

当前受检版本没有剩余实现阻断发现。只有保守拒绝路径的明确可用性局限：仍存的定向/security 请求、无完整 SHA 的点赞、未知或失配汇总不能通过；不会把只完成 Code Review 标为 Security Review 已通过。实际主线发布、检查生命周期与分支保护效果继续保持未验证，不改变人审/激活门槛。

复验 SHA256：scripts/review_gate.py `5e6dbdad534125a846edd686ea931b533d6721acbc945cdb95c0b3774710f95e`；tests/test_review_gate.py `8d0c3b20320e0164eaef4173140ea09686800232eb60a3b59f6e93d48c711266`。
