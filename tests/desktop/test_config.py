"""Host configuration tests: strict schema, derived destination and profile limits."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.desktop import support
from host_config import ConfigError, load_config


@unittest.skipUnless(os.name == "nt", "Windows installer verification script")
class InstallProcessOwnershipTests(unittest.TestCase):
    def test_owned_tree_orphans_and_reused_pid(self):
        script = r"""
$ErrorActionPreference = 'Stop'
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile(
 (Join-Path (Get-Location) 'apps/desktop/scripts/verify_install.ps1'),[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'parse failure' }
$fn=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-OwnedProcesses'},$true)
Invoke-Expression $fn.Extent.Text
$ScenarioRoot='synthetic-unique-scenarios'
$WebViewRoot='synthetic-unique-webview'
$t=[datetime]'2026-01-01T00:00:00Z'
function row($id,$parent,$seconds,$name,$command) {
 [pscustomobject]@{ProcessId=$id;ParentProcessId=$parent;CreationDate=$t.AddSeconds($seconds);Name=$name;CommandLine=$command}
}
$script:OwnedApp=row 10 1 1 'quintessence-desktop.exe' ''
$script:Rows=@(
 $script:OwnedApp,
 (row 11 10 2 'pcs-bridge.exe' ''),
 (row 12 11 3 'msedgewebview2.exe' ''),
 (row 20 1 1 'quintessence-desktop.exe' ''),
 (row 21 20 2 'pcs-bridge.exe' ''),
 (row 30 999 3 'msedgewebview2.exe' $WebViewRoot),
 (row 31 999 3 'pcs-bridge.exe' $ScenarioRoot),
 (row 32 999 3 'pcs-bridge.exe' $null),
 (row 40 10 0 'unrelated.exe' '')
)
function Get-CimInstance { param($ClassName) $script:Rows }
$ids=@(Get-OwnedProcesses | ForEach-Object ProcessId | Sort-Object)
if (($ids -join ',') -ne '10,11,12,30,31') { throw "unexpected ownership: $ids" }
$script:Rows=@((row 10 1 100 'quintessence-desktop.exe' ''),(row 11 10 101 'pcs-bridge.exe' ''))
if (@(Get-OwnedProcesses).Count -ne 0) { throw 'PID reuse selected foreign process' }
$script:OwnedApp=$null
if (@(Get-OwnedProcesses).Count -ne 0) { throw 'initial cleanup selected foreign process' }
"""
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ConfigTests(unittest.TestCase):
    def test_ps06_ledger_defaults_to_the_instance_root(self) -> None:
        # A new instance must not start unprotected: the ledger defaults to
        # <store.root>/ledger, and the anchor stays inside the same instance.
        config = self.load(support.config_document(self.database))
        self.assertEqual(self.directory / "ledger", config.deletion_ledger_dir)
        self.assertEqual(
            self.directory / "anchor", config.deletion_anchor_dir
        )
        self.assertIsNone(config.ps06_unprotected)

    def test_ps06_explicit_opt_out_requires_operator_and_reason(self) -> None:
        document = support.config_document(self.database)
        document["store"]["deletion_ledger_dir"] = False
        document["store"]["unprotected"] = {"by": "operator@lab", "reason": "no ledger"}
        config = self.load(document)
        self.assertIsNone(config.deletion_ledger_dir)
        self.assertEqual(
            {"by": "operator@lab", "reason": "no ledger"}, config.ps06_unprotected
        )

        for incomplete in ({"by": "", "reason": "x"}, {"by": "x"}, {}):
            document = support.config_document(self.database)
            document["store"]["deletion_ledger_dir"] = False
            document["store"]["unprotected"] = incomplete
            with self.assertRaises(ConfigError):
                self.load(document)

    def test_j07_context_cap_is_configurable_and_defaults_to_64k(self) -> None:
        document = support.config_document(self.database)
        self.assertEqual(65536, self.load(document).pcs_context_max_bytes)
        for cap in (24576, 32768, 49152, 65536, 81920):
            document["budget"]["pcs_context_max_bytes"] = cap
            self.assertEqual(cap, self.load(document).pcs_context_max_bytes)

    def test_j07_invalid_cap_and_disabled_receipt_refuse_startup(self) -> None:
        for value in (False, 0, -1, "24576"):
            document = support.config_document(self.database)
            document["budget"]["pcs_context_max_bytes"] = value
            with self.assertRaises(ConfigError):
                self.load(document)
        document = support.config_document(self.database)
        document["budget"]["pcs_belief_coverage"] = False
        with self.assertRaises(ConfigError):
            self.load(document)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="desktop-config-")
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name)
        self.database = self.directory / "desktop.sqlite"

    def load(self, document: dict) -> object:
        path = support.write_config(self.directory, document, name="host.json")
        return load_config(path)

    def test_minimal_lab_configuration_loads_and_derives_destination(self) -> None:
        config = self.load(support.config_document(self.database))
        self.assertEqual(config.profile, "lab")
        self.assertEqual(config.destination.provider, support.PROVIDER)
        self.assertTrue(config.destination.external)
        self.assertTrue(config.destination.confirmation_required)
        self.assertTrue(config.destination.destination_id.startswith("sha256:"))
        self.assertNotIn("127.0.0.1", json.dumps(config.destination.to_public_dict()))

    def test_local_destination_does_not_require_confirmation(self) -> None:
        document = support.config_document(self.database, external=False)
        config = self.load(document)
        self.assertFalse(config.destination.confirmation_required)
        self.assertEqual(config.destination.to_public_dict()["kind"], "local")

    def test_destination_id_changes_with_model_identity(self) -> None:
        first = self.load(support.config_document(self.database))
        document = support.config_document(self.database)
        document["destination"]["model"] = "scripted-collab-v2"
        document["destination"]["manifest"]["model"] = "scripted-collab-v2"
        second = self.load(document)
        self.assertNotEqual(
            first.destination.destination_id, second.destination.destination_id
        )

    def test_destination_id_is_stable_for_identical_configuration(self) -> None:
        first = self.load(support.config_document(self.database))
        second = self.load(support.config_document(self.database))
        self.assertEqual(
            first.destination.destination_id, second.destination.destination_id
        )

    def test_unknown_key_is_refused(self) -> None:
        document = support.config_document(self.database)
        document["host"]["unexpected"] = True
        with self.assertRaises(ConfigError) as caught:
            self.load(document)
        self.assertEqual(caught.exception.code, "CONFIG_INVALID")

    def test_production_profile_refuses_lab_fixtures(self) -> None:
        document = support.config_document(self.database)
        document["app"]["profile"] = "production"
        with self.assertRaises(ConfigError):
            self.load(document)

    def test_production_profile_refuses_scripted_transport(self) -> None:
        document = support.config_document(self.database)
        document["app"]["profile"] = "production"
        document.pop("lab")
        with self.assertRaises(ConfigError) as caught:
            self.load(document)
        self.assertIn("scripted", caught.exception.message)

    def test_manifest_must_match_destination(self) -> None:
        document = support.config_document(self.database)
        document["destination"]["manifest"]["provider"] = "another-provider"
        with self.assertRaises(ConfigError):
            self.load(document)

    def test_manifest_path_is_resolved_relative_to_configuration(self) -> None:
        document = support.config_document(self.database)
        manifest = document["destination"].pop("manifest")
        (self.directory / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        document["destination"]["manifest_path"] = "manifest.json"
        config = self.load(document)
        self.assertEqual(config.model.manifest.provider, support.PROVIDER)

    def test_database_environment_reference_is_expanded(self) -> None:
        document = support.config_document(self.database)
        document["store"]["database"] = "${DESKTOP_TEST_DATA}/nested/desktop.sqlite"
        previous = os.environ.get("DESKTOP_TEST_DATA")
        os.environ["DESKTOP_TEST_DATA"] = str(self.directory)
        try:
            config = self.load(document)
        finally:
            if previous is None:
                os.environ.pop("DESKTOP_TEST_DATA", None)
            else:
                os.environ["DESKTOP_TEST_DATA"] = previous
        self.assertEqual(
            config.database_path, (self.directory / "nested" / "desktop.sqlite").resolve()
        )

    def test_missing_environment_reference_is_refused(self) -> None:
        document = support.config_document(self.database)
        document["store"]["database"] = "${DESKTOP_TEST_UNSET_VALUE}/desktop.sqlite"
        with self.assertRaises(ConfigError) as caught:
            self.load(document)
        self.assertEqual(caught.exception.code, "CONFIG_ENV_MISSING")

    def test_question_limit_is_bounded(self) -> None:
        document = support.config_document(self.database)
        document["consult"]["max_question_chars"] = 100000
        with self.assertRaises(ConfigError):
            self.load(document)

    def test_query_plan_is_bounded(self) -> None:
        document = support.config_document(self.database)
        document["consult"]["query_plan"]["limit"] = 0
        with self.assertRaises(ConfigError):
            self.load(document)

    def test_duplicate_json_keys_are_refused(self) -> None:
        document = support.config_document(self.database)
        text = json.dumps(document, ensure_ascii=False)
        text = text.replace('"schema":', '"schema": "desktop.host.v1", "schema":', 1)
        path = self.directory / "duplicate.json"
        path.write_text(text, encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(path)

    def test_invalid_namespace_is_refused(self) -> None:
        document = support.config_document(self.database)
        document["host"]["namespace"] = "Example.Invalid"
        with self.assertRaises(ConfigError):
            self.load(document)

    def test_input_privacy_must_be_in_the_access_set(self) -> None:
        document = support.config_document(self.database)
        document["host"]["input_privacy_class"] = "restricted"
        with self.assertRaises(ConfigError):
            self.load(document)

    def test_conversation_section_defaults_and_bounds(self) -> None:
        config = self.load(support.config_document(self.database))
        self.assertEqual(8, config.history_max_turns)
        self.assertEqual(8192, config.history_max_bytes)

        document = support.config_document(self.database)
        document["conversation"] = {
            "history_max_turns": 4,
            "history_max_bytes": 4096,
        }
        explicit = self.load(document)
        self.assertEqual(4, explicit.history_max_turns)
        self.assertEqual(4096, explicit.history_max_bytes)

        for section in (
            {"history_max_turns": 0},
            {"history_max_turns": 65},
            {"history_max_bytes": 512},
            {"unexpected": 1},
        ):
            document = support.config_document(self.database)
            document["conversation"] = dict(section)
            with self.assertRaises(ConfigError):
                self.load(document)

        # The bound is ``context_max_bytes - 2048``.  Pin the 24 KiB cap
        # explicitly: the product default is now 64 KiB, which would make
        # 24000 legal and silently weaken this assertion.
        document = support.config_document(self.database)
        document["budget"]["pcs_context_max_bytes"] = 24576
        document["conversation"] = {"history_max_bytes": 24000}
        with self.assertRaises(ConfigError):
            self.load(document)

        # The same relation must hold at the new default.
        document = support.config_document(self.database)
        document["conversation"] = {"history_max_bytes": 65000}
        with self.assertRaises(ConfigError):
            self.load(document)

    def test_rg03_tool_budgets_and_depth_cap(self) -> None:
        config = self.load(support.config_document(self.database))
        self.assertEqual(8, config.max_model_requests_per_turn)
        self.assertEqual(6, config.max_tool_calls_per_turn)
        self.assertEqual(180, config.turn_wall_seconds)
        self.assertEqual(30, config.tool_wall_seconds)
        self.assertEqual(2, config.commentary_max_per_turn)
        self.assertEqual(60, config.commentary_max_chars)
        self.assertEqual(1536, config.tool_result_excerpt_bytes)
        self.assertEqual(6144, config.tool_results_total_bytes)
        self.assertTrue(config.tools_enabled)

        document = support.config_document(self.database)
        document["conversation"] = {
            "max_model_requests_per_turn": 4,
            "max_tool_calls_per_turn": 2,
            "turn_wall_seconds": 90,
            "tool_wall_seconds": 10,
            "commentary_max_per_turn": 1,
            "commentary_max_chars": 40,
            "tool_result_excerpt_bytes": 512,
            "tool_results_total_bytes": 2048,
            "tools_enabled": False,
        }
        explicit = self.load(document)
        self.assertEqual(2, explicit.max_tool_calls_per_turn)
        self.assertEqual(512, explicit.tool_result_excerpt_bytes)
        self.assertFalse(explicit.tools_enabled)

        # 7/8 tool calls are outside the deletion-closure depth budget.
        for section in (
            {"max_tool_calls_per_turn": 7},
            {"max_tool_calls_per_turn": 8},
            {"max_tool_calls_per_turn": -1},
            {"max_model_requests_per_turn": 0},
            {"commentary_max_chars": 0},
            {"tool_result_excerpt_bytes": 4096, "tool_results_total_bytes": 1024},
            {"tool_results_total_bytes": 24000},
            {"tools_enabled": "yes"},
        ):
            document = support.config_document(self.database)
            document["conversation"] = dict(section)
            with self.subTest(section=section):
                with self.assertRaises(ConfigError):
                    self.load(document)

    def test_rg08_commentary_is_bounded_and_can_be_disabled(self) -> None:
        document = support.config_document(self.database)
        document["conversation"] = {
            "commentary_max_per_turn": 1,
            "commentary_max_chars": 60,
        }
        config = self.load(document)
        self.assertEqual(1, config.commentary_max_per_turn)
        self.assertEqual(60, config.commentary_max_chars)

        document = support.config_document(self.database)
        document["conversation"] = {"commentary_max_per_turn": 0}
        self.assertEqual(0, self.load(document).commentary_max_per_turn)

        for section in (
            {"commentary_max_per_turn": 5},
            {"commentary_max_per_turn": -1},
        ):
            document = support.config_document(self.database)
            document["conversation"] = dict(section)
            with self.subTest(section=section):
                with self.assertRaises(ConfigError):
                    self.load(document)

    def test_rg07_expression_mode_defaults_to_off_and_is_strict(self) -> None:
        config = self.load(support.config_document(self.database))
        self.assertEqual("off", config.sdrpe.mode)
        self.assertEqual("sdrpe.v3", config.sdrpe.policy_version)

        document = support.config_document(self.database)
        document["conversation"] = {"sdrpe": {"mode": "on"}}
        self.assertEqual("on", self.load(document).sdrpe.mode)

        # `auto` is a reserved position: it parses so the setting can be shown
        # as unavailable; it is never an implemented trigger policy.
        document = support.config_document(self.database)
        document["conversation"] = {"sdrpe": {"mode": "auto"}}
        self.assertEqual("auto", self.load(document).sdrpe.mode)

        for section in (
            {"sdrpe": {"mode": "always"}},
            {"sdrpe": {"mode": 1}},
            {"sdrpe": {"mode": None}},
            {"sdrpe": {"mode": "on", "extra": 1}},
            {"sdrpe": "on"},
            {"sdrpe": {"mode": "on", "auto_trigger": {}}},
        ):
            document = support.config_document(self.database)
            document["conversation"] = dict(section)
            with self.subTest(section=section):
                with self.assertRaises(ConfigError):
                    self.load(document)

    def test_rg08_web_is_opt_in_by_default_and_strict(self) -> None:
        config = self.load(support.config_document(self.database))
        self.assertFalse(config.web.enabled)
        self.assertTrue(config.web.search)
        self.assertTrue(config.web.fetch)
        self.assertEqual(20, config.web.timeout_seconds)
        self.assertEqual((), config.web.allowed_hosts)
        self.assertEqual("metadata", config.web.capture_default)
        self.assertEqual("DEEPSEEK_API_KEY", config.web.api_key_env)

        document = support.config_document(self.database)
        document["web"] = {
            "enabled": True,
            "search": True,
            "fetch": True,
            "timeout_seconds": 15,
            "allowed_hosts": [".wikipedia.org", "example.com"],
            "capture_default": "evidence",
            "api_key_env": "RG08_WEB_KEY",
        }
        explicit = self.load(document)
        self.assertTrue(explicit.web.enabled)
        self.assertEqual(15, explicit.web.timeout_seconds)
        self.assertEqual(
            (".wikipedia.org", "example.com"), explicit.web.allowed_hosts
        )
        self.assertEqual("evidence", explicit.web.capture_default)
        self.assertEqual("RG08_WEB_KEY", explicit.web.api_key_env)

        for section in (
            {"enabled": "yes"},
            {"enabled": True, "timeout_seconds": 4},
            {"enabled": True, "timeout_seconds": 31},
            {"enabled": True, "allowed_hosts": "example.com"},
            {"enabled": True, "allowed_hosts": ["127.0.0.1"]},
            {"enabled": True, "allowed_hosts": ["localhost"]},
            {"enabled": True, "allowed_hosts": ["example.com", "example.com"]},
            {"enabled": True, "capture_default": "full"},
            {"enabled": True, "api_key_env": "not-an-env-name"},
            {"enabled": True, "search": False, "fetch": False},
            {"enabled": True, "unexpected": 1},
        ):
            document = support.config_document(self.database)
            document["web"] = dict(section)
            with self.subTest(section=section):
                with self.assertRaises(ConfigError):
                    self.load(document)

        production = support.config_document(
            self.database, profile="production", transport_kind="http"
        )
        production["web"] = {"enabled": True}
        self.assertTrue(self.load(production).web.enabled)

        inert = support.config_document(
            self.database, profile="production", transport_kind="http"
        )
        inert["web"] = {"enabled": False, "allowed_hosts": ["example.com"]}
        self.assertFalse(self.load(inert).web.enabled)

    def test_rg08_sidecar_builds_a_web_surface_only_from_the_environment(self) -> None:
        from runtime import _web_tool_config

        off = self.load(support.config_document(self.database))
        self.assertIsNone(_web_tool_config(off))
        document = support.config_document(self.database)
        document["web"] = {
            "enabled": True,
            "timeout_seconds": 10,
            "allowed_hosts": ["example.com"],
        }
        config = self.load(document)
        name = "DEEPSEEK_API_KEY"
        previous = os.environ.pop(name, None)
        try:
            missing = _web_tool_config(config)
            self.assertIsNotNone(missing)
            self.assertIsNone(missing.client)
            os.environ[name] = "synthetic-web-key-not-real"
            present = _web_tool_config(config)
            self.assertIsNotNone(present.client)
            self.assertNotIn("synthetic-web-key", repr(present))
            self.assertEqual(("web.search", "web.fetch"), present.names())
            self.assertEqual(("example.com",), present.policy.allowed_hosts)
        finally:
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous

    def test_ai_display_name_is_optional_and_bounded(self) -> None:
        document = support.config_document(self.database)
        document["app"]["ai_display_name"] = "小助手"
        config = self.load(document)
        self.assertEqual(config.ai_display_name, "小助手")
        document["app"]["ai_display_name"] = "x" * 41
        with self.assertRaises(ConfigError):
            self.load(document)

    def test_modified_copy_is_independent(self) -> None:
        document = support.config_document(self.database)
        clone = copy.deepcopy(document)
        clone["destination"]["provider"] = "other"
        self.assertEqual(document["destination"]["provider"], support.PROVIDER)

    def test_review_permissions_are_closed_and_need_the_review_purpose(self) -> None:
        document = support.config_document(
            self.database, review_permissions=support.REVIEW_PERMISSIONS
        )
        config = self.load(document)
        self.assertEqual(
            config.authority.review_permissions,
            frozenset(support.REVIEW_PERMISSIONS),
        )
        self.assertIn("review", config.authority.purposes)

        missing_purpose = copy.deepcopy(document)
        missing_purpose["host"]["purposes"] = ["consult", "reasoning"]
        with self.assertRaises(ConfigError) as caught:
            self.load(missing_purpose)
        self.assertIn("review purpose", caught.exception.message)

        unknown = copy.deepcopy(document)
        unknown["review"]["permissions"] = ["review.root"]
        with self.assertRaises(ConfigError):
            self.load(unknown)

        duplicate = copy.deepcopy(document)
        duplicate["review"]["permissions"] = ["work.scan", "work.scan"]
        with self.assertRaises(ConfigError):
            self.load(duplicate)

    def test_review_script_is_scripted_only_and_non_empty(self) -> None:
        document = support.config_document(
            self.database,
            review_permissions=support.REVIEW_PERMISSIONS,
            review_script=[{"operation": "wait", "reason": "synthetic"}],
        )
        config = self.load(document)
        self.assertEqual(len(config.model.review_script), 1)

        empty = copy.deepcopy(document)
        empty["destination"]["transport"]["review_script"] = []
        with self.assertRaises(ConfigError):
            self.load(empty)

        non_object = copy.deepcopy(document)
        non_object["destination"]["transport"]["review_script"] = ["not-an-object"]
        with self.assertRaises(ConfigError):
            self.load(non_object)

        http = support.config_document(self.database)
        http["destination"]["transport"] = {
            "kind": "http",
            "review_script": [{"operation": "wait", "reason": "synthetic"}],
        }
        with self.assertRaises(ConfigError):
            self.load(http)

    def test_review_script_is_empty_when_not_configured(self) -> None:
        config = self.load(support.config_document(self.database))
        self.assertEqual(config.model.review_script, ())
        self.assertEqual(config.authority.review_permissions, frozenset())

    def test_managed_production_section_is_optional_and_closed(self) -> None:
        document = support.config_document(
            self.database,
            profile="production",
            external=False,
            transport_kind="http",
            desktop={"managed": True, "provision": True},
        )
        config = self.load(document)
        self.assertTrue(config.managed)
        self.assertTrue(config.provision)

        bad = copy.deepcopy(document)
        bad["desktop"]["provision"] = True
        bad["desktop"]["managed"] = False
        with self.assertRaises(ConfigError):
            self.load(bad)

        unknown = copy.deepcopy(document)
        unknown["desktop"]["root"] = True
        with self.assertRaises(ConfigError):
            self.load(unknown)


if __name__ == "__main__":
    unittest.main()


class CognitionConfigTests(ConfigTests):
    """RG05: cognition is an explicit 64 KiB opt-in, not a default."""

    def cognition_document(self, *, cap: int, propose: bool = True, scope=True):
        document = support.config_document(self.database)
        document["budget"]["pcs_context_max_bytes"] = cap
        document["cognition"] = {
            "propose": propose,
            "working_set_limit": 5,
            "pending_limit": 3,
        }
        if not scope:
            document["consult"]["query_plan"]["scope"] = {}
        return document

    def test_cognition_requires_64_kib(self) -> None:
        for cap in (24576, 32768, 49152):
            with self.assertRaises(ConfigError):
                self.load(self.cognition_document(cap=cap))
        config = self.load(self.cognition_document(cap=65536))
        self.assertTrue(config.cognition.enabled)
        self.assertTrue(config.cognition.propose)

    def test_cognition_requires_a_scope(self) -> None:
        with self.assertRaises(ConfigError):
            self.load(self.cognition_document(cap=65536, scope=False))

    def test_absent_block_keeps_the_64_kib_default(self) -> None:
        config = self.load(support.config_document(self.database))
        self.assertFalse(config.cognition.enabled)
        self.assertEqual(65536, config.pcs_context_max_bytes)
