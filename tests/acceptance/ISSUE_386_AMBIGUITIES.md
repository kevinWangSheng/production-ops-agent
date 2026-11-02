# #386 acceptance ambiguities

- The r2.1 text is now present on `main`; the Observer sentinel fixture follows the corrected `seams-386.md` §6 requirements (`sweep_expired_sessions`, a real lease, and a failing source query).
- The seam says the web service exception sites are reached through `Workbench._recovery_view` and `evidence_chart`, but does not specify a minimal constructor or argument fixture. The sentinel test uses `object.__new__` and the documented method names; if the public seam requires setup state, the contract should publish that fixture.
- The seam requires a no-PG web entry observation for uvicorn, while the no-PG web recipe exits before uvicorn. The test covers the documented standalone `uvicorn.run(..., log_config=None)` recipe; a true web-entry uvicorn assertion remains PG-gated.
