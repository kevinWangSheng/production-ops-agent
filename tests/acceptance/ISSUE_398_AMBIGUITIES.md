# Issue #398 contract ambiguities

The agreed #398 seams, Q10–Q11 and the P7 r3 tuning rule fix the behaviour
below the representation level. The acceptance tests take these readings and
do not assert anything stricter; the lead should settle any that matter.

1. **Exit code of a completed round.** Driver interface §0.1 gives debug exit 0
   when every planned call was made and formal 1/6 for no_baseline/pending.
   `tune` is not listed. Tests accept: `passed` → 0; `failed` → 0 or 1;
   `pending_user_decision` → 0 or 6. The outcome is asserted from `score.json`.
2. **Refusal reason codes.** Only `TUNING_ROUNDS_EXHAUSTED` is named. Skipped or
   repeated rounds, second rerun, rerun with a changed prompt or non-`eval_env`
   triage, unexposed validation items, missing manual balances and changed
   items/annotations/adjudications/label pack are asserted only as §0.1 exit 3
   (no judge call, no balance read, no file written, no exposure row).
3. **Validation items not yet formally exposed.** `debug --tuning` without
   `--item` silently skips them; `tune` must send all 36. Tests read "same
   exposure rules" as a refusal (exit 3) rather than a reduced schedule.
4. **Label pack change detection.** `tune` has no `--pack-hash`; the tests change
   a file by one byte (appended newline) and expect a refusal, without saying
   which binding catches it.
5. **Retried transport failure.** judge_once retries one connection failure. For
   that case the tests assert `started >= 60` and one triage row with hint
   `transport` for the observation; whether the retry is a second "started
   attempt" is open. HTTP and timeout failures are single sends and assert
   `started == 60`.
6. **Hint precedence.** A false pass is also a verdict disagreement; the tests
   require hint `false_pass` for it (the specific hint the seams list) and one
   triage row per attempt. An HTTP error gives `http`, not `output_invalid`.
7. **`triage.json` envelope.** A bare list or an object holding exactly one row
   list are both accepted; row fields `item_id`, `provider`, `repetition`,
   `hint`, `category` are required, extra fields (for example `label_concern`)
   allowed.
8. **`triage-check` output.** Category counts may be session-wide or per
   provider (summed); the denominator must appear under a key containing
   `started` or `denominator`, as 180 for the session or 60 per provider.
   Refusals (null or unknown category) are asserted as exit 3 and the file is
   never rewritten.
9. **Prompt change for the rerun check.** Simulated by monkeypatching
   `opspilot.evaluation.judge.PROMPT` (and `scripts.eval_p7.PROMPT` if that
   name exists). An implementation that caches the hash at import time elsewhere
   would not see the change.
10. **Rounds after a passed round, rounds across workspaces, `--round 0`, a rerun
    of a rerun, a rerun with `--round` different from S0's, and a rerun of a
    round with no failing rows** are not specified and not tested. Prior rounds
    in the tests fail (zai and openai each get one HTTP error) to match the
    real flow.
11. **Exposure row of a non-rerun.** The tests accept `rerun_of` absent or null.
12. **`score_tuning` import path** is the implementer's choice; it is exercised
    through `tune` and `score.json`, whose `TuningScore` may carry extra fields.
