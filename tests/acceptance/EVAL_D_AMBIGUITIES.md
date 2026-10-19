# PR-D r1 验收读法与歧义

仅依据 `docs/tasks/2026-10-18-eval-pr-d-interface.md` 和 `AGENTS.md`；没有查看 PR-D 实现。本表记录测试采用的读法，不替代合同裁决。范围为公开接口第 1–3、5 节；不验收 Make/CI 隔离部署。

## 1. HTTP 成对事件

- 原文：「一次成对的 http 事件」。
- 读法 A：一个 `RecordingEvent(kind="http")` 同时具有 request/response 引用。
- 读法 B：两个独立事件分别代表请求与响应。
- 测试采用 A：至少一个 HTTP 事件有 response 引用；计数按 B2 `Recording.events` 的 kind 逐条统计，不把请求与响应计为两条。

## 2. 调用链是否要求相邻

- 原文：「至少含一次 model→tool→delivery→model 的链条」。
- 读法 A：四种事件在全局序列中必须相邻。
- 读法 B：按该顺序存在，允许中间有 HTTP、其他工具或 delivery。
- 测试采用 B：检查有序子序列，再通过 B2 reader/structural replay 核验包；不固定内部调用次数或要求相邻。

## 3. CANARY 风格的范围

- 原文：「所有服务名、操作名和标识使用 CANARY 风格的合成值」。
- 读法 A：每一个字符串（包括 schema、枚举、工具协议名、版本与 hash）都要带 CANARY。
- 读法 B：业务服务、操作与身份字段带 CANARY，标准协议字段、版本和 hash 使用其合法编码。
- 测试采用 B：递归检查 service/operation 与明确的业务身份字段，大小写无关地包含 `canary`；同时检查 JSON 字符串和十六进制 raw 中的主机。没有通用私有协议字段清单，因此不发明字段黑名单；包合法性交由 B2 公开 reader/replay 检查。

## 4. `.env` 值的来源（lead 已裁决）

- 原文：「不含 `.env` 键名对应的值」。
- 读法 A：读取仓库或开发者本机的真实 dotenv/凭据文件，在内存中与 fixture 字节比对。
- 读法 B：只使用测试自己生成的 CANARY 假值，不读取任何真实凭据文件。
- 测试采用 B（lead 裁决）：删除真实 dotenv 读取与比对代码。仅向子进程临时目录下的 `.env`、`.env.judge`、临时 HOME 的 `.env` 和 `.aws/credentials` 写入 CANARY 假值，再注入假凭据环境变量；断言入口 JSON 与无凭据环境下相同，且 fixture 的全部文件字节中不含这些假值。子进程的凭据文件读取拦截继续保留。fixture 是否含有类似凭据的串由 CI 的 gitleaks 检查，验收测试不负责。

## 5. 无项时固定错误码放在哪里

- 原文：「3：DIR 不存在或没有子目录（`NOTHING_TO_VERIFY`）」；stdout 示例只有 `schema/status/exit_code/results`。
- 读法 A：还必须在顶层 error、results 或 stderr 某处输出 `NOTHING_TO_VERIFY`。
- 读法 B：该码是退出条件名称，接口没有指定 JSON 中的承载位置。
- 测试采用 B：断言退出 3、status=`refused`、results 为空；不为该码发明额外 JSON 字段或 stderr 要求。只有普通文件的目录也按「没有子目录」处理。

## 6. status 与失败阶段字段

- 原文：「status: verified|failed|refused」；「每项结果包含 name, recording_sha256, loaded, structural_verified, counts_ok, error」。
- 读法 A：任意错误都可以使用 refused，未到达的步骤字段可以省略或为 null。
- 读法 B：成功用 verified，单项失败用 failed，无项用 refused；三个核验字段始终为布尔值。
- 测试采用 B：符合退出码分工；只对明确相关字段作阶段断言，例如 reader 失败 loaded=False、身份失败 structural_verified=False、计数不符 counts_ok=False。未执行的其他步骤不规定取值。成功 error=None；失败 error 为非空固定码。

## 7. JSON 严格键与额外输出字段

- 原文：「fixture.json：键严格限定为……」；「每项结果包含……」；「stdout 只输出一个 JSON 对象」。
- 读法 A：fixture、入口顶层和 results 都只能有列出的键。
- 读法 B：仅 fixture 与事件计数具有明确的严格键要求，入口输出允许额外字段。
- 测试采用 B：拒绝 fixture 多余/缺失键以及非对象、损坏、多个 JSON 文档；入口 JSON 用一次 `json.loads` 解析整个 stdout，允许前后空白、额外结果字段，禁止日志或第二个 JSON。未额外规定重复 JSON 键的处理。

## 8. 非负整数是否包含布尔和浮点数

- 原文：「值为非负整数」。
- 读法 A：Python 的 bool 或数值等于整数的 float 也可以接受。
- 读法 B：JSON integer，且不是 boolean；不接受浮点数或字符串。
- 测试采用 B：冻结 fixture 使用 `type(value) is int`；拒绝 true、1.0、"1" 和负数。

## 9. 结构元数据错误归属

- 原文：第 1 节规定「attempt.mode == structural、product_run_id is None」、manifest 的 replay_fixture 为指定 hash；第 3 节「严格解析 fixture.json，不合格记 FIXTURE_INVALID」。
- 读法 A：上述条件属于 fixture schema，进入 reader/replay 前拒绝。
- 读法 B：只校验 DTO 形状，把 mode、product_run_id 或 replay_fixture 错配交给 replay，返回 IDENTITY_MISMATCH。
- 测试采用 A：这些明确的 fixture 条件违反时断言 FIXTURE_INVALID；另以合法编码但 scenario 身份错配测试 replay 的 IDENTITY_MISMATCH，并检查 StorageError 的 code 原样透传。
- lead 补充裁决：`test_recording_tamper_is_rejected[sha]` 只改 recording_sha256，未同步 manifest 的 replay_fixture hash，因此允许在 reader/replay 前以 FIXTURE_INVALID 拒绝。接口第 5 节对此仅要求返回 1、error 非空；该用例断言 status=failed、loaded=False，不指定具体错误码。blob 分支继续断言 reader 的 StorageError code 原样透传。

## 10. name 支持集合与可重命名的 fixture

- 原文：「name 等于目录名，限定 [a-z0-9-]{1,64}」；生成器「支持的 name 集合在代码中固定」。
- 读法 A：入口也只接受生成器支持的固定 name。
- 读法 B：固定集合只约束生成器，入口接受满足 schema 与目录一致性的任何 name。
- 测试采用 B：对复制包同步改目录名与 fixture name，检查 1/64 字符边界、按名排序和混合成败；未知生成器 name 返回 2。没有要求全部合法名称都能生成。

## 11. 不写文件与离线环境的边界

- 原文：「入口的约束：不写任何文件」；「环境变量被清空或填入 CANARY 假凭据的条件下，入口结果不变」。
- 读法 A：只检查 fixture 没有修改；清空仅指删除凭据变量。
- 读法 B：入口不能产生任何文件写入；子进程环境从最小白名单构造，再注入假凭据做对照。
- 测试采用 B：所有脚本调用都在包装子进程里禁用 socket.socket/create_connection，并拦截底层 socket 与外部进程调用。入口另拦截写文件/目录/改名等事件，对 fixtures 和 tmp_path 作完整字节及目录快照。保留 PATH/LANG/HOME/TMPDIR，使用 `-B` 禁止解释器缓存写入。比较解析后的 JSON，不要求空白格式相同；这不构成任意环境读取、native 扩展或模型客户端构造均未发生的穷举证明。

## 12. aggregate 接收什么结构结果

- 原文：「把它送入 PR-A 的 aggregate 时，pass_rate 为 None，baseline_complete 为 False（NO_QUALITY_SLOTS）」。
- 读法 A：直接向 aggregate 传入口 stdout 字典或 ReplayCapture。
- 读法 B：按公开 aggregate API 转为 RunEvaluation 和 PlannedSlot。
- 测试采用 B：以真实入口的 structural_verified 构造一个已执行的 structural 评测，并故意令 overall=True，验证仍然没有质量率；必须含 NO_QUALITY_SLOTS，但允许同时出现未校准等其他原因。不调用 judge，不把结构成功解释为质量成功。
