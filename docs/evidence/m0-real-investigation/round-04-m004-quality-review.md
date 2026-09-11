# M0-04 real quality closure candidates

- `m004-normal-01`: strict completed/partial, 3 HTTP / 12 tools; trusted time policy delivered; no checkout failure evidence in queried signals; old Envoy field P2 not present. Coverage/SLO unknowns remain explicit.
- `m004-fault-01`: strict completed/partial, 3 HTTP / 9 tools; trusted time policy delivered; direct frontend-proxy POST `/api/checkout` 500, shared trace id, checkout PlaceOrder 13 and payment Charge error. Parent edges are not visible and token origin/payment logs remain unknown.
- Raw observations and safe business summaries are committed separately; old failed reports remain unchanged.
