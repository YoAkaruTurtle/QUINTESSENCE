"""End-to-end tests over a real ConsultFacade, Context, Attempt and Artifact."""

from __future__ import annotations

import json
import sqlite3
import unittest

from tests.desktop.support import DesktopCase, config_document


def payload_body(text: str):
    value = json.loads(text)
    return value.get("body") if isinstance(value, dict) else value


class ConsultEndToEndTests(DesktopCase):
    def _process(self, document: dict | None = None):
        process = self.start(document=document)
        info = process.request("app.info")["result"]
        process.request(
            "app.confirm_destination",
            {
                "session_id": info["session_id"],
                "destination_id": info["destination"]["destination_id"],
            },
        )
        return process, info

    def _consult(self, process, info, question: str, *, destination: str | None = None):
        return process.request(
            "consult.interact",
            {
                "session_id": info["session_id"],
                "question": question,
                "confirmed_destination_id": (
                    info["destination"]["destination_id"]
                    if destination is None
                    else destination
                ),
            },
        )

    def test_answered_consult_writes_the_real_chain(self) -> None:
        process, info = self._process()
        before = self.counts()
        question = "合成问题：请给出本周评审的三条待办。"
        reply = self._consult(process, info, question)
        self.assertTrue(reply["ok"], reply)
        result = reply["result"]
        self.assertEqual(result["state"], "answered")
        self.assertTrue(result["reply"])
        self.assertIsNotNone(result["query"])
        self.assertEqual(result["query"]["kind"], "belief")
        self.assertIn(result["query"]["status"], ("ok", "empty"))
        self.assertTrue(result["query_source_refs"])
        self.assertNotIn("generations", result)
        self.assertNotIn("attempts", result)
        self.assertNotIn("prompt", json.dumps(result).lower())

        after = self.counts()
        self.assertEqual(after["collab_inputs"], before["collab_inputs"] + 1)
        self.assertEqual(
            after["collab_interactions"], before["collab_interactions"] + 1
        )
        self.assertEqual(after["collab_artifacts"], before["collab_artifacts"] + 2)
        self.assertEqual(after["model_attempts"], before["model_attempts"] + 2)
        self.assertEqual(
            after["context_snapshots"], before["context_snapshots"] + 2
        )
        for table in (
            "records",
            "record_versions",
            "pending_work",
            "work_progress",
            "processed_commands",
            "event_journal",
        ):
            self.assertEqual(after[table], before[table], table)

        input_row = self.query(
            "SELECT i.origin_kind, p.body_json, i.privacy_class, "
            "i.retention_policy, i.content_version "
            "FROM collab_inputs i JOIN private_payloads p "
            "ON p.payload_ref = i.body_payload_ref WHERE i.input_id = ?",
            (result["input"]["input_ref"]["input_id"],),
        )[0]
        self.assertEqual(input_row["origin_kind"], "user_statement")
        self.assertEqual(payload_body(input_row["body_json"]), question)
        self.assertEqual(input_row["privacy_class"], "private")
        self.assertEqual(input_row["retention_policy"], "standard")
        self.assertEqual(input_row["content_version"], 1)

        interaction = self.query(
            "SELECT mode, result, access_ref, policy_ref, context_id, "
            "host_instance FROM collab_interactions WHERE interaction_id = ?",
            (result["interaction_id"],),
        )[0]
        self.assertEqual(interaction["mode"], "consult")
        self.assertEqual(interaction["result"], "answered")
        self.assertEqual(
            interaction["access_ref"], "authorization:host:desktop-lab"
        )
        self.assertEqual(interaction["policy_ref"], "policy:collab.v1")
        self.assertIsNotNone(interaction["context_id"])
        input_links = self.query(
            "SELECT input_id, content_version FROM collab_interaction_inputs "
            "WHERE interaction_id = ?",
            (result["interaction_id"],),
        )
        self.assertEqual(len(input_links), 1)
        self.assertEqual(
            input_links[0]["input_id"],
            result["input"]["input_ref"]["input_id"],
        )
        self.assertEqual(interaction["host_instance"], "host:desktop-lab")

        contexts = self.query(
            "SELECT context_id, run_id FROM context_snapshots ORDER BY rowid"
        )
        self.assertEqual(len(contexts), 2)
        self.assertEqual(contexts[1]["context_id"], result["context_id"])
        self.assertNotEqual(contexts[0]["context_id"], contexts[1]["context_id"])

        attempts = self.query(
            "SELECT attempt_id, attempt_no, context_id, outcome, request_hash "
            "FROM model_attempts ORDER BY attempt_no"
        )
        self.assertEqual(len(attempts), 2)
        self.assertEqual([row["outcome"] for row in attempts], ["ok", "ok"])
        self.assertNotEqual(attempts[0]["context_id"], attempts[1]["context_id"])

        artifacts = self.query(
            "SELECT a.artifact_id, a.kind, a.origin_kind, p.body_json, "
            "a.attempt_ref_json FROM collab_artifacts a JOIN private_payloads p "
            "ON p.payload_ref = a.body_payload_ref ORDER BY a.kind"
        )
        kinds = {row["kind"] for row in artifacts}
        self.assertEqual(kinds, {"query_result", "reply"})
        reply_artifact = [row for row in artifacts if row["kind"] == "reply"][0]
        self.assertEqual(reply_artifact["origin_kind"], "model_output")
        self.assertEqual(payload_body(reply_artifact["body_json"]), result["reply"])
        query_artifact = [row for row in artifacts if row["kind"] == "query_result"][0]
        self.assertEqual(query_artifact["origin_kind"], "deterministic_query")
        self.assertIsNone(query_artifact["attempt_ref_json"])

        attempt_ref = json.loads(reply_artifact["attempt_ref_json"])
        self.assertEqual(attempt_ref["attempt_id"], attempts[1]["attempt_id"])
        self.assertEqual(
            attempt_ref["request_hash"], attempts[1]["request_hash"]
        )
        self.assertEqual(
            result["reply_artifact_ref"]["artifact_id"],
            reply_artifact["artifact_id"],
        )
        self.assertEqual(
            result["query_result_ref"]["artifact_id"],
            query_artifact["artifact_id"],
        )

    def test_waiting_is_a_valid_result_without_an_answer(self) -> None:
        document = config_document(
            self.db_path, script=[{"action": "wait", "brief_reason": "暂不回答"}]
        )
        process, info = self._process(document)
        result = self._consult(process, info, "合成问题：等待。")["result"]
        self.assertEqual(result["state"], "waiting")
        self.assertIsNone(result["reply"])
        self.assertEqual(
            self.scalar(
                "SELECT result FROM collab_interactions WHERE interaction_id = ?",
                (result["interaction_id"],),
            ),
            "waiting",
        )

    def test_failed_is_not_packaged_as_an_answer(self) -> None:
        document = config_document(self.db_path, script=[{"action": "none"}])
        process, info = self._process(document)
        reply = self._consult(process, info, "合成问题：失败。")
        self.assertTrue(reply["ok"], reply)
        result = reply["result"]
        self.assertEqual(result["state"], "failed")
        self.assertIsNone(result["reply"])
        self.assertTrue(result["detail"])
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM collab_artifacts"), 0
        )
        self.assertEqual(
            self.scalar(
                "SELECT result FROM collab_interactions WHERE interaction_id = ?",
                (result["interaction_id"],),
            ),
            "failed",
        )

    def test_incomplete_is_non_terminal_and_has_no_answer(self) -> None:
        document = config_document(
            self.db_path,
            script=[{"action": "query", "brief_reason": "需要查询"}],
            query_plan=None,
        )
        process, info = self._process(document)
        result = self._consult(process, info, "合成问题：缺少查询计划。")["result"]
        self.assertEqual(result["state"], "incomplete")
        self.assertIsNone(result["reply"])
        self.assertEqual(
            self.scalar(
                "SELECT result FROM collab_interactions WHERE interaction_id = ?",
                (result["interaction_id"],),
            ),
            "incomplete",
        )
        self.assertIsNone(
            self.scalar(
                "SELECT completed_at FROM collab_interactions WHERE interaction_id = ?",
                (result["interaction_id"],),
            )
        )

    def test_input_registration_failure_creates_no_interaction(self) -> None:
        process, info = self._process()
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                "CREATE TRIGGER desktop_block_input BEFORE INSERT ON collab_inputs "
                "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
            )
            connection.commit()
        finally:
            connection.close()
        before = self.counts()
        reply = self._consult(process, info, "合成问题：登记失败。")
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"]["code"], "INPUT_REGISTRATION_FAILED")
        self.assertFalse(reply["error"]["result_unknown"])
        after = self.counts()
        self.assertEqual(after["collab_inputs"], before["collab_inputs"])
        self.assertEqual(
            after["collab_interactions"], before["collab_interactions"]
        )
        self.assertEqual(after["model_attempts"], before["model_attempts"])

    def test_input_is_preserved_when_the_consult_fails(self) -> None:
        document = config_document(self.db_path, script=[{"action": "none"}])
        process, info = self._process(document)
        question = "合成问题：登记保留。"
        result = self._consult(process, info, question)["result"]
        self.assertEqual(result["state"], "failed")
        row = self.query(
            "SELECT p.body_json FROM collab_inputs i JOIN private_payloads p "
            "ON p.payload_ref = i.body_payload_ref WHERE i.input_id = ?",
            (result["input"]["input_ref"]["input_id"],),
        )[0]
        self.assertEqual(payload_body(row["body_json"]), question)
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM collab_inputs"), 1
        )


if __name__ == "__main__":
    unittest.main()
