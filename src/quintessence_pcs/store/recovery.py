"""Restore gate and deletion replay (J10 W1).

A cold restore can restore bytes a deletion barrier had removed.  Two things
guard against that:

* the **independent ledger** (``deletion_ledger``) records every authorized
  deletion before the database commits it, and lives outside the database
  directory;
* the **restore gate** (``restore-gate.json``) records whether this database is
  trusted right now.  It is closed by default, and only opens when the ledger
  and the database agree on the watermarks.

The gate cannot prove that the ledger was not rolled back together with the
database.  That is what the **anchor** is for: a small file kept outside both
the database and the ledger directory, holding the highest watermarks ever
seen.  A restore that puts both the database *and* the ledger behind the anchor
is a same-point rollback and is refused (``restore_same_point_rollback``).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .deletion_ledger import (
    GENESIS,
    OP_ADOPT_BASELINE,
    OP_ADOPT_TARGET,
    PRE_BASELINE_UNPROVABLE,
    DeletionLedger,
    LedgerCorrupt,
    LedgerUnavailable,
)

GATE_FILE = "restore-gate.json"
ANCHOR_FILE = "anchor.json"

STATUS_OPEN = "open"
STATUS_CLOSED = "closed"

#: The restore gate is only as strong as the ledger behind it.  These values
#: say whether deletion safety is actually enforced for this instance, so that
#: "opened because nothing is configured" can never read like "opened because
#: the deletion history was verified".
DELETION_SAFETY_ENFORCED = "enforced"
DELETION_SAFETY_NOT_CONFIGURED = "not_configured"
DELETION_SAFETY_UNPROTECTED = "unprotected"

REASON_PS06_UNPROTECTED = "ps06_unprotected"
REASON_PS06_UNPROTECTED_INVALID = "ps06_unprotected_invalid"

REASON_NOT_CONFIGURED = "ledger_not_configured"
REASON_LEDGER_MISSING = "restore_ledger_missing"
REASON_LEDGER_CORRUPT = "restore_ledger_corrupt"
REASON_LEDGER_STALE = "restore_ledger_stale"
REASON_BEFORE_BASELINE = "restore_before_ledger_baseline"
REASON_SAME_POINT = "restore_same_point_rollback"
REASON_GATE_STALE = "restore_gate_stale"
REASON_ANCHOR_MISSING = "restore_anchor_missing"
REASON_REPLAY_FAILED = "restore_replay_failed"
REASON_CLEANUP_INCOMPLETE = "restore_cleanup_incomplete"
REASON_ALREADY_ADOPTED = "restore_already_adopted"
REASON_ADOPT_INVALID = "restore_adopt_invalid"


class AdoptionRefused(Exception):
    """Raised when an explicit adoption request cannot be honoured."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message or f"adoption refused: {reason}")
        self.reason = reason


class RestoreBlocked(Exception):
    """Raised by every guarded read/write entry while the gate is closed."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message or f"restore gate is closed: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class RestoreDecision:
    status: str
    reason: str | None
    namespaces: Mapping[str, int] = field(default_factory=dict)
    head_hash: str = GENESIS
    adopted: bool = False
    pre_baseline: str | None = None
    deletion_safety: str = DELETION_SAFETY_ENFORCED

    @property
    def is_open(self) -> bool:
        return self.status == STATUS_OPEN

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "namespaces": dict(sorted(self.namespaces.items())),
            "ledger_head_hash": self.head_hash,
            "adopted": self.adopted,
            "pre_baseline_deletions": self.pre_baseline,
            "deletion_safety": self.deletion_safety,
            "updated_at": None,
        }


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def gate_path(db_path: Path) -> Path:
    return Path(db_path).parent / GATE_FILE


def anchor_path(db_path: Path, anchor_dir: Path | None = None) -> Path:
    digest = _sha(str(Path(db_path).resolve()))[:16]
    base = Path(anchor_dir) if anchor_dir is not None else Path.home() / ".quintessence-anchors"
    return base / f"{digest}.json"


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Per-writer temp name plus a short retry: two processes may open the same
    # database at once (the single-writer guard rejects the second one later),
    # and on Windows a rename onto a file the other process just opened fails
    # with a sharing violation for a few milliseconds.
    temp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(temp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temp, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.02)
    finally:
        try:
            temp.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------- open


def _db_epochs(store: Any) -> dict[str, int]:
    rows = store.connection.execute(
        "SELECT namespace, privacy_epoch FROM namespace_state"
    ).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def _ledger_for(store: Any) -> DeletionLedger | None:
    directory = getattr(store, "deletion_ledger_dir", None)
    if directory is None:
        return None
    ledger = getattr(store, "_deletion_ledger", None)
    if ledger is None:
        ledger = DeletionLedger(directory, clock=getattr(store, "_ledger_clock", None))
        store._deletion_ledger = ledger
    return ledger


def _gate_payload(
    status: str,
    reason: str | None,
    head: str,
    epochs: Mapping[str, int],
    adopted: bool,
    pre_baseline: str | None,
    deletion_safety: str = DELETION_SAFETY_ENFORCED,
) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "ledger_head_hash": head,
        "ledger_max_epoch": dict(sorted(epochs.items())),
        "adopted": adopted,
        "pre_baseline_deletions": pre_baseline,
        "deletion_safety": deletion_safety,
        "updated_at": _now(),
    }


def _close(
    store: Any,
    reason: str,
    epochs: Mapping[str, int],
    head: str,
    deletion_safety: str = DELETION_SAFETY_ENFORCED,
) -> RestoreDecision:
    adoption = _adoption_of(store)
    decision = RestoreDecision(
        STATUS_CLOSED,
        reason,
        dict(epochs),
        head,
        adopted=adoption is not None,
        pre_baseline=PRE_BASELINE_UNPROVABLE if adoption is not None else None,
        deletion_safety=deletion_safety,
    )
    store.restore_decision = decision
    write_json(
        gate_path(store.db_path),
        _gate_payload(
            STATUS_CLOSED,
            reason,
            head,
            epochs,
            decision.adopted,
            decision.pre_baseline,
            deletion_safety,
        ),
    )
    return decision


def _now() -> str:
    from .deletion import utc_now_rfc3339

    return utc_now_rfc3339()


def record_anchor(store: Any, namespace: str, epoch: int) -> None:
    """Remember the highest watermark this instance has ever committed.

    The anchor is what makes a same-point rollback visible: if both the
    database and the ledger come back behind it, the pair was restored
    together and cannot prove the deletion history.
    """

    path = anchor_path(store.db_path, getattr(store, "deletion_anchor_dir", None))
    payload = read_json(path) or {"version": 1, "namespaces": {}}
    known = {
        str(key): int(value)
        for key, value in (payload.get("namespaces") or {}).items()
        if type(value) is int
    }
    known[namespace] = max(known.get(namespace, 0), int(epoch))
    write_json(
        path,
        {"version": 1, "namespaces": dict(sorted(known.items())), "updated_at": _now()},
    )


def _adoption_of(store: Any) -> dict[str, Any] | None:
    ledger = _ledger_for(store)
    if ledger is None:
        return None
    try:
        return ledger.adoption()
    except (LedgerUnavailable, LedgerCorrupt):
        return None


def adopt_existing(store: Any, *, adopted_by: str, adopt_reason: str) -> None:
    """One-shot adoption of a database that already carries deletion barriers.

    This is an explicit *relaxation*: it records the current watermark as the
    baseline and states that earlier deletions cannot be proven.  It never
    claims completeness, it refuses a second adoption, and it installs the
    ledger atomically so a crash leaves either no ledger or a whole one.
    """

    for name, value in (("adopted_by", adopted_by), ("adopt_reason", adopt_reason)):
        if not isinstance(value, str) or not value.strip():
            raise AdoptionRefused(REASON_ADOPT_INVALID, f"{name} is required")
    ledger = _ledger_for(store)
    if ledger is None:
        raise AdoptionRefused(REASON_ADOPT_INVALID, "no ledger directory is configured")
    if ledger.directory.exists():
        try:
            if ledger.adoption() is not None:
                raise AdoptionRefused(REASON_ALREADY_ADOPTED, "this instance is already adopted")
            ledger.entries()
        except (LedgerUnavailable, LedgerCorrupt):
            pass
        else:
            raise AdoptionRefused(REASON_ALREADY_ADOPTED, "a ledger already exists")

    epochs = _db_epochs(store)
    rows = store.connection.execute(
        "SELECT namespace, target_kind, target_id, target_version, authorization_ref, "
        "status, created_at FROM deletion_targets "
        "ORDER BY namespace, created_at, target_id"
    ).fetchall()
    payloads: list[dict[str, Any]] = [
        {
            "op": OP_ADOPT_BASELINE,
            "operation_id": "adopt-baseline",
            "adopted_by": adopted_by.strip(),
            "adopt_reason": adopt_reason.strip(),
            "pre_baseline_deletions": PRE_BASELINE_UNPROVABLE,
            "baselines": dict(sorted(epochs.items())),
        }
    ]
    for row in rows:
        namespace = str(row[0])
        payloads.append(
            {
                "op": OP_ADOPT_TARGET,
                "operation_id": f"adopt-target:{namespace}:{row[2]}",
                "namespace": namespace,
                "target_kind": str(row[1]),
                "target_id": str(row[2]),
                "target_version": int(row[3]),
                "authorization_ref": str(row[4]),
                "privacy_epoch": int(epochs.get(namespace, 0)),
                "created_at": str(row[6]),
                "status": str(row[5]),
            }
        )
    # Anchor first: a crash after this still leaves the instance closed (no
    # ledger), and a re-run can finish the job.  The ledger directory is then
    # installed in one atomic rename.
    for namespace, epoch in epochs.items():
        record_anchor(store, namespace, epoch)
    ledger.install_adoption(
        payloads=payloads,
        baselines=epochs,
        adopted_by=adopted_by.strip(),
        adopt_reason=adopt_reason.strip(),
    )
    ledger._cache = None


def _open_unprotected(store: Any, request: Any) -> RestoreDecision:
    """Open without a ledger only when the operator says so, in writing.

    An unprotected run cannot record what it deletes, so it must never be the
    default and must never be entered on a database that already carries a
    deletion watermark.  Deletions themselves are refused while it is active
    (see ``DeletionMixin.request_deletion``): otherwise every deletion would
    accumulate history that no future restore could ever prove.
    """

    epochs = _db_epochs(store)
    by = str(request.get("by", "")).strip() if isinstance(request, Mapping) else ""
    reason = str(request.get("reason", "")).strip() if isinstance(request, Mapping) else ""
    if not by or not reason:
        return _close(
            store,
            REASON_PS06_UNPROTECTED_INVALID,
            epochs,
            GENESIS,
            deletion_safety=DELETION_SAFETY_NOT_CONFIGURED,
        )
    # A restored database may have forgotten its own deletions. An explicit
    # experiment must not hide surviving evidence in the anchor or ledger.
    witness_path = anchor_path(store.db_path, getattr(store, "deletion_anchor_dir", None))
    witness = read_json(witness_path)
    if witness_path.exists() and (
        witness is None
        or witness.get("version") != 1
        or not isinstance(witness.get("namespaces"), dict)
        or any(type(v) is not int or v < 0 for v in witness["namespaces"].values())
    ):
        return _close(store, REASON_ANCHOR_MISSING, epochs, GENESIS,
                      deletion_safety=DELETION_SAFETY_NOT_CONFIGURED)
    witnessed_epochs = {} if witness is None else witness["namespaces"]
    ledger = _ledger_for(store)
    ledger_history = False
    if ledger is not None and ledger.directory.exists():
        try:
            ledger.verify()
            ledger_history = any(ledger.max_epoch(ns) > 0 for ns in ledger.namespaces())
        except (LedgerUnavailable, LedgerCorrupt):
            return _close(store, REASON_LEDGER_CORRUPT, epochs, GENESIS,
                          deletion_safety=DELETION_SAFETY_NOT_CONFIGURED)
    if (any(epoch > 0 for epoch in epochs.values())
            or any(epoch > 0 for epoch in witnessed_epochs.values())
            or ledger_history):
        # There is a deletion history to protect; an unprotected run would be
        # exactly the silent downgrade this gate exists to prevent.
        return _close(
            store,
            REASON_LEDGER_MISSING,
            epochs,
            GENESIS,
            deletion_safety=DELETION_SAFETY_NOT_CONFIGURED,
        )
    decision = RestoreDecision(
        STATUS_OPEN,
        REASON_PS06_UNPROTECTED,
        {},
        GENESIS,
        deletion_safety=DELETION_SAFETY_UNPROTECTED,
    )
    store.restore_decision = decision
    write_json(
        gate_path(store.db_path),
        _gate_payload(
            STATUS_OPEN,
            REASON_PS06_UNPROTECTED,
            GENESIS,
            {},
            False,
            None,
            DELETION_SAFETY_UNPROTECTED,
        ),
    )
    return decision


def bootstrap(store: Any) -> RestoreDecision:
    """Evaluate the restore gate at ``open`` time (never raises to the caller)."""

    unprotected = getattr(store, "ps06_unprotected", None)
    if unprotected is not None:
        return _open_unprotected(store, unprotected)

    ledger = _ledger_for(store)
    if ledger is None:
        # A missing ledger is never a licence to run.  "The database carries no
        # deletion watermark" is not evidence that nothing was ever deleted: a
        # restored old snapshot forgets its own deletions, and an old host
        # configuration forgets the ledger.  The anchor is the only witness
        # that lives outside both, so its presence closes the gate even when
        # the database looks untouched.
        epochs = _db_epochs(store)
        return _close(
            store,
            REASON_LEDGER_MISSING,
            epochs,
            GENESIS,
            deletion_safety=DELETION_SAFETY_NOT_CONFIGURED,
        )

    epochs = _db_epochs(store)
    request = getattr(store, "deletion_adopt", None)
    if request:
        try:
            adopt_existing(
                store,
                adopted_by=str(request.get("by", "")),
                adopt_reason=str(request.get("reason", "")),
            )
        except AdoptionRefused as refused:
            return _close(store, refused.reason, epochs, GENESIS)
        except (LedgerUnavailable, LedgerCorrupt):
            return _close(store, REASON_LEDGER_CORRUPT, epochs, GENESIS)
        epochs = _db_epochs(store)
        ledger = _ledger_for(store)
        if ledger is None:  # pragma: no cover - configuration changed under us
            return _close(store, REASON_LEDGER_MISSING, epochs, GENESIS)
    try:
        ledger.verify()
    except LedgerUnavailable:
        # A brand-new database may create its ledger; one that already carries
        # watermarks cannot -- that is a missing ledger, not a fresh start.
        if epochs and any(value > 0 for value in epochs.values()):
            return _close(store, REASON_LEDGER_MISSING, epochs, GENESIS)
        try:
            ledger.initialize()
        except (LedgerUnavailable, LedgerCorrupt):
            return _close(store, REASON_LEDGER_MISSING, epochs, GENESIS)
    except LedgerCorrupt:
        return _close(store, REASON_LEDGER_CORRUPT, epochs, GENESIS)

    # A fresh database needs a baseline before it may delete anything; the
    # baseline is the current epoch so nothing older can be claimed.
    for namespace, epoch in epochs.items():
        try:
            ledger.ensure_ready(namespace, epoch)
        except (LedgerUnavailable, LedgerCorrupt):
            return _close(store, REASON_LEDGER_CORRUPT, epochs, ledger.head_hash())

    anchor = read_json(anchor_path(store.db_path, getattr(store, "deletion_anchor_dir", None)))
    anchor_epochs = {} if anchor is None else {
        str(k): int(v) for k, v in (anchor.get("namespaces") or {}).items() if type(v) is int
    }
    first_run = not epochs or all(v == 0 for v in epochs.values())
    if anchor is None and not first_run:
        return _close(store, REASON_ANCHOR_MISSING, epochs, ledger.head_hash())

    db_total = sum(epochs.values())
    max_total = sum(ledger.max_epoch(ns) for ns in set(epochs) | ledger.namespaces())
    anchor_total = sum(anchor_epochs.values())

    # Same-point rollback: both sides sit behind what this instance has already
    # seen.  Refuse -- a human cannot vouch for a deletion history.
    if anchor is not None and db_total < anchor_total and max_total < anchor_total:
        return _close(store, REASON_SAME_POINT, epochs, ledger.head_hash())

    for namespace, epoch in list(epochs.items()):
        baseline = ledger.baseline_epoch(namespace)
        if baseline is not None and epoch < baseline:
            return _close(store, REASON_BEFORE_BASELINE, epochs, ledger.head_hash())
        ledger_max = ledger.max_epoch(namespace)
        if epoch > ledger_max:
            # The database remembers a deletion the ledger never saw: the ledger
            # is behind (or was rolled back) and cannot be trusted.
            return _close(store, REASON_LEDGER_STALE, epochs, ledger.head_hash())

    replay_counts = replay_missing(store, ledger, epochs)
    if replay_counts is None:
        return _close(store, REASON_REPLAY_FAILED, epochs, ledger.head_hash())

    epochs_after = _db_epochs(store)
    for namespace in set(epochs_after) | ledger.namespaces():
        if epochs_after.get(namespace, 0) < ledger.max_epoch(namespace):
            return _close(store, REASON_REPLAY_FAILED, epochs_after, ledger.head_hash())

    # Replay must leave no replayed target "pending cleanup": a barrier without
    # the payload cleared still holds the bytes the deletion removed.  Only the
    # namespaces this bootstrap replayed are checked -- a pending cleanup left
    # over from normal operation is a retryable state, not a restore failure.
    replayed = {ns for ns, count in (replay_counts or {}).items() if count}
    if replayed:
        try:
            store.cleanup_deletions()
        except Exception:  # noqa: BLE001 - a cleanup failure keeps the gate closed
            return _close(store, REASON_CLEANUP_INCOMPLETE, epochs_after, ledger.head_hash())
        for namespace in replayed:
            pending = store.connection.execute(
                "SELECT COUNT(*) FROM deletion_targets "
                "WHERE namespace = ? AND status != 'cleaned'",
                (namespace,),
            ).fetchone()
            if pending is not None and int(pending[0]) > 0:
                return _close(
                    store, REASON_CLEANUP_INCOMPLETE, epochs_after, ledger.head_hash()
                )

    gate = read_json(gate_path(store.db_path))
    head = ledger.head_hash()
    if gate is not None and gate.get("status") == STATUS_OPEN:
        recorded = gate.get("ledger_head_hash")
        if recorded != head:
            # Either normal deletions advanced the head (ledger is at least as
            # new as the gate: fine, adopt it) or the ledger rolled back behind
            # an open gate (refuse).
            gate_epochs = {
                str(k): int(v)
                for k, v in (gate.get("ledger_max_epoch") or {}).items()
                if type(v) is int
            }
            if sum(gate_epochs.values()) > sum(ledger.max_epoch(ns) for ns in gate_epochs):
                return _close(store, REASON_GATE_STALE, epochs_after, head)

    adoption = _adoption_of(store)
    decision = RestoreDecision(
        STATUS_OPEN,
        None,
        dict(epochs_after),
        head,
        adopted=adoption is not None,
        pre_baseline=PRE_BASELINE_UNPROVABLE if adoption is not None else None,
    )
    store.restore_decision = decision
    write_json(
        gate_path(store.db_path),
        _gate_payload(STATUS_OPEN, None, head, epochs_after, decision.adopted, decision.pre_baseline),
    )
    merged = dict(anchor_epochs)
    for namespace, epoch in epochs_after.items():
        merged[namespace] = max(merged.get(namespace, 0), epoch, ledger.max_epoch(namespace))
    write_json(
        anchor_path(store.db_path, getattr(store, "deletion_anchor_dir", None)),
        {"version": 1, "namespaces": dict(sorted(merged.items())), "updated_at": _now()},
    )
    return decision


def replay_missing(store: Any, ledger: DeletionLedger, epochs: Mapping[str, int]) -> dict[str, int] | None:
    """Re-apply every ledger prewrite the restored database has not seen.

    Returns per-namespace applied counts, or ``None`` when replay could not be
    completed (the caller then keeps the gate closed).
    """

    applied: dict[str, int] = {}
    namespaces = set(epochs) | ledger.namespaces()
    try:
        entries = ledger.entries()
    except (LedgerUnavailable, LedgerCorrupt):
        return None
    for namespace in sorted(namespaces):
        current = int(epochs.get(namespace, 0))
        pending = [
            entry
            for entry in entries
            if entry.is_prewrite
            and entry.namespace == namespace
            and entry.privacy_epoch > current
        ]
        count = 0
        for entry in sorted(pending, key=lambda item: (item.privacy_epoch, item.seq)):
            try:
                outcome = store.replay_deletion(
                    namespace=entry.namespace,
                    target_kind=entry.target_kind,
                    target_id=entry.target_id,
                    target_version=entry.target_version,
                    authorization_ref=entry.authorization_ref,
                    privacy_epoch=entry.privacy_epoch,
                )
            except Exception:  # noqa: BLE001 - replay failures keep the gate closed
                return None
            count += 1
        applied[namespace] = count
    return applied
