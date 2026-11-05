"""Check one host configuration's Memory-bridge entry action set.

This is a deployment/acceptance helper, not part of the installed bridge.  It
reads the configured entry source with :mod:`ast` and never imports, executes,
or opens the Memory store, the PCS database or a model.  Use it after repointing
``memory_bridge.entry`` (for example from an older capture/search-only checkout
to the current adapter):

    python apps/desktop/scripts/check_memory_bridge_entry.py --config <host.json>

Exit codes: 0 compatible (or undeclared/unknown), 2 configuration error,
3 a declared entry is missing a required action.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
for _path in (REPO / "src", REPO / "apps" / "desktop" / "sidecar"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from entry_compat import (  # noqa: E402 - path setup must precede this import
    check_entry_compatibility,
    required_actions,
)
from host_config import ConfigError, load_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(json.dumps({"ok": False, "error": exc.code}, ensure_ascii=False))
        return 2

    bridge = config.memory_bridge
    configured = bool(
        bridge is not None
        and bridge.enabled
        and "memory_bridge" in bridge.permissions
    )
    if not configured:
        print(
            json.dumps(
                {
                    "ok": True,
                    "configured": False,
                    "compatible": True,
                    "checked": False,
                    "reason": "memory_bridge_not_configured",
                },
                ensure_ascii=False,
            )
        )
        return 0

    assert bridge is not None
    compat = check_entry_compatibility(
        bridge.entry,
        required_actions(
            recall_enabled=config.memory.recall.enabled,
            time_enabled=config.memory.time.enabled,
        ),
    )
    payload = {
        "ok": True,
        "configured": True,
        "checked": compat["checked"],
        "compatible": compat["compatible"],
        "required": compat["required"],
        "actions": compat["actions"],
        "missing": compat["missing"],
        "reason": compat["reason"],
    }
    if not compat["checked"]:
        payload["warning"] = "entry declares no action set; compatibility is unproven"
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if compat["compatible"] else 3


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
