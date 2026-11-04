# F14 上游更新策略与扩展对应关系（HolmesGPT `a0009195`）

2026-11-04；F14 第 4 步验收后半句「retain provenance and an upstream-update strategy」。按用户裁决 U4（F1 收尾计划，2026-11-04）：OpsPilot 本身就是第 4 步里的「采纳的扩展」，映射表的「必要扩展」行就是采纳内容；与未改上游的同条件对比直接复用 F1 第 4 步，不另跑改过的 Holmes。验收文字不改，这是解释。任务记录见 [F14 上游对照](../tasks/2026-10-27-m3-f14-upstream.md)，计划见 F1 收尾计划 S9–S10。

本页回答三件事：OpsPilot 作为扩展与上游的对应关系、以后怎样跟进上游、F1 第 4 步结果出来后怎样填进 F14 第 4 步的证据。

## 1. 当前固定点

| 项 | 值 | 出处 |
|---|---|---|
| 上游提交 | `a0009195468e…`（tree `e94a57b3…`，上游提交时间 2026-10-21），Apache-2.0 | [pin.json](../evidence/f14-holmes-baseline/pin.json) |
| 镜像 | 本地构建、未推送；arm64 image ID `sha256:3f2995d1…`（本机 kind lab）。开考主机上的 amd64 镜像 ID 不同，由 S9 另记，两种镜像的结果不混在一轮 | pin.json；S7 Holmes runner 任务记录 H8 |
| 不可复现原因 | 基础镜像按 tag 引用、alpine edge 源、poetry 未锁版本、`apk upgrade` | pin.json `non_reproducible_because` |
| 配置 | 只开 `prometheus/metrics` 与 GET-only `jaeger`，无 K8s 凭据，走 lab 只读代理 `holmes` 账号 | pin.json `config`；隔离取证 8 项全过 |
| 代码里的钉子 | `scripts/holmes_baseline/mapping.py` 的 `COMMIT`/`TREE`，`scripts/holmes_baseline/run.py` 的 `IMAGE`；`upstream-files --check` 与 `records` 要求 checkout 恰好是这个提交且无本地改动 | 源码 |
| 映射表 | 77 行，OpsPilot 侧对照 main `659bcd9c` | [mapping.json](../evidence/f14-upstream-mapping/mapping.json) |
| 引用的上游文件 | 第 2 步 48 个、第 3 步另一份清单，均带 blob SHA 与行数 | [第 2 步](../evidence/f14-upstream-mapping/upstream-files.json)、[第 3 步](../evidence/f14-upstream-issues/upstream-files.json) |

## 2. OpsPilot 作为扩展：与上游的对应关系

映射表当前分类（第 3 步回写后）：

| 类别 | 行数 | 对第 4 步的意义 |
|---|---|---|
| 必要扩展 `required_extension` | 52 | 采纳的扩展本体。第 2 步合并时是 53 行，第 3 步把 F7 第 2 步注入边界一行改为已确认缺陷 |
| 复用 `reuse` | 13 | 照上游做；其中 4 行计划中（F1 防作弊出题、重复运行、事后三类分诊，F8 健康端点）、1 行部分（F9 归属与许可证） |
| 配置 `configuration` | 9 | 同条件基线自身的配置 3 行，其余是 OpsPilot 自写的同等机制 |
| 已确认缺陷 `confirmed_defect` | 3 | F1 第 4 步 `MAX_OUTPUT_TOKEN_RESERVATION` 不生效、F3 第 4 步空结果报成错误、F7 第 2 步告警原文里的指令被执行；都有 `a0009195` 运行证据 |

必要扩展按功能：F1 8、F2 6、F3 6、F9 6、F6 5、F12 5、F7 4、F8 4、F11 4、F13 4。

同条件对比能直接量到的只是其中一部分。F1 第 4 步只有问题入口题两边都跑（收口合同 V2-R12），所以对比结果是下面这些行的运行证据：

- F3 调查（ReportV2、证据身份、错误分类）与上面三条已确认缺陷中的 F3、F1 两条：两边同一问句、同一只读代理、同一预算下的结论与失败。
- F7 只读与注入边界：Q11 确定性列里 Holmes 只适用「只读未越权、调查跑完」，OpsPilot 另有证据绑定与 `read_only` 权限列。
- F1 结果合同与确定性门本身：对比的判分代码就是这些扩展。

F2 持久事故、F6 恢复观察、F11 发布观察、F12 工作台、F13 复盘知识、F8/F9 的扩展行，上游没有对应入口，不在对比里；它们的证据是各自功能的验收步骤（已 `passes: true` 的以该功能证据为准），本页不重复。告警入口题在结果表里标「上游无此入口」，只有 OpsPilot 一行。

## 3. 怎样跟进上游

原则：pin 不自动移动；CI 不连上游（第 2、3 步已定）；不 fork、不向上游提 issue 或 PR（第 3 步 R12，需要用户另授权）；旧 pin 的证据与对比结果保留不覆盖，新结果按新 pin 另存。

### 复核分三级

| 级别 | 什么时候 | 做什么 | 费用 |
|---|---|---|---|
| L0 漂移检查 | 项目活跃期间每月一次，以及 OpsPilot 每次打 minor 版本 tag 前（先到者为准） | 把两份 `upstream-files.json` 的路径合并去重，和上游 `a0009195...master` 的改动文件求交集（命令见下）；同时看上游 release 说明与安全公告。只记录：上游新 HEAD、领先提交数、被改动的引用文件、是否命中下面的触发条件 | 零模型费用 |
| L1 映射复核 | L0 命中任一触发条件 | 在新提交的独立 checkout 上，只复读引用了被改文件的映射行，重跑这些行的 `absence_checks` 命令（命令原文在表里，`<checkout>` 换成新 checkout），判断类别是否会变；已确认缺陷涉及的文件被改时，用 `run.py repro` 的脚本化模型端点用例（零费用）在新提交上重跑对应说法。结论写进复核 issue，不改 `mapping.json` | 零或极少模型费用 |
| L2 移动 pin | L1 认为类别会变且值得更新，或要对外发布新的对比数字 | 按第 1 步重新钉住：构建镜像、新 `pin.json`、隔离取证 8 项、请求核对；改 `mapping.py` 的 `COMMIT`/`TREE` 与 `run.py` 的 `IMAGE`；重生成两份 `upstream-files.json`，按 L1 结论更新 `mapping.json`；跑下面的同条件对比子集 | 真实 Run 费用，按余额差对账 |

L0 命令。GitHub compare API 最多返回 300 个改动文件，命令在达到 300 时报错退出、不出交集；这时改用上游的完整 clone（主仓库 `tmp/upstream/holmesgpt-a0009195` 是浅克隆，不能用）：

```sh
jq -r '.files|keys[]' docs/evidence/f14-upstream-mapping/upstream-files.json \
  docs/evidence/f14-upstream-issues/upstream-files.json | sort -u > cited.txt
gh api repos/HolmesGPT/holmesgpt/compare/a0009195468ebc55a7dff7d602b61feabe3cda1b...master \
  --jq 'if (.files|length) >= 300 then error("compare truncated at 300 files; use a full clone") else .files[].filename end' \
  > changed.txt && sort -o changed.txt changed.txt && comm -12 cited.txt changed.txt
# 达到 300 时：在完整 clone 里
# git fetch origin master && git diff --name-only a0009195 FETCH_HEAD | sort > changed.txt && comm -12 cited.txt changed.txt
```

首次 L0（2026-11-04，写本页时跑）：上游 master `586045ca`（2026-11-04T14:01Z），领先 pin 32 个提交，改动 206 个文件；51 个引用文件中有 16 个被改，包括 `holmes/plugins/toolsets/prometheus/prometheus.py`（F3 第 4 步已确认缺陷与 #2139 所在）、`holmes/common/env_vars.py`（`MAX_OUTPUT_TOKEN_RESERVATION`）、`holmes/core/tool_calling_llm.py`、`holmes/core/llm.py`、`Dockerfile`、`pyproject.toml` 与 `tests/llm/utils/classifiers.py`。这命中触发条件 1，按本策略该做 L1；L1 还没做，pin 不动，F1 第 4 步照旧用 `a0009195`。

触发 L1 的条件（任一）：

1. 被改动的上游文件在两份引用清单里。
2. 上游修了三条已确认缺陷之一，或第 3 步四个选题（#2139、#2356、#2376、#2168）相关代码有变化。
3. 上游新增 OpsPilot 映射表里写「上游没有」的能力（L0 时按 release 说明判断；拿不准就做 L1）。
4. 许可证文件变化，或上游安全公告涉及已启用的 Prometheus/Jaeger 工具集或 LiteLLM。
5. 要开工 4 行计划中的复用项之一：先对新提交复核该行再移植，避免移植已被上游改掉的做法。
6. OpsPilot 侧改动让 `upstream-files --check` 报出 OpsPilot 路径或符号问题（这时只修 OpsPilot 一侧引用，不需要移动 pin）。

### L2 的同条件对比子集

目的不是重做 F1 第 4 步，而是回答「新 pin 下上游基线有没有变」。

- 题：F1 第 4 步数据集里两边都跑的问题入口题，每类取 1 正 1 反（类别以 F1 第 4 步实际用到的为准）；保留集在 S10 发布后已转为已见，可并入，但结果只能标「开发集」。
- 次数：每题 3 次，与 F1 第 4 步相同。
- 跑什么：新 pin 的 Holmes 与旧 pin 的 Holmes 配对（同一次注入、同窗先后跑、先后轮换，配对排程与 S7 相同）。OpsPilot 没改动且 lab 与模型未变时不重跑，直接引用 F1 第 4 步的结果；否则 OpsPilot 也进同一配对。
- 判法：与 F1 第 4 步同一套判分代码（两家二值 judge、分歧交用户裁、Q11 确定性列），同模型、同只读代理、同预算。
- 产出：新 pin 目录下的 `summary.json`，逐题列旧 pin / 新 pin（/ OpsPilot）结果与失败；trace 照常上传 LangSmith。规模估计：按 F14 第 3 步实测约 5.38 CNY / 11 次真实 Run 的量级（未按新题型实测）。

### 谁做

- L0、L1：编排 Agent 派一个执行者做，结论记在一个 GitHub issue（例如「F14 上游复核 2026-12」）。L1 有类别变化结论时，由未参与的全新上下文 Agent 抽查后才算定。没有自动定时任务，L0 由接手的 Agent 在接手清单里按日期判断是否到期。
- L2：一个有界工作项，走正常任务记录、合同预审与 PR；改动只在 `scripts/holmes_baseline/` 与证据目录，不改 `opspilot/`。
- 找用户：对外发布的对比数字要换成新 pin 的结果（README、作品集评测页）；新 pin 需要新的数据出口（出口代理白名单以外的主机）或更多工具集与权限；决定向上游报告问题。其余由 Agent 按推荐自定。
- 项目进入不活跃期时不做定期 L0；对外文档写明「上游对照截至 HolmesGPT `a0009195`（上游提交 2026-10-21）」。

## 4. F14 第 4 步证据（待 F1 第 4 步结果，S10 后填）

> 以下为占位，不含任何结果。S10 发布 F1 第 4 步结果后，由执行 S10 的 Agent 在同一 PR 或紧随其后的文档 PR 里填写，并在 F14 任务记录写一行指向这里。

| 项 | 内容 |
|---|---|
| F1 第 4 步结果 | 待填：`summary.json` 路径与 sha256、结果页链接 |
| 对比条件 | 待填：OpsPilot 冻结 commit 与 `opspilot/` 树 hash（S3）；评测代码冻结 commit 与树 hash（S7）；Holmes `a0009195` 与开考主机镜像 ID；配置 sha256；模型；预算；主机类型（独享 / 共享 vCPU，U7） |
| 数据集 | 待填：开发集与保留集的 `dataset` 维（`<名>:<hash>`）、题数、每题次数 |
| 结果摘要 | 待填：两边问题入口题的单次通过率与 pass^k、全部已开始尝试计入分母；两家 judge 一致率与用户裁决数；Q11 各自适用列；OpsPilot「根因证据引用率」（不进对比分母） |
| 失败 | 待填：逐题失败与作废/重跑记录（按出题前写好的规则） |
| 对应的映射行 | 待填：结果分别作为第 2 节哪些必要扩展与已确认缺陷行的运行证据；结果与某行说法矛盾时照实写，并开 L1 复核 |
| 费用 | 待填：DeepSeek 余额差 |
| 措辞 | 「冻结后由未参与调优的独立会话出题；未做技术隔离、非盲测」；判法写「未校准二值 judge + 人工裁决分歧」 |

F14 第 4 步达成需要：上表填完且证据入库；本页（更新策略）已合并；任务记录写明 U4 是解释、验收文字未改。之后按 SPEC 2026-10-27 段由 Agent 翻 F14 `passes`（F14 第 1–3 步的证据已在任务记录）。
