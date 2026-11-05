-- 0017: persistent conversations, turns, stages, Context links, steps,
-- public messages, ordered events and the history ledger.
--
-- Additive only.  No existing column, trigger or body table changes: every
-- new table stores references, never body text (bodies stay in
-- private_payloads through collab_inputs/collab_artifacts).  The single
-- existing-table change is one nullable column (context_snapshots.
-- renderer_version), which leaves every old row NULL.

-- ------------------------------------------------------- conversations
CREATE TABLE collab_conversations (
    conversation_id TEXT NOT NULL PRIMARY KEY
                    CHECK (length(conversation_id) = 36),
    namespace       TEXT NOT NULL
                    CHECK (length(namespace) BETWEEN 1 AND 128),
    host_instance   TEXT NOT NULL
                    CHECK (length(host_instance) BETWEEN 1 AND 256),
    mode            TEXT NOT NULL CHECK (mode IN ('chat', 'work')),
    -- RG02 never accepts a title from the WebView; a future card that does
    -- must store it as a private payload, not as plaintext here.
    title           TEXT CHECK (title IS NULL
                                OR length(title) BETWEEN 1 AND 200),
    next_turn_no    INTEGER NOT NULL DEFAULT 1
                    CHECK (next_turn_no BETWEEN 1 AND 1000000),
    created_at      TEXT NOT NULL
                    CHECK (length(created_at) BETWEEN 20 AND 27),
    updated_at      TEXT NOT NULL
                    CHECK (length(updated_at) BETWEEN 20 AND 27),
    UNIQUE (namespace, conversation_id)
);
CREATE INDEX collab_conversations_recent_idx
    ON collab_conversations (namespace, host_instance, updated_at DESC,
                             conversation_id DESC);

-- ------------------------------------------------------------- turns
CREATE TABLE collab_turns (
    turn_id                TEXT NOT NULL PRIMARY KEY
                           CHECK (length(turn_id) = 36),
    namespace              TEXT NOT NULL
                           CHECK (length(namespace) BETWEEN 1 AND 128),
    conversation_id        TEXT NOT NULL
                           CHECK (length(conversation_id) = 36),
    turn_no                INTEGER NOT NULL
                           CHECK (turn_no BETWEEN 1 AND 1000000),
    client_request_id      TEXT NOT NULL
                           CHECK (length(client_request_id) BETWEEN 1 AND 128),
    client_request_digest  TEXT NOT NULL
                           CHECK (length(client_request_digest) = 64
                                  AND client_request_digest NOT GLOB '*[^0-9a-f]*'),
    input_ref_json         TEXT NOT NULL
                           CHECK (length(input_ref_json) BETWEEN 2 AND 16384),
    state                  TEXT NOT NULL CHECK (state IN (
                               'accepted', 'running', 'waiting',
                               'completed', 'failed', 'cancelled',
                               'interrupted')),
    profile_ref_json       TEXT NOT NULL
                           CHECK (length(profile_ref_json) BETWEEN 2 AND 16384),
    mode                   TEXT NOT NULL CHECK (mode IN ('chat', 'work')),
    strategy_json          TEXT NOT NULL
                           CHECK (length(strategy_json) BETWEEN 2 AND 16384),
    last_event_seq         INTEGER NOT NULL DEFAULT 0
                           CHECK (last_event_seq BETWEEN 0 AND 1000000),
    final_message_ref      TEXT
                           CHECK (final_message_ref IS NULL
                                  OR length(final_message_ref) = 36),
    created_at             TEXT NOT NULL
                           CHECK (length(created_at) BETWEEN 20 AND 27),
    updated_at             TEXT NOT NULL
                           CHECK (length(updated_at) BETWEEN 20 AND 27),
    started_at             TEXT CHECK (started_at IS NULL
                                      OR length(started_at) BETWEEN 20 AND 27),
    completed_at           TEXT CHECK (completed_at IS NULL
                                      OR length(completed_at) BETWEEN 20 AND 27),
    -- Required by every composite child FK below and by v5 §5.3.14.
    UNIQUE (namespace, turn_id),
    UNIQUE (namespace, conversation_id, turn_no),
    UNIQUE (namespace, conversation_id, client_request_id),
    FOREIGN KEY (namespace, conversation_id)
        REFERENCES collab_conversations (namespace, conversation_id)
);
CREATE INDEX collab_turns_state_idx ON collab_turns (namespace, state);

-- ------------------------------------------------------------ stages
CREATE TABLE collab_turn_stages (
    stage_id       TEXT NOT NULL PRIMARY KEY
                   CHECK (length(stage_id) = 36),
    namespace      TEXT NOT NULL
                   CHECK (length(namespace) BETWEEN 1 AND 128),
    turn_id        TEXT NOT NULL CHECK (length(turn_id) = 36),
    kind           TEXT NOT NULL CHECK (kind IN ('content', 'expression')),
    status         TEXT NOT NULL CHECK (status IN (
                       'pending', 'running', 'succeeded', 'failed',
                       'skipped', 'cancelled')),
    policy_version TEXT NOT NULL
                   CHECK (length(policy_version) BETWEEN 1 AND 128),
    skip_reason    TEXT CHECK (skip_reason IS NULL
                               OR length(skip_reason) BETWEEN 1 AND 128),
    created_at     TEXT NOT NULL
                   CHECK (length(created_at) BETWEEN 20 AND 27),
    completed_at   TEXT CHECK (completed_at IS NULL
                               OR length(completed_at) BETWEEN 20 AND 27),
    UNIQUE (namespace, stage_id),
    UNIQUE (namespace, turn_id, kind),
    FOREIGN KEY (namespace, turn_id)
        REFERENCES collab_turns (namespace, turn_id)
);

-- --------------------------------------------------- stage -> Context
CREATE TABLE collab_stage_contexts (
    namespace   TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    stage_id    TEXT NOT NULL CHECK (length(stage_id) = 36),
    context_id  TEXT NOT NULL CHECK (length(context_id) = 36),
    ordinal     INTEGER NOT NULL CHECK (ordinal BETWEEN 1 AND 1000000),
    purpose     TEXT NOT NULL CHECK (purpose IN (
                    'content_initial', 'content_after_commentary',
                    'content_after_tool', 'content_repair', 'expression')),
    created_at  TEXT NOT NULL
                CHECK (length(created_at) BETWEEN 20 AND 27),
    PRIMARY KEY (namespace, stage_id, ordinal),
    UNIQUE (namespace, context_id),
    FOREIGN KEY (namespace, stage_id)
        REFERENCES collab_turn_stages (namespace, stage_id),
    FOREIGN KEY (context_id) REFERENCES context_snapshots (context_id)
);
CREATE INDEX collab_stage_contexts_context_idx
    ON collab_stage_contexts (namespace, context_id);

-- ------------------------------------------------------------- steps
CREATE TABLE collab_turn_steps (
    step_id                TEXT NOT NULL PRIMARY KEY
                           CHECK (length(step_id) = 36),
    namespace              TEXT NOT NULL
                           CHECK (length(namespace) BETWEEN 1 AND 128),
    turn_id                TEXT NOT NULL CHECK (length(turn_id) = 36),
    stage_id               TEXT NOT NULL CHECK (length(stage_id) = 36),
    step_no                INTEGER NOT NULL CHECK (step_no BETWEEN 1 AND 1000),
    tool_name              TEXT NOT NULL
                           CHECK (length(tool_name) BETWEEN 1 AND 128),
    arguments_payload_ref  TEXT CHECK (arguments_payload_ref IS NULL
                                       OR length(arguments_payload_ref)
                                          BETWEEN 1 AND 1024),
    arguments_digest       TEXT CHECK (arguments_digest IS NULL
                                       OR (length(arguments_digest) = 64
                                           AND arguments_digest NOT GLOB '*[^0-9a-f]*')),
    result_artifact_id     TEXT CHECK (result_artifact_id IS NULL
                                       OR length(result_artifact_id) = 36),
    result_artifact_version INTEGER CHECK (result_artifact_version IS NULL
                                           OR result_artifact_version >= 1),
    result_digest          TEXT CHECK (result_digest IS NULL
                                       OR (length(result_digest) = 71
                                           AND result_digest LIKE 'sha256:%')),
    operation_key          TEXT NOT NULL
                           CHECK (length(operation_key) BETWEEN 1 AND 256),
    status                 TEXT NOT NULL CHECK (status IN (
                               'planned', 'running', 'succeeded', 'failed',
                               'cancelled', 'outcome_unknown')),
    started_at             TEXT CHECK (started_at IS NULL
                                       OR length(started_at) BETWEEN 20 AND 27),
    finished_at            TEXT CHECK (finished_at IS NULL
                                       OR length(finished_at) BETWEEN 20 AND 27),
    error_label            TEXT CHECK (error_label IS NULL
                                       OR length(error_label) BETWEEN 1 AND 128),
    UNIQUE (namespace, turn_id, operation_key),
    UNIQUE (namespace, stage_id, step_no),
    CHECK ((result_artifact_id IS NULL) = (result_artifact_version IS NULL)),
    FOREIGN KEY (namespace, turn_id)
        REFERENCES collab_turns (namespace, turn_id),
    FOREIGN KEY (namespace, stage_id)
        REFERENCES collab_turn_stages (namespace, stage_id),
    FOREIGN KEY (arguments_payload_ref)
        REFERENCES private_payloads (payload_ref)
);
CREATE INDEX collab_turn_steps_result_idx
    ON collab_turn_steps (namespace, result_artifact_id,
                          result_artifact_version);

-- ---------------------------------------------------- public messages
CREATE TABLE collab_turn_messages (
    message_id       TEXT NOT NULL PRIMARY KEY
                     CHECK (length(message_id) = 36),
    namespace        TEXT NOT NULL
                     CHECK (length(namespace) BETWEEN 1 AND 128),
    turn_id          TEXT NOT NULL CHECK (length(turn_id) = 36),
    seq              INTEGER NOT NULL CHECK (seq BETWEEN 1 AND 1000000),
    kind             TEXT NOT NULL CHECK (kind IN (
                         'user', 'commentary', 'final', 'wait_question')),
    input_ref_json   TEXT CHECK (input_ref_json IS NULL
                                 OR length(input_ref_json) BETWEEN 2 AND 16384),
    artifact_id      TEXT CHECK (artifact_id IS NULL
                                 OR length(artifact_id) = 36),
    artifact_version INTEGER CHECK (artifact_version IS NULL
                                    OR artifact_version >= 1),
    attempt_ref_json TEXT CHECK (attempt_ref_json IS NULL
                                 OR length(attempt_ref_json) BETWEEN 2 AND 16384),
    status           TEXT NOT NULL CHECK (status IN ('published', 'suppressed')),
    published_at     TEXT CHECK (published_at IS NULL
                                 OR length(published_at) BETWEEN 20 AND 27),
    created_at       TEXT NOT NULL
                     CHECK (length(created_at) BETWEEN 20 AND 27),
    UNIQUE (namespace, turn_id, seq),
    CHECK ((kind = 'user') = (input_ref_json IS NOT NULL)),
    CHECK ((kind = 'user') = (artifact_id IS NULL)),
    CHECK ((artifact_id IS NULL) = (artifact_version IS NULL)),
    CHECK ((status = 'published') = (published_at IS NOT NULL)),
    FOREIGN KEY (namespace, turn_id)
        REFERENCES collab_turns (namespace, turn_id)
);
CREATE UNIQUE INDEX collab_turn_messages_one_final
    ON collab_turn_messages (namespace, turn_id)
    WHERE kind = 'final' AND status = 'published';
-- The projection guard in store/experience.py looks up by artifact; without
-- this index the two NOT EXISTS clauses would be a scan per artifact.
CREATE INDEX collab_turn_messages_artifact_idx
    ON collab_turn_messages (namespace, artifact_id, artifact_version);

-- ------------------------------------------------------------ events
CREATE TABLE collab_turn_events (
    namespace    TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    turn_id      TEXT NOT NULL CHECK (length(turn_id) = 36),
    seq          INTEGER NOT NULL CHECK (seq BETWEEN 1 AND 1000000),
    event_id     TEXT NOT NULL CHECK (length(event_id) = 36),
    kind         TEXT NOT NULL CHECK (length(kind) BETWEEN 1 AND 64),
    payload_json TEXT NOT NULL
                 CHECK (length(payload_json) BETWEEN 2 AND 16384),
    created_at   TEXT NOT NULL
                 CHECK (length(created_at) BETWEEN 20 AND 27),
    PRIMARY KEY (namespace, turn_id, seq),
    UNIQUE (namespace, event_id),
    FOREIGN KEY (namespace, turn_id)
        REFERENCES collab_turns (namespace, turn_id)
);

-- --------------------------------------------------- context history
-- A′: the exact historical messages a Context actually rendered, in order.
-- These rows are NOT material sources: they never enter context_sources or
-- collab_source_links and the reply artifact is not required to cite them.
-- They exist so (a) a deleted historical message makes the Context stale and
--       (b) deletion cleanup can find and clear the rendered copy.
CREATE TABLE collab_context_history (
    namespace    TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    context_id   TEXT NOT NULL CHECK (length(context_id) = 36),
    ordinal      INTEGER NOT NULL CHECK (ordinal BETWEEN 1 AND 1000000),
    ref_kind     TEXT NOT NULL
                 CHECK (ref_kind IN ('collab_input', 'collab_artifact')),
    ref_id       TEXT NOT NULL CHECK (length(ref_id) = 36),
    ref_version  INTEGER NOT NULL CHECK (ref_version >= 1),
    created_at   TEXT NOT NULL
                 CHECK (length(created_at) BETWEEN 20 AND 27),
    PRIMARY KEY (namespace, context_id, ordinal),
    UNIQUE (namespace, context_id, ref_kind, ref_id, ref_version),
    FOREIGN KEY (context_id) REFERENCES context_snapshots (context_id)
);
-- Reverse lookup for the deletion cascade: which Contexts rendered this
-- exact historical ref.
CREATE INDEX collab_context_history_ref_idx
    ON collab_context_history (namespace, ref_kind, ref_id, ref_version);

-- Append-only evidence tables (R5).  The public message table is deliberately
-- NOT frozen: its status may move published -> suppressed.  Turns/stages/
-- conversations keep their state transitions.
CREATE TRIGGER collab_turn_events_no_update
BEFORE UPDATE ON collab_turn_events
BEGIN
    SELECT RAISE(ABORT, 'collab_turn_events rows are append-only evidence');
END;

CREATE TRIGGER collab_turn_events_no_delete
BEFORE DELETE ON collab_turn_events
BEGIN
    SELECT RAISE(ABORT, 'collab_turn_events rows are append-only evidence');
END;

CREATE TRIGGER collab_context_history_no_update
BEFORE UPDATE ON collab_context_history
BEGIN
    SELECT RAISE(ABORT, 'collab_context_history rows are append-only evidence');
END;

CREATE TRIGGER collab_context_history_no_delete
BEFORE DELETE ON collab_context_history
BEGIN
    SELECT RAISE(ABORT, 'collab_context_history rows are append-only evidence');
END;

-- --------------------------------------- existing table: one nullable column
ALTER TABLE context_snapshots
    ADD COLUMN renderer_version TEXT
        CHECK (renderer_version IS NULL
               OR length(renderer_version) BETWEEN 1 AND 64);

-- ------------------------------------------------ v5 §5.3.8 item 9 (index)
CREATE INDEX memory_delivery_ref_idx
    ON memory_delivery (namespace, ref_type, ref_id, ref_version);
