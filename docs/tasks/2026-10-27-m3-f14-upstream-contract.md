# F14 第 1 步合同预审：HolmesGPT a0009195 容器化 + 接同一只读代理 + 一次基线调查

- 预审者：未参与实现的 Agent，只读；2026-10-28；main `db5360c3`
- 上游原文：GitHub contents/tree API，固定提交 `a0009195468ebc55a7dff7d602b61feabe3cda1b`（2026-10-21，比 master 落后 11 个提交）。本机没有该提交的 checkout，只有 `tmp/*-prereview/upstream/holmesgpt-46e3a728…`；M0 用过的 `5e983c17` 源码也不在本机
- 范围：只管第 1 步（镜像、digest、许可证、配置、模型、接代理、一次基线 Run，以及以隔离取证关闭 #93 的 Holmes 子项）。映射表、issue 复现、同条件对比是后续步骤
- 裁决（r1，用户 2026-10-28）：U1、U4、U5、U7 按推荐（决策稿第二轮 Q6–Q9）；U2、U3、U6、U8、U9 属可逆默认，按推荐由 Agent 定；可逆默认表按推荐。见[决策稿第二轮](2026-10-27-m2-exit-m3-opening-decision.md#第二轮裁决2026-10-28)。

## 全量探针

- 原样 origin/main（临时 worktree `poa-prereview-f14`）：`.venv/bin/python -m pytest tests/acceptance -q` → **探针 0 失败**（2791 passed、299 skipped、409.8s；跳过的都是 `M1_DURABLE_POSTGRES=1` 门控）。
- 打草稿补丁再跑：草稿把 `scripts/kind_lab.py` 代理的 `@read` 改成对**所有账号**放行 POST（删掉 `proxy_pass_request_body off`，加 `client_max_body_size 64k`），对应下面 U1-b。
  - `tests/acceptance`：0 失败。
  - 定向跑 `tests/test_kind_lab_readonly_proxy.py tests/test_f7_authz_probe.py tests/test_m1_prometheus_credentials.py tests/acceptance/test_f7_step1_entry_points.py`：1 失败，原样如下——
    `FAILED tests/test_kind_lab_readonly_proxy.py::test_exact_nginx_rules_and_normalized_upstream`，`assert "if ($request_method !~ ^(GET|HEAD)$) { return 403; }" in config`（`:61`）
  - 证据型验收（grep 不到，靠读出来的）：冻结的 `docs/evidence/f7-authz-probe/probe.json:141-145,413-416` 记录了 investigator/observer 发 `POST /api/v1/query` 期望 403、实得 403；`scripts/f7_authz_probe.py:32` 把它列为拒绝项。`docs/evidence/f3-step6-authz-probe/probe.json`（40 处 POST）由 `tests/acceptance/test_f3_step5_real_runs.py:54,212-213` 静态校验。如果对所有账号放开 POST，下次真实探测就和这两份冻结证据冲突，F7 第 1 步和 F3 第 6 步的证据都要重新取。
- 清理：`git status --ignored` 只有草稿补丁和 `__pycache__`；已执行 `git worktree remove`，删除分支 `chore/prereview-f14-probe`。草稿补丁另存在 scratchpad 的 `probe-draft.patch`。

## 关键事实（每条推荐都依赖它们）

- 只读代理（F7 PR-C）**只管 Prometheus**：nginx 路径白名单，只放 `/api/v1/query|query_range|series|labels|status/buildinfo` 和 `/api/v1/label/<name>/values`，**只允许 GET/HEAD**，并且不转发请求体（`scripts/kind_lab.py:332-384`）；每个角色用 basic auth（`:146-161`）；`access_log off`（`:360`，被 `tests/test_kind_lab_readonly_proxy.py:77` 钉住）；宿主端口是 127.0.0.1:19090（`scripts/kind_lab/kind-config.yaml:18-21`）。Jaeger 走 16686 NodePort，没有鉴权，也不经代理（`scripts/kind_lab/jaeger-nodeport.yaml`；`opspilot/tools/otel_demo.py:1121-1122`）。
- Holmes `a0009195` 的 Prometheus 工具，`query` 和 `query_range` **用 POST 表单**（`holmes/plugins/toolsets/prometheus/prometheus.py:1623-1631,1877-1885`），前置检查用 GET `query?query=up`（`:2248-2256`）。结果是：在现有代理下工具集显示 enabled，但每一次查询都会拿到 403。它还会调用 `/api/v1/metadata`（`:1515`）和 `/api/v1/rules`（`:962`），代理都返回 403。
- `holmes ask` 用 `enable_all_toolsets_possible=True`，标签过滤 CORE+CLI（`holmes/main.py:371-374`；`holmes/config.py:458-480`）。bash 工具集 `enabled=True`（`holmes/plugins/toolsets/bash/bash_toolset.py:411-421`）。默认启用 `kubernetes/core,kubernetes/logs,robusta,internet`（`holmes/common/env_vars.py:29-31`）。镜像里带 kubectl、helm、argocd、curl、tcpdump、bash（`Dockerfile:47-64,107-138`）。
- 这个提交下**没有 Jaeger 工具集**（tree 里只有 grafana/tempo）。内置 `http` 工具集支持 hosts、paths、methods 白名单，默认只允许 GET（`holmes/plugins/toolsets/http/http_toolset.py:242-270`），`block_internal_ips` 默认 False（`:314-318`）。
- 这个提交**没有发布镜像**：最近的 release 是 0.45.0（2026-10-15）；dev 镜像只为 PR 构建，30 天后删除（`.github/workflows/docker-dev-images.yaml:117-129`）。只能自己从源码构建。Dockerfile 的基础镜像用的是 tag 不是 digest（`:14,82`），用了 alpine edge 仓库（`:27-30,122-126`），poetry 没固定版本（`:71`），还有 `apk upgrade`（`:107`），所以构建不可复现。许可证是 Apache-2.0（`LICENSE:1-3`）。
- 遥测和出口：`ENABLE_TELEMETRY` 默认 False，`SENTRY_DSN` 默认为空（`env_vars.py:64,70`），`ROBUSTA_API_ENDPOINT=https://api.robusta.dev`（`:51`）。CLI 的 `--trace braintrust|otel`，设置了 `OTEL_EXPORTER_OTLP_ENDPOINT` 就会自动开 OTel（`main.py:244`）。internet 工具可以抓任意网页（`internet/internet.py:386-394`）。
- 模型与预算对照：OpsPilot 请求 `deepseek-flash`，thinking 打开，effort high（`opspilot/investigation/loop.py:80,199-206`）；单 Run 最多 100 次模型请求，输出 65536，单请求 360s，Run 墙钟 7200s（`opspilot/investigation/limits.py:26,34`，后两项见 limits.py 同文件 `MODEL_REQUEST_TIMEOUT_SECONDS`/`RUN_WALL_SECONDS`）；工具 30s（`opspilot/tools/registry.py:78`）。Holmes 默认 `max_steps=100`（`holmes/config.py:113`），输出预留 16384（`env_vars.py:118`），`LLM_REQUEST_TIMEOUT=600`（`:166`），Prometheus 查询默认 20s、上限 180s（`prometheus.py:55-56`），thinking 和 effort 由 `THINKING`、`REASONING_EFFORT` 环境变量设置（`holmes/core/llm.py:740-755`），`TEMPERATURE` 默认 1e-8（`env_vars.py:84`）。Holmes CLI 有没有 Run 级墙钟：未确认。
- M0 先例：上游跑 5 次只有 1 次出报告（`docs/evidence/m0-real-environment/holmes-execution.md:3`）；LiteLLM 拒收顶层 thinking 参数，改为 extra_body 才行（`holmes-contract.md:15`）；trace 上传 0 次（`:5`）；用的是库方式加自定义四工具，不是 CLI（`holmes-contract.md:5-6,10`）。M0 的隔离证据是 UID 0 下的路径不可见，矩阵明确写了「不能替代非 root/独立 OS 隔离」（`docs/evidence/m0-real-investigation/m0-exit-matrix.md:85`；`round-07-container-isolation-refusal.txt`）。
- #93 正文是「正式 judge 校准与未见保留集」（`gh issue view 93`）；Holmes 隔离是挂在它下面的子项（`docs/tasks/2026-10-05-m0-entry-conditions.md:18,42`；SPEC 第 6、24 行）。所以这一步只能勾掉子项，不能关闭 #93。

## 必答类别对照

| 类别 | 结论 |
|---|---|
| 1 时间 | 待决，见 R4（可逆默认） |
| 2 计数与口径 | 待决：U6（步数口径差异）、R5 |
| 3 状态转移 | 待决：U7（终态投影）、R11 |
| 4 主体与权限 | 待决：U1、U2、U3、U4、U5 |
| 5 数据合同 | 已定：无 PG 表、无迁移、无部署顺序（只有证据文件，R8/R12） |
| 6 旧数据与已归档证据 | 待决：U1（F7/F3 冻结探测证据）；M0 `5e983c17` 证据不动（R12） |
| 7 并发与水位 | 待决：U1（共享 lab auth 文件波及其他 worktree）、R7 |
| 8 外部调用 | 待决：U6（模型与预算对齐）、U8（trace 出口）、R10、R11 |
| 9 验收投影与 passes | 待决：U7（第 1 步通过判据）、U9（digest 口径）；F14 `passes` 第 1 步之后保持 false |

## 需用户裁决

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| U1 | Holmes 查询用 POST，代理只放 GET/HEAD（`prometheus.py:1630,1884`；`kind_lab.py:376`）。不处理的话，Holmes 每次查指标都 403 | a. 新增 `holmes` 代理账号，nginx 按 `$remote_user` 只对 holmes 在 `query`/`query_range` 放行 POST（转发表单体，限 64k）。investigator/observer 不变，F7/F3 冻结探测照样成立。代价：`PROMETHEUS_ACCOUNTS` 和账号漂移规则要改（`kind_lab.py:130,272-288`）；共享 lab auth 文件变成 6 个账号后，其他 worktree 里的旧版 `kind_lab.py up` 会拒绝运行（先例见 `docs/evidence/f7-authz-probe/run.md`「与 main 版工具的兼容」）；`f7_authz_probe.py` 要加 holmes 的行。<br>b. 对所有账号放开 POST：探针证实 `test_kind_lab_readonly_proxy.py:61` 失败，F7 第 1 步和 F3 第 6 步的冻结证据要重新取，OpsPilot 的权限面也跟着变大。<br>c. 代理不改：基线没有意义。<br>d. 改 Holmes 让它发 GET：就不再是「未改动的上游」（违反 F14 第 4 步） | a | lab 代理配置、F7 探测、共享 lab 目录、其他 worktree | |
| U2 | 代理拒绝 Holmes 用的 `/api/v1/metadata` 和 `/api/v1/rules` | a. 保持 403，在映射表里记为实验因素（OpsPilot 也没有这两个接口，权限一致，符合 C3 `:403`「尽可能匹配权限」）。<br>b. 只对 holmes 放行：Holmes 拿到比 OpsPilot 更多的权限 | a | 权限对齐；映射表 | |
| U3 | trace 数据源：Holmes 没有 Jaeger 工具集 | a. 配置内置 `http` 工具集，只允许 GET Jaeger query API 的 host 和 `/jaeger/ui/api/*` 路径（`http_toolset.py:242-270`），属于「配置」类复用。<br>b. 不给 trace：Holmes 少一个 OpsPilot 有的数据源。<br>c. 自写工具集或 MCP：属于扩展，不再是未改动的上游 | a | 数据源对齐；映射表 | |
| U4 | Holmes 能用哪些工具，给不给 K8s 访问 | a. 配置文件显式只启用 prometheus/metrics 加 http(jaeger)，显式关掉 bash、internet、kubernetes/*、robusta 等；不挂 kubeconfig，不给 SA token。这和 OpsPilot 一致（产品不持有 K8s 凭据，#92 记为不适用，见 SPEC 2026-10-15 段）。代价：拿掉了 Holmes 最强的 kubectl 能力，要作为实验因素披露。<br>b. 给一个只读 RBAC SA（get/list/watch/logs）：Holmes 权限比 OpsPilot 大，新增凭据，基线对不齐 | a | 权限；基线可比性 | |
| U5 | 出口与隔离做法，以及什么算「隔离证据」（用来勾掉 #93 的 Holmes 子项） | a. 在 colima m1-kind 的 docker 里跑容器：internal 网络，只通 kind 节点上的 30090（代理）和 30686（Jaeger）；模型出口只走一个白名单转发代理（只允许 CONNECT `api.deepseek.com:443`）；非 root UID，根文件系统只读，`cap-drop ALL`，`no-new-privileges`，除只读配置和输出目录外没有宿主挂载，没有 docker.sock。证据清单：容器内实测 UID≠0；仓库路径、`.env`、lab auth 文件、`~/.kube` 都不存在；环境变量名只在白名单内（不打印值）；访问 kind API :6443、Prometheus 后端、任意外网都失败；模型域名只能经出口代理到达；代理访问日志显示 holmes 账号只用了允许的路径。<br>b. 以 Pod 跑在 kind 里加 NetworkPolicy：kindnet 支不支持 NetworkPolicy 未确认；而且 Holmes 就和被调查目标在同一个集群。<br>c. 独立 VM：最干净，但要花钱或更多内存。<br>勾子项：只在清单逐项通过后在 #93 评论勾掉子项，issue 本身不关（judge 校准和保留集仍挂在上面）；任何一项没过，基线只算开发参考（Q8） | a，清单如左 | 隔离判据；#93；SPEC 第 6 行入口条件 | |
| U6 | 模型与预算对齐到什么程度（第 1 步就对齐，第 4 步直接复用） | a. 只用环境变量和配置对齐，不改代码：`deepseek-flash` 走 `api.deepseek.com`，`THINKING={"type":"enabled"}`，`REASONING_EFFORT=high`，`MAX_OUTPUT_TOKEN_RESERVATION=65536`，`LLM_REQUEST_TIMEOUT=360`，Prometheus 查询硬上限 30s，容器外部墙钟 7200s 强杀，`max_steps=100`（Holmes 不把压缩和重试计入步数，OpsPilot 计入，这个口径差异要披露，见上游设计审计第 32 行）。先离线核对 LiteLLM 组出来的请求里 thinking 和 effort 都在（M0 在这里踩过坑；a0009195 下是否还有这个问题未确认）。<br>b. 用 Holmes 默认值：反映上游开箱即用的样子，但不满足 F1 第 4 步和 Q8「同一组预算上限」 | a | F1 第 4 步、F14 第 4 步可比性；费用 | |
| U7 | 第 1 步怎么算通过；Holmes 的输出怎么映射到 `IncidentOutcome` | Holmes 只输出自由文本 analysis，没有 ReportV2 和证据 ID（上游设计审计第 65 行），评测的确定性门（eval-suite 合同 D6）用不上。<br>a. 只投影不评分：`final_state`（completed/failed/budget_exhausted）、`report_available`、工具调用（Holmes JSON 输出加代理日志）、实际观察到的权限、模型调用与用量、`handoff_reasons`（一律为空）。通过判据：pin 记录齐全，隔离清单通过，一次 Run 到达终态，其中至少一次经代理的 Prometheus 查询成功、模型调用成功。诊断对不对不判；失败原样保留，Q10 允许重跑一次。<br>b. 第 1 步就要求诊断正确：等于提前做第 4 步，而且评分口径还没定。<br>Holmes 在第 4 步怎么评分（只用 judge，还是另加门），留到第 4 步预审再定 | a | F14 第 1 步验收；F14 `passes` 仍为 false | |
| U8 | Holmes 的 trace 要不要导出 | a. 不导出：清掉 `OTEL_*`、`BRAINTRUST*`、`SENTRY*` 这些环境变量，证据只用 Holmes JSON 输出、代理日志和摘要（M0 也是上传 0 次）。ADR-0006 要求附 LangSmith 链接的是改 OpsPilot loop 的 PR，这个 PR 不改 loop。<br>b. 用 `--trace otel` 导到 LangSmith：Holmes 的 span 里可能带完整消息和 `reasoning_content`，违反 C3 §12 `:450-452`，属于新增数据出口 | a | 数据出口；ADR-0006 | |
| U9 | F14 第 1 步说的「image digest」用什么 | a. 用未改动的 Dockerfile 从固定提交构建，记录本地 image ID（sha256）、`docker save` 产物的 sha256、构建时解析出的基础镜像 digest、包清单，不推送。注明构建不可复现（Dockerfile `:14,71,82,107`）。<br>b. 推到 GHCR 拿 registry digest：这是对外发布，要遵守 Apache-2.0 再分发义务，属于 Q10 用户门。<br>c. 改 Dockerfile 把 FROM 固定到 digest：可复现，但改了上游构建文件 | a | 验收措辞的解释；对外发布 | |

## 可逆默认（Agent 自定，一行理由）

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| R1 | 上游 checkout 放在哪 | 放 worktree 里 / 放主仓库 `tmp/upstream/holmesgpt-a0009195` | 主仓库 tmp，记录 tree SHA 和 LICENSE 的 sha256。理由：`git worktree remove` 会连 ignored 数据一起删 | 本地 | 默认 |
| R2 | runner 放在哪 | `scripts/f14_holmes_baseline.py` 加 `scripts/f14_holmes/`（配置模板、出口白名单） | 照 kind_lab 的写法做成工程脚本（PRODUCT-CONSTRAINTS 允许工程方的 harness）。理由：不进产品路径 | 仓库脚本 | 默认 |
| R3 | 用哪个场景和 prompt | `eval-payment-failure-1`，用 `kind_lab fault inject`（paymentFailure）；问题原文照抄（`evals/scenarios/eval-payment-failure-1/scenario.json`），只追加一行绝对 UTC 窗口，记录 hash | 照抄原文。理由：不为 Holmes 调 prompt；原文里写死的「json report」和过期的 target_id 作为实验因素披露 | 基线输入 | 默认 |
| R4 | 时间怎么定 | Run 开始时记录宿主、VM、容器三方 UTC 和注入时间戳，偏差超过 2s 就不跑；窗口和容差照评测的 Window 规则 | 理由：VM 休眠后时钟可能漂（kind lab 重启有先例） | 时间口径 | 默认 |
| R5 | 计数和费用怎么算 | 模型调用以 Holmes 输出的 usage 为准，出口代理的 CONNECT 日志做交叉核对；工具调用以 Holmes 输出为准，代理访问日志核对；费用按余额差，期间不跑其他真实 Run | 理由：AGENTS.md 规定按余额差对账 | 计数 | 默认 |
| R6 | 代理访问日志 | 在 U1-a 的配置里只给 holmes 账号开一个精简格式的 access_log（不记 Authorization）；`test_kind_lab_readonly_proxy.py:77` 跟着调整 | 理由：隔离清单需要服务端证据 | lab 配置 | 默认 |
| R7 | 并发与水位 | 串行跑；Holmes Run 和镜像构建都避开其他真实 Run 和观察窗；构建前查 swap（构建要从源码编译 confluent-kafka，见 Dockerfile `:17-30`；colima m1-kind 只有 8GiB） | 理由：本机 lab 的 swap 有过先例 | lab | 默认 |
| R8 | 证据怎么放 | `docs/evidence/f14-holmes-baseline/`：image.json、脱敏后的配置、isolation-probe.json、outcome.json、summary.json；原始输出留在 ignored 的 tmp，只入库 hash 清单；提交前跑 `scripts/check_secrets.py` | 理由：遵守 ADR-0006 的冻结摘要做法 | 证据 | 默认 |
| R9 | 凭据怎么传 | DeepSeek key 用 0600 的 env-file 在 `docker run` 时传入；代理密码写在只读挂载的 Holmes 配置 headers 里；都不进镜像层，用 `docker history` 和 inspect 自查 | 理由：C3 §12 凭据规则 | 凭据 | 默认 |
| R10 | 花费 | 1 次 Run，Q10 允许最多重跑 1 次；按余额差记录 | 理由：真实调用有常设授权（AGENTS.md）；M0 时 16 次 HTTP 的上界约 0.92 CNY | 费用 | 默认 |
| R11 | 失败映射 | Holmes 非零退出 → failed（记原因）；7200s 被强杀 → budget_exhausted(wall)；达到 max_steps → budget_exhausted；被出口拦截 → 在出口日志里记录 | 理由：失败照实写入分母 | 状态 | 默认 |
| R12 | 数据合同与旧证据 | 不建表、不迁移；M0 `5e983c17` 的证据不改；`docs/testing/m0-upstream-mapping.md` 第 2 步再更新 | 理由：第 1 步只有证据文件 | 无 | 默认 |
| R13 | 版本清单 | Run 记录里填 `upstream` 维度（`opspilot/evaluation/rounds.py:15`）：SHA 加 image ID | 理由：第 4 步直接复用 | 清单 | 默认 |
| R14 | 代理环境变量 | 容器内 `HTTPS_PROXY` 指向出口代理，`NO_PROXY` 填 kind 节点 IP；不继承宿主的 Clash 代理变量 | 理由：先例见 `kind_lab.py:209-223`；Holmes 的 requests/httpx 实际是否遵循这些变量未确认，以 U5 的负向探测为准 | 出口 | 默认 |

## 未确认

- 本机没有 a0009195 的 checkout，以上都是 GitHub API 读出来的原文。
- kindnet 是否支持 NetworkPolicy。
- Holmes CLI 有没有 Run 级墙钟。
- a0009195 下 LiteLLM 是否接受顶层 thinking 参数。
- Holmes 实际是否遵循 `HTTPS_PROXY`/`NO_PROXY`。
- `--json-output-file` 里到底有哪些字段（工具调用和 usage）。

## 第 2 步合同（r2）：逐功能上游映射表（#385）

- 预审：全新上下文 Agent（Opus 5.5），只读，基线 origin/main `62e3cde2`，2026-10-31；全文在仓库外 `~/.agent/handoffs/opspilot-prereview-385-f14-step2-2026-10-31.md`（下称「预审」，行号指该文件）。上游原文用本机完整 checkout（主仓库 ignored 的 `tmp/upstream/holmesgpt-a0009195`，HEAD `a0009195`、tree `e94a57b3`、`git ls-files` 1885 个文件）。
- 裁决（r2，用户 2026-10-31）：F14S2-Q1–Q5 全部按推荐 (a)；可逆默认 F14S2-R1–R14 照预审。记录见 `~/.agent/handoffs/opspilot-rulings-2026-10-31.md` §2。
- 写入时核对（main `cefa56c1`）：与基线之间只改了文档；`feature_list.json` 中非退役、非 F14 的 10 个功能合计 47 步（F1 5、F2 5、F3 6、F6 5、F7 5、F8 4、F9 5、F11 4、F12 4、F13 4）；SPEC:76 活动功能清单、`m0-upstream-mapping.md:11`、`docs/README.md:44`、上游 `holmes/common/env_vars.py:118` 均与预审一致。
- 全量探针（预审 :81-88）：只加两个草稿文档文件，`tests/acceptance` 探针 0 失败（2959 passed、302 skipped）；证明的只是新文件不误伤现有测试。

### 需用户裁决（r2 已裁决）

| 编号 | 问题 | 裁决要点 | 影响面 |
|---|---|---|---|
| F14S2-Q1 | 「每个活动功能」的粒度 | (a) 功能 × 验收步骤：SPEC:76 的 10 个功能（F14 自身除外，F4/F5/F10 已退役），每步至少一行；多机制拆子行，每行只归一类；覆盖面从 `feature_list.json` 推导 | 表规模；完整性断言 |
| F14S2-Q2 | 什么算「已确认缺陷」 | (a) 必须有 `a0009195` 运行证据（第 1 步已有的 Run/请求核对，或第 3 步复现）；只有源码观察的按 OpsPilot 当前依赖归类并加 `candidate_defect` 指向第 3 步，复现后改类、复现不出去掉标记 | 类别口径；第 2、3 步衔接 |
| F14S2-Q3 | 开 issue / 报上游 | (a) 第 2 步不向 HolmesGPT 报任何东西；只为 OpsPilot 要动手的缺陷在本仓库开 issue；上游报告留到第 3 步复现后单独问用户 | 对外通知；issue 卫生 |
| F14S2-Q4 | 未实现功能（F8、F9、F1 未完成步） | (a) 照样映射，标 `opspilot_state: planned`，结论只作 F8/F9 实施线的建议，不重开已裁决合同 | #384、#386 实施线 |
| F14S2-Q5 | 完成判据与测试 | (a) `mapping.json` 唯一权威 + 一页人读说明；非实现者写离线形状测试；独立审查对照 checkout 抽查 ≥15 行且四类都抽到；CI 不拉上游；F14 `passes` 保持 false | 新验收测试；F14 第 2 步判据 |

### 可逆默认（照预审，一行理由）

| 编号 | 默认 | 理由 |
|---|---|---|
| F14S2-R1 | 新建权威表与人读说明（路径见下）；`m0-upstream-mapping.md` 正文不改，只在文件头加一行注记指向新表；`docs/README.md` 加链接 | M0 草案对应旧 SHA，留作历史 |
| F14S2-R2 | 行字段与文件头见下方代码块 | 测试能逐字段断言 |
| F14S2-R3 | 四类各自的证据规则见下方代码块 | 证据充分性 |
| F14S2-R4 | 「上游没有」只在完整 checkout 上核查，命令与命中数记进行里；不用部分 clone 或单文件 API 抽查下「没有」的结论 | 1885 文件全树可查 |
| F14S2-R5 | 只认 `a0009195` 工件（pin/summary/request-check/isolation-probe/authz-probe）；M0 `5e983c17` 只算线索；第 2 步不做新的真实 Run，需要运行才能下结论的标候选交第 3 步 | 费用与 lab 窗口留给第 3 步 |
| F14S2-R6 | 10-22 审计的上游引用（同一 SHA）可搬，搬前逐条在 checkout 确认行号；OpsPilot 一侧按当前 main 重新核对（#307 A、#308、#309、#310、#311 已有实现）；09-07 审计只当线索；H1–H6 作 F1 各行上游证据；审计正文不改 | 审计 OpsPilot 侧对照的 `a4eea42` 已过时 |
| F14S2-R7 | OpsPilot 一侧用「路径 + 符号名」，不用行号；测试只检查文件存在；文件头记 `opspilot_commit` | 代码会漂移 |
| F14S2-R8 | 审计四类换算：「该照搬」且已移植 → 复用（`ported`）；「该照搬」未实现 → 按现状归类并链 issue；「理由仍成立」「无差距（OpsPilot 更强）」→ 必要扩展，`deviation_basis` 引审计原文；「理由已不成立」→ 按现状归类并注明；逐行人工判断 | 两套四类口径不同 |
| F14S2-R9 | 上游够用而 OpsPilot 自写的行照实写：`opspilot_choice: own_impl` + `deviation_basis`，不触发返工（已有 issue 除外） | 讲真且不推倒合理机制 |
| F14S2-R10 | 只从 OpsPilot 功能出发映射；上游有而 OpsPilot 没有的能力（K8s 工具、bash、MCP、skills 拉取）只写在相关行 `note`，不另列附录 | 步骤原文是 map every active feature |
| F14S2-R11 | 入库被引用上游文件清单（blob SHA + 行数，取自 tree `e94a57b3` 的 `git ls-tree -r`），由 `scripts/holmes_baseline` 新增离线子命令生成并本地校验行号；CI 只校验引用在清单内且行号不超行数 | CI 不需要 checkout |
| F14S2-R12 | 任务记录把 #93 的措辞改成「#93 的 Holmes 子项」（#93 标题是正式 judge 校准与未见保留集） | 文档准确 |
| F14S2-R13 | 按 F14 功能 PR 处理，分支 `feature/F14-385-upstream-mapping`；按决策稿 Q10 / SPEC:30 的 M3 授权，CI 绿、CLEAN、独立审查已处置后由 Agent 合并并事后简报；`passes` 不翻；不碰 loop，不需 LangSmith trace | ADR-0006 |
| F14S2-R14 | 提交前跑 `scripts/check_secrets.py --binary <gitleaks>`；blob SHA 与 commit SHA 同形，预计不误报（未实测） | 安全 |

逐字节细节（原文摘自预审，行号为预审文件行号）：

```text
# 预审 :61（F14S2-R1）
新建 `docs/evidence/f14-upstream-mapping/mapping.json`（权威）和 `docs/testing/f14-upstream-mapping.md`（人读说明：四类的计数、按功能的摘要、和审计的关系）。`m0-upstream-mapping.md` 正文不改，只在文件头加一行注记指向新表（做法同 `holmes-source-audit-2026-09-07.md:3`）；`docs/README.md` 加链接。理由：M0 草案对应旧 SHA，留作历史
# 预审 :71（F14S2-R11）
docs/evidence/f14-upstream-mapping/upstream-files.json
# 预审 :77（切分）
tests/acceptance/test_385_upstream_mapping.py
```

```text
# 预审 :62（F14S2-R2）
每行：`feature`、`step`（从 1 开始）、`mechanism`、`class`（`reuse`/`configuration`/`confirmed_defect`/`required_extension`）、`candidate_defect`（可选，带 `step3_ref`）、`opspilot_state`（`implemented`/`partial`/`planned`）、`opspilot_choice`（`ported`/`own_impl`/`upstream_config`）、`requirement_ref`（PRODUCT-CONSTRAINTS/C3/验收步骤原文出处）、`upstream_evidence[]`、`opspilot_evidence[]`、`runtime_evidence[]`、`deviation_basis`（可选，引审计原文的分类）、`note`。文件头：`schema`、上游 commit/tree、`opspilot_commit`。
```

```text
# 预审 :63（F14S2-R3）
**复用**：上游 `path:Lx-Ly`（a0009195）+ OpsPilot 移植处的路径。**配置**：上游的配置项出处 + 实际用的配置（`scripts/holmes_baseline/` 的配置）；只要该配置在 run1 里被用过，就附 run1 的 summary/request-check 作为运行证据，没用过的写「源码证据，未运行」。**已确认缺陷**：上游源码出处 + `a0009195` 运行证据（Q2）。**必要扩展**：需求出处 + 上游「没有」或「不够」的证据：「没有」附完整 checkout 上的核查命令与 0 命中，「不够」引上游最接近的机制，说明差在哪
# 预审 :64（F14S2-R4）
`git -C <checkout> grep -n -i <模式>` 或 `git ls-files \| grep`
```

### 切分与范围

单 PR `feature/F14-385-upstream-mapping`（预审 :76-79）：未参与实现的作者先按本表写离线验收测试（覆盖面从 `feature_list.json` 读、类别枚举、各类证据规则、清单校验）；实现者写 `mapping.json`、`upstream-files.json`、生成/校验子命令、人读说明、`m0-upstream-mapping.md` 注记、`docs/README.md` 链接与任务记录第 2 步节。完成前全新上下文审查者对照 checkout 抽查 ≥15 行（四类都抽到）。不含：第 3 步复现与回写候选缺陷行、任何对上游的报告。

### 未确认（预审 :98-104）

#307/#308/#310/#311 各自剩余未做的部分（#307 B 的 K8s 工具未实现）；上游 `conversations_worker`、`holmes_operator` triggered/scheduled healthcheck、skills 分别对应 F2、F11/F6、F13 的哪些步骤及类别；F8/F9 在上游的对应物（Helm chart、`docker-compose.yaml`、`usage_recorder.py`、`otel_tracing.py`）是否够用；预计行数（约 50–70）；秘密扫描未实测；run1 用到的每个上游机制能为哪一行的「配置」类提供运行证据。
