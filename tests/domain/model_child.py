"""Child-process entry point for the PCS2-009 budget crash test.

The parent kills this process after the fake provider has received the request.
The point is not what the child prints, but what the committed database still
says: an attempt was reserved and never returned.

This is a test helper, not a test case.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC_DIR = HERE.parents[1] / "src"
for _path in (HERE, SRC_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from quintessence_pcs.runtime import (  # noqa: E402
    CognitionRuntime,
    DirectHttpModel,
)
from quintessence_pcs.runtime.manifest import ModelManifest  # noqa: E402
from quintessence_pcs.store import CognitionStore  # noqa: E402


def main() -> int:
    payload = json.loads(sys.argv[1])
    store = CognitionStore.open(payload["db"])
    try:
        manifest = ModelManifest.from_mapping(payload["manifest"])
        model = DirectHttpModel(store, manifest)
        runtime = CognitionRuntime(store, model=model)
        result = runtime.run_step(
            namespace=payload["namespace"],
            trigger_event_id=payload["event_id"],
        )
        # The parent usually kills this process before the line is reached.
        print(result.outcome, flush=True)
    finally:
        store.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
