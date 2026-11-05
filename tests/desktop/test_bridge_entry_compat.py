"""RG09 P8: a bridge entry's declared action set is checked, not assumed.

The configured Memory bridge entry is a pinned Python shim.  The current tree
declares ``capture``/``search``/``recall``/``time_window``; the older main tree
declared only ``capture``/``search``.  These tests prove that the host reads the
declaration without executing the entry and that ``app.info`` stops advertising
automatic recall when the configured entry cannot serve it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.desktop import support

from entry_compat import (  # noqa: E402 - sidecar path is added by support
    check_entry_compatibility,
    read_declared_actions,
    required_actions,
)

ALL_ACTIONS = ("capture", "search", "recall", "time_window")
OLD_ACTIONS = ("capture", "search")


class EntryActionSetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="rg09-entry-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def _entry(self, name: str, text: str) -> Path:
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_old_capture_search_entry_is_missing_recall_and_time_window(self) -> None:
        path = self._entry("old_cli.py", "_ACTIONS = ('capture', 'search')\n")
        compat = check_entry_compatibility(
            path,
            required_actions(recall_enabled=True, time_enabled=True),
        )
        self.assertTrue(compat["checked"])
        self.assertFalse(compat["compatible"])
        self.assertEqual(["recall", "time_window"], compat["missing"])
        self.assertEqual(["capture", "search"], compat["actions"])

    def test_current_entry_covers_every_required_action(self) -> None:
        path = self._entry(
            "current_cli.py",
            "_ACTIONS = ('capture', 'search', 'recall', 'time_window')\n",
        )
        compat = check_entry_compatibility(
            path,
            required_actions(recall_enabled=True, time_enabled=True),
        )
        self.assertTrue(compat["checked"])
        self.assertTrue(compat["compatible"])
        self.assertEqual([], compat["missing"])

    def test_declared_actions_are_read_without_running_the_entry(self) -> None:
        path = self._entry(
            "not_executed.py",
            "_ACTIONS = ('capture', 'search')\n"
            "raise SystemExit('this file must not be imported or executed')\n",
        )
        actions, reason = read_declared_actions(path)
        self.assertEqual(("capture", "search"), actions)
        self.assertEqual("declared", reason)

    def test_undeclared_wrapper_is_unknown_not_a_mismatch(self) -> None:
        path = self._entry("wrapper.py", "print('a wrapper')\n")
        compat = check_entry_compatibility(
            path,
            required_actions(recall_enabled=True, time_enabled=True),
        )
        self.assertFalse(compat["checked"])
        self.assertTrue(compat["compatible"])
        self.assertEqual("actions_not_declared", compat["reason"])

    def test_unreadable_or_unparsable_entry_is_unknown(self) -> None:
        missing = check_entry_compatibility(self.tmp / "missing.py", ALL_ACTIONS)
        self.assertFalse(missing["checked"])
        self.assertTrue(missing["compatible"])
        self.assertEqual("entry_unreadable", missing["reason"])

        broken = self._entry("broken.py", "def broken(:\n")
        compat = check_entry_compatibility(broken, ALL_ACTIONS)
        self.assertFalse(compat["checked"])
        self.assertEqual("entry_unparsed", compat["reason"])

    def test_required_actions_follow_the_enabled_surfaces(self) -> None:
        self.assertEqual(("capture", "search"), required_actions(
            recall_enabled=False, time_enabled=True
        ))
        self.assertEqual(
            ("capture", "search", "recall"),
            required_actions(recall_enabled=True, time_enabled=False),
        )
        self.assertEqual(
            ("capture", "search", "recall", "time_window"),
            required_actions(recall_enabled=True, time_enabled=True),
        )


class BridgeEntryAppInfoTests(support.DesktopCase):
    def _entry(self, actions: tuple[str, ...] | None) -> Path:
        path = self.tmp_path / "bridge_entry.py"
        declaration = (
            f"_ACTIONS = {actions!r}\n" if actions is not None else ""
        )
        path.write_text(
            "import json, sys\n"
            + declaration
            + "json.loads(sys.stdin.buffer.read())\n"
            + "json.dump({'kind':'ok','receipt':{'disposition':'inserted',"
            + "'durability':'persisted','event_refs':[]}}, sys.stdout)\n",
            encoding="utf-8",
        )
        return path

    def _document(self, actions: tuple[str, ...] | None):
        memory_dir = self.tmp_path / "memory-external"
        memory_dir.mkdir(exist_ok=True)
        return support.config_document(
            self.db_path,
            purposes=["consult", "memory.bridge"],
            memory_bridge={
                "enabled": True,
                "python_executable": sys.executable,
                "entry": str(self._entry(actions)),
                "package_path": str(self.tmp_path),
                "data_directory": "memory-external",
                "agent_id": "rg09-entry",
                "installation_id": "rg09-entry-install",
                "scope_id": "scope:rg09-entry",
                "session_id": "session:rg09-entry",
                "permissions": ["memory_bridge"],
            },
            memory={
                "timezone": "Asia/Shanghai",
                "recall": {
                    "enabled": True,
                    "max_items": 4,
                    "budget_units": 2048,
                    "timeout_seconds": 1.5,
                },
                "time": {"enabled": True},
            },
        )

    def _recall(self, actions: tuple[str, ...] | None) -> dict:
        process = self.start(document=self._document(actions))
        info = process.request("app.info")["result"]
        return info["capabilities"]["memory"]["recall"]

    def test_app_info_refuses_to_advertise_recall_for_an_old_entry(self) -> None:
        recall = self._recall(OLD_ACTIONS)
        self.assertTrue(recall["enabled"])
        self.assertTrue(recall["bridge_configured"])
        self.assertFalse(recall["configured"])
        self.assertTrue(recall["entry"]["checked"])
        self.assertFalse(recall["entry"]["compatible"])
        self.assertEqual(["recall", "time_window"], recall["entry"]["missing"])
        self.assertIn("entry.recall", recall["missing"])
        self.assertIn("entry.time_window", recall["missing"])

    def test_app_info_reports_a_compatible_current_entry(self) -> None:
        recall = self._recall(ALL_ACTIONS)
        self.assertTrue(recall["configured"])
        self.assertTrue(recall["entry"]["checked"])
        self.assertTrue(recall["entry"]["compatible"])
        self.assertEqual([], recall["entry"]["missing"])
        self.assertNotIn("entry.recall", recall["missing"])

    def test_an_undeclared_wrapper_stays_unknown_not_rejected(self) -> None:
        recall = self._recall(None)
        self.assertTrue(recall["configured"])
        self.assertFalse(recall["entry"]["checked"])
        self.assertTrue(recall["entry"]["compatible"])

    def test_deployment_check_script_reports_the_old_entry(self) -> None:
        config_path = support.write_config(
            self.tmp_path, self._document(OLD_ACTIONS), name="deploy-check.json"
        )
        script = (
            support.REPO_ROOT
            / "apps"
            / "desktop"
            / "scripts"
            / "check_memory_bridge_entry.py"
        )
        completed = subprocess.run(
            [sys.executable, str(script), "--config", str(config_path)],
            cwd=str(support.REPO_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        self.assertEqual(3, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["checked"])
        self.assertFalse(payload["compatible"])
        self.assertEqual(["recall", "time_window"], payload["missing"])
        self.assertNotIn(str(config_path), completed.stdout)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
