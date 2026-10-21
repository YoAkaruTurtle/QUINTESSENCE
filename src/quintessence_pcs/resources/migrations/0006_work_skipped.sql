-- PCS2-008-R1: a deleted pending source must reach a terminal work state.
--
-- 0005's ``work_progress`` only allowed ``committed`` / ``checked_no_belief``,
-- both of which require a live Context.  A source event can be deleted after
-- Tx A created ``pending_work`` but before derivation; such a work item has no
-- Context, yet must not stay in the unfinished queue forever.  This migration
-- adds the third terminal outcome ``skipped`` (reason ``source_redacted``) to
-- 008's own table.
--
-- 008's table is rebuilt (not ``pending_work``): the old CHECK is frozen and
-- additive SQLite cannot widen a CHECK in place.  The rebuild is lossless:
-- every existing row is copied with ``reason = NULL`` and its old outcome.
-- The migration is ordered so the old table is dropped only after the copy
-- has been inserted; the table's triggers and namespace index are recreated
-- afterwards.  No immutable table (record_versions, event_journal,
-- private_payloads) is touched.

CREATE TABLE work_progress_new (
    work_id                  TEXT NOT NULL PRIMARY KEY CHECK (length(work_id) = 36),
    namespace                TEXT NOT NULL CHECK (length(namespace) BETWEEN 1 AND 128),
    outcome                  TEXT NOT NULL
                             CHECK (outcome IN ('committed', 'checked_no_belief', 'skipped')),
    context_id               TEXT
                             CHECK (context_id IS NULL
                                    OR length(context_id) BETWEEN 1 AND 128),
    completed_state_revision INTEGER NOT NULL
                             CHECK (completed_state_revision >= 0),
    reason                   TEXT
                             CHECK (reason IS NULL OR reason = 'source_redacted'),
    updated_at               TEXT NOT NULL CHECK (length(updated_at) BETWEEN 20 AND 27),
    -- A normal completion belongs to a Context and has no skip reason.  A skip
    -- is caused by a committed deletion barrier, has no Context, and names
    -- that cause explicitly; it is never a stand-in for a legal WAIT.
    CHECK (
        (outcome = 'skipped'
            AND context_id IS NULL
            AND reason = 'source_redacted')
        OR
        (outcome IN ('committed', 'checked_no_belief')
            AND context_id IS NOT NULL
            AND reason IS NULL)
    ),
    FOREIGN KEY (work_id) REFERENCES pending_work (work_id),
    FOREIGN KEY (context_id) REFERENCES context_snapshots (context_id)
);

INSERT INTO work_progress_new (
    work_id, namespace, outcome, context_id, completed_state_revision, reason, updated_at
)
SELECT work_id, namespace, outcome, context_id, completed_state_revision, NULL, updated_at
FROM work_progress;

DROP TABLE work_progress;

ALTER TABLE work_progress_new RENAME TO work_progress;

CREATE INDEX work_progress_namespace_idx
    ON work_progress (namespace, outcome);

CREATE TRIGGER work_progress_no_update
BEFORE UPDATE ON work_progress
BEGIN
    SELECT RAISE(ABORT, 'work_progress rows are immutable');
END;

CREATE TRIGGER work_progress_no_delete
BEFORE DELETE ON work_progress
BEGIN
    SELECT RAISE(ABORT, 'work_progress rows are immutable');
END;
