-- 0015: an 'ok' model attempt may be a tool round, not only a final answer.
--
-- J07 agent mode calls the model once per tool round.  Such a call succeeds
-- (HTTP 200, a parsed body, no error) while its finish_reason is 'tool_calls',
-- and the ledger previously insisted that an 'ok' row must end in 'stop'.
-- That made every real agent round unsettleable -- and left its reservation
-- counted against the budget.
--
-- The relaxation is deliberately narrow: an 'ok' row still has to carry the
-- response payload it parsed and still may not carry an error label.  What
-- changes is only which finish_reason counts as a successful call.
--
-- model_attempts is rebuilt rather than altered: SQLite cannot change a CHECK
-- in place.  Nothing references this table, so the copy is a straight move.

CREATE TABLE model_attempts_new (
    run_id                    TEXT NOT NULL CHECK (length(run_id) = 36),
    attempt_no                INTEGER NOT NULL CHECK (attempt_no BETWEEN 1 AND 1000),
    attempt_id                TEXT NOT NULL UNIQUE CHECK (length(attempt_id) = 36),
    namespace                 TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    context_id                TEXT NOT NULL CHECK (length(context_id) = 36),
    authorization_id          TEXT NOT NULL,
    authorization_ref         TEXT NOT NULL
                              CHECK (length(authorization_ref) BETWEEN 1 AND 4096),
    manifest_hash             TEXT NOT NULL
                              CHECK (length(manifest_hash) = 71
                                     AND manifest_hash LIKE 'sha256:%'),
    provider                  TEXT NOT NULL CHECK (length(provider) BETWEEN 1 AND 64),
    model_requested           TEXT NOT NULL
                              CHECK (length(model_requested) BETWEEN 1 AND 128),
    model_returned            TEXT CHECK (model_returned IS NULL
                                         OR length(model_returned) BETWEEN 1 AND 256),
    system_fingerprint        TEXT CHECK (system_fingerprint IS NULL
                                         OR length(system_fingerprint) BETWEEN 1 AND 256),
    adapter_kind              TEXT NOT NULL
                              CHECK (length(adapter_kind) BETWEEN 1 AND 64),
    policy_version            TEXT NOT NULL
                              CHECK (length(policy_version) BETWEEN 1 AND 128),
    request_payload_ref       TEXT NOT NULL
                              CHECK (length(request_payload_ref) BETWEEN 1 AND 1024),
    request_hash              TEXT NOT NULL
                              CHECK (length(request_hash) = 71
                                     AND request_hash LIKE 'sha256:%'),
    response_payload_ref      TEXT
                              CHECK (response_payload_ref IS NULL
                                     OR length(response_payload_ref) BETWEEN 1 AND 1024),
    response_hash             TEXT
                              CHECK (response_hash IS NULL
                                     OR (length(response_hash) = 71
                                         AND response_hash LIKE 'sha256:%')),
    usage_json                TEXT CHECK (usage_json IS NULL
                                         OR length(usage_json) BETWEEN 2 AND 65536),
    usage_known               INTEGER NOT NULL DEFAULT 0
                              CHECK (usage_known IN (0, 1)),
    cost_estimate             REAL CHECK (cost_estimate IS NULL
                                         OR cost_estimate >= 0),
    cost_known                INTEGER NOT NULL DEFAULT 0
                              CHECK (cost_known IN (0, 1)),
    finish_reason             TEXT CHECK (finish_reason IS NULL
                                         OR length(finish_reason) BETWEEN 1 AND 64),
    status                    TEXT NOT NULL
                              CHECK (status IN ('reserved', 'finished')),
    outcome                   TEXT CHECK (outcome IN (
                                  'ok', 'malformed', 'failed', 'timeout',
                                  'budget_exceeded')),
    error_label               TEXT CHECK (error_label IS NULL
                                         OR length(error_label) BETWEEN 1 AND 128),
    started_at                TEXT NOT NULL
                              CHECK (length(started_at) BETWEEN 20 AND 27),
    finished_at               TEXT CHECK (finished_at IS NULL
                                         OR length(finished_at) BETWEEN 20 AND 27),
    PRIMARY KEY (run_id, attempt_no),
    FOREIGN KEY (context_id) REFERENCES context_snapshots (context_id),
    FOREIGN KEY (authorization_id)
        REFERENCES model_authorizations (authorization_id),
    CHECK ((response_payload_ref IS NULL) = (response_hash IS NULL)),
    CHECK (usage_known = 0 OR usage_json IS NOT NULL),
    CHECK (cost_known = 0 OR cost_estimate IS NOT NULL),
    -- Before sending: nothing about the result may exist yet.
    CHECK (
        status = 'finished'
        OR (
            outcome IS NULL
            AND response_payload_ref IS NULL
            AND response_hash IS NULL
            AND model_returned IS NULL
            AND system_fingerprint IS NULL
            AND finish_reason IS NULL
            AND error_label IS NULL
            AND finished_at IS NULL
            AND usage_known = 0
            AND cost_known = 0
            AND cost_estimate IS NULL
        )
    ),
    -- A finished row always names what happened.  An 'ok' row is one call that
    -- the provider answered: the parsed response is stored and there is no
    -- error.  Its finish_reason says whether that call ended the turn or asked
    -- for a tool; both are successful calls, only one is a final answer.
    CHECK (
        status = 'reserved'
        OR (
            outcome IS NOT NULL
            AND finished_at IS NOT NULL
            AND (outcome <> 'ok'
                 OR (finish_reason IN ('stop', 'tool_calls')
                     AND response_payload_ref IS NOT NULL
                     AND error_label IS NULL))
        )
    )
);

INSERT INTO model_attempts_new (
    run_id, attempt_no, attempt_id, namespace, context_id, authorization_id,
    authorization_ref, manifest_hash, provider, model_requested, model_returned,
    system_fingerprint, adapter_kind, policy_version, request_payload_ref,
    request_hash, response_payload_ref, response_hash, usage_json, usage_known,
    cost_estimate, cost_known, finish_reason, status, outcome, error_label,
    started_at, finished_at
)
SELECT
    run_id, attempt_no, attempt_id, namespace, context_id, authorization_id,
    authorization_ref, manifest_hash, provider, model_requested, model_returned,
    system_fingerprint, adapter_kind, policy_version, request_payload_ref,
    request_hash, response_payload_ref, response_hash, usage_json, usage_known,
    cost_estimate, cost_known, finish_reason, status, outcome, error_label,
    started_at, finished_at
FROM model_attempts;

DROP TABLE model_attempts;

ALTER TABLE model_attempts_new RENAME TO model_attempts;

CREATE INDEX model_attempts_authorization_idx
    ON model_attempts (authorization_id, status);

CREATE INDEX model_attempts_context_idx
    ON model_attempts (context_id);

CREATE INDEX model_attempts_namespace_started_idx
    ON model_attempts (namespace, started_at);

CREATE TRIGGER model_attempts_no_delete
BEFORE DELETE ON model_attempts
BEGIN
    SELECT RAISE(ABORT, 'model_attempts rows are audit evidence');
END;

CREATE TRIGGER model_attempts_finish_only
BEFORE UPDATE ON model_attempts
BEGIN
    SELECT RAISE(ABORT, 'model_attempts may only transition reserved -> finished once')
    WHERE OLD.status <> 'reserved'
       OR NEW.status <> 'finished';
    SELECT RAISE(ABORT, 'model_attempts request and binding columns are immutable')
    WHERE NEW.run_id IS NOT OLD.run_id
       OR NEW.attempt_no IS NOT OLD.attempt_no
       OR NEW.attempt_id IS NOT OLD.attempt_id
       OR NEW.namespace IS NOT OLD.namespace
       OR NEW.context_id IS NOT OLD.context_id
       OR NEW.authorization_id IS NOT OLD.authorization_id
       OR NEW.authorization_ref IS NOT OLD.authorization_ref
       OR NEW.manifest_hash IS NOT OLD.manifest_hash
       OR NEW.provider IS NOT OLD.provider
       OR NEW.model_requested IS NOT OLD.model_requested
       OR NEW.adapter_kind IS NOT OLD.adapter_kind
       OR NEW.policy_version IS NOT OLD.policy_version
       OR NEW.request_payload_ref IS NOT OLD.request_payload_ref
       OR NEW.request_hash IS NOT OLD.request_hash
       OR NEW.started_at IS NOT OLD.started_at;
END;
