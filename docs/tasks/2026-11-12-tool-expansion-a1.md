# 工具面拓展 A1：lab 只读集群身份与拒绝取证

- 状态：实施中（脚本与单元测试已写、本地定向测试通过，未推送；真实取证等租机 #580）
- 更新日期：2026-11-13
- 依据：[工具面拓展任务记录](2026-11-12-tool-expansion.md)、[决定表 r3](2026-11-12-tool-expansion-contract.md)（r1 的 N4、R4、R5、Q5、Q6，r3 的 A1-1 至 A1-10）、SPEC 2026-11-12 段、C3 第 3、8 节；上游 HolmesGPT `a0009195` 的 `helm/holmes/templates/holmesgpt-service-account.yaml`。issue #553。开工前预审在仓库外（A1 预审，2026-11-12），没有需要用户裁决的项。
- 工作区：worktree `/Users/shenghuikevin/dev/AI/poa-a1-impl`，分支 `feature/tool-expansion-a1-cluster-identity`（起点 `main` `52d640a0`）。

## 目标

给 lab 集群建一个只读身份，交给 worker 使用，并用一次拒绝取证证明它能读什么、不能读什么。A2 起的集群工具在真实 Run 里都用这份身份。

## 行为（公开接口）

- 起 lab 时自动建好只读身份并发一张 24 小时有效的 token；另有一个单独的命令只重发 token，不必重跑整套 lab。
- 这个身份能读整个集群的常见对象、事件、节点和 Pod 日志，包括 `kube-system`；能读注入故障用的开关 ConfigMap（按 Q5 披露）。
- 它读不了 Secret，改不了、删不了、建不了任何东西，不能 exec 进容器、不能走节点或 Pod 代理、不能 watch、不能给自己发新 token、不能冒充别的用户；没授权的自定义资源（Argo、Flux、Istio、Chaos Mesh 等）也读不了。
- token 过期后，集群工具会明确报「来源拒绝」，不会静默失败。
- 凭据和 CA 文件只放在 lab 私有目录（权限 600），证据里不出现 token 原文。
- 宿主上跑的 worker 用本机回环地址连集群；容器里的 worker 用节点容器名连集群。两种地址由同一处生成。

规则清单、文件位置、地址与 token 的细节以决定表 A1-1 至 A1-10 为准。规则取窄读法：只用上游模板里不带条件的规则，上游默认打开的自定义资源授权不搬（A1-2，与 Q6 一致）。

## 这个 PR 做什么

- lab 脚本：只读身份的 RBAC 清单，起 lab 时应用并发 token，单独重发 token 的子命令，宿主与容器两种形态的 env 与 CA 文件。
- 容器里的 worker 拿集群凭据：部署入口脚本的变量放行名单加上 `OPSPILOT_K8S_*`；lab 覆盖 compose 文件把 CA 文件挂进 worker（只挂 worker，web 与 observer 不挂）；写 worker 环境时重发的 token 与地址替换旧值，并重启 worker（A1-7）。
- 拒绝取证探测脚本，只用标准库、只从 env 文件读凭据，写类探测一律带 dry-run（A1-9）；落盘前自查输出，出现 token 原文或 JWT 形状就拒绝写入。
- 单元测试，不需要集群：清单不变量（只有 get、list，没有 secrets、通配、watch、exec、proxy、token）、env 文件权限与重发替换、探测脚本用替身传输时的判定与输出自查、入口脚本导出 `OPSPILOT_K8S_*`。

## 不做

产品侧的 Kubernetes 代码（A2）、token 自动续发、ITBench 集群、自定义资源、对 `kube-system` 做 Secret 以外的进一步收窄。不要把 kubeconfig 之类的字样写进 `opspilot/`（`tests/acceptance/test_f7_step1_entry_points.py:376-430` 会扫）。

## 完成条件

1. 脚本阶段（本机）：上面各项写完，单元测试与 CI 通过，`otel-demo` 档位不变，没有任何集群运行。合并后 #553 关闭。
2. 租机后：起 lab 成功发出 token；拒绝取证探测表（该读的 200、该拒的 403、在全集群 ConfigMap 里搜 lab 已知秘密值 0 命中）在宿主与 worker 容器两侧都全部符合；证据入库 `docs/evidence/a1-cluster-identity/`，gitleaks 通过，token 原文搜索 0 命中。之后 A2 的真实 Run 用这份身份。这一步归租机集中取证（#580）。

## 没有真集群时的已知风险

以下未确认，到租机上核对，可能返工一次：

1. 容器按节点容器名访问 API server 时，TLS 证书名是否对得上（按 kind `--internal` 的用法推断，未在 lab 验证）。
2. 24 小时有效期会不会被 API server 截短（以实际返回的过期时间为准）。
3. kind `v0.33.0` 默认的 Kubernetes 版本未确认；在该版本上 exec 的 GET 与 POST、dry-run 写请求、未授权自定义资源路径是否都回 403。期望值若要修，修正后只重跑一次并照实记录。
4. compose 对缺失的 secret 文件的实际表现（本机不能起 Docker）；按推断，CA secret 不放进基础 `compose.yaml`。
5. lab 的 ConfigMap 里有没有 lab 自己的凭据（预期没有，要实测）。

## 合并方式

A1 改 `deploy/` 里 worker 拿凭据的方式，属「数据流与凭据」类变更，按功能 PR 的用户门处理：简报里单列这一处，由用户合并或授权合并。

## 实施记录

自定的可逆细节（每条一行理由）：

- CA 与 server 用 `kubectl config view --raw --minify -o json` 读 lab kubeconfig：`kind_lab.py` 只用标准库，不解析 YAML。
- token 用 `kubectl create token ... -o json` 取 TokenRequest，过期时间取返回的 `expirationTimestamp`：API server 截短有效期时记录的是实际值（风险 2）。
- 宿主地址只接受 `https://127.0.0.1:<端口>`，否则拒绝：A1-6 写明回环地址，别的值说明 kubeconfig 不是 lab 的。
- `up` 在应用 Jaeger NodePort 之后、等待 Deployment 之前应用身份并发 token：演示应用慢或健康检查失败时也有身份可查。
- SA 设 `automountServiceAccountToken: false`：身份只给外部 worker 用，不该进任何 Pod。
- 容器形态的 CA 由 `control-secrets` 复制到 secrets 目录的 `k8s_ca.crt`，`compose.lab.yaml` 以 secret `k8s_ca` 只挂给 worker（`/run/secrets/k8s_ca`）：compose 文件不依赖检出目录与实例后缀。
- `worker_env` 里 token 与地址两行替换（重复的旧行删掉），CA 路径行沿用已有的补齐规则；lab 还没发 token 时不写任何 `OPSPILOT_K8S_*`（旧 lab 与现有测试不变）。
- 重启 worker 用 `docker compose -p <项目> -f compose.yaml -f compose.lab.yaml up --detach --no-deps --force-recreate --wait worker`，只在值变了或在跑的 worker 没挂 CA 时做，之后只把 worker 重新接入 kind 网络；项目名取 `--project`，否则 install.env 的 `OPSPILOT_PROJECT`，否则 `opspilot`。
- 入口脚本放行 `OPSPILOT_K8S_*` 通配，但名字含 `[A-Z0-9_]` 以外字符就跳过：名字会进 `eval`。
- 为配合独立验收测试的接缝（2026-11-13）：`k8s_api_url` 另收 `container=True/False`，不给 server 时直接读 lab kubeconfig 文件（JSON 或 kind 的 YAML，只认唯一一行 `server:`）；`kubectl create token` 参数改为 `--duration 24h` 分开写；输出不是 TokenRequest JSON 时按裸 token 处理，过期时间取 JWT `exp`，取不到就按请求时长推算，并在 `expires_at_source` 里写明来源；CA 只要求非空，不再要求 PEM 头；SA 去掉 labels。探测脚本提供 `authorization(env_file)`、`request(method, path, auth, *, env_file, headers, body)`、`probe(env_file=..., namespace=..., known=...)` 与 `--output`、`--rules-output`；`--known-secrets` 改为可选，不给时报告 `ok` 为假；exec POST 也带 `dryRun=All`；JWT 形状另认三段各 8 字符以上的点分串。
- 探测脚本完全独立（不 import `scripts.kind_lab`）：worker 镜像里没有 `scripts/`，容器侧用 `python -c "$(cat ...)"` 运行，已知秘密值用 `--known-secrets -` 从 stdin 给。
- 探测输出只记状态码、发现的 Pod/节点名、规则列表与计数；ConfigMap 搜索命中只记 `namespace/name/key`；token 本身也算已知秘密参与搜索。
- 输出自查：含 token 全文、token 中 16 字符以上的段、已知秘密值或 `eyJ...\.…` JWT 形状就整份拒绝，退出码 2，不写文件。
- `tests/test_413_compose_impl.py` 的 lab override 断言从整体相等改为只比 ports：worker 多了 `secrets`，端口不变。

偏离与已知限制：

- `install.sh` 不用 `compose.lab.yaml`（现有测试钉死），所以 install/upgrade 重建的 worker 不挂 CA，须之后再跑一次 `control-secrets`；命令会提示。
- exec 的 POST 行不带 `dryRun`（connect 请求没有这个参数），靠授权层先拒；期望 403 未在真集群验证（风险 3）。
- 写类探测期望 403 依赖「授权先于存在性检查」，例如 `deployments/checkout/scale` 的 Deployment 名未核对。
- 本机没跑任何集群、kubectl、docker 命令，也没跑 gitleaks（本机未装）；全量测试由 CI 跑。

## 下一步

由实现会话接手：脚本与单元测试可与 A2 并行写；真实取证与 A2 的真实 Run 一起等租机。
