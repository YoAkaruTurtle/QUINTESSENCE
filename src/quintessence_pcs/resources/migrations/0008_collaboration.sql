-- C05b: collaboration storage and source barriers.
--
-- This migration is additive except for one lossless rebuild: 0003's
-- ``deletion_targets`` primary key could only name a whole record/event, while
-- a collaboration input/artifact is deleted at one exact version.  Every old
-- row is copied with ``target_version = 0`` and the table is recreated with
-- ``(namespace, target_kind, target_id, target_version)``.  Record/event
-- targets always keep version 0 (whole-object deletion); collaboration targets
-- match the exact version.
--
-- No business table stores body text: input/artifact bodies and candidate
-- proposals live in ``private_payloads`` through opaque payload refs.  Rows
-- that are evidence are immutable; candidates alone may move from pending to
-- one terminal decision, and grants alone may be revoked.

-- ------------------------------------------------ context_snapshots.kind
ALTER TABLE context_snapshots
    ADD COLUMN context_kind TEXT NOT NULL DEFAULT 'review'
        CHECK (context_kind IN ('review', 'consult'));

-- A consult Context is defined by having no review trigger; a complete review
-- Context (run_id set by insert_context) must have one.  006-era bare watermark
-- rows from capture_snapshot have run_id NULL and are exempt from the review
-- rule, so the trigger never fires for them.
CREATE TRIGGER context_snapshots_kind_insert
BEFORE INSERT ON context_snapshots
BEGIN
    SELECT RAISE(ABORT, 'consult context cannot carry a review trigger')
    WHERE NEW.context_kind = 'consult' AND NEW.trigger_ref_json IS NOT NULL;
    SELECT RAISE(ABORT, 'a complete review context requires a trigger')
    WHERE NEW.context_kind = 'review'
      AND NEW.run_id IS NOT NULL
      AND NEW.trigger_ref_json IS NULL;
END;

CREATE TRIGGER context_snapshots_kind_update
BEFORE UPDATE ON context_snapshots
BEGIN
    SELECT RAISE(ABORT, 'consult context cannot carry a review trigger')
    WHERE NEW.context_kind = 'consult' AND NEW.trigger_ref_json IS NOT NULL;
    SELECT RAISE(ABORT, 'a complete review context requires a trigger')
    WHERE NEW.context_kind = 'review'
      AND NEW.run_id IS NOT NULL
      AND NEW.trigger_ref_json IS NULL;
END;

-- ------------------------------------------------ deletion_targets rebuild
CREATE TABLE deletion_targets_new (
    target_kind       TEXT NOT NULL
                      CHECK (target_kind IN (
                          'record', 'event', 'collab_input', 'collab_artifact')),
    namespace         TEXT NOT NULL
                      CHECK (length(namespace) BETWEEN 1 AND 128),
    target_id         TEXT NOT NULL CHECK (length(target_id) = 36),
    target_version    INTEGER NOT NULL DEFAULT 0
                      CHECK (target_version >= 0),
    status            TEXT NOT NULL
                      CHECK (status IN ('pending_cleanup', 'cleaned')),
    authorization_ref TEXT NOT NULL
                      CHECK (length(authorization_ref) BETWEEN 1 AND 512),
    created_at        TEXT NOT NULL
                      CHECK (length(created_at) BETWEEN 20 AND 27),
    updated_at        TEXT NOT NULL
                      CHECK (length(updated_at) BETWEEN 20 AND 27),
    last_error        TEXT
                      CHECK (last_error IS NULL OR length(last_error) BETWEEN 1 AND 1024),
    PRIMARY KEY (namespace, target_kind, target_id, target_version)
);

INSERT INTO deletion_targets_new (
    target_kind, namespace, target_id, target_version, status,
    authorization_ref, created_at, updated_at, last_error
)
SELECT
    target_kind, namespace, target_id, 0, status,
    authorization_ref, created_at, updated_at, last_error
FROM deletion_targets;

DROP TABLE deletion_targets;

ALTER TABLE deletion_targets_new RENAME TO deletion_targets;

CREATE INDEX deletion_targets_pending_idx
    ON deletion_targets (status, namespace, created_at);

CREATE INDEX deletion_targets_target_idx
    ON deletion_targets (namespace, target_kind, target_id);

-- ---------------------------------------------------- collab_interactions
CREATE TABLE collab_interactions (
    interaction_id      TEXT NOT NULL PRIMARY KEY
                        CHECK (length(interaction_id) = 36),
    namespace           TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    schema_version      TEXT NOT NULL CHECK (length(schema_version) BETWEEN 1 AND 128),
    mode                TEXT NOT NULL CHECK (mode IN ('consult', 'review')),
    activation_kind     TEXT NOT NULL
                        CHECK (activation_kind IN ('user_input', 'manual_review')),
    host_instance       TEXT NOT NULL CHECK (length(host_instance) BETWEEN 1 AND 256),
    trigger_ref_json    TEXT
                        CHECK (trigger_ref_json IS NULL
                               OR length(trigger_ref_json) BETWEEN 2 AND 16384),
    work_id             TEXT CHECK (work_id IS NULL OR length(work_id) = 36),
    work_generation     INTEGER CHECK (work_generation IS NULL OR work_generation >= 0),
    context_id          TEXT CHECK (context_id IS NULL OR length(context_id) = 36),
    result              TEXT CHECK (result IS NULL OR result IN (
                            'answered', 'waiting', 'candidate_pending', 'committed',
                            'rejected', 'stale', 'denied', 'failed', 'incomplete')),
    receipt_request_id  TEXT
                        CHECK (receipt_request_id IS NULL
                               OR length(receipt_request_id) = 36),
    access_ref          TEXT NOT NULL CHECK (length(access_ref) BETWEEN 1 AND 512),
    policy_ref          TEXT NOT NULL CHECK (length(policy_ref) BETWEEN 1 AND 512),
    created_at          TEXT NOT NULL CHECK (length(created_at) BETWEEN 20 AND 27),
    completed_at        TEXT CHECK (completed_at IS NULL
                                    OR length(completed_at) BETWEEN 20 AND 27),
    UNIQUE (namespace, interaction_id),
    -- review is pinned to a trigger and to a work item; consult has neither.
    CHECK ((mode = 'review') = (trigger_ref_json IS NOT NULL)),
    CHECK ((mode = 'consult') = (work_id IS NULL))
);

CREATE INDEX collab_interactions_namespace_idx
    ON collab_interactions (namespace, created_at);

CREATE INDEX collab_interactions_context_idx
    ON collab_interactions (context_id);

CREATE TRIGGER collab_interactions_no_delete
BEFORE DELETE ON collab_interactions
BEGIN
    SELECT RAISE(ABORT, 'collab_interactions rows are audit evidence');
END;

-- Only the outcome envelope may change after creation: result, completion
-- time, the Context it was bound to, and the receipt it produced.  Mode,
-- trigger, work binding and access policy are immutable.
CREATE TRIGGER collab_interactions_update_guard
BEFORE UPDATE ON collab_interactions
BEGIN
    SELECT RAISE(ABORT, 'collab interaction binding columns are immutable')
    WHERE NEW.interaction_id IS NOT OLD.interaction_id
       OR NEW.namespace IS NOT OLD.namespace
       OR NEW.schema_version IS NOT OLD.schema_version
       OR NEW.mode IS NOT OLD.mode
       OR NEW.activation_kind IS NOT OLD.activation_kind
       OR NEW.host_instance IS NOT OLD.host_instance
       OR NEW.trigger_ref_json IS NOT OLD.trigger_ref_json
       OR NEW.work_id IS NOT OLD.work_id
       OR NEW.work_generation IS NOT OLD.work_generation
       OR NEW.access_ref IS NOT OLD.access_ref
       OR NEW.policy_ref IS NOT OLD.policy_ref
       OR NEW.created_at IS NOT OLD.created_at;
END;

-- ----------------------------------------------------------- collab_inputs
CREATE TABLE collab_inputs (
    namespace        TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    input_id         TEXT NOT NULL CHECK (length(input_id) = 36),
    content_version  INTEGER NOT NULL CHECK (content_version >= 1),
    schema_version   TEXT NOT NULL CHECK (length(schema_version) BETWEEN 1 AND 128),
    origin_kind      TEXT NOT NULL
                     CHECK (origin_kind IN ('user_statement', 'host_supplied')),
    body_payload_ref TEXT NOT NULL
                     CHECK (length(body_payload_ref) BETWEEN 1 AND 1024),
    content_digest   TEXT NOT NULL
                     CHECK (length(content_digest) = 71
                            AND content_digest LIKE 'sha256:%'),
    privacy_class    TEXT NOT NULL
                     CHECK (privacy_class IN ('private', 'restricted', 'public-example')),
    retention_policy TEXT NOT NULL
                     CHECK (retention_policy IN ('session', 'standard', 'durable')),
    observed_at      TEXT CHECK (observed_at IS NULL
                                 OR length(observed_at) BETWEEN 20 AND 27),
    recorded_at      TEXT NOT NULL CHECK (length(recorded_at) BETWEEN 20 AND 27),
    supersedes_json  TEXT CHECK (supersedes_json IS NULL
                                 OR length(supersedes_json) BETWEEN 2 AND 4096),
    PRIMARY KEY (namespace, input_id, content_version),
    FOREIGN KEY (body_payload_ref) REFERENCES private_payloads (payload_ref)
);

CREATE INDEX collab_inputs_namespace_idx
    ON collab_inputs (namespace, recorded_at);

CREATE TRIGGER collab_inputs_no_update
BEFORE UPDATE ON collab_inputs
BEGIN
    SELECT RAISE(ABORT, 'collab_inputs rows are immutable');
END;

CREATE TRIGGER collab_inputs_no_delete
BEFORE DELETE ON collab_inputs
BEGIN
    SELECT RAISE(ABORT, 'collab_inputs rows are immutable');
END;

-- -------------------------------------------------------- collab_artifacts
-- A candidate is the same immutable artifact row plus a typed decision state.
-- The proposal itself is a payload, never a TEXT column here.
CREATE TABLE collab_artifacts (
    namespace                 TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    artifact_id               TEXT NOT NULL CHECK (length(artifact_id) = 36),
    artifact_version          INTEGER NOT NULL CHECK (artifact_version >= 1),
    schema_version            TEXT NOT NULL CHECK (length(schema_version) BETWEEN 1 AND 128),
    kind                      TEXT NOT NULL CHECK (kind IN ('reply', 'query_result', 'candidate')),
    interaction_id            TEXT NOT NULL CHECK (length(interaction_id) = 36),
    context_id                TEXT NOT NULL CHECK (length(context_id) = 36),
    attempt_ref_json          TEXT CHECK (attempt_ref_json IS NULL
                                          OR length(attempt_ref_json) BETWEEN 2 AND 16384),
    origin_kind               TEXT NOT NULL
                              CHECK (origin_kind IN ('model_output', 'deterministic_query')),
    body_payload_ref          TEXT NOT NULL
                              CHECK (length(body_payload_ref) BETWEEN 1 AND 1024),
    content_digest            TEXT NOT NULL
                              CHECK (length(content_digest) = 71
                                     AND content_digest LIKE 'sha256:%'),
    retention_policy          TEXT NOT NULL
                              CHECK (retention_policy IN ('session', 'standard', 'durable')),
    created_at                TEXT NOT NULL CHECK (length(created_at) BETWEEN 20 AND 27),
    proposal_payload_ref      TEXT CHECK (proposal_payload_ref IS NULL
                                          OR length(proposal_payload_ref) BETWEEN 1 AND 1024),
    proposal_digest           TEXT CHECK (proposal_digest IS NULL
                                          OR (length(proposal_digest) = 71
                                              AND proposal_digest LIKE 'sha256:%')),
    proposal_version          TEXT CHECK (proposal_version IS NULL
                                          OR length(proposal_version) BETWEEN 1 AND 64),
    work_id                   TEXT CHECK (work_id IS NULL OR length(work_id) = 36),
    work_generation           INTEGER CHECK (work_generation IS NULL
                                             OR work_generation >= 0),
    request_id                TEXT CHECK (request_id IS NULL OR length(request_id) = 36),
    command_digest            TEXT CHECK (command_digest IS NULL
                                          OR (length(command_digest) = 64
                                              AND command_digest NOT GLOB '*[^0-9a-f]*')),
    structural_validation     TEXT CHECK (structural_validation IS NULL
                                          OR structural_validation = 'passed'),
    rerun_authorization_ref   TEXT CHECK (rerun_authorization_ref IS NULL
                                          OR length(rerun_authorization_ref) BETWEEN 1 AND 256),
    commit_authorization_ref  TEXT CHECK (commit_authorization_ref IS NULL
                                          OR length(commit_authorization_ref) BETWEEN 1 AND 256),
    status                    TEXT CHECK (status IS NULL OR status IN (
                                  'pending', 'committed', 'rejected', 'stale', 'failed')),
    decided_at                TEXT CHECK (decided_at IS NULL
                                          OR length(decided_at) BETWEEN 20 AND 27),
    decision_ref              TEXT CHECK (decision_ref IS NULL
                                          OR length(decision_ref) BETWEEN 1 AND 256),
    PRIMARY KEY (namespace, artifact_id, artifact_version),
    FOREIGN KEY (body_payload_ref) REFERENCES private_payloads (payload_ref),
    FOREIGN KEY (proposal_payload_ref) REFERENCES private_payloads (payload_ref),
    -- Candidate columns are present exactly for kind='candidate'.  The four
    -- decision/authorization columns (rerun, commit, decided_at, decision_ref)
    -- stay nullable even on a candidate.
    CHECK (
        (kind = 'candidate')
        = (
            proposal_payload_ref IS NOT NULL
            AND proposal_digest IS NOT NULL
            AND proposal_version IS NOT NULL
            AND work_id IS NOT NULL
            AND work_generation IS NOT NULL
            AND request_id IS NOT NULL
            AND command_digest IS NOT NULL
            AND structural_validation IS NOT NULL
            AND status IS NOT NULL
        )
    )
);

CREATE INDEX collab_artifacts_interaction_idx
    ON collab_artifacts (namespace, interaction_id);

CREATE INDEX collab_artifacts_work_idx
    ON collab_artifacts (namespace, work_id, work_generation);

CREATE INDEX collab_artifacts_status_idx
    ON collab_artifacts (namespace, kind, status);

CREATE TRIGGER collab_artifacts_no_delete
BEFORE DELETE ON collab_artifacts
BEGIN
    SELECT RAISE(ABORT, 'collab_artifacts rows are audit evidence');
END;

-- A candidate may be decided exactly once, and only from pending to one
-- terminal state.  Body, proposal, identities and the generation are frozen.
CREATE TRIGGER collab_artifacts_update_guard
BEFORE UPDATE ON collab_artifacts
BEGIN
    SELECT RAISE(ABORT, 'collab artifact content and binding columns are immutable')
    WHERE NEW.namespace IS NOT OLD.namespace
       OR NEW.artifact_id IS NOT OLD.artifact_id
       OR NEW.artifact_version IS NOT OLD.artifact_version
       OR NEW.schema_version IS NOT OLD.schema_version
       OR NEW.kind IS NOT OLD.kind
       OR NEW.interaction_id IS NOT OLD.interaction_id
       OR NEW.context_id IS NOT OLD.context_id
       OR NEW.attempt_ref_json IS NOT OLD.attempt_ref_json
       OR NEW.origin_kind IS NOT OLD.origin_kind
       OR NEW.body_payload_ref IS NOT OLD.body_payload_ref
       OR NEW.content_digest IS NOT OLD.content_digest
       OR NEW.retention_policy IS NOT OLD.retention_policy
       OR NEW.created_at IS NOT OLD.created_at
       OR NEW.proposal_payload_ref IS NOT OLD.proposal_payload_ref
       OR NEW.proposal_digest IS NOT OLD.proposal_digest
       OR NEW.proposal_version IS NOT OLD.proposal_version
       OR NEW.work_id IS NOT OLD.work_id
       OR NEW.work_generation IS NOT OLD.work_generation
       OR NEW.request_id IS NOT OLD.request_id
       OR NEW.command_digest IS NOT OLD.command_digest
       OR NEW.structural_validation IS NOT OLD.structural_validation
       OR NEW.rerun_authorization_ref IS NOT OLD.rerun_authorization_ref;
    SELECT RAISE(ABORT, 'only a pending candidate may be decided once')
    WHERE OLD.status IS NOT 'pending'
       OR NEW.status IS NULL
       OR NEW.status NOT IN ('committed', 'rejected', 'stale', 'failed');
END;

-- ----------------------------------------------------- collab_work_control
CREATE TABLE collab_work_control (
    namespace                TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    work_id                  TEXT NOT NULL CHECK (length(work_id) = 36),
    schema_version           TEXT NOT NULL CHECK (length(schema_version) BETWEEN 1 AND 128),
    generation               INTEGER NOT NULL CHECK (generation >= 0),
    blocked_after_rejection  INTEGER NOT NULL DEFAULT 0
                             CHECK (blocked_after_rejection IN (0, 1)),
    control_version          INTEGER NOT NULL CHECK (control_version >= 1),
    rerun_authorization_ref  TEXT CHECK (rerun_authorization_ref IS NULL
                                         OR length(rerun_authorization_ref) BETWEEN 1 AND 256),
    block_reason             TEXT CHECK (block_reason IS NULL OR block_reason = 'rejected'),
    updated_at               TEXT NOT NULL CHECK (length(updated_at) BETWEEN 20 AND 27),
    last_candidate_ref       TEXT CHECK (last_candidate_ref IS NULL
                                         OR length(last_candidate_ref) = 36),
    PRIMARY KEY (namespace, work_id)
);

CREATE TRIGGER collab_work_control_no_delete
BEFORE DELETE ON collab_work_control
BEGIN
    SELECT RAISE(ABORT, 'collab_work_control rows are audit evidence');
END;

-- ------------------------------------------------------ collab_source_links
CREATE TABLE collab_source_links (
    namespace       TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    owner_kind      TEXT NOT NULL CHECK (owner_kind IN ('context', 'artifact')),
    owner_id        TEXT NOT NULL CHECK (length(owner_id) = 36),
    owner_version   INTEGER NOT NULL CHECK (owner_version >= 0),
    source_kind     TEXT NOT NULL
                    CHECK (source_kind IN ('event', 'record', 'collab_input', 'collab_artifact')),
    source_id       TEXT NOT NULL CHECK (length(source_id) = 36),
    source_version  INTEGER NOT NULL CHECK (source_version >= 0),
    created_at      TEXT NOT NULL CHECK (length(created_at) BETWEEN 20 AND 27),
    PRIMARY KEY (namespace, owner_kind, owner_id, owner_version,
                 source_kind, source_id, source_version),
    -- A Context is one whole owner; an artifact owner names its exact version.
    CHECK (
        (owner_kind = 'context' AND owner_version = 0)
        OR (owner_kind = 'artifact' AND owner_version >= 1)
    ),
    -- Events have no version.  Records and collaboration rows are versioned.
    CHECK (
        (source_kind = 'event' AND source_version = 0)
        OR (source_kind IN ('record', 'collab_input', 'collab_artifact')
            AND source_version >= 1)
    )
);

-- Reverse lookup: which owners depend on this exact source (cleanup and
-- barrier).  For record/event the version is ignored by the query, matching
-- the whole-object deletion semantics; for collaboration rows it is exact.
CREATE INDEX collab_source_links_source_idx
    ON collab_source_links (namespace, source_kind, source_id, source_version);

CREATE INDEX collab_source_links_owner_idx
    ON collab_source_links (namespace, owner_kind, owner_id, owner_version);

CREATE TRIGGER collab_source_links_no_update
BEFORE UPDATE ON collab_source_links
BEGIN
    SELECT RAISE(ABORT, 'collab_source_links rows are immutable');
END;

CREATE TRIGGER collab_source_links_no_delete
BEFORE DELETE ON collab_source_links
BEGIN
    SELECT RAISE(ABORT, 'collab_source_links rows are immutable');
END;

-- ------------------------------------------------------------ collab_grants
-- Five mutually exclusive branches: access, egress, commit(policy),
-- commit(work_generation), rerun.  Only revoked_at may change.
CREATE TABLE collab_grants (
    grant_id                 TEXT NOT NULL PRIMARY KEY
                             CHECK (length(grant_id) = 36),
    namespace                TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    schema_version           TEXT NOT NULL CHECK (length(schema_version) BETWEEN 1 AND 128),
    kind                     TEXT NOT NULL
                             CHECK (kind IN ('access', 'egress', 'commit', 'rerun')),
    grant_version            INTEGER NOT NULL CHECK (grant_version >= 1),
    subject                  TEXT NOT NULL CHECK (length(subject) BETWEEN 1 AND 256),
    authorization_ref        TEXT NOT NULL
                             CHECK (length(authorization_ref) BETWEEN 1 AND 512),
    issued_at                TEXT NOT NULL CHECK (length(issued_at) BETWEEN 20 AND 27),
    expires_at               TEXT CHECK (expires_at IS NULL
                                         OR length(expires_at) BETWEEN 20 AND 27),
    revoked_at               TEXT CHECK (revoked_at IS NULL
                                         OR length(revoked_at) BETWEEN 20 AND 27),
    scope                    TEXT CHECK (scope IS NULL
                                         OR scope IN ('policy', 'work_generation')),
    purposes_json            TEXT CHECK (purposes_json IS NULL
                                         OR length(purposes_json) BETWEEN 2 AND 4096),
    data_classes_json        TEXT CHECK (data_classes_json IS NULL
                                         OR length(data_classes_json) BETWEEN 2 AND 256),
    provider                 TEXT CHECK (provider IS NULL
                                         OR length(provider) BETWEEN 1 AND 128),
    endpoint                 TEXT CHECK (endpoint IS NULL
                                         OR length(endpoint) BETWEEN 1 AND 512),
    model                    TEXT CHECK (model IS NULL
                                         OR length(model) BETWEEN 1 AND 256),
    manifest_hash            TEXT CHECK (manifest_hash IS NULL
                                         OR (length(manifest_hash) = 71
                                             AND manifest_hash LIKE 'sha256:%')),
    privacy_classes_json     TEXT CHECK (privacy_classes_json IS NULL
                                         OR length(privacy_classes_json) BETWEEN 2 AND 256),
    model_authorization_ref  TEXT CHECK (model_authorization_ref IS NULL
                                         OR length(model_authorization_ref) = 36),
    operations_json          TEXT CHECK (operations_json IS NULL
                                         OR length(operations_json) BETWEEN 2 AND 16384),
    trigger_kinds_json       TEXT CHECK (trigger_kinds_json IS NULL
                                         OR length(trigger_kinds_json) BETWEEN 2 AND 8192),
    work_id                  TEXT CHECK (work_id IS NULL OR length(work_id) = 36),
    generation               INTEGER CHECK (generation IS NULL OR generation >= 0),
    CHECK (
        (kind = 'access'
            AND purposes_json IS NOT NULL
            AND data_classes_json IS NOT NULL
            AND scope IS NULL AND provider IS NULL AND endpoint IS NULL
            AND model IS NULL AND manifest_hash IS NULL
            AND privacy_classes_json IS NULL AND model_authorization_ref IS NULL
            AND operations_json IS NULL AND trigger_kinds_json IS NULL
            AND work_id IS NULL AND generation IS NULL)
        OR
        (kind = 'egress'
            AND provider IS NOT NULL AND endpoint IS NOT NULL AND model IS NOT NULL
            AND manifest_hash IS NOT NULL AND privacy_classes_json IS NOT NULL
            AND purposes_json IS NOT NULL AND model_authorization_ref IS NOT NULL
            AND data_classes_json IS NULL AND scope IS NULL
            AND operations_json IS NULL AND trigger_kinds_json IS NULL
            AND work_id IS NULL AND generation IS NULL)
        OR
        (kind = 'commit' AND scope = 'policy'
            AND operations_json IS NOT NULL AND trigger_kinds_json IS NOT NULL
            AND purposes_json IS NULL AND data_classes_json IS NULL
            AND provider IS NULL AND endpoint IS NULL AND model IS NULL
            AND manifest_hash IS NULL AND privacy_classes_json IS NULL
            AND model_authorization_ref IS NULL
            AND work_id IS NULL AND generation IS NULL)
        OR
        (kind = 'commit' AND scope = 'work_generation'
            AND work_id IS NOT NULL AND generation IS NOT NULL
            AND purposes_json IS NULL AND data_classes_json IS NULL
            AND provider IS NULL AND endpoint IS NULL AND model IS NULL
            AND manifest_hash IS NULL AND privacy_classes_json IS NULL
            AND model_authorization_ref IS NULL
            AND operations_json IS NULL AND trigger_kinds_json IS NULL)
        OR
        (kind = 'rerun'
            AND work_id IS NOT NULL AND generation IS NOT NULL
            AND scope IS NULL AND purposes_json IS NULL
            AND data_classes_json IS NULL AND provider IS NULL
            AND endpoint IS NULL AND model IS NULL AND manifest_hash IS NULL
            AND privacy_classes_json IS NULL AND model_authorization_ref IS NULL
            AND operations_json IS NULL AND trigger_kinds_json IS NULL)
    )
);

CREATE INDEX collab_grants_scope_idx
    ON collab_grants (namespace, kind, scope);

CREATE TRIGGER collab_grants_no_delete
BEFORE DELETE ON collab_grants
BEGIN
    SELECT RAISE(ABORT, 'collab_grants rows are audit evidence');
END;

CREATE TRIGGER collab_grants_revoke_only
BEFORE UPDATE ON collab_grants
BEGIN
    SELECT RAISE(ABORT, 'only revoked_at may change on a grant')
    WHERE NEW.grant_id IS NOT OLD.grant_id
       OR NEW.namespace IS NOT OLD.namespace
       OR NEW.schema_version IS NOT OLD.schema_version
       OR NEW.kind IS NOT OLD.kind
       OR NEW.grant_version IS NOT OLD.grant_version
       OR NEW.subject IS NOT OLD.subject
       OR NEW.authorization_ref IS NOT OLD.authorization_ref
       OR NEW.issued_at IS NOT OLD.issued_at
       OR NEW.expires_at IS NOT OLD.expires_at
       OR NEW.scope IS NOT OLD.scope
       OR NEW.purposes_json IS NOT OLD.purposes_json
       OR NEW.data_classes_json IS NOT OLD.data_classes_json
       OR NEW.provider IS NOT OLD.provider
       OR NEW.endpoint IS NOT OLD.endpoint
       OR NEW.model IS NOT OLD.model
       OR NEW.manifest_hash IS NOT OLD.manifest_hash
       OR NEW.privacy_classes_json IS NOT OLD.privacy_classes_json
       OR NEW.model_authorization_ref IS NOT OLD.model_authorization_ref
       OR NEW.operations_json IS NOT OLD.operations_json
       OR NEW.trigger_kinds_json IS NOT OLD.trigger_kinds_json
       OR NEW.work_id IS NOT OLD.work_id
       OR NEW.generation IS NOT OLD.generation;
END;

-- ------------------------------------------------- collab_interaction_inputs
-- The exact inputs (and versions) an interaction was opened with, recoverable
-- before any Context is assembled.  Position preserves the caller's order.
CREATE TABLE collab_interaction_inputs (
    namespace        TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    interaction_id   TEXT NOT NULL CHECK (length(interaction_id) = 36),
    input_id         TEXT NOT NULL CHECK (length(input_id) = 36),
    content_version  INTEGER NOT NULL CHECK (content_version >= 1),
    position         INTEGER NOT NULL CHECK (position >= 0),
    PRIMARY KEY (namespace, interaction_id, input_id, content_version),
    UNIQUE (namespace, interaction_id, position),
    FOREIGN KEY (namespace, interaction_id)
        REFERENCES collab_interactions (namespace, interaction_id),
    FOREIGN KEY (namespace, input_id, content_version)
        REFERENCES collab_inputs (namespace, input_id, content_version)
);

CREATE TRIGGER collab_interaction_inputs_no_update
BEFORE UPDATE ON collab_interaction_inputs
BEGIN
    SELECT RAISE(ABORT, 'collab_interaction_inputs rows are immutable');
END;

CREATE TRIGGER collab_interaction_inputs_no_delete
BEFORE DELETE ON collab_interaction_inputs
BEGIN
    SELECT RAISE(ABORT, 'collab_interaction_inputs rows are immutable');
END;

-- ------------------------------------------------------------ reverse join
-- Fast lookup of every Context that used one collaboration source; used by
-- the delivery barrier and by attempt-payload cleanup.
CREATE INDEX collab_source_links_context_source_idx
    ON collab_source_links (namespace, source_kind, source_id, source_version, owner_id)
    WHERE owner_kind = 'context';
