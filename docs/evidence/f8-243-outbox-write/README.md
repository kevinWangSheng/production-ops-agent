# #427 trace outbox write side: one real Run on the installed stack (2026-11-01)

Category: integration run (local colima `m1-kind` VM, its Docker daemon only; Compose install `opspilot-393`, real DeepSeek model, `fixture` tool profile). Not a production proof, not a soak. Nothing was delivered or sent out: PR-1 has no delivery code, and the export below stayed in a local file. Frozen summary: [summary.json](summary.json).

**The `opspilot-393` stack has been built from the #427 branch and upgraded to 0024** (`21314e57`, `OPSPILOT_IMAGE=opspilot:local`). This is reversible: the 0024 downgrade drops the outbox table and leaves business rows untouched.

Raw records are not in the repository (ADR-0006). They are outside it in `~/.cache/opspilot-427-run/` and contain no key, password or token value: `upgrade.log` (install.sh output), `trigger.log` (webhook response), `outbox-rows.json` (every outbox row, exported through the owner connection inside the postgres container), and the reference content used for the check (`step-responses.json`, `tool-results.json`, `events.json`, `postmortem-content.json`; local only, never committed). The webhook token was read into memory by the trigger script and never printed; the DeepSeek key and `worker_env` were not read.

## Steps and results

| Step | Result |
|---|---|
| `install.sh upgrade` (08:22:08–08:22:27Z) | image built from the worktree at `21314e57`; stopping runtimes → postgres → migrate `schema upgraded: 0024_trace_outbox` → init-roles (4 logins at head 0024) → web, worker, observer healthy |
| worker settings | `OPSPILOT_WORKER_CAPACITY=2`, `OPSPILOT_POOL_MAX_SIZE=5` (Compose default after 4→5); it started under the `capacity + 3` floor |
| synthetic Alertmanager webhook (08:22:50Z) | 200, `created`, incident `d67c3cd0…`, Run `cabbecc4…` |
| Run | one tool round (`metrics_range_query`, ok), then a final step; parked at `waiting_human`, handoff `INCOMPLETE_INVESTIGATION` (fixture data does not cover the alert window, same as #393). Budget: 2 spent, 0 unknown of 100 |
| outbox rows of the Run | 6, all `pending`: `run_state` ×2 (`running`, `waiting_human`; keys `…:1`, `…:2`), `model_step` ×3 (sequences 0–2), `tool_result` ×1 (key ends with the provider call id) |
| other outbox rows | 2 `generation` rows for the earlier incident `606d9341…` (from #393): on start the postmortem worker closed an attempt left `running` under a lapsed lease (`abandoned`, `LEASE_LOST`) and then ran a new attempt that `succeeded`. One row per attempt |
| older Runs | no rows (no backfill); total in the table: 8 |
| `compose stop` | runtimes and postgres stopped, volumes kept |

## Content check (per row)

- Every payload key is in `PAYLOAD_KEYS` (the Q14 allowlist).
- Every string value matches `^[A-Za-z0-9._:+-]{1,64}$` (ids, times, revisions, model name, codes); every other value is a number.
- 29 content strings (≥ 8 characters) taken from the Run's assistant messages, tool views, the alert question and the postmortem content, plus `reasoning_content`, `checkout-prod`, `rate(`, `Investigate why` and the alert name, were searched in every payload. The only hit is `metrics_range_query`, the registry tool name (allowlisted as `tool_name`), which is also the function name in the assistant's tool call.
- Token counts are kept: first model step 2079 in / 144 out / 2223 total / 98 reasoning; second 2685 / 2360 / 5045 / 756. `finish_reason` `tool_calls` and `stop`.
- No row holds a prompt, reply, tool view, query, target or service name, report text, `reasoning_content` or credential. `content_fields_found` is empty.

## Notes and limits

- The Run did not publish: `waiting_human` is where it stopped (a parked handoff), the same outcome as the first #393 Run on fixture data. The outbox rows cover every step, tool result and state change it made.
- `entry_id` 7 is missing: the trigger's probe for the second state row first tried `run_state:<run>:1`, which `ON CONFLICT DO NOTHING` skipped after the identity value was drawn. Expected; ids are not dense.
- State rows carry no `handoff_reason` (not on the Run row; known gap, PR-2).
- The first `compose stop` without the `agent` profile stopped postgres and web before worker and observer; the worker logged one lost-connection error and then `worker stopped`. A second `compose --profile agent stop` stopped worker and observer. No Run was in flight.
- Cost: the Run records 2 spent of 100. The postmortem attempt sent 1 model request (6364 prompt / 8867 completion tokens). The DeepSeek balance difference is reconciled by the lead (the Agent does not touch the key).
- The kind cluster workloads in the VM were not touched; the VM was left running for the next window.
