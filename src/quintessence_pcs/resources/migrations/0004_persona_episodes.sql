-- PCS2-007: minimal PersonaProfile, Episode and current-version retrieval.
--
-- Additive only.  0001_init.sql, 0002_events.sql and 0003_txb_deletion.sql stay
-- byte-identical.  This migration adds
--
--   * the active PersonaProfile pointer on ``namespace_state``.  Both columns
--     are nullable and null means neutral: there is no "pick the newest"
--     fallback.  Triggers keep the pair set-or-cleared together;
--   * ``record_versions.scope_json``: the J.2 five-field retrieval scope as
--     canonical JSON text.  Rows written before this card keep NULL, which
--     means namespace-general; only new writes fill it;
--   * ``episode_sources``: the structural "one source event -> one episode"
--     mapping.  The (namespace, event_id) primary key is the concurrency
--     backstop, not an in-process cache;
--   * the (namespace, kind, record_id) index the current-version episode read
--     goes through;
--   * ``authorization_audit``: the non-sensitive audit trail the card requires
--     for a published Profile ("record the authorization in the version
--     metadata/audit").  It stores a reference label, never a body, and is
--     append-only.
--
-- No "persona table" and no "episode table" exist: a PersonaProfile and an
-- Episode are both unified Records whose ``kind`` is 'persona_profile' or
-- 'episode'.

-- ---------------------------------------------------------------- namespace

ALTER TABLE namespace_state
    ADD COLUMN active_profile_record_id TEXT
        CHECK (active_profile_record_id IS NULL OR length(active_profile_record_id) = 36);

ALTER TABLE namespace_state
    ADD COLUMN active_profile_version INTEGER
        CHECK (active_profile_version IS NULL OR active_profile_version >= 1);

-- Set-or-clear together: a half pointer would make "neutral" ambiguous.
CREATE TRIGGER namespace_state_active_profile_pair_insert
BEFORE INSERT ON namespace_state
WHEN (NEW.active_profile_record_id IS NULL) <> (NEW.active_profile_version IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'active profile pointer must be set or cleared as a pair');
END;

CREATE TRIGGER namespace_state_active_profile_pair_update
BEFORE UPDATE ON namespace_state
WHEN (NEW.active_profile_record_id IS NULL) <> (NEW.active_profile_version IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'active profile pointer must be set or cleared as a pair');
END;

-- ------------------------------------------------------------------- scope

-- Canonical JSON of the J.2 scope object.  NULL means "namespace-general";
-- an empty object is stored as NULL so the two representations cannot diverge.
ALTER TABLE record_versions
    ADD COLUMN scope_json TEXT
        CHECK (scope_json IS NULL OR length(scope_json) BETWEEN 2 AND 16384);

-- Current-version reads by kind: PCS2-007 episode search.
CREATE INDEX record_versions_kind_lookup_idx
    ON record_versions (namespace, kind, record_id);

-- ---------------------------------------------------------- episode sources

-- One source event maps to at most one Episode record.  ``record_version`` is
-- the exact version the mapping was created with; later versions of the same
-- episode keep the same mapping.  Rows are immutable: rebuilding an episode
-- after a deletion is refused instead of resurrecting it.
CREATE TABLE episode_sources (
    namespace      TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    event_id       TEXT NOT NULL CHECK (length(event_id) = 36),
    record_id      TEXT NOT NULL CHECK (length(record_id) = 36),
    record_version INTEGER NOT NULL CHECK (record_version >= 1),
    created_at     TEXT NOT NULL CHECK (length(created_at) BETWEEN 20 AND 27),
    PRIMARY KEY (namespace, event_id),
    FOREIGN KEY (namespace, event_id)
        REFERENCES event_journal (namespace, event_id),
    FOREIGN KEY (namespace, record_id, record_version)
        REFERENCES record_versions (namespace, record_id, record_version)
);

-- Search asks "which source events belong to this episode record".
CREATE INDEX episode_sources_record_idx
    ON episode_sources (namespace, record_id);

CREATE TRIGGER episode_sources_no_update
BEFORE UPDATE ON episode_sources
BEGIN
    SELECT RAISE(ABORT, 'episode_sources rows are immutable');
END;

CREATE TRIGGER episode_sources_no_delete
BEFORE DELETE ON episode_sources
BEGIN
    SELECT RAISE(ABORT, 'episode_sources rows are immutable');
END;

-- ------------------------------------------------------ authorization audit

-- Why this table exists: the PCS2-007 card requires publish_profile to record
-- its authorization in "version metadata / audit".  A dedicated append-only
-- table records the same fact for activation and episode building without
-- widening the immutable record_versions row beyond the one column the card
-- names (scope_json).  This is an audit ledger, not an authentication model:
-- the authorization_ref is a label produced by the caller that owns the admin
-- entry point, exactly like deletion_targets.authorization_ref in 0003.
CREATE TABLE authorization_audit (
    audit_id          TEXT NOT NULL PRIMARY KEY CHECK (length(audit_id) = 36),
    namespace         TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    operation         TEXT NOT NULL CHECK (length(operation) BETWEEN 1 AND 128),
    target_kind       TEXT NOT NULL CHECK (target_kind IN ('record', 'event')),
    target_id         TEXT NOT NULL CHECK (length(target_id) = 36),
    target_version    INTEGER CHECK (target_version IS NULL OR target_version >= 1),
    authorization_ref TEXT NOT NULL
                      CHECK (length(authorization_ref) BETWEEN 1 AND 512),
    recorded_at       TEXT NOT NULL CHECK (length(recorded_at) BETWEEN 20 AND 27)
);

CREATE INDEX authorization_audit_target_idx
    ON authorization_audit (namespace, target_kind, target_id);

CREATE TRIGGER authorization_audit_no_update
BEFORE UPDATE ON authorization_audit
BEGIN
    SELECT RAISE(ABORT, 'authorization_audit rows are immutable');
END;

CREATE TRIGGER authorization_audit_no_delete
BEFORE DELETE ON authorization_audit
BEGIN
    SELECT RAISE(ABORT, 'authorization_audit rows are immutable');
END;
