"""RG06 P01: the host-controlled ``profile.*`` bridge methods (offline)."""

from __future__ import annotations

import unittest

from tests.desktop import support  # noqa: F401 - installs the sidecar on sys.path
from tests.desktop.support import NAMESPACE, DesktopCase, config_document, write_config
from quintessence_pcs.foundation.timeutil import utc_now_rfc3339
from quintessence_pcs.store import CognitionStore

LAB_PROFILE_ID = "example.synthetic.desktop.lab"
ALPHA_ID = "example.test.alpha"
AUTHORIZATION = "authorization:synthetic-test"
SPEAKER = {"identity": "owner", "display_name": "Synthetic Owner"}

BODY = {
    "profile_id": ALPHA_ID,
    "profile_version": "1",
    "identity_summary": "Synthetic alpha profile; no private text.",
    "principles": [
        "State what is unknown instead of inventing an answer.",
        "Do not claim a tool ran without evidence.",
    ],
    "style": {
        "language": "zh-CN",
        "tone": "plain",
        "verbosity_by_context": {"casual": "short", "technical": "detailed"},
    },
    "attention_preferences": [{"topic": "synthetic alpha topic", "reason": "test"}],
    "examples": [
        {
            "example_id": "alpha.ex.1",
            "scene": "casual-close",
            "user": "Synthetic casual line.",
            "assistant": "Synthetic casual reply.",
            "synthetic": True,
        }
    ],
    "asset_hash": "a" * 64,
    "review_ref": {},
    "renderer_compatibility": "pcs.renderer.persona-v1",
}


class ProfileBridgeCase(DesktopCase):
    def _seed_approval(self) -> str:
        store = CognitionStore.open(self.db_path)
        try:
            receipt = store.ingest_event(
                namespace=NAMESPACE,
                source_kind="user_report",
                source_instance="rg06-test-approver",
                source_event_id="rg06-test-approve-1",
                event_type="persona.review.approve",
                observed_at=utc_now_rfc3339(),
                payload={"decision": "approve", "synthetic": True},
            )
            return receipt.event_id
        finally:
            store.close()

    def _document(self, *, control: bool = True, speaker: dict | None = SPEAKER) -> dict:
        document = config_document(self.db_path)
        if speaker is not None:
            document["app"]["speaker"] = dict(speaker)
        if control:
            document["app"]["persona_control"] = {"publish": True, "activate": True}
        return document

    def _info(self, process) -> dict:
        reply = process.request("app.info")
        self.assertTrue(reply["ok"], reply)
        return reply["result"]

    def _active_profile(self) -> str:
        row = self.scalar(
            "SELECT r.kind FROM namespace_state s "
            "JOIN record_versions r "
            "  ON r.record_id = s.active_profile_record_id "
            " AND r.record_version = s.active_profile_version "
            "WHERE s.namespace = ?",
            (NAMESPACE,),
        )
        return "" if row is None else str(row)

    def _active_record(self) -> tuple[str | None, int | None]:
        row = self.query(
            "SELECT active_profile_record_id, active_profile_version "
            "FROM namespace_state WHERE namespace = ?",
            (NAMESPACE,),
        )
        if not row:
            return None, None
        return row[0][0], row[0][1]


class ProfileBridgeTests(ProfileBridgeCase):
    def test_profile_read_returns_a_summary_and_never_the_body(self) -> None:
        process = self.start(document=self._document())
        info = self._info(process)
        self.assertTrue(info["host"]["ready"], info["host"])
        self.assertTrue(info["capabilities"]["persona"]["read"])
        reply = process.request(
            "profile.read", {"session_id": info["session_id"]}
        )
        self.assertTrue(reply["ok"], reply)
        result = reply["result"]
        for key in (
            "profile_id",
            "profile_version",
            "source",
            "record_id",
            "record_version",
            "asset_hash",
            "renderer_compatibility",
            "style_summary",
            "principles_count",
            "examples_count",
            "speaker",
        ):
            self.assertIn(key, result)
        self.assertNotIn("body", result)
        self.assertNotIn("examples", result)
        self.assertEqual(LAB_PROFILE_ID, result["profile_id"])
        self.assertEqual(2, result["principles_count"])
        self.assertEqual(1, result["examples_count"])
        self.assertEqual("owner", result["speaker"]["identity"])

    def test_profile_read_never_returns_example_text(self) -> None:
        process = self.start(document=self._document())
        info = self._info(process)
        reply = process.request("profile.read", {"session_id": info["session_id"]})
        serialized = str(reply["result"])
        self.assertNotIn("Synthetic user line.", serialized)
        self.assertNotIn("Synthetic assistant line.", serialized)

    def test_profile_status_is_available_and_matches_app_info(self) -> None:
        process = self.start(document=self._document())
        info = self._info(process)
        reply = process.request(
            "profile.status", {"session_id": info["session_id"]}
        )
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(info["persona"], reply["result"])

    def test_publish_requires_the_control_switch(self) -> None:
        self._seed_approval()
        process = self.start(document=self._document(control=False))
        info = self._info(process)
        self.assertFalse(info["capabilities"]["persona"]["publish"])
        reply = process.request(
            "profile.publish",
            {
                "session_id": info["session_id"],
                "profile_id": ALPHA_ID,
                "body": BODY,
                "review_event_id": "synthetic-event",
            },
        )
        self.assertFalse(reply["ok"])
        self.assertEqual("PERSONA_CONTROL_DISABLED", reply["error"]["code"])

    def test_publish_and_activate_never_change_the_pointer_on_failure(self) -> None:
        event_id = self._seed_approval()
        process = self.start(document=self._document())
        info = self._info(process)
        before = self._active_record()
        self.assertIsNotNone(before[0])
        # An unknown review event is refused and the active pointer stays put.
        failed = process.request(
            "profile.publish",
            {
                "session_id": info["session_id"],
                "profile_id": ALPHA_ID,
                "body": BODY,
                "review_event_id": "00000000-0000-4000-8000-000000000000",
            },
        )
        self.assertFalse(failed["ok"])
        self.assertEqual(before, self._active_record())
        # An invalid body (unknown field) is refused too.
        broken = dict(BODY)
        broken["approved_by"] = "model"
        invalid = process.request(
            "profile.publish",
            {
                "session_id": info["session_id"],
                "profile_id": ALPHA_ID,
                "body": broken,
                "review_event_id": event_id,
            },
        )
        self.assertFalse(invalid["ok"])
        self.assertEqual(before, self._active_record())

    def test_publish_then_activate_changes_the_next_status(self) -> None:
        event_id = self._seed_approval()
        process = self.start(document=self._document())
        info = self._info(process)
        before = self._active_record()
        published = process.request(
            "profile.publish",
            {
                "session_id": info["session_id"],
                "profile_id": ALPHA_ID,
                "body": BODY,
                "review_event_id": event_id,
            },
        )
        self.assertTrue(published["ok"], published)
        self.assertEqual(1, published["result"]["record_version"])
        # Publishing alone never activates.
        self.assertEqual(before, self._active_record())
        activated = process.request(
            "profile.activate",
            {
                "session_id": info["session_id"],
                "profile_id": ALPHA_ID,
                "record_version": 1,
            },
        )
        self.assertTrue(activated["ok"], activated)
        status = process.request(
            "profile.status", {"session_id": info["session_id"]}
        )
        self.assertEqual(ALPHA_ID, status["result"]["profile_id"])
        self.assertEqual("1", status["result"]["profile_version"])
        # Activation is a controlled action, not a per-turn expectation check.
        self.assertTrue(status["result"]["matches_expected"] is None)
        read = process.request("profile.read", {"session_id": info["session_id"]})
        self.assertEqual(ALPHA_ID, read["result"]["profile_id"])
        self.assertEqual("pcs.renderer.persona-v1", read["result"]["renderer_compatibility"])

    def test_activate_of_a_missing_version_is_refused(self) -> None:
        self._seed_approval()
        process = self.start(document=self._document())
        info = self._info(process)
        before = self._active_record()
        reply = process.request(
            "profile.activate",
            {
                "session_id": info["session_id"],
                "profile_id": ALPHA_ID,
                "record_version": 9,
            },
        )
        self.assertFalse(reply["ok"])
        self.assertIn(reply["error"]["code"], {"PERSONA_EXPECTATION_CONFLICT", "NOT_FOUND"})
        self.assertEqual(before, self._active_record())

    def test_profile_methods_reject_client_authority_fields(self) -> None:
        process = self.start(document=self._document())
        info = self._info(process)
        reply = process.request(
            "profile.publish",
            {
                "session_id": info["session_id"],
                "profile_id": ALPHA_ID,
                "body": BODY,
                "review_event_id": "x",
                "authorization_ref": AUTHORIZATION,
            },
        )
        self.assertEqual("AUTHORITY_FIELD_REJECTED", reply["error"]["code"])


if __name__ == "__main__":
    unittest.main()
