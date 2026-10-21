"""PCS2-009 C group: manifest validation and the fixed, tool-free request body."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from model_support import make_manifest  # noqa: E402

from quintessence_pcs.runtime.manifest import (  # noqa: E402
    ManifestError,
    ModelManifest,
    build_request_body,
)
from quintessence_pcs.runtime.model_port import ModelConfigurationError  # noqa: E402

EXTERNAL_BASE = "https://api.deepseek.com"


def manifest_mapping(**overrides):
    values = {
        "provider": "deepseek",
        "base_url": EXTERNAL_BASE,
        "model": "deepseek-flash",
        "api_key_env": "PCS_MODEL_API_KEY",
        "max_calls": 4,
        "max_output_tokens_per_call": 512,
        "max_wall_seconds": 300,
        "thinking": "disabled",
        "temperature": 0,
    }
    values.update(overrides)
    return values


class ManifestHashTests(unittest.TestCase):
    def test_the_cards_example_has_a_pinned_hash(self) -> None:
        manifest = ModelManifest.from_mapping(manifest_mapping())
        self.assertEqual(
            "sha256:95c70aaa17f796d9ce8f876c2512aa232161843101419da17e09e894b50fdab1",
            manifest.manifest_hash,
        )
        self.assertEqual(
            EXTERNAL_BASE + "/chat/completions", manifest.endpoint
        )

    def test_field_order_does_not_change_the_hash(self) -> None:
        first = ModelManifest.from_mapping(manifest_mapping())
        mapping = manifest_mapping()
        reordered = {key: mapping[key] for key in reversed(list(mapping))}
        second = ModelManifest.from_mapping(reordered)
        self.assertEqual(first.manifest_hash, second.manifest_hash)

    def test_changing_model_base_or_limits_changes_the_hash(self) -> None:
        base = ModelManifest.from_mapping(manifest_mapping())
        for override in (
            {"model": "deepseek-v4-pro"},
            {"base_url": "https://api.deepseek.com/v1"},
            {"max_calls": 5},
            {"max_output_tokens_per_call": 256},
            {"max_wall_seconds": 301},
        ):
            changed = ModelManifest.from_mapping(manifest_mapping(**override))
            self.assertNotEqual(
                base.manifest_hash, changed.manifest_hash, override
            )

    def test_hash_never_depends_on_the_environment_value(self) -> None:
        first = ModelManifest.from_mapping(manifest_mapping())
        second = ModelManifest.from_mapping(manifest_mapping())
        self.assertEqual(first.manifest_hash, second.manifest_hash)
        # The manifest has no key field at all; the key's env name is covered.
        self.assertNotIn("sk-", first.canonical_text)


class ManifestValidationTests(unittest.TestCase):
    def test_unknown_key_is_rejected(self) -> None:
        with self.assertRaises(ManifestError):
            ModelManifest.from_mapping(
                manifest_mapping(api_key="sk-not-allowed-in-config")
            )

    def test_missing_key_is_rejected(self) -> None:
        mapping = manifest_mapping()
        del mapping["thinking"]
        with self.assertRaises(ManifestError):
            ModelManifest.from_mapping(mapping)

    def test_thinking_must_be_explicitly_disabled(self) -> None:
        with self.assertRaises(ManifestError):
            ModelManifest.from_mapping(manifest_mapping(thinking="enabled"))

    def test_temperature_must_be_zero(self) -> None:
        with self.assertRaises(ManifestError):
            ModelManifest.from_mapping(manifest_mapping(temperature=1))

    def test_non_loopback_http_is_rejected(self) -> None:
        with self.assertRaises(ManifestError):
            ModelManifest.from_mapping(
                manifest_mapping(base_url="http://api.deepseek.com")
            )

    def test_loopback_http_is_allowed_for_tests(self) -> None:
        manifest = ModelManifest.from_mapping(
            manifest_mapping(base_url="http://127.0.0.1:1234")
        )
        self.assertEqual(
            "http://127.0.0.1:1234/chat/completions", manifest.endpoint
        )
        localhost = ModelManifest.from_mapping(
            manifest_mapping(base_url="http://localhost:9999")
        )
        self.assertEqual("http://localhost:9999/chat/completions", localhost.endpoint)

    def test_https_is_allowed_for_any_host(self) -> None:
        manifest = ModelManifest.from_mapping(
            manifest_mapping(base_url="https://api.deepseek.com/v1")
        )
        self.assertEqual(
            "https://api.deepseek.com/v1/chat/completions", manifest.endpoint
        )

    def test_base_url_cannot_embed_credentials_or_a_query(self) -> None:
        for url in (
            "https://user:pass@api.deepseek.com",
            "https://api.deepseek.com?x=1",
            "https://api.deepseek.com#fragment",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ManifestError):
                    ModelManifest.from_mapping(manifest_mapping(base_url=url))

    def test_api_key_env_is_a_name_not_a_key(self) -> None:
        with self.assertRaises(ManifestError):
            ModelManifest.from_mapping(
                manifest_mapping(api_key_env="sk-1234567890")
            )

    def test_integer_fields_reject_booleans_and_floats(self) -> None:
        for field in ("max_calls", "max_output_tokens_per_call", "max_wall_seconds"):
            for value in (True, 4.0):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ManifestError):
                        ModelManifest.from_mapping(
                            manifest_mapping(**{field: value})
                        )

    def test_from_json_is_strict(self) -> None:
        document = json.dumps(manifest_mapping(), ensure_ascii=False)
        manifest = ModelManifest.from_json(document)
        self.assertEqual(
            ModelManifest.from_mapping(manifest_mapping()).manifest_hash,
            manifest.manifest_hash,
        )
        with self.assertRaises(ManifestError):
            ModelManifest.from_json("not json")
        with self.assertRaises(ManifestError):
            ModelManifest.from_json('{"max_calls": NaN}')


class RequestBodyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = ModelManifest.from_mapping(manifest_mapping())
        self.rendered = '{"schema":"synthetic","text":"中文与 ASCII"}'

    def test_first_body_has_exactly_one_user_message_and_no_tools(self) -> None:
        text = build_request_body(
            self.manifest, rendered_input=self.rendered, max_tokens=512
        )
        document = json.loads(text)
        self.assertEqual(
            [{"role": "user", "content": self.rendered}],
            document["messages"],
        )
        self.assertEqual({"type": "disabled"}, document["thinking"])
        self.assertEqual(0, document["temperature"])
        self.assertEqual(512, document["max_tokens"])
        self.assertIs(False, document["stream"])
        self.assertEqual(set(document), {
            "model",
            "messages",
            "thinking",
            "temperature",
            "max_tokens",
            "stream",
        })

    def test_repair_adds_the_previous_assistant_and_feedback_only(self) -> None:
        text = build_request_body(
            self.manifest,
            rendered_input=self.rendered,
            max_tokens=512,
            previous_output="not json",
            feedback="return exactly one JSON object",
        )
        document = json.loads(text)
        self.assertEqual(
            ["user", "assistant", "user"],
            [message["role"] for message in document["messages"]],
        )
        self.assertEqual(self.rendered, document["messages"][0]["content"])
        self.assertEqual("not json", document["messages"][1]["content"])
        self.assertEqual(
            "return exactly one JSON object",
            document["messages"][2]["content"],
        )
        self.assertNotIn("system", [message["role"] for message in document["messages"]])

    def test_feedback_without_previous_output_is_refused(self) -> None:
        with self.assertRaises(ModelConfigurationError):
            build_request_body(
                self.manifest,
                rendered_input=self.rendered,
                max_tokens=512,
                feedback="repair",
            )

    def test_helper_manifest_is_loopback(self) -> None:
        manifest = make_manifest("http://127.0.0.1:1")
        self.assertTrue(manifest.endpoint.startswith("http://127.0.0.1:1/"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
