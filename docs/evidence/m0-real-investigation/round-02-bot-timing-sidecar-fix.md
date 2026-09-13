# PR16 timing sidecar 原始时钟绑定

基线cec52d2850bc457a7619b3f03ffd62f2b64fc40d；GitHub bot发现3979271073，位于旧wrapper的initial-timings-file覆盖路径。用户要求本轮本地双轴审查结束后改回bot；本修复由实现者自测并等待bot复审，没有再次启动本地code-review skill。

## 问题与修复

原bundle已逐字段核验raw时钟，但sidecar仅验证view hash/可见事件范围，再覆盖registered_timings。实际旧wrapper探针接受了raw缺时钟时sidecar补造的完整采集时间，随后断言失败；原始红输出保留。

保留CLI兼容参数，但降为已验证Timing的回显：view身份和完整规范化Timing必须相等，不再替换registered_timings。它不能补造、修改或擦除capture/source时钟及依据，合理但未由bundle证明的时间也不接受；需要其他时间依据时必须先通过原bundle验证。等价UTC编码允许通过。小型纯边界函数由真实CLI调用，source/permission/历史数据不改，不引入新时钟权威或平台。

## 自测

实际固定Holmes解析/运行路径配合fake pipe覆盖11场景：raw missing/null/known与invent/match/erase/equivalent/source-bounds/unknown-id/no-flag。拒绝场景credential_reads=0、fake_transports=0；合法场景仅fake transport=1。所有原source bundle逐文件hash不变，0真实HTTP/trace/后端。基线红、修复绿输出见本目录对应文件。

新增14项CI可运行纯合同测试；完整隔离代码615 passed/44 PG默认skip（18.90s），ruff lint/format通过。本修复未运行PG/服务。探针共享fixture不能要求Holmes环境安装pytest；测试准备时曾误加pytest顶层import导致ModuleNotFoundError，已将pytest用例移至独立测试模块、原fixture逐字恢复，未安装依赖。编写测试时缺pytest import的lint错误已修正；这些调试失败不冒充原P1红证据。

旧任务worktree在当前沙箱为只读，代码及验证均先在/private/tmp纯tracked archive进行，未复制.env/ignored DB或私有供应商数据。root接手完成实现；原始临时工件保留。应用时核对原HEAD/干净状态和以下文件hash；秘密扫描通过后提交推送，之后直接等待bot Code/Security Review。

代码补丁SHA256：46b422684ac12e08f37081f2de47c816e926dcc9731190b4067da1a9d1f85e3b

```json
{
  "scripts/m0_environment/initial_evidence.py": "52c4eb7a811bdfa0db1ab0faa7881ca179af95093b45ba2f6ab2de1c3673d064",
  "scripts/m0_environment/holmes_baseline.py": "3cf11cca6dcc7a3ef7614ce0977dfd6c924745bf7440516e5221adf9d5356e27",
  "tests/test_m0_timing_echo.py": "7562b5f8c0b14f4d74dbaa21389d78dde49e41078c79e870f48349fa2ea28d33",
  "tests/fixtures/m0_environment/timing_override_probe.py": "001fbcf2bfb41c59a944681fd5ab4f74868b19fae7e077967e4254bcc5d8f387"
}
```

实际模型仍20请求用尽，0新增付费；旧quality FAIL/M1 not cleared保持，本修复仅证明sidecar约束。旧源码/schema快照保留各自历史覆盖范围，不重签旧运行。
