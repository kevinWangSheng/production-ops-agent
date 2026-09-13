# M0-02 真实模型/PG组合执行前独立审查

日期：2026-09-10。状态：**修复后有条件通过：必须使用外层360秒进程组监督；尚无真实组合运行证据**。只读实际driver、private transport、httpcore2/httpx2源码与离线测试；无真实HTTP、无.env或实际private读取，无PG生命周期操作。

## 发现

1. **P1 响应model身份未校验。** 初始request()直接取usage/choices/finish并commit_response，不核对reply.model。可能接受非Flash响应且按Flash费率结算；必须严格拒绝mismatch，并先保全受限raw及安全诊断，防再次丢失真实响应。不能用请求model代替响应身份。
2. **P1 父硬退出后子transport迟发窗口。** 父持PG control session锁，但Popen后至send_request_body.complete信号前若父崩溃/SIGKILL，PG连接关闭即释放锁，子transport仍存在且可能随后发送；sent_fd在body已发送后才写，BrokenPipe不能撤回。正常异常路径reap覆盖不了父硬退出。若本组合要认证worker中断后取消无迟发，须让实际transport在发起边界自己持有并检查PG权威，或给出同等可证明约束；仅已有三项无信号超时测试不覆盖此项。若保持当前探针范围，必须明确不认证父硬退出→新控制的发起安全，是否允许有此限制的付费组合由父统一任务目标判断。
3. **P2 结算锁无上界。** finish_global用blocking flock，其他Run持全轮锁时可以超过360秒；需deadline/有限尝试，失败保留原unknown。回收宽限也应包含在对外总wall说明中，不能把360加清理余量仍叫严格360。
4. **复现记录缺项。** 初始record有model/固定VERSIONS但没有实际driver/transport/profile/hash及有效发送参数；应保存精确指纹以匹配独立审查与真实执行。

## 已核验与限制

原三项离线subprocess测试通过：有信号正常响应、无信号超时先reap、invalid body在读取凭据前退出且stdout/stderr为空。查本机httpcore2/_sync/http11.py与_trace.py，http11.send_request_body.complete确在完整body发送方法返回后触发；这证明信号语义，并不消除父死亡窗口。

renew在共享锁内_valid核对完整fence，lease_until取run.deadline与当前+60秒较小值；跨PID第二阶段保留原Run deadline/PG步骤与工具观察，可测试同Run续传。文件全轮账本是费用权威，PG账本为机制mirror；任何mirror成功不能绕过全轮预留。尚未执行真实组合。

实现持续修复中，待作者冻结后独立复验并记录覆盖hash。不得依据本稿发真实HTTP。

## 修复后独立复验

冻结后重读实现：prepare_request仅预留/创建one-use grant；实际transport自己持PG session lock，核完整fence、最新physical request、body/input hash并原子消费grant；header/body.started再次核时间和lease，body.complete才release。父死亡不再释放子自身持有的发起锁；cancel先提交则子拒绝。reply先保存完整bounded raw到受限PG表再严格检查共享PROFILE双Flash响应名；model mismatch固定code且不接受。finish_global改deadline非阻塞轮询；版本/profile实际hash保存并二阶段比较。四项发现已按此处理。

独立 `M0_STEP_POSTGRES=1 .venv/bin/python -m pytest tests/test_m0_pg_live_probe.py tests/integration/test_m0_step_store_postgres.py -q`：**20 passed in 8.13s**。其中真实父SIGKILL→取消提交→孤儿transport放行屏障后CONTROL_DENIED、0本地HTTP、预留保留；还有一次grant重复/错误bodyhash拒绝。未读凭据或实际private，无provider调用。

**启动条件：** 内部wait到deadline后reap仍可能多4秒且有PG收尾。若对外合同为严格360秒总wall，父必须使用已审run_bounded.supervise(command,timeout=360)一类外层独立进程组监督启动每stage，把TERM/KILL余量包括在360秒内；不能裸driver执行后声称全流程360硬上界。锁/等待超时仍保留unknown。满足此执行条件可进行本轮授权真实组合，成功与否以实际工件另判。

审查最新hash：
- `scripts/m0_pg_live_probe.py` `1aefd748dbd820a993892747cb5b480ec3f4eaecc4ade76973d3ffaa08d122e9`
- `scripts/m0_pg_private_transport.py` `50cd86e99941b6ad076ad77f7e369429f1ac5b6baf95de4df820fd6c2823fcb8`
- `scripts/m0/step_store.py` `71bf4c2452ee44e73c9c7b438296d2e6451fd722329975d1d542b69324f2e02b`
- `scripts/m0/step_store.sql` `e3cba8e242aedc329dac605736b52b5e3f31b9225293a7fcbadf4830ba73ddaf`
- `scripts/m0/budget.py` `0eee8b60ab91d21df7fe5f97a4116bb6c74a0afbccf4407857effb9e3262408b`
- 共享环境round02.py `33ef91543e1266bf7428c9585d86d4a5f067f8d8f4563f304fa4fb46b6386e12`

## 真实400后最终兼容小修独立复验

原首阶段400/unknown不改，不复用原Run。独立8项offline tests PASS/1.44s；round-02-bridge-driver-fix-hashes.json全部当前hash匹配，driver为6d86a7dc64462709b33936b0d3eac0cb9ea5dba05795f1853a2937f57cc1fac6。首请求完全省略tool_choice、复用protocol-v1 fixture输入和tools，thinking/high不变。返回仍须精确fixture调用和目标；非200固定HTTP分类先于modelguard，raw先PG保存。

新增wire_history仅对deepcopy出的同Run请求副本，将带tool_calls的assistant content null改空字符串；私有reasoning和配对不改，原PG完整raw/response不改；record保存归一化版本及实际是否发生。该行为依据已核官方V4兼容说明（非nullcontent），不是删协议或新Run伪装恢复。两stage仍记录不同PID、同Run和原观察时间/hash。

此最终小修独立通过，可按父新分配和新record/新Run执行，必须沿前述supervise360外层监督。旧PG400的3.44064unknown及旧24未核占用不得释放。仍无本审查者真实HTTP。
