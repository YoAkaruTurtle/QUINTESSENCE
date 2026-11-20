"""Private calendar work and immutable, source-linked Dreaming journals."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any
from uuid import UUID

from ..adapters.scope_recall.dreaming import require_dreaming_owner
from ..domains.memory.time_window import resolve_zone
from ..foundation.errors import CorruptStore, InvalidInput
from ..foundation.jsonutil import canonical_json_text, sha256_hex, strict_json_loads
from ..foundation.sqlite_db import connect
from .dreaming import _file_identity, _owned_file, _path_info, _stamp, _utc


_KINDS = ("day", "week", "month", "year")
_DATABASE = "calendar.sqlite3"
_SIDECARS = ("-journal", "-wal", "-shm")
_SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE calendar_identity (
    instance_id TEXT PRIMARY KEY NOT NULL,
    version INTEGER NOT NULL
);
CREATE TABLE running_days (
    timezone TEXT NOT NULL,
    local_day TEXT NOT NULL,
    PRIMARY KEY (timezone, local_day)
);
CREATE TABLE periods (
    key TEXT PRIMARY KEY NOT NULL,
    kind TEXT NOT NULL,
    level INTEGER NOT NULL,
    timezone TEXT NOT NULL,
    start_day TEXT NOT NULL,
    end_day TEXT NOT NULL,
    payload TEXT,
    revision TEXT UNIQUE,
    retry_after TEXT,
    CHECK ((payload IS NULL) = (revision IS NULL))
);
CREATE INDEX periods_due ON periods (timezone, end_day, level) WHERE payload IS NULL;
CREATE INDEX periods_published ON periods (timezone, end_day, level) WHERE payload IS NOT NULL;
CREATE TABLE journal_progress (
    period_key TEXT PRIMARY KEY NOT NULL,
    basis TEXT NOT NULL,
    value TEXT NOT NULL
);
CREATE TABLE journal_authorization (
    slot INTEGER PRIMARY KEY CHECK (slot = 1),
    fingerprint TEXT NOT NULL
);
"""


def _period_dates(kind: str, day: date) -> tuple[date, date]:
    if type(day) is not date:
        raise InvalidInput("Journal period requires a local calendar date")
    try:
        if kind == "day":
            return day, day + timedelta(days=1)
        if kind == "week":
            start = day - timedelta(days=day.weekday())
            return start, start + timedelta(days=7)
        if kind == "month":
            start = day.replace(day=1)
            if day.month == 12:
                return start, date(day.year + 1, 1, 1)
            return start, date(day.year, day.month + 1, 1)
        if kind == "year":
            return date(day.year, 1, 1), date(day.year + 1, 1, 1)
    except (OverflowError, ValueError) as exc:
        raise InvalidInput("Journal period is outside the supported calendar") from exc
    raise InvalidInput("Unknown journal period kind")


@dataclass(frozen=True)
class JournalPeriod:
    """A natural, half-open calendar period identified by its local dates."""

    kind: str
    start: date
    end: date
    timezone: str

    def __post_init__(self) -> None:
        if type(self.end) is not date or (self.start, self.end) != _period_dates(self.kind, self.start):
            raise InvalidInput("Journal period dates are not aligned to the calendar")
        resolve_zone(self.timezone)

    @classmethod
    def containing(cls, kind: str, day: date, timezone: str) -> JournalPeriod:
        start, end = _period_dates(kind, day)
        return cls(kind, start, end, timezone)

    @property
    def key(self) -> str:
        return canonical_json_text([self.timezone, self.kind, self.start.isoformat(), self.end.isoformat()])

    @property
    def bounds(self) -> tuple[datetime, datetime]:
        zone = resolve_zone(self.timezone)
        return (
            datetime.combine(self.start, time.min, tzinfo=zone).astimezone(UTC),
            datetime.combine(self.end, time.min, tzinfo=zone).astimezone(UTC),
        )


@dataclass(frozen=True)
class JournalEntry:
    period: JournalPeriod
    text: str
    tags: tuple[str, ...]
    refs: tuple[dict[str, Any], ...]
    children: tuple[str, ...]
    revision: str


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidInput(f"Journal {name} must be nonempty text")
    return value


def _refs(values: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    refs = {}
    for value in values:
        if not isinstance(value, Mapping) or not value:
            raise InvalidInput("Journal source reference must be a nonempty mapping")
        ref = dict(value)
        refs.setdefault(canonical_json_text(ref), ref)
    return tuple(refs.values())


def _period(row: sqlite3.Row) -> JournalPeriod:
    period = JournalPeriod(
        row["kind"], date.fromisoformat(row["start_day"]), date.fromisoformat(row["end_day"]),
        row["timezone"],
    )
    if row["key"] != period.key or row["level"] != _KINDS.index(period.kind):
        raise CorruptStore("Journal period identity does not match")
    return period


def _decode(row: sqlite3.Row | None) -> JournalEntry | None:
    if row is None or row["payload"] is None:
        return None
    try:
        period = _period(row)
        payload = strict_json_loads(row["payload"])
        if set(payload) != {"text", "tags", "refs", "children"}:
            raise ValueError("invalid journal fields")
        revision = _revision(period, row["payload"])
        if row["revision"] != revision:
            raise ValueError("journal revision does not match")
        if not all(isinstance(payload[name], list) for name in ("tags", "refs", "children")):
            raise ValueError("invalid journal list fields")
        return JournalEntry(
            period, _text(payload["text"], "text"),
            tuple(_text(tag, "tag") for tag in payload["tags"]), _refs(payload["refs"]),
            tuple(_text(child, "child revision") for child in payload["children"]),
            revision,
        )
    except (KeyError, TypeError, ValueError, InvalidInput) as exc:
        raise CorruptStore("Journal entry is invalid") from exc


def _revision(period: JournalPeriod, payload: str) -> str:
    return "sha256:" + sha256_hex(period.key + "\n" + payload)


def _children_for_day(period: JournalPeriod, day: date) -> JournalPeriod:
    if period.kind == "year":
        kind = "month"
    elif period.kind == "month":
        week_start, week_end = _period_dates("week", day)
        kind = "week" if period.start <= week_start and week_end <= period.end else "day"
    else:
        kind = "day"
    return JournalPeriod.containing(kind, day, period.timezone)


class CalendarJournalStore:
    """Persist calendar work inside a verified Dreaming owner directory.

    A witnessed running day schedules calendar work. Only immutable completed
    entries can satisfy another journal's dependency or be recalled by the
    host. Authorization and version checks against live PCS are host duties.
    """

    def __init__(self, directory: Path, *, instance_id: str) -> None:
        try:
            self.instance_id = str(UUID(instance_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise InvalidInput("Journal instance identity must be a UUID") from exc
        self._directory = Path(directory).absolute()
        self._path = self._directory / _DATABASE
        require_dreaming_owner(self._directory, self.instance_id)
        self._directory_identity = _file_identity(self._directory.lstat())
        if _path_info(self._path) is None:
            self._initialize()
        info = _path_info(self._path)
        if not _owned_file(info):
            raise CorruptStore("Journal database path is occupied")
        self._database_identity = _file_identity(info)
        self._check_paths()
        self._require_identity()

    def _initialize(self) -> None:
        if any(_path_info(self._path.with_name(_DATABASE + suffix)) is not None for suffix in _SIDECARS):
            raise CorruptStore("Journal sidecar exists without its database")
        descriptor, name = tempfile.mkstemp(prefix=".calendar-", suffix=".sqlite3", dir=self._directory)
        os.close(descriptor)
        staging = Path(name)
        try:
            with closing(connect(staging)) as connection:
                connection.executescript(_SCHEMA)
                connection.execute(
                    "INSERT INTO calendar_identity VALUES (?, ?)", (self.instance_id, _SCHEMA_VERSION),
                )
            try:
                if os.name == "nt":
                    staging.rename(self._path)
                else:
                    os.link(staging, self._path)
            except FileExistsError:
                # A concurrent owner may have won publication. Validate its file normally.
                pass
        finally:
            staging.unlink(missing_ok=True)

    def _check_paths(self) -> None:
        require_dreaming_owner(self._directory, self.instance_id)
        file = _path_info(self._path)
        if (
            _file_identity(self._directory.lstat()) != self._directory_identity
            or not _owned_file(file) or _file_identity(file) != self._database_identity
        ):
            raise CorruptStore("Journal database or its directory changed")
        for suffix in _SIDECARS:
            sidecar = _path_info(self._path.with_name(_DATABASE + suffix))
            if sidecar is not None and (suffix != "-journal" or not _owned_file(sidecar)):
                raise CorruptStore("Journal database has an unexpected SQLite sidecar")

    def _require_identity(self) -> None:
        # Identity commits only when the new database is created. Checking it
        # this way cannot recover, launch schema triggers, or change a collision.
        try:
            with closing(sqlite3.connect(self._path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
                identity = db.execute("SELECT instance_id, version FROM calendar_identity LIMIT 2").fetchall()
                tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        except sqlite3.Error as exc:
            raise CorruptStore("Journal database path is occupied by an unknown file") from exc
        if identity != [(self.instance_id, _SCHEMA_VERSION)] or not {
            "running_days", "periods", "journal_progress", "journal_authorization",
        }.issubset(tables):
            raise CorruptStore("Journal database identity or version does not match")

    def _connect(self) -> sqlite3.Connection:
        self._check_paths()
        db = sqlite3.connect(self._path.as_uri() + "?mode=rw", uri=True, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def record_running(self, now: datetime, timezone: str) -> bool:
        """Witness this local date only. Do not interpolate across a gap."""
        day = _utc(now).astimezone(resolve_zone(timezone)).date()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            created = db.execute(
                "INSERT OR IGNORE INTO running_days VALUES (?, ?)", (timezone, day.isoformat()),
            ).rowcount == 1
            if created:
                for level, kind in enumerate(_KINDS):
                    period = JournalPeriod.containing(kind, day, timezone)
                    db.execute(
                        "INSERT OR IGNORE INTO periods (key, kind, level, timezone, start_day, end_day) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (period.key, kind, level, timezone, period.start.isoformat(), period.end.isoformat()),
                    )
            return created

    def next_due(self, now: datetime, timezone: str) -> JournalPeriod | None:
        day = _utc(now).astimezone(resolve_zone(timezone)).date()
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT * FROM periods WHERE timezone = ? AND end_day <= ? AND payload IS NULL "
                "AND (retry_after IS NULL OR retry_after <= ?) "
                "ORDER BY end_day, level LIMIT 1", (timezone, day.isoformat(), _stamp(_utc(now))),
            ).fetchone()
        return None if row is None else _period(row)

    def defer(self, period: JournalPeriod, *, until: datetime) -> None:
        """Let other due periods advance while a failed period waits to retry."""
        with closing(self._connect()) as db:
            db.execute(
                "UPDATE periods SET retry_after = ? WHERE key = ? AND payload IS NULL",
                (_stamp(_utc(until)), period.key),
            )

    def _inputs(self, db: sqlite3.Connection, period: JournalPeriod) -> tuple[JournalEntry, ...]:
        if period.kind == "day":
            return ()
        days = db.execute(
            "SELECT local_day FROM running_days WHERE timezone = ? AND local_day >= ? AND local_day < ? "
            "ORDER BY local_day", (period.timezone, period.start.isoformat(), period.end.isoformat()),
        )
        required = dict.fromkeys(
            _children_for_day(period, date.fromisoformat(row["local_day"])) for row in days
        )
        entries = []
        for child in required:
            entry = self._get(db, child)
            if entry is None:
                raise InvalidInput("Journal child period has not been completed")
            entries.append(entry)
        return tuple(entries)

    def inputs(self, period: JournalPeriod) -> tuple[JournalEntry, ...]:
        """Return exact completed dependencies, with daily fragments on month edges."""
        with closing(self._connect()) as db, db:
            db.execute("BEGIN")
            return self._inputs(db, period)

    @staticmethod
    def _get(db: sqlite3.Connection, period: JournalPeriod) -> JournalEntry | None:
        return _decode(db.execute("SELECT * FROM periods WHERE key = ?", (period.key,)).fetchone())

    def get(self, period: JournalPeriod) -> JournalEntry | None:
        with closing(self._connect()) as db:
            return self._get(db, period)

    def get_revision(self, revision: str) -> JournalEntry | None:
        with closing(self._connect()) as db:
            return _decode(db.execute("SELECT * FROM periods WHERE revision = ?", (revision,)).fetchone())

    def recent(
        self, before: datetime, timezone: str, limit: int | None = None,
    ) -> tuple[JournalEntry, ...]:
        day = _utc(before).astimezone(resolve_zone(timezone)).date()
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT * FROM periods WHERE timezone = ? AND end_day <= ? AND payload IS NOT NULL "
                "ORDER BY end_day DESC, level LIMIT ?",
                (timezone, day.isoformat(), -1 if limit is None else limit),
            )
            return tuple(_decode(row) for row in rows)

    def save(
        self, period: JournalPeriod, *, text: str, tags: Iterable[str],
        refs: Iterable[Mapping[str, Any]], children: Iterable[str],
    ) -> JournalEntry:
        """Commit one immutable result, its exact dependencies and its progress together."""
        text = _text(text, "text")
        tags = tuple(dict.fromkeys(_text(tag, "tag") for tag in tags))
        sources = _refs(refs)
        child_revisions = tuple(dict.fromkeys(_text(child, "child revision") for child in children))
        payload = canonical_json_text({
            "text": text, "tags": tags, "refs": sources, "children": child_revisions,
        })
        revision = _revision(period, payload)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = self._get(db, period)
            if existing is not None:
                if revision != existing.revision:
                    raise InvalidInput("Journal period is already finalized")
                return existing
            if not {entry.revision for entry in self._inputs(db, period)}.issubset(child_revisions):
                raise InvalidInput("Journal is missing required child revisions")
            for child in child_revisions:
                if db.execute("SELECT 1 FROM periods WHERE revision = ?", (child,)).fetchone() is None:
                    raise InvalidInput("Journal child revision does not exist")
            changed = db.execute(
                "UPDATE periods SET payload = ?, revision = ? WHERE key = ? AND payload IS NULL",
                (payload, revision, period.key),
            ).rowcount
            if changed != 1:
                raise InvalidInput("Journal period has no witnessed running day")
            db.execute("DELETE FROM journal_progress WHERE period_key = ?", (period.key,))
        return JournalEntry(period, text, tags, sources, child_revisions, revision)

    def load_progress(self, period: JournalPeriod, basis: str) -> dict[str, Any] | None:
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT value FROM journal_progress WHERE period_key = ? AND basis = ?", (period.key, basis),
            ).fetchone()
        if row is None:
            return None
        value = strict_json_loads(row["value"])
        if not isinstance(value, dict):
            raise CorruptStore("Journal progress is invalid")
        return value

    def save_progress(self, period: JournalPeriod, basis: str, value: Mapping[str, Any]) -> None:
        if not isinstance(value, Mapping):
            raise InvalidInput("Journal progress must be an object")
        document = canonical_json_text(dict(value))
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM periods WHERE key = ? AND payload IS NULL", (period.key,),
            ).fetchone() is None:
                raise InvalidInput("Journal period has no pending work")
            db.execute(
                "INSERT INTO journal_progress VALUES (?, ?, ?) "
                "ON CONFLICT(period_key) DO UPDATE SET basis = excluded.basis, value = excluded.value",
                (period.key, _text(basis, "progress basis"), document),
            )

    def authorization(self) -> str | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT fingerprint FROM journal_authorization WHERE slot = 1").fetchone()
        return None if row is None else row["fingerprint"]

    def set_authorization(self, fingerprint: str | None) -> None:
        with closing(self._connect()) as db:
            if fingerprint is None:
                db.execute("DELETE FROM journal_authorization WHERE slot = 1")
            else:
                db.execute(
                    "INSERT INTO journal_authorization VALUES (1, ?) "
                    "ON CONFLICT(slot) DO UPDATE SET fingerprint = excluded.fingerprint",
                    (_text(fingerprint, "authorization fingerprint"),),
                )
