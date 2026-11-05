-- J06 S7a: Work-less cognition candidates and the BLOCKED impact level.
--
-- Two frozen tables are rebuilt in place.  SQLite cannot alter a CHECK
-- constraint, so each table is copied into a shadow table, the original is
-- dropped, and the shadow is renamed.  No other table references either one
-- (verified: no FOREIGN KEY ... REFERENCES collab_artifacts in any migration),
-- so the drop cannot orphan a child row.  Foreign keys stay ON for the whole
-- migration: every copied row keeps a live private_payloads parent.

-- ---------------------------------------------------------------- artifacts
--
-- The candidate invariant keeps all eight content columns mandatory but no
-- longer demands a work binding: ``work_id``/``work_generation`` are either
-- both present (the C07 review path) or both absent (the J06 cognition-change
-- path).  A half-present pair stays illegal.

DROP TRIGGER collab_artifacts_no_delete;

DROP TRIGGER collab_artifacts_update_guard;

CREATE TABLE collab_artifacts_new (
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
    -- The eight candidate columns are all-or-nothing, as before; only the work
    -- binding becomes optional, and it must be present as a pair.
    CHECK (
        (kind = 'candidate')
        = (
            proposal_payload_ref IS NOT NULL
            AND proposal_digest IS NOT NULL
            AND proposal_version IS NOT NULL
            AND request_id IS NOT NULL
            AND command_digest IS NOT NULL
            AND structural_validation IS NOT NULL
            AND status IS NOT NULL
        )
    ),
    CHECK ((work_id IS NULL) = (work_generation IS NULL))
);

INSERT INTO collab_artifacts_new (
    namespace, artifact_id, artifact_version, schema_version, kind,
    interaction_id, context_id, attempt_ref_json, origin_kind,
    body_payload_ref, content_digest, retention_policy, created_at,
    proposal_payload_ref, proposal_digest, proposal_version,
    work_id, work_generation, request_id, command_digest,
    structural_validation, rerun_authorization_ref, commit_authorization_ref,
    status, decided_at, decision_ref
)
SELECT
    namespace, artifact_id, artifact_version, schema_version, kind,
    interaction_id, context_id, attempt_ref_json, origin_kind,
    body_payload_ref, content_digest, retention_policy, created_at,
    proposal_payload_ref, proposal_digest, proposal_version,
    work_id, work_generation, request_id, command_digest,
    structural_validation, rerun_authorization_ref, commit_authorization_ref,
    status, decided_at, decision_ref
FROM collab_artifacts;

DROP TABLE collab_artifacts;

ALTER TABLE collab_artifacts_new RENAME TO collab_artifacts;

CREATE INDEX collab_artifacts_interaction_idx
    ON collab_artifacts (namespace, interaction_id);

CREATE INDEX collab_artifacts_work_idx
    ON collab_artifacts (namespace, work_id, work_generation);

CREATE INDEX collab_artifacts_status_idx
    ON collab_artifacts (namespace, kind, status);

-- 0010's two partial unique indexes live on this table; dropping the table
-- drops them, so they are recreated here with their original definitions.
CREATE UNIQUE INDEX collab_artifacts_request_key_idx
    ON collab_artifacts (namespace, request_id)
    WHERE kind = 'candidate' AND request_id IS NOT NULL;

CREATE UNIQUE INDEX collab_candidates_single_version_idx
    ON collab_artifacts (namespace, artifact_id)
    WHERE kind = 'candidate';

CREATE TRIGGER collab_artifacts_no_delete
BEFORE DELETE ON collab_artifacts
BEGIN
    SELECT RAISE(ABORT, 'collab_artifacts rows are audit evidence');
END;

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

-- ---------------------------------------------------------------- decisions
--
-- ``impact`` gains the fourth level.  BLOCKED is not "very high risk": it is
-- the level no authority may commit, so the column has to be able to say so.

DROP TRIGGER cognition_review_decisions_no_update;

DROP TRIGGER cognition_review_decisions_no_delete;

CREATE TABLE cognition_review_decisions_new (
    namespace              TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    decision_id            TEXT NOT NULL CHECK (length(decision_id) = 36),
    decision_seq           INTEGER NOT NULL CHECK (decision_seq >= 1),
    candidate_id           TEXT NOT NULL CHECK (length(candidate_id) = 36),
    artifact_version       INTEGER NOT NULL CHECK (artifact_version >= 1),
    interaction_id         TEXT CHECK (interaction_id IS NULL OR length(interaction_id) = 36),
    decision               TEXT NOT NULL CHECK (decision IN (
                               'awaiting_human', 'deferred', 'escalated',
                               'not_authorized', 'rejected', 'committed',
                               'conflict', 'approval_expired', 'approval_revoked')),
    reason_code            TEXT NOT NULL CHECK (length(reason_code) BETWEEN 1 AND 64),
    impact                 TEXT NOT NULL CHECK (impact IN ('E0', 'E1', 'E2', 'BLOCKED')),
    derived_status         TEXT NOT NULL CHECK (derived_status IN (
                               'proposed', 'supported', 'contested',
                               'contradicted', 'stale', 'none')),
    support_basis          TEXT NOT NULL CHECK (length(support_basis) BETWEEN 1 AND 64),
    requires_human         INTEGER NOT NULL CHECK (requires_human IN (0, 1)),
    evidence_linkage       TEXT NOT NULL CHECK (evidence_linkage IN (
                               'verified', 'unverified', 'not_applicable')),
    policy_id              TEXT NOT NULL CHECK (length(policy_id) BETWEEN 1 AND 128),
    policy_version         TEXT NOT NULL CHECK (length(policy_version) BETWEEN 1 AND 64),
    impact_policy_id       TEXT NOT NULL CHECK (length(impact_policy_id) BETWEEN 1 AND 128),
    impact_policy_version  TEXT NOT NULL CHECK (length(impact_policy_version) BETWEEN 1 AND 64),
    authority_kind         TEXT NOT NULL CHECK (authority_kind IN (
                               'manual_approval', 'auto_authorization',
                               'user_directive', 'none')),
    authorization_id       TEXT CHECK (authorization_id IS NULL OR length(authorization_id) = 36),
    directive_digest       TEXT CHECK (directive_digest IS NULL
                                       OR (length(directive_digest) = 71
                                           AND directive_digest LIKE 'sha256:%')),
    approval_id            TEXT CHECK (approval_id IS NULL OR length(approval_id) = 36),
    change_digest          TEXT NOT NULL CHECK (length(change_digest) = 71
                                                AND change_digest LIKE 'sha256:%'),
    change_fingerprint     TEXT CHECK (change_fingerprint IS NULL
                                       OR (length(change_fingerprint) = 64
                                           AND change_fingerprint NOT GLOB '*[^0-9a-f]*')),
    target_refs_json       TEXT CHECK (target_refs_json IS NULL
                                       OR length(target_refs_json) BETWEEN 2 AND 16384),
    evidence_refs_json     TEXT CHECK (evidence_refs_json IS NULL
                                       OR length(evidence_refs_json) BETWEEN 2 AND 16384),
    scope_json             TEXT CHECK (scope_json IS NULL
                                       OR length(scope_json) BETWEEN 2 AND 16384),
    verification_result    TEXT NOT NULL DEFAULT 'unavailable' CHECK (verification_result IN (
                               'unavailable', 'supported', 'contradicted')),
    approval_scope_json    TEXT CHECK (approval_scope_json IS NULL
                                       OR length(approval_scope_json) BETWEEN 2 AND 16384),
    targets_expected_json  TEXT CHECK (targets_expected_json IS NULL
                                       OR length(targets_expected_json) BETWEEN 2 AND 16384),
    approval_expires_at    TEXT CHECK (approval_expires_at IS NULL
                                       OR length(approval_expires_at) BETWEEN 20 AND 27),
    decision_request_id    TEXT NOT NULL CHECK (length(decision_request_id) = 36),
    commit_request_id      TEXT CHECK (commit_request_id IS NULL OR length(commit_request_id) = 36),
    review_after           TEXT CHECK (review_after IS NULL OR length(review_after) BETWEEN 20 AND 27),
    decided_at             TEXT NOT NULL CHECK (length(decided_at) BETWEEN 20 AND 27),
    PRIMARY KEY (namespace, decision_id),
    UNIQUE (namespace, decision_seq),
    UNIQUE (namespace, decision_request_id)
);

INSERT INTO cognition_review_decisions_new (
    namespace, decision_id, decision_seq, candidate_id, artifact_version,
    interaction_id, decision, reason_code, impact, derived_status,
    support_basis, requires_human, evidence_linkage,
    policy_id, policy_version, impact_policy_id, impact_policy_version,
    authority_kind, authorization_id, directive_digest, approval_id,
    change_digest, change_fingerprint, target_refs_json, evidence_refs_json,
    scope_json, verification_result, approval_scope_json, targets_expected_json,
    approval_expires_at, decision_request_id, commit_request_id, review_after,
    decided_at
)
SELECT
    namespace, decision_id, decision_seq, candidate_id, artifact_version,
    interaction_id, decision, reason_code, impact, derived_status,
    support_basis, requires_human, evidence_linkage,
    policy_id, policy_version, impact_policy_id, impact_policy_version,
    authority_kind, authorization_id, directive_digest, approval_id,
    change_digest, change_fingerprint, target_refs_json, evidence_refs_json,
    scope_json, verification_result, approval_scope_json, targets_expected_json,
    approval_expires_at, decision_request_id, commit_request_id, review_after,
    decided_at
FROM cognition_review_decisions;

DROP TABLE cognition_review_decisions;

ALTER TABLE cognition_review_decisions_new RENAME TO cognition_review_decisions;

CREATE UNIQUE INDEX idx_decisions_approval_once
ON cognition_review_decisions (namespace, approval_id)
WHERE approval_id IS NOT NULL AND decision = 'committed';

CREATE INDEX idx_decisions_candidate
ON cognition_review_decisions (namespace, candidate_id, decision_seq);

CREATE INDEX idx_decisions_fingerprint
ON cognition_review_decisions (namespace, change_fingerprint, decided_at);

CREATE TRIGGER cognition_review_decisions_no_update
BEFORE UPDATE ON cognition_review_decisions
BEGIN
    SELECT RAISE(ABORT, 'cognition_review_decisions is append-only');
END;

CREATE TRIGGER cognition_review_decisions_no_delete
BEFORE DELETE ON cognition_review_decisions
BEGIN
    SELECT RAISE(ABORT, 'cognition_review_decisions is append-only');
END;

-- --------------------------------------------------------------- interactions
--
-- A cognition-change candidate is reviewed by a Work-less interaction: the
-- trigger is the Context's own event and there is no work item to bind.  The
-- frozen CHECK ``(mode = 'consult') = (work_id IS NULL)`` forces every review
-- interaction to carry a work binding, so this table is rebuilt with an explicit
-- ``work_less`` flag.  The flag defaults to 0 and only ``register_interaction(
-- work_less=True)`` — called exclusively by the J06 change service — sets it, so
-- the ordinary review path is unchanged.
--
-- ``collab_interaction_inputs`` carries a foreign key to this table, so its rows
-- are first copied into an unconstrained holding table, then both tables are
-- dropped child-first, then both are recreated.  No live child ever references
-- the parent while it is being dropped.

DROP TRIGGER collab_interactions_no_delete;

DROP TRIGGER collab_interactions_update_guard;

DROP TRIGGER collab_interaction_inputs_no_update;

DROP TRIGGER collab_interaction_inputs_no_delete;

DROP INDEX collab_interactions_namespace_idx;

DROP INDEX collab_interactions_context_idx;

CREATE TABLE collab_interaction_inputs_hold AS
SELECT namespace, interaction_id, input_id, content_version, position
FROM collab_interaction_inputs;

DROP TABLE collab_interaction_inputs;

CREATE TABLE collab_interactions_new (
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
    work_less           INTEGER NOT NULL DEFAULT 0 CHECK (work_less IN (0, 1)),
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
    CHECK ((mode = 'review') = (trigger_ref_json IS NOT NULL)),
    CHECK (
        (mode = 'consult') = (work_id IS NULL)
        OR (work_less = 1 AND work_id IS NULL AND trigger_ref_json IS NOT NULL)
    )
);

INSERT INTO collab_interactions_new (
    interaction_id, namespace, schema_version, mode, activation_kind,
    host_instance, trigger_ref_json, work_id, work_generation, context_id,
    result, receipt_request_id, access_ref, policy_ref, created_at, completed_at
)
SELECT
    interaction_id, namespace, schema_version, mode, activation_kind,
    host_instance, trigger_ref_json, work_id, work_generation, context_id,
    result, receipt_request_id, access_ref, policy_ref, created_at, completed_at
FROM collab_interactions;

DROP TABLE collab_interactions;

ALTER TABLE collab_interactions_new RENAME TO collab_interactions;

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

INSERT INTO collab_interaction_inputs (
    namespace, interaction_id, input_id, content_version, position
)
SELECT namespace, interaction_id, input_id, content_version, position
FROM collab_interaction_inputs_hold;

DROP TABLE collab_interaction_inputs_hold;

CREATE INDEX collab_interactions_namespace_idx
    ON collab_interactions (namespace, created_at);

CREATE INDEX collab_interactions_context_idx
    ON collab_interactions (context_id);

CREATE TRIGGER collab_interactions_no_delete
BEFORE DELETE ON collab_interactions
BEGIN
    SELECT RAISE(ABORT, 'collab_interactions rows are audit evidence');
END;

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
       OR NEW.work_less IS NOT OLD.work_less
       OR NEW.access_ref IS NOT OLD.access_ref
       OR NEW.policy_ref IS NOT OLD.policy_ref
       OR NEW.created_at IS NOT OLD.created_at;
END;

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
