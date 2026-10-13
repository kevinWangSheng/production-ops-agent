# 场景评测数据

每个场景一个目录：`scenario.json` 只含被测模型可见的刺激（经 `opspilot.evaluation.investigator_input` 过白名单），`answer.json` 只给评测侧（expected answer、注入故障、证据特征规则），不得进入被测模型输入。合同见 [任务记录](../docs/tasks/2026-10-12-eval-suite.md) 与 [PR-A 接口](../docs/tasks/2026-10-13-eval-pr-a-interface.md)。

证据特征规则（r7 P8）首版只认可见错误 span：故障服务自身的 server span，或 checkout 上同一 RPC 的失败 client span。服务名与操作名取自 `docs/evidence/m1-01-alignment-c-effect/` 三个故障包中实际出现的错误 span。
