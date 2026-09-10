# 真实结构核验与离线投影依赖组合修复

日期：2026-09-10。范围：离线bridge/DTO/测试；0网络、模型、trace、PG或环境操作。独立quality失败不因结构结果改写；产品门槛与passes不变。

## 真实已执行Run的结构结果

使用各Run实际执行的完整`22a96b87cf6575c8246abeb7edbed5101e1c6951fcc62b5e8243b6e589c6efd8`源码快照，不使用最新候选重签旧工件：

- [fault01结构结果](round-02-fault01-bridge-result.json)：19份captured/19份实际交付view，绑定`m002-fault-01:http:17`，initial_views=0，结构无违例。模型自述conclusion=supported并不等于独立支持；[原质量审查](round-02-fault-outcome-review.md)保持。
- [normal03结构结果](round-02-normal03-bridge-result.json)：12份captured/12份实际交付view，绑定`m002-normal-03:http:20`，initial_views=0，结构无违例；[原质量审查](round-02-normal03-outcome-review.md)保持。

两次原命令均为主`.venv/bin/python -m scripts.m0.holmes_bridge --run-dir <相应真实Run> --projection-source-sha256 22a96b87cf6575c8246abeb7edbed5101e1c6951fcc62b5e8243b6e589c6efd8 --projection-source-file docs/evidence/m0-real-environment/runtime-sources/22a96b87cf6575c8246abeb7edbed5101e1c6951fcc62b5e8243b6e589c6efd8.py.txt --output <上述新结构结果文件>`，退出0。没有覆盖历史结果或读取provider私有记录。

## 候选组合失败与修复

对最新`e15d10cc9fc5ea523f8fd73af5e71e08596bc87481d6f78f1f0c8828ec783b63`wrapper离线调用trace-v3，先实际复现`NameError: name 'trace_projection_v3' is not defined`：旧bridge只抽取wrapper函数，没有加载被移出的纯函数依赖。

修复增加`ProjectionDependency`与显式可信依赖manifest入口，仅支持`legacy_projections`和`trace_view`两种模块。每个snapshot实际bytes必须匹配其SHA；只编译固定名称纯函数与trace的VERSION/DETAIL_KEYS字面常量，不执行模块imports/main。缺依赖改成固定`PROJECTION_DEPENDENCY_MISSING`，错hash/重复依赖拒绝。依赖来自可信调用方的manifest参数，不从raw/遥测字段发现代码路径。

wrapper、依赖和每份view继续保存各自projection revision/hash；EvidenceView另保存依赖hash集合摘要，Versions记录同一集合。支持metrics-v1/v2与trace-v2/v3显式版本调用；旧Run继续用旧完整snapshot。

候选使用[离线源码manifest](../m0-real-environment/round-02-offline-view-source-manifest.json)中的固定组合：

- wrapper `e15d10cc9fc5ea523f8fd73af5e71e08596bc87481d6f78f1f0c8828ec783b63`
- trace_view `0fdac927db8e633bdcae63d2dbacc988e3b3c5d66977ddddea22dbafb8b88f4b`
- legacy_projections `0ed7d37aeca6057f2fd59ca2da692cab73b1247bd0e209555200ef74f32e27fc`

最高seam回归在独立临时合成Run中生成metrics-v2、trace-v3实际纯投影及固定tool消息，再构建`IncidentScenario -> IncidentOutcome`检查。覆盖缺依赖、错SHA、raw中伪代码路径不执行、可见错误详情、缺series语义、最后请求引用与空initial_views。该合成组合不是新的真实模型Run或正式质量评价。

验证：`python -m pytest tests/test_m0_holmes_bridge.py tests/test_m0_outcomes_v3.py tests/test_m0_outcomes.py -q`（使用主既有.venv）为98项通过；随后强化trace报告引用的bridge/v3子集18项通过。Ruff与diff检查通过。最新本地hash见[清单](round-02-bridge-dependency-fix-hashes.json)，仍须独立复验。

新候选离线入口可增加`--projection-dependencies-manifest docs/evidence/m0-real-environment/round-02-offline-view-source-manifest.json`，同时提供匹配的wrapper snapshot和SHA。该参数不能用于把旧真实Run的raw/view升级后冒称重新取得运行证据。
