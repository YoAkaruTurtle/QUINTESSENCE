-- PCS2-009: manifest-separated model authorization and per-HTTP-attempt ledger.
--
-- Two tables, both owned by the runtime plane rather than the domain plane:
--
--   * ``model_authorizations`` is the *permission* to call a provider.  It is
--     deliberately not a config flag: a manifest says how to connect, while an
--     authorization row (written by an explicit human/API step) says whether a
--     call is allowed.  The row is bound to provider + model + manifest_hash;
--     changing any of them invalidates it.
--
--   * ``model_attempts`` is one row per HTTP try.  A logical run may have more
--     than one try (a 429/5xx/timeout retry, or one format repair), so the
--     primary key is (run_id, attempt_no), never just run_id.
--
-- The one intentional departure from the "stored evidence is immutable" rule:
-- a model attempt must be written *before* the request is sent so that a crash
-- after sending cannot make the call look like it never happened.  The row is
-- inserted with status='reserved' and is updated exactly once to
-- status='finished'.  The trigger below enforces both the single transition and
-- the immutability of every request/binding column.  No DELETE is allowed.

-- ----------------------------------------------------- model_authorizations
CREATE TABLE model_authorizations (
    authorization_id          TEXT NOT NULL PRIMARY KEY
                              CHECK (length(authorization_id) = 36),
    namespace                 TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    -- The user's own words (or the explicit API caller's reference).  This is
    -- the audit record of *who allowed this and for what scope*; it is never
    -- derived from a config file.
    authorization_ref         TEXT NOT NULL
                              CHECK (length(authorization_ref) BETWEEN 1 AND 4096),
    provider                  TEXT NOT NULL CHECK (length(provider) BETWEEN 1 AND 64),
    model                     TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 128),
    manifest_hash             TEXT NOT NULL
                              CHECK (length(manifest_hash) = 71
                                     AND manifest_hash LIKE 'sha256:%'),
    max_calls                 INTEGER NOT NULL
                              CHECK (max_calls BETWEEN 1 AND 1000),
    max_output_tokens_per_call INTEGER NOT NULL
                              CHECK (max_output_tokens_per_call BETWEEN 1 AND 393216),
    max_wall_seconds          INTEGER NOT NULL
                              CHECK (max_wall_seconds BETWEEN 1 AND 86400),
    granted_at                TEXT NOT NULL
                              CHECK (length(granted_at) BETWEEN 20 AND 27),
    expires_at                TEXT
                              CHECK (expires_at IS NULL
                                     OR length(expires_at) BETWEEN 20 AND 27),
    status                    TEXT NOT NULL
                              CHECK (status IN ('active', 'revoked', 'exhausted')),
    updated_at                TEXT NOT NULL
                              CHECK (length(updated_at) BETWEEN 20 AND 27),
    CHECK (expires_at IS NULL
           OR julianday(expires_at) > julianday(granted_at))
);

CREATE INDEX model_authorizations_scope_idx
    ON model_authorizations (namespace, provider, model, manifest_hash, status);

CREATE TRIGGER model_authorizations_no_delete
BEFORE DELETE ON model_authorizations
BEGIN
    SELECT RAISE(ABORT, 'model_authorizations rows are audit evidence');
END;

-- Only the lifecycle status may change, and only leaving 'active' once.
-- Scope, limits and grant times are immutable; revoking is not a way to rewrite
-- what was authorized.
CREATE TRIGGER model_authorizations_status_only
BEFORE UPDATE ON model_authorizations
BEGIN
    SELECT RAISE(ABORT, 'model_authorizations binding columns are immutable')
    WHERE NEW.authorization_id IS NOT OLD.authorization_id
       OR NEW.namespace IS NOT OLD.namespace
       OR NEW.authorization_ref IS NOT OLD.authorization_ref
       OR NEW.provider IS NOT OLD.provider
       OR NEW.model IS NOT OLD.model
       OR NEW.manifest_hash IS NOT OLD.manifest_hash
       OR NEW.max_calls IS NOT OLD.max_calls
       OR NEW.max_output_tokens_per_call IS NOT OLD.max_output_tokens_per_call
       OR NEW.max_wall_seconds IS NOT OLD.max_wall_seconds
       OR NEW.granted_at IS NOT OLD.granted_at
       OR NEW.expires_at IS NOT OLD.expires_at;
    SELECT RAISE(ABORT, 'model_authorizations status may only leave active once')
    WHERE OLD.status <> 'active'
       OR NEW.status NOT IN ('revoked', 'exhausted');
END;

-- ----------------------------------------------------------- model_attempts
CREATE TABLE model_attempts (
    run_id                    TEXT NOT NULL CHECK (length(run_id) = 36),
    attempt_no                INTEGER NOT NULL CHECK (attempt_no BETWEEN 1 AND 1000),
    attempt_id                TEXT NOT NULL UNIQUE CHECK (length(attempt_id) = 36),
    namespace                 TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    context_id                TEXT NOT NULL CHECK (length(context_id) = 36),
    authorization_id          TEXT NOT NULL,
    -- Denormalized user-facing reference; the authorization row is the
    -- authority and the FK below is the structural link.
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
    -- Each attempt owns its own request/response payload rows.  They are not
    -- content-addressed, so clearing one attempt after a deletion cannot clear
    -- another attempt by accident.
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
    -- A finished row always names what happened.  An 'ok' row's own columns
    -- must agree with the card: stop, non-empty content was stored, no error.
    CHECK (
        status = 'reserved'
        OR (
            outcome IS NOT NULL
            AND finished_at IS NOT NULL
            AND (outcome <> 'ok'
                 OR (finish_reason = 'stop'
                     AND response_payload_ref IS NOT NULL
                     AND error_label IS NULL))
        )
    )
);

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

-- One controlled UPDATE: reserved -> finished, once, without rewriting the
-- request/binding columns.  This is the documented exemption from the
-- "immutable evidence" rule; it exists because the budget must be occupied
-- before the network call and settled after it.
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
