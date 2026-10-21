"""Child process used by the PCS2-008 crash-window tests.

Usage:
    python cognition_child.py <src-dir> <mode> <db> <namespace> <event-id> <marker>

``mode`` is ``run`` (one full ``run_step``) or ``build`` (Context only, no
commit).  The child writes a small JSON result to *marker* and then sleeps, so
the parent can kill it at a known point instead of relying on wall-clock
sleeps.  It exits non-zero if the work raises before the marker.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

src_dir, mode, db_path, namespace, event_id, marker = sys.argv[1:7]
sys.path.insert(0, src_dir)

from quintessence_pcs.domains.refs import EventRef  # noqa: E402
from quintessence_pcs.runtime import CognitionRuntime, ContextService  # noqa: E402
from quintessence_pcs.store import CognitionStore  # noqa: E402


def main() -> None:
    store = CognitionStore.open(db_path)
    try:
        if mode == "run":
            result = CognitionRuntime(store).run_step(
                namespace=namespace, trigger_event_id=event_id
            )
            payload = {
                "outcome": result.outcome,
                "context_id": result.context_id,
                "error_code": result.error_code,
                "belief_id": (
                    result.belief_ref.record_id if result.belief_ref else None
                ),
                "state_revision": result.state_revision,
            }
        elif mode == "build":
            context = ContextService(store).build_context(
                namespace=namespace,
                trigger_ref=EventRef(
                    namespace=namespace, event_id=event_id
                ).to_dict(),
            )
            payload = {"outcome": "built", "context_id": context.context_id}
        elif mode == "reconcile":
            # CognitionStore.open() performs the one-shot R1 recovery.  This
            # mode proves a *new process* reaches the terminal state and exits
            # immediately instead of sleeping.
            rows = store.work_progress_rows()
            payload = {
                "outcome": "reconciled",
                "skipped": sum(1 for row in rows if row.outcome == "skipped"),
                "total": len(rows),
            }
            Path(marker).write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
                newline="\n",
            )
            store.close()
            return
        else:
            raise SystemExit(f"unknown mode {mode!r}")
    except BaseException as exc:  # noqa: BLE001 - report it to the parent
        payload = {
            "outcome": "exception",
            "error": f"{type(exc).__name__}: {exc}",
        }
    Path(marker).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8", newline="\n"
    )
    time.sleep(30)


if __name__ == "__main__":
    main()
