"""Start a second sidecar directly and prove it reports DATA_LOCKED.

This is a verification helper, not part of the installed application.  It uses
the test harness only for the frame codec and process plumbing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
for _path in (REPO, REPO / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from tests.desktop.support import BridgeProcess  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    process = BridgeProcess(config_path=args.config, executable=args.exe)
    try:
        process.wait_ready()
        info = process.request("app.info")["result"]
        locked = not info["host"]["ready"] and info["host"]["reason_code"] == "DATA_LOCKED"
        reply = process.request(
            "consult.interact",
            {
                "session_id": info["session_id"],
                "question": "第二个 sidecar 的验证问题",
                "confirmed_destination_id": (
                    info["destination"]["destination_id"]
                    if info["destination"] is not None
                    else None
                ),
            },
        )
        code = reply.get("error", {}).get("code")
        print(
            json.dumps(
                {
                    "second_sidecar_locked": locked,
                    "reason_code": info["host"]["reason_code"],
                    "consult_error": code,
                    "passed": locked and code == "DATA_LOCKED",
                },
                ensure_ascii=False,
            )
        )
        return 0 if locked and code == "DATA_LOCKED" else 1
    finally:
        process.close()


if __name__ == "__main__":
    raise SystemExit(main())
