# #668 Holmes 同题同模型对照（租机前准备）

- 状态：准备已合并（#732；租机修复 #760、#764）；2026-11-20/21 集中租机（#669）Holmes 侧 53 题 × 3 遍全量已跑完，结果见下文「租机结果」
- 更新日期：2026-11-21
- 依据：issue #668；[B/C 批次记录](2026-11-16-tool-expansion-bc.md)；[决定表](2026-11-12-tool-expansion-contract.md) R8-3、R8-4、R8-5、R8-13、R8-15、R10-1；[#666 记录](2026-11-18-holmes-evals-666.md)；上游 HolmesGPT `a0009195`
- 工作区：`../poa-668-compare`，分支 `feature/668-holmes-compare`，起点 `origin/main` `7fe919df`

## 目标与范围

租机上一条命令跑上游 pytest harness（官方原样配置）的 Holmes 侧，题单、模型、judge、R8-13、R10-1 与 OpsPilot 的 #666 轨同口径；再把两家逐题逐遍记录合成一张表。本机不跑 Holmes、不调模型；`opspilot/` 不改；上游代码不改、不 fork。

## 跑法（租机，Linux）

```
scripts/holmes_compare/run_rental.sh <轮次> <env 文件> [--judge-path upstream] [--questions 254 --trials 1] [--kubeconfig F]
.venv/bin/python -m scripts.holmes_compare.merge --opspilot-evidence docs/evidence/holmes-evals-<轮次> \
  --holmes-evidence docs/evidence/holmes-compare-<轮次> --opspilot-es-mode external --out-md <F.md> --out-json <F.json>
```

- 安装：`install_upstream.sh` 浅取 `a0009195` 到 `tmp/holmes-compare/upstream`，Poetry 1.8.5（上游锁文件版本）装 `--with dev`，最后 `--collect-only` 自检；工作树有改动或有 `.env` 拒绝。
- env 文件：`DEEPSEEK_API_KEY`、`ELASTICSEARCH_URL`、`ELASTICSEARCH_API_KEY`（工程师 key，上游 `before_test` 与工具集都读它）、`ZAI_API_KEY`/`GLM_API_KEY`；只在 runner 进程内读，不打印。
- 每题每遍一个 pytest 会话（`-n 0`，`ITERATIONS=1`）：插件 `pytest_plugin/opspilot_holmes_compare.py` 只留该题 → 上游 session 只跑该题 `before_test` → 插件调 OpsPilot 的 `apply_shift` 平移数据并改写 `user_prompt`（R10-1）→ Holmes 跑 → 插件截住上游要送 judge 的文本（含上游默认的中间输出与 `include_tool_calls` 附件），按 OpsPilot 同一规则反平移写 `judge-input.txt` → 会话结束上游 `after_test`，runner 再按名删一次 `app-<题号>-*`（同 #666 H-7）。
- R8-13：6 题（B1 的 275–278、280 与 C5 的 282，`questions.R8_13_SKILL_ASSERTION_DROPPED`）插件把 `memories_generated` 置空：上游不附「# Suggested Skills」段、不做建议条数硬断言、不跑 replay；SuggestSkills 工具本身照官方注入。
- 环境类失败（setup、平移、harness 起不来）每遍 3 次机会，同 OpsPilot；Holmes 跑了但没有回答记 `no_report`，有文本无结论记 `judge_exhausted`，均记 0。
- 输出：原始文件在 `tmp/holmes-compare/<轮次>/<题号>/trial-<n>/chance-<k>/`（pytest 日志与 judge 理由在 `answer-derived/`，跑时不读）；证据 `docs/evidence/holmes-compare-<轮次>/<题号>/trial-<n>/summary.json`（schema `holmes-compare-trial-1`），只有类别、judge choice 与错误码、Holmes 耗时、工具调用数、LLM 调用数、平移记录、文本哈希；合表只读这些字段。
- 合表：两家用同一个 `scoring.score`；表列题号、两家 3 遍通过数与通过率、摇摆/缺遍/无答/判分失败标记、R8-13 标记、平移标记（O/H）；两家总分分列不合并；结果页首行为「Holmes 按官方默认配置运行，含 OpsPilot 按只读约束不提供的工具」。

## 核实结论（读上游代码，未运行）

- 题面：上游 `TestCaseLoader` 直接读 `test_case.yaml` 的 `user_prompt`，无环境变量覆盖（`tests/llm/utils/test_case_utils.py:464-560`）；注入改为插件在 setup 后改测试参数对象，不改文件。
- 模型：`MODEL=deepseek/deepseek-flash` 加 `THINKING`、`REASONING_EFFORT=high`、`OVERRIDE_MAX_OUTPUT_TOKEN` 等，直接复用 F14 的 `scripts/holmes_baseline/config.py` `MODEL_ENV`（上游 `holmes/core/llm.py:741-757` 读这些变量）。
- judge：上游不走 litellm，走 autoevals `LLMClassifier` + openai SDK（`classifiers.py:97-235`），`CLASSIFIER_MODEL`、`OPENAI_API_KEY`、`OPENAI_API_BASE` 能把端点与模型换成 Z.ai `glm-5.3-flashx`。但与 OpsPilot 的 judge 仍有四处不同：autoevals `use_cot` 用强制具名 tool call 取 choice（Z.ai 是否支持具名 `tool_choice` 未确认）；不带 `reasoning_effort=high`（`binary_judge` 注释：GLM-5.3 默认最高档接近超时）；作答说明是 autoevals 的 CoT 而非 `ANSWER_INSTRUCTION` + json schema；判错时上游断言信息会打印期望答案（日志已放 `answer-derived/`、`--tb=no`）。
- ES：上游根 `conftest.py:258-293` 的 `responses` 夹具拦截所有 `requests` 调用，只放行 `http://localhost…` 与几个云 ES 域名；`http://127.0.0.1` 不放行。runner 对其他 URL 直接拒绝（`ES_URL_NOT_PASSTHRU`）。
- 工具面（R8-15，读题目 `toolsets.yaml`）：30 题都开 `elasticsearch/data`、`elasticsearch/cluster`，269 另开 `kubernetes/core`、`kubernetes/logs`；另加上游 `default_toolsets.yaml` 里未被题目关掉的工具集。

## 可逆自定（一行理由）

- 每题每遍单独一个 pytest 会话：与 OpsPilot 每遍重跑 setup、ES 只留当前题一致；上游一个会话跑多遍时共用一次 setup。
- 模型参数用 F14 `MODEL_ENV`：已有同模型对齐裁决（U6），不另起一套。
- `opspilot` judge 模式下上游分类器预检指向 DeepSeek 自己的 OpenAI 兼容端点：Z.ai key 不进 Holmes 进程（Holmes 有 bash 等工具）。
- 平移量各自按运行时刻算：规则相同，数据字节不同（R10-1 要求同一规则）。

## 裁决（lead，2026-11-19，可逆，均在已批准合同内）

- judge 路径默认 `opspilot`：上游 harness 建的 judge 文本用 #666 `judge_trial` 判，两家同一 judge 口径（R8-3）；`--judge-path upstream` 保留为可选对照。结果页表头写明所用口径；同一轮混用两种口径时合表拒绝（`HOLMES_JUDGE_PATH_MIXED`）。
- ES：两家用同一个真实 ES 8（R8-12），Holmes 侧用 `http://localhost:<端口>`；runner 收到 `127.0.0.1` 时拒绝并提示改用 localhost（`ES_URL_USE_LOCALHOST`）。OpsPilot 的 summary 不记 ES 模式，合表要求操作者声明 `--opspilot-es-mode`，`fake` 直接拒绝（`DATA_PLANE_DIFFERS`）：比在表头标注更简单，也不用改 #666 的 summary schema。
- 269：租机有 kind 就跑，没有集群照实记 `env_broken`。
- 282_loki：加入 R8-13 题单。B1 的 `SKILL_ASSERTION_DROPPED` 被 #666 合同测试钉成 B1 五题且是 B1 子集，所以另加 `R8_13_SKILL_ASSERTION_DROPPED`（B1 五题 + 282），对照侧读它；只改数据，不读答案。282 要等 C5 题进入题单（runner 目前只认 B1）才会实际跑到。

## 租机结果（2026-11-20/21，#669）

- 同题同模型（deepseek-flash），两侧同一 opspilot judge 口径，main `8b912caa`，2026-11-20 20:29 至 11-21 01:30 PT。主集合 Holmes 150/159（94.3%），OpsPilot 133/159（83.6%）；分组与逐题见 [全量记录](../evidence/rental-2026-11-20/full/run.md) 与 `../evidence/rental-2026-11-20/full/compare-*.md`。
- Holmes 5 遍 no_report：MongoDB 题 229×3、230×2，pytest call 阶段 KeyError 或 Skipped，记 HOLMES_NO_ANSWER；是否是上游 harness 问题未确认。
- 冒烟核实（[检查点 2 线 L](../evidence/rental-2026-11-20/cp2/L/run.md)）：上游 classifier 预检打 DeepSeek 未失败；scores 落在 `--evidence-root` 下（#764）。R8-13 外网 URL、184/185 判分文本大小未执行。
- 上游 harness 已在租机装好（[主机记录](../evidence/rental-2026-11-20/run.md)，收集 294 项）；下文「未确认」其余各项本次未单独核实。

- 上游 harness 在租机上的单题耗时、安装时长与磁盘占用；首跑先 `--questions 254 --trials 1` 冒烟。
- 上游 `additional_system_prompt` 夹具在含 frontend 工具的题（R8-13 五题）会从上游默认 URL 拉附加提示词，需要租机能出网。
- Z.ai 对 autoevals 强制 tool call 的兼容性（只影响 `upstream` 路径）。

## 证据

- 本机：`tests/test_holmes_compare_contract.py` 31 passed（合成两家记录；插件在上游测试模块替身上端到端跑两种 judge 路径；裁决后加默认口径、ES 提示、合表拒绝、282）；ruff、`mypy --strict` 干净；`opspilot/` 0 行改动。
