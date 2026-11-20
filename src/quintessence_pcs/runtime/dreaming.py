"""Instance-local reservations and proposals for untasked Dreaming.

The host owns admission, live PCS authorization and the model call. This
module only records the wake and its outcome in an instance-owned directory.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from typing import Any
from uuid import UUID, uuid4

from ..domains.refs import EventRef, RecordRef
from ..domains.scope import normalize_scope
from ..foundation import identifiers
from ..foundation.errors import CorruptStore, InvalidInput
from ..foundation.jsonutil import canonical_json_text, strict_json_loads
from ..foundation.sqlite_db import connect, table_exists
from ..store.experience import _normalize_ref
from ..store.model import (
    MODEL_ATTEMPT_OK, MODEL_ATTEMPT_OUTCOMES, ModelLedgerMixin, initialize_model_authorization_table,
)
from ..store.records import RecordStore

MIN_IDLE = timedelta(minutes=15)
WAKE_COOLDOWN = timedelta(minutes=30)
WAKE_WINDOW = timedelta(hours=24)
MAX_WAKE_ATTEMPTS = 4

_SCHEMA_VERSION = 1
_DB_NAME = "wake.sqlite3"
_SIDECARS = ("-journal", "-wal", "-shm")
_MAX_PROPOSAL_BYTES = 16 * 1024
_MAX_SOURCE_REFS = 16
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")
_REQUEST_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_SCHEMA = """
CREATE TABLE wake_identity (
    instance_id TEXT PRIMARY KEY NOT NULL,
    version INTEGER NOT NULL
);
CREATE TABLE wake_attempts (
    attempt_id TEXT PRIMARY KEY,
    generation TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'reserved', 'proposed', 'noop', 'failed', 'discarded'
    )),
    request_hash TEXT,
    source_refs_json TEXT,
    authorization_id TEXT,
    provider_outcome TEXT,
    usage_json TEXT,
    settled_at TEXT,
    proposal_json TEXT,
    CHECK ((status = 'reserved') = (settled_at IS NULL)),
    CHECK ((status = 'proposed') = (proposal_json IS NOT NULL)),
    CHECK ((request_hash IS NULL) = (authorization_id IS NULL)),
    CHECK (request_hash IS NOT NULL OR provider_outcome IS NULL),
    CHECK (provider_outcome IS NOT NULL OR usage_json IS NULL)
);
CREATE INDEX wake_attempts_time ON wake_attempts (reserved_at);
CREATE INDEX wake_attempts_authorization ON wake_attempts (authorization_id);
"""


@dataclass(frozen=True)
class WakeAttempt:
    """An already committed reservation. Its ID also identifies any proposal."""

    attempt_id: str
    instance_id: str
    generation: str
    reserved_at: datetime


DreamingSourceRef = EventRef | RecordRef | Mapping[str, Any]


class _DreamingAuthorizations(ModelLedgerMixin, RecordStore):
    """The PCS model authorization ledger, scoped to the Dreaming component."""


@dataclass(frozen=True)
class WakeProposal:
    """An expiring suggestion for trusted host review."""

    action: str
    reason: str
    scope: Mapping[str, Any]
    source_refs: tuple[DreamingSourceRef, ...]
    effect: str
    expires_at: datetime


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise InvalidInput("Dreaming times must have a time zone")
    return value.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _source_ref(value: Mapping[str, Any]) -> DreamingSourceRef:
    if value.get("ref_type") == "record":
        return RecordRef.from_value(value)
    if value.get("ref_type") == "event":
        return EventRef.from_value(value)
    return _normalize_ref(value)[0]


def source_ref_mapping(value: DreamingSourceRef) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else value.to_dict()


def _proposal_text(value: str, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise InvalidInput(f"Dreaming proposal {name} must contain 1 to {limit} characters")
    return value.strip()


def _source_refs(refs: tuple[DreamingSourceRef, ...]) -> list[dict[str, Any]]:
    if len(refs) > _MAX_SOURCE_REFS:
        raise InvalidInput("Dreaming material has too many source references")
    return [source_ref_mapping(_source_ref(source_ref_mapping(ref))) for ref in refs]


def _proposal_json(proposal: WakeProposal, expires_at: datetime) -> str:
    content = canonical_json_text({
        "action": _proposal_text(proposal.action, "action", 512),
        "reason": _proposal_text(proposal.reason, "reason", 2048),
        "scope": normalize_scope(proposal.scope),
        "source_refs": _source_refs(proposal.source_refs),
        "effect": _proposal_text(proposal.effect, "effect", 512),
        "expires_at": _stamp(expires_at),
    })
    if len(content.encode("utf-8")) > _MAX_PROPOSAL_BYTES:
        raise InvalidInput("Dreaming proposal exceeds its storage limit")
    return content


def _usage_json(usage: Mapping[str, Any] | None) -> str | None:
    if usage is None:
        return None
    counts = {}
    for key in _USAGE_KEYS:
        count = usage.get(key)
        if type(count) is int and 0 <= count <= identifiers.MAX_SAFE_INTEGER:
            counts[key] = count
    return canonical_json_text(counts) if counts else None


def _path_info(path: Path) -> os.stat_result | None:
    """Inspect a directory entry without traversing a final link."""
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _is_reparse_point(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _owned_file(info: os.stat_result | None) -> bool:
    return (
        info is not None
        and stat.S_ISREG(info.st_mode)
        and not _is_reparse_point(info)
        and info.st_nlink == 1
    )


def _file_identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


class DreamingState:
    """Persistent wake state in a verified, instance-owned Dreaming directory.

    The host supplies the current generation and rechecks it and authority
    before sending, finishing and delivering results. It observes idle time
    independently of the wall clock used for the rolling budget.
    """

    def __init__(self, directory: Path, *, instance_id: str) -> None:
        try:
            self.instance_id = str(UUID(instance_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise InvalidInput("Dreaming instance identity must be a UUID") from exc
        directory = Path(directory).absolute()
        info = _path_info(directory)
        if info is None or not stat.S_ISDIR(info.st_mode) or _is_reparse_point(info):
            raise CorruptStore("Dreaming state needs an owned directory")
        self._directory_identity = _file_identity(info)
        self._path = directory / _DB_NAME
        if _path_info(self._path) is None:
            self._initialize()
        file_info = _path_info(self._path)
        if not _owned_file(file_info):
            raise CorruptStore("Dreaming state path is occupied")
        self._database_identity = _file_identity(file_info)
        self._check_paths()
        self._require_identity()
        with closing(self._connect()) as connection:
            fields = {row["name"] for row in connection.execute("PRAGMA table_info(wake_attempts)")}
            if "source_refs_json" not in fields:
                # Additive upgrade for a previously initialized Dreaming ledger.
                connection.execute("BEGIN IMMEDIATE")
                fields = {row["name"] for row in connection.execute("PRAGMA table_info(wake_attempts)")}
                if "source_refs_json" not in fields:
                    connection.execute("ALTER TABLE wake_attempts ADD COLUMN source_refs_json TEXT")
                connection.execute("COMMIT")

    def _initialize(self) -> None:
        # Publish a fully initialized database without replacing a collision.
        for suffix in _SIDECARS:
            if _path_info(self._path.with_name(_DB_NAME + suffix)) is not None:
                raise CorruptStore("Dreaming state has a sidecar without its database")
        descriptor, name = tempfile.mkstemp(
            prefix=".wake-", suffix=".sqlite3", dir=self._path.parent,
        )
        os.close(descriptor)
        temporary = Path(name)
        try:
            with closing(connect(temporary)) as connection:
                connection.executescript(_SCHEMA)
                connection.execute(
                    "INSERT INTO wake_identity (instance_id, version) VALUES (?, ?)",
                    (self.instance_id, _SCHEMA_VERSION),
                )
            try:
                if os.name == "nt":
                    # Windows rename fails if the destination already exists.
                    temporary.rename(self._path)
                else:
                    os.link(temporary, self._path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)

    def _require_identity(self) -> None:
        # The identity never changes after publication. Read only that static
        # row here without allowing SQLite to recover or create sidecars.
        try:
            with closing(sqlite3.connect(
                self._path.as_uri() + "?mode=ro&immutable=1", uri=True,
            )) as connection:
                identities = connection.execute(
                    "SELECT instance_id, version FROM wake_identity LIMIT 2"
                ).fetchall()
        except sqlite3.Error as exc:
            raise CorruptStore("Dreaming state path is occupied by an unknown file") from exc
        if identities != [(self.instance_id, _SCHEMA_VERSION)]:
            raise CorruptStore("Dreaming state identity or version does not match")

    def _check_paths(self) -> None:
        directory = _path_info(self._path.parent)
        if (
            directory is None
            or not stat.S_ISDIR(directory.st_mode)
            or _is_reparse_point(directory)
            or _file_identity(directory) != self._directory_identity
        ):
            raise CorruptStore("Dreaming state directory changed")
        database = _path_info(self._path)
        if not _owned_file(database) or _file_identity(database) != self._database_identity:
            raise CorruptStore("Dreaming state database changed")
        for suffix in _SIDECARS:
            sidecar = _path_info(self._path.with_name(_DB_NAME + suffix))
            if sidecar is None:
                continue
            # This state uses SQLite's rollback journal. An owned rollback
            # journal may be needed for recovery after an interrupted write.
            if suffix != "-journal" or not _owned_file(sidecar):
                raise CorruptStore("Dreaming state has an unexpected SQLite sidecar")

    def _connect(self) -> sqlite3.Connection:
        self._check_paths()
        # mode=rw also stops the host from recreating a file removed externally.
        connection = sqlite3.connect(
            self._path.as_uri() + "?mode=rw", uri=True, isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def authorizations(self, migrations_dir: Path, *, provision: bool = False):
        """Reuse PCS's grant schema and API without opening the legacy DB for writes."""
        with closing(self._connect()) as connection:
            if not table_exists(connection, "model_authorizations"):
                if not provision:
                    yield None
                    return
                initialize_model_authorization_table(connection, migrations_dir)
            yield _DreamingAuthorizations(connection, migrations_dir)

    def reserve_wake(
        self,
        *,
        generation: str,
        now: datetime,
        idle_since: datetime,
        foreground_busy: bool,
        pending_work: bool,
        eligible: bool,
    ) -> WakeAttempt | None:
        """Reserve at most one request after true idle and within the wake budget."""
        if foreground_busy or pending_work or not eligible:
            return None
        generation = identifiers.require_context_id(generation)
        now = _utc(now)
        if now - _utc(idle_since) < MIN_IDLE:
            return None

        reserved_at = _stamp(now)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            latest = connection.execute(
                "SELECT reserved_at, generation, status FROM wake_attempts "
                "ORDER BY reserved_at DESC LIMIT 1"
            ).fetchone()
            if latest is not None:
                if latest["reserved_at"] > _stamp(now - WAKE_COOLDOWN):
                    return None
                if latest["generation"] == generation and latest["status"] == "reserved":
                    return None
            used = connection.execute(
                "SELECT COUNT(*) FROM wake_attempts WHERE reserved_at > ?",
                (_stamp(now - WAKE_WINDOW),),
            ).fetchone()[0]
            if used >= MAX_WAKE_ATTEMPTS:
                return None

            # A reservation from a dead or superseded generation still counts.
            # Retire it when the new generation next acquires a wake.
            connection.execute(
                "UPDATE wake_attempts SET status = 'discarded', settled_at = ? "
                "WHERE status = 'reserved'",
                (reserved_at,),
            )
            attempt = WakeAttempt(str(uuid4()), self.instance_id, generation, now)
            connection.execute(
                "INSERT INTO wake_attempts (attempt_id, generation, reserved_at, status) "
                "VALUES (?, ?, ?, 'reserved')",
                (attempt.attempt_id, generation, reserved_at),
            )
        return attempt

    def record_request(
        self, attempt: WakeAttempt, *, request_hash: str, authorization_id: str,
        source_refs: tuple[DreamingSourceRef, ...] = (),
    ) -> bool:
        """Record the exact request hash once, immediately before the host sends."""
        if attempt.instance_id != self.instance_id:
            return False
        if not isinstance(request_hash, str) or _REQUEST_HASH.fullmatch(request_hash) is None:
            raise InvalidInput("Dreaming request hash must be a prefixed SHA-256")
        authorization_id = identifiers.require_request_id(authorization_id)
        with closing(self._connect()) as connection:
            updated = connection.execute(
                "UPDATE wake_attempts SET request_hash = ?, authorization_id = ?, source_refs_json = ? "
                "WHERE attempt_id = ? AND generation = ? AND reserved_at = ? "
                "AND status = 'reserved' AND request_hash IS NULL",
                (
                    request_hash, authorization_id, canonical_json_text(_source_refs(source_refs)),
                    attempt.attempt_id,
                    attempt.generation, _stamp(attempt.reserved_at),
                ),
            )
            return updated.rowcount == 1

    def count_model_requests(self, authorization_id: str) -> int:
        """Count claimed requests, including interrupted and discarded attempts."""
        authorization_id = identifiers.require_request_id(authorization_id)
        with closing(self._connect()) as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM wake_attempts WHERE authorization_id = ?",
                (authorization_id,),
            ).fetchone()[0]

    def finish_wake(
        self,
        attempt: WakeAttempt,
        *,
        now: datetime,
        current_generation: str,
        proposal: WakeProposal | None,
        eligible: bool = True,
        provider_outcome: str = MODEL_ATTEMPT_OK,
        usage: Mapping[str, Any] | None = None,
        recap: str | None = None,
        complete: Callable[[sqlite3.Connection], bool] | None = None,
    ) -> bool:
        """Settle a model result once, retaining proposals and recaps only while eligible."""
        if provider_outcome != MODEL_ATTEMPT_OK:
            raise InvalidInput("only a successful model response can finish a wake")
        if attempt.instance_id != self.instance_id:
            return False
        now = _utc(now)
        status = "discarded"
        content = None
        if eligible and attempt.generation == current_generation and now >= attempt.reserved_at:
            if proposal is None:
                status = "noop"
            else:
                expires_at = _utc(proposal.expires_at)
                if expires_at > now:
                    status = "proposed"
                    content = _proposal_json(proposal, expires_at)
        if recap is not None:
            recap = _proposal_text(recap, "recap", 1200)
        if status == "discarded":
            self._settle(attempt, now, status, content, provider_outcome, usage)
            return False
        return self._settle(
            attempt, now, status, content, provider_outcome, usage,
            recap=recap, complete=complete,
        )

    def fail_wake(
        self,
        attempt: WakeAttempt,
        *,
        now: datetime,
        provider_outcome: str | None = None,
        usage: Mapping[str, Any] | None = None,
    ) -> bool:
        """Record a failed wake. Unknown provider outcome remains unknown."""
        if attempt.instance_id != self.instance_id:
            return False
        if provider_outcome is not None and provider_outcome not in MODEL_ATTEMPT_OUTCOMES:
            raise InvalidInput("unknown Dreaming provider outcome")
        return self._settle(attempt, _utc(now), "failed", None, provider_outcome, usage)

    def _settle(
        self,
        attempt: WakeAttempt,
        now: datetime,
        status: str,
        proposal: str | None,
        provider_outcome: str | None,
        usage: Mapping[str, Any] | None,
        *,
        recap: str | None = None,
        complete: Callable[[sqlite3.Connection], bool] | None = None,
    ) -> bool:
        with closing(self._connect()) as connection, connection:
            if recap is not None or complete is not None:
                connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                "UPDATE wake_attempts "
                "SET status = ?, settled_at = ?, proposal_json = ?, "
                "provider_outcome = ?, usage_json = ? "
                "WHERE attempt_id = ? AND generation = ? AND reserved_at = ? "
                "AND status = 'reserved' AND (? IS NULL OR request_hash IS NOT NULL)",
                (
                    status, _stamp(max(now, attempt.reserved_at)), proposal,
                    provider_outcome, _usage_json(usage) if provider_outcome else None,
                    attempt.attempt_id,
                    attempt.generation, _stamp(attempt.reserved_at), provider_outcome,
                ),
            )
            if updated.rowcount != 1:
                return False
            if complete is not None and not complete(connection):
                connection.execute(
                    "UPDATE wake_attempts SET status = 'discarded', proposal_json = NULL "
                    "WHERE attempt_id = ?", (attempt.attempt_id,),
                )
                return False
            if recap is not None:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS dreaming_deep_recaps ("
                    "attempt_id TEXT PRIMARY KEY, source_refs_json TEXT NOT NULL, recap TEXT NOT NULL)",
                )
                connection.execute(
                    "INSERT INTO dreaming_deep_recaps "
                    "SELECT attempt_id, source_refs_json, ? FROM wake_attempts WHERE attempt_id = ?",
                    (recap, attempt.attempt_id),
                )
            return True

    def journal_reflections(self, since: datetime, until: datetime) -> tuple[dict[str, Any], ...]:
        """Read historical idle proposals as observations for journal research."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT attempt_id, settled_at, proposal_json, source_refs_json "
                "FROM wake_attempts WHERE status = 'proposed' AND settled_at >= ? "
                "AND settled_at < ? ORDER BY settled_at, attempt_id",
                (_stamp(_utc(since)), _stamp(_utc(until))),
            ).fetchall()
        result = []
        for row in rows:
            proposal = strict_json_loads(row["proposal_json"])
            result.append({
                "attempt_id": row["attempt_id"],
                "settled_at": row["settled_at"],
                "action": proposal["action"],
                "reason": proposal["reason"],
                "effect": proposal["effect"],
                "source_refs": strict_json_loads(row["source_refs_json"]),
            })
        return tuple(result)

    def proposal_authorization(self, attempt_id: str) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT authorization_id FROM wake_attempts WHERE attempt_id = ? AND status = 'proposed'",
                (attempt_id,),
            ).fetchone()
        return None if row is None else row["authorization_id"]

    def read_proposal(self, attempt_id: str, *, now: datetime) -> WakeProposal | None:
        """Load a local proposal for the host to recheck before any delivery."""
        now = _utc(now)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT proposal_json FROM wake_attempts WHERE attempt_id = ? "
                "AND status = 'proposed' AND settled_at <= ?",
                (attempt_id, _stamp(now)),
            ).fetchone()
        if row is None:
            return None
        data = strict_json_loads(row["proposal_json"])
        expires_at = datetime.fromisoformat(data["expires_at"])
        if expires_at <= now:
            return None
        return WakeProposal(
            action=data["action"],
            reason=data["reason"],
            scope=data["scope"],
            source_refs=tuple(_source_ref(ref) for ref in data["source_refs"]),
            effect=data["effect"],
            expires_at=expires_at,
        )
