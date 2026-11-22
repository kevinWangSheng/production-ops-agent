# 批次 1 凭据脱敏加固：开工前合同预审（2026-11-22）

- 预审者：独立 Agent，没参与实现，只读，没改仓库、没开 PR、没评论 issue。对照 `origin/main` `7ebb04fa`（`git fetch` 后核实）。范围：#574、#359、#365、#366、#783、#742。
- 读过：共用任务书 `~/.agent/handoffs/opspilot-next-batches-brief-2026-11-22.md`、AGENTS.md、PRODUCT-CONSTRAINTS.md（`:40`）、`docs/tasks/README.md` 合同预审节、C3 §5（`technical-proposal-2026-09-07.md:184-197`）与 §8（`:316`）、`docs/tasks/2026-11-12-tool-expansion.md:17,74,98`、`docs/tasks/2026-10-26-319-escaped-userinfo-redaction.md:42`、六个 issue 正文与评论（`gh issue view --json`）；代码 `opspilot/redaction.py` 全文、`opspilot/tools/{executor,evidence_read,otel_demo_ext,knowledge_fetch,kubernetes_core,kubernetes_tabular,redis_inspect,outcomes}.py` 的相关段、`opspilot/web/{app,service}.py` 的脱敏调用点；`docs/portfolio/{limitations,threat-model}.md`。上游 HolmesGPT `a0009195`（`tmp/upstream/holmesgpt-a0009195`）：工具输出不做脱敏（`holmes/core/tools.py:150-161` 只清理参数，`:313` 只遮 request_context）；工具集目录里没有 Redis【核实】。
- 标注：【核实】读过代码或实测过；【推断】从代码推出，没在实例上验证；【未确认】没核对。
- 草稿补丁与探针脚本（临时，可删）：`/private/tmp/claude-501/-Users-shenghuikevin-dev-AI-production-ops-agent/1919b61b-982e-4a65-b1db-3f3dc1fb5f6e/scratchpad/b1/`（`make_draft.py` 生成 `draft_redaction.py`；`compare.py`、`corpus.py` 是对照脚本；`only365/` 是只含 #365 改动的版本）。

## 全量探针

- **pytest 验收探针没跑**：`sysctl vm.swapusage` 已用 16389.56M / 17408M（16 GB，超过任务书的 10 GB 线），按任务书跳过，也没建探针 worktree。开工后让草稿 PR 的 CI 跑 `tests/acceptance`。
- 共享函数实测**做了**（纯 Python，负载很轻）：同一批输入分别过当前 `opspilot.redaction` 和草稿，结果如下。

### 典型输入：当前与草稿的输出（节选，56 条里 22 条变了，全部是下面列出的有意改动）

| 输入 | 当前 | 草稿 |
|---|---|---|
| `"prompt_tokens": 12` / `total_tokens=5` / `max_view_tokens=25000` | 值被换成标记 | 原样（#366） |
| `sessionAffinity: None` / `SESSION_TIMEOUT=30` | 值被换成标记 | 原样（#366） |
| `session_id=abc` / `session=abc` / `auth: ok` / `token=abc123` / `session_key=zzz` | 标记 | 标记（不变） |
| `sessionid=abc` | **原样（当前漏了）** | 标记 |
| `DB_PASS=pw1` / `SMTP_PWD=pw2` | **原样（当前漏了）** | 标记（见 B1-D4） |
| `https:%2F%2Fuser:s3cret@db.example.com`、`https%3A%2F%2Fuser%3As3cret%40db…`、`\\\/\\\/user:s3cret@…`、`&#x2F;&#x2F;user:s3cret@…`、`&#47;&#47;user:s3cret&#64;…`、`&sol;&sol;default:pw@redis` | 原样（漏） | 前缀照原样 + 标记 + `@` + host（#359） |
| `/search?q=a%40b.com&page=2`、`email=alice%40corp.com`、`redirect=https%3A%2F%2Fexample.com%3Fnext%3Dbob%40x.com`、`next=%2F%2Fcdn.example.com%2Fa%40b.png`、`<a href="&#x2F;docs&#x2F;x">`、`&#x2F;&#x2F;cdn.example.com&#x2F;img.png`、`mailto:bob@example.com`、`git@github.com:org/repo.git` | 原样 | 原样（不误伤） |
| `{"name":"DB_PASSWORD","value":"hunter2"}`、`{"name": "AUTH_TOKEN", "value": "t0k"}` | 原样（漏） | `value` 换成标记（#574 文本形） |
| `{"name":"DB_HOST","value":"postgres:5432"}`、`{"name":"metric","value":"3"}` | 原样 | 原样 |
| 日志与普通文本（`level=error msg=… addr=10.0.0.1:5432`、`image: ghcr.io/…:1.11.1`、`key: value`、`author: bob`） | 原样 | 原样 |

草稿对这 56 条都幂等（二次脱敏不变）。`access_tokens=abc,def` 当前与草稿都只遮到 `abc`、留下 `,def`，是已有的限制，不在本批范围。

### Pod 对象（`redact_payload`）

当前：`env` 里 `DB_PASSWORD`、`API_KEY`、`AUTH_TOKEN` 的值原样出现；`sessionAffinity`、`prompt_tokens` 被换成标记。草稿：前三个值换成标记，`DB_HOST`、`LOG_LEVEL`、`SESSION_TIMEOUT` 原样，`secretKeyRef` 引用名照 #775 可见，`sessionAffinity`、`prompt_tokens` 恢复原值。`kubectl.kubernetes.io/last-applied-configuration` 注解（JSON 写在字符串里，带着 env 原值）草稿也能遮住【核实】。

### 真实语料对照（上游 Holmes 测试夹具 + SREGym 清单，共 1222 个 YAML 文档）

19 个文档的输出有变化，逐条：
- 13 处 `spec.template.spec.containers[].env[].value`（如 `fake_password`）、1 处 flagd sidecar 的 env 值：当前原样、草稿遮住（#574，目标内）。
- `max_tokens: 4000`、`server.service.sessionAffinity: None`、Kafka 消费者配置 `session_timeout_ms=30000`、Keycloak `ssoSessionIdleTimeout: 1800`、`ssoSessionMaxLifespan`、负载阈值 `session_post/session_login/…`：当前被遮、草稿恢复（#366，目标内）。其中 `session_timeout_ms` 就是 Kafka 消费延迟类题目要看的配置。
- **4 处 `calibration.golden_pass: 3/3` 被草稿新遮住：这是误伤**，来自把 `pass` 加成通用凭据词。所以 B1-D4 改成只在 env 名字上认 `pass`/`pwd`。

### #365 单独改动

只改 `_QUOTED_ASSIGNMENT` 的 scalar 分支（匹配值的第一个字符，不再吞到值尾），1222 个语料文档 0 变化、56 条输入 0 变化；20 万条随机短串模糊对照 1 条不同：畸形输入 `…apasswordb="token":#{\:b`，新版多遮了一段（更安全的方向）。耗时：

| 输入 `"a:b"*n` | 当前 | 草稿 |
|---|---|---|
| 16 KiB | 0.16 s | 0.01 s |
| 64 KiB | 2.52 s | 0.05 s |
| 512 KiB | 估计约 150 s（issue） | 0.50 s |

草稿新加的规则在 512 KiB 病态输入上：`%2F%2F…` 0.05 s，`&#x2F;&#x2F;…` 0.07 s，多重反斜杠 0.07 s，重复 env 对 0.14 s，都是线性。4 MiB Pod 列表（2514 个 Pod）整体过一遍草稿 `redact_payload` 0.90 s（B1-U2 要用）。

### 预计会动的钉值与证据型验收（按 grep，没跑）

- `PROJECTION_REVISION` 字面值：`tests/test_m1_report_contract_citations.py:197,201`、`tests/acceptance/test_f7_step3_source_secrets.py:335,354`、`tests/acceptance/test_f7_step3_source_secrets_postgres.py:182`；`tests/acceptance/f3_step5_rubric_gates_support.py:521` 是合成夹具里的字面值，评估代码不拿它和常量比（`opspilot/evaluation` 里没有比对），可以不改。
- 升 projection 会改所有档位的 `tool_schema_revision`（`otel_demo_ext.py:526`、`otel_demo.py:1073` 把它算进哈希）：20 个测试文件里 991 处 `otel-demo-ext-<hash>` 钉值，加 `tests/fixtures/{c6b_sql/pg_mysql_pins.json,c6c_mongo/pins.json,redis_773/pins.json}`。每次升都要机械重算一遍。
- F3 第 5 步等价证明（`docs/evidence/f3-step5-competing-cause/v11-equivalence.json`，脚本 `scripts/f3_surface_equivalence.py`）：#366 会让它记的「网关拒绝视图里 `max_view_tokens`/`view_tokens` 被二次改写」那一条消失。F3 维持 `passes=false`、不再为 F3 投入（已裁决），所以不重做等价证明（B1-D9）。
- 断言当前过度脱敏的测试：没找到断言 `*_tokens`、`sessionAffinity` 被遮的；`tests/test_f7_observer_redaction.py:47,64` 断言标签 `session`（单词）被遮，草稿保留这一条【核实】。

## 需用户裁决（4 项）

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| B1-U1 | **Pod 环境变量的值，哪些对模型隐藏**（#574，重开 2026-11-12 的延期）。现在：`env` 里写死的值全部原样进证据库和模型输入，`DB_PASSWORD=hunter2` 模型也能看到【核实】。上游 Holmes 全部原样显示（工具输出不脱敏） | **a. 按名字遮**：变量名像凭据（含 password/secret/token/api_key/auth 等，沿用现有凭据名规则，env 名字上另认 `PASS`、`PWD`）的，值换成标记；`DB_HOST`、`LOG_LEVEL`、端口、开关类变量照常可见。代价：起了普通名字的秘密（如 `STRIPE=sk_live…`）还是会漏，和现在其他字段同一类限制。**b. 全部遮**：所有写死的 env 值都换成标记，只留名字。代价：「环境变量配错了」这类故障（主机名、端口、开关写错，评测和 SREGym 里常见）模型就看不到值，诊断不出来。**c. 不做**，维持延期 | **a**。遮的位置：结构化对象里 `env[]` 的 `{name, value}`；JSON 写成字符串的地方（`last-applied-configuration` 注解、表格工具的单元格）。环境变量名本身、`valueFrom` 引用名照 #775 可见 | `redaction.py` 共享规则；C3 §8 `:316` 那句「Pod 环境变量的值也未覆盖（#574）」改写；`docs/portfolio/{limitations.md:59,threat-model.md:29}`；F7 `passes` 不变 | |
| B1-U2 | **模型自己写的 jq / 列查询绕过脱敏怎么办**（随 #574 发现，现状缺口）。现在：`kubernetes_jq_query` 和 `kubernetes_tabular_query` 先在**没脱敏**的原始对象上跑模型给的表达式，脱敏只作用在输出上（`kubernetes_core.py:1379-1382`、`kubernetes_tabular.py:225-244`，`executor.py:1512`）。所以模型写 `[.items[].data.DB_PASSWORD]`（ConfigMap 可读，`kubernetes_core.py:223`）或 `.spec.containers[].env[].value`，拿到的是光秃秃的值，旁边没有名字，规则认不出来，值就进了模型输入【推断，按代码路径】。这与 PRODUCT-CONSTRAINTS `:40`「数据源结果按名称上下文脱敏后才入库、给模型」不符 | **a. 先脱敏再跑查询**：集群对象读回来后先整体过一遍脱敏，再交给 jq 或列提取。代价：模型按被遮字段筛选（例如 `select(.data.API_TOKEN=="x")`）会筛不到；4 MiB 读取上限下多 0.9 s（实测）。**b. 不改**，在限制文档写明「jq 能取出不带名字的值」 | **a** | `kubernetes_core.py`、`kubernetes_tabular.py` 各一处；安全测试加一条变异（jq 取值、列取值各一例）；工具描述可不改 | |
| B1-U3 | **收窄过度脱敏，哪些字段恢复可见**（#366）。现在：名字里含 `token` 的计数（`prompt_tokens: 12`、`max_tokens: 4000`）、含 `session` 的配置（K8s Service 的 `sessionAffinity`、Kafka 的 `session_timeout_ms`、Keycloak 的 `ssoSessionIdleTimeout`）、`auth: ok` 的值都被换成标记【核实】。trace 里模型实际看到的字节与导出也因此不一致 | **a. 只放开两类**：名字以 `tokens` 结尾**且值是数字**的（计数；`access_tokens` 这类值不是数字时照遮）；`session` 只在作为名字最后一个词或后面跟 `id`/`key`/`cookie` 时算凭据（`sessionAffinity`、`SESSION_TIMEOUT` 放开，`session_id`、`session`、`sessionid` 照遮，`sessionid` 当前反而漏了）。`auth`、`session_id` 维持遮。**b. 在 a 基础上连 `auth`、`session_id` 也放开**（issue 列了它们）。代价：`auth: user:pass`、Web 会话号会漏。**c. 不改** | **a** | `redaction.py` 名字规则（也影响工具登记的参数名拒绝和告警标签处理，同一定义）；升 projection revision | |
| B1-U4 | **URL 里带账号密码，再多认哪些编码写法**（#359）。现在只认字面 `//` 和 JSON 转义 `\/`；百分号编码、多重反斜杠、HTML 实体写法的 `user:pass@host` 原样进模型【核实】 | **a. 三种都认，每种单独一条规则**：百分号（`%2F%2F` 开头，`@` 可以是 `%40`）、任意层反斜杠（`\\\/`）、HTML 实体（`&#x2F;`、`&#47;`、`&sol;`，`@` 可以是 `&#64;`、`&#x40;`、`&commat;`）。前提是前面必须有编码后的 `//`，所以查询串里普通的 `%40`（邮箱）、带 `?`/`#` 的跳转地址、HTML 里普通的 `&#x2F;` 路径不受影响（实测 8 条不误伤）。输出与 #319 同形：前缀照写、标记、`@`、host。不认：混合写法（`%2F/`）、二次百分号（`%252F`）、base64，写进已知限制。**b. 先解码再匹配**：覆盖面大，但要把整段文本解码再编码回去，输出字节难保持原样、幂等难证明。**c. 只认百分号** | **a** | `redaction.py` 三条正则；升 projection revision；限制注释改写（`redaction.py:104-108`） | |

## 必答类别（docs/tasks/README.md 合同预审 1–9）

| 类 | 状态 |
|---|---|
| 1 时间 | 不涉及数据时间。唯一的时间项是 Redis DNS 解析超时（B1-D11），沿用现有连接超时（`redis_inspect.py:893-896`） |
| 2 计数与口径 | #366 的「计数」口径见 B1-U3 a：名字以 `tokens` 结尾且值是数字。#365 的口径：病态输入单遍线性，512 KiB 测试加 `"a:b"*n` 这一形态（现有 512 KiB 测试没覆盖，issue 正文） |
| 3 状态转移 | 唯一的状态影响：升 `PROJECTION_REVISION` 后，在途 Run 被重新领取时进入 `blocked(INCOMPATIBLE_STATE)`（C3 §5 `:193-197`，不得为避免而不升，已定）。没有新状态 |
| 4 主体与权限 | 不改角色、凭据、只读账号。脱敏在 worker 的执行器里（`executor.py:1512`），web 展示时再过一遍（`web/app.py:1453,1495,1534`）。Redis CONFIG GET 黑名单加 `primaryuser`（B1-D10）是收紧 |
| 5 数据合同 | 不改库表、不迁移。只改常量 `PROJECTION_REVISION`（`outcomes.py:70`，证据行已有 `projection_revision` 列）。部署顺序：web 与 worker 同一镜像一起升 |
| 6 旧数据与归档证据 | 库里已有的证据行保留当时的脱敏结果，不回写（原始字节有哈希）；web 展示时按新规则再脱敏一遍，所以 #574/#359 的新规则对旧行的**显示**也生效，#366 放开的字段在旧行里已是标记、恢复不了【推断】。`docs/evidence/` 下的 summary.json 不改 |
| 7 并发与水位 | 不涉及并发。耗时见探针：#365 修好后 512 KiB 0.5 s；B1-U2 a 每次集群读取多约 0.9 s / 4 MiB，在单次调用 30 s 内 |
| 8 外部调用 | 不新增数据源、模型或出口。Redis：DNS 解析目前在看门狗启动前、没有超时（`redis_inspect.py:481`，看门狗在 `:910` 才起），执行器只把超时传给传输层（`executor.py:842`），没找到外层强制中断【推断；Run 级别是否另有看门狗【未确认】】 |
| 9 验收投影与 passes | `feature_list.json` 不翻：F7 维持 `true`（第 3 步「模型输入、证据导出、trace 里没有凭据」，本批是收紧），F3 维持 `false`。验收步骤文字不改。新增安全测试进 `tests/safety`，每条新规则配一个变异（现有最大编号 M46，`tests/mutation/mutations.py`） |

## 可逆默认（实现者自定，15 项）

| 编号 | 问题 | 默认 | 理由 |
|---|---|---|---|
| B1-D1 | 每个 PR 升不升 projection revision | 改变模型可见字节的 PR 各升一次（#365 也升：模糊对照有 1 条畸形输入输出变了）。#742 不升（可达输入字节不变，PR 里写明）。991 处钉值和 3 个 pins.json 机械重算 | C3 §5 `:195-197` |
| B1-D2 | 之前漏升的两次 | #710（`09bd4daf`）和 #775（`a17887e0`）都改了模型可见字节但没升（两次提交都没动 `outcomes.py`）【核实】。第一个升版的 PR 在 `outcomes.py` 的版本注释里补记这两次 | 同上；只补注释，不改行为 |
| B1-D3 | #365 修法 | `_QUOTED_ASSIGNMENT` 的 scalar 只匹配值的第一个字符，值由 `_value_redaction`/`_SCALAR_VALUE` 读（与 `_CREDENTIAL_ASSIGNMENT` 同一写法）；512 KiB 线性测试加 `"a:b"`、`/a:b`、`ua:b` 三种形态 | 探针：语料 0 变化，64 KiB 2.52 s → 0.05 s |
| B1-D4 | `PASS`/`PWD` 算不算凭据词 | 只在 env 变量名上算（`DB_PASS`、`RABBITMQ_DEFAULT_PASS`、`SMTP_PWD`）；不加进通用名字规则 | 加进通用规则会误伤 `golden_pass: 3/3`（语料实测 4 处） |
| B1-D5 | env 规则的位置 | 结构化：`redact_payload` 里父字段是 `env` 的对象，`name` 是凭据名时把 `value` 换成标记（与 #775 的按父字段判断同一机制，`redaction.py:507-511`）；文本：`"name":"X","value":…` 相邻（规范 JSON 按键名排序，`name` 总在 `value` 前）。`value` 在前的写法、更深层转义的 JSON 不认，写进限制注释 | 草稿实测；`kubernetes_tabular._text` 用 `canonical`（排序键） |
| B1-D6 | env 里数字值的计数豁免 | `MAX_TOKENS="4096"` 这类 env 值照遮（env 值都是字符串，不套 B1-U3 的计数豁免） | 简单、偏安全；草稿当前就是这样 |
| B1-D7 | #359 输出里 `%40`、`&#64;` 怎么写 | 换成字面 `@`，与 #319 同形（前缀照写、标记、`@`、host） | issue 要求同形 |
| B1-D8 | `imagePullSecrets[].name` | 与 #775 的三处引用名同样放开（拉镜像失败时要知道引用了哪个 Secret），只放 `name` 一个字段，其余照遮。若认为不在 #775 裁决内，删掉这一条即可 | 2026-11-22 裁决「Pod 引用的 Secret 名字可见，值永不可见」；当前整段被遮（`imagePullSecrets` 名含 secret）【核实】 |
| B1-D9 | F3 等价证明 | 不重做；PR 正文写明 #366 会去掉 v11 等价证明里那条「拒绝视图键名被改写」 | F3 `passes=false` 不动，不为 F3 投入（已裁决） |
| B1-D10 | #783 第 2 条 | 黑名单加 `primaryuser`；`primaryauth` 已被 `auth` 子串覆盖，测试里写一条确认 | 与 `masteruser` 对齐（`redis_inspect.py:648`） |
| B1-D11 | #783 第 3 条 | DNS 解析放进连接阶段并受同一个连接超时约束（守护线程里跑 `getaddrinfo`，超时就是 `REDIS_UNAVAILABLE`，`source_contact` 照连接失败处理） | 现有失败分类（`redis_inspect.py:899-903`） |
| B1-D12 | #783 第 1 条 | 读满 8 KiB 时，先去掉末尾不完整的 UTF-8 字符（最多 3 字节）再解码，照旧标记截断；真正的二进制值仍是 `0x` | issue 修法；修后文本值能被脱敏规则看到 |
| B1-D13 | #783 第 4 条 | 不做：非 UTF-8 键名不能回传给另两个工具，记为已知限制（任务记录一行），不改工具参数、不改描述 | 少见；改参数会动 `tool_schema_revision` 和 pins，收益小；上游没有 Redis 工具可照搬 |
| B1-D14 | #742 修法 | `read_evidence` 切片的 `citable_as_fact` 取父工具登记的值：`EvidenceReadTransport` 在 `carried` 里带上父工具的 `citable_as_fact`（与 `result_paths` 一起在 `otel_demo_ext.py:1037` 建表），执行器 `executor.py:1838-1840` 再与它相与；`_carried_problem` 同步认这个键。不选「把 `fetch_skill` 从可切片工具里去掉」，那样超限的 runbook 结果就读不回来了 | issue 修法；目前只有 `fetch_skill` 登记为不可引用（`knowledge_fetch.py:482`） |
| B1-D15 | 真实 Run | 本批不改调查循环、报告校验、恢复路径，AGENTS 的「附真实 Run trace」不强制。#574/B1-U2 的 PR 如本机 kind 实验可用，补一次有界真实 Run（Pod 带凭据名 env 值、ConfigMap 带凭据键），证明模型输入与 trace 里没有该值；本机 swap 紧时不跑，PR 里写「未跑」。不需要租机 | AGENTS 验证节；本机 swap 现状 |

## PR 拆分与顺序（不叠 PR，前一个合了再开下一个；前四个都改 `redaction.py`）

| 序 | PR | 内容 | 证据 | 合并类别 |
|---|---|---|---|---|
| 1 | `fix/365-linear-quoted-scalar` | B1-D3；升 v12；B1-D2 补注释 | 线性测试三形态；语料对照 0 变化 | 代码缺陷修复，审查干净后 Agent 合并（2026-11-13 起的做法）；不改 C3 |
| 2 | `fix/366-narrow-credential-names` | B1-U3；B1-D8；升 v13 | 典型输入对照表；安全测试 + 1 变异（`access_tokens` 非数字值仍遮） | **用户门**（凭据数据流：放开模型可见字段） |
| 3 | `fix/359-encoded-userinfo` | B1-U4、B1-D7；升 v14；限制注释 | 每种写法 1 条样例 + 不误伤样例；安全测试 + 1 变异 | **用户门**（凭据规则覆盖面） |
| 4 | `fix/574-env-values` | B1-U1、B1-U2、B1-D4–D6；升 v15；C3 `:316`、limitations、threat-model 改写 | 合同测试（结构化、注解字符串、jq 取值、列取值）；安全测试 + 变异；可选 kind 真实 Run（B1-D15） | **用户门**（C3 与凭据数据流） |
| 5 | `fix/783-redis-followups` | B1-D10–D13 | `tests/test_redis_inspect.py` 单测；假 DNS 卡住的超时测试 | Agent 合并（工具缺陷，收紧） |
| 6 | `fix/742-slice-citable` | B1-D14 | 合同测试：不可引用工具的切片 `citable_as_fact=false`，普通工具切片仍 `true` | Agent 合并（不可达缺陷）；触碰报告引用资格，按 AGENTS 要独立审查 |

5、6 不碰 `redaction.py`，可以和 1–4 并行做，按就绪先后合。每个 PR 的验收与合同测试由未参与实现的全新 Opus Agent 写（AGENTS 独立审查节）。

## 建议移出本批或放弃

- #783 第 4 条（非 UTF-8 键名回传）：建议不做，只记限制（B1-D13）。
- `access_tokens=abc,def` 只遮到第一个逗号：已有限制，本批不处理，必要时另开 issue。
- F3 等价证明重做：放弃（B1-D9）。
