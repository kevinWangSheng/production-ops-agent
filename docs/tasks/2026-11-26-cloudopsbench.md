# Cloud-OpsBench 接入与分层子集运行

- 状态：进行中
- 更新日期：2026-11-26
- 依据：issue #871；[合同决定表 r1](2026-11-26-cloudopsbench-contract.md)；先例 [ITBench-Lite 适配](2026-11-12-itbench-lite-adapter.md)、`scripts/sregym_eval/`
- 工作区：分支 `chore/871-cloudopsbench`，worktree `../poa-871-cloudopsbench`（起点 `main` 565fe15f）

## 目标与范围

在租机上用公开基准 Cloud-OpsBench（LLM4Ops/Cloud-OpsBench，MIT，快照式 K8s 根因分析，754 题）评测 OpsPilot，并在同一子集上跑同模型对照（基准自带 ReAct + `deepseek-flash`）。只加评测侧代码（`scripts/cloudopsbench_eval/`），不改产品代码、报告合同和提示词；不做 MCP 客户端。简历口径不在本任务。

## 前提与完成条件

- 前提：DeepSeek / LangSmith 常设授权（AGENTS.md「费用与真实调用」）；租机由用户在 Vultr 控制台创建，代码就绪前不建机（U8）。
- 完成条件：
  1. 适配代码合入 `main`（CI 绿、独立审查发现已处置）。
  2. 冒烟按 U7 通过：两条线各跑 1 题 Boutique + 1 题 TrainTicket，答案可被基准评分器读入、缓存命中 > 0、OpsPilot 模型输入无真值、trace 可访问。
  3. 约 100 题分层子集（U5，名单与种子在运行前冻结入库）两条线跑完，冻结 `summary.json` 与结果入库（A6），费用按 token × 单价记账。

## 合同（r1）

- U1 评测专用只读工具族，放 `scripts/cloudopsbench_eval/`，走产品 `ReadOnlyToolExecutor` 与账本，不进镜像。
- U2 题面给 57 个标签与合法资源名，固定模板钉 sha256；8192 字符上限内放不下时压缩说明文字，不删标签。
- U3 报告→答案确定性转换，不调模型；题面要求逐字写出标签与 `kind/name`；未找到计 0。
- U4 保持产品默认 100 次模型请求上限，结果并列写明对照的 20 次迭代。
- U5 按（系统, 大类, 标签）分层，每层 ≥ 1，补到约 100，固定种子，运行前冻结；报未加权均值与全集加权估计。
- U6 同机同子集跑自带 ReAct（`deepseek-flash`，温度 0，20 次迭代），用基准 `evaluation.py` 打分；基准代码只在独立 venv 中运行。
- U7 冒烟通过条件见完成条件 2；答对不是条件；冒烟后先看反馈再定是否改适配。
- U8 租机 4 vCPU / 16 GB 起步，不够换 8 / 31 / 150；跑完由用户 Destroy。
- U9 推迟。A1–A9 见决定表。

实施中由执行 Agent 自定的可逆细节（A10 起，各一行理由）：

- A10 工具名：产品注册表只收小写名（`opspilot/tools/registry.py` `_NAME`），12/10 个工具按固定映射注册为 snake_case（`GetResources`→`get_resources`），参数名、类型、枚举、必填与参数描述原样，描述首句写明对应的基准工具名；轨迹写回评分文件时用原名，A4 仍可算。要逐字同名，最小产品改动是放宽工具名正则允许大写（产品改动，未做）。
- A11 后端用基准自带 MCP 服务（独立 venv、`python -I`、只给快照视图），不重写缓存键：语义与基准一致，JSONL 轨迹直接给 A3 计数和 A4 轨迹。每题核对 `tools/list` 与钉住的 `scripts/cloudopsbench_eval/tool_catalog.json` 一致，不一致记 `TOOLS_MISMATCH`。
- A12 注册方式：`product_entry` 在评测进程内把 web/worker 唯一一处 `select_profile` 调用换成评测 profile；产品文件和产品 profile 列表都不改。
- A13 提示变体沿用扩展档位的 `replay-candidate-k8s`（产品 K8s 调查指导原文），不新增提示词。
- A14 工具结果按行放进 `lines`；超过 25k token 按产品规则拒绝（RESULT_TOO_LARGE），不截断；不注册证据分页工具。模型给可选参数传 null 时，产品网关按 INVALID_PARAMS 拒绝，这次调用不进 MCP 轨迹。
- A15 转换细则（U3 的展开）：对象先看 `root_cause` 字段（Node→node，Namespace→namespace，ResourceQuota→其所在 namespace，其余 kind 按名字去 Pod/ReplicaSet 哈希后缀、再取最长的 List B 应用名前缀），其次 summary 里的 `fault object: kind/name`，再次 summary 里第一个 `kind/name`；标签先看 summary 的 `Root cause: <label>`，其次 summary 第一个标签，再次 fact/hypothesis 声明里第一个标签（不看 rejected_hypothesis、counter_evidence、recommendation）；标签整词匹配、不分大小写。冻结摘要只记用了哪条规则、找没找到，不记预测值。
- A16 缓存计数按 MCP 轨迹的输出文本分四类：hit；not_found（`Error from server (NotFound)` 开头，A3 的合成答案）；not_recorded（只认基准 `tools/snapshot.py` 自己的“未录制/查找出错”原文，如 `... is not recorded.`、`An unexpected error occurred during snapshot lookup ...`，清单见 `answer.NOT_RECORDED`）；adapter_error（服务端 isError，参数被拒）。其余文本（含以 `Error` 开头的已录制输出和参数错误提示）计 hit。
- A17 隔离：尝试目录按清单序号命名（`c001`），不含题号（题号含故障大类）；题面、幂等键（`cob-<round>-c001-t1`）和 LangSmith 项目名都不含题号；web/worker 环境里出现数据集路径或题号即拒绝（`PRODUCT_ENV_LEAK`）；每个 Run 的原始 ledger 检查答案标记（metadata.json、milestone、fault_object 等），命中只记录不改分（U7 ④ 人工判定）。
- A18 Trace：评测 web/worker 不设 `OPSPILOT_TOOL_PROFILE`，tracer 的 lab 检查按默认值通过；送出的数据是公开基准快照，属 U1 认定的 synthetic lab。
- A19 对照在基准代码的临时副本里写配置，不写 checkout；API key 只在进程运行期间存在于 600 权限的配置文件里，结束即清空。其余参数用基准默认（`max_tokens` 4096、`timeout` 60，api_base `https://api.deepseek.com/v1`）。产品请求开 thinking，基准 ModelRunner 不传 thinking 参数，差异照实记录。
- A20 评分：每个（系统, 大类）调用一次基准 `evaluation.py`，用它自己的 `case_split` 文件限定到子集；只读回逐题指标，评分器的明细文件（含标准答案）留在 `tmp/`。
- A21 子集已冻结：种子 871、目标 100、58 层，[subset.json](../evidence/cloudopsbench-871/subset.json)（sha256 `cf14c665…48bd93`）与 [strata.json](../evidence/cloudopsbench-871/strata.json)（各层全集与子集题数）；题与层的对应不入库，评分时在租机上重算。
- A22 转换诊断：冻结摘要另记 `root_cause` 对象与 summary 里显式 `fault object: kind/name` 是否一致（任一缺失为 null），轮摘要汇总 agree/disagree/not_comparable；只作诊断，U3 的优先顺序不变。
- A23 评分口径：基准自带 `summarize` 只对有答案文件的题求平均；本适配两条线都给每题写答案文件，失败题按 0 计入分母。引用发布成绩对比时要写明这一口径差异。
- A24 打分时按每条尝试记录里写下的题号对应记录（不按清单位置）；租机上重算的分层与入库的 `strata.json` 不一致即拒绝打分（`STRATA_MISMATCH`），只列两题的冒烟清单没有 `strata.json`，记 `no_committed_strata`。
- A25 对照 `max_tokens` 4096 是基准默认；若 DeepSeek 在不传 thinking 参数时默认开思考，输出可能被截断，冒烟时检查对照轨迹的 stop_reason 与末步输出是否完整。
- A26 冒烟另查：尝试目录名是 `c00N`（不含题号），web/worker 进程环境里没有题号与数据集路径（`/proc/<pid>/environ` 抽查）。

## 执行进展与证据

- 2026-11-26：预审完成（未参与实现的 Opus 子 Agent，只读）；用户裁决 U1–U8 按推荐，U9 推迟 → r1。
- 2026-11-26：适配实现（执行 Agent，分支 `chore/871-cloudopsbench`）。本地离线核对，均未调模型：基准 MCP 服务（mcp 2.3.0，临时 venv）在 boutique/codedefect/1 与 trainticket/service/25 的视图上经产品 `ReadOnlyToolExecutor` 返回缓存结果，未录制查询返回合成 NotFound；不带 worker 的一次尝试走通视图、MCP、PG、web 收题（201）、冻结、清理与端口释放；基准 `evaluation.py` 读入转换后的答案（`top_level_final_answer`），失败题按 `unparsed` 计 0；对照入口在关闭端口上跑完 20 步，配置里不留 key。题面最长 6941 字符（TrainTicket）。

## 租机运行手册

前提：Ubuntu 24.04，Python 3.12，PostgreSQL 17 二进制（同 [rental-2026-11-23](../evidence/rental-2026-11-23/run.md) 的装法），私有 env 文件含 `DEEPSEEK_API_KEY` 与 `LANGSMITH_API_KEY`（可选 `LANGSMITH_ENDPOINT`、`LANGSMITH_WORKSPACE_ID`）。

1. 两个 venv：仓库 `make setup`（项目 `.venv`）；基准 `git clone https://github.com/LLM4Ops/Cloud-OpsBench ~/cob && git -C ~/cob checkout ec1d4f3`（含 3.6 GB `benchmark/`），`python3.12 -m venv ~/cob-venv && ~/cob-venv/bin/pip install -r ~/cob/cloudopsbench_mcp/requirements.txt -r ~/cob/agents/cloudops_agent/requirements.txt`。基准代码只由 `~/cob-venv/bin/python -I` 运行。
2. 公共参数：`C="--checkout $HOME/cob --cob-python $HOME/cob-venv/bin/python --env-file <env>"`。
3. 冒烟（U7）：各取一题 Boutique codedefect 与一题 TrainTicket，两条线各跑一次再打分：
   `.venv/bin/python -m scripts.cloudopsbench_eval.driver attempt --round smoke-<date> $C --case <boutique/codedefect/N> --seq 1`（第二题 `--seq 2`）；
   `... driver control --round smoke-<date> $C --case <id> --seq <n>`；
   打分要一个只列这两题的 subset 文件：`... driver score --round smoke-<date> --line opspilot $C --subset <file>`（`--line control` 同理）。看 `tmp/cloudopsbench/<round>/opspilot/c00N/attempt.json` 的 `run_wait`、`cache`、`leak_check`、`trace`。
4. 子集：`... driver round --round <r> $C --subset docs/evidence/cloudopsbench-871/subset.json --resume`；并行时每个进程用不同 `--shard i/n` 与 `--port-offset`（10、20…）；对照用 `control`，参数相同。
5. 打分与冻结：`score --line opspilot` 与 `--line control`（可加 `--prices <命中>,<未命中>,<输出>`，美元每百万 token），写 `docs/evidence/cloudopsbench-<r>/<line>/summary.json`；入库前跑 `scripts/check_secrets.py` 与 gitleaks（A9）。

## 下一步与交接

- 独立测试作者按合同写验收测试，独立审查后开 PR。
- 代码就绪后请用户建租机，跑冒烟。
- 未确认：发布成绩是否全集与每题次数；单题耗时与并发；DeepSeek 对照在不传 thinking 参数时的默认模式。
