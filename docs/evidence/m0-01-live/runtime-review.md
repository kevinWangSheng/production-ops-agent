# M0-01 Python 与已安装依赖绑定：独立复核

2026-09-09；由未参与实现的独立 Agent 复核。基线 `d14d09c0a29e45b4f3bf1a9cf02e4967e7a92458`，仅覆盖新增 `scripts/m0/runtime.py` 及 `scripts/m0/live.py`、`tests/test_m0_live.py` 的运行环境绑定变更。沿用 SPEC 实施/授权门槛及 C3 §5/12；本审查不授权真实运行。

## 结论与范围

未发现本次窄改动的阻塞缺陷。合同要求批准的 Python 完整版本及实现与当前 CPython 一致，当前版本还必须满足锁文件 Python 范围。已安装发行包版本逐项对照批准源码摘要所覆盖的 uv.lock；任何版本不符、包缺失或解析错误均在账本 claim 前拒绝。检查默认 dev/m0 依赖闭包，含传递依赖及 psycopg binary extra，平台 marker 决定活跃分支；多版本锁显式拒绝而非猜测选择。

这是发行包 metadata 版本校验，不是安装文件哈希、已加载模块字节或恶意本机环境的完整性证明。未来锁格式、extra marker 或多版本解析需求变化仍需相应测试和审查；当前锁中 marker 与 extra 结构已实际检查。不改变预算占用、响应模型约束、数据出口、产品门槛或旧批准不自动迁移的规则。

## 验证证据

- 独立执行 `.venv/bin/python -m pytest tests/test_m0_live.py -q`：**44 passed in 1.30s**。覆盖五个发行包漂移、Python 版本不符及原模型/授权/trace 边界。
- 额外离线探针捕获当前平台实际检查的 **34 个活跃发行包**；对每包分别注入版本 `0.0.0` 和 `PackageNotFoundError`，共 **68 种**情形均以 `LIVE_RUNTIME_MISMATCH` 拒绝，MemoryLedger 未 claim。全部通过真实 `execute` 路径、Mock/合成配置及 `no_network()`。
- 活跃闭包包含 `psycopg-binary`、`packaging` 和各直接/传递运行依赖；当前平台不读取 `colorama`、`tzdata` 或 `httpx2-jsfetch` 版本。
- 用合成 marker 环境及锁内版本 metadata 模拟 Linux/Windows：分别检查 **34/36 包**，Windows 纳入 `colorama` 与 `tzdata`，两个平台均纳入 binary extra 并排除 jsfetch。这是解析分支模拟，不是跨平台实际安装或运行证明。
- 静态确认 `check_runtime` 在 `validate` 内、`ledger.claim` 前执行；`code_digest()` 包含新增 runtime.py 与锁文件，完整批准合同继续进入持久合同摘要。

未读取真实凭据或批准文件、未联网、未执行 live CLI、未触碰 PostgreSQL。远程最新提交 CI 和机器人审查仍须主流程等待与核对，本地结论不代替 PR 审查闭环。

## 已审文件 SHA-256

- `scripts/m0/runtime.py`：`7e6a024fbf0df437b73a59f5a069ac154c3087944bf8b707ee6f6dde7a82bab5`
- `scripts/m0/live.py`：`227af86356a445ca8678bf97ba7158758cd20562b918ae0eb02026bc1db90826`
- `tests/test_m0_live.py`：`19115c1e7f8c77e3c034b7072bddbdddaca9f31928e88b57c6a8401d733d7b0d`
