# 开发工具环境

- 状态：已完成（本批开发工具范围）
- 更新日期：2026-09-08
- 依据：[C3 技术栈](../design/technical-proposal-2026-09-07.md)、[开发工作约定](../../AGENTS.md)
- 工作区：现有项目工作区，用户批准的限定工具维护；保留原有 dirty 文档，不搬运或提交用户 WIP。

## 目标与范围

建立 Python 3.12、uv、Ruff、pytest 的开发入口和只读环境诊断。无产品实现、模型调用、Docker 启动或 Kubernetes 安装。AGENTS.md 开发命令段落已于用户确认后写入。

## 前提与完成条件

用户已授权除 AGENTS.md 外的工具文件与依赖准备。锁定依赖后安装验证；诊断能识别缺失前提，Ruff/pytest 可执行并保留非零状态，产品规范和验收清单不变。

## 必要上下文

[开发说明](../development.md)、[M0 计划](../plans/m0-validation-plan-2026-09-07.md)。开发工具就绪不等于实验环境完整或产品验收通过。

## 执行进展与证据

- `uv lock --python 3.12` 成功，生成 8 个解析包的锁文件；`make setup` 成功，安装 6 个开发包。实际版本：uv 0.10.8、Python 3.12.13、pytest 9.1.1、Ruff 0.16.6。
- 安装前真实 doctor 返回 1，报告项目环境未就绪；安装后 `make doctor` 返回 0。
- `make check` 返回 0：离线锁检查、Ruff lint、格式检查和 5 项 pytest 全部通过。新增文件用 Ruff 显式格式化后复验；check 本身不修复。
- 测试包含模拟的缺失/错版解释器、基础与实验状态区分、诊断层不写入，以及临时 Git 仓库的真实 CLI 缺失环境非零/前后快照不变验证。模拟单元测试不等于真实 CLI 证据。
- 真实空测试目录 pytest 返回 5。首次探测误将 venv 启动路径 resolve 到基础解释器，导致找不到 pytest；修正为保留 venv 启动路径后得到预期结果。产品工具入口未使用该错误路径。
- `doctor --lab` 报告基础环境可用、实验设施未就绪：Docker daemon 不可达、kubectl/Helm 不可用。没有启动服务或自动安装设施。
- 独立实文代码审查未发现阻断问题；其要求的真实 CLI 无写入验证已增加并通过。
- `git diff --check` 通过；AGENTS、CLAUDE、SPEC、PRD、feature_list.json 与本轮起点字节相同，11 项产品验收均未通过。证据属于 dirty 工作区工具验证，不是 M0 或产品验收。

## 下一步与交接

AGENTS.md 开发命令段落已获用户确认并写入；现有 Claude 加载验证缺口与 skills 适配留待后续。实验设施按对应 M0 任务准备。本批未启动服务、提交、合并或清理 worktree。Python 下载位于 uv 用户缓存，开发依赖位于项目 .venv；未修改系统默认 Python。

## 受检文件 SHA-256

- `.python-version`：`7b55f8e67b5623c4bef3fa691288da9437d79d3aba156de48d481db32ac7d16d`
- `pyproject.toml`：`d0766b991628f1a072b12b36093e95a4d01e755e56a6bdc1831ccf260bcb3b80`
- `uv.lock`：`b1322bf88861fac272300f69ae0e757da1f7981f7b2d37bd062a8140368e98e1`
- `Makefile`：`9d00533f371d9d4ffe41277ee25362d601aeffc7f525c7d76b15a5d20754dd7b`
- `scripts/doctor.py`：`60cbffdaa4db2cdae405fbb47805112072da49905380944d15a5847fff9c6c94`
- `tests/test_doctor.py`：`b3fbbc1099e32247c993c14d91ef042f5ca4895147d843d1a838985bcb99fcb6`
- `docs/development.md`：`957d70e673f3bb5b95872b6463ff644812f04264215f1ef3787aecb80dec0a5c`

## 命令导航确认

2026-09-08：用户确认开发命令段落，按展示版本写入 AGENTS.md；内容一致性、开发指南链接、Claude 单行引用及格式检查通过。本次仅修改导航与状态，不重复运行已通过的工具测试。
