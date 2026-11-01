# #427 ambiguities to resolve during implementation

- The brief and seams name the generation-attempt write point but do not expose
  the public generation-store helper signature. The PG test uses the existing
  generation store path once the lead wires it; no new public API is assumed.
- “Bulk hold” is a web control path, while the durable-store contract currently
  exposes single-run `control()`. The PG test records this as a required path;
  the lead should bind it to the actual web helper without weakening the
  trigger assertion.
- The exact runtime error class/message for a pool below `capacity + 3` is not
  fixed by the brief, so the offline test accepts the existing worker startup
  error family and checks the rejection condition.
- `RUNTIME_GRANTS`' post-0024 SHA-256 is intentionally left as a TODO in the
  existing pin test; the lead must fill it from the implementation, never guess.
