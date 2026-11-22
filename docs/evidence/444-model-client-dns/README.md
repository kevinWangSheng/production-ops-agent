# #444 model client DNS bound: bounded real Run on the normal path (2026-11-22)

One bounded real Run at commit `34a18408` (`fix: bound model client DNS resolution`), to show the normal network path still works after the model client's hostname lookup moved onto its own 2 resolver slots (B3-R10).

- **How:** `scripts/m1_live_runner.py --lab-round 444-dns` with `OPSPILOT_TRACE=lab`, against a throwaway PostgreSQL migrated to head (`0028`). Fixture tool, real DeepSeek client (`deepseek-flash`) with the default opener, so both requests resolved `api.deepseek.com` through the new bounded connector.
- **Result:** 2 of 2 model requests completed (prompt 4806 / completion 2220 tokens, about 12.7 s model time). The Run ended `waiting_human` with `INCOMPLETE_INVESTIGATION` because the runner's default budget is 2 model requests; that ending is the budget, not a transport failure. Known cost upper bound: 0.029973 CNY.
- **Trace:** LangSmith project `opspilot-lab-444-dns`, read back `found`: https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/72071430-0d42-40e4-a9d3-7cfcd7986fb3/r/00000000-0000-0000-42c1-a5a502db2664?poll=true
- **Frozen summary:** [`689b6e3f-ec3a-4567-8b62-bc6699771968/summary.json`](689b6e3f-ec3a-4567-8b62-bc6699771968/summary.json). The raw ledger stayed in the ignored `tmp/lab-ledgers/`.

This run does not exercise a hung resolver; that is the acceptance test's job (B3-R9).
