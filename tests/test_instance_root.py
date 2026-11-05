"""J03 instance-root configuration tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
SIDECAR_DIR = REPO_ROOT / "apps" / "desktop" / "sidecar"
for _path in (SRC_DIR, SIDECAR_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import host_config  # noqa: E402

MANIFEST = {
    "provider": "synthetic",
    "base_url": "http://127.0.0.1:9/v1",
    "model": "j03-test-model",
    "api_key_env": "QUINTESSENCE_J03_TEST_MODEL_KEY",
    "max_calls": 4,
    "max_output_tokens_per_call": 512,
    "max_wall_seconds": 60,
    "thinking": "disabled",
    "temperature": 0,
}


def config_document(
    root: Path,
    *,
    database: Path | None = None,
    request_budget_units: int = 200_000,
    output_reserve_units: int = 4096,
    namespace: str = "example.test.j03",
) -> dict:
    store: dict = {"root": str(root)}
    if database is not None:
        store["database"] = str(database)
    return {
        "schema": "desktop.host.v1",
        "app": {"profile": "lab", "label": "J03"},
        "store": store,
        "budget": {
            "request_budget_units": request_budget_units,
            "output_reserve_units": output_reserve_units,
        },
        "host": {
            "namespace": namespace,
            "host_instance": "host:j03",
            "operator": "operator:j03",
            "access_ref": "authorization:host:j03",
            "privacy_classes": ["private"],
            "purposes": ["consult"],
            "disclose": True,
            "retention": "standard",
            "policy_ref": "policy:collab.v1",
        },
        "consult": {"query_plan": None, "max_question_chars": 8000},
        "destination": {
            "provider": "synthetic",
            "model": "j03-test-model",
            "model_protocol": "collab.v1",
            "external": True,
            "manifest": MANIFEST,
            "transport": {"kind": "http"},
        },
    }


class InstanceRootTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="j03-root-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name).resolve()
        self.root = self.base / "instance"
        self.root.mkdir()

    def _load(self, document: dict):
        path = self.base / "host.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return host_config.load_config(path)

    def _assert_invalid(self, document: dict) -> None:
        with self.assertRaises(host_config.ConfigError) as caught:
            self._load(document)
        self.assertEqual("CONFIG_INVALID", caught.exception.code)

    def test_missing_root_is_refused(self) -> None:
        document = config_document(self.root)
        del document["store"]["root"]
        self._assert_invalid(document)

    def test_relative_root_is_refused(self) -> None:
        document = config_document(self.root)
        document["store"]["root"] = "relative/instance"
        self._assert_invalid(document)

    def test_missing_directory_is_refused(self) -> None:
        self._assert_invalid(config_document(self.base / "absent"))

    def test_file_instead_of_directory_is_refused(self) -> None:
        file_root = self.base / "file-root"
        file_root.write_text("x", encoding="utf-8")
        self._assert_invalid(config_document(file_root))

    def test_symlinked_root_is_refused(self) -> None:
        link = self.base / "linked-root"
        try:
            os.symlink(self.root, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks are not available on this host")
        self._assert_invalid(config_document(link))

    def test_valid_root_defaults_the_database_inside_it(self) -> None:
        before = sorted(p.name for p in self.root.iterdir())
        config = self._load(config_document(self.root))
        self.assertEqual(self.root, config.instance_root)
        self.assertEqual(self.root / "pcs.sqlite", config.database_path)
        self.assertEqual(200_000, config.request_budget_units)
        self.assertEqual(4096, config.output_reserve_units)
        self.assertEqual(before, sorted(p.name for p in self.root.iterdir()))

    def test_database_outside_the_root_is_refused(self) -> None:
        document = config_document(self.root, database=self.base / "outside.sqlite")
        self._assert_invalid(document)

    def test_budget_is_required(self) -> None:
        document = config_document(self.root)
        del document["budget"]
        self._assert_invalid(document)

    def test_budget_must_leave_room_for_the_safety_margin(self) -> None:
        document = config_document(
            self.root, request_budget_units=1000, output_reserve_units=900
        )
        self._assert_invalid(document)

    def _make_junction(self, link: Path, target: Path) -> None:
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            self.fail(
                "junction creation failed (AC-04 requires a real junction): "
                + (result.stderr or result.stdout).strip()
            )

    def test_junction_root_is_refused(self) -> None:
        link = self.base / "junction-root"
        self._make_junction(link, self.root)
        self._assert_invalid(config_document(link))

    def test_junction_ancestor_is_refused(self) -> None:
        ancestor = self.base / "junction-ancestor"
        self._make_junction(ancestor, self.root)
        nested = ancestor / "nested"
        nested.mkdir()
        self._assert_invalid(config_document(nested))

    def test_generated_lab_config_carries_root_and_budget_without_secrets(self) -> None:
        output = self.base / "lab-host.json"
        data_dir = self.base / "lab-data"
        data_dir.mkdir()
        script = REPO_ROOT / "apps" / "desktop" / "scripts" / "make_lab_host.py"
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--output",
                str(output),
                "--data-dir",
                str(data_dir),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertTrue(Path(document["store"]["root"]).is_absolute())
        self.assertEqual(200_000, document["budget"]["request_budget_units"])
        self.assertEqual(4096, document["budget"]["output_reserve_units"])
        text = output.read_text(encoding="utf-8")
        self.assertNotIn("sk-", text)
        self.assertNotIn("\"api_key\"", text)
        self.assertEqual(str(data_dir), document["store"]["root"])
        self.assertNotIn("private-instance", text)

    # ------------------------------------------------- RG01 generator options

    def _run_generator(self, *extra: str, data_dir: Path, output: Path):
        script = REPO_ROOT / "apps" / "desktop" / "scripts" / "make_lab_host.py"
        return subprocess.run(
            [
                sys.executable,
                str(script),
                "--output",
                str(output),
                "--data-dir",
                str(data_dir),
                *extra,
            ],
            capture_output=True,
            text=True,
        )

    def test_default_generator_output_carries_no_optional_sections(self) -> None:
        data_dir = self.base / "lab-default"
        data_dir.mkdir()
        output = self.base / "lab-default.json"
        result = self._run_generator(data_dir=data_dir, output=output)
        self.assertEqual(0, result.returncode, result.stderr)
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertNotIn("memory_bridge", document)
        self.assertNotIn("web", document)
        self.assertNotIn("tool_mode", document["destination"]["manifest"])
        self.assertEqual(
            ["consult", "reasoning", "review"], document["host"]["purposes"]
        )
        self.assertEqual(
            [
                {"action": "query", "brief_reason": "deterministic belief lookup"},
                {
                    "action": "none",
                    "reply": (
                        "合成回答：本周可披露材料中只有一条能力条目 "
                        "capability:lab-file-read。"
                        "这条回答由真实的 ConsultFacade、Context、确定性查询与 Attempt "
                        "路径生成，仅用于 C12b 桌面验收，不包含任何真实私人内容。"
                    ),
                },
            ],
            document["destination"]["transport"]["script"],
        )
        # the generated document is still a valid host configuration
        loaded = host_config.load_config(output)
        self.assertEqual("example.test.desktop.lab", loaded.authority.namespace)
        self.assertEqual("tool_free", loaded.model.manifest.tool_mode)

    def test_generator_options_are_written_and_still_validate(self) -> None:
        data_dir = self.base / "lab-options"
        data_dir.mkdir()
        output = self.base / "lab-options.json"
        script_file = self.base / "script.json"
        script_file.write_text(
            json.dumps([{"action": "none", "reply": "script-file probe"}]),
            encoding="utf-8",
        )
        result = self._run_generator(
            "--namespace",
            "example.rg01.alt",
            "--tool-mode",
            "agent",
            "--script-file",
            str(script_file),
            "--budget-units",
            "180000",
            "--output-reserve",
            "2048",
            data_dir=data_dir,
            output=output,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual("example.rg01.alt", document["host"]["namespace"])
        self.assertEqual("agent", document["destination"]["manifest"]["tool_mode"])
        self.assertEqual(
            [{"action": "none", "reply": "script-file probe"}],
            document["destination"]["transport"]["script"],
        )
        self.assertEqual(180_000, document["budget"]["request_budget_units"])
        self.assertEqual(2_048, document["budget"]["output_reserve_units"])
        loaded = host_config.load_config(output)
        self.assertEqual("example.rg01.alt", loaded.authority.namespace)
        self.assertEqual("agent", loaded.model.manifest.tool_mode)
        self.assertIsNone(loaded.memory_bridge)

    def test_web_switch_is_opt_in_and_still_validates(self) -> None:
        data_dir = self.base / "lab-web"
        data_dir.mkdir()
        output = self.base / "lab-web.json"
        result = self._run_generator("--web", data_dir=data_dir, output=output)
        self.assertEqual(0, result.returncode, result.stderr)
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual({"enabled": True}, document["web"])
        self.assertNotIn("sk-", output.read_text(encoding="utf-8"))
        loaded = host_config.load_config(output)
        self.assertTrue(loaded.web.enabled)
        self.assertEqual("DEEPSEEK_API_KEY", loaded.web.api_key_env)

    def test_memory_bridge_requires_every_path_before_writing(self) -> None:
        data_dir = self.base / "lab-mb-missing"
        data_dir.mkdir()
        output = self.base / "lab-mb-missing.json"
        result = self._run_generator(
            "--memory-bridge", data_dir=data_dir, output=output
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("--memory-bridge requires", result.stderr)
        self.assertFalse(output.exists())

    def test_memory_bridge_section_is_additive_and_inside_the_instance(self) -> None:
        data_dir = self.base / "lab-mb"
        data_dir.mkdir()
        output = self.base / "lab-mb.json"
        package = self.base / "pkg"
        package.mkdir()
        result = self._run_generator(
            "--memory-bridge",
            "--memory-python",
            sys.executable,
            "--memory-entry",
            str(REPO_ROOT / "src" / "quintessence_pcs" / "adapters" / "scope_recall" / "cli.py"),
            "--memory-package-path",
            str(package),
            "--memory-agent-id",
            "agent:rg01",
            "--memory-installation-id",
            "install:rg01",
            "--memory-scope-id",
            "scope:rg01",
            "--memory-session-id",
            "session:rg01",
            data_dir=data_dir,
            output=output,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        document = json.loads(output.read_text(encoding="utf-8"))
        self.assertIn("memory.bridge", document["host"]["purposes"])
        section = document["memory_bridge"]
        self.assertTrue(section["enabled"])
        self.assertEqual(["memory_bridge"], section["permissions"])
        self.assertTrue(
            Path(section["data_directory"])
            .resolve()
            .is_relative_to(data_dir.resolve())
        )
        loaded = host_config.load_config(output)
        self.assertIsNotNone(loaded.memory_bridge)
        self.assertTrue(loaded.memory_bridge.enabled)
        self.assertEqual("scope:rg01", loaded.memory_bridge.scope_id)

    def test_same_root_keeps_the_binding_and_a_different_root_is_rejected(self) -> None:
        first = self._load(config_document(self.root))
        second = self._load(config_document(self.root))
        self.assertEqual(first.instance_root, second.instance_root)
        self.assertEqual(first.database_path, second.database_path)
        self.assertEqual(first.authority.namespace, second.authority.namespace)
        self.assertEqual(first.authority.host_instance, second.authority.host_instance)

        other_root = self.base / "instance-two"
        other_root.mkdir()
        with self.assertRaises(host_config.ConfigError) as caught:
            self._load(config_document(other_root))
        self.assertEqual("CONFIG_INVALID", caught.exception.code)

        registry = json.loads(
            (self.base / "instances.json").read_text(encoding="utf-8")
        )
        self.assertEqual("pcs.instances.v1", registry["schema"])
        self.assertEqual(
            str(self.root),
            registry["bindings"]["example.test.j03"]["root"],
        )

    def test_first_load_registers_the_namespace_once(self) -> None:
        self.assertFalse((self.base / "instances.json").exists())
        self._load(config_document(self.root))
        registry = json.loads(
            (self.base / "instances.json").read_text(encoding="utf-8")
        )
        binding = registry["bindings"]["example.test.j03"]
        self.assertEqual(str(self.root), binding["root"])
        self.assertIn("bound_at", binding)

    def test_same_root_variants_do_not_conflict(self) -> None:
        first = self._load(config_document(self.root))
        trailing = config_document(self.root)
        trailing["store"]["root"] = str(self.root) + os.sep
        self.assertEqual(first.instance_root, self._load(trailing).instance_root)

        cased = config_document(self.root)
        cased["store"]["root"] = str(self.root).upper()
        try:
            self._load(cased)
        except host_config.ConfigError:
            self.fail("a case variant of the same root was treated as a conflict")

    def test_corrupt_registry_is_refused_and_not_overwritten(self) -> None:
        registry_path = self.base / "instances.json"
        registry_path.write_text("{not json", encoding="utf-8")
        before = registry_path.read_text(encoding="utf-8")
        with self.assertRaises(host_config.ConfigError) as caught:
            self._load(config_document(self.root))
        self.assertEqual("CONFIG_INVALID", caught.exception.code)
        self.assertEqual(before, registry_path.read_text(encoding="utf-8"))

    def test_registry_with_a_foreign_schema_is_refused(self) -> None:
        registry_path = self.base / "instances.json"
        registry_path.write_text(
            json.dumps({"schema": "other.v1", "bindings": {}}), encoding="utf-8"
        )
        with self.assertRaises(host_config.ConfigError):
            self._load(config_document(self.root))

    def test_one_root_cannot_bind_to_two_namespaces(self) -> None:
        self._load(config_document(self.root))
        registry_path = self.base / "instances.json"
        before = registry_path.read_bytes()
        with self.assertRaises(host_config.ConfigError) as caught:
            self._load(config_document(self.root, namespace="example.test.j03.other"))
        self.assertEqual("CONFIG_INVALID", caught.exception.code)
        self.assertEqual(before, registry_path.read_bytes())

    def test_unrelated_corrupt_binding_is_refused_without_rewrite(self) -> None:
        self._load(config_document(self.root))
        registry_path = self.base / "instances.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        registry["bindings"]["example.test.other"] = {
            "root": 123,
            "bound_at": "2026-10-25T00:00:00Z",
        }
        registry_path.write_text(json.dumps(registry), encoding="utf-8")
        before = registry_path.read_bytes()
        with self.assertRaises(host_config.ConfigError):
            self._load(config_document(self.root))
        self.assertEqual(before, registry_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
