-- PCS2-005: the event journal, late causation edges, pending work and the
-- per-namespace event watermark (appendix J.2 / J.3 / J.6).
--
-- Additive only: 0001_init.sql has already been applied and is checksummed, so
-- the new tables arrive as migration 2 and the one existing table is extended
-- with ALTER TABLE. Later cards add further numbered migrations.

-- ``state_revision`` (domain commits, J.7) and ``last_event_seq`` (accepted
-- events, J.6) are separate watermarks on purpose: ingesting an event advances
-- this one and must never advance the other.
ALTER TABLE namespace_state
    ADD COLUMN last_event_seq INTEGER NOT NULL DEFAULT 0 CHECK (last_event_seq >= 0);

-- Accepted events. No private body is stored here: ``payload_ref`` points into
-- the controlled payload area created by 0001 (J.3). The source idempotency
-- key is the UNIQUE constraint, not an in-process cache (J.2).
CREATE TABLE event_journal (
    namespace       TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    event_id        TEXT NOT NULL CHECK (length(event_id) = 36),
    event_seq       INTEGER NOT NULL CHECK (event_seq >= 1),
    source_kind     TEXT NOT NULL CHECK (source_kind IN (
                        'host_observation', 'user_report', 'imported_memory',
                        'model_inference', 'synthetic_fixture', 'system_internal')),
    source_instance TEXT NOT NULL CHECK (length(source_instance) BETWEEN 1 AND 256),
    source_event_id TEXT NOT NULL CHECK (length(source_event_id) BETWEEN 1 AND 512),
    source_position INTEGER CHECK (
                        source_position IS NULL
                        OR (source_position >= 0 AND source_position <= 9007199254740991)),
    event_type      TEXT NOT NULL CHECK (length(event_type) BETWEEN 1 AND 128),
    observed_at     TEXT CHECK (
                        observed_at IS NULL OR length(observed_at) BETWEEN 20 AND 27),
    recorded_at     TEXT NOT NULL CHECK (length(recorded_at) BETWEEN 20 AND 27),
    payload_ref     TEXT NOT NULL CHECK (length(payload_ref) BETWEEN 1 AND 1024),
    privacy_class   TEXT NOT NULL CHECK (
                        privacy_class IN ('private', 'restricted', 'public-example')),
    PRIMARY KEY (namespace, event_id),
    -- Receive order is per namespace; a second namespace numbers from 1 again.
    UNIQUE (namespace, event_seq),
    -- The frozen idempotency key (namespace, source_instance, source_event_id).
    UNIQUE (namespace, source_instance, source_event_id),
    FOREIGN KEY (payload_ref) REFERENCES private_payloads (payload_ref)
);

-- Events are accepted facts and are not rewritten in place. PCS2-006 (privacy
-- deletion) is the explicit exception and will relax these triggers with its
-- own migration, exactly as 0001's record-version triggers will be relaxed.
CREATE TRIGGER event_journal_no_update
BEFORE UPDATE ON event_journal
BEGIN
    SELECT RAISE(ABORT, 'event_journal rows are immutable');
END;

CREATE TRIGGER event_journal_no_delete
BEFORE DELETE ON event_journal
BEGIN
    SELECT RAISE(ABORT, 'event_journal rows are immutable');
END;

-- Causation edges. ``to_event_id`` deliberately has no foreign key: a late
-- reference to an event that has not arrived yet is allowed and is stored as
-- status='pending' (J.2 / the PCS2-005 card). PCS2-005 implements no
-- resolution; a later card flips pending -> resolved.
CREATE TABLE event_causes (
    namespace     TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    from_event_id TEXT NOT NULL CHECK (length(from_event_id) = 36),
    to_event_id   TEXT NOT NULL CHECK (length(to_event_id) = 36),
    status        TEXT NOT NULL CHECK (status IN ('pending', 'resolved')),
    created_at    TEXT NOT NULL CHECK (length(created_at) BETWEEN 20 AND 27),
    PRIMARY KEY (namespace, from_event_id, to_event_id),
    CHECK (from_event_id <> to_event_id),
    FOREIGN KEY (namespace, from_event_id) REFERENCES event_journal (namespace, event_id)
);

-- The future resolution card looks pending edges up by their target.
CREATE INDEX event_causes_target_idx
    ON event_causes (namespace, to_event_id, status);

-- Work created inside Tx A. PCS2-005 only writes the 'pending' row; consuming
-- or scheduling it belongs to PCS2-007/016, which add their own states with a
-- new migration instead of widening this CHECK now.
CREATE TABLE pending_work (
    work_id         TEXT NOT NULL PRIMARY KEY CHECK (length(work_id) = 36),
    namespace       TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    kind            TEXT NOT NULL CHECK (length(kind) BETWEEN 1 AND 128),
    event_id        TEXT NOT NULL CHECK (length(event_id) = 36),
    source_event_id TEXT NOT NULL CHECK (length(source_event_id) BETWEEN 1 AND 512),
    status          TEXT NOT NULL CHECK (status = 'pending'),
    created_at      TEXT NOT NULL CHECK (length(created_at) BETWEEN 20 AND 27),
    -- One accepted event produces at most one piece of work of a given kind,
    -- so a duplicate delivery cannot create a second row even by raw SQL.
    UNIQUE (namespace, event_id, kind),
    FOREIGN KEY (namespace, event_id) REFERENCES event_journal (namespace, event_id)
);

CREATE INDEX pending_work_status_idx
    ON pending_work (namespace, status);
