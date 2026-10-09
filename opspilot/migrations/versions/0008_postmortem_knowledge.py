"""0008 postmortem and knowledge store: versions, conclusions, review audit.

Revision ID: 0008_postmortem_knowledge
Revises: 0007_ending_job_identity
Create Date: 2026-10-09

M1-03 step 1 (F13; contract decisions D1-D9 in the M1-03 task record, C3
section 10 "复盘与知识"). Tables:

* ``opspilot_postmortems`` -- one per incident: the object's own generation
  (D9, independent of the incident control generation) and its latest
  version number. The only rows that are ever updated, and only by bumping
  the generation in a transaction that wrote the matching audit row.
* ``opspilot_postmortem_versions`` -- one generated draft each, with the
  canonical content text and its sha256 (checked by the database), and the
  generation watermarks it was assembled at (D1): incident control and
  observation generations, the ended observation session and its ending,
  the Run count/last Run, the input watermark and the evidence snapshot
  hash. ``state`` and ``stale_reason`` are the only mutable columns, along
  the domain ``POSTMORTEM`` edges; every state but draft/under_review is
  terminal, so an approved version can never be updated again (D7).
* ``opspilot_postmortem_conclusions`` -- the sections/claims of a version,
  each with ``certainty``, whether its citations validated, its evidence
  references, and whether code or the model wrote it (D2, D3).
* ``opspilot_postmortem_disputes`` -- append-only disputes on a conclusion
  with reason and who raised it (D3). A conclusion is disputed while any
  row exists; nothing removes one, so a disputed version can never be
  approved -- a new version is the only way forward.
* ``opspilot_postmortem_proposals`` -- the structured knowledge entries a
  version proposes (D4: name, tags, symptoms/checks/evidence references in
  the content), optionally naming the entry it would supersede.
* ``opspilot_knowledge_entries`` -- a knowledge entry's own generation and
  latest revision number.
* ``opspilot_knowledge_revisions`` -- immutable revisions: monotonic integer
  per entry, fixed content hash, the approved postmortem version it came
  from, who approved it. Never updated (D7).
* ``opspilot_knowledge_revocations`` -- the tombstone of a revoked revision
  with its reason (D5). Revocation changes only whether a revision is
  retrievable; the view ``opspilot_knowledge_revision_states`` derives
  ``active | superseded | revoked`` from revisions and tombstones.
* ``opspilot_f13_requests`` / ``opspilot_f13_audit`` -- one row per
  idempotency key (the request fingerprint and its result, for replay) and
  one audit row per object a request changed: actor id, authenticated
  principal kind, action, reason, expected and resulting generation (D6,
  D9).

Every table refuses DELETE and TRUNCATE for every role, the owner included,
and every table but the two object rows refuses UPDATE (state/stale_reason
of a non-terminal version excepted). Every state change, version, revision,
dispute and tombstone must be written in the same transaction as its audit
row (``xmin`` = the current transaction), and review actions require the
``basic_auth`` principal kind. Like the Observer triggers of 0003, these
guard against code and SQL errors, not against a compromised owner
credential (an owner can disable triggers).
"""

import re
from collections.abc import Sequence

from alembic import op

revision: str = "0008_postmortem_knowledge"
down_revision: str | Sequence[str] | None = "0007_ending_job_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, column) -> allowed values. The version state equals the domain
# Literal ``opspilot.domain.knowledge.PostmortemState``
# (``tests/test_schema_postmortem_store.py`` compares them).
CHECKS: dict[tuple[str, str], tuple[str, ...]] = {
    ("opspilot_postmortem_versions", "state"): (
        "draft",
        "under_review",
        "approved",
        "rejected",
        "returned",
        "stale",
    ),
}
# Why a draft went stale (D1): the generation watermark it was bound to moved.
STALE_REASONS = (
    "control_generation_changed",
    "incident_reopened",
    "observation_changed",
    "run_added",
    "input_added",
    "evidence_changed",
)
# Code-assembled facts are deterministic; model narrative is supported only
# when its citations validated, otherwise uncertain (D2).
CERTAINTIES = ("deterministic", "supported", "uncertain")
AUTHORS = ("code", "model")
PRINCIPAL_KINDS = ("basic_auth", "worker")
ACTIONS = (
    "generate",
    "submit",
    "mark_stale",
    "dispute",
    "approve",
    "reject",
    "return",
    "publish",
    "supersede",
    "revoke",
)
# What a worker principal may write (D8: drafts and generation events);
# every other action is a human review action through the web (D6).
WORKER_ACTIONS = ("generate", "submit", "mark_stale", "dispute")
OBJECT_KINDS = ("postmortem", "knowledge_entry")

TABLES = (
    "opspilot_postmortems",
    "opspilot_postmortem_versions",
    "opspilot_postmortem_conclusions",
    "opspilot_postmortem_disputes",
    "opspilot_postmortem_proposals",
    "opspilot_knowledge_entries",
    "opspilot_knowledge_revisions",
    "opspilot_knowledge_revocations",
    "opspilot_f13_requests",
    "opspilot_f13_audit",
)
# Tables whose rows never change once written.
FROZEN_TABLES = (
    "opspilot_postmortem_conclusions",
    "opspilot_postmortem_disputes",
    "opspilot_postmortem_proposals",
    "opspilot_knowledge_revisions",
    "opspilot_knowledge_revocations",
    "opspilot_f13_requests",
    "opspilot_f13_audit",
)

_WORD = re.compile(r"^[a-z_]+$")


def _in_list(values: Sequence[str]) -> str:
    for value in values:
        if not _WORD.match(value):
            raise ValueError(f"value {value!r} is not a plain lowercase word")
    return ", ".join(f"'{value}'" for value in values)


def _check(
    table: str, column: str, values: Sequence[str], *, nullable: bool = False
) -> str:
    allow_null = f"{column} IS NULL OR " if nullable else ""
    return (
        f"CONSTRAINT {table}_{column}_check "
        f"CHECK ({allow_null}{column} IN ({_in_list(values)}))"
    )


# sha256 of the canonical content text, computed by the database itself so
# a stored hash can never disagree with the stored content.
def _hash_check(table: str) -> str:
    return (
        f"CONSTRAINT {table}_content_sha256_check CHECK ("
        "content_sha256 = encode(sha256(convert_to(content, 'UTF8')), 'hex'))"
    )


def upgrade() -> None:
    versions = "opspilot_postmortem_versions"
    conclusions = "opspilot_postmortem_conclusions"
    disputes = "opspilot_postmortem_disputes"
    proposals = "opspilot_postmortem_proposals"
    revisions = "opspilot_knowledge_revisions"
    revocations = "opspilot_knowledge_revocations"
    audit = "opspilot_f13_audit"
    requests = "opspilot_f13_requests"
    op.execute(
        f"""
        CREATE TABLE {requests} (
          idempotency_key text PRIMARY KEY CHECK (idempotency_key <> ''),
          action text NOT NULL,
          -- sha256 of the canonical request: a key reused for a different
          -- request is a conflict, the same request replays ``result``
          request_sha256 text NOT NULL CHECK (request_sha256 ~ '^[0-9a-f]{{64}}$'),
          result jsonb NOT NULL,
          recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          {_check(requests, "action", ACTIONS)}
        );
        CREATE TABLE {audit} (
          event_id uuid PRIMARY KEY,
          -- deferred: the request row carries the result, so it is written
          -- last in the transaction that wrote the audit rows
          idempotency_key text NOT NULL REFERENCES {requests} DEFERRABLE INITIALLY DEFERRED,
          object_kind text NOT NULL,
          object_id uuid NOT NULL,
          action text NOT NULL,
          version integer,
          revision integer,
          actor_id text NOT NULL CHECK (actor_id <> ''),
          principal_kind text NOT NULL,
          reason text,
          expected_generation integer NOT NULL CHECK (expected_generation >= 0),
          resulting_generation integer NOT NULL,
          recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          {_check(audit, "object_kind", OBJECT_KINDS)},
          {_check(audit, "action", ACTIONS)},
          {_check(audit, "principal_kind", PRINCIPAL_KINDS)},
          CONSTRAINT {audit}_principal_action_check CHECK (
            principal_kind = 'basic_auth' OR action IN ({_in_list(WORKER_ACTIONS)})
          ),
          CONSTRAINT {audit}_generation_check CHECK (resulting_generation = expected_generation + 1),
          CONSTRAINT {audit}_reason_check CHECK (
            action NOT IN ('reject', 'return', 'mark_stale', 'dispute', 'revoke')
            OR (reason IS NOT NULL AND reason <> '')
          ),
          UNIQUE (idempotency_key, object_kind, object_id),
          UNIQUE (object_kind, object_id, resulting_generation)
        );
        CREATE INDEX {audit}_object_idx ON {audit}(object_kind, object_id, recorded_at);
        CREATE TABLE opspilot_postmortems (
          postmortem_id uuid PRIMARY KEY,
          incident_id uuid NOT NULL UNIQUE REFERENCES opspilot_incidents,
          generation integer NOT NULL DEFAULT 0 CHECK (generation >= 0),
          latest_version integer NOT NULL DEFAULT 0 CHECK (latest_version >= 0),
          created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
        );
        CREATE TABLE {versions} (
          postmortem_id uuid NOT NULL REFERENCES opspilot_postmortems,
          version integer NOT NULL CHECK (version > 0),
          -- the returned version this one revises (NULL: a fresh generation)
          revises_version integer,
          state text NOT NULL DEFAULT 'draft',
          stale_reason text,
          -- canonical JSON text exactly as hashed (jsonb would re-serialize it)
          content text NOT NULL,
          content_sha256 text NOT NULL,
          -- generation watermarks the draft was assembled at (D1)
          incident_control_generation integer NOT NULL,
          observation_generation integer NOT NULL,
          observation_session_id uuid NOT NULL REFERENCES opspilot_observation_sessions,
          observation_ending_id uuid NOT NULL REFERENCES opspilot_observation_endings,
          run_count integer NOT NULL CHECK (run_count >= 0),
          last_run_id uuid REFERENCES opspilot_runs,
          input_watermark integer NOT NULL CHECK (input_watermark >= 0),
          evidence_snapshot_sha256 text NOT NULL CHECK (evidence_snapshot_sha256 ~ '^[0-9a-f]{{64}}$'),
          generated_by text NOT NULL CHECK (generated_by <> ''),
          generated_by_kind text NOT NULL,
          created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          PRIMARY KEY (postmortem_id, version),
          FOREIGN KEY (postmortem_id, revises_version) REFERENCES {versions},
          CONSTRAINT {versions}_revises_check CHECK (revises_version IS NULL OR revises_version < version),
          {_check(versions, "state", CHECKS[(versions, "state")])},
          {_check(versions, "stale_reason", STALE_REASONS, nullable=True)},
          {_check(versions, "generated_by_kind", PRINCIPAL_KINDS)},
          CONSTRAINT {versions}_stale_check CHECK ((state = 'stale') = (stale_reason IS NOT NULL)),
          CONSTRAINT {versions}_run_check CHECK ((run_count = 0) = (last_run_id IS NULL)),
          {_hash_check(versions)}
        );
        -- at most one version open for review work per postmortem
        CREATE UNIQUE INDEX {versions}_one_open_idx ON {versions}(postmortem_id)
          WHERE state IN ('draft', 'under_review');
        CREATE TABLE {conclusions} (
          postmortem_id uuid NOT NULL,
          version integer NOT NULL,
          conclusion_key text NOT NULL CHECK (conclusion_key <> ''),
          ordinal integer NOT NULL CHECK (ordinal >= 0),
          section text NOT NULL CHECK (section <> ''),
          body text NOT NULL,
          author text NOT NULL,
          certainty text NOT NULL,
          citations_valid boolean NOT NULL,
          -- [{{"evidence_id", "scope", "window_start", "window_end"}}, ...]
          evidence_refs jsonb NOT NULL CHECK (jsonb_typeof(evidence_refs) = 'array'),
          PRIMARY KEY (postmortem_id, version, conclusion_key),
          UNIQUE (postmortem_id, version, ordinal),
          FOREIGN KEY (postmortem_id, version) REFERENCES {versions},
          {_check(conclusions, "author", AUTHORS)},
          {_check(conclusions, "certainty", CERTAINTIES)},
          CONSTRAINT {conclusions}_author_certainty_check CHECK ((author = 'code') = (certainty = 'deterministic')),
          -- a failed citation check makes the conclusion uncertain (D2)
          CONSTRAINT {conclusions}_citations_check CHECK (citations_valid OR certainty = 'uncertain')
        );
        CREATE TABLE {disputes} (
          dispute_id uuid PRIMARY KEY,
          postmortem_id uuid NOT NULL,
          version integer NOT NULL,
          conclusion_key text NOT NULL,
          reason text NOT NULL CHECK (reason <> ''),
          raised_by text NOT NULL CHECK (raised_by <> ''),
          raised_by_kind text NOT NULL,
          event_id uuid NOT NULL REFERENCES {audit},
          raised_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          FOREIGN KEY (postmortem_id, version, conclusion_key) REFERENCES {conclusions},
          {_check(disputes, "raised_by_kind", PRINCIPAL_KINDS)}
        );
        CREATE INDEX {disputes}_version_idx ON {disputes}(postmortem_id, version);
        CREATE TABLE opspilot_knowledge_entries (
          entry_id uuid PRIMARY KEY,
          generation integer NOT NULL DEFAULT 0 CHECK (generation >= 0),
          latest_revision integer NOT NULL DEFAULT 0 CHECK (latest_revision >= 0),
          created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
        );
        CREATE TABLE {proposals} (
          postmortem_id uuid NOT NULL,
          version integer NOT NULL,
          proposal_key text NOT NULL CHECK (proposal_key <> ''),
          name text NOT NULL CHECK (name <> ''),
          tags text[] NOT NULL,
          -- canonical JSON: symptoms, checks, evidence references
          content text NOT NULL,
          content_sha256 text NOT NULL,
          -- the existing entry this proposal would supersede (NULL: new entry)
          supersedes_entry_id uuid REFERENCES opspilot_knowledge_entries,
          PRIMARY KEY (postmortem_id, version, proposal_key),
          FOREIGN KEY (postmortem_id, version) REFERENCES {versions},
          {_hash_check(proposals)}
        );
        CREATE TABLE {revisions} (
          entry_id uuid NOT NULL REFERENCES opspilot_knowledge_entries,
          revision integer NOT NULL CHECK (revision > 0),
          name text NOT NULL CHECK (name <> ''),
          tags text[] NOT NULL,
          content text NOT NULL,
          content_sha256 text NOT NULL,
          source_postmortem_id uuid NOT NULL,
          source_version integer NOT NULL,
          source_proposal_key text NOT NULL,
          supersedes_revision integer,
          approved_by text NOT NULL CHECK (approved_by <> ''),
          approved_by_kind text NOT NULL,
          event_id uuid NOT NULL REFERENCES {audit},
          approved_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          PRIMARY KEY (entry_id, revision),
          FOREIGN KEY (source_postmortem_id, source_version, source_proposal_key) REFERENCES {proposals},
          FOREIGN KEY (entry_id, supersedes_revision) REFERENCES {revisions},
          CONSTRAINT {revisions}_supersedes_check CHECK (supersedes_revision IS NULL OR supersedes_revision = revision - 1),
          CONSTRAINT {revisions}_approved_by_kind_check CHECK (approved_by_kind = 'basic_auth'),
          {_hash_check(revisions)}
        );
        CREATE UNIQUE INDEX {revisions}_supersedes_idx ON {revisions}(entry_id, supersedes_revision)
          WHERE supersedes_revision IS NOT NULL;
        CREATE TABLE {revocations} (
          entry_id uuid NOT NULL,
          revision integer NOT NULL,
          reason text NOT NULL CHECK (reason <> ''),
          revoked_by text NOT NULL CHECK (revoked_by <> ''),
          revoked_by_kind text NOT NULL,
          event_id uuid NOT NULL REFERENCES {audit},
          revoked_at timestamptz NOT NULL DEFAULT clock_timestamp(),
          PRIMARY KEY (entry_id, revision),
          FOREIGN KEY (entry_id, revision) REFERENCES {revisions},
          CONSTRAINT {revocations}_revoked_by_kind_check CHECK (revoked_by_kind = 'basic_auth')
        );
        CREATE VIEW opspilot_knowledge_revision_states AS
          SELECT r.entry_id, r.revision,
                 CASE
                   WHEN x.entry_id IS NOT NULL THEN 'revoked'
                   WHEN s.entry_id IS NOT NULL THEN 'superseded'
                   ELSE 'active'
                 END AS state
          FROM {revisions} r
          LEFT JOIN {revocations} x ON x.entry_id = r.entry_id AND x.revision = r.revision
          LEFT JOIN {revisions} s ON s.entry_id = r.entry_id AND s.supersedes_revision = r.revision;
        """
    )
    _install_guards()


def _install_guards() -> None:
    # Shared helper: did this very transaction write an audit row for the
    # object with one of ``actions``? ``xmin`` equals the current
    # transaction id only for rows this transaction inserted (0003 uses the
    # same test). ``version``/``revision`` NULL match any.
    op.execute(
        """
        CREATE FUNCTION opspilot_f13_audited(
          p_kind text, p_object uuid, p_actions text[], p_version integer,
          p_revision integer, p_basic_auth boolean
        ) RETURNS boolean
        LANGUAGE sql STABLE SET search_path = pg_catalog, pg_temp AS $$
          SELECT EXISTS (
            SELECT 1 FROM public.opspilot_f13_audit a
            WHERE a.object_kind = p_kind AND a.object_id = p_object
              AND a.action = ANY (p_actions)
              AND (p_version IS NULL OR a.version = p_version)
              AND (p_revision IS NULL OR a.revision = p_revision)
              AND (NOT p_basic_auth OR a.principal_kind = 'basic_auth')
              AND a.xmin = pg_catalog.pg_current_xact_id()::xid
          )
        $$;

        CREATE FUNCTION opspilot_f13_refuse() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
          RAISE insufficient_privilege USING MESSAGE =
            pg_catalog.format('%s is append-only: %s refused', TG_TABLE_NAME, TG_OP);
        END
        $$;

        -- Object rows: identity fixed; the generation moves by exactly one,
        -- with the audit row carrying the resulting generation written in
        -- this transaction; the latest version/revision never goes back.
        CREATE FUNCTION opspilot_f13_object_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
          kind text := CASE TG_TABLE_NAME WHEN 'opspilot_postmortems' THEN 'postmortem' ELSE 'knowledge_entry' END;
          new_row jsonb := pg_catalog.to_jsonb(NEW);
          old_row jsonb := pg_catalog.to_jsonb(OLD);
          v_object uuid;
        BEGIN
          IF TG_OP = 'INSERT' THEN
            IF NEW.generation <> 0
               OR COALESCE((new_row ->> 'latest_version')::int, (new_row ->> 'latest_revision')::int) <> 0 THEN
              RAISE check_violation USING MESSAGE =
                pg_catalog.format('a new %s row starts at generation 0 and number 0', TG_TABLE_NAME);
            END IF;
            RETURN NEW;
          END IF;
          v_object := (new_row ->> CASE WHEN kind = 'postmortem' THEN 'postmortem_id' ELSE 'entry_id' END)::uuid;
          IF (new_row - 'generation' - 'latest_version' - 'latest_revision' - 'updated_at')
             IS DISTINCT FROM (old_row - 'generation' - 'latest_version' - 'latest_revision' - 'updated_at') THEN
            RAISE insufficient_privilege USING MESSAGE =
              pg_catalog.format('%s identity columns are immutable', TG_TABLE_NAME);
          END IF;
          IF NEW.generation <> OLD.generation + 1 THEN
            RAISE check_violation USING MESSAGE =
              pg_catalog.format('%s generation must advance by one (%s -> %s)', TG_TABLE_NAME, OLD.generation, NEW.generation);
          END IF;
          IF COALESCE((new_row ->> 'latest_version')::int, (new_row ->> 'latest_revision')::int)
             < COALESCE((old_row ->> 'latest_version')::int, (old_row ->> 'latest_revision')::int) THEN
            RAISE check_violation USING MESSAGE = pg_catalog.format('%s latest number went back', TG_TABLE_NAME);
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM public.opspilot_f13_audit a
            WHERE a.object_kind = kind AND a.object_id = v_object
              AND a.resulting_generation = NEW.generation
              AND a.expected_generation = OLD.generation
              AND a.xmin = pg_catalog.pg_current_xact_id()::xid
          ) THEN
            RAISE insufficient_privilege USING MESSAGE =
              pg_catalog.format('%s generation %s -> %s without an audit row', TG_TABLE_NAME, OLD.generation, NEW.generation);
          END IF;
          RETURN NEW;
        END
        $$;

        -- Versions: written as drafts under a generate audit row of this
        -- transaction at the latest version number; afterwards only state
        -- and stale_reason move, along the domain POSTMORTEM edges, each
        -- with its audit row; terminal states never change again (D7).
        CREATE FUNCTION opspilot_postmortem_version_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
          action text;
        BEGIN
          IF TG_OP = 'INSERT' THEN
            IF NEW.state <> 'draft' OR NEW.stale_reason IS NOT NULL THEN
              RAISE check_violation USING MESSAGE = 'a postmortem version is written as a draft';
            END IF;
            IF NEW.version <> (SELECT p.latest_version FROM public.opspilot_postmortems p WHERE p.postmortem_id = NEW.postmortem_id) THEN
              RAISE check_violation USING MESSAGE = 'a postmortem version must be the latest version number';
            END IF;
            IF NEW.revises_version IS NOT NULL AND NOT EXISTS (
              SELECT 1 FROM public.opspilot_postmortem_versions v
              WHERE v.postmortem_id = NEW.postmortem_id AND v.version = NEW.revises_version AND v.state = 'returned'
            ) THEN
              RAISE check_violation USING MESSAGE = 'only a returned version can be revised';
            END IF;
            IF NOT public.opspilot_f13_audited('postmortem', NEW.postmortem_id, ARRAY['generate'], NEW.version, NULL, false) THEN
              RAISE insufficient_privilege USING MESSAGE = 'a postmortem version needs its generate audit row';
            END IF;
            RETURN NEW;
          END IF;
          IF (pg_catalog.to_jsonb(NEW) - 'state' - 'stale_reason') IS DISTINCT FROM (pg_catalog.to_jsonb(OLD) - 'state' - 'stale_reason') THEN
            RAISE insufficient_privilege USING MESSAGE = 'postmortem version content and watermarks are immutable';
          END IF;
          action := CASE
            WHEN OLD.state = 'draft' AND NEW.state = 'under_review' THEN 'submit'
            WHEN OLD.state IN ('draft', 'under_review') AND NEW.state = 'stale' THEN 'mark_stale'
            WHEN OLD.state = 'under_review' AND NEW.state = 'approved' THEN 'approve'
            WHEN OLD.state = 'under_review' AND NEW.state = 'rejected' THEN 'reject'
            WHEN OLD.state = 'under_review' AND NEW.state = 'returned' THEN 'return'
          END;
          IF action IS NULL THEN
            RAISE insufficient_privilege USING MESSAGE =
              pg_catalog.format('postmortem version %s -> %s refused', OLD.state, NEW.state);
          END IF;
          IF NOT public.opspilot_f13_audited('postmortem', NEW.postmortem_id, ARRAY[action], NEW.version, NULL,
                                             action IN ('approve', 'reject', 'return')) THEN
            RAISE insufficient_privilege USING MESSAGE =
              pg_catalog.format('postmortem version %s -> %s without a %s audit row', OLD.state, NEW.state, action);
          END IF;
          IF action IN ('submit', 'approve') AND EXISTS (
            SELECT 1 FROM public.opspilot_postmortem_conclusions c
            WHERE c.postmortem_id = NEW.postmortem_id AND c.version = NEW.version AND NOT c.citations_valid
          ) THEN
            RAISE check_violation USING MESSAGE = 'a version with failed citations cannot enter or pass review';
          END IF;
          IF action = 'approve' AND EXISTS (
            SELECT 1 FROM public.opspilot_postmortem_disputes d
            WHERE d.postmortem_id = NEW.postmortem_id AND d.version = NEW.version
          ) THEN
            RAISE check_violation USING MESSAGE = 'a version with a disputed conclusion cannot be approved';
          END IF;
          RETURN NEW;
        END
        $$;

        -- Conclusions and proposals are part of the draft: written only in
        -- the transaction that wrote their version.
        CREATE FUNCTION opspilot_postmortem_part_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM public.opspilot_postmortem_versions v
            WHERE v.postmortem_id = NEW.postmortem_id AND v.version = NEW.version
              AND v.state = 'draft' AND v.xmin = pg_catalog.pg_current_xact_id()::xid
          ) THEN
            RAISE insufficient_privilege USING MESSAGE =
              pg_catalog.format('%s rows are written with their draft version only', TG_TABLE_NAME);
          END IF;
          RETURN NEW;
        END
        $$;

        CREATE FUNCTION opspilot_postmortem_dispute_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM public.opspilot_postmortem_versions v
            WHERE v.postmortem_id = NEW.postmortem_id AND v.version = NEW.version
              AND v.state IN ('draft', 'under_review')
          ) THEN
            RAISE check_violation USING MESSAGE = 'disputes are raised on a draft or a version under review';
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM public.opspilot_f13_audit a
            WHERE a.event_id = NEW.event_id AND a.object_kind = 'postmortem'
              AND a.object_id = NEW.postmortem_id AND a.version = NEW.version
              AND a.action IN ('generate', 'dispute')
              AND a.actor_id = NEW.raised_by AND a.principal_kind = NEW.raised_by_kind
              AND a.xmin = pg_catalog.pg_current_xact_id()::xid
          ) THEN
            RAISE insufficient_privilege USING MESSAGE = 'a dispute needs its audit row in this transaction';
          END IF;
          RETURN NEW;
        END
        $$;

        -- Revisions: the latest revision number of the entry, from a
        -- proposal of an approved version, under a publish/supersede audit
        -- row of a basic_auth principal; a new revision supersedes the
        -- previous one exactly when that one is still active.
        CREATE FUNCTION opspilot_knowledge_revision_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
          previous_active boolean;
        BEGIN
          IF NEW.revision <> (SELECT e.latest_revision FROM public.opspilot_knowledge_entries e WHERE e.entry_id = NEW.entry_id) THEN
            RAISE check_violation USING MESSAGE = 'a knowledge revision must be the latest revision number';
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM public.opspilot_postmortem_versions v
            WHERE v.postmortem_id = NEW.source_postmortem_id AND v.version = NEW.source_version AND v.state = 'approved'
          ) THEN
            RAISE check_violation USING MESSAGE = 'knowledge comes only from an approved postmortem version';
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM public.opspilot_postmortem_proposals p
            WHERE p.postmortem_id = NEW.source_postmortem_id AND p.version = NEW.source_version
              AND p.proposal_key = NEW.source_proposal_key
              AND p.content_sha256 = NEW.content_sha256 AND p.name = NEW.name AND p.tags = NEW.tags
          ) THEN
            RAISE check_violation USING MESSAGE = 'a knowledge revision must equal its approved proposal';
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM public.opspilot_f13_audit a
            WHERE a.event_id = NEW.event_id AND a.object_kind = 'knowledge_entry' AND a.object_id = NEW.entry_id
              AND a.action IN ('publish', 'supersede') AND a.revision = NEW.revision
              AND a.principal_kind = 'basic_auth' AND a.actor_id = NEW.approved_by
              AND (a.action = 'supersede') = (NEW.supersedes_revision IS NOT NULL)
              AND a.xmin = pg_catalog.pg_current_xact_id()::xid
          ) THEN
            RAISE insufficient_privilege USING MESSAGE = 'a knowledge revision needs its publish/supersede audit row';
          END IF;
          IF NEW.revision > 1 THEN
            SELECT s.state = 'active' INTO previous_active
            FROM public.opspilot_knowledge_revision_states s
            WHERE s.entry_id = NEW.entry_id AND s.revision = NEW.revision - 1;
            IF previous_active AND NEW.supersedes_revision IS NULL THEN
              RAISE check_violation USING MESSAGE = 'a new revision must supersede the active one';
            END IF;
            IF NOT previous_active AND NEW.supersedes_revision IS NOT NULL THEN
              RAISE check_violation USING MESSAGE = 'only an active revision can be superseded';
            END IF;
          ELSIF NEW.supersedes_revision IS NOT NULL THEN
            RAISE check_violation USING MESSAGE = 'a first revision supersedes nothing';
          END IF;
          RETURN NEW;
        END
        $$;

        CREATE FUNCTION opspilot_knowledge_revocation_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM public.opspilot_knowledge_revision_states s
            WHERE s.entry_id = NEW.entry_id AND s.revision = NEW.revision AND s.state = 'active'
          ) THEN
            RAISE check_violation USING MESSAGE = 'only an active knowledge revision can be revoked';
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM public.opspilot_f13_audit a
            WHERE a.event_id = NEW.event_id AND a.object_kind = 'knowledge_entry' AND a.object_id = NEW.entry_id
              AND a.action = 'revoke' AND a.revision = NEW.revision
              AND a.principal_kind = 'basic_auth' AND a.actor_id = NEW.revoked_by
              AND a.reason = NEW.reason
              AND a.xmin = pg_catalog.pg_current_xact_id()::xid
          ) THEN
            RAISE insufficient_privilege USING MESSAGE = 'a revocation needs its revoke audit row';
          END IF;
          RETURN NEW;
        END
        $$;

        CREATE TRIGGER opspilot_postmortems_guard BEFORE INSERT OR UPDATE ON opspilot_postmortems
          FOR EACH ROW EXECUTE FUNCTION opspilot_f13_object_guard();
        CREATE TRIGGER opspilot_knowledge_entries_guard BEFORE INSERT OR UPDATE ON opspilot_knowledge_entries
          FOR EACH ROW EXECUTE FUNCTION opspilot_f13_object_guard();
        CREATE TRIGGER opspilot_postmortem_versions_guard BEFORE INSERT OR UPDATE ON opspilot_postmortem_versions
          FOR EACH ROW EXECUTE FUNCTION opspilot_postmortem_version_guard();
        CREATE TRIGGER opspilot_postmortem_conclusions_guard BEFORE INSERT ON opspilot_postmortem_conclusions
          FOR EACH ROW EXECUTE FUNCTION opspilot_postmortem_part_guard();
        CREATE TRIGGER opspilot_postmortem_proposals_guard BEFORE INSERT ON opspilot_postmortem_proposals
          FOR EACH ROW EXECUTE FUNCTION opspilot_postmortem_part_guard();
        CREATE TRIGGER opspilot_postmortem_disputes_guard BEFORE INSERT ON opspilot_postmortem_disputes
          FOR EACH ROW EXECUTE FUNCTION opspilot_postmortem_dispute_guard();
        CREATE TRIGGER opspilot_knowledge_revisions_guard BEFORE INSERT ON opspilot_knowledge_revisions
          FOR EACH ROW EXECUTE FUNCTION opspilot_knowledge_revision_guard();
        CREATE TRIGGER opspilot_knowledge_revocations_guard BEFORE INSERT ON opspilot_knowledge_revocations
          FOR EACH ROW EXECUTE FUNCTION opspilot_knowledge_revocation_guard();
        """
    )
    for table in TABLES:
        op.execute(
            f"""
            CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table}
              FOR EACH ROW EXECUTE FUNCTION opspilot_f13_refuse();
            CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table}
              FOR EACH STATEMENT EXECUTE FUNCTION opspilot_f13_refuse();
            """
        )
    for table in FROZEN_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table}
              FOR EACH ROW EXECUTE FUNCTION opspilot_f13_refuse();
            """
        )


def downgrade() -> None:
    op.execute(
        """
        DROP VIEW opspilot_knowledge_revision_states;
        DROP TABLE opspilot_knowledge_revocations;
        DROP TABLE opspilot_knowledge_revisions;
        DROP TABLE opspilot_postmortem_proposals;
        DROP TABLE opspilot_knowledge_entries;
        DROP TABLE opspilot_postmortem_disputes;
        DROP TABLE opspilot_postmortem_conclusions;
        DROP TABLE opspilot_postmortem_versions;
        DROP TABLE opspilot_postmortems;
        DROP TABLE opspilot_f13_audit;
        DROP TABLE opspilot_f13_requests;
        DROP FUNCTION opspilot_knowledge_revocation_guard();
        DROP FUNCTION opspilot_knowledge_revision_guard();
        DROP FUNCTION opspilot_postmortem_dispute_guard();
        DROP FUNCTION opspilot_postmortem_part_guard();
        DROP FUNCTION opspilot_postmortem_version_guard();
        DROP FUNCTION opspilot_f13_object_guard();
        DROP FUNCTION opspilot_f13_refuse();
        DROP FUNCTION opspilot_f13_audited(text, uuid, text[], integer, integer, boolean);
        """
    )
