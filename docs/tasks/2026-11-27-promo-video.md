# 作品集宣传片（45 秒）

- 状态：已完成（成片、GIF、4:5 版已交付，独立审查与理解测试通过）
- 更新日期：2026-11-27
- 依据：issue #892；作品集更新 #876 已完成（[任务记录](2026-11-26-portfolio-media.md)），本片与其中的演示录屏（#880）分开
- 工作区：仓库外 `~/dev/AI/opspilot-portfolio/promo/`；README 嵌入走 `chore/promo-video-embed`（worktree `../production-ops-agent-promo`）

## 目标与范围

做一段约 45 秒、营销风格的宣传片，放中英 README 顶部与作品集导览首页，片尾引向完整演示录屏。读者是 AI Agent / 后端岗位的面试官。不改产品代码，不重跑真实环境。

## 用户裁决（2026-11-27）

| 编号 | 裁决 |
|---|---|
| U1 | 做，作为独立子项，不依赖租机 |
| U2 | 画面用卡通/扁平插画与图标动画抽象表达功能，不用产品截图、不还原界面细节（2026-11-27 修订，取代「用真实界面」）；先出分镜与关键帧给用户看，确认后再整片渲染 |
| U3 | 工具 Remotion（官方 Claude Code skill，借 EveryInc/product-launch-video 的分镜与多轮评审流程）；不用生成视频画面，不用数字人 |
| U4 | 无配音，烧录英文字幕，配一首免版税音乐；原始音轨不进公开仓库 |
| U5 | 交付：1080p H.264 MP4 ≤ 10 MB；README 顶部 8–15 秒 GIF 循环链到完整版；另出 20–30 秒 4:5 版给 LinkedIn |
| U6 | 全新上下文 Agent 按下方标准逐帧验收 |

Agent 自定（一行理由）：

- A1 插画用代码绘制的 SVG（Remotion 内），一套统一配色与线条风格：不依赖生成图片模型，风格可控、可逐帧改。
- A2 Remotion 源码放仓库外：Remotion 是源码可见许可，非开源；视频文件体积大。
- A3 制作与验收模型用 Opus 5.5：项目 UI 工作已定用 Opus。

## 结构（起点，分镜阶段可调）

约 45 秒、按功能分段，每屏不超过 7 个英文词，全片 45–60 词。功能级表达，不讲实现细节：半夜告警 → 证据散在日志、指标、集群里 → 「OpsPilot investigates. You decide.」 → 只读地收集证据 → 比较假设、给出带证据的结论 → 工程师随时接管或确认 → 独立观察恢复 → 生成复盘草稿供人工审核 → 一个标出处的评测数字 + 仓库地址。

## 验收标准

- 画面表达的功能是产品真有的能力，可以抽象，不可夸大。产品只读（[PRODUCT-CONSTRAINTS](../../PRODUCT-CONSTRAINTS.md) Explicit exclusions）：不得出现批准修复、自动回滚、重启等动作；人工环节只能是接管、确认、暂停/取消、审核复盘。
- 数字能对到仓库冻结证据，片中标出处。
- 无 slop 套路：粒子网络/大脑/灯泡代指 AI、渐变发光标题、全部元素同帧淡入、无出处百分比、看不清的滚动输出。
- 1080p 下字幕停留够读完；画面无密钥、token、内部地址。
- 理解测试：另一全新 Agent 只看成品复述「系统做什么」，与事实不符即退回。

## 必要上下文

- 调研（仓库外）：`~/dev/AI/opspilot-portfolio/research/promo/` 下 `01-examples-social.md`（23 个 AI 宣传片实例与 slop 批评）、`02-tooling.md`（工具链、许可、README 嵌入限制）、`03-reference-promos.md`（19 个官方宣传片逐段拆解，45 秒结构出处）。
- 对标：Datadog Bits AI SRE、Linear Agent、Cursor 2.0、Cleric（链接见 `03-reference-promos.md`）。

## 进展与证据

- 2026-11-27：用户裁决 U1–U6；issue #892 已建。
- 分镜 v1 → v2：用户看过关键帧后要求人物与物品更生动，v2 加入贯穿全片的同一位工程师（睡觉、困惑、伏案、头像）和场景道具；用户确认通过。
- 音乐：Pixabay 拒绝自动下载（403 挑战页），改用 Mixkit「Minimal Emotion」（Stock Music Free License：商用与网络发布免费、无需署名，不得把原曲单独分发或混成纯音乐）；按 48 秒内响度最平稳选取（短时响度标准差 0.9 dB）。原始音轨只在仓库外 `promo/audio/`。
- 第一轮独立逐帧审查（全新 Opus 5.5 上下文，只给评分标准、原始帧与冻结证据）：阻塞项为 4 条字幕停留 1–2 秒，不够读完；其余为工具计数中间顺序与真实调用顺序不同、气泡短暂遮住「Writes to your systems: none」等次要项。修复：字幕提前、相关镜头加长（48 秒 → 57 秒），计数按 `summary.json` 真实顺序，「Writes」行移到标题下。复审 6 项全部通过；3 条字幕被判临界（±0.5 秒采样误差），按代码帧数计算均达标（如「You stay in charge. Always.」完整显示 2.8 秒，要求 2.67 秒）。
- 理解测试（另一全新上下文，只看 96 帧）：复述为只读调查、人修复、独立 Observer 确认恢复、复盘草稿待人审，数字与限定语全部复述正确；与事实相符。
- 交付（仓库外 `~/dev/AI/opspilot-portfolio/promo/out/`）：`opspilot-promo.mp4` 57 秒 1920×1080 H.264 6.5 MB；`opspilot-promo-4x5.mp4` 29 秒 1080×1350 1.4 MB（LinkedIn 由用户自行发布）；`opspilot-promo-loop.gif` 12.5 秒 1.8 MB 入库于 `docs/assets/promo/`。完整版经 `gh issue comment --attach`（gh 升级到 2.106.0）发在 #892，README 与作品集导览链接该地址。

## 与计划的出入

- 时长 57 秒，超出「约 45 秒」：为满足字幕停留标准，按可读性优先取舍。
- 片尾显示仓库地址，未链接 #880 演示录屏：录屏未公开发布（只在仓库外），无可链接地址。

## 待决

无。
