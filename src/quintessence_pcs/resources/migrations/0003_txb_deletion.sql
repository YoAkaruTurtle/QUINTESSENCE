-- PCS2-006: Tx B snapshots/idempotency receipts and the deletion barrier.
--
-- Additive only.  0001_init.sql and 0002_events.sql stay byte-identical: this
-- migration neither drops nor relaxes their immutability triggers.  Deletion
-- is implemented as a read barrier (``deletion_targets``) plus clearing the
-- controlled payload body (plan A / B1 in the handoff card), so no immutable
-- row has to be rewritten and no guard trigger is needed.
--
-- Payload references written from this card on are opaque (``payload:<uuid>``)
-- so an immutable ``body_payload_ref`` / ``payload_ref`` never pins the
-- digest of a deleted body.  The digest lives only in ``private_payloads``,
-- which cleanup overwrites.  The index below supports de-duplication by
-- digest instead of by reference.

CREATE TABLE context_snapshots (
    context_id        TEXT PRIMARY KEY
                      CHECK (length(context_id) BETWEEN 1 AND 128),
    namespace         TEXT NOT NULL
                      CHECK (length(namespace) BETWEEN 1 AND 128),
    state_revision    INTEGER NOT NULL CHECK (state_revision >= 0),
    context_event_seq INTEGER NOT NULL CHECK (context_event_seq >= 0),
    privacy_epoch     INTEGER NOT NULL CHECK (privacy_epoch >= 0),
    created_at        TEXT NOT NULL
                      CHECK (length(created_at) BETWEEN 20 AND 27)
);

-- Snapshot lookup for diagnostics; commit_command looks rows up by primary key.
CREATE INDEX context_snapshots_namespace_idx
    ON context_snapshots (namespace, created_at);

-- One committed command per (namespace, request_id).  ``receipt_json`` holds
-- only IDs, versions and watermarks: never private body text (J.3/J.6).
CREATE TABLE processed_commands (
    namespace      TEXT NOT NULL
                   CHECK (length(namespace) BETWEEN 1 AND 128),
    request_id     TEXT NOT NULL
                   CHECK (length(request_id) BETWEEN 1 AND 128),
    request_digest TEXT NOT NULL CHECK (length(request_digest) = 64),
    operation      TEXT NOT NULL
                   CHECK (length(operation) BETWEEN 1 AND 128),
    receipt_json   TEXT NOT NULL CHECK (length(receipt_json) >= 2),
    committed_at   TEXT NOT NULL
                   CHECK (length(committed_at) BETWEEN 20 AND 27),
    PRIMARY KEY (namespace, request_id)
);

-- The durable deletion barrier.  A row exists from the moment Tx1 commits;
-- status only moves pending_cleanup -> cleaned (never back).  No private body
-- is stored here: ``authorization_ref`` names the authorization, not its
-- content, and ``last_error`` is a public diagnostic string.
CREATE TABLE deletion_targets (
    target_kind       TEXT NOT NULL
                      CHECK (target_kind IN ('record', 'event')),
    namespace         TEXT NOT NULL
                      CHECK (length(namespace) BETWEEN 1 AND 128),
    target_id         TEXT NOT NULL CHECK (length(target_id) = 36),
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
    PRIMARY KEY (namespace, target_kind, target_id)
);

-- Cleanup resumes from this index after a restart or a crash.
CREATE INDEX deletion_targets_pending_idx
    ON deletion_targets (status, namespace, created_at);

-- De-duplication after B1: lookup by digest, not by payload_ref.
CREATE INDEX private_payloads_sha256_idx
    ON private_payloads (sha256);
