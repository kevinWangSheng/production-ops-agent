# Pi runtime choice — 2026-09-07

Status: candidate investigation, static source evidence only; no runtime tests, package installs or feature implementation. Not an adopted stack decision.

## Version and evidence boundary

Primary checkout `/Users/shenghuikevin/dev/AI/pi`: `086c32e74530564922d011ade23ff582c9d63116`, pi-agent-core package 0.84.2. Dirty read.ts and untracked local docs/.codex were preserved; that modified tool implementation is not evidence here. Secondary checkout `/Users/shenghuikevin/dev/AI/agent-research/repos/pi`: `d3ab2af969d64997338253c9151190aa1bc33580`, clean, not mixed into primary conclusions. Official former badlogic/pi-mono URL now redirects to [earendil-works/pi](https://github.com/earendil-works/pi); its current README confirms the renamed packages and layered organization. Current default-branch head was not asserted equal to the local pin.

All code links below pin primary SHA. “Test evidence” means the test source was read, not executed.

## Three distinct integration routes

- **pi-ai** supplies provider/model abstraction, streaming, token/cost accounting. It is not an incident runtime. Official [AI README](https://github.com/earendil-works/pi/blob/086c32e74530564922d011ade23ff582c9d63116/packages/ai/README.md) includes provider factories, dynamic providers and compatibility options; concrete model availability/auth and parity require validation.
- **pi-agent-core Agent** supplies shared messages/model/tool loop, context hooks, steering, events and cooperative abort. Recommended Pi candidate for a service-owned incident domain. It avoids importing coding CLI defaults but leaves incident persistence, budget, ownership and recovery orchestration to the application.
- **pi-coding-agent createAgentSession** supplies coding-oriented settings/resources, extensions, skills, compaction and JSONL session history. Embedded SDK works in-process; [RPC](https://github.com/earendil-works/pi/blob/086c32e74530564922d011ade23ff582c9d63116/packages/coding-agent/docs/rpc.md) is coding-agent JSONL over stdin/stdout, not an independent durable service. RPC adds subprocess/protocol lifecycle. Do not adopt it merely to obtain history files.

## Actual context and tool path

[Agent.prompt/continue](https://github.com/earendil-works/pi/blob/086c32e74530564922d011ade23ff582c9d63116/packages/agent/src/agent.ts#L350-L391) rejects concurrent runs and calls the loop. [agent-loop.ts](https://github.com/earendil-works/pi/blob/086c32e74530564922d011ade23ff582c9d63116/packages/agent/src/agent-loop.ts#L159-L235) streams a model response, executes calls and appends results; [L284-L315](https://github.com/earendil-works/pi/blob/086c32e74530564922d011ade23ff582c9d63116/packages/agent/src/agent-loop.ts#L284-L315) transforms context before convertToLlm. [Test L221-L274](https://github.com/earendil-works/pi/blob/086c32e74530564922d011ade23ff582c9d63116/packages/agent/test/agent-loop.test.ts#L221-L274) checks that order with a mock stream.

[Tool execution L605-L744](https://github.com/earendil-works/pi/blob/086c32e74530564922d011ade23ff582c9d63116/packages/agent/src/agent-loop.ts#L605-L744) resolves/validates calls, invokes beforeToolCall, awaits execute with AbortSignal and postprocesses results. Hook mutations need application validation: the test at L444 explicitly covers mutated args without revalidation. Cooperative signal is not proof an uncooperative network client/process is killed. Hooks are integration points, not OS/network identity isolation.

The coding SDK [options L38-L86](https://github.com/earendil-works/pi/blob/086c32e74530564922d011ade23ff582c9d63116/packages/coding-agent/src/core/sdk.ts#L38-L86) default to read/bash/edit/write, DefaultResourceLoader and settings/session files. Explicit tool allowlist plus controlled resource loader/config required; merely saying read-only in prompt is insufficient. [AgentSession L470-L510](https://github.com/earendil-works/pi/blob/086c32e74530564922d011ade23ff582c9d63116/packages/coding-agent/src/core/agent-session.ts#L470-L510) installs extension tool_call/tool_result interception. Upstream [README](https://github.com/earendil-works/pi) explicitly states no built-in OS/process/network/credential permission system.

## Persistence: usable history versus unfinished durable runtime

Coding SDK reads `sessionManager.buildSessionContext()` (sdk.ts L190); AgentSession persists completed message events at [L635-L658](https://github.com/earendil-works/pi/blob/086c32e74530564922d011ade23ff582c9d63116/packages/coding-agent/src/core/agent-session.ts#L635-L658). [SessionManager L1014-L1048](https://github.com/earendil-works/pi/blob/086c32e74530564922d011ade23ff582c9d63116/packages/coding-agent/src/core/session-manager.ts#L1014-L1048) appends JSONL and delays first flush until an assistant exists. This is conversation persistence, not an atomic Incident/Run/tool-intent commit. Agent.continue works from transcript, not an arbitrary in-flight network call. Need own recovery policy for crash before/after query and evidence commit, plus fenced ownership if workers can compete.

**Important source/document discrepancy:** new [harness.md](https://github.com/earendil-works/pi/blob/086c32e74530564922d011ade23ff582c9d63116/packages/agent/docs/harness.md) describes durable recovery, session backends, checkpoints and writer leases. But [AgentHarness L350-L384](https://github.com/earendil-works/pi/blob/086c32e74530564922d011ade23ff582c9d63116/packages/agent/src/harness/agent-harness.ts#L350-L384) throws HarnessNotImplemented for restore, prompt, resume and abort. [Scaffold test L59-L85](https://github.com/earendil-works/pi/blob/086c32e74530564922d011ade23ff582c9d63116/packages/agent/test/harness/agent-harness-scaffold.test.ts#L59-L85) expects restore rejection. The new SQLite/session pieces do not prove a working durable harness. Do not choose based on future design claims.

## Observability, MCP and hosting responsibilities

[pi-telemetry README](https://github.com/earendil-works/pi/blob/086c32e74530564922d011ade23ff582c9d63116/packages/telemetry/README.md) supplies explicit span contracts, NOOP/in-memory reference, no exporter/backend. OTel adapter/exporter and incident/run correlation remain application integration. New harness schema is not evidence classic Agent auto-emits all described spans. Classic loop events/custom stream/tool wrappers can provide instrumentation; validate usage, errors, parenting and redaction in a spike.

[Coding README](https://github.com/earendil-works/pi/blob/086c32e74530564922d011ade23ff582c9d63116/packages/coding-agent/README.md#L490-L503) explicitly has no built-in MCP; use extension/adapter if required. MCP is optional for native tools. API service, auth, event ingestion, Incident/Run model, evidence store, queue/leases, external read scopes, hard budgets, crash recovery, deployment/upgrade and independent recovery verifier remain our responsibilities.

## Recommendation within Pi alternatives

Prefer evaluating **Agent + pi-ai with custom read-only tools**, not coding-agent RPC, if TypeScript/Node is a good whole-project fit and owning service orchestration is acceptable. Reuse loop/provider logic; avoid a coding shell as authority boundary. Coding SDK becomes attractive if its skills/compaction/extension features remove demonstrable work and can be safely isolated. The unfinished new AgentHarness is not a selectable production runtime at this pin.

Pi advantage is small controllable generic loop plus multi-provider access, with no fixed symptom routing. Cost is implementing and validating the product-level persistence/recovery/integration around it and handling current API/package evolution (renamed scope, compat path, new harness transition). This note does not claim LangGraph or Agents SDK inferior: compare their concrete durable-state and telemetry paths in separate investigations, and compare whole-system responsibility rather than framework feature names.

## Required proof before adopting

1. Fake provider + two read tools: trace shared context updates, denied/wrong-target call and hook argument mutation.
2. Crash at accepted request, assistant tool intent, query completion and evidence commit; verify no silent lost Incident and explicit unknown execution outcome.
3. Cooperative and non-cooperative tools: cancellation, deadlines and cleanup; worker kill if required.
4. Reopen history vs resume Run separately; cancel during restart; duplicate event/worker claim.
5. OTel adapter smoke: incident/run IDs, provider usage, tool errors, no secrets; provider parity for selected paid API models.
6. Version upgrade compatibility fixtures for stored messages/tools. No arbitrary benchmark thresholds until measured baseline.

No feature gates changed. No runtime correctness claimed.
