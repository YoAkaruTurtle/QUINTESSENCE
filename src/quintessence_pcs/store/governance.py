"""J06: governance access layer for the cognition change loop.

This module is a thin, read-mostly wrapper over the four tables created by
``0013_cognition_governance.sql``.  It deliberately holds **no policy**: the
impact classifier, the review rules and the commit decision live in
``collaboration/`` (S6/S7).  What lives here is only the plumbing those layers
need:

* the append-only review-decision log and its per-namespace read cursor;
* the bounded auto-commit authorizations, with the revocation/expiry predicate
  expressed as data rather than as a policy decision;
* the project subject authority registry, including the trusted
  seeding/revoking API;
* the provenance reverse lookup ("which decision wrote this exact version?").

S5b adds the change-candidate state layer: ``lifecycle()`` derives
``pending``/``committed``/``rejected`` from the Tx B receipt and the ``rejected``
terminal decision row only, and ``require_current_change_candidate()`` is the
Work-less counterpart of C06a's ``require_current_candidate``.  Neither reads
``work_id``: how a candidate is *created* is a creation policy, how it is *read*
is a currentness fact.

Why a wrapper and not another store mixin: ``CognitionStore`` is composed from
mixins in ``store/cognition.py`` and this card must not edit that file.  Wrapping
keeps the access layer usable with any already-open store (services construct it
the same way they construct ``CollaborationService``) and keeps the read paths
honest: a governance read can never write a record version.

Nothing here is a second authority.  ``commit_command`` remains the only writer
of record versions and receipts, and ``cognition_review_decisions`` remains
append-only at the database level.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..foundation import identifiers
from ..foundation.errors import (
    CorruptStore,
    InvalidInput,
    RecordKindMismatch,
    RecordNotFound,
    ScopeViolation,
)
from ..foundation.jsonutil import canonical_json_text, strict_json_loads
from ..foundation.timeutil import utc_now_rfc3339
from .errors import CandidateTransitionDenied

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from .collaboration import ArtifactRow

#: The closed permission vocabulary for the governance surface.  A host builds
#: :class:`GovernanceAccess`; model output can never widen it.
GOVERNANCE_PERMISSIONS = frozenset({"governance.admin", "governance.read"})

#: The only outer operation a Work-less J06 candidate may carry.  S5b's
#: ``require_current_change_candidate`` will match a proposal against it.
CHANGE_OPERATION = "cognition.change"

#: The four levels the decision table accepts.  ``BLOCKED`` is not "very high
#: risk": it is the level no authority may commit, which is why the column has
#: to store it (``0014`` widens the frozen CHECK).  This tuple is the single
#: source of truth; the review policy imports and re-exports it.
IMPACT_LEVELS = ("E0", "E1", "E2", "BLOCKED")

DECISION_KINDS = (
    "awaiting_human",
    "deferred",
    "escalated",
    "not_authorized",
    "rejected",
    "committed",
    "conflict",
    "approval_expired",
    "approval_revoked",
)

#: The vocabulary the S5b lifecycle derivation will use.  Kept here so the
#: three states are named in one place rather than re-invented by callers.
LIFECYCLE_STATES = ("pending", "committed", "rejected")

SUBJECT_AUTHORITY_BASES = ("owner", "delegated")

_MAX_LIMIT = 500
_MAX_TEXT = 512
_MAX_IDENTIFIER = 256
_MAX_FINGERPRINTS = 16

_DECISION_COLUMNS = (
    "namespace",
    "decision_id",
    "decision_seq",
    "candidate_id",
    "artifact_version",
    "interaction_id",
    "decision",
    "reason_code",
    "impact",
    "derived_status",
    "support_basis",
    "requires_human",
    "evidence_linkage",
    "policy_id",
    "policy_version",
    "impact_policy_id",
    "impact_policy_version",
    "authority_kind",
    "authorization_id",
    "directive_digest",
    "approval_id",
    "change_digest",
    "change_fingerprint",
    "target_refs_json",
    "evidence_refs_json",
    "scope_json",
    "verification_result",
    "approval_scope_json",
    "targets_expected_json",
    "approval_expires_at",
    "decision_request_id",
    "commit_request_id",
    "review_after",
    "decided_at",
)

_DECISION_JSON_COLUMNS = (
    "target_refs_json",
    "evidence_refs_json",
    "scope_json",
    "approval_scope_json",
    "targets_expected_json",
)


@dataclass(frozen=True)
class GovernanceAccess:
    """Trusted-host authority for one governance call.

    As with every other access object in PCS this is **not** authentication: the
    host constructs it and the store only checks its shape.  ``permissions``
    must stay inside :data:`GOVERNANCE_PERMISSIONS`.
    """

    namespace: str
    host_instance: str
    operator: str
    access_ref: str
    permissions: frozenset[str]

    def __post_init__(self) -> None:
        identifiers.require_namespace(self.namespace)
        _require_text(self.host_instance, "host_instance", _MAX_IDENTIFIER)
        _require_text(self.operator, "operator", _MAX_IDENTIFIER)
        _require_text(self.access_ref, "access_ref", _MAX_TEXT)
        if not isinstance(self.permissions, frozenset):
            raise InvalidInput("permissions must be a frozenset")
        for permission in sorted(self.permissions):
            if permission not in GOVERNANCE_PERMISSIONS:
                raise InvalidInput(
                    "permission must be one of "
                    + ", ".join(sorted(GOVERNANCE_PERMISSIONS))
                )


@dataclass(frozen=True)
class DecisionRow:
    """One append-only review decision, decoded strictly."""

    namespace: str
    decision_id: str
    decision_seq: int
    candidate_id: str
    artifact_version: int
    interaction_id: str | None
    decision: str
    reason_code: str
    impact: str
    derived_status: str
    support_basis: str
    requires_human: bool
    evidence_linkage: str
    policy_id: str
    policy_version: str
    impact_policy_id: str
    impact_policy_version: str
    authority_kind: str
    authorization_id: str | None
    directive_digest: str | None
    approval_id: str | None
    change_digest: str
    change_fingerprint: str | None
    target_refs: Any
    evidence_refs: Any
    scope: Any
    verification_result: str
    approval_scope: Any
    targets_expected: Any
    approval_expires_at: str | None
    decision_request_id: str
    commit_request_id: str | None
    review_after: str | None
    decided_at: str


@dataclass(frozen=True)
class AuthorizationRow:
    """One bounded auto-commit authorization."""

    namespace: str
    authorization_id: str
    host_instance: str
    access_ref: str
    operations: tuple[str, ...]
    claim_types: tuple[str, ...]
    scope: dict[str, Any]
    max_impact: str
    max_changes_per_bundle: int
    max_targets_per_bundle: int
    policy_id: str
    policy_version: str
    authorization_source: str
    issued_at: str
    expires_at: str | None
    revoked_at: str | None
    revoke_reason: str | None
    issued_by: str


@dataclass(frozen=True)
class SubjectAuthorityRow:
    """One "this subject may decide for this project" registration."""

    namespace: str
    project_id: str
    subject_ref: str
    basis: str
    granted_by: str
    granted_at: str
    expires_at: str | None
    revoked_at: str | None
    revoke_reason: str | None


# --------------------------------------------------------------------------
# validators
# --------------------------------------------------------------------------


def _require_text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidInput(f"{field} must be a non-empty string")
    if len(value) > maximum:
        raise InvalidInput(f"{field} must be at most {maximum} characters")
    return value


def _require_timestamp(value: Any, field: str) -> str:
    return identifiers.require_recorded_at(value)


def _require_now(value: Any) -> str:
    return _require_timestamp(value, "now")


def _require_decision_id(value: Any) -> str:
    return identifiers.require_record_id(value)


def _require_fingerprint(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise InvalidInput("fingerprint must be 64 lower-case hex characters")
    return value


def _decode_json(raw: Any, field: str) -> Any:
    if raw is None:
        return None
    try:
        return strict_json_loads(raw)
    except ValueError as exc:
        raise CorruptStore(f"stored {field} is not strict JSON") from exc


def _text_tuple(value: Any, field: str, maximum: int) -> tuple[str, ...]:
    """Validate one write-side text list destined for a JSON column."""

    if isinstance(value, (str, bytes)) or not isinstance(value, tuple):
        raise InvalidInput(f"{field} must be a tuple of strings")
    if not value or len(value) > maximum:
        raise InvalidInput(f"{field} must have 1..{maximum} items")
    out: list[str] = []
    for index, item in enumerate(value):
        out.append(_require_text(item, f"{field}[{index}]", 128))
    return tuple(out)


def _decode_text_tuple(raw: Any, field: str) -> tuple[str, ...]:
    value = _decode_json(raw, field)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CorruptStore(f"stored {field} must be an array of strings")
    return tuple(value)


def _decode_scope(raw: Any, field: str) -> dict[str, Any]:
    value = _decode_json(raw, field)
    if not isinstance(value, dict):
        raise CorruptStore(f"stored {field} must be a JSON object")
    return value


# --------------------------------------------------------------------------
# the access layer
# --------------------------------------------------------------------------


class GovernanceStore:
    """Read/write accessors for the J06 governance tables.

    Construct it around an already-open store::

        governance = GovernanceStore(store)

    Unrecognised attribute access is delegated to the wrapped store, so callers
    can keep using ``.connection`` / ``.transaction()`` / ``.commit_command``
    through the wrapper without the wrapper exposing a second write path.
    """

    def __init__(
        self,
        store: Any,
        *,
        clock: Callable[[], str] | None = None,
    ) -> None:
        if not hasattr(store, "connection"):
            raise InvalidInput("store must be an open PCS store")
        if clock is not None and not callable(clock):
            raise InvalidInput("clock must be callable")
        self._store = store
        self._clock = clock if clock is not None else utc_now_rfc3339

    # ----------------------------------------------------------- delegation

    @property
    def store(self) -> Any:
        return self._store

    def __getattr__(self, name: str) -> Any:
        # ``object.__getattribute__`` avoids recursing through ``__getattr__``
        # while ``_store`` itself is being set up.
        return getattr(object.__getattribute__(self, "_store"), name)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"GovernanceStore({self._store!r})"

    # ------------------------------------------------------------- helpers

    def _now(self, now: str | None) -> str:
        return self._clock() if now is None else _require_now(now)

    def _require_admin(self, access: GovernanceAccess) -> None:
        if not isinstance(access, GovernanceAccess):
            raise InvalidInput("access must be a GovernanceAccess")
        if "governance.admin" not in access.permissions:
            raise ScopeViolation(
                "governance.admin is required for this operation"
            )

    def _require_read(self, access: GovernanceAccess) -> None:
        if not isinstance(access, GovernanceAccess):
            raise InvalidInput("access must be a GovernanceAccess")
        if not access.permissions:
            raise ScopeViolation("governance.read is required for this operation")

    # ------------------------------------------------------ decision cursor

    def next_decision_seq(self, namespace: str) -> int:
        """Return ``MAX(decision_seq) + 1`` for one namespace.

        Must run inside the writing transaction: two concurrent writers that
        computed the value outside a ``BEGIN IMMEDIATE`` block would collide on
        the ``UNIQUE (namespace, decision_seq)`` index.  Calling it outside a
        transaction is a programming error, not a runtime condition.
        """

        ns = identifiers.require_namespace(namespace)
        if getattr(self._store, "_tx_depth", 0) <= 0:
            raise InvalidInput(
                "next_decision_seq must run inside the writing transaction"
            )
        row = self._store.connection.execute(
            "SELECT COALESCE(MAX(decision_seq), 0) + 1 "
            "FROM cognition_review_decisions WHERE namespace = ?",
            (ns,),
        ).fetchone()
        return int(row[0])

    # ------------------------------------------------------ decision reads

    def _decode_decision(self, row: Any) -> DecisionRow:
        keys = set(row.keys())
        missing = [name for name in _DECISION_COLUMNS if name not in keys]
        if missing:
            raise CorruptStore(
                "stored review decision is missing column(s): "
                + ", ".join(missing)
            )
        values: dict[str, Any] = {name: row[name] for name in _DECISION_COLUMNS}
        for column in _DECISION_JSON_COLUMNS:
            values[column] = _decode_json(values[column], column)
        values["requires_human"] = bool(values["requires_human"])
        values["target_refs"] = values.pop("target_refs_json")
        values["evidence_refs"] = values.pop("evidence_refs_json")
        values["scope"] = values.pop("scope_json")
        values["approval_scope"] = values.pop("approval_scope_json")
        values["targets_expected"] = values.pop("targets_expected_json")
        return DecisionRow(**values)

    def list_decisions(
        self,
        namespace: str,
        *,
        after_seq: int = 0,
        limit: int = 50,
    ) -> tuple[DecisionRow, ...]:
        """Read decisions in ``decision_seq`` order.

        ``after_seq`` is the read cursor and is deliberately independent of
        ``state_revision``: a decision-only command is a legal no-op that does
        not move the cognitive watermark, and it must still be observable.
        """

        ns = identifiers.require_namespace(namespace)
        if type(after_seq) is not int or after_seq < 0:
            raise InvalidInput("after_seq must be a non-negative integer")
        if type(limit) is not int or not 1 <= limit <= _MAX_LIMIT:
            raise InvalidInput(f"limit must be between 1 and {_MAX_LIMIT}")
        rows = self._store.connection.execute(
            "SELECT * FROM cognition_review_decisions "
            "WHERE namespace = ? AND decision_seq > ? "
            "ORDER BY decision_seq LIMIT ?",
            (ns, after_seq, limit),
        ).fetchall()
        return tuple(self._decode_decision(row) for row in rows)

    def decisions_for_candidate(
        self,
        namespace: str,
        candidate_id: str,
        artifact_version: int | None = None,
    ) -> tuple[DecisionRow, ...]:
        """Every decision recorded for one candidate, oldest first."""

        ns = identifiers.require_namespace(namespace)
        identifier = identifiers.require_record_id(candidate_id)
        sql = (
            "SELECT * FROM cognition_review_decisions "
            "WHERE namespace = ? AND candidate_id = ?"
        )
        params: list[Any] = [ns, identifier]
        if artifact_version is not None:
            if type(artifact_version) is not int or artifact_version < 1:
                raise InvalidInput("artifact_version must be a positive integer")
            sql += " AND artifact_version = ?"
            params.append(artifact_version)
        sql += " ORDER BY decision_seq"
        rows = self._store.connection.execute(sql, tuple(params)).fetchall()
        return tuple(self._decode_decision(row) for row in rows)

    def latest_decision(
        self,
        namespace: str,
        candidate_id: str,
        artifact_version: int,
    ) -> DecisionRow | None:
        rows = self.decisions_for_candidate(
            namespace, candidate_id, artifact_version
        )
        return rows[-1] if rows else None

    def rejected_fingerprints(
        self,
        namespace: str,
        *,
        since: str,
        fingerprints: Sequence[str],
    ) -> frozenset[str]:
        """Fingerprints among ``fingerprints`` that were *rejected* at/after ``since``.

        This is the read side of the rejection guard: only a ``rejected``
        terminal row counts, so a candidate that was merely deferred or is still
        awaiting a human does not poison later submissions.  The lookup is a
        single indexed point query per fingerprint on
        ``idx_decisions_fingerprint`` (namespace, change_fingerprint,
        decided_at); it never scans the table.
        """

        ns = identifiers.require_namespace(namespace)
        boundary = _require_timestamp(since, "since")
        if isinstance(fingerprints, (str, bytes)) or not isinstance(
            fingerprints, Sequence
        ):
            raise InvalidInput("fingerprints must be a sequence of hex digests")
        if len(fingerprints) > _MAX_FINGERPRINTS:
            raise InvalidInput(
                f"at most {_MAX_FINGERPRINTS} fingerprints may be probed at once"
            )
        wanted = [_require_fingerprint(item) for item in fingerprints]
        if not wanted:
            return frozenset()
        placeholders = ", ".join("?" for _ in wanted)
        rows = self._store.connection.execute(
            "SELECT DISTINCT change_fingerprint FROM cognition_review_decisions "
            "WHERE namespace = ? AND decision = 'rejected' "
            f"AND change_fingerprint IN ({placeholders}) "
            "AND decided_at >= ?",
            (ns, *wanted, boundary),
        ).fetchall()
        return frozenset(row["change_fingerprint"] for row in rows)

    def _decision_by_id(self, namespace: str, decision_id: str) -> DecisionRow | None:
        ns = identifiers.require_namespace(namespace)
        identifier = _require_decision_id(decision_id)
        row = self._store.connection.execute(
            "SELECT * FROM cognition_review_decisions "
            "WHERE namespace = ? AND decision_id = ?",
            (ns, identifier),
        ).fetchone()
        return None if row is None else self._decode_decision(row)

    # ----------------------------------------------------- authorizations

    def _decode_authorization(self, row: Any) -> AuthorizationRow:
        return AuthorizationRow(
            namespace=row["namespace"],
            authorization_id=row["authorization_id"],
            host_instance=row["host_instance"],
            access_ref=row["access_ref"],
            operations=_decode_text_tuple(row["operations_json"], "operations_json"),
            claim_types=_decode_text_tuple(row["claim_types_json"], "claim_types_json"),
            scope=_decode_scope(row["scope_json"], "scope_json"),
            max_impact=row["max_impact"],
            max_changes_per_bundle=int(row["max_changes_per_bundle"]),
            max_targets_per_bundle=int(row["max_targets_per_bundle"]),
            policy_id=row["policy_id"],
            policy_version=row["policy_version"],
            authorization_source=row["authorization_source"],
            issued_at=row["issued_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
            revoke_reason=row["revoke_reason"],
            issued_by=row["issued_by"],
        )

    def load_commit_authorization(
        self, *, namespace: str, authorization_id: str
    ) -> AuthorizationRow | None:
        ns = identifiers.require_namespace(namespace)
        identifier = identifiers.require_record_id(authorization_id)
        row = self._store.connection.execute(
            "SELECT * FROM cognition_commit_authorizations "
            "WHERE namespace = ? AND authorization_id = ?",
            (ns, identifier),
        ).fetchone()
        return None if row is None else self._decode_authorization(row)

    def authorization_is_active(self, row: AuthorizationRow, now: str) -> bool:
        """Whether an authorization may still be used at ``now``."""

        moment = _require_now(now)
        if row.revoked_at is not None:
            return False
        if row.expires_at is not None and row.expires_at <= moment:
            return False
        return True

    def grant_commit_authorization(
        self,
        access: GovernanceAccess,
        *,
        authorization_id: str,
        host_instance: str,
        access_ref: str,
        operations: tuple[str, ...],
        claim_types: tuple[str, ...],
        scope: Mapping[str, Any],
        max_impact: str,
        max_changes_per_bundle: int,
        max_targets_per_bundle: int,
        policy_id: str,
        policy_version: str,
        authorization_source: str,
        issued_at: str,
        expires_at: str | None = None,
        issued_by: str | None = None,
    ) -> AuthorizationRow:
        """Seed one bounded auto-commit authorization.

        This is the only supported way to populate
        ``cognition_commit_authorizations``.  The row is what the commit guard
        re-reads inside the write transaction, so a revoked or expired
        authorization stops the commit even when the caller still holds its id.
        """

        self._require_admin(access)
        identifier = identifiers.require_record_id(authorization_id)
        host = _require_text(host_instance, "host_instance", 256)
        ref = _require_text(access_ref, "access_ref", 512)
        ops = _text_tuple(operations, "operations", 16)
        claims = _text_tuple(claim_types, "claim_types", 32)
        if max_impact not in ("E0", "E1"):
            raise InvalidInput("max_impact must be E0 or E1")
        if type(max_changes_per_bundle) is not int or not 1 <= max_changes_per_bundle <= 4:
            raise InvalidInput("max_changes_per_bundle must be 1..4")
        if type(max_targets_per_bundle) is not int or not 1 <= max_targets_per_bundle <= 4:
            raise InvalidInput("max_targets_per_bundle must be 1..4")
        if authorization_source not in ("host_config", "user_confirmed"):
            raise InvalidInput("authorization_source is not a frozen value")
        issued = _require_timestamp(issued_at, "issued_at")
        expires = (
            None if expires_at is None else _require_timestamp(expires_at, "expires_at")
        )
        payload = canonical_json_text(dict(scope))
        ns = access.namespace
        with self._store.transaction():
            self._store.connection.execute(
                "INSERT INTO cognition_commit_authorizations ("
                "namespace, authorization_id, host_instance, access_ref, "
                "operations_json, claim_types_json, scope_json, max_impact, "
                "max_changes_per_bundle, max_targets_per_bundle, policy_id, "
                "policy_version, authorization_source, issued_at, expires_at, "
                "revoked_at, revoke_reason, issued_by"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?) "
                "ON CONFLICT (namespace, authorization_id) DO UPDATE SET "
                "host_instance = excluded.host_instance, "
                "access_ref = excluded.access_ref, "
                "operations_json = excluded.operations_json, "
                "claim_types_json = excluded.claim_types_json, "
                "scope_json = excluded.scope_json, "
                "max_impact = excluded.max_impact, "
                "max_changes_per_bundle = excluded.max_changes_per_bundle, "
                "max_targets_per_bundle = excluded.max_targets_per_bundle, "
                "policy_id = excluded.policy_id, "
                "policy_version = excluded.policy_version, "
                "authorization_source = excluded.authorization_source, "
                "issued_at = excluded.issued_at, expires_at = excluded.expires_at, "
                "revoked_at = NULL, revoke_reason = NULL, "
                "issued_by = excluded.issued_by",
                (
                    ns, identifier, host, ref,
                    canonical_json_text(list(ops)),
                    canonical_json_text(list(claims)),
                    payload, max_impact, max_changes_per_bundle,
                    max_targets_per_bundle, policy_id, policy_version,
                    authorization_source, issued, expires,
                    access.operator if issued_by is None else issued_by,
                ),
            )
        loaded = self.load_commit_authorization(
            namespace=ns, authorization_id=identifier
        )
        if loaded is None:  # pragma: no cover - the insert above just ran
            raise CorruptStore("commit authorization disappeared after grant")
        return loaded

    def revoke_commit_authorization(
        self,
        access: GovernanceAccess,
        *,
        authorization_id: str,
        reason: str,
        now: str | None = None,
    ) -> AuthorizationRow:
        """Revoke one authorization; the next guard read refuses it."""

        self._require_admin(access)
        identifier = identifiers.require_record_id(authorization_id)
        text = _require_text(reason, "reason", 256)
        moment = self._now(now)
        with self._store.transaction():
            cursor = self._store.connection.execute(
                "UPDATE cognition_commit_authorizations SET revoked_at = ?, "
                "revoke_reason = ? WHERE namespace = ? AND authorization_id = ? "
                "AND revoked_at IS NULL",
                (moment, text, access.namespace, identifier),
            )
            if cursor.rowcount == 0:
                existing = self.load_commit_authorization(
                    namespace=access.namespace, authorization_id=identifier
                )
                if existing is None:
                    raise RecordNotFound(
                        f"commit authorization {identifier} does not exist"
                    )
        loaded = self.load_commit_authorization(
            namespace=access.namespace, authorization_id=identifier
        )
        if loaded is None:  # pragma: no cover
            raise CorruptStore("commit authorization disappeared after revoke")
        return loaded

    def grant_subject_authority(
        self,
        access: GovernanceAccess,
        *,
        project_id: str,
        subject_ref: str,
        basis: str,
        expires_at: str | None = None,
        now: str | None = None,
    ) -> SubjectAuthorityRow:
        """Seed (or re-seed) one project decision authority.

        This is the only supported way to populate
        ``cognition_subject_authority``: without it the table would be
        unusable outside a test that writes SQL directly, and the
        ``project_decision`` claim type would depend on hand-edited data.
        """

        self._require_admin(access)
        project = _require_text(project_id, "project_id", _MAX_IDENTIFIER)
        subject = _require_text(subject_ref, "subject_ref", _MAX_IDENTIFIER)
        if basis not in SUBJECT_AUTHORITY_BASES:
            raise InvalidInput(
                "basis must be one of " + ", ".join(SUBJECT_AUTHORITY_BASES)
            )
        expires = None if expires_at is None else _require_timestamp(expires_at, "expires_at")
        moment = self._now(now)
        ns = access.namespace
        with self._store.transaction():
            self._store.connection.execute(
                "INSERT INTO cognition_subject_authority ("
                "namespace, project_id, subject_ref, basis, granted_by, "
                "granted_at, expires_at, revoked_at, revoke_reason"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL) "
                "ON CONFLICT (namespace, project_id, subject_ref) DO UPDATE SET "
                "basis = excluded.basis, granted_by = excluded.granted_by, "
                "granted_at = excluded.granted_at, expires_at = excluded.expires_at, "
                "revoked_at = NULL, revoke_reason = NULL",
                (ns, project, subject, basis, access.operator, moment, expires),
            )
        loaded = self.load_subject_authority(
            namespace=ns, project_id=project, subject_ref=subject
        )
        if loaded is None:  # pragma: no cover - the insert above just ran
            raise CorruptStore("subject authority row disappeared after grant")
        return loaded

    def revoke_subject_authority(
        self,
        access: GovernanceAccess,
        *,
        project_id: str,
        subject_ref: str,
        reason: str,
        now: str | None = None,
    ) -> SubjectAuthorityRow:
        """Revoke one project decision authority, effective immediately.

        Revoking twice is idempotent: the first revocation time and reason are
        kept, because rewriting them would falsify the audit trail.
        """

        self._require_admin(access)
        project = _require_text(project_id, "project_id", _MAX_IDENTIFIER)
        subject = _require_text(subject_ref, "subject_ref", _MAX_IDENTIFIER)
        why = _require_text(reason, "reason", _MAX_TEXT)
        moment = self._now(now)
        ns = access.namespace
        with self._store.transaction():
            row = self._store.connection.execute(
                "SELECT revoked_at FROM cognition_subject_authority "
                "WHERE namespace = ? AND project_id = ? AND subject_ref = ?",
                (ns, project, subject),
            ).fetchone()
            if row is None:
                raise RecordNotFound(
                    f"no subject authority for project {project!r} subject {subject!r}"
                )
            if row["revoked_at"] is None:
                self._store.connection.execute(
                    "UPDATE cognition_subject_authority SET revoked_at = ?, "
                    "revoke_reason = ? WHERE namespace = ? AND project_id = ? "
                    "AND subject_ref = ?",
                    (moment, why, ns, project, subject),
                )
        loaded = self.load_subject_authority(
            namespace=ns, project_id=project, subject_ref=subject
        )
        if loaded is None:  # pragma: no cover - the update above just ran
            raise CorruptStore("subject authority row disappeared after revoke")
        return loaded

    def load_subject_authority(
        self, *, namespace: str, project_id: str, subject_ref: str
    ) -> SubjectAuthorityRow | None:
        ns = identifiers.require_namespace(namespace)
        project = _require_text(project_id, "project_id", _MAX_IDENTIFIER)
        subject = _require_text(subject_ref, "subject_ref", _MAX_IDENTIFIER)
        row = self._store.connection.execute(
            "SELECT * FROM cognition_subject_authority "
            "WHERE namespace = ? AND project_id = ? AND subject_ref = ?",
            (ns, project, subject),
        ).fetchone()
        if row is None:
            return None
        return SubjectAuthorityRow(
            namespace=row["namespace"],
            project_id=row["project_id"],
            subject_ref=row["subject_ref"],
            basis=row["basis"],
            granted_by=row["granted_by"],
            granted_at=row["granted_at"],
            expires_at=row["expires_at"],
            revoked_at=row["revoked_at"],
            revoke_reason=row["revoke_reason"],
        )

    def subject_authority_is_active(
        self, row: SubjectAuthorityRow, now: str
    ) -> bool:
        moment = _require_now(now)
        if row.revoked_at is not None:
            return False
        if row.expires_at is not None and row.expires_at <= moment:
            return False
        return True

    # ----------------------------------------------------------- provenance

    def decision_for_record(
        self, namespace: str, record_id: str, record_version: int
    ) -> DecisionRow | None:
        """Which decision committed this exact record version?

        Uses the ``cognition_record_decisions`` primary key, so this is a point
        lookup and never a scan.
        """

        ns = identifiers.require_namespace(namespace)
        identifier = identifiers.require_record_id(record_id)
        identifiers.require_record_version(record_version)
        row = self._store.connection.execute(
            "SELECT decision_id FROM cognition_record_decisions "
            "WHERE namespace = ? AND record_id = ? AND record_version = ?",
            (ns, identifier, record_version),
        ).fetchone()
        if row is None:
            return None
        decision = self._decision_by_id(ns, row["decision_id"])
        if decision is None:
            raise CorruptStore(
                f"provenance row for {identifier} v{record_version} points at "
                "a missing decision"
            )
        return decision

    # ------------------------------------------------- change candidate state

    def lifecycle(self, namespace: str, candidate_id: str) -> str:
        """Derive one change candidate's authoritative lifecycle.

        Only two durable facts decide it: a Tx B receipt for the candidate's
        command (``committed``) and a ``rejected`` terminal decision row
        (``rejected``).  Every other decision row is an *attempt* and never
        moves this value -- a loser's ``conflict`` row appended after a commit
        must not read the candidate back to ``pending``.

        A candidate that is committed *and* rejected is contradictory stored
        state, not a preference: this fails closed with ``CorruptStore``.
        """

        ns = identifiers.require_namespace(namespace)
        candidate = self._load_candidate_row(ns, candidate_id)
        return self._lifecycle_of(ns, candidate)

    def require_current_change_candidate(
        self, namespace: str, candidate_id: str
    ) -> "ArtifactRow":
        """Return a change candidate that may still be reviewed or committed.

        This is the Work-less counterpart of C06a's
        ``require_current_candidate``: it proves the artifact is a
        ``cognition.change`` candidate and that its lifecycle is ``pending``.
        It deliberately does **not** look at ``work_id`` -- how a candidate is
        created (with or without a work anchor) is a creation policy, not a
        currentness fact.
        """

        ns = identifiers.require_namespace(namespace)
        candidate = self._load_candidate_row(ns, candidate_id)
        proposal = candidate.proposal or {}
        operation = proposal.get("operation")
        if operation != CHANGE_OPERATION:
            raise CandidateTransitionDenied(
                f"artifact {candidate_id} is a candidate for {operation!r}, "
                f"not {CHANGE_OPERATION!r}"
            )
        state = self._lifecycle_of(ns, candidate)
        if state != "pending":
            raise CandidateTransitionDenied(
                f"change candidate {candidate_id} is {state!r}; only a pending "
                "candidate can be reviewed or committed"
            )
        return candidate

    def _load_candidate_row(self, namespace: str, candidate_id: str) -> "ArtifactRow":
        if not isinstance(candidate_id, str) or len(candidate_id) != 36:
            raise InvalidInput("candidate_id must be a canonical UUID string")
        candidate = self._store.load_artifact(
            namespace=namespace, artifact_id=candidate_id, artifact_version=1
        )
        if candidate.kind != "candidate":
            raise RecordKindMismatch(
                f"artifact {candidate_id} is kind {candidate.kind!r}, not a candidate"
            )
        if candidate.request_id is None:
            raise CorruptStore(
                f"candidate {candidate_id} has no request id; its commit command "
                "can never be identified"
            )
        return candidate

    def _lifecycle_of(self, namespace: str, candidate: "ArtifactRow") -> str:
        receipt = self._store.lookup_command_receipt(
            namespace=namespace, request_id=candidate.request_id
        )
        committed = receipt is not None and bool(receipt.record_refs)
        rejected = any(
            decision.decision == "rejected"
            for decision in self.decisions_for_candidate(
                namespace, candidate.artifact_id
            )
        )
        if committed and rejected:
            raise CorruptStore(
                f"candidate {candidate.artifact_id} has both a commit receipt and "
                "a rejected decision; the stored history contradicts itself"
            )
        if committed:
            return "committed"
        if rejected:
            return "rejected"
        return "pending"


__all__ = [
    "CHANGE_OPERATION",
    "DECISION_KINDS",
    "GOVERNANCE_PERMISSIONS",
    "GovernanceAccess",
    "GovernanceStore",
    "IMPACT_LEVELS",
    "LIFECYCLE_STATES",
    "AuthorizationRow",
    "DecisionRow",
    "SubjectAuthorityRow",
]
