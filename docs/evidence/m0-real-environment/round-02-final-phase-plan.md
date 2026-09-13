# M0-02 最终报告协议修复候选

依据：m002-normal-01 四次HTTP200、16工具；最后stop但content完全是DSML继续工具请求，completion2416。因此不是length或额度不足；`investigation_returned`只检查stop/nonempty误认工具协议正文为报告。原失败保留，不再抬常数。

固定Holmes `tool_calling_llm.py:1161` 最后步只设置tools=None，没有显式采证关闭指令。官方[Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)定义tool_choice=none为禁止工具调用并生成消息，支持response_format=json_object；[JSON指南](https://api-docs.deepseek.com/zh-cn/guides/json_mode/)要求prompt含json与示例，提示空content和length仍可能发生。官方无tools时默认已经是none，因此不能把缺显式none断言为DSML唯一原因；本次是明确收束指令、保持历史消息协议与JSON格式的有界组合修复假设。仅JSON语法不能证明业务完整，须可信侧验收。

最小修复：保留原Holmes循环和所有历史messages/private字段；第4个HTTP沿原循环无tools步且不发送显式tool_choice（默认none），追加通用closed-collection/final-report指令、JSON格式示例，并设置response_format=json_object。明确不再查询；缺证据写gaps并inconclusive/incomplete，不伪造答案。模型主动提前结束时也要求同一报告格式。wrapper从实际最终出站业务messages取得已交付evidence_id集合，校验最终JSON中的全部引用，不信模型自报已见。最后响应若仍有结构tool_calls或正文DSML、空值、length、非JSON/非法结构，保全并不计returned；不执行额外工具或第五次HTTP。

报告接口固定：schema_version='m0-report-v1'，assessment_status='completed'|'incomplete'，conclusion='supported'|'partial'|'inconclusive'，summary为非空字符串，claims为数组，每项严格{kind:'fact'|'hypothesis'|'recommendation'|'counter_evidence'|'rejected_hypothesis',text:非空字符串,evidence_ids:完整ID数组（fact/counter_evidence/rejected_hypothesis非空）}；gaps与next_steps为字符串数组。incomplete要求inconclusive且gaps非空；supported要求至少一条带可见证据的fact。严格顶层字段集合，不接受额外tool字段。结构通过仍需独立事实/因果质量评审；本地不认证supported的语义真实性。

不变：request model deepseek-v4-flash，thinking enabled/high；同Run私有字段完整保留；4HTTP/Run、32768输出、512KiB请求、360秒、预算/权限不变；无按故障分类prompt。不复写任何旧report/raw/view/ledger。

离线先用实际DSML业务正文建立红回归，验证不得视为报告；合成final wire确认原private/messages完全保留，最后无tools/无显式tool_choice/JSONmode和closed控制一致；正例/未知引用/length/空正文/非法字段及incomplete状态组合。真实正常02仅在父调配预算及独立复核后执行，可用同真实正常观察窗，不重搭环境；本工作项0新HTTP。

共享schema复用v3作者生成的ModelReport.v1.schema.json（本目录同bytes副本，唯一形状来源为scripts/m0/outcomes_v3.py::ModelReport）；wrapper导入该schema的version/kind枚举。hypothesis/recommendation允许无引用但需明确暂定，facts/counter_evidence/rejected_hypothesis必须有引用。当前报告只核最后实际出站delivery的已登记view hash/ID，并输出report_request_id，不取历史delivery并集。

当前最终选择：父PG真实返回thinking模式不支持forced-tool choice的HTTP400；一般API schema不能证明thinking与显式none组合。为避免第三轮采证后引入未证组合，最后请求保留上游无tools且省略tool_choice，仅新增closed控制/JSONmode；全部历史private消息仍原样传输。工具调用明确由本地collection_closed拒绝，服务端如仍返回调用或DSML则不计报告。这是有界组合修复假设，不称DSML唯一根因已定位。

父进一步取得官方[Oh My Pi 接入说明](https://api-docs.deepseek.com/quick_start/agent_integrations/oh_my_pi/)的明确模式限制：supportsToolChoice:false，V4 thinking拒绝tool_choice参数。因此最终阶段不依赖一般API schema所列显式none，保持省略参数。前三次采证沿原已HTTP200的上游行为；不在本次另改采证策略。

执行证据：41定向tests PASS；`tmp/m0-environment/holmes-venv/bin/python tests/fixtures/m0_environment/final_wire_probe.py` 使用真实固定Holmes循环及真正httpx.Request，只替换pipe网络传输与凭据读取为本地合成，3组通过：合法JSON、旧真实DSML拒绝、最后结构tool_calls被closed拒绝且未多查。每组4模型伪响应/3工具伪响应，真实HTTP为0。核Content-Length与actual wire JSON、第一至第三步不变、末步无tools/无tool_choice/JSONmode/closed、全部3段synthetic private原样续传、末次report_request_id与实际可见ID。输出见round-02-final-wire-probe.txt。不含真实private。
