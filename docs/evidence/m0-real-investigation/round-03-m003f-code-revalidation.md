# m003f final revalidation

HEAD `f6e9c536c122c7e6c5965c0cd67324916a8da2d7`.

- Targeted pytest: **11 passed, 47 deselected** (only pytest cache permission warning).
- Targeted Ruff: **All checks passed**.
- Envoy parser now requires exactly 22 tokens, ISO timestamp parsed by `datetime`, rejects CR/LF, malformed time, truncation and appended tail; upstream service time `-` maps unknown.
- v4 projection labels matching proxy.access rows independent of status, covering normal 200 and error 500. Fixture verifies v3 visible rows and v4 visible + omitted equals backend returned.
- Bridge AST whitelist includes `_envoy_access_fields`, `_log_projection_v3`; legacy dependency loader includes `_bind_identity_v2` and `log_projection_v2`.
- Scope/run metadata tests pass for matching and stale window/revision/run_id rejection.

No P1/P2 found. Remaining pytest cache warning is environmental and non-blocking.
