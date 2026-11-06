"""Folder-owned instance preparation. Unknown/missing private state fails closed.

Only this trusted-host entry creates components. Runtime modules continue to own
Persona, conversations, Cognition, memory and PS06; no new SQL migration is used.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from locking import DataInstanceLock

MANIFEST = "instance.json"
CONFIG = "instance-host.json"
SCHEMA = "quintessence.instance.v1"
README = """# 完善这个 AI 实例的人格

这个文件夹保存此实例的身份、人格、聊天历史、记忆和认识。关闭软件后，复制整个文件夹即可迁移；不要只复制数据库，也不要单独删除安全记录。

你可以先直接聊天。没有自定义人格时，使用模型本身的默认表达风格，不必先写设定。

逐步完善人格的方法：
1. 先写清希望助手如何称呼你、使用什么语言，以及你希望它遵守的几条原则。
2. 再说明表达习惯，例如回答长短、解释深度、何时需要追问。避免互相矛盾的要求。
3. 加入几组你满意的用户提问和助手回答，帮助表达风格保持一致。示例对话不是实际记忆。
4. 在软件的人格设置中导入并检查结构化人格文件，确认后发布、激活。保留旧版本，方便比较。

已有的人格文件不会被本次连接覆盖。仅有文字草稿时，它会原样保留，等你整理或导入；有且只有一个完整结构化人格文件时，可作为新实例的初始人格导入。

Memory 保存可召回的经历，Cognition 保存带来源和审核状态的认识；它们不会因为你修改此说明文件而改变。

恢复保护会检查数据库、删除账本和 Anchor 的一致性。但它们随整个文件夹一起回退时，无法可靠检测共同回滚。旧备份可能仍含后来删除的内容，请妥善保管。
"""


class InstanceError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pairs(items):
    out = {}
    for key, value in items:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _read(path: Path) -> dict:
    try:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("oversized metadata")
        value = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_pairs,
                           parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))
        if not isinstance(value, dict):
            raise ValueError("not an object")
        return value
    except (OSError, ValueError) as exc:
        raise InstanceError("INSTANCE_METADATA_INVALID", f"无法读取有效实例元数据：{path.name}；原文件未被替换。") from exc


def _atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".pending")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _inside(root: Path, value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise InstanceError("INSTANCE_PATH_ESCAPE", "实例持久状态必须位于所选文件夹内。")
    current = path
    while current != root:
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise InstanceError("INSTANCE_PATH_ESCAPE", "实例组件不能通过符号链接或目录联接指向其他位置。")
        if current.parent == current:
            break
        current = current.parent
    return path


def _root(value: str | Path) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise InstanceError("INSTANCE_FOLDER_INVALID", "请选择已存在的绝对文件夹路径。")
    if os.path.normcase(str(path)) != os.path.normcase(str(path.resolve())):
        raise InstanceError("INSTANCE_PATH_ESCAPE", "所选文件夹不能经过符号链接或目录联接。")
    return path.resolve()


def _connect(path: Path):
    # This is read-only preflight. Never create a database while identifying it.
    if not path.is_file() or path.stat().st_size == 0:
        raise InstanceError("INSTANCE_COMPONENT_INVALID", "已有数据库为空或不可读；不能创建空数据替代。")
    connection = None
    try:
        connection = sqlite3.connect(str(path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=1")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            connection.close()
            raise InstanceError("INSTANCE_COMPONENT_INVALID", "数据库完整性检查失败；原数据未被重置。")
        return connection
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        raise InstanceError("INSTANCE_COMPONENT_INVALID", "数据库损坏或格式不兼容；原数据未被重置。") from exc


def _pcs_info(path: Path) -> dict:
    connection = _connect(path)
    try:
        version = connection.execute("SELECT MAX(migration_id) FROM schema_migrations").fetchone()[0]
        if version != 17:
            raise InstanceError("INSTANCE_VERSION_UNSUPPORTED", "实例数据库版本不受支持；请先使用兼容版本完成迁移。")
        namespaces = [row[0] for row in connection.execute("SELECT namespace FROM namespace_state")]
        hosts = [row[0] for row in connection.execute("SELECT DISTINCT host_instance FROM collab_conversations")]
        delivered = connection.execute("SELECT COUNT(*) FROM memory_delivery WHERE state IN ('applied','verified') OR external_ref IS NOT NULL").fetchone()[0]
        managed_identity = connection.execute("SELECT COUNT(*) FROM event_journal WHERE event_type IN ('InstanceCreated','InstanceAdopted') AND source_instance='instance-manager'").fetchone()[0]
        return {"namespaces": namespaces, "hosts": hosts, "delivered": delivered, "managed_identity": managed_identity}
    except sqlite3.Error as exc:
        raise InstanceError("INSTANCE_COMPONENT_INVALID", "PCS 数据结构不完整；不能按新实例打开。") from exc
    finally:
        connection.close()


def _memory_info(path: Path) -> dict:
    from scope_recall.core.schema import SCHEMA_VERSION
    connection = _connect(path)
    try:
        if connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            raise InstanceError("INSTANCE_VERSION_UNSUPPORTED", "Memory 版本不兼容；未自动迁移或替换。")
        row = connection.execute("SELECT * FROM instance_meta WHERE singleton=1").fetchone()
        if row is None:
            raise InstanceError("INSTANCE_COMPONENT_INVALID", "Memory 身份元数据丢失；不能重新生成身份。")
        if row["installation_kind"] != "local":
            raise InstanceError("INSTANCE_VERSION_UNSUPPORTED", "此 Memory 为共享存储，不能作为单个独立实例自动接入。")
        scopes = [r[0] for r in connection.execute("SELECT scope_id FROM instance_scopes")]
        if not scopes:
            raise InstanceError("INSTANCE_COMPONENT_INVALID", "Memory 缺少作用域绑定。")
        return {**dict(row), "scopes": scopes}
    except sqlite3.Error as exc:
        raise InstanceError("INSTANCE_COMPONENT_INVALID", "Memory 数据结构不完整。") from exc
    finally:
        connection.close()


def default_document() -> dict:
    from quintessence_pcs.runtime.manifest import DEFAULT_DESKTOP_MAX_CALLS

    return {
        "schema": "desktop.host.v1", "app": {"profile": "production", "label": "QUINTESSENCE", "persona_control": {"publish": True, "activate": True}},
        "store": {}, "budget": {"request_budget_units": 2000000, "output_reserve_units": 4096},
        "host": {"operator": "operator:local", "access_ref": "authorization:host:desktop",
                 "privacy_classes": ["private"], "purposes": ["consult", "reasoning", "review", "memory.bridge", "maintenance"],
                 "disclose": True, "retention": "standard", "policy_ref": "policy:collab.v1"},
        "destination": {"provider": "deepseek", "model": "deepseek-flash", "model_protocol": "collab.v2", "external": True,
                        "manifest": {"provider": "deepseek", "base_url": "https://api.deepseek.com", "model": "deepseek-flash",
                                     "api_key_env": "QUINTESSENCE_DESKTOP_MODEL_KEY", "max_calls": DEFAULT_DESKTOP_MAX_CALLS,
                                     "max_output_tokens_per_call": 8192, "max_wall_seconds": 120, "thinking": "enabled",
                                     "reasoning_effort": "high", "protocol_version": "EXP-01-THINKING-CANDIDATE-v1"}},
        "consult": {"max_question_chars": 8000, "query_plan": {"kind": "belief_current", "claim_types": ["capability"], "scope": {"scene": "quintessence.general"}, "limit": 5}},
        "desktop": {"managed": True, "provision": True},
        "review": {"permissions": ["work.scan", "experience.read", "review.generate", "review.approve", "review.reject", "result.read"]},
        "conversation": {"tools_enabled": True, "sdrpe": {"mode": "off"}},
        "cognition": {"enabled": True, "propose": True},
        "maintenance": {"enabled": False, "permissions": ["maintenance"]},
        "memory": {"timezone": "Asia/Shanghai", "recall": {"enabled": True}, "time": {"enabled": True}},
    }


def _relocate(root: Path, value: str, old_root: str | None) -> str:
    path = Path(value)
    if path.is_absolute() and old_root:
        try:
            path = path.relative_to(Path(old_root))
        except ValueError as exc:
            raise InstanceError("INSTANCE_EXTERNAL_STATE", "配置引用实例外的私人持久状态；请先把原状态完整迁入副本，不能新建替代。") from exc
    return str(_inside(root, path))


def _one(paths: list[Path], label: str) -> Path | None:
    found = list(dict.fromkeys(p for p in paths if p.exists()))
    if len(found) > 1:
        raise InstanceError("INSTANCE_AMBIGUOUS", f"发现多份{label}，无法安全自动选择。")
    return found[0] if found else None


def _new_manifest(root: Path, template: dict | None, legacy_adoption: dict | None = None, component_declaration: dict | None = None) -> tuple[dict, dict]:
    if (root / CONFIG).exists():
        raise InstanceError("INSTANCE_IDENTITY_MISSING", "实例身份描述丢失，但已有实例配置仍在；不能生成替代身份。")
    config_path = _one([root / n for n in (CONFIG, "host.json", "desktop-host.json", "desktop-managed-host.json")], "宿主配置")
    if config_path:
        document = _read(config_path)
    else:
        document = default_document()
        def merge(target, incoming):
            for key, value in incoming.items():
                if isinstance(value, dict) and isinstance(target.get(key), dict):
                    merge(target[key], value)
                else:
                    target[key] = copy.deepcopy(value)
        if template:
            merge(document, template)
        document["destination"]["model_protocol"] = "collab.v2"
    if document.get("schema") != "desktop.host.v1":
        raise InstanceError("INSTANCE_VERSION_UNSUPPORTED", "宿主配置版本不受支持。")
    store = document.setdefault("store", {})
    old_root = store.get("root")
    pcs_candidates = [root / "pcs.sqlite", root / "data/pcs/pcs.sqlite", root / "data/pcs.sqlite"]
    if store.get("database"):
        pcs_candidates.append(Path(_relocate(root, store["database"], old_root)))
    pcs = _one(pcs_candidates, "PCS 数据库")
    info = _pcs_info(pcs) if pcs else None
    if info and info["managed_identity"]:
        raise InstanceError("INSTANCE_IDENTITY_MISSING", "数据库记录了文件夹实例身份，但身份描述已丢失；未生成替代身份。")
    if pcs is None and config_path and store.get("database"):
        raise InstanceError("INSTANCE_COMPONENT_MISSING", "已有配置引用的 PCS 数据库缺失；不能创建空库替代。")
    document.setdefault("cognition", {"enabled": True, "propose": True})
    document.setdefault("memory", {"timezone": "Asia/Shanghai", "recall": {"enabled": True}, "time": {"enabled": True}})
    memory_spec = document.get("memory_bridge") or {}
    if config_path and memory_spec.get("enabled") is False:
        raise InstanceError("INSTANCE_MEMORY_DISABLED", "已有配置明确关闭了 Memory；未自动改写此选择。请先确认启用配置后再接入。")
    memory_candidates = [root / "memory/memory.sqlite3", root / "data/memory/memory.sqlite3"]
    if memory_spec.get("data_directory"):
        memory_candidates.append(Path(_relocate(root, memory_spec["data_directory"], old_root)) / "memory.sqlite3")
    memory = _one(memory_candidates, "Memory 数据库")
    mem_info = _memory_info(memory) if memory else None
    if memory is None and any(p.parent.is_dir() and any(p.parent.iterdir()) for p in memory_candidates):
        raise InstanceError("INSTANCE_COMPONENT_MISSING", "Memory 目录仍有遗留状态但数据库缺失；不能声明从未建立或创建空替代。")
    if memory is None and ((memory_spec.get("enabled") is True) or (info and info["delivered"])):
        raise InstanceError("INSTANCE_COMPONENT_MISSING", "已有实例声明或记录了 Memory，但实际数据丢失；未创建空 Memory。")
    if pcs is None and memory is None and config_path is None:
        allowed = {"persona", "persona-import", "README.md", "pcs.sqlite.lock"}
        if any(p.name not in allowed for p in root.iterdir()):
            raise InstanceError("INSTANCE_UNRECOGNIZED", "文件夹含未识别的现有内容；请选择空文件夹或完整实例文件夹。")
    instance_id = str(uuid.uuid4()) if mem_info is None else str(uuid.uuid5(uuid.NAMESPACE_URL, mem_info["agent_id"] + ":" + mem_info["installation_id"]))
    namespace = document.setdefault("host", {}).get("namespace")
    if info:
        if namespace is None and len(info["namespaces"]) == 1:
            namespace = info["namespaces"][0]
        if namespace not in info["namespaces"]:
            raise InstanceError("INSTANCE_IDENTITY_AMBIGUOUS", "无法唯一恢复实例身份；没有生成替代身份。")
    namespace = namespace or "instance." + instance_id.replace("-", "")
    host = document["host"]
    host["namespace"] = namespace
    if "host_instance" not in host:
        if info and len(info["hosts"]) > 1:
            raise InstanceError("INSTANCE_IDENTITY_AMBIGUOUS", "历史会话包含多个宿主身份，需要明确选择。")
        host["host_instance"] = info["hosts"][0] if info and info["hosts"] else "host:" + instance_id
    pcs = pcs or root / "pcs.sqlite"
    memory = memory or root / "memory/memory.sqlite3"
    store["root"] = str(root)
    store["database"] = pcs.relative_to(root).as_posix()
    for key, default in (("deletion_ledger_dir", pcs.parent / "ledger"), ("deletion_anchor_dir", pcs.parent / "anchor")):
        if store.get(key) is False or document.get("store", {}).get("unprotected"):
            raise InstanceError("INSTANCE_UNPROTECTED", "此实例显式禁用了恢复保护；不能自动改写其安全策略。")
        store[key] = Path(_relocate(root, store[key], old_root)).relative_to(root).as_posix() if store.get(key) else default.relative_to(root).as_posix()
    if info:
        ledger = _inside(root, store["deletion_ledger_dir"])
        anchor = _inside(root, store["deletion_anchor_dir"])
        if not ledger.is_dir() or not anchor.is_dir():
            if not legacy_adoption:
                raise InstanceError("INSTANCE_RECOVERY_REQUIRED", "已有 PCS 的删除账本或 Anchor 缺失，无法证明从未初始化；请恢复原记录或明确办理旧实例接入。原数据未被替换。")
            if ledger.exists() or anchor.exists():
                raise InstanceError("INSTANCE_RECOVERY_REQUIRED", "部分安全状态仍在；显式旧实例接入也不能覆盖或替换现有见证。请恢复缺失部分。")
    unknown = ["memory"] if info and mem_info is None else ["pcs"] if mem_info and info is None else []
    if component_declaration is not None and set(component_declaration["components"]) != set(unknown):
        raise InstanceError("INSTANCE_DECLARATION_INVALID", "组件声明必须只包含当前缺少、且没有既有初始化见证的组件。")
    if unknown and component_declaration is None:
        name = unknown[0]
        raise InstanceError("INSTANCE_" + name.upper() + "_HISTORY_UNKNOWN", "旧实例缺少 " + name + "，现有记录不能证明它从未建立。请恢复原组件；仅在确定从未建立时显式登记，不能用空组件替代丢失的数据。")
    if mem_info:
        for field in ("agent_id", "installation_id"):
            if memory_spec.get(field) not in (None, mem_info[field]):
                raise InstanceError("INSTANCE_IDENTITY_MISMATCH", "Memory 身份与配置不一致。")
    memory_spec.update({"enabled": True, "test_mode": bool(mem_info["test_mode"]) if mem_info else False, "data_directory": memory.parent.relative_to(root).as_posix(),
                        "agent_id": mem_info["agent_id"] if mem_info else memory_spec.get("agent_id", "agent:" + instance_id),
                        "installation_id": mem_info["installation_id"] if mem_info else memory_spec.get("installation_id", "install:" + instance_id),
                        "scope_id": memory_spec.get("scope_id", mem_info["scopes"][0] if mem_info and len(mem_info["scopes"]) == 1 else "scope:" + instance_id),
                        "session_id": memory_spec.get("session_id", "session:" + instance_id), "permissions": ["memory_bridge"]})
    if mem_info and (len(mem_info["scopes"]) != 1 or memory_spec["scope_id"] not in mem_info["scopes"]):
        raise InstanceError("INSTANCE_IDENTITY_AMBIGUOUS", "Memory 作用域无法唯一匹配；原绑定未被改写。")
    document["memory_bridge"] = memory_spec
    if "memory.bridge" not in host.setdefault("purposes", []):
        host["purposes"].append("memory.bridge")
    manifest = {"schema": SCHEMA, "instance_id": instance_id, "namespace": namespace, "host_instance": host["host_instance"],
                "created_at": _now(), "root": old_root or str(root), "config": CONFIG, "config_state": "pending", "initial_config": document,
                "managed": bool(document.get("desktop", {}).get("managed", False)),
                "origin": {"pcs_existed": info is not None, "memory_existed": mem_info is not None},
                "components": {"pcs": {"path": pcs.relative_to(root).as_posix(), "state": "ready" if info else "pending"},
                               "memory": {"path": memory.relative_to(root).as_posix(), "state": "ready" if mem_info else "pending"},
                               "recovery": {"state": "ready" if info and not legacy_adoption else "pending"}},
                "rollback_protection": "instance-local; whole-folder rollback is not reliably detectable"}
    if component_declaration is not None:
        manifest["component_declaration"] = {**copy.deepcopy(component_declaration), "at": _now(), "basis": "operator-confirmed-never-created; not inferred from absent delivery rows"}
    return manifest, document


def _manifest(root: Path, template: dict | None, legacy_adoption: dict | None = None, component_declaration: dict | None = None) -> tuple[dict, dict, bool]:
    path = root / MANIFEST
    if not path.exists():
        manifest, document = _new_manifest(root, template, legacy_adoption, component_declaration)
        return manifest, document, True
    if component_declaration is not None:
        raise InstanceError("INSTANCE_DECLARATION_REFUSED", "已有实例已经登记组件状态；不能用新声明把丢失组件当作从未建立。")
    manifest = _read(path)
    if manifest.get("schema") != SCHEMA:
        raise InstanceError("INSTANCE_VERSION_UNSUPPORTED", "实例描述文件版本不受支持。")
    try:
        uuid.UUID(manifest["instance_id"])
        components = manifest["components"]
        if manifest["config_state"] not in ("pending", "initializing", "ready"):
            raise ValueError("invalid configuration state")
        for name in ("pcs", "memory", "recovery"):
            if components[name]["state"] not in ("pending", "initializing", "ready"):
                raise ValueError("invalid state")
        for name in ("pcs", "memory"):
            component = components[name]
            component_path = _inside(root, component["path"])
            if component["state"] != "pending" and not component_path.is_file():
                raise InstanceError("INSTANCE_COMPONENT_MISSING", f"已初始化的 {name} 数据缺失；未创建空替代。")
            if component_path.exists():
                info = (_pcs_info if name == "pcs" else _memory_info)(component_path)
                if name == "pcs" and manifest["namespace"] not in info["namespaces"]:
                    raise InstanceError("INSTANCE_IDENTITY_MISMATCH", "PCS 数据库与实例身份不一致；未改写身份。")
        config_path = _inside(root, manifest["config"])
        document = _read(config_path) if config_path.exists() else copy.deepcopy(manifest["initial_config"])
        if not config_path.exists() and manifest["config_state"] != "pending":
            raise InstanceError("INSTANCE_COMPONENT_MISSING", "已有实例配置丢失；不能用初始配置覆盖后续设置。")
        if document["host"]["namespace"] != manifest["namespace"] or document["host"]["host_instance"] != manifest["host_instance"]:
            raise InstanceError("INSTANCE_IDENTITY_MISMATCH", "实例身份描述与宿主配置不一致。")
    except (KeyError, TypeError, ValueError) as exc:
        raise InstanceError("INSTANCE_METADATA_INVALID", "实例身份或组件元数据损坏；未重置。") from exc
    return manifest, document, False


def _audit(root: Path, kind: str, **fields) -> None:
    path = root / "instance-history.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": _now(), "kind": kind, **fields}, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _adapter(document: dict) -> None:
    spec = document["memory_bridge"]
    if getattr(sys, "frozen", False):
        spec.update(python_executable=sys.executable, entry="builtin:scope-recall", package_path=str(Path(sys.executable).parent))
    else:
        import scope_recall
        import quintessence_pcs
        spec.update(python_executable=sys.executable,
                    entry=str(Path(quintessence_pcs.__file__).parent / "adapters/scope_recall/cli.py"),
                    package_path=str(Path(scope_recall.__file__).parent.parent))
    spec.pop("adopt_data_directory", None)


def _persona_source(root: Path, document: dict) -> tuple[Path, dict] | None:
    from quintessence_pcs.domains.persona.profiles import validate_profile_body
    from quintessence_pcs.domains.refs import EventRef
    directory = root / "persona"
    candidates = []
    if directory.exists():
        for path in directory.rglob("*.json"):
            _inside(root, path)
            body = _read(path)
            if {"profile_id", "profile_version", "identity_summary", "style"}.issubset(body):
                candidates.append((path, body))
    expected = document.get("app", {}).get("persona_expected")
    if expected:
        candidates = [(p, b) for p, b in candidates if all(b.get(k) == expected.get(k) for k in ("profile_id", "profile_version"))]
        if not candidates:
            raise InstanceError("INSTANCE_PERSONA_MISSING", "配置指定的人格资产不可恢复；未替换为默认人格。")
    if len(candidates) > 1:
        raise InstanceError("INSTANCE_PERSONA_AMBIGUOUS", "人格文件夹含多个结构化版本，请明确保留一个初始版本或指定 persona_expected。")
    if not candidates:
        return None
    path, original = candidates[0]
    body = copy.deepcopy(original)
    reference = {"ref_type": "event", "namespace": document["host"]["namespace"], "event_id": "11111111-1111-4111-8111-111111111111"}
    body["review_ref"] = reference
    try:
        validate_profile_body(body, profile_id=body["profile_id"], review_ref=EventRef.from_value(reference))
    except Exception as exc:
        raise InstanceError("INSTANCE_PERSONA_INVALID", "已有结构化人格格式不兼容；原文件未被改写。") from exc
    document["app"]["persona_expected"] = {k: body[k] for k in ("profile_id", "profile_version")}
    document["app"].setdefault("speaker", {"identity": "unknown"})
    return path, original


def _prepare(root: Path, template: dict | None, legacy_adoption: dict | None = None, component_declaration: dict | None = None) -> dict:
    # Import dependencies before creating any private state.
    from scope_recall.contracts import InstanceBinding
    from scope_recall.core import CoreConfig, MemoryCore
    from quintessence_pcs.store import CognitionStore
    from quintessence_pcs.store.deletion_ledger import DeletionLedger
    from quintessence_pcs.store.recovery import anchor_path
    from quintessence_pcs.domains.memory.instance_binding import ensure_binding

    manifest, document, new = _manifest(root, template, legacy_adoption, component_declaration)
    components = manifest["components"]
    previous_root = manifest["root"]
    store = document["store"]
    old_root = store.get("root", previous_root)
    for key in ("database", "deletion_ledger_dir", "deletion_anchor_dir"):
        store[key] = Path(_relocate(root, store[key], old_root)).relative_to(root).as_posix()
    store["root"] = str(root)
    spec = document["memory_bridge"]
    spec["data_directory"] = Path(_relocate(root, spec["data_directory"], old_root)).relative_to(root).as_posix()
    pcs = _inside(root, store["database"])
    memory = _inside(root, spec["data_directory"]) / "memory.sqlite3"
    if pcs != _inside(root, components["pcs"]["path"]) or memory != _inside(root, components["memory"]["path"]):
        raise InstanceError("INSTANCE_IDENTITY_MISMATCH", "配置中的数据路径与实例组件登记不一致。")
    ledger_dir = _inside(root, store["deletion_ledger_dir"])
    anchor_dir = _inside(root, store["deletion_anchor_dir"])
    expected_anchor = anchor_path(pcs, anchor_dir)
    old_db = Path(previous_root) / components["pcs"]["path"]
    old_anchor = anchor_dir / (hashlib.sha256(str(old_db.resolve()).encode()).hexdigest()[:16] + ".json")
    anchor_source = expected_anchor if expected_anchor.exists() else old_anchor
    if components["recovery"]["state"] != "pending":
        if not ledger_dir.is_dir() or not anchor_source.is_file():
            raise InstanceError("INSTANCE_RECOVERY_REQUIRED", "已初始化的恢复安全记录缺失；不会重建空账本或 Anchor。")
        try:
            ledger = DeletionLedger(ledger_dir)
            ledger.verify()
            if not (ledger_dir / "baseline.json").is_file():
                raise ValueError("missing recovery baseline")
            ledger.baseline_epoch(manifest["namespace"])
        except Exception as exc:
            raise InstanceError("INSTANCE_RECOVERY_INVALID", "删除账本校验失败；原记录未被覆盖。") from exc
        anchor = _read(anchor_source)
        if anchor.get("version") != 1 or not isinstance(anchor.get("namespaces"), dict) or any(type(v) is not int or v < 0 for v in anchor["namespaces"].values()):
            raise InstanceError("INSTANCE_RECOVERY_INVALID", "Anchor 损坏；不能创建空安全记录替代。")
    if legacy_adoption and (not new or components["recovery"]["state"] != "pending"):
        raise InstanceError("INSTANCE_ADOPTION_REFUSED", "显式旧实例接入只能登记尚无安全资产的旧实例，不能重复接入或覆盖现有保护。")
    if memory.exists():
        mem = _memory_info(memory)
        if (mem["agent_id"], mem["installation_id"], mem["scopes"]) != (spec["agent_id"], spec["installation_id"], [spec["scope_id"]]):
            raise InstanceError("INSTANCE_IDENTITY_MISMATCH", "Memory 身份或作用域与实例不一致。")
    elif memory.parent.exists() and any(memory.parent.iterdir()):
        raise InstanceError("INSTANCE_COMPONENT_MISSING", "Memory 目录已有状态但数据库缺失；未初始化替代。")
    persona_source = _persona_source(root, document) if components["pcs"]["state"] == "pending" else None
    preferences = None
    scope_migration = None
    if (root / "ui-preferences.json").exists():
        preferences = _read(root / "ui-preferences.json")
        scopes = preferences.get("conversation_ui", {})
        if not isinstance(scopes, dict):
            raise InstanceError("INSTANCE_PREFERENCES_INVALID", "实例显示设置损坏；未恢复默认值替代。")
        old_scope = hashlib.sha256(b"quintessence.log.instance.v1\0" + manifest["namespace"].encode() + b"\0" + manifest["host_instance"].encode() + b"\0" + os.path.normcase(os.path.normpath(previous_root)).encode()).hexdigest()
        stable_scope = hashlib.sha256(("quintessence.instance.ui.v1:" + manifest["instance_id"]).encode()).hexdigest()
        if old_scope in scopes:
            if stable_scope in scopes and scopes[stable_scope] != scopes[old_scope]:
                raise InstanceError("INSTANCE_PREFERENCES_CONFLICT", "实例显示设置有两份不同的作用域记录；未覆盖任何一份。")
            if stable_scope not in scopes:
                scopes[stable_scope] = copy.deepcopy(scopes[old_scope])
                scope_migration = (old_scope, stable_scope)
    _adapter(document)
    from host_config import _parse
    _parse(document, root / CONFIG, bind_instance=False)
    # Registry is cache-like, but an existing binding must be checked and
    # explicitly moved with an audit trail instead of silently replaced.
    registry = root / "instances.json"
    registry_document = _read(registry) if registry.exists() else None
    if registry_document is not None:
        from host_config import _load_registry
        bindings = _load_registry(registry)
        binding = bindings.get(manifest["namespace"])
        if binding and os.path.normcase(binding["root"]) not in (os.path.normcase(previous_root), os.path.normcase(str(root))):
            raise InstanceError("INSTANCE_IDENTITY_MISMATCH", "现有目录注册表指向未知实例位置。")
    if new:
        _atomic(root / MANIFEST, manifest)
        _audit(root, "instance_registered", instance_id=manifest["instance_id"], existing_pcs=pcs.exists(), existing_memory=memory.exists())
    if scope_migration is not None:
        _atomic(root / "ui-preferences.json", preferences)
        _audit(root, "display_scope_migrated", old=scope_migration[0], new=scope_migration[1])
    if not expected_anchor.exists() and anchor_source.exists():
        with expected_anchor.open("x", encoding="utf-8") as handle:
            handle.write(anchor_source.read_text(encoding="utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        _audit(root, "anchor_path_rebound", source=anchor_source.name, destination=expected_anchor.name,
               sha256=hashlib.sha256(expected_anchor.read_bytes()).hexdigest())
    if memory.exists():
        adopted = ensure_binding(memory.parent, adopt_by="local-folder-selection", adopt_reason="the user selected this complete instance folder at its current location")
        if adopted:
            _audit(root, "memory_path_rebound", old=adopted["old_data_directory"], new=adopted["new_data_directory"])
    creating_pcs = components["pcs"]["state"] == "pending"
    if creating_pcs:
        if pcs.exists() or ledger_dir.exists() or anchor_dir.exists():
            raise InstanceError("INSTANCE_RECOVERY_REQUIRED", "待创建组件位置已存在状态，需要先核对，未执行覆盖。")
        pcs.parent.mkdir(parents=True, exist_ok=True)
        components["pcs"]["state"] = "initializing"
        components["recovery"]["state"] = "initializing"
        _atomic(root / MANIFEST, manifest)
    if legacy_adoption:
        components["recovery"]["state"] = "initializing"
        _atomic(root / MANIFEST, manifest)
    from runtime import _migrations_dir
    opened = CognitionStore.open(pcs, migrations_dir=_migrations_dir(), deletion_ledger_dir=ledger_dir, deletion_anchor_dir=anchor_dir,
                                 deletion_adopt=legacy_adoption)
    try:
        if not opened.restore_decision.is_open:
            raise InstanceError("INSTANCE_RECOVERY_BLOCKED", "恢复一致性检查拒绝连接：" + str(opened.restore_decision.reason))
        if creating_pcs or new:
            from quintessence_pcs.foundation.timeutil import utc_now_rfc3339
            opened.ingest_event(namespace=manifest["namespace"], source_kind="host_observation",
                                source_instance="instance-manager", source_event_id="instance-registration:" + manifest["instance_id"],
                                event_type="InstanceCreated" if creating_pcs else "InstanceAdopted",
                                payload={"instance_id": manifest["instance_id"], "component_declaration": manifest.get("component_declaration")}, privacy_class="private", observed_at=utc_now_rfc3339())
        if creating_pcs:
            from quintessence_pcs.store.recovery import record_anchor
            opened.deletion_ledger.ensure_ready(manifest["namespace"], 0)
            record_anchor(opened, manifest["namespace"], 0)
            if persona_source is not None:
                from quintessence_pcs.domains.persona.profiles import PersonaProfileService, REVIEW_APPROVE_EVENT_TYPE
                source_path, original = persona_source
                source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
                receipt = opened.ingest_event(namespace=manifest["namespace"], source_kind="user_report",
                    source_instance="local-folder-selection", source_event_id="persona-import:" + source_hash,
                    event_type=REVIEW_APPROVE_EVENT_TYPE, payload={"decision": "approve", "provisioning": True,
                    "source": "user-selected-initial-persona", "asset_sha256": source_hash},
                    privacy_class="private", observed_at=utc_now_rfc3339())
                reference = {"ref_type": "event", "namespace": manifest["namespace"], "event_id": receipt.event_id}
                body = copy.deepcopy(original)
                body["review_ref"] = reference
                profiles = PersonaProfileService(opened)
                version = profiles.publish_profile(namespace=manifest["namespace"], profile_id=body["profile_id"], body=body,
                    review_ref=reference, authorization_ref=document["host"]["access_ref"])
                profiles.activate_profile(namespace=manifest["namespace"], record_id=version.record_id,
                    record_version=version.record_version, authorization_ref=document["host"]["access_ref"])
                _audit(root, "initial_persona_imported", path=source_path.relative_to(root).as_posix(), sha256=source_hash,
                    profile_id=body["profile_id"], profile_version=body["profile_version"])
    finally:
        opened.close()
    if new and manifest.get("component_declaration"):
        _audit(root, "legacy_component_never_created_declared", **manifest["component_declaration"])
    if legacy_adoption:
        manifest["legacy_adoption"] = {**legacy_adoption, "at": _now(), "pre_baseline_deletions": "unprovable"}
        _audit(root, "legacy_recovery_baseline_adopted", **manifest["legacy_adoption"])
    store.pop("adoption", None)
    components["pcs"]["state"] = "ready"
    components["recovery"]["state"] = "ready"
    _atomic(root / MANIFEST, manifest)
    if components["memory"]["state"] == "pending":
        components["memory"]["state"] = "initializing"
        _atomic(root / MANIFEST, manifest)
        core = MemoryCore(CoreConfig(InstanceBinding(spec["agent_id"], spec["installation_id"], memory.parent, frozenset([spec["scope_id"]]), spec.get("test_mode", True))))
        core.initialize()
        components["memory"]["state"] = "ready"
    elif components["memory"]["state"] == "initializing":
        # Existing valid files can finish the interrupted bookkeeping, but a
        # missing file has already been rejected in _manifest.
        components["memory"]["state"] = "ready"
    persona = root / "persona"
    _inside(root, persona / "README.md")
    persona.mkdir(exist_ok=True)
    guide = persona / "README.md"
    if not guide.exists():
        with guide.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(README)
    imports = root / "persona-import"
    _inside(root, imports / "placeholder")
    imports.mkdir(exist_ok=True)
    if registry_document is not None:
        bindings = registry_document["bindings"]
        binding = bindings.get(manifest["namespace"])
        if binding and os.path.normcase(binding["root"]) != os.path.normcase(str(root)):
            binding["root"] = str(root)
            _atomic(registry, registry_document)
            _audit(root, "registry_path_rebound", old=previous_root, new=str(root))
    desktop = document.setdefault("desktop", {})
    desktop.setdefault("managed", True)
    desktop.setdefault("provision", True)
    manifest["root"] = str(root)
    if manifest["config_state"] == "pending":
        manifest["config_state"] = "initializing"
        _atomic(root / MANIFEST, manifest)
    _atomic(root / CONFIG, document)
    manifest["config_state"] = "ready"
    _atomic(root / MANIFEST, manifest)
    if previous_root != str(root):
        _audit(root, "instance_moved", old=previous_root, new=str(root))
    return {"instance_id": manifest["instance_id"], "namespace": manifest["namespace"], "root": str(root),
            "config_path": str(root / CONFIG), "state": ("restored" if previous_root != str(root) else
                      "completed" if new and (manifest["origin"]["pcs_existed"] or manifest["origin"]["memory_existed"] or persona_source) else
                      "created" if new else "connected"),
            "deletion_safety": "enforced", "rollback_protection": manifest["rollback_protection"]}


def prepare(value: str | Path, template: dict | None = None, *, legacy_adoption: dict | None = None, component_declaration: dict | None = None) -> dict:
    root = _root(value)
    if legacy_adoption is not None:
        if not isinstance(legacy_adoption, dict) or set(legacy_adoption) != {"by", "reason"} or any(not isinstance(v, str) or not v.strip() or len(v) > 512 for v in legacy_adoption.values()):
            raise InstanceError("INSTANCE_ADOPTION_INVALID", "显式旧实例接入必须记录操作者和原因。")
    if component_declaration is not None:
        declaration = component_declaration
        if (not isinstance(declaration, dict) or set(declaration) != {"by", "reason", "components"}
            or any(not isinstance(declaration[k], str) or not declaration[k].strip() or len(declaration[k]) > 512 for k in ("by", "reason"))
            or not isinstance(declaration["components"], list) or len(declaration["components"]) != 1
            or declaration["components"][0] not in ("pcs", "memory")):
            raise InstanceError("INSTANCE_DECLARATION_INVALID", "必须明确登记此前从未建立的组件、操作者和原因。")
    # This also serializes two first connections before any metadata exists.
    with DataInstanceLock(root / "pcs.sqlite"):
        manifest, _, _ = _manifest(root, template, legacy_adoption, component_declaration)
        actual = _inside(root, manifest["components"]["pcs"]["path"])
        if actual != root / "pcs.sqlite":
            with DataInstanceLock(actual):
                return _prepare(root, template, legacy_adoption, component_declaration)
        return _prepare(root, template, legacy_adoption, component_declaration)
