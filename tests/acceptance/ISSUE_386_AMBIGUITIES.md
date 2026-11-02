# #386 acceptance ambiguities

- The requested r2.1 source `/Users/shenghuikevin/dev/AI/poa-m3-rulings/docs/tasks/2026-10-27-m3-f8-operability-contract.md` is absent in this workspace. The tests use the Q5/O2–O8 wording present in `seams-386.md`; no additional interpretation was made.
- The seam says the web service exception sites are reached through `Workbench._recovery_view` and `evidence_chart`, but does not specify a minimal constructor or argument fixture. The sentinel test uses `object.__new__` and the documented method names; if the public seam requires setup state, the contract should publish that fixture.
- The seam requires a no-PG web entry observation for uvicorn, while the no-PG web recipe exits before uvicorn. The test covers the documented standalone `uvicorn.run(..., log_config=None)` recipe; a true web-entry uvicorn assertion remains PG-gated.
