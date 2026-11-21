# HolmesGPT 公开评测集：OpsPilot 与 Holmes 同题同模型对照

> Holmes 按官方默认配置运行，含 OpsPilot 按只读约束不提供的工具
>
> Holmes 判分口径：默认 opspilot（上游 harness 建的 judge 文本，用 OpsPilot #666 同一 judge 判分，两家同一 judge 口径）

| 题 | OpsPilot 通过/3 | 通过率 | 标记 | Holmes 通过/3 | 通过率 | 标记 | 去 skill 断言 | 时间平移（O/H） |
|---|---|---|---|---|---|---|---|---|
| 283_todowrite_multistep_audit | 3 | 100% |  | 3 | 100% |  |  | — |

## 总分（两轨分列，不合并）

- OpsPilot：3/3（100%），摇摆 0 题，缺遍 0
- Holmes：3/3（100%），摇摆 0 题，缺遍 0

## 说明

- Holmes 判分口径：默认 opspilot（上游 harness 建的 judge 文本，用 OpsPilot #666 同一 judge 判分，两家同一 judge 口径）
- Holmes 按官方默认配置运行，含 OpsPilot 按只读约束不提供的工具
- 两家总分分列，不合并、不相减；同一计分规则：每题 3 遍，只有 judge 判 A 算通过，缺遍、无回答、judge 无结论均记 0 并留在分母。
- 同模型（DeepSeek deepseek-flash，thinking 开、effort high）、同题、同 judge 接收方与模型（Z.ai glm-5.3-flashx）。
- R8-13：标「去 skill 断言」的题两家都不判 SuggestSkills 建议条数，也不跑 replay。
- R10-1：固定时间题两家都按同一平移规则改写数据与题面，送 judge 前反平移；平移量随各自运行时刻不同。
