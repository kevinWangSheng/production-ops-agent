# 本机 lab 库 Alembic 接管（2026-10-05）

任务记录：[数据访问层标准化](../../tasks/2026-10-05-m1-prep-schema-migrations.md)；PR-a #104（main `4609171`）。

- 接管前：5 张 `opspilot_*` 表，无版本表；行数 incidents 10609、runs 10611、steps 5218、controls 42226、budget_reservations 1349。全库备份 `tmp/m0-b/backup-m0_budget-pre-alembic-2026-10-05.dump`（`pg_dump -Fc`，3.0 MB，35 张表数据，sha256 前缀 `7926411abfb6c7a8`，git 忽略、本机保留）。
- `psql -f tests/integration/legacy_schema_2026-10-05.sql` 补到旧版头（15 张表）；`make migrate` 不带 flag → 拒绝，diff 只有 2 个 hunk，均为列顺序；带 `MIGRATE_FLAGS=--accept-column-order` → 接管并打印被接受的 diff：
  ```
  @@ -35,11 +35,11 @@  (opspilot_incidents)
  -    lifecycle text DEFAULT 'open'::text NOT NULL,
  +    lifecycle text DEFAULT 'open'::text NOT NULL,
  @@ -87,12 +87,12 @@  (opspilot_steps)
  -    sequence integer DEFAULT 0 NOT NULL,
  +    sequence integer DEFAULT 0 NOT NULL,
  ```
- 接管后：`python -m opspilot.schema check` → `schema at head 0001_baseline`；`alembic_version` = `0001_baseline`；五张表行数与接管前一致；无残留临时库；`DurableStore(...).install()` 通过。lab 实例接管前为停止状态，接管后已停止。
- 回滚途径：停 lab，`pg_restore` 上述备份到重建的 `m0_budget`（未执行）。
