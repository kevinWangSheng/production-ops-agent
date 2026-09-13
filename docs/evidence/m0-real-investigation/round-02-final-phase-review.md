# M0-02 最终报告协议独立审查

日期2026-09-10。状态：**最终无tools/省略tool_choice版本独立离线复验通过；真实normal02待执行和质量评审**。

独立读取真实normal01 result：4HTTP/16工具、stop正文全为DSML calls，不是业务报告。原investigation_returned为旧stop/nonempty判据的误判，实际失败必须保留。未读实际private或.env，未调用provider/工具。

已核固定Holmes源码末步tools=None/tool_choice=None；独立官方[Chat Completions文档](https://api-docs.deepseek.com/api/create-chat-completion/)搜索全文确认tool_choice=none与response_format=json_object正式支持，且无tools默认none。因而不能断言“缺显式none”是DSML唯一根因；该方案是以已观察失败为依据的组合适配假设，待实际验证。

候选round-02-final-phase-plan.md：最终HTTP显式结束采证、恢复同schema但tool_choice none、JSON报告合同；不改前3步、不增加第五请求、不改thinking/high、32k输出/512KiB/360秒，不删同Run私有协议。语法/schema/引用通过不替代因果质量；允许uncertain/incomplete，不能逼模型编造supported。方向符合C3可验证模型适配边界。

执行前要求：实际DSML安全fixture红→绿；实际最终wire验证原messages/private完整、工具配对保留、仅末步的closed控制/JSONmode/none生效、content-length与byte/token guard针对最终body；结构tool_calls/DSML/length/空文/非法JSON/不可见引用均拒绝。提前结束也遵循同一报告合同。不得只测试未接到真实HTTP路径的helper。原normal01失败不回写passed，新的normal02单独Run，模式改动与源码hash必须记录。

后续等作者冻结后运行独立定向checks并附最终hash；此稿不构成normal02发起前通过。

## 兼容前提更正与最终复验

父取得专门[Thinking兼容来源](https://api-docs.deepseek.com/quick_start/agent_integrations/oh_my_pi/)，审查者亦独立打开today正文，明确supportsToolChoice=false及拒绝tool_choice。此前基于通用API schema接受“保留schema+显式none”的判断不足以证明thinking组合兼容；该候选撤销，未依据其发起normal02。

最终末步删除tools并彻底省略tool_choice，保留完整messages/private/tool配对，追加closed collection与json_object。前3步仍沿实际normal01已200的显式auto协议；这与专门文档宽泛表述有差异，不能声称全轮无tool_choice。此轮只改变有失败证据的最后报告阶段，不倒推唯一DSML原因。

最终41定向tests PASS/1.50s。独立实际运行固定Holmes Python的tests/fixtures/m0_environment/final_wire_probe.py：真实Holmes循环/SDK/httpx.Request，仅pipe transport和dotenv值替身，3case全通过，0真实HTTP。有效JSON returned；实际normal01 DSML判incomplete；末步结构tool_calls判incomplete且总3工具查询不增加。每case均4物理替身请求，末包无tools/choice，json_object、closed指令、3段合成private和全部历史配对保留，Content-Length匹配最终bytes。

审查曾指出visible_ids union所有历史delivery，已改仅最终response_received delivery并记录report_request_id。report结构与bridge对齐为supported/partial/inconclusive和fact/hypothesis/recommendation/counter_evidence/rejected_hypothesis；确定性验证只认证结构/引用，不认证因果。

额外审查者抽取实际guarded_send AST的隔离wire probe也已验证Request字节和长度；第一次测试替身capture缺http_status参数抛TypeError，修正替身后通过。最终上方完整Holmes三case比该局部probe更强，不以局部probe替代完整链路。

正常02调用前父须检查实际normal phase可容纳4新请求；normal01已4，若旧phase上限7不够，必须在全轮20不变前提先落盘分配。原normal01失败和所有消耗不重写。

最终覆盖hash：
- `report_contract.py` `c6ed3a8796d9b0291a14359e5d2446b37619829fbd462cbd3ee42ff7c6d77845`
- `holmes_baseline.py` `7fd7326644b26fe00d0a8bad25aab4b865dfbd453bb5a7b4fa491eda5539a676`
- `round02.py` `33ef91543e1266bf7428c9585d86d4a5f067f8d8f4563f304fa4fb46b6386e12`
- `transport_worker.py` `559b32fd55eeb5fa03513e7739e2c331dcde3b67d4264497274324f4649b7582`
- `run_bounded.py` `c46493aa1c44a4e5a0f627a62525d1ce82215dbc73dc76405fa4bd40e0550d24`
