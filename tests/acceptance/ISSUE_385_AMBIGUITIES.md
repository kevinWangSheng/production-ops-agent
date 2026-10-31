# Issue #385 contract ambiguities

These points are intentionally left open by the r2 contract and are not made
stricter by the acceptance tests:

1. The JSON encoding of an upstream citation, evidence reference, and absence
   check is unspecified. Tests accept strings or structured JSON and inspect
   only recognizable path/line and command/hit-count information.
2. The contract names the mapping rows but does not explicitly name the
   top-level rows key or whether `upstream-files.json` is an array or an object.
   The tests accept `rows` or `mapping`, and either array or keyed-file forms.
3. The exact wording and location of the human-readable summary counts are
   unspecified; tests require only the named files and the required legacy
   header/README pointers.
4. The contract says `planned` rows are suggestions but does not define a
   dedicated field or exact wording. Tests do not require a new field and only
   require a non-empty note or suggestion context.
