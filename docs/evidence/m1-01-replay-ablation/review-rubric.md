# Blind review rubric (given verbatim to every reviewer)

You are checking investigation reports against the tool-result views the
report writer was shown. You receive: this rubric, a folder of views per Run
(`views/R<k>/*.json`, one file per tool result, in delivery order; each has
an `evidence_id`, the `content` rows, and for trace views per-row
`status_state`, `status_tags`, `error_by_visible_tags`, `duration_us`,
`service`, `operation`, `trace_id`), and a list of report samples, each headed
by its anonymous id and the Run alias whose views apply. Nothing else about a
sample is known to you and nothing else should be assumed.

For each sample, count exactly two kinds of error. Every counted error needs
the report's own words and the view evidence it contradicts.

- **a-class**: the report attributes a status code or status value to a span
  that has none recorded -- a row with `status_state` `not_recorded` (or
  empty `status_tags`) is described as status 0, OK, HTTP 200, "success
  status", "gRPC 0", or similar. This includes summarizing a mixed set of rows
  (some recorded, some not) as "all rows show status 0" / "every call returned
  200" / "no errors recorded on all N spans". Absence of a status is not a
  status. Saying a row has *no status recorded* or *no error tag* is correct,
  not an error. `error_by_visible_tags: false` on an unrecorded row means only
  that no error tag is visible; describing it as "not flagged as error" is
  fine, "returned OK" is an a-class error.
- **e-class**: a number in the report does not match the cited view: a
  duration range endpoint (min/max) that is not the min/max of the rows the
  statement covers, a count of rows/traces/spans that is wrong, a mean or
  other statistic that does not follow from the rows, a range that mixes
  rows of different operations or parent/child spans while presenting them as
  one operation, or a numeric value with no row supporting it. Rounding to
  the nearest millisecond or reasonable unit conversion (`duration_us` 156384
  = 156 ms) is not an error. A count that the report explicitly scopes
  ("of the 17 rows shown") is judged against that scope.

Count each distinct wrong statement once. A statement in `summary` and the
same statement repeated in a claim count once, but two different wrong
numbers count twice. Do not count anything else (style, missing information,
citation format, hedging, the choice of conclusion) as an error.

Also record the report's overall verdict, without judging whether it is
right, as one of: `no_failure` (the Run's window shows no failing calls or
elevated errors), `failure_located` (a failing dependency call is named --
give the service and operation the report blames), `other` (anything else,
e.g. inconclusive without a named cause, or contradictory).

Write one JSON object per sample to your results file as you finish it:

```json
{"sample": "S1234", "run": "R2",
 "a_errors": [{"quote": "...report words...", "basis": "views/R2/07-traces_search.json rows 3,5,9 have status_state not_recorded"}],
 "e_errors": [{"quote": "...", "basis": "..."}],
 "verdict": "failure_located", "verdict_target": "checkout -> payment PaymentService/Charge",
 "notes": ""}
```

Work through the samples in the order of `index.json`. Read a Run's views
once and keep your own notes of the per-operation row counts, status
distributions and duration min/max per view; then check each report against
those notes and re-open the view file whenever a number needs confirming.
