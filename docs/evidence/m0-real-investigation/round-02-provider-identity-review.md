# M0-02 服务端报告名称独立审查

日期：2026-09-10。范围：兼容决定与已捕获业务metadata；0 provider请求/生成/trace/余额调用。未读取.env或实际private。独立读取候选决定、GET/models安全记录、第二saved response-business，并独立浏览官方定价页。

## 判断

**允许将本轮请求deepseek-v4-flash的响应允许集固定为deepseek-v4-flash、deepseek-flash。** 这是有证据的服务reported身份兼容，不是固定权重/模型版本相同的证明，不是允许任意Flash名称或Pro。

依据：第二真实出站请求固定deepseek-v4-flash、thinking/high，官方HTTPS响应报告deepseek-flash；父此前受认证GET/models安全metadata列deepseek-flash owned_by deepseek。独立打开当前官方[模型价格页](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)显示请求名仍deepseek-v4-flash、文档版本Flash-0731。两项运行观察与文档共同支持请求别名/报告标识差异的推断。GET/models单独并不能证明两个字符串的版本等价；结论只在上述边界成立。

实现须保持出站exact v4-flash、thinking/high和原endpoint；响应仅exact二项allowlist，记录requested_model与reported_model，绑定本轮metadata源/时间/hash，不重写服务返回值。Pro、任意其他名、缺失/畸形model仍拒绝且保全。后续provider返回新名称须重新取得证据，不能扩大为前缀匹配。

## 用量与历史

第二response-business usage完整自洽：35721 prompt、15104 completion、50825 total；2048 cache hit +33673 cache miss=35721。独立打开官方中文页面today快照确认Flash峰值cache-miss输入3 CNY/M、输出9 CNY/M。因此第二请求本轮全输入按峰值miss的保守费用上界为(35721*3+15104*9)/1e6=**0.243099 CNY**。可在单独带依据的本轮上界重算记录中使用并释放第二请求相应预留差额，不能称实际账单/归属实际支出已核。必须保留原失败记录和重算审计；不得静默覆写历史。

首请求返回model和usage未保存，仍unknown，3.44064 CNY全额占用不释放；旧24 CNY不动。第二reported别名获兼容不将原failed改为passed，不消除报告质量问题，也不证明主动调查/恢复组合通过。

## 验证边界

本次未重新请求/models；其真实性依据父当前轮授权metadata实物与受认证客户端执行记录，未独立重发。定价首次英文打开超时，搜索结果曾命中上月旧价，未采用；随后直接中文URL当前页面核实3/9。无需以额外付费推理验证别名。

审查工件hash：
- `docs/evidence/m0-real-investigation/round-02-provider-identity-decision.md` `2f7100759f78889139b4b1315e38d505d8466b511f9e77719a7cbb2e50638d9e`
- `docs/evidence/m0-real-investigation/round-02-provider-models.json` `37a7903d6cc9ade127bd5d688953237d0c419de581a17351089bf62ee0eb900b`
