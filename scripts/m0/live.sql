-- Explicit isolated experimental setup only; no product or existing synthetic table migration.
CREATE TABLE IF NOT EXISTS m0_live_once (
 experiment_id uuid PRIMARY KEY,
 run_id uuid NOT NULL UNIQUE,
 approval_hash text NOT NULL UNIQUE,
 contract_hash text NOT NULL,
 deadline timestamptz NOT NULL,
 reserved_cny numeric NOT NULL DEFAULT 2.00 CHECK(reserved_cny=2.00),
 cost_state text NOT NULL DEFAULT 'unreconciled' CHECK(cost_state='unreconciled'),
 attempts text[] NOT NULL DEFAULT '{}',
 business text NOT NULL DEFAULT 'handoff' CHECK(business IN ('handoff','failed','completed')),
 outbox jsonb,
 usage jsonb,
 trace_status text NOT NULL DEFAULT 'pending' CHECK(trace_status IN ('pending','unknown','verified'))
);
