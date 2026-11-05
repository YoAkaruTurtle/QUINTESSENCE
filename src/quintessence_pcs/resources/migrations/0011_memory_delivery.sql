-- J04: Experience source bridge, delivery outbox and cleanup todos.
--
-- Additive only: no existing table or column changes.  The bridge keeps the
-- original experience ref/version, a single authorized privacy class (the
-- artifact closure is stored as a JSON set even when it has one element), the
-- exact source dependencies used for deletion invalidation, and a stable
-- delivery record.  Bodies are never copied here.

CREATE TABLE experience_source_mapping (
    namespace                TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    ref_type                 TEXT NOT NULL CHECK (ref_type IN ('collab_input', 'collab_artifact')),
    ref_id                   TEXT NOT NULL CHECK (length(ref_id) = 36),
    ref_version              INTEGER NOT NULL CHECK (ref_version >= 1),
    event_id                 TEXT NOT NULL CHECK (length(event_id) = 36),
    source_event_key         TEXT NOT NULL CHECK (length(source_event_key) BETWEEN 1 AND 512),
    source_revision          INTEGER NOT NULL CHECK (source_revision >= 1),
    privacy_classes_json     TEXT NOT NULL CHECK (json_valid(privacy_classes_json)),
    retention                TEXT NOT NULL,
    origin                   TEXT NOT NULL,
    role                     TEXT NOT NULL,
    principal_kind           TEXT NOT NULL,
    root_source_key          TEXT NOT NULL CHECK (length(root_source_key) = 64),
    root_members_json        TEXT NOT NULL CHECK (json_valid(root_members_json)),
    root_verified            INTEGER NOT NULL CHECK (root_verified IN (0, 1)),
    source_dependencies_json TEXT NOT NULL CHECK (json_valid(source_dependencies_json)),
    delivery_id              TEXT NOT NULL CHECK (length(delivery_id) = 36),
    format_version           TEXT NOT NULL,
    created_at               TEXT NOT NULL,
    invalidated_at           TEXT,
    invalidation_reason      TEXT,
    PRIMARY KEY (namespace, ref_type, ref_id, ref_version),
    UNIQUE (namespace, event_id),
    UNIQUE (namespace, source_event_key, source_revision)
);

CREATE INDEX experience_source_mapping_event_idx
ON experience_source_mapping (namespace, event_id);

CREATE TABLE memory_delivery (
    delivery_id        TEXT PRIMARY KEY CHECK (length(delivery_id) = 36),
    namespace          TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    event_id           TEXT NOT NULL CHECK (length(event_id) = 36),
    ref_type           TEXT NOT NULL CHECK (ref_type IN ('collab_input', 'collab_artifact')),
    ref_id             TEXT NOT NULL CHECK (length(ref_id) = 36),
    ref_version        INTEGER NOT NULL CHECK (ref_version >= 1),
    source_event_key   TEXT NOT NULL CHECK (length(source_event_key) BETWEEN 1 AND 512),
    source_revision    INTEGER NOT NULL CHECK (source_revision >= 1),
    request_hash       TEXT NOT NULL CHECK (length(request_hash) = 64),
    request_json       TEXT NOT NULL CHECK (json_valid(request_json)),
    state              TEXT NOT NULL CHECK (state IN (
                           'pending', 'applied', 'verified', 'needs_review', 'blocked', 'obsolete')),
    attempt            INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    lease_token        TEXT,
    lease_until        TEXT,
    next_retry_at      TEXT,
    external_ref       TEXT,
    external_revision  INTEGER,
    receipt_json       TEXT,
    last_error         TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    invalidated_at     TEXT,
    invalidation_reason TEXT
);

CREATE INDEX memory_delivery_state_idx
ON memory_delivery (namespace, state, next_retry_at);

CREATE INDEX memory_delivery_event_idx
ON memory_delivery (namespace, event_id);

CREATE TABLE memory_cleanup_todo (
    todo_id           TEXT PRIMARY KEY CHECK (length(todo_id) = 36),
    namespace         TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    ref_type          TEXT NOT NULL CHECK (ref_type IN (
                          'collab_input', 'collab_artifact', 'event', 'record')),
    ref_id            TEXT NOT NULL,
    ref_version       INTEGER NOT NULL CHECK (ref_version >= 0),
    event_id          TEXT,
    delivery_id       TEXT,
    source_event_key  TEXT,
    source_revision   INTEGER,
    external_ref      TEXT,
    external_revision INTEGER,
    unknown_result    INTEGER NOT NULL CHECK (unknown_result IN (0, 1)),
    reason            TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    resolved_at       TEXT
);

CREATE INDEX memory_cleanup_todo_open_idx
ON memory_cleanup_todo (namespace, resolved_at, created_at);

CREATE TABLE experience_bridge_state (
    namespace  TEXT PRIMARY KEY CHECK (length(namespace) BETWEEN 1 AND 128),
    cursor     TEXT,
    updated_at TEXT NOT NULL
);
