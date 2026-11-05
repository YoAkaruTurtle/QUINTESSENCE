"""J11: refuse a thinking manifest whose output budget cannot hold an answer.

With ``thinking`` enabled the reasoning tokens are paid out of
``max_output_tokens_per_call``.  A 2048 cap was consumed entirely by reasoning
and a structured proposal came back as ``finish_reason=length`` with no
content — a paid call that could never succeed.  The guard below turns that
into a configuration error at load time.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.desktop import support
from host_config import MIN_THINKING_OUTPUT_TOKENS, ConfigError, load_config


def thinking_manifest(tokens: int) -> dict:
    manifest = support.manifest()
    manifest.pop("temperature", None)
    manifest["thinking"] = "enabled"
    manifest["reasoning_effort"] = "high"
    manifest["protocol_version"] = "EXP-01-THINKING-CANDIDATE-v1"
    manifest["max_output_tokens_per_call"] = tokens
    return manifest


class ThinkingOutputBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="j11-budget-")
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name)
        self.database = self.directory / "desktop.sqlite"

    def load(self, document: dict) -> object:
        path = support.write_config(self.directory, document, name="host.json")
        return load_config(path)

    def document(self, tokens: int) -> dict:
        document = support.config_document(self.database)
        document["destination"]["manifest"] = thinking_manifest(tokens)
        return document

    def test_the_floor_is_the_measured_proposal_budget(self) -> None:
        self.assertEqual(MIN_THINKING_OUTPUT_TOKENS, 8192)

    def test_a_thinking_manifest_below_the_floor_is_refused(self) -> None:
        for tokens in (2048, 4096, 8191):
            with self.assertRaises(ConfigError) as caught:
                self.load(self.document(tokens))
            self.assertEqual(caught.exception.code, "CONFIG_INVALID")
            self.assertIn("8192", caught.exception.message)

    def test_a_thinking_manifest_at_the_floor_loads(self) -> None:
        config = self.load(self.document(8192))
        self.assertEqual(config.model.manifest.thinking, "enabled")
        self.assertEqual(config.model.manifest.max_output_tokens_per_call, 8192)

    def test_a_manifest_without_thinking_keeps_a_small_budget(self) -> None:
        document = support.config_document(self.database)
        self.assertEqual(document["destination"]["manifest"]["thinking"], "disabled")
        config = self.load(document)
        self.assertLess(config.model.manifest.max_output_tokens_per_call, 8192)


if __name__ == "__main__":
    unittest.main()
