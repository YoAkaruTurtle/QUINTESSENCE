-- J06: cognition governance — trusted commit authorizations, append-only
-- review decisions, record provenance and project subject authority.
--
-- Additive only.  No existing table changes and 0011/0012 stay byte-identical.
-- Decision rows are append-only; the two triggers below make that a database
-- property rather than an API convention.  Provenance rows are written in the
-- same command as the record versions they point at, so "why is this belief
-- current?" stays answerable without a second audit store.

-- ------------------------------------------------ commit authorizations
--
-- One bounded, revocable pre-authorization for autonomous submission.  The
-- columns are the binding predicate: what may be submitted (operations and
-- claim types), inside which scope, up to which impact, by whom, under which
-- policy version, and until when.

CREATE TABLE cognition_commit_authorizations (
    namespace                TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    authorization_id         TEXT NOT NULL CHECK (length(authorization_id) = 36),
    host_instance            TEXT NOT NULL CHECK (length(host_instance) BETWEEN 1 AND 256),
    access_ref               TEXT NOT NULL CHECK (length(access_ref) BETWEEN 1 AND 512),
    operations_json          TEXT NOT NULL CHECK (length(operations_json) BETWEEN 2 AND 16384),
    claim_types_json         TEXT NOT NULL CHECK (length(claim_types_json) BETWEEN 2 AND 16384),
    scope_json               TEXT NOT NULL CHECK (length(scope_json) BETWEEN 2 AND 16384),
    max_impact               TEXT NOT NULL CHECK (max_impact IN ('E0', 'E1')),
    max_changes_per_bundle   INTEGER NOT NULL CHECK (max_changes_per_bundle BETWEEN 1 AND 4),
    max_targets_per_bundle   INTEGER NOT NULL CHECK (max_targets_per_bundle BETWEEN 1 AND 4),
    policy_id                TEXT NOT NULL CHECK (length(policy_id) BETWEEN 1 AND 128),
    policy_version           TEXT NOT NULL CHECK (length(policy_version) BETWEEN 1 AND 64),
    authorization_source     TEXT NOT NULL CHECK (authorization_source IN ('host_config', 'user_confirmed')),
    issued_at                TEXT NOT NULL CHECK (length(issued_at) BETWEEN 20 AND 27),
    expires_at               TEXT CHECK (expires_at IS NULL OR length(expires_at) BETWEEN 20 AND 27),
    revoked_at               TEXT CHECK (revoked_at IS NULL OR length(revoked_at) BETWEEN 20 AND 27),
    revoke_reason            TEXT CHECK (revoke_reason IS NULL OR length(revoke_reason) BETWEEN 1 AND 512),
    issued_by                TEXT NOT NULL CHECK (length(issued_by) BETWEEN 1 AND 256),
    PRIMARY KEY (namespace, authorization_id)
);

CREATE INDEX idx_commit_auth_active
ON cognition_commit_authorizations (namespace, revoked_at, expires_at);

-- ------------------------------------------------ review decisions
--
-- Append-only audit of every review conclusion, including the ones that write
-- no record version (awaiting_human / deferred / escalated / not_authorized /
-- conflict).  ``decision_seq`` is the per-namespace read cursor; it is assigned
-- inside the writing transaction.

CREATE TABLE cognition_review_decisions (
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
    impact                 TEXT NOT NULL CHECK (impact IN ('E0', 'E1', 'E2')),
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

-- One approval may authorise at most one committed decision.
CREATE UNIQUE INDEX idx_decisions_approval_once
ON cognition_review_decisions (namespace, approval_id)
WHERE approval_id IS NOT NULL AND decision = 'committed';

CREATE INDEX idx_decisions_candidate
ON cognition_review_decisions (namespace, candidate_id, decision_seq);

CREATE INDEX idx_decisions_fingerprint
ON cognition_review_decisions (namespace, change_fingerprint, decided_at);

-- Append-only is enforced by the database, not only by the write API.
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

-- ------------------------------------------------ record provenance
--
-- Reverse lookup: given an exact committed record version, which decision put
-- it there?  Written in the same transaction as the version itself.

CREATE TABLE cognition_record_decisions (
    namespace      TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    record_id      TEXT NOT NULL CHECK (length(record_id) = 36),
    record_version INTEGER NOT NULL CHECK (record_version >= 1),
    decision_id    TEXT NOT NULL CHECK (length(decision_id) = 36),
    decided_at     TEXT NOT NULL CHECK (length(decided_at) BETWEEN 20 AND 27),
    PRIMARY KEY (namespace, record_id, record_version)
);

CREATE INDEX idx_record_decisions_decision
ON cognition_record_decisions (namespace, decision_id);

-- ------------------------------------------------ project subject authority
--
-- "This person may decide for this project."  Seeded by a trusted governance
-- API; a user statement alone is not evidence of project authority.

CREATE TABLE cognition_subject_authority (
    namespace      TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    project_id     TEXT NOT NULL CHECK (length(project_id) BETWEEN 1 AND 256),
    subject_ref    TEXT NOT NULL CHECK (length(subject_ref) BETWEEN 1 AND 256),
    basis          TEXT NOT NULL CHECK (basis IN ('owner', 'delegated')),
    granted_by     TEXT NOT NULL CHECK (length(granted_by) BETWEEN 1 AND 256),
    granted_at     TEXT NOT NULL CHECK (length(granted_at) BETWEEN 20 AND 27),
    expires_at     TEXT CHECK (expires_at IS NULL OR length(expires_at) BETWEEN 20 AND 27),
    revoked_at     TEXT CHECK (revoked_at IS NULL OR length(revoked_at) BETWEEN 20 AND 27),
    revoke_reason  TEXT CHECK (revoke_reason IS NULL OR length(revoke_reason) BETWEEN 1 AND 512),
    PRIMARY KEY (namespace, project_id, subject_ref)
);
