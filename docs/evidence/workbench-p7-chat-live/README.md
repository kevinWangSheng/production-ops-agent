# Workbench P7 Chat panel: bounded real-model check (2026-11-18)

The user ran `~/.agent/p7run.sh` once against the lab PostgreSQL at PR #711 head `b896be6b`.

- **Setup:** one published incident seeded in a throwaway database. The investigation itself was scripted.
- **What was sent:** one question and one steer, through the product HTTP routes.
- **Who answered:** the product `ChatWorker` with the real DeepSeek client, at most 2 model calls; 2 calls were made and both were ok.
- **Steer:** the forward was made through the product forward route.

[`summary.json`](summary.json) holds the result fields. It was rebuilt from the run's printed summary; answer texts and token counts are not included.

**Results:**

- The ask turn was answered with `steer=false`, citing record evidence. Investigation inputs, Runs and steps did not change.
- The steer turn was answered with `steer=true`. It was forwarded as message input 1, signed by the asker, on channel `chat_forward`, with the text equal to the stored question.
- The model's text was not found in any input, and no turn row contains the word `reasoning`.

**No LangSmith trace:** none exists by design. Contract E4 says Chat is not exported, and the Chat thread never opens a Run span.
