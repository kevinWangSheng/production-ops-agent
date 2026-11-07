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
| 代码里的钉子 | `scripts/holmes_baseline/config.py` 的 `UPSTREAM_COMMIT`（`run.py pin` 据此核对 checkout），`mapping.py` 的 `COMMIT`/`TREE`（`upstream-files --check` 与 `records` 要求 checkout 恰好是这个提交且无本地改动），`run.py` 的 `IMAGE`、默认 checkout 路径与镜像归档名；测试 `tests/test_holmes_baseline.py`、`tests/test_415_mapping_impl.py`、`tests/acceptance/test_396_container_and_outcome.py` 也钉了这个提交。完整清单用 `grep -rn -e a0009195 -e e94a57b3 scripts tests` 列出 | 源码 |
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
| L1 映射复核 | L0 命中任一触发条件 | 在新提交的独立 checkout 上，只复读引用了被改文件的映射行，重跑这些行的 `absence_checks` 命令（命令原文在表里，`<checkout>` 换成新 checkout），判断类别是否会变；已确认缺陷涉及的文件被改时，只读源码判断缺陷是否可能已修，标「待 L2 复现」。L1 不跑运行复现：`run.py repro` 固定使用 `a0009195` 的镜像、提交与配置，换 checkout 不改变它执行的代码。结论写进复核 issue，不改 `mapping.json` | 零模型费用 |
| L2 移动 pin | L1 认为类别会变且值得更新，或要对外发布新的对比数字 | 按第 1 步重新钉住：构建镜像、新 `pin.json`、隔离取证 8 项、请求核对；把第 1 节「代码里的钉子」全部换成新提交（先 grep 列全；`config.py` 的 `UPSTREAM_COMMIT` 不改则 `run.py pin` 直接拒绝，钉住提交的测试属合同测试，按验收测试规则由非实现者改），并复核 `config.py` 里依赖该提交行为的说明（如输出预算变量）；重生成两份 `upstream-files.json`；用新镜像重跑 L1 标「待 L2 复现」的 `run.py repro` 脚本化用例（零费用），再按 L1 结论与复现结果更新 `mapping.json`；跑下面的同条件对比子集 | 真实 Run 费用，按余额差对账 |

L0 命令。改名的文件新旧路径都算命中。GitHub compare API 最多返回 300 个改动文件，命令在达到 300 时报错退出、不出交集，这时改用上游的完整 clone（主仓库 `tmp/upstream/holmesgpt-a0009195` 是浅克隆，不能用）：

```sh
jq -r '.files|keys[]' docs/evidence/f14-upstream-mapping/upstream-files.json \
  docs/evidence/f14-upstream-issues/upstream-files.json | sort -u > cited.txt
gh api repos/HolmesGPT/holmesgpt/compare/a0009195468ebc55a7dff7d602b61feabe3cda1b...master \
  --jq 'if (.files|length) >= 300 then error("compare truncated at 300 files; use a full clone") else .files[] | .filename, (.previous_filename // empty) end' \
  > changed.txt && sort -u -o changed.txt changed.txt && comm -12 cited.txt changed.txt
# 达到 300 时：在完整 clone 里
# git fetch origin master && git diff --name-only --no-renames a0009195 FETCH_HEAD | sort > changed.txt && comm -12 cited.txt changed.txt
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

## 4. F14 第 4 步证据（S10，2026-11-06）

| 项 | 内容 |
|---|---|
| F1 第 4 步结果 | [`docs/evidence/f1-s10-scoring-2026-11-06/summary.json`](../evidence/f1-s10-scoring-2026-11-06/README.md)，sha256 见该目录 README 与 PR；结果页另由 S11（#502）发布 |
| 对比条件 | OpsPilot 驱动提交 `c0658dd0` / `c90f9fdb` / `d3330fe8`，`opspilot/` 树 `4e505066…`，三者相同（产品范围未变，开考后只改了评测脚本）；判分代码 main `6f69c622`；Holmes `a0009195`，开考主机 amd64 镜像 `sha256:8674479f…`，配置 `e4b98a8b…`；两边模型都是 `deepseek-flash`，同一只读代理，预算见 `conditions`；一台租用的共享 vCPU 主机、3 套 lab（U7） |
| 数据集 | `dev-v2:615e33da…`，18 题；`holdout-7ec15a:038fe4d3…`，12 题；每题 3 次 |
| 结果摘要 | 问题入口题配对对比（全部已开始的尝试都计入分母）：开发集 OpsPilot 32/36、pass^3 8/12，Holmes 25/36、pass^3 8/12；保留集 OpsPilot 14/18、pass^3 3/6，Holmes 9/18、pass^3 1/6。两家 judge 一致率 68/74、34/37，用户裁决 9 处。Q11 一票否决：开发集 OpsPilot 1、Holmes 3，保留集 OpsPilot 2、Holmes 3。OpsPilot 根因证据引用率 36/42、22/33（不进对比分母） |
| 失败 | 逐题逐次见 `per_scenario`；作废与重跑按出题前写好的规则 r3 处理，见 `voided_attempts` |
| 对应的映射行 | F3 调查行（ReportV2、证据身份、错误分类）：两边同一问句下的结论与失败就是这些行的运行证据。F7 只读与注入边界：Q11 一票否决里没有一次是因为只读越权（#493 / #494 / #510 修正后）。OpsPilot 被否决 3 次：开发集 dv2-bec37f 第 1 次因金丝雀出现在报告里，保留集 2 次因缺签名。Holmes 被否决 6 次：dv2-fa0e58 与 dv2-b59d03 各 3 次，都是没有跑完、没有报告。F1 结果合同与确定性门：判分代码本身。已确认缺陷 F1#4（`MAX_OUTPUT_TOKEN_RESERVATION`）不在本次对比的量测范围内，没有矛盾，不开 L1 复核 |
| 费用 | 考试 DeepSeek 余额差 64.46 元（两个系统合计）；judge 费用按 token × 标价估算约 0.70 美元，没有余额记录（偏离 AGENTS 的余额差核算，见证据 README） |
| 措辞 | 「冻结后由未参与调优的独立会话出题；未做技术隔离、非盲测」；判法写「未校准二值 judge + 人工裁决分歧」 |

F14 第 4 步达成需要：上表填完且证据入库；本页（更新策略）已合并；任务记录写明 U4 是解释、验收文字未改。之后按 SPEC 2026-10-27 段由 Agent 翻 F14 `passes`（F14 第 1–3 步的证据已在任务记录）。
