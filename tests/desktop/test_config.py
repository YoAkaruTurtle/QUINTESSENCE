"""Host configuration tests: strict schema, derived destination and profile limits."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

from tests.desktop import support
from host_config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
