CREATE TABLE IF NOT EXISTS m0_experiments (
 id uuid PRIMARY KEY, ceiling numeric NOT NULL CHECK (ceiling >= 0),
 deadline timestamptz NOT NULL, synthetic boolean NOT NULL CHECK(synthetic),
 blocked boolean NOT NULL DEFAULT false
);
CREATE TABLE IF NOT EXISTS m0_runs (
 id uuid PRIMARY KEY, experiment_id uuid NOT NULL REFERENCES m0_experiments(id),
 provider text NOT NULL, deadline timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS m0_requests (
 id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES m0_runs(id),
 reserved numeric NOT NULL CHECK(reserved > 0),
 state text NOT NULL CHECK(state IN ('reserved','unknown','settled')),
 actual numeric CHECK(actual >= 0),
 CHECK ((state = 'settled') = (actual IS NOT NULL))
);
