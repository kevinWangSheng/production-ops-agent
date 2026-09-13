-- Additive, explicitly installed isolated M0 experiment records only.
CREATE TABLE IF NOT EXISTS m0_v3_subject (
 id uuid PRIMARY KEY, experiment_id uuid NOT NULL REFERENCES m0_experiments(id),
 intake_key text NOT NULL, intake_hash text NOT NULL, input jsonb NOT NULL,
 current_run uuid NOT NULL REFERENCES m0_runs(id), generation bigint NOT NULL DEFAULT 0,
 state text NOT NULL DEFAULT 'running', owner uuid, epoch bigint NOT NULL DEFAULT 0,
 lease_until timestamptz, versions jsonb NOT NULL, final jsonb,
 UNIQUE(experiment_id,intake_key)
);
CREATE TABLE IF NOT EXISTS m0_v3_step (
 id uuid PRIMARY KEY, subject uuid NOT NULL REFERENCES m0_v3_subject(id),
 run_id uuid NOT NULL REFERENCES m0_runs(id), segment text NOT NULL, round integer NOT NULL,
 input jsonb NOT NULL, input_hash text NOT NULL, response jsonb, response_hash text,
 UNIQUE(run_id,segment,round)
);
CREATE TABLE IF NOT EXISTS m0_v3_run_input (
 run_id uuid PRIMARY KEY REFERENCES m0_runs(id), subject uuid NOT NULL REFERENCES m0_v3_subject(id),
 input jsonb NOT NULL, versions jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS m0_v3_operation (
 step uuid NOT NULL REFERENCES m0_v3_step(id), ordinal integer NOT NULL,
 call jsonb NOT NULL, result jsonb, captured_at timestamptz,
 status text CHECK(status IN ('success','failed','cancelled','unknown')),
 PRIMARY KEY(step,ordinal)
);
CREATE TABLE IF NOT EXISTS m0_v3_dispatch (
 request uuid PRIMARY KEY REFERENCES m0_requests(id), subject uuid NOT NULL REFERENCES m0_v3_subject(id),
 step uuid NOT NULL REFERENCES m0_v3_step(id), kind text NOT NULL CHECK(kind IN ('model','tool')),
 ordinal integer, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS m0_v3_audit (
 sequence bigserial PRIMARY KEY, subject uuid NOT NULL REFERENCES m0_v3_subject(id),
 event text NOT NULL, accepted boolean NOT NULL, generation bigint NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS m0_v3_tool_attempt (
 id uuid PRIMARY KEY, step uuid NOT NULL, ordinal integer NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(step,ordinal) REFERENCES m0_v3_operation(step,ordinal)
);
CREATE TABLE IF NOT EXISTS m0_v3_report (
 run_id uuid PRIMARY KEY REFERENCES m0_runs(id), candidate jsonb NOT NULL,
 generation bigint NOT NULL, published_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS m0_v3_tool_execution (
 attempt uuid PRIMARY KEY REFERENCES m0_v3_tool_attempt(id),
 generation bigint NOT NULL, owner uuid NOT NULL, epoch bigint NOT NULL
);
CREATE TABLE IF NOT EXISTS m0_v3_model_execution (
 request uuid PRIMARY KEY REFERENCES m0_v3_dispatch(request),
 generation bigint NOT NULL, owner uuid NOT NULL, epoch bigint NOT NULL
);
CREATE TABLE IF NOT EXISTS m0_v3_control (
 subject uuid NOT NULL REFERENCES m0_v3_subject(id), generation bigint NOT NULL,
 action text NOT NULL, payload jsonb NOT NULL,
 PRIMARY KEY(subject,generation)
);
CREATE TABLE IF NOT EXISTS m0_v3_send_grant (
 request uuid PRIMARY KEY REFERENCES m0_v3_model_execution(request),
 claimed boolean NOT NULL DEFAULT false
);
-- Round-07 work package 3 additions: scope pause, observer authorization,
-- persistent observation stream and work package 2 stream-interruption audit.
ALTER TABLE m0_v3_subject ADD COLUMN IF NOT EXISTS target_key text;
CREATE TABLE IF NOT EXISTS m0_v3_pause (
 scope text NOT NULL CHECK(scope IN ('global','target')), target_key text NOT NULL,
 active boolean NOT NULL DEFAULT false, version bigint NOT NULL DEFAULT 0,
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(scope,target_key)
);
CREATE TABLE IF NOT EXISTS m0_v3_pause_event (
 sequence bigserial PRIMARY KEY, scope text NOT NULL, target_key text NOT NULL,
 action text NOT NULL CHECK(action IN ('pause','resume')), version bigint NOT NULL,
 reason text, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS m0_v3_observer (
 id uuid PRIMARY KEY, subject uuid NOT NULL REFERENCES m0_v3_subject(id),
 run_id uuid NOT NULL UNIQUE REFERENCES m0_runs(id),
 experiment_id uuid NOT NULL REFERENCES m0_experiments(id),
 window_start timestamptz NOT NULL, window_end timestamptz NOT NULL,
 query_limit integer NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS m0_v3_observer_attempt (
 id uuid PRIMARY KEY, observer uuid NOT NULL REFERENCES m0_v3_observer(id),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS m0_v3_observation_stream (
 sequence bigserial PRIMARY KEY, subject uuid NOT NULL REFERENCES m0_v3_subject(id),
 profile_revision text NOT NULL, observation_revision text NOT NULL,
 captured_at timestamptz NOT NULL, evidence_ids jsonb NOT NULL, verdict text NOT NULL,
 accepted boolean NOT NULL, code text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS m0_v3_stream_interruption (
 request uuid PRIMARY KEY REFERENCES m0_v3_dispatch(request),
 partial_sha256 text, partial_bytes integer NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
