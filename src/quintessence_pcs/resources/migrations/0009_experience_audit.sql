-- C05c: append-only audit of experience reads.
--
-- One row records one successful resolution of a collaboration reference to a
-- stored object.  Missing and unauthorized reads write nothing, so an arbitrary
-- identifier cannot flood this table and the audit does not distinguish those
-- two cases.  Only metadata is stored: the ref, the caller's access label, the
-- purpose and the final readability.  No body text, digest or summary is here.
--
-- Rows are never updated or deleted; the read path only inserts.

CREATE TABLE collab_experience_reads (
    read_id       TEXT PRIMARY KEY CHECK (length(read_id) = 36),
    namespace     TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    host_instance TEXT NOT NULL CHECK (length(host_instance) BETWEEN 1 AND 256),
    access_ref    TEXT NOT NULL CHECK (length(access_ref) BETWEEN 1 AND 512),
    purpose       TEXT NOT NULL CHECK (length(purpose) BETWEEN 1 AND 64),
    ref_type      TEXT NOT NULL CHECK (ref_type IN ('collab_input', 'collab_artifact')),
    ref_id        TEXT NOT NULL CHECK (length(ref_id) = 36),
    ref_version   INTEGER NOT NULL CHECK (ref_version >= 1),
    outcome       TEXT NOT NULL CHECK (outcome IN ('available', 'redacted', 'unavailable')),
    read_at       TEXT NOT NULL CHECK (length(read_at) BETWEEN 20 AND 27)
);

CREATE INDEX collab_experience_reads_ns_idx
    ON collab_experience_reads (namespace, read_at);

CREATE TRIGGER collab_experience_reads_no_update
BEFORE UPDATE ON collab_experience_reads
BEGIN
    SELECT RAISE(ABORT, 'collab_experience_reads rows are audit evidence');
END;

CREATE TRIGGER collab_experience_reads_no_delete
BEFORE DELETE ON collab_experience_reads
BEGIN
    SELECT RAISE(ABORT, 'collab_experience_reads rows are audit evidence');
END;
