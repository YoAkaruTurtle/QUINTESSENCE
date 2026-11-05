"""Independent, append-only deletion ledger (J10 W1).

The ledger lives **outside** the PCS database directory so that restoring a
database snapshot cannot restore the ledger with it.  It is a JSONL file with a
back-link hash chain plus a seal file that pins the last count and hash.  The
chain detects truncation and tampering; it does **not** prevent a rollback of
the whole file, which is why recovery also compares watermarks both ways.

Entries never carry record bodies, claims or any user content: a line names the
target identity, the authorization reference and the operation id only.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

GENESIS = "0" * 64
LEDGER_FILE = "ledger.jsonl"
SEAL_FILE = "ledger.seal"
BASELINE_FILE = "baseline.json"

OP_PREWRITE = "prewrite"
OP_CONFIRM = "confirm"
OP_ADOPT_BASELINE = "adopt_baseline"
OP_ADOPT_TARGET = "adopt_target"

#: An adopted instance cannot prove deletions that happened before its
#: baseline; the ledger says so instead of implying completeness.
PRE_BASELINE_UNPROVABLE = "unprovable"

STATUS_PREWRITTEN = "prewritten"


class LedgerError(Exception):
    """Base class for ledger failures."""


class LedgerUnavailable(LedgerError):
    """The ledger cannot be written or read at all."""


class LedgerCorrupt(LedgerError):
    """The ledger chain, the seal or the baseline is not trustworthy."""


@dataclass(frozen=True)
class LedgerEntry:
    seq: int
    prev: str
    digest: str
    op: str
    operation_id: str
    namespace: str
    target_kind: str
    target_id: str
    target_version: int
    authorization_ref: str
    privacy_epoch: int
    created_at: str
    adopted_by: str = ""
    adopt_reason: str = ""
    status: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "prev": self.prev,
            "op": self.op,
            "operation_id": self.operation_id,
            "namespace": self.namespace,
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "target_version": self.target_version,
            "authorization_ref": self.authorization_ref,
            "privacy_epoch": self.privacy_epoch,
            "created_at": self.created_at,
            "hash": self.digest,
            **({"adopted_by": self.adopted_by} if self.adopted_by else {}),
            **({"adopt_reason": self.adopt_reason} if self.adopt_reason else {}),
            **({"status": self.status} if self.status else {}),
        }

    @property
    def is_prewrite(self) -> bool:
        return self.op == OP_PREWRITE


def _canonical(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


class DeletionLedger:
    """Append-only deletion ledger in one directory (kept outside the DB dir)."""

    def __init__(self, directory: Path | str, *, clock: Any | None = None) -> None:
        self._dir = Path(directory)
        self._clock = clock
        self._cache: list[LedgerEntry] | None = None

    # ------------------------------------------------------------- basics

    @property
    def directory(self) -> Path:
        return self._dir

    def _path(self, name: str) -> Path:
        return self._dir / name

    def _now(self) -> str:
        if self._clock is not None:
            return str(self._clock())
        from .deletion import utc_now_rfc3339

        return utc_now_rfc3339()

    def _fsync_dir(self) -> None:
        try:  # pragma: no cover - best effort on platforms without dir fsync
            fd = os.open(str(self._dir), os.O_RDONLY)
        except OSError:  # pragma: no cover
            return
        try:  # pragma: no cover
            os.fsync(fd)
        finally:  # pragma: no cover
            os.close(fd)

    def initialize(self) -> None:
        """Create an empty ledger and seal; safe to call repeatedly."""

        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - permissions
            raise LedgerUnavailable(str(exc)) from exc
        if not self._path(LEDGER_FILE).exists():
            self._path(LEDGER_FILE).write_text("", encoding="utf-8")
        if not self._path(SEAL_FILE).exists():
            self._write_seal(0, GENESIS)
        else:
            count, head = self._load_seal()
            if count != 0 or head != GENESIS:
                raise LedgerCorrupt("refusing to reinitialize a non-empty ledger")

    # ------------------------------------------------------------ baseline

    def ensure_ready(self, namespace: str, current_epoch: int) -> int:
        """Create the ledger files and this namespace's baseline when absent.

        A fresh ledger may only start from the *current* epoch: anything older
        cannot be proven and is refused by recovery (``restore_before_ledger_baseline``).
        """

        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - permissions
            raise LedgerUnavailable(str(exc)) from exc
        baselines = self._load_baseline()
        if namespace in baselines:
            return int(baselines[namespace])
        baselines[namespace] = int(current_epoch)
        self._write_baseline(baselines)
        if not self._path(LEDGER_FILE).exists():
            self._path(LEDGER_FILE).write_text("", encoding="utf-8")
            self._write_seal(0, GENESIS)
        return int(current_epoch)

    def baseline_epoch(self, namespace: str) -> int | None:
        baselines = self._load_baseline()
        if namespace not in baselines:
            return None
        return int(baselines[namespace])

    def _load_baseline(self) -> dict[str, int]:
        path = self._path(BASELINE_FILE)
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LedgerCorrupt(f"baseline unreadable: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("namespaces"), dict):
            raise LedgerCorrupt("baseline has an unexpected shape")
        result: dict[str, int] = {}
        for name, epoch in payload["namespaces"].items():
            if not isinstance(name, str) or type(epoch) is not int or epoch < 0:
                raise LedgerCorrupt("baseline entries must be name -> non-negative int")
            result[name] = epoch
        return result

    def _write_baseline(self, baselines: Mapping[str, int]) -> None:
        payload = {"version": 1, "namespaces": dict(sorted(baselines.items()))}
        self._atomic_write(self._path(BASELINE_FILE), _canonical(payload))

    def _atomic_write(self, path: Path, text: str) -> None:
        # Per-writer temp name: two processes may initialise the same ledger at
        # the same time (a second host still has to open the store before the
        # single-writer guard rejects it).  A shared ".tmp" lets one rename
        # collide with the other process's open handle on Windows.
        temp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            with open(temp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except OSError as exc:
            try:
                temp.unlink()
            except OSError:
                pass
            raise LedgerUnavailable(str(exc)) from exc

    # -------------------------------------------------------------- entries

    def entries(self) -> list[LedgerEntry]:
        if self._cache is not None:
            return list(self._cache)
        path = self._path(LEDGER_FILE)
        if not path.exists():
            raise LedgerUnavailable(f"ledger file is missing: {path}")
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise LedgerUnavailable(str(exc)) from exc
        parsed: list[LedgerEntry] = []
        prev = GENESIS
        for number, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except ValueError as exc:
                raise LedgerCorrupt(f"line {number} is not JSON") from exc
            if not isinstance(payload, dict):
                raise LedgerCorrupt(f"line {number} is not an object")
            digest = payload.pop("hash", None)
            expected = _digest(payload)
            if digest != expected:
                raise LedgerCorrupt(f"line {number} hash mismatch")
            if payload.get("seq") != number:
                raise LedgerCorrupt(f"line {number} seq mismatch")
            if payload.get("prev") != prev:
                raise LedgerCorrupt(f"line {number} back-link mismatch")
            prev = digest
            parsed.append(
                LedgerEntry(
                    seq=number,
                    prev=payload.get("prev", ""),
                    digest=digest,
                    op=str(payload.get("op", "")),
                    operation_id=str(payload.get("operation_id", "")),
                    namespace=str(payload.get("namespace", "")),
                    target_kind=str(payload.get("target_kind", "")),
                    target_id=str(payload.get("target_id", "")),
                    target_version=int(payload.get("target_version", 0) or 0),
                    authorization_ref=str(payload.get("authorization_ref", "")),
                    privacy_epoch=int(payload.get("privacy_epoch", 0) or 0),
                    created_at=str(payload.get("created_at", "")),
                    adopted_by=str(payload.get("adopted_by", "")),
                    adopt_reason=str(payload.get("adopt_reason", "")),
                    status=str(payload.get("status", "")),
                )
            )
        self._verify_seal(len(parsed), prev)
        self._cache = parsed
        return list(parsed)

    def _load_seal(self) -> tuple[int, str]:
        path = self._path(SEAL_FILE)
        if not path.exists():
            raise LedgerUnavailable(f"ledger seal is missing: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LedgerCorrupt(f"seal unreadable: {exc}") from exc
        count = payload.get("count")
        head = payload.get("head")
        if type(count) is not int or not isinstance(head, str):
            raise LedgerCorrupt("seal has an unexpected shape")
        return count, head

    def _verify_seal(self, count: int, head: str) -> None:
        sealed_count, sealed_head = self._load_seal()
        if sealed_count != count or sealed_head != head:
            raise LedgerCorrupt("ledger tail does not match the seal (truncated or extended)")

    def _write_seal(self, count: int, head: str) -> None:
        self._atomic_write(self._path(SEAL_FILE), _canonical({"count": count, "head": head}))

    def _append(self, payload: Mapping[str, Any]) -> LedgerEntry:
        entries = self.entries()
        prev = entries[-1].digest if entries else GENESIS
        body = dict(payload)
        body["seq"] = len(entries) + 1
        body["prev"] = prev
        body["hash"] = _digest(body)
        try:
            with open(self._path(LEDGER_FILE), "a", encoding="utf-8", newline="\n") as handle:
                handle.write(_canonical(body) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise LedgerUnavailable(str(exc)) from exc
        self._write_seal(body["seq"], body["hash"])
        self._fsync_dir()
        self._cache = None
        return self.entries()[-1]

        # ------------------------------------------------------------- adoption

    def adoption(self) -> dict[str, Any] | None:
        """Return the one-shot adoption record, if this ledger was adopted."""

        try:
            entries = self.entries()
        except (LedgerUnavailable, LedgerCorrupt):
            return None
        for entry in entries:
            if entry.op == OP_ADOPT_BASELINE:
                return {
                    "adopted_by": entry.adopted_by,
                    "adopt_reason": entry.adopt_reason,
                    "adopted_at": entry.created_at,
                    "pre_baseline_deletions": PRE_BASELINE_UNPROVABLE,
                    "baselines": self._load_baseline(),
                }
        return None

    def install_adoption(
        self,
        *,
        payloads: Sequence[Mapping[str, Any]],
        baselines: Mapping[str, int],
        adopted_by: str,
        adopt_reason: str,
    ) -> LedgerEntry:
        """Atomically create a ledger that adopts an existing database.

        Everything is written to a sibling directory first and moved into place
        with one ``os.replace``: a crash either leaves no ledger (so the
        instance still refuses to open) or a complete one.  A second adoption
        can never be appended -- the target directory then already exists.
        """

        if not adopted_by or not adopt_reason:
            raise LedgerUnavailable("adopted_by and adopt_reason are required")
        if self._dir.exists():
            raise LedgerCorrupt(f"refusing to adopt over an existing ledger: {self._dir}")
        temp = self._dir.with_name(self._dir.name + f".adopting-{os.getpid()}")
        if temp.exists():
            raise LedgerUnavailable(f"an adoption staging directory is in the way: {temp}")
        try:
            temp.mkdir(parents=True)
        except OSError as exc:  # pragma: no cover - permissions
            raise LedgerUnavailable(str(exc)) from exc
        try:
            self._write_baseline_into(temp, baselines)
            lines: list[str] = []
            prev = GENESIS
            body_base = {"created_at": self._now()}
            body_base.update({"adopted_by": adopted_by, "adopt_reason": adopt_reason})
            payloads = list(payloads)
            for number, payload in enumerate(payloads, start=1):
                body = dict(payload)
                body.setdefault("operation_id", "")
                body.setdefault("namespace", "")
                body.setdefault("target_kind", "")
                body.setdefault("target_id", "")
                body.setdefault("target_version", 0)
                body.setdefault("authorization_ref", "")
                body.setdefault("privacy_epoch", 0)
                body.setdefault("created_at", self._now())
                combined = dict(body)
                combined["seq"] = number
                combined["prev"] = prev
                combined["hash"] = _digest(combined)
                lines.append(_canonical(combined))
                prev = combined["hash"]
            ledger_path = temp / LEDGER_FILE
            with open(ledger_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(lines) + ("\n" if lines else ""))
                handle.flush()
                os.fsync(handle.fileno())
            seal_path = temp / SEAL_FILE
            with open(seal_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(_canonical({"count": len(lines), "head": prev}))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                fd = os.open(str(temp), os.O_RDONLY)
            except OSError:  # pragma: no cover
                fd = None
            if fd is not None:  # pragma: no cover
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            os.replace(temp, self._dir)
        except OSError as exc:
            raise LedgerUnavailable(str(exc)) from exc
        finally:
            if temp.exists():
                import shutil

                shutil.rmtree(temp, ignore_errors=True)
        self._cache = None
        return self.entries()[-1]

    def _write_baseline_into(self, directory: Path, baselines: Mapping[str, int]) -> None:
        payload = {"version": 1, "namespaces": dict(sorted(baselines.items()))}
        path = directory / BASELINE_FILE
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(_canonical(payload))
            handle.flush()
            os.fsync(handle.fileno())

    # ------------------------------------------------------------- writes

    def prewrite(
        self,
        *,
        operation_id: str,
        namespace: str,
        target_kind: str,
        target_id: str,
        target_version: int,
        authorization_ref: str,
        privacy_epoch: int,
    ) -> LedgerEntry:
        """Write the authorized deletion *before* the database sees it."""

        if not operation_id or not isinstance(operation_id, str):
            raise LedgerUnavailable("operation_id is required for a prewrite")
        for name, value in (
            ("namespace", namespace),
            ("target_kind", target_kind),
            ("target_id", target_id),
            ("authorization_ref", authorization_ref),
        ):
            if not isinstance(value, str) or not value:
                raise LedgerUnavailable(f"{name} is required for a prewrite")
        if type(target_version) is not int or target_version < 0:
            raise LedgerUnavailable("target_version must be a non-negative integer")
        if type(privacy_epoch) is not int or privacy_epoch < 1:
            raise LedgerUnavailable("privacy_epoch must be a positive integer")
        return self._append(
            {
                "op": OP_PREWRITE,
                "operation_id": operation_id,
                "namespace": namespace,
                "target_kind": target_kind,
                "target_id": target_id,
                "target_version": target_version,
                "authorization_ref": authorization_ref,
                "privacy_epoch": privacy_epoch,
                "created_at": self._now(),
                "status": STATUS_PREWRITTEN,
            }
        )

    def confirm(self, operation_id: str) -> None:
        """Mark a prewrite as committed to the database (append-only)."""

        self._append({"op": OP_CONFIRM, "operation_id": operation_id, "created_at": self._now()})

    # -------------------------------------------------------------- views

    def confirmed_operations(self) -> set[str]:
        return {entry.operation_id for entry in self.entries() if entry.op == OP_CONFIRM}

    def prewrites(self) -> list[LedgerEntry]:
        confirmed = self.confirmed_operations()
        return [
            entry
            for entry in self.entries()
            if entry.is_prewrite and entry.operation_id not in confirmed
        ]

    def max_epoch(self, namespace: str) -> int:
        epochs = [
            entry.privacy_epoch
            for entry in self.entries()
            if entry.is_prewrite and entry.namespace == namespace
        ]
        if not epochs:
            baseline = self.baseline_epoch(namespace)
            return 0 if baseline is None else baseline
        return max(epochs)

    def namespaces(self) -> set[str]:
        names = {entry.namespace for entry in self.entries() if entry.is_prewrite}
        names.update(self._load_baseline())
        return names

    def head_hash(self) -> str:
        entries = self.entries()
        return entries[-1].digest if entries else GENESIS

    def head_seq(self) -> int:
        return len(self.entries())

    def verify(self) -> None:
        """Full chain + seal check; raises ``LedgerCorrupt``/``LedgerUnavailable``."""

        self.entries()
