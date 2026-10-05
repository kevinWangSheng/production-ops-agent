"""0001 baseline: the schema as the four ``install()`` methods built it.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-05

Verbatim copy (indentation aside) of the DDL that ``DurableStore``,
``DurableEventLog``, ``DurableEvidenceStore`` and ``DurableWebLedger``
ran at commit 3d6c94f: 15 tables, 13 ``ADD COLUMN IF NOT EXISTS``, seed
rows and indexes, in the original order. ``IF NOT EXISTS`` is kept so the
statements stay idempotent; an existing database is never upgraded through
this revision anyway, it is *stamped* after ``opspilot.schema`` has proven
its dump identical to a fresh head (ADR-0007, task record 2026-10-05).

No downgrade: there is no schema before this one to go back to, and
dropping the business tables would destroy the recovery authority
(ADR-0003). Every later revision must implement ``downgrade()``.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # opspilot/persistence.py DurableStore.install()
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS opspilot_incidents (
          incident_id uuid PRIMARY KEY, intake_key text UNIQUE NOT NULL,
          state text NOT NULL, lifecycle text NOT NULL DEFAULT 'open', control_generation integer NOT NULL DEFAULT 0,
          current_run_id uuid, conclusion jsonb, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
        );
        ALTER TABLE opspilot_incidents ADD COLUMN IF NOT EXISTS lifecycle text NOT NULL DEFAULT 'open';
        CREATE TABLE IF NOT EXISTS opspilot_targets (
          target_id uuid PRIMARY KEY, resource_uid text UNIQUE NOT NULL,
          created_at timestamptz NOT NULL DEFAULT clock_timestamp()
        );
        ALTER TABLE opspilot_incidents ADD COLUMN IF NOT EXISTS target_id uuid REFERENCES opspilot_targets;
        CREATE INDEX IF NOT EXISTS opspilot_incidents_target_id_idx ON opspilot_incidents(target_id);
        CREATE TABLE IF NOT EXISTS opspilot_scope_controls (
          scope_id smallint PRIMARY KEY CHECK (scope_id = 1),
          global_suspended boolean NOT NULL DEFAULT false,
          global_generation integer NOT NULL DEFAULT 0
        );
        INSERT INTO opspilot_scope_controls(scope_id) VALUES(1) ON CONFLICT DO NOTHING;
        CREATE TABLE IF NOT EXISTS opspilot_target_suspensions (
          target_id uuid PRIMARY KEY REFERENCES opspilot_targets,
          suspended boolean NOT NULL DEFAULT false,
          generation integer NOT NULL DEFAULT 0,
          updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
        );
        INSERT INTO opspilot_target_suspensions(target_id) SELECT target_id FROM opspilot_targets ON CONFLICT DO NOTHING;
        CREATE TABLE IF NOT EXISTS opspilot_suspension_audit (
          audit_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, target_id uuid,
          suspended boolean NOT NULL, generation integer NOT NULL, actor text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT clock_timestamp()
        );
        CREATE TABLE IF NOT EXISTS opspilot_runs (
          run_id uuid PRIMARY KEY, incident_id uuid NOT NULL REFERENCES opspilot_incidents,
          state text NOT NULL, epoch integer NOT NULL DEFAULT 0, owner uuid,
          lease_until timestamptz, control_generation integer NOT NULL,
          budget_limit bigint NOT NULL, budget_reserved bigint NOT NULL DEFAULT 0,
          budget_spent bigint NOT NULL DEFAULT 0, budget_unknown bigint NOT NULL DEFAULT 0,
          deadline timestamptz NOT NULL, versions jsonb NOT NULL, input_watermark integer NOT NULL DEFAULT 0
        );
        ALTER TABLE opspilot_runs ADD COLUMN IF NOT EXISTS input_watermark integer NOT NULL DEFAULT 0;
        ALTER TABLE opspilot_suspension_audit ADD COLUMN IF NOT EXISTS actor text NOT NULL DEFAULT 'unknown';
        -- PostgreSQL 不为外键列自动建索引。control() 按 incident_id 推进 run
        -- 状态，前置的 incident 行锁把 worker 写路径排在这条 UPDATE 之后，
        -- 全表扫描会随表增长直接变成写路径的排队时间。
        CREATE INDEX IF NOT EXISTS opspilot_runs_incident_id_idx ON opspilot_runs(incident_id);
        CREATE TABLE IF NOT EXISTS opspilot_steps (
          step_id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES opspilot_runs,
          sequence integer NOT NULL DEFAULT 0, logical_key text NOT NULL, status text NOT NULL, response jsonb,
          tool_results jsonb NOT NULL DEFAULT '[]'::jsonb, control_generation integer NOT NULL,
          observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          UNIQUE(run_id, logical_key)
        );
        ALTER TABLE opspilot_steps ADD COLUMN IF NOT EXISTS sequence integer NOT NULL DEFAULT 0;
        ALTER TABLE opspilot_steps ADD COLUMN IF NOT EXISTS observed_at timestamptz NOT NULL DEFAULT clock_timestamp();
        CREATE TABLE IF NOT EXISTS opspilot_controls (
          audit_id uuid PRIMARY KEY, incident_id uuid NOT NULL REFERENCES opspilot_incidents,
          action text NOT NULL, expected_generation integer NOT NULL,
          resulting_generation integer NOT NULL, actor text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
        );
        -- 同上：审计行按 incident 读取，且外键列无索引时父行的键变更要扫全表。
        CREATE INDEX IF NOT EXISTS opspilot_controls_incident_id_idx ON opspilot_controls(incident_id);
        ALTER TABLE opspilot_controls ADD COLUMN IF NOT EXISTS payload jsonb;
        CREATE TABLE IF NOT EXISTS opspilot_inputs (
          input_id uuid PRIMARY KEY, incident_id uuid NOT NULL REFERENCES opspilot_incidents,
          sequence integer NOT NULL, kind text NOT NULL, content jsonb NOT NULL,
          actor text, control_generation integer NOT NULL,
          received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          UNIQUE(incident_id, sequence)
        );
        CREATE INDEX IF NOT EXISTS opspilot_inputs_incident_id_idx ON opspilot_inputs(incident_id, sequence);
        CREATE TABLE IF NOT EXISTS opspilot_input_rounds (
          run_id uuid NOT NULL REFERENCES opspilot_runs, logical_key text NOT NULL,
          control_generation integer NOT NULL, input_watermark integer NOT NULL,
          committed boolean NOT NULL DEFAULT false,
          PRIMARY KEY(run_id,logical_key)
        );
        CREATE TABLE IF NOT EXISTS opspilot_budget_reservations (
          reservation_id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES opspilot_runs,
          amount bigint NOT NULL, state text NOT NULL DEFAULT 'reserved', UNIQUE(run_id, reservation_id)
        );
        -- 工具次数/秒数是每 Run 的冻结上限（C3 第 13 节：重启不能重置预算）。
        -- 累计值落在 run 行；**每一次真实派发**记一行，主键是 dispatch_id，
        -- 同一次派发重复结算只更新秒数，不重复计次。键不是 operation_id：
        -- 后者由步骤 ID 和工具序号稳定生成（C3 第 7 节），而 C3 第 13 节要求
        -- 「重试计入次数和费用」、第 4 节明确不承诺外部查询 exactly-once，
        -- 所以同一 operation 的第二次真实读取必须再计一次，不能被去重掉。
        ALTER TABLE opspilot_runs ADD COLUMN IF NOT EXISTS tool_operations_used integer NOT NULL DEFAULT 0;
        ALTER TABLE opspilot_runs ADD COLUMN IF NOT EXISTS tool_seconds_used double precision NOT NULL DEFAULT 0;
        CREATE TABLE IF NOT EXISTS opspilot_tool_charges (
          dispatch_id uuid PRIMARY KEY,
          run_id uuid NOT NULL REFERENCES opspilot_runs, epoch integer NOT NULL,
          operation_id text NOT NULL,
          -- 预留的授权秒数（C3 第 13 节：预算原子预留和结算）。seconds 为 NULL
          -- 表示尚未结算，此时该次派发按 reserved 占用预算——「未知费用保持占用」。
          reserved double precision NOT NULL DEFAULT 0,
          seconds double precision
        );
        CREATE INDEX IF NOT EXISTS opspilot_tool_charges_run_epoch_idx ON opspilot_tool_charges(run_id, epoch);
        -- 输入快照（C3 第 5 节：重建输入所需的实际内容或固定持久引用）。新 worker
        -- 只有这一列和步骤行可读，没有它就无法重建 system/user 消息与工具面。
        ALTER TABLE opspilot_runs ADD COLUMN IF NOT EXISTS input jsonb;
        -- 模型请求的活跃时间与次数同一套预留/结算（C3 第 13 节「租约状态不明时
        -- 保守计量」）：预留时按超时上界占用 reserved_seconds，结算时写实测 seconds；
        -- 请求中崩溃的预留永远没有 seconds，读取时按上界计入。
        ALTER TABLE opspilot_budget_reservations ADD COLUMN IF NOT EXISTS reserved_seconds double precision NOT NULL DEFAULT 0;
        ALTER TABLE opspilot_budget_reservations ADD COLUMN IF NOT EXISTS seconds double precision;
        """
    )

    # opspilot/web/events.py DurableEventLog.install()
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS opspilot_subject_events (
          subject_id uuid NOT NULL, sequence bigint NOT NULL, kind text NOT NULL,
          payload jsonb NOT NULL, recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          PRIMARY KEY (subject_id, sequence)
        );
        """
    )

    # opspilot/web/evidence.py DurableEvidenceStore.install()
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS opspilot_evidence (
          evidence_id text PRIMARY KEY, run_id text NOT NULL, subject_id text NOT NULL,
          status text NOT NULL, adopted boolean NOT NULL, raw bytea NOT NULL,
          raw_sha256 text NOT NULL, view jsonb NOT NULL, view_sha256 text NOT NULL,
          projection_revision text NOT NULL, observed_at timestamptz NOT NULL,
          data_as_of timestamptz
        );
        CREATE INDEX IF NOT EXISTS opspilot_evidence_run_id_idx ON opspilot_evidence(run_id);
        ALTER TABLE opspilot_evidence ADD COLUMN IF NOT EXISTS committed boolean NOT NULL DEFAULT false;
        """
    )

    # opspilot/web/store.py DurableWebLedger.install()
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS opspilot_web_ledger (
          namespace text NOT NULL, key text NOT NULL, value jsonb NOT NULL,
          created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          PRIMARY KEY (namespace, key)
        );
        """
    )


def downgrade() -> None:
    raise RuntimeError(
        "0001_baseline has no downgrade: it would drop the business tables"
    )
