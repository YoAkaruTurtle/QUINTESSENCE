-- PCS2-008: Belief revision, minimal SelfView, Context and FakeModel step.
--
-- Additive only.  0001_init.sql .. 0004_persona_episodes.sql stay byte
-- identical: this migration does not relax or replace any trigger, CHECK or
-- foreign key from PCS2-004/005/006/007.
--
-- Two decisions are structural rather than conventional:
--
--   * ``pending_work`` is NOT rebuilt.  0002 froze its CHECK to
--     ``status = 'pending'``; completion therefore lives in the separate
--     ``work_progress`` table and a pending row is never rewritten.
--   * the PCS2-006 column set of ``context_snapshots`` stays nullable at the
--     schema level so the new columns can be added with ALTER TABLE.  A row is
--     a *valid PCS2-008 Context* only when every J.5 column below is non-null;
--     a 006-era row (all new columns NULL) is answered with NEEDS_CONTEXT and
--     is never interpreted as an empty read_set or a complete model input.
--     ``adapter_kind`` is the one exception: it stays nullable because a
--     context assembled without a model call has no adapter, while
--     ``model_input_complete = 1`` requires it to be set.

-- ------------------------------------------------------- context_snapshots
-- The 22 J.5 ContextEnvelope fields not already present from 0003, plus the
-- two PCS2-008 evidence columns named by the handoff card.  Every column is
-- nullable so ALTER TABLE works; the completeness rule is enforced by the
-- store on read and write, not by a table-wide NOT NULL constraint (which
-- SQLite cannot add to an existing table in place).

ALTER TABLE context_snapshots
    ADD COLUMN run_id TEXT
        CHECK (run_id IS NULL OR length(run_id) BETWEEN 1 AND 256);

ALTER TABLE context_snapshots
    ADD COLUMN trigger_ref_json TEXT
        CHECK (trigger_ref_json IS NULL OR length(trigger_ref_json) BETWEEN 2 AND 16384);

ALTER TABLE context_snapshots
    ADD COLUMN profile_ref_json TEXT
        CHECK (profile_ref_json IS NULL OR length(profile_ref_json) BETWEEN 2 AND 4096);

ALTER TABLE context_snapshots
    ADD COLUMN read_set_json TEXT
        CHECK (read_set_json IS NULL OR length(read_set_json) BETWEEN 2 AND 1048576);

ALTER TABLE context_snapshots
    ADD COLUMN memory_hits_json TEXT
        CHECK (memory_hits_json IS NULL OR length(memory_hits_json) BETWEEN 2 AND 1048576);

ALTER TABLE context_snapshots
    ADD COLUMN belief_refs_json TEXT
        CHECK (belief_refs_json IS NULL OR length(belief_refs_json) BETWEEN 2 AND 1048576);

ALTER TABLE context_snapshots
    ADD COLUMN question_refs_json TEXT
        CHECK (question_refs_json IS NULL OR length(question_refs_json) BETWEEN 2 AND 1048576);

ALTER TABLE context_snapshots
    ADD COLUMN focus_refs_json TEXT
        CHECK (focus_refs_json IS NULL OR length(focus_refs_json) BETWEEN 2 AND 1048576);

ALTER TABLE context_snapshots
    ADD COLUMN unknown_execution_ids_json TEXT
        CHECK (unknown_execution_ids_json IS NULL
               OR length(unknown_execution_ids_json) BETWEEN 2 AND 1048576);

ALTER TABLE context_snapshots
    ADD COLUMN permission_view_ref TEXT
        CHECK (permission_view_ref IS NULL OR length(permission_view_ref) BETWEEN 1 AND 512);

ALTER TABLE context_snapshots
    ADD COLUMN budget_view_json TEXT
        CHECK (budget_view_json IS NULL OR length(budget_view_json) BETWEEN 2 AND 16384);

ALTER TABLE context_snapshots
    ADD COLUMN pending_source_event_refs_json TEXT
        CHECK (pending_source_event_refs_json IS NULL
               OR length(pending_source_event_refs_json) BETWEEN 2 AND 1048576);

ALTER TABLE context_snapshots
    ADD COLUMN omissions_json TEXT
        CHECK (omissions_json IS NULL OR length(omissions_json) BETWEEN 2 AND 1048576);

ALTER TABLE context_snapshots
    ADD COLUMN index_generation INTEGER
        CHECK (index_generation IS NULL
               OR (index_generation >= 0 AND index_generation <= 9007199254740991));

ALTER TABLE context_snapshots
    ADD COLUMN index_coverage_json TEXT
        CHECK (index_coverage_json IS NULL OR length(index_coverage_json) BETWEEN 2 AND 16384);

-- ``sha256:<64 lower-case hex>`` of the exact rendered input text.
ALTER TABLE context_snapshots
    ADD COLUMN content_hash TEXT
        CHECK (content_hash IS NULL
               OR (length(content_hash) = 71 AND content_hash LIKE 'sha256:%'));

ALTER TABLE context_snapshots
    ADD COLUMN model_input_complete INTEGER
        CHECK (model_input_complete IS NULL OR model_input_complete IN (0, 1));

-- The controlled private payload that holds the exact rendered input.  The
-- hash above is recomputed from this text on every read; storing only the
-- hash is explicitly not enough (handoff card C1).
ALTER TABLE context_snapshots
    ADD COLUMN rendered_input_payload_ref TEXT
        CHECK (rendered_input_payload_ref IS NULL
               OR length(rendered_input_payload_ref) BETWEEN 1 AND 1024);

-- Nullable by design: a context built without a model call has no adapter.
-- ``model_input_complete = 1`` is only written together with
-- ``'fake-model'`` in PCS2-008 (C3).
ALTER TABLE context_snapshots
    ADD COLUMN adapter_kind TEXT
        CHECK (adapter_kind IS NULL OR length(adapter_kind) BETWEEN 1 AND 64);

-- ---------------------------------------------------------- context_sources
-- Which exact sources this Context actually used.  The point is deletion:
-- when a source is cleaned, every Context that rendered a fragment derived
-- from it must be found and its rendered payload cleared.  ``ref_version`` is
-- 0 for events (events have no version) and the exact record_version for
-- records.  Rows are immutable: a Context's source list is evidence of what
-- was read, not a mutable cache.
CREATE TABLE context_sources (
    context_id  TEXT NOT NULL CHECK (length(context_id) BETWEEN 1 AND 128),
    ref_kind    TEXT NOT NULL CHECK (ref_kind IN ('event', 'record')),
    ref_id      TEXT NOT NULL CHECK (length(ref_id) = 36),
    ref_version INTEGER NOT NULL CHECK (ref_version >= 0),
    PRIMARY KEY (context_id, ref_kind, ref_id, ref_version),
    CHECK ((ref_kind = 'event' AND ref_version = 0)
        OR (ref_kind = 'record' AND ref_version >= 1)),
    FOREIGN KEY (context_id) REFERENCES context_snapshots (context_id)
);

-- Cleanup goes from a deleted target back to the contexts that used it.
CREATE INDEX context_sources_target_idx
    ON context_sources (ref_kind, ref_id, context_id);

CREATE TRIGGER context_sources_no_update
BEFORE UPDATE ON context_sources
BEGIN
    SELECT RAISE(ABORT, 'context_sources rows are immutable');
END;

CREATE TRIGGER context_sources_no_delete
BEFORE DELETE ON context_sources
BEGIN
    SELECT RAISE(ABORT, 'context_sources rows are immutable');
END;

-- ------------------------------------------------------------- work_progress
-- Completion of one ``pending_work`` row.  ``pending_work`` itself is never
-- updated: its 0002 CHECK is frozen.  The primary key is the concurrency
-- backstop promised by the card: two transactions may both pass every
-- watermark check, but only one can insert the completion row, and the loser
-- rolls back its whole transaction.
CREATE TABLE work_progress (
    work_id                  TEXT NOT NULL PRIMARY KEY CHECK (length(work_id) = 36),
    namespace                TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    outcome                  TEXT NOT NULL
                             CHECK (outcome IN ('committed', 'checked_no_belief')),
    context_id               TEXT NOT NULL CHECK (length(context_id) BETWEEN 1 AND 128),
    completed_state_revision INTEGER NOT NULL
                             CHECK (completed_state_revision >= 0),
    updated_at               TEXT NOT NULL CHECK (length(updated_at) BETWEEN 20 AND 27),
    FOREIGN KEY (work_id) REFERENCES pending_work (work_id),
    FOREIGN KEY (context_id) REFERENCES context_snapshots (context_id)
);

CREATE INDEX work_progress_namespace_idx
    ON work_progress (namespace, outcome);

CREATE TRIGGER work_progress_no_update
BEFORE UPDATE ON work_progress
BEGIN
    SELECT RAISE(ABORT, 'work_progress rows are immutable');
END;

CREATE TRIGGER work_progress_no_delete
BEFORE DELETE ON work_progress
BEGIN
    SELECT RAISE(ABORT, 'work_progress rows are immutable');
END;

-- --------------------------------------------------------------- retrieval
-- The handoff card asks for this exact composite index.  It complements the
-- (namespace, record_id, record_version) primary key for "current version of
-- kind" reads that also need the version column.
CREATE INDEX record_versions_kind_version_idx
    ON record_versions (namespace, kind, record_id, record_version);
