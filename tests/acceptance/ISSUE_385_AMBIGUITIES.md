# Issue #385 contract ambiguities

These points are intentionally left open by the r2 contract and are not made
stricter by the acceptance tests:

1. The exact wording and location of the human-readable summary counts are
   unspecified; tests require only the named files and the required legacy
   header/README pointers.
2. The contract says `planned` rows are suggestions but does not define a
   dedicated field or exact wording. Tests do not require a new field and only
   require a non-empty note or suggestion context.

The lead-approved seams resolve the former open points: the mapping header,
`rows` key, citation strings, `upstream-files.json` object shape, structured
`absence_checks` and `candidate_defect`, `path::symbol` OpsPilot evidence,
runtime evidence paths, and list-valued `requirement_ref` are asserted in the
acceptance tests.
