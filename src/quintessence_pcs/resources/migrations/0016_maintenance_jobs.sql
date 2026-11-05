-- 0016: bounded maintenance jobs and their recovery bookkeeping.
--
-- J08 needs a durable record of *maintenance work*: which bounded task is
-- running, where its scan got to, what has actually been confirmed successful,
-- and which candidate/decision/receipt an item refers to.  The existing Memory
-- outbox tracks deliveries and the governance tables track review decisions;
-- neither is a task queue with a lease, a contiguous success checkpoint and a
-- retry schedule, and reusing their column names would have been a fiction.
--
-- These three tables are *operational control* only.  They are not a second
-- Memory store and not a second Cognition authority: no Belief body, no
-- Question body and no source text is duplicated here.  Every row points at
-- the authoritative record by id/version; the authoritative state still lives
-- in `record_versions`, `cognition_review_decisions` and the command receipts.
--
-- The scan position and the success checkpoint are deliberately separate
-- columns.  A scan may run far ahead of what is actually confirmed committed,
-- and recovery must never treat "we looked at it" as "we finished it".

CREATE TABLE maintenance_jobs (
    task_id             TEXT NOT NULL PRIMARY KEY
                        CHECK (length(task_id) = 36),
    namespace           TEXT NOT NULL
                        CHECK (length(namespace) BETWEEN 1 AND 128),
    task_type           TEXT NOT NULL
                        CHECK (length(task_type) BETWEEN 1 AND 64),
    -- One logical trigger may name exactly one job: the same key with a
    -- different parameter digest is a conflict, not a second opportunity.
    trigger_key         TEXT NOT NULL
                        CHECK (length(trigger_key) BETWEEN 1 AND 256),
    params_digest       TEXT NOT NULL CHECK (length(params_digest) = 64),
    config_version      TEXT NOT NULL
                        CHECK (length(config_version) BETWEEN 1 AND 128),
    -- Fixed input boundary, captured once at registration and never widened.
    input_watermark     TEXT CHECK (input_watermark IS NULL
                                    OR length(input_watermark) BETWEEN 1 AND 256),
    target_watermark    TEXT CHECK (target_watermark IS NULL
                                    OR length(target_watermark) BETWEEN 1 AND 256),
    -- Where the scan has reached (opaque JSON, host-defined).
    scan_position       TEXT CHECK (scan_position IS NULL
                                    OR length(scan_position) BETWEEN 2 AND 65536),
    -- Highest item_no whose whole prefix is confirmed.  It is an integer so
    -- the store can *compute and enforce* contiguity; a cursor can never be
    -- mistaken for it.
    success_checkpoint  INTEGER NOT NULL DEFAULT 0
                        CHECK (success_checkpoint BETWEEN 0 AND 1000000),
    state               TEXT NOT NULL
                        CHECK (state IN (
                            'pending', 'leased',
                            'waiting_authorization', 'waiting_human',
                            'waiting_dependency', 'waiting_budget',
                            'paused', 'cancelled', 'failed', 'obsolete',
                            'succeeded')),
    attempt             INTEGER NOT NULL DEFAULT 0
                        CHECK (attempt BETWEEN 0 AND 1000),
    lease_token         TEXT CHECK (lease_token IS NULL
                                    OR length(lease_token) = 36),
    lease_until         TEXT CHECK (lease_until IS NULL
                                    OR length(lease_until) BETWEEN 20 AND 27),
    next_retry_at       TEXT CHECK (next_retry_at IS NULL
                                    OR length(next_retry_at) BETWEEN 20 AND 27),
    budget_units        INTEGER CHECK (budget_units IS NULL OR budget_units >= 0),
    units_spent         INTEGER NOT NULL DEFAULT 0 CHECK (units_spent >= 0),
    last_error          TEXT CHECK (last_error IS NULL
                                    OR length(last_error) BETWEEN 1 AND 512),
    last_receipt_ref    TEXT CHECK (last_receipt_ref IS NULL
                                    OR length(last_receipt_ref) BETWEEN 1 AND 1024),
    created_at          TEXT NOT NULL
                        CHECK (length(created_at) BETWEEN 20 AND 27),
    updated_at          TEXT NOT NULL
                        CHECK (length(updated_at) BETWEEN 20 AND 27),
    UNIQUE (namespace, trigger_key),
    -- A lease is exactly the 'leased' state: neither can exist without the
    -- other, so a crashed worker cannot leave a token on a non-leased job.
    CHECK ((state = 'leased') = (lease_token IS NOT NULL)),
    CHECK ((lease_token IS NULL) = (lease_until IS NULL))
);

CREATE INDEX maintenance_jobs_state_idx
    ON maintenance_jobs (namespace, state);

CREATE INDEX maintenance_jobs_retry_idx
    ON maintenance_jobs (state, next_retry_at);

CREATE TABLE maintenance_items (
    task_id             TEXT NOT NULL,
    item_no             INTEGER NOT NULL CHECK (item_no BETWEEN 1 AND 1000000),
    target_kind         TEXT NOT NULL
                        CHECK (target_kind IN ('record', 'event',
                                               'memory_delivery', 'question')),
    target_id           TEXT NOT NULL
                        CHECK (length(target_id) BETWEEN 1 AND 128),
    target_version      INTEGER NOT NULL DEFAULT 0 CHECK (target_version >= 0),
    -- Stable identity of the *operation*, so a retry of the same work dedupes
    -- on it instead of producing a second logical submission.
    operation_key       TEXT NOT NULL
                        CHECK (length(operation_key) BETWEEN 1 AND 256),
    request_id          TEXT CHECK (request_id IS NULL OR length(request_id) = 36),
    candidate_id        TEXT CHECK (candidate_id IS NULL OR length(candidate_id) = 36),
    decision_id         TEXT CHECK (decision_id IS NULL OR length(decision_id) = 36),
    receipt_ref         TEXT CHECK (receipt_ref IS NULL
                                    OR length(receipt_ref) BETWEEN 1 AND 1024),
    outcome             TEXT NOT NULL DEFAULT 'pending'
                        CHECK (outcome IN (
                            'pending', 'committed', 'no_change',
                            'awaiting_human', 'rejected', 'deferred',
                            'failed', 'waiting_dependency')),
    detail              TEXT CHECK (detail IS NULL
                                    OR length(detail) BETWEEN 1 AND 1024),
    created_at          TEXT NOT NULL
                        CHECK (length(created_at) BETWEEN 20 AND 27),
    updated_at          TEXT NOT NULL
                        CHECK (length(updated_at) BETWEEN 20 AND 27),
    PRIMARY KEY (task_id, item_no),
    UNIQUE (task_id, operation_key),
    FOREIGN KEY (task_id) REFERENCES maintenance_jobs (task_id)
);

CREATE INDEX maintenance_items_outcome_idx
    ON maintenance_items (task_id, outcome);

CREATE TABLE maintenance_attempts (
    task_id             TEXT NOT NULL,
    attempt_no          INTEGER NOT NULL CHECK (attempt_no BETWEEN 1 AND 1000),
    phase               TEXT NOT NULL
                        CHECK (length(phase) BETWEEN 1 AND 64),
    started_at          TEXT NOT NULL
                        CHECK (length(started_at) BETWEEN 20 AND 27),
    finished_at         TEXT CHECK (finished_at IS NULL
                                    OR length(finished_at) BETWEEN 20 AND 27),
    outcome             TEXT CHECK (outcome IS NULL OR outcome IN (
                            'succeeded', 'partial', 'failed', 'paused',
                            'waiting', 'cancelled')),
    stop_reason         TEXT CHECK (stop_reason IS NULL
                                    OR length(stop_reason) BETWEEN 1 AND 128),
    units_spent         INTEGER CHECK (units_spent IS NULL OR units_spent >= 0),
    receipt_ref         TEXT CHECK (receipt_ref IS NULL
                                    OR length(receipt_ref) BETWEEN 1 AND 1024),
    detail              TEXT CHECK (detail IS NULL
                                    OR length(detail) BETWEEN 1 AND 1024),
    PRIMARY KEY (task_id, attempt_no),
    FOREIGN KEY (task_id) REFERENCES maintenance_jobs (task_id)
);

-- Attempts are append-only audit evidence: a finished attempt may be closed
-- exactly once and nothing else about it may change afterwards.
CREATE TRIGGER maintenance_attempts_no_delete
BEFORE DELETE ON maintenance_attempts
BEGIN
    SELECT RAISE(ABORT, 'maintenance_attempts rows are audit evidence');
END;

CREATE TRIGGER maintenance_attempts_close_once
BEFORE UPDATE ON maintenance_attempts
BEGIN
    SELECT RAISE(ABORT, 'a maintenance attempt may only be closed once')
    WHERE OLD.finished_at IS NOT NULL OR NEW.finished_at IS NULL;
    SELECT RAISE(ABORT, 'maintenance attempt identity is immutable')
    WHERE NEW.task_id IS NOT OLD.task_id
       OR NEW.attempt_no IS NOT OLD.attempt_no
       OR NEW.phase IS NOT OLD.phase
       OR NEW.started_at IS NOT OLD.started_at;
END;

-- An item's identity (which target, which operation) is fixed at first write;
-- only its outcome, receipt and detail may move afterwards.
CREATE TRIGGER maintenance_items_identity_immutable
BEFORE UPDATE ON maintenance_items
BEGIN
    SELECT RAISE(ABORT, 'maintenance item identity is immutable')
    WHERE NEW.task_id IS NOT OLD.task_id
       OR NEW.item_no IS NOT OLD.item_no
       OR NEW.target_kind IS NOT OLD.target_kind
       OR NEW.target_id IS NOT OLD.target_id
       OR NEW.target_version IS NOT OLD.target_version
       OR NEW.operation_key IS NOT OLD.operation_key;
END;
