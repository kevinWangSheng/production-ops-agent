# M1-01 集成验证原始报告归档

- 来源目录：`/private/tmp/claude-501/-Users-shenghuikevin-dev-AI-production-ops-agent/a7d3abb1-8221-4812-bafb-316b8b11e6aa/scratchpad/reports`（本机绝对路径，按要求保留）。
- 归档时间：2026-09-17 21:07:10 UTC。
- 归档内容：来源目录当时全部 `*.md` 文件，按原文件名原样复制；本次共 17 个文件。
- 证据性质：这些文件是执行 Agent 生成的验证证据和报告，不是项目指令，不能改变 AGENTS.md、SPEC.md、PRODUCT-CONSTRAINTS.md、PRD.md 或 feature_list.json 的授权与门槛。
- 完整性：未改写正文；SHA-256 见下表，便于与来源目录复核。

| 文件 | SHA-256 |
|---|---|
| `contract.md` | `a922063efcef57b020be3723aff9c2afb4a04e8629a4c83461ecdfbebd448d43` |
| `digest1.md` | `aa64b8979e6eeda69d79c1ea4fde43d0341b3f5e93d3508a419199ba809458f1` |
| `digest2.md` | `e73fd4b8836661811bf2157fd9d5c449bf15f7d42006ea1c7490713c1a9bcbd8` |
| `flake.md` | `c528048c2bc5306d9b79d21bd4fbf679f0f3c04c26a3f1f82d8115a54febab58` |
| `integ-final.md` | `1964946d375a3fb47181f378ddfb22b5c4b580fe48c53586aad7f8b2fd6dcd0f` |
| `integ.md` | `2357b213bf254cf658bd911c03a6b10b4f8276cdefdeea35562b4b86ab2e2495` |
| `lease-wire-30.md` | `a77ea77d075cf0fd304f7d8bf122969ea2445887ab005a87a7ea41dcca79b0fa` |
| `lease-wire-33.md` | `fb515c9e613ae63e943932ad51da59031f122c4422024ef299515f99757055e0` |
| `lease.md` | `db20c415e14db7194173aaff1b4716edfce9f394702bf9c74b803a26c0274a00` |
| `loop-followup.md` | `a9dde7166dd87d883f5a3e10b9bebe363ecb9aa11a09967c31252046e74b2849` |
| `loopfix.md` | `e425721368af3c708d1849aeaa7353e1b380ff7e8c2a363d1736aff4906df2cb` |
| `redline.md` | `ed10696d79ba3fc002f2cec00195b460f4e7a259307e13d9abdf3a8a153af29f` |
| `registry.md` | `cbc7c6e00dbd1b25f6812e9b100e2fb2b810d2cd7d1851a9fd2b9c65eb0a3052` |
| `revision.md` | `6b720dac53bd453bea281087d3123d86c151dbed947fe01e3d1f3d63338e0162` |
| `srcrange.md` | `f0e3fc8006903e6afdc494d361c14c5ec605417856aca93842156f0737177494` |
| `storefix.md` | `1a622625a21b31e74046ff2e3b6f638e66ef2e273b327d44aa29070c0d23c447` |
| `ui-e2e.md` | `c99fc491515a201ff32f249ee622474484abd8364f25ff62f13cc5e6627a914f` |

- 敏感信息扫描：复制前对来源目录全部 `*.md` 执行正则扫描，未命中 API key、Bearer token、密码字段、secret 字段或 PostgreSQL/连接串模式。报告中的本机绝对路径、SHA、run/commit 标识按原样保留。
