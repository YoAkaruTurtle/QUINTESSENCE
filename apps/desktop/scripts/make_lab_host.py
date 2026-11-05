"""Generate a synthetic Lab host configuration outside the source checkout.

This is a Lab helper, not a production default: the desktop application starts
not-ready when no private host configuration is supplied.  Every value written
here is synthetic and local; no real provider, Persona or Memory content is
involved.

Optional sections are opt-in and additive.  A default invocation produces
exactly the same document as before the RG01 extension (proved by regenerating
and diffing against the pre-extension output).  ``--memory-bridge`` adds the
memory bridge block, ``--script-file`` replaces the built-in scripted
transport, ``--tool-mode`` records the manifest tool mode, ``--web`` enables the
RG08 web tools (off by default), and ``--budget-units``/``--output-reserve``
override the request budget.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

NAMESPACE = "example.test.desktop.lab"
PROVIDER = "synthetic"
MODEL = "scripted-collab-v1"
KEY_ENV = "QUINTESSENCE_DESKTOP_LAB_MODEL_KEY"

#: Host purposes every Lab configuration must carry.
BASE_PURPOSES = ["consult", "reasoning", "review"]

#: The memory bridge needs its own purpose; without it the host config is
#: refused (host_config._memory_bridge).
MEMORY_BRIDGE_PURPOSE = "memory.bridge"

#: Tool modes accepted by the model manifest (runtime.manifest).
TOOL_MODES = ("tool_free", "agent")


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


def load_script(path: Path) -> list[dict[str, object]]:
    """Read one explicit scripted-transport script.

    The host config validates the entries again; this check only fails early
    with a readable message instead of writing an unusable Lab config.
    """

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"cannot read --script-file {path}: {exc}") from exc
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise SystemExit(f"--script-file {path} is not strict JSON: {exc}") from exc
    if not isinstance(value, list) or not value:
        raise SystemExit(f"--script-file {path} must hold a non-empty JSON list")
    entries: list[dict[str, object]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise SystemExit(f"--script-file {path}[{index}] must be an object")
        if "action" not in item:
            raise SystemExit(f"--script-file {path}[{index}] has no action")
        entries.append(dict(item))
    return entries


def memory_bridge_section(args: argparse.Namespace) -> dict[str, object]:
    """Build the memory bridge block from explicit paths only.

    ``data_directory`` stays under ``--data-dir`` so the whole Lab instance
    keeps its data in one directory, and the host config refuses a bridge whose
    data directory is outside ``store.root``.
    """

    data_directory = Path(args.memory_dir or (Path(args.data_dir) / "memory"))
    return {
        "enabled": True,
        "python_executable": str(args.memory_python),
        "entry": str(args.memory_entry),
        "package_path": str(args.memory_package_path),
        "data_directory": str(data_directory),
        "agent_id": args.memory_agent_id,
        "installation_id": args.memory_installation_id,
        "scope_id": args.memory_scope_id,
        "session_id": args.memory_session_id,
        "permissions": ["memory_bridge"],
    }


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
    if args.tool_mode is not None:
        manifest["tool_mode"] = args.tool_mode
    query_plan: dict[str, object] | None = {
        "kind": "belief_current",
        "claim_types": ["capability"],
        "scope": {"scene": "lab.synthetic"},
        "limit": 5,
    }
    if args.mode == "incomplete":
        query_plan = None
    purposes = list(BASE_PURPOSES)
    if args.memory_bridge:
        purposes.append(MEMORY_BRIDGE_PURPOSE)
    script = (
        load_script(args.script_file)
        if args.script_file is not None
        else script_for(args.mode, args.reply, args.delay_ms)
    )
    document: dict[str, object] = {
        "schema": "desktop.host.v1",
        "app": {
            "profile": "lab",
            "label": "QUINTESSENCE Lab",
            **({"ai_display_name": args.ai_name} if args.ai_name else {}),
        },
        "store": {
            "root": str(Path(args.data_dir)),
            "database": str(Path(args.data_dir) / args.database),
        },
        "budget": {
            "request_budget_units": args.budget_units,
            "output_reserve_units": args.output_reserve,
        },
        "host": {
            "namespace": args.namespace,
            "host_instance": "host:desktop-lab",
            "operator": "operator:desktop-lab",
            "access_ref": "authorization:host:desktop-lab",
            "privacy_classes": ["private"],
            "purposes": purposes,
            "disclose": True,
            "retention": "standard",
            "policy_ref": "policy:collab.v1",
        },
        "review": {
            "permissions": [
                "work.scan",
                "experience.read",
                "review.generate",
                "review.approve",
                "review.reject",
                "result.read",
            ]
        },
        "consult": {"query_plan": query_plan, "max_question_chars": 8000},
        "destination": {
            "provider": PROVIDER,
            "model": MODEL,
            "model_protocol": args.model_protocol,
            "external": not args.local,
            "manifest": manifest,
            "transport": {
                "kind": "scripted",
                "script": script,
                "delay_ms": args.delay_ms,
            },
        },
    }
    if args.memory_bridge:
        document["memory_bridge"] = memory_bridge_section(args)
    if args.web:
        # RG08: an explicit Lab switch.  Nothing is written by default, so a
        # configuration produced before this option is byte-identical.
        document["web"] = {"enabled": True}
    if args.persona_expected_id:
        app = document["app"]
        expected: dict[str, object] = {"profile_id": args.persona_expected_id}
        if args.persona_expected_version:
            expected["profile_version"] = args.persona_expected_version
        app["persona_expected"] = expected
        speaker: dict[str, object] = {"identity": args.speaker_identity or "unknown"}
        if args.speaker_name:
            speaker["display_name"] = args.speaker_name
        app["speaker"] = speaker
    elif args.speaker_identity or args.speaker_name:
        parser_note = "--speaker-* requires --persona-expected-id"
        raise SystemExit(parser_note)
    if args.persona_control:
        document["app"]["persona_control"] = {
            "publish": "publish" in args.persona_control,
            "activate": "activate" in args.persona_control,
        }
    if args.persona_examples_max_items is not None:
        document.setdefault("conversation", {})[
            "persona_examples_max_items"
        ] = args.persona_examples_max_items
    if args.persona_examples_max_bytes is not None:
        document.setdefault("conversation", {})[
            "persona_examples_max_bytes"
        ] = args.persona_examples_max_bytes
    document["lab"] = {
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
    }
    return document


def _require_memory_paths(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Refuse a half-configured memory bridge before writing any file."""

    required = {
        "--memory-python": args.memory_python,
        "--memory-entry": args.memory_entry,
        "--memory-package-path": args.memory_package_path,
        "--memory-agent-id": args.memory_agent_id,
        "--memory-installation-id": args.memory_installation_id,
        "--memory-scope-id": args.memory_scope_id,
        "--memory-session-id": args.memory_session_id,
    }
    missing = sorted(name for name, value in required.items() if not value)
    if missing:
        parser.error(
            "--memory-bridge requires " + ", ".join(missing)
        )


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
    parser.add_argument(
        "--model-protocol",
        choices=("collab.v1", "collab.v2"),
        default="collab.v1",
        help="destination.model_protocol; v2 enables the structured tool loop",
    )
    parser.add_argument("--local", action="store_true")
    parser.add_argument(
        "--ai-name",
        default=None,
        help="optional AI display name for this private Lab config",
    )
    # --- RG01 additions: every one of them is opt-in ---
    parser.add_argument(
        "--namespace",
        default=NAMESPACE,
        help="override the synthetic namespace (default keeps the C12b one)",
    )
    parser.add_argument(
        "--tool-mode",
        default=None,
        choices=TOOL_MODES,
        help="write manifest.tool_mode; omitted by default (tool_free)",
    )
    parser.add_argument(
        "--script-file",
        default=None,
        type=Path,
        help="JSON list replacing the built-in scripted transport script",
    )
    parser.add_argument("--budget-units", default=200_000, type=int)
    parser.add_argument("--output-reserve", default=4096, type=int)
    parser.add_argument(
        "--web",
        action="store_true",
        help=(
            "write web.enabled=true; the host then publishes web.search/"
            "web.fetch and reads the key from DEEPSEEK_API_KEY (never written "
            "to the file). Omitted by default: no network access"
        ),
    )
    parser.add_argument("--memory-bridge", action="store_true")
    parser.add_argument("--memory-dir", default=None, type=Path)
    parser.add_argument("--memory-python", default=None, type=Path)
    parser.add_argument("--memory-entry", default=None, type=Path)
    parser.add_argument("--memory-package-path", default=None, type=Path)
    parser.add_argument("--memory-agent-id", default=None)
    parser.add_argument("--memory-installation-id", default=None)
    parser.add_argument("--memory-scope-id", default=None)
    parser.add_argument("--memory-session-id", default=None)
    # --- RG06 additions: persona Lab fixtures are opt-in ---
    parser.add_argument(
        "--persona-expected-id",
        default=None,
        help="write app.persona_expected (startup contract) for this profile id",
    )
    parser.add_argument(
        "--persona-expected-version",
        default=None,
        help="optional profile body version inside app.persona_expected",
    )
    parser.add_argument(
        "--speaker-identity",
        default=None,
        choices=("owner", "known_other", "unknown"),
        help="app.speaker.identity used with --persona-expected-id",
    )
    parser.add_argument(
        "--speaker-name",
        default=None,
        help="optional app.speaker.display_name (never for identity=unknown)",
    )
    parser.add_argument(
        "--persona-control",
        action="append",
        default=[],
        choices=("publish", "activate"),
        help="enable profile.publish/profile.activate in this Lab host",
    )
    parser.add_argument("--persona-examples-max-items", default=None, type=int)
    parser.add_argument("--persona-examples-max-bytes", default=None, type=int)
    args = parser.parse_args()
    if args.memory_bridge:
        _require_memory_paths(parser, args)
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
