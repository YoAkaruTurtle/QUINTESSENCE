"""Generate a synthetic Lab host configuration outside the source checkout.

This is a Lab helper, not a production default: the desktop application starts
not-ready when no private host configuration is supplied.  Every value written
here is synthetic and local; no real provider, Persona or Memory content is
involved.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

NAMESPACE = "example.test.desktop.lab"
PROVIDER = "synthetic"
MODEL = "scripted-collab-v1"
KEY_ENV = "QUINTESSENCE_DESKTOP_LAB_MODEL_KEY"


def script_for(mode: str, reply: str, delay_ms: int) -> list[dict[str, object]]:
    if mode == "answered":
        return [
            {"action": "query", "brief_reason": "deterministic belief lookup"},
            {"action": "none", "reply": reply},
        ]
    if mode == "waiting":
        return [{"action": "wait", "brief_reason": "Lab waiting fixture"}]
    if mode == "failed":
        return [{"action": "none"}]
    if mode == "incomplete":
        return [{"action": "query", "brief_reason": "Lab query without a host plan"}]
    raise SystemExit(f"unknown mode {mode!r}")


def build(args: argparse.Namespace) -> dict[str, object]:
    manifest = {
        "provider": PROVIDER,
        "base_url": "http://127.0.0.1:9/v1",
        "model": MODEL,
        "api_key_env": KEY_ENV,
        "max_calls": 64,
        "max_output_tokens_per_call": 512,
        "max_wall_seconds": 120,
        "thinking": "disabled",
        "temperature": 0,
    }
    query_plan: dict[str, object] | None = {
        "kind": "belief_current",
        "claim_types": ["capability"],
        "scope": {"scene": "lab.synthetic"},
        "limit": 5,
    }
    if args.mode == "incomplete":
        query_plan = None
    return {
        "schema": "desktop.host.v1",
        "app": {
            "profile": "lab",
            "label": "QUINTESSENCE Lab",
            **({"ai_display_name": args.ai_name} if args.ai_name else {}),
        },
        "store": {"database": str(Path(args.data_dir) / args.database)},
        "host": {
            "namespace": NAMESPACE,
            "host_instance": "host:desktop-lab",
            "operator": "operator:desktop-lab",
            "access_ref": "authorization:host:desktop-lab",
            "privacy_classes": ["private"],
            "purposes": ["consult", "reasoning"],
            "disclose": True,
            "retention": "standard",
            "policy_ref": "policy:collab.v1",
        },
        "consult": {"query_plan": query_plan, "max_question_chars": 8000},
        "destination": {
            "provider": PROVIDER,
            "model": MODEL,
            "model_protocol": "collab.v1",
            "external": not args.local,
            "manifest": manifest,
            "transport": {
                "kind": "scripted",
                "script": script_for(args.mode, args.reply, args.delay_ms),
                "delay_ms": args.delay_ms,
            },
        },
        "lab": {
            "seed": True,
            "profile_id": "example.synthetic.desktop.lab",
            "profile_version": "0.0.1",
            "authorization_ref": "authorization:desktop-lab-model",
            "belief": {
                "claim": "capability:lab-file-read",
                "claim_type": "capability",
                "strength": "strong",
                "scope": {"scene": "lab.synthetic"},
                "reason": "synthetic desktop Lab fixture; not a real belief",
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--database", default="desktop-lab.sqlite")
    parser.add_argument(
        "--mode",
        default="answered",
        choices=("answered", "waiting", "failed", "incomplete"),
    )
    parser.add_argument(
        "--reply",
        default=(
            "合成回答：本周可披露材料中只有一条能力条目 capability:lab-file-read。"
            "这条回答由真实的 ConsultFacade、Context、确定性查询与 Attempt 路径生成，"
            "仅用于 C12b 桌面验收，不包含任何真实私人内容。"
        ),
    )
    parser.add_argument("--delay-ms", default=0, type=int)
    parser.add_argument("--local", action="store_true")
    parser.add_argument(
        "--ai-name",
        default=None,
        help="optional AI display name for this private Lab config",
    )
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    document = build(args)
    args.output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(str(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
