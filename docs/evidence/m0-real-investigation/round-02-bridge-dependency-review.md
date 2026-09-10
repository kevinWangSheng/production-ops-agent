# Round 02 bridge 依赖加载独立复验

日期：2026-09-10 UTC。审查者：fresh-context `offline_view_review`，未参与 bridge 实现；已完成前序离线 view 审查，本轮只接续新增纯函数依赖机制。

结论：**此有界机制无阻塞发现**。18 项 bridge/v3 测试与独立边界探针通过。真实旧 fault/normal03 使用旧完整源码快照重放通过；这仅为结构一致性，不改变其独立报告质量 FAIL，不构成新真实调用或 M0 退出证据。

## 审查版本与范围

主实验 worktree `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01`。亲自重新计算并核对 `round-02-bridge-dependency-fix-hashes.json`：

- `scripts/m0/holmes_bridge.py`：`e1c9bca7351dddfa79ecff268c1cbe2257f994021b65b420e1fe2dcc62b03389`
- `scripts/m0/outcomes_v3.py`：`aa42ba5594648e7408a85e4ef80182ec7705d76fd011c0111f69ad43cddc68ee`
- `tests/test_m0_holmes_bridge.py`：`b977d3553d6871ea0f402d80b97594b9e5d7cd4f4e6ef5323244a7e043bba647`

检查显式 `ProjectionContext.dependencies`、可信调用方 manifest 入口、源码 hash 验证、固定函数/字面常量加载、投影版本分派及 EvidenceView/Versions 对依赖 hash 集合的记录。未改实现或提交。

## 亲自执行的离线证据

Python：`/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python`。

1. `-m pytest tests/test_m0_holmes_bridge.py tests/test_m0_outcomes_v3.py -q`：**18 passed，0.23 秒**。真实候选快照组合的合成最高 seam 覆盖 metric-v2/trace-v3、错误字段、缺失 series 语义、报告引用与空 initial_views。
2. 独立内存/临时文件探针：分别移除 trace_view、legacy_projections、全部依赖，非空 trace 均固定拒绝 `PROJECTION_DEPENDENCY_MISSING`；重复依赖拒绝 `PROJECTION_DEPENDENCY_DUPLICATE`；错 SHA 拒绝 `PROJECTION_DEPENDENCY_HASH_MISMATCH`；依赖文件 symlink 拒绝 `PROJECTION_DEPENDENCY_INVALID`。
3. 创建会写 sentinel 的临时代码文件，将其路径和有效 hash 塞入 raw 的 source_path/projection_dependencies。投影成功但 sentinel 不存在，证明这次实际路径注入未被加载。对显式可信测试依赖附加顶层 sentinel 写语句并提供匹配测试 hash，投影成功且 sentinel 仍不存在，核对非选中模块顶层语句没有执行。所有文件仅在 TemporaryDirectory 内生成。
4. 实际旧 Run 分别调用 `load_packet`，明确指定 `22a96b87cf6575c8246abeb7edbed5101e1c6951fcc62b5e8243b6e589c6efd8.py.txt` 和同 SHA，**不提供新候选依赖**。`check_outcome == []`；fault-01 captured/delivered 为 19/19，最终请求 `m002-fault-01:http:17`；normal-03 为 12/12，最终请求 `m002-normal-03:http:20`。对两个目录全部 `*-raw.json` 与 `*-tool-model-view.json` 计算前后文件 hash，均未改变。旧 Run 没有重新签名或升级视图。

## 判断和明确限制

新 wrapper e15d10 的调用依赖通过显式可信组合加载 trace_view 0fdac9、legacy_projections 0ed7d3，消除了只抽 wrapper 函数造成的依赖缺失。各依赖实际 bytes 与传入 SHA 比较，业务 raw 不决定代码路径；可见 revision/hash 继续与实投影绑定。

固定 AST 名称集合和字面常量抽取**不是任意不可信 Python 的安全沙箱**：已选函数本身及其定义仍必须是审核过的可信源码，SHA 证明完整性而非代码无害。当前入口明确把 manifest/context 交给可信调用方，故不把这一限制当成该修复的缺陷；未来若改为接受用户上传或遥测指定 manifest，必须重新审查权限边界。

本轮没有网络、模型、trace、PG、VM 或真实环境操作，没有读取 .env、真实 provider 私有记录或 reasoning；不重复 PG/报告质量试验。离线结构 PASS 不替代既有独立质量 FAIL，也不授权任何新的付费请求。
