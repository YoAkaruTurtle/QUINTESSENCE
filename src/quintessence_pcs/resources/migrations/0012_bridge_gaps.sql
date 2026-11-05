-- J04: persist bridge gaps before the sync cursor advances.
--
-- Kept as a separate migration so the already-applied 0011 checksum stays
-- byte-identical.  A gap is durable evidence that one exact Experience ref was
-- not projected; it is resolved when a later sync succeeds.

CREATE TABLE experience_bridge_gap (
    namespace   TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    ref_type    TEXT NOT NULL CHECK (ref_type IN ('collab_input', 'collab_artifact')),
    ref_id      TEXT NOT NULL CHECK (length(ref_id) = 36),
    ref_version INTEGER NOT NULL CHECK (ref_version >= 1),
    reason      TEXT NOT NULL CHECK (length(reason) BETWEEN 1 AND 128),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    resolved_at TEXT,
    PRIMARY KEY (namespace, ref_type, ref_id, ref_version)
);

CREATE INDEX experience_bridge_gap_open_idx
ON experience_bridge_gap (namespace, resolved_at, updated_at);
