"""The PCS2-004 shared version store.

Shape (appendix J.3):

* ``records`` is only the current pointer: ``(namespace, record_id)`` and the
  latest published version;
* ``record_versions`` holds one immutable row per ``(record_id,
  record_version)``; a second write for the same key fails on the primary key
  instead of overwriting;
* ``private_payloads`` holds the canonical body text and its SHA-256;
  ``record_versions`` points at it through ``body_payload_ref``;
* ``namespace_state`` holds the monotonically increasing ``state_revision``.
  This card only implements "reserve the next revision"; PCS2-006 owns the
  concurrency rules.

Two read paths exist on purpose and must stay separate:

``get_version(namespace, record_id, record_version)``
    Exact historical read. It queries ``record_versions`` directly and never
    consults ``records.current_version``.

``get_current(namespace, record_id)``
    Follows the current pointer. This is the only place that does.

All writes run inside ``transaction()``; the outermost block commits or rolls
back. Inside one transaction every store operation shares the same reserved
``state_revision`` (J.7: one transaction, one revision).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Union

from ..foundation import identifiers
from ..foundation.errors import (
    CorruptStore,
    DbBusy,
    InvalidInput,
    MigrationRequired,
    RecordKindMismatch,
    RecordNotFound,
    RecordVersionConflict,
    RecordVersionExists,
    StandaloneTransactionRequired,
)
from ..foundation.jsonutil import (
    PAYLOAD_REF_PREFIX,
    canonical_json_text,
    new_payload_ref,
    sha256_hex,
    strict_json_loads,
)
from ..foundation.migrations import (
    apply_migrations as _apply_migrations,
    default_migrations_dir,
    inspect_migrations,
)
from ..foundation.sqlite_db import connect
from ..foundation.timeutil import utc_now_rfc3339

_VERSION_COLUMNS = (
    "namespace, record_id, record_version, kind, schema_version, status, "
    "privacy_class, recorded_at, committed_state_revision, body_payload_ref, "
    "scope_json"
)

#: Upper bound of the canonical ``scope_json`` text (0004 CHECK).  A scope is a
#: retrieval hint, not a container: rejecting an absurd one is better than
#: storing it and scanning it later.
MAX_SCOPE_JSON_LENGTH = 16384


@dataclass(frozen=True)
class RecordVersion:
    """One stored, immutable record version as read back from the store."""

    namespace: str
    record_id: str
    record_version: int
    kind: str
    schema_version: str
    status: str
    privacy_class: str
    recorded_at: str
    committed_state_revision: int
    body_payload_ref: str
    body_sha256: str
    body: dict[str, Any]
    #: J.2 retrieval scope; ``None`` means namespace-general.  Rows written
    #: before PCS2-007 always read back as ``None``.
    scope: dict[str, Any] | None = None


class RecordStore:
    """SQLite-backed record version store for one database file."""

    def __init__(self, connection: sqlite3.Connection, migrations_dir: Path):
        self._connection: sqlite3.Connection | None = connection
        self._migrations_dir = migrations_dir
        self._tx_depth = 0
        # namespace -> revision reserved by the current transaction
        self._tx_revisions: dict[str, int] = {}

    # ------------------------------------------------------------------ open

    @classmethod
    def open(
        cls,
        db_path: Union[str, Path],
        *,
        migrations_dir: Union[str, Path, None] = None,
        apply_migrations: bool = True,
    ) -> "RecordStore":
        """Open (and by default migrate) a PCS database file."""

        directory = Path(migrations_dir) if migrations_dir is not None else default_migrations_dir()
        connection = connect(db_path)
        try:
            if apply_migrations:
                _apply_migrations(connection, directory)
            else:
                plan = inspect_migrations(connection, directory)
                if plan.pending:
                    versions = ", ".join(f"{m.version:04d}" for m in plan.pending)
                    raise MigrationRequired(f"database has pending migrations: {versions}")
                if not plan.applied:
                    raise MigrationRequired("database has no applied migrations")
        except BaseException:
            connection.close()
            raise
        return cls(connection, directory)

    # ------------------------------------------------------------ lifecycle

    @property
    def connection(self) -> sqlite3.Connection:
        """Low-level handle for diagnostics and constraint tests.

        Business writes must go through the store methods; the raw handle is
        exposed because the FK/immutability proofs are direct SQL statements.
        """

        return self._require_connection()

    @property
    def migrations_dir(self) -> Path:
        return self._migrations_dir

    def close(self) -> None:
        if self._connection is None:
            return
        try:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
        finally:
            self._connection.close()
            self._connection = None
            self._tx_depth = 0
            self._tx_revisions.clear()

    def __enter__(self) -> "RecordStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("stores cannot be used after close()")
        return self._connection

    # ---------------------------------------------------------- transactions

    @contextmanager
    def transaction(self) -> Iterator["RecordStore"]:
        """Run a unit of work; joins the enclosing transaction if there is one.

        The outermost block owns BEGIN/COMMIT/ROLLBACK. Nested use does not
        create savepoints: an exception still aborts the whole transaction.
        A lock that cannot be acquired is reported as :class:`DbBusy`
        (``DB_BUSY``), never as a bare ``sqlite3.OperationalError``.
        """

        connection = self._require_connection()
        outermost = self._tx_depth == 0
        if outermost:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                if _is_lock_error(exc):
                    raise DbBusy(
                        "the store write lock is held by another connection or process"
                    ) from exc
                raise
        self._tx_depth += 1
        try:
            yield self
        except BaseException:
            self._tx_depth -= 1
            if outermost:
                try:
                    connection.execute("ROLLBACK")
                finally:
                    self._tx_revisions.clear()
            raise
        else:
            self._tx_depth -= 1
            if outermost:
                try:
                    connection.execute("COMMIT")
                except sqlite3.OperationalError as exc:
                    try:
                        connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    finally:
                        self._tx_revisions.clear()
                    if _is_lock_error(exc):
                        raise DbBusy(
                            "the store write lock could not be released for commit"
                        ) from exc
                    raise
                except BaseException:
                    try:
                        connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    finally:
                        self._tx_revisions.clear()
                    raise
                else:
                    self._tx_revisions.clear()

    def _require_standalone(self, operation: str) -> None:
        """Refuse to join a caller's transaction (PCS2-006 control plane)."""

        if self._tx_depth != 0:
            raise StandaloneTransactionRequired(
                f"{operation} owns its transaction and cannot join an open one"
            )

    # -------------------------------------------------------- state revision

    def current_state_revision(self, namespace: str) -> int:
        """Current committed ``state_revision`` (0 for an unused namespace)."""

        identifiers.require_namespace(namespace)
        row = self._require_connection().execute(
            "SELECT state_revision FROM namespace_state WHERE namespace = ?", (namespace,)
        ).fetchone()
        return 0 if row is None else int(row["state_revision"])

    def next_state_revision(self, namespace: str) -> int:
        """Reserve the next revision; repeated calls in one transaction share it."""

        identifiers.require_namespace(namespace)
        with self.transaction():
            return self._allocate_state_revision_locked(namespace)

    def _allocate_state_revision_locked(self, namespace: str) -> int:
        if self._tx_depth == 0:  # pragma: no cover - internal invariant
            raise RuntimeError("state revisions may only be reserved inside a transaction")
        reserved = self._tx_revisions.get(namespace)
        if reserved is not None:
            return reserved
        row = self._require_connection().execute(
            "UPDATE namespace_state SET state_revision = state_revision + 1 "
            "WHERE namespace = ? RETURNING state_revision",
            (namespace,),
        ).fetchone()
        if row is None:
            self._require_connection().execute(
                "INSERT INTO namespace_state (namespace, state_revision) VALUES (?, 1)",
                (namespace,),
            )
            value = 1
        else:
            value = int(row[0])
        self._tx_revisions[namespace] = value
        return value

    # ---------------------------------------------------------------- writes

    def append_record_version(
        self,
        *,
        namespace: str,
        record_id: str,
        record_version: int,
        kind: str,
        body: dict[str, Any],
        status: str = "active",
        schema_version: str = "1",
        privacy_class: str = "private",
        recorded_at: Union[str, None] = None,
        scope: Union[Mapping[str, Any], None] = None,
    ) -> RecordVersion:
        """Publish one immutable record version and advance the current pointer.

        A new record must start at version 1. For an existing record the only
        writable versions are the current one (a re-write is rejected; the
        primary key is the final backstop) and ``current_version + 1``.

        ``scope`` is the optional J.2 retrieval scope (PCS2-007).  ``None`` or
        the empty object is stored as NULL, meaning namespace-general.
        """

        with self.transaction():
            return self._append_record_version_locked(
                namespace=namespace,
                record_id=record_id,
                record_version=record_version,
                kind=kind,
                body=body,
                status=status,
                schema_version=schema_version,
                privacy_class=privacy_class,
                recorded_at=recorded_at,
                scope=scope,
            )

    def _append_record_version_locked(
        self,
        *,
        namespace: str,
        record_id: str,
        record_version: int,
        kind: str,
        body: dict[str, Any],
        status: str,
        schema_version: str,
        privacy_class: str,
        recorded_at: Union[str, None],
        scope: Union[Mapping[str, Any], None] = None,
    ) -> RecordVersion:
        identifiers.require_namespace(namespace)
        identifiers.require_record_id(record_id)
        identifiers.require_record_version(record_version)
        identifiers.require_kind(kind)
        identifiers.require_schema_version(schema_version)
        identifiers.require_status(status)
        identifiers.require_privacy_class(privacy_class)
        if recorded_at is None:
            recorded_at = utc_now_rfc3339()
        else:
            identifiers.require_recorded_at(recorded_at)
        if not isinstance(body, dict):
            raise InvalidInput("record body must be a JSON object")

        body_text = canonical_json_text(body)
        body_digest = sha256_hex(body_text)
        scope_text = _scope_json_text(scope)

        # PCS2-006 write barrier: a deleted record may not gain new versions.
        # The default hook is a no-op; the deletion layer overrides it.
        self._check_record_writable(namespace, record_id)

        connection = self._require_connection()
        record = connection.execute(
            "SELECT kind, current_version FROM records WHERE namespace = ? AND record_id = ?",
            (namespace, record_id),
        ).fetchone()

        if record is None:
            if record_version != 1:
                raise RecordNotFound(
                    f"record {record_id} does not exist in namespace {namespace}; "
                    "its first version must be 1"
                )
            current_version = 0
        else:
            if record["kind"] != kind:
                raise RecordKindMismatch(
                    f"record {record_id} is kind {record['kind']!r}; "
                    f"version {record_version} tried to use {kind!r}"
                )
            current_version = int(record["current_version"])
            if record_version < current_version:
                raise RecordVersionConflict(
                    f"record_version {record_version} is older than current version "
                    f"{current_version}; published versions are immutable"
                )
            if record_version > current_version + 1:
                raise RecordVersionConflict(
                    f"record_version {record_version} skips version {current_version + 1}"
                )
            if record_version == current_version:
                existing = connection.execute(
                    "SELECT 1 FROM record_versions "
                    "WHERE namespace = ? AND record_id = ? AND record_version = ?",
                    (namespace, record_id, record_version),
                ).fetchone()
                if existing is not None:
                    raise RecordVersionExists(
                        f"record version {record_version} of {record_id} already exists "
                        "and is immutable"
                    )

        body_ref = self._store_payload(body_text, body_digest)
        committed_state_revision = self._allocate_state_revision_locked(namespace)

        if record is None:
            connection.execute(
                "INSERT INTO records (namespace, record_id, kind, current_version) "
                "VALUES (?, ?, ?, ?)",
                (namespace, record_id, kind, 1),
            )

        try:
            connection.execute(
                "INSERT INTO record_versions ("
                "namespace, record_id, record_version, kind, schema_version, status, "
                "privacy_class, recorded_at, committed_state_revision, body_payload_ref, "
                "scope_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    namespace,
                    record_id,
                    record_version,
                    kind,
                    schema_version,
                    status,
                    privacy_class,
                    recorded_at,
                    committed_state_revision,
                    body_ref,
                    scope_text,
                ),
            )
        except sqlite3.IntegrityError as exc:
            message = str(exc)
            if "UNIQUE constraint failed" in message and "record_versions" in message:
                # Backstop for a race; the normal duplicate path is caught above.
                raise RecordVersionExists(
                    f"record version {record_version} of {record_id} already exists"
                ) from exc
            raise

        if record is not None and record_version == current_version + 1:
            cursor = connection.execute(
                "UPDATE records SET current_version = ? "
                "WHERE namespace = ? AND record_id = ? AND current_version = ?",
                (record_version, namespace, record_id, current_version),
            )
            if cursor.rowcount != 1:
                raise CorruptStore("current pointer changed unexpectedly while appending")

        return RecordVersion(
            namespace=namespace,
            record_id=record_id,
            record_version=record_version,
            kind=kind,
            schema_version=schema_version,
            status=status,
            privacy_class=privacy_class,
            recorded_at=recorded_at,
            committed_state_revision=committed_state_revision,
            body_payload_ref=body_ref,
            body_sha256=body_digest,
            body=body,
            scope=None if scope_text is None else dict(scope),
        )

    def _store_payload(self, body_text: str, digest: str) -> str:
        """Reuse a live payload row with the same bytes, or insert an opaque one.

        De-duplication is by the digest column (PCS2-006 B1), never by asking a
        caller to derive a reference from the content.  A cleared row keeps an
        empty body and an unrelated tombstone digest, so a later write of the
        same content creates a fresh row instead of reusing the tombstone.
        Pre-0003 ``sha256:`` rows are never reused either; they cannot give the
        deletion guarantee and are documented as rebuild-only.
        """

        connection = self._require_connection()
        row = connection.execute(
            "SELECT pp.payload_ref, pp.body_json FROM private_payloads pp "
            "WHERE pp.sha256 = ? AND pp.body_json <> '' "
            f"AND pp.payload_ref LIKE '{PAYLOAD_REF_PREFIX}%' "
            # PCS2-008 Context payloads are independent deletion units.  A
            # record/event body that happens to serialize exactly like one must
            # not adopt that row, or clearing one side would silently destroy
            # the other.
            "AND NOT EXISTS ("
            "  SELECT 1 FROM context_snapshots cs "
            "  WHERE cs.rendered_input_payload_ref = pp.payload_ref"
            ") "
            "ORDER BY pp.created_at, pp.payload_ref LIMIT 1",
            (digest,),
        ).fetchone()
        if row is not None:
            if row["body_json"] != body_text:
                raise CorruptStore(
                    f"payload {row['payload_ref']} shares a digest with different bytes"
                )
            return str(row["payload_ref"])

        payload_ref = new_payload_ref()
        connection.execute(
            "INSERT INTO private_payloads (payload_ref, sha256, body_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (payload_ref, digest, body_text, utc_now_rfc3339()),
        )
        return payload_ref

    def _check_record_writable(self, namespace: str, record_id: str) -> None:
        """PCS2-006 hook; the deletion layer refuses writes to deleted records."""

        return None

    # ----------------------------------------------------------------- reads

    def get_version(
        self, namespace: str, record_id: str, record_version: int
    ) -> RecordVersion:
        """Exact historical read; never consults ``records.current_version``."""

        identifiers.require_namespace(namespace)
        identifiers.require_record_id(record_id)
        identifiers.require_record_version(record_version)
        row = self._read_version_row(namespace, record_id, record_version)
        if row is None:
            raise RecordNotFound(
                f"record {record_id} has no version {record_version} in namespace {namespace}"
            )
        return self._version_from_row(row)

    def get_current(self, namespace: str, record_id: str) -> RecordVersion:
        """Follow the current pointer and return its exact version row."""

        identifiers.require_namespace(namespace)
        identifiers.require_record_id(record_id)
        connection = self._require_connection()
        pointer = connection.execute(
            "SELECT kind, current_version FROM records "
            "WHERE namespace = ? AND record_id = ?",
            (namespace, record_id),
        ).fetchone()
        if pointer is None:
            raise RecordNotFound(f"record {record_id} does not exist in namespace {namespace}")
        row = self._read_version_row(namespace, record_id, int(pointer["current_version"]))
        if row is None:
            raise CorruptStore(
                f"current pointer of {record_id} references missing version "
                f"{pointer['current_version']}"
            )
        if row["kind"] != pointer["kind"]:
            raise CorruptStore(f"record {record_id} kind disagrees with its current version")
        return self._version_from_row(row)

    def _read_version_row(
        self, namespace: str, record_id: str, record_version: int
    ) -> sqlite3.Row | None:
        return self._require_connection().execute(
            f"SELECT {_VERSION_COLUMNS} FROM record_versions "
            "WHERE namespace = ? AND record_id = ? AND record_version = ?",
            (namespace, record_id, record_version),
        ).fetchone()

    def _version_from_row(self, row: sqlite3.Row) -> RecordVersion:
        body_sha256, body = self._load_body(row["body_payload_ref"])
        return RecordVersion(
            namespace=row["namespace"],
            record_id=row["record_id"],
            record_version=int(row["record_version"]),
            kind=row["kind"],
            schema_version=row["schema_version"],
            status=row["status"],
            privacy_class=row["privacy_class"],
            recorded_at=row["recorded_at"],
            committed_state_revision=int(row["committed_state_revision"]),
            body_payload_ref=row["body_payload_ref"],
            body_sha256=body_sha256,
            body=body,
            scope=_scope_from_stored_json(row["scope_json"]),
        )

    def _load_body(self, payload_ref: str) -> tuple[str, dict[str, Any]]:
        digest, body, _text = self._load_payload(payload_ref, owner="record version")
        return digest, body

    def _load_payload(
        self, payload_ref: str, *, owner: str
    ) -> tuple[str, dict[str, Any], str]:
        """Read one controlled payload and verify its digest and shape.

        Shared with the PCS2-005 event store: both tables reference the same
        ``private_payloads`` area, so integrity is checked in exactly one place.
        ``owner`` only names the referrer in the error message.  The canonical
        text is returned as the third element for callers that need the stored
        bytes rather than the parsed object.

        Since PCS2-006 (B1) the reference is an opaque ID, so the check is
        "stored digest matches these bytes", not "the reference equals the
        digest".  A ``sha256:`` reference can only come from a database written
        before 0003; such rows are refused explicitly instead of being read
        with weaker deletion guarantees.
        """

        row = self._require_connection().execute(
            "SELECT sha256, body_json FROM private_payloads WHERE payload_ref = ?",
            (payload_ref,),
        ).fetchone()
        if row is None:
            raise CorruptStore(f"{owner} references missing payload {payload_ref}")
        if not payload_ref.startswith(PAYLOAD_REF_PREFIX):
            raise CorruptStore(
                f"{owner} uses the pre-0003 content-addressed payload reference "
                f"{payload_ref!r}; rebuild this database (opaque references are "
                "required for deletion to be effective)"
            )
        body_text = row["body_json"]
        stored_digest = row["sha256"]
        if body_text == "":
            raise CorruptStore(f"{owner} references a cleared payload {payload_ref}")
        actual_digest = sha256_hex(body_text)
        if stored_digest != actual_digest:
            raise CorruptStore(f"payload {payload_ref} sha256 does not match its bytes")
        try:
            body = strict_json_loads(body_text)
        except ValueError as exc:
            raise CorruptStore(f"payload {payload_ref} is not strict JSON") from exc
        if not isinstance(body, dict):
            raise CorruptStore(f"payload {payload_ref} is not a JSON object")
        try:
            if canonical_json_text(body) != body_text:
                raise CorruptStore(f"payload {payload_ref} is not canonical JSON")
        except InvalidInput as exc:
            raise CorruptStore(f"payload {payload_ref} is not canonical JSON") from exc
        return stored_digest, body, body_text


def _is_lock_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _scope_json_text(scope: Union[Mapping[str, Any], None]) -> str | None:
    """Canonical text for ``record_versions.scope_json`` (``None`` = general).

    Deep shape validation belongs to the domain layer (PCS2-007); this boundary
    only enforces what the column promises: an object with string keys, JSON
    values and a bounded canonical representation.
    """

    if scope is None:
        return None
    if not isinstance(scope, Mapping):
        raise InvalidInput("scope must be a JSON object or null")
    if not scope:
        return None
    for key in scope:
        if not isinstance(key, str):
            raise InvalidInput("scope keys must be strings")
    text = canonical_json_text(dict(scope))
    if len(text) > MAX_SCOPE_JSON_LENGTH:
        raise InvalidInput(
            f"scope must be at most {MAX_SCOPE_JSON_LENGTH} characters once serialized"
        )
    return text


def _scope_from_stored_json(text: str | None) -> dict[str, Any] | None:
    """Read ``scope_json`` defensively; corrupt text is never returned as scope."""

    if text is None:
        return None
    try:
        value = strict_json_loads(text)
    except ValueError as exc:
        raise CorruptStore("stored scope_json is not strict JSON") from exc
    if not isinstance(value, dict):
        raise CorruptStore("stored scope_json is not a JSON object")
    try:
        if canonical_json_text(value) != text:
            raise CorruptStore("stored scope_json is not canonical JSON")
    except InvalidInput as exc:
        raise CorruptStore("stored scope_json is not canonical JSON") from exc
    return value


__all__ = ["MAX_SCOPE_JSON_LENGTH", "RecordStore", "RecordVersion"]
