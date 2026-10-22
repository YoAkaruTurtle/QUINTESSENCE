-- C05d: stable candidate request keys and single-version candidates.
--
-- Two partial unique indexes.  They do not rebuild any table and do not touch
-- reply/query_result rows.
--
-- * ``(namespace, request_id)`` is the stable request key inside candidates.
--   A retry with the same key is compared by the complete identity fingerprint
--   in the store, not by a digest query, so a hash collision can never be
--   treated as the same identity.
-- * ``(namespace, artifact_id)`` is unique inside candidates, which makes
--   ``artifact_id`` without a version unambiguous for every state API.  v1
--   candidates remain single-version objects (``artifact_version`` is 1).

CREATE UNIQUE INDEX collab_artifacts_request_key_idx
    ON collab_artifacts (namespace, request_id)
    WHERE kind = 'candidate' AND request_id IS NOT NULL;

CREATE UNIQUE INDEX collab_candidates_single_version_idx
    ON collab_artifacts (namespace, artifact_id)
    WHERE kind = 'candidate';
