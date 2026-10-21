-- PCS2-004 initial schema (appendix J.3): a current-version pointer over an
-- immutable version table, with record bodies held in a controlled payload area.
--
-- This migration is immutable once applied: schema_migrations stores the
-- SHA-256 of these exact bytes and the runner refuses to open the database if
-- the file changes. Later cards add new numbered migrations; they never edit
-- this file.

CREATE TABLE schema_migrations (
    migration_id INTEGER PRIMARY KEY CHECK (migration_id >= 1),
    name         TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 128),
    checksum     TEXT NOT NULL CHECK (length(checksum) = 64),
    applied_at   TEXT NOT NULL CHECK (length(applied_at) BETWEEN 20 AND 27)
);

CREATE TRIGGER schema_migrations_no_update
BEFORE UPDATE ON schema_migrations
BEGIN
    SELECT RAISE(ABORT, 'schema_migrations is append-only');
END;

CREATE TRIGGER schema_migrations_no_delete
BEFORE DELETE ON schema_migrations
BEGIN
    SELECT RAISE(ABORT, 'schema_migrations is append-only');
END;

CREATE TABLE namespace_state (
    namespace      TEXT PRIMARY KEY CHECK (length(namespace) BETWEEN 1 AND 128),
    state_revision INTEGER NOT NULL DEFAULT 0 CHECK (state_revision >= 0),
    -- Reserved for the PCS2-006 deletion/privacy work. This card stores the
    -- column but implements no privacy_epoch behaviour.
    privacy_epoch  INTEGER NOT NULL DEFAULT 0 CHECK (privacy_epoch >= 0)
);

-- Controlled body area. Bodies are canonical UTF-8 JSON text addressed by the
-- SHA-256 of those bytes; record_versions points here, it never stores bodies.
CREATE TABLE private_payloads (
    payload_ref TEXT PRIMARY KEY CHECK (length(payload_ref) BETWEEN 1 AND 1024),
    sha256      TEXT NOT NULL CHECK (length(sha256) = 64),
    body_json   TEXT NOT NULL,
    created_at  TEXT NOT NULL CHECK (length(created_at) BETWEEN 20 AND 27)
);

-- Current pointer only: (namespace, record_id) -> latest published version.
CREATE TABLE records (
    namespace       TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    record_id       TEXT NOT NULL CHECK (length(record_id) BETWEEN 1 AND 128),
    kind            TEXT NOT NULL CHECK (length(kind) BETWEEN 1 AND 128),
    current_version INTEGER NOT NULL CHECK (current_version >= 1),
    PRIMARY KEY (namespace, record_id)
);

-- Immutable published versions. The composite foreign key is the whole point
-- of the FK test: a version row cannot exist without its current-pointer row.
CREATE TABLE record_versions (
    namespace                TEXT NOT NULL,
    record_id                TEXT NOT NULL,
    record_version           INTEGER NOT NULL CHECK (record_version >= 1),
    kind                     TEXT NOT NULL CHECK (length(kind) BETWEEN 1 AND 128),
    schema_version           TEXT NOT NULL CHECK (length(schema_version) BETWEEN 1 AND 128),
    status                   TEXT NOT NULL CHECK (length(status) BETWEEN 1 AND 64),
    privacy_class            TEXT NOT NULL CHECK (privacy_class IN ('private', 'restricted', 'public-example')),
    recorded_at              TEXT NOT NULL CHECK (length(recorded_at) BETWEEN 20 AND 27),
    committed_state_revision INTEGER NOT NULL CHECK (committed_state_revision >= 1),
    body_payload_ref         TEXT NOT NULL,
    PRIMARY KEY (namespace, record_id, record_version),
    FOREIGN KEY (namespace, record_id) REFERENCES records (namespace, record_id),
    FOREIGN KEY (body_payload_ref) REFERENCES private_payloads (payload_ref)
);

-- Supports the future state.read(state_revision=r) projection (J.3).
CREATE INDEX record_versions_revision_idx
    ON record_versions (namespace, committed_state_revision);

-- Published versions are not edited in place. PCS2-006 (privacy deletion) is
-- the explicit exception; it will add its own migration that relaxes these
-- triggers rather than editing 0001.
CREATE TRIGGER record_versions_no_update
BEFORE UPDATE ON record_versions
BEGIN
    SELECT RAISE(ABORT, 'record_versions rows are immutable');
END;

CREATE TRIGGER record_versions_no_delete
BEFORE DELETE ON record_versions
BEGIN
    SELECT RAISE(ABORT, 'record_versions rows are immutable');
END;

-- kind is duplicated on record_versions so an exact read needs no pointer
-- table; this trigger keeps the copy honest for raw SQL inserts too.
CREATE TRIGGER record_versions_kind_matches_record
BEFORE INSERT ON record_versions
WHEN NEW.kind <> (SELECT kind FROM records WHERE namespace = NEW.namespace AND record_id = NEW.record_id)
BEGIN
    SELECT RAISE(ABORT, 'record_versions.kind must match records.kind');
END;
