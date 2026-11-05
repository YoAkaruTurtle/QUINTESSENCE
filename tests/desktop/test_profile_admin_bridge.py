"""UI-F02: the reviewed persona-management bridge (list/body/import/publish).

The real sidecar, the real store and the real PersonaProfileService are used.
No test writes a review event by hand except the two seeded-negative cases that
prove the digest binding and the legacy path; the publish path itself must write
its own ``persona.review.approve`` event.
"""

from __future__ import annotations

import json
import time
import unittest
import uuid

from tests.desktop.support import (
    ACCESS_REF,
    NAMESPACE,
    DesktopCase,
    config_document,
)

LAB_PROFILE_ID = "example.synthetic.desktop.lab"
IMPORT_PROFILE_ID = "example.test.imported"
IMPORT_BODY = {
    "profile_id": IMPORT_PROFILE_ID,
    "profile_version": "1.0.0",
    "identity_summary": "Imported synthetic profile; no private text.",
    "principles": ["State what is unknown instead of inventing an answer."],
    "style": {
        "language": "zh-CN",
        "tone": "plain",
        "verbosity_by_context": {"casual": "short", "technical": "detailed"},
    },
    "attention_preferences": [{"topic": "synthetic import", "reason": "test"}],
    "examples": [],
    "asset_hash": "b" * 64,
    "renderer_compatibility": "pcs.renderer.imported-v1",
}


class ProfileAdminBridgeTests(DesktopCase):
    def setUp(self) -> None:
        super().setUp()
        self.import_dir = self.tmp_path / "persona-import"
        self.import_dir.mkdir()

    def document(self, *, control: bool = True, delay_ms: int = 0, script=None):
        document = config_document(self.db_path, delay_ms=delay_ms)
        if script is not None:
            document["destination"]["transport"]["script"] = list(script)
        if control:
            document["app"]["persona_control"] = {"publish": True, "activate": True}
        return document

    def start_admin(self, *, control: bool = True, delay_ms: int = 0, script=None):
        process = self.start(
            document=self.document(control=control, delay_ms=delay_ms, script=script)
        )
        info = process.request("app.info")["result"]
        return process, info

    def write_import(self, name: str = "alpha.json", body: dict | None = None) -> None:
        (self.import_dir / name).write_text(
            json.dumps(IMPORT_BODY if body is None else body, ensure_ascii=False),
            encoding="utf-8",
        )

    def preview(self, process, info, name: str = "alpha.json") -> dict:
        return process.request(
            "profile.import.preview", {"session_id": info["session_id"], "name": name}
        )

    def publish_reviewed(self, process, info, import_id: str, **extra) -> dict:
        params = {"session_id": info["session_id"], "import_id": import_id, "confirm": True}
        params.update(extra)
        return process.request("profile.publish_reviewed", params)

    # ------------------------------------------------------------------- info

    def test_capabilities_and_methods_are_disclosed_honestly(self) -> None:
        process, info = self.start_admin()
        self.assertTrue(info["capabilities"]["persona"]["read_body"])
        self.assertTrue(info["capabilities"]["persona"]["import"])
        self.assertEqual(
            {"read": True, "revoke": True}, info["capabilities"]["preferences"]
        )
        for method in (
            "profile.list",
            "profile.read_body",
            "profile.import.list",
            "profile.import.preview",
            "profile.publish_reviewed",
            "preference.list",
            "preference.revoke",
        ):
            self.assertIn(method, info["methods"])

    def test_neutral_instance_keeps_summary_only(self) -> None:
        process, info = self.start_admin(control=False)
        self.assertFalse(info["capabilities"]["persona"]["read_body"])
        self.assertFalse(info["capabilities"]["persona"]["import"])
        listed = process.request(
            "profile.list", {"session_id": info["session_id"]}
        )
        self.assertTrue(listed["ok"], listed)
        body = process.request(
            "profile.read_body",
            {
                "session_id": info["session_id"],
                "record_id": listed["result"]["profiles"][0]["record_id"],
                "record_version": 1,
            },
        )
        self.assertFalse(body["ok"])
        self.assertEqual("PERSONA_CONTROL_DISABLED", body["error"]["code"])

    def test_activation_only_still_allows_the_manager_body_read(self) -> None:
        document = self.document(control=False)
        document["app"]["persona_control"] = {"publish": False, "activate": True}
        process = self.start(document=document)
        info = process.request("app.info")["result"]
        self.assertTrue(info["capabilities"]["persona"]["read_body"])
        listed = process.request("profile.list", {"session_id": info["session_id"]})
        profile = listed["result"]["profiles"][0]
        body = process.request(
            "profile.read_body",
            {
                "session_id": info["session_id"],
                "record_id": profile["record_id"],
                "record_version": profile["current"]["record_version"],
            },
        )
        self.assertTrue(body["ok"], body)
        self.assertEqual(LAB_PROFILE_ID, body["result"]["body"]["profile_id"])

    # ------------------------------------------------------------------- list

    def test_list_reports_versions_and_active_pointer_without_body(self) -> None:
        process, info = self.start_admin()
        listed = process.request("profile.list", {"session_id": info["session_id"]})
        self.assertTrue(listed["ok"], listed)
        result = listed["result"]
        self.assertIsNotNone(result["active"])
        self.assertEqual(LAB_PROFILE_ID, result["active"]["profile_id"])
        profile = next(
            item for item in result["profiles"] if item["profile_id"] == LAB_PROFILE_ID
        )
        self.assertTrue(profile["is_active"])
        self.assertEqual(1, profile["current"]["record_version"])
        self.assertEqual("active", profile["current"]["record_status"])
        self.assertEqual(1, profile["versions_total"])
        self.assertTrue(profile["versions"][0]["is_active_version"])
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("Synthetic desktop Lab profile", serialized)
        self.assertNotIn("Synthetic user line", serialized)
        self.assertNotIn("identity_summary", serialized)

    def test_read_body_returns_the_exact_body_and_writes_one_audit_row(self) -> None:
        process, info = self.start_admin()
        listed = process.request("profile.list", {"session_id": info["session_id"]})
        profile = listed["result"]["profiles"][0]
        reply = process.request(
            "profile.read_body",
            {
                "session_id": info["session_id"],
                "record_id": profile["record_id"],
                "record_version": profile["current"]["record_version"],
            },
        )
        self.assertTrue(reply["ok"], reply)
        body = reply["result"]
        self.assertEqual(LAB_PROFILE_ID, body["profile_id"])
        self.assertEqual("0" * 64, body["body"]["asset_hash"])
        self.assertGreater(body["size_bytes"], 0)
        self.assertEqual(64, len(body["body_sha256"]))
        # UI-F02 review: the read view is the persona text, not the approval
        # metadata.  No review reference and no internal event id may leak.
        self.assertNotIn("review_ref", body["body"])
        self.assertNotIn("review_ref", json.dumps(body, ensure_ascii=False))
        review_event_id = profile["versions"][0]["review_event_id"]
        self.assertIsNotNone(review_event_id)
        self.assertNotIn(review_event_id, json.dumps(body, ensure_ascii=False))
        for key in (
            "profile_id",
            "profile_version",
            "identity_summary",
            "principles",
            "style",
            "attention_preferences",
            "examples",
            "asset_hash",
            "renderer_compatibility",
        ):
            self.assertIn(key, body["body"])
        self.assertEqual(
            "pcs.renderer.synthetic-v1", body["body"]["renderer_compatibility"]
        )
        rows = self.query(
            "SELECT operation, target_id, target_version FROM authorization_audit "
            "WHERE operation = 'persona.profile.read_body'"
        )
        self.assertEqual(1, len(rows))
        self.assertEqual(profile["record_id"], rows[0]["target_id"])
        # A missing version is NOT_FOUND and writes no audit row.
        missing = process.request(
            "profile.read_body",
            {
                "session_id": info["session_id"],
                "record_id": profile["record_id"],
                "record_version": 99,
            },
        )
        self.assertFalse(missing["ok"])
        self.assertEqual("NOT_FOUND", missing["error"]["code"])
        self.assertEqual(
            1,
            self.scalar(
                "SELECT COUNT(*) FROM authorization_audit "
                "WHERE operation = 'persona.profile.read_body'"
            ),
        )

    def test_read_body_refuses_a_body_above_the_host_limit(self) -> None:
        process, info = self.start_admin()
        listed = process.request("profile.list", {"session_id": info["session_id"]})
        profile = listed["result"]["profiles"][0]
        process.close()
        from quintessence_pcs.store import CognitionStore

        store = CognitionStore.open(self.db_path)
        try:
            huge = {
                "profile_id": LAB_PROFILE_ID,
                "profile_version": "0.0.2",
                "identity_summary": "x" * 70_000,
                "principles": [],
                "style": {
                    "language": "zh-CN",
                    "tone": "plain",
                    "verbosity_by_context": {"casual": "short", "technical": "long"},
                },
                "attention_preferences": [],
                "examples": [],
                "asset_hash": "0" * 64,
                "review_ref": {},
                "renderer_compatibility": "pcs.renderer.synthetic-v1",
            }
            store.append_record_version(
                namespace=NAMESPACE,
                record_id=profile["record_id"],
                record_version=2,
                kind="persona_profile",
                body=huge,
                status="active",
                schema_version="2",
            )
        finally:
            store.close()
        process = self.start(document=self.document())
        info = process.request("app.info")["result"]
        reply = process.request(
            "profile.read_body",
            {
                "session_id": info["session_id"],
                "record_id": profile["record_id"],
                "record_version": 2,
            },
        )
        self.assertFalse(reply["ok"])
        self.assertEqual("INVALID_PARAMS", reply["error"]["code"])

    # ----------------------------------------------------------------- import

    def test_import_dir_missing_is_a_real_empty_state(self) -> None:
        process, info = self.start_admin()
        self.import_dir.rmdir()
        reply = process.request(
            "profile.import.list", {"session_id": info["session_id"]}
        )
        self.assertTrue(reply["ok"], reply)
        self.assertFalse(reply["result"]["available"])
        self.assertEqual("IMPORT_UNAVAILABLE", reply["result"]["reason"])
        self.assertEqual([], reply["result"]["files"])
        self.assertNotIn(str(self.tmp_path), json.dumps(reply["result"]))
        preview = self.preview(process, info)
        self.assertFalse(preview["ok"])
        self.assertEqual("IMPORT_UNAVAILABLE", preview["error"]["code"])

    def test_import_lists_only_bare_json_files(self) -> None:
        self.write_import()
        (self.import_dir / "notes.txt").write_text("not imported", encoding="utf-8")
        (self.import_dir / ".hidden.json").write_text("{}", encoding="utf-8")
        (self.import_dir / "sub").mkdir()
        process, info = self.start_admin()
        reply = process.request(
            "profile.import.list", {"session_id": info["session_id"]}
        )
        self.assertTrue(reply["ok"], reply)
        names = [item["name"] for item in reply["result"]["files"]]
        self.assertEqual(["alpha.json"], names)
        self.assertNotIn(str(self.tmp_path), json.dumps(reply["result"]))

    def test_import_preview_returns_a_summary_and_never_the_body(self) -> None:
        self.write_import()
        process, info = self.start_admin()
        reply = self.preview(process, info)
        self.assertTrue(reply["ok"], reply)
        result = reply["result"]
        self.assertEqual(IMPORT_PROFILE_ID, result["profile_id"])
        self.assertEqual(1, result["principles_count"])
        self.assertEqual(0, result["examples_count"])
        self.assertTrue(result["import_id"])
        self.assertEqual(64, len(result["body_sha256"]))
        self.assertNotIn("Imported synthetic profile", json.dumps(result))
        self.assertNotIn("body", result)

    def test_import_preview_replaces_an_old_review_reference(self) -> None:
        body = dict(IMPORT_BODY)
        body["review_ref"] = {
            "ref_type": "event",
            "namespace": NAMESPACE,
            "event_id": str(uuid.uuid4()),
        }
        self.write_import(body=body)
        process, info = self.start_admin()
        reply = self.preview(process, info)
        self.assertTrue(reply["ok"], reply)
        self.assertIn("review_ref_replaced", reply["result"]["warnings"])

    def test_import_rejects_bad_names_and_bad_files(self) -> None:
        process, info = self.start_admin()
        for name in ("../escape.json", "alpha.txt", ".json", ""):
            reply = self.preview(process, info, name)
            self.assertFalse(reply["ok"], name)
            self.assertEqual("INVALID_PARAMS", reply["error"]["code"], name)
        (self.import_dir / "broken.json").write_text("{not json", encoding="utf-8")
        broken = self.preview(process, info, "broken.json")
        self.assertFalse(broken["ok"])
        self.assertEqual("IMPORT_INVALID", broken["error"]["code"])
        missing_keys = dict(IMPORT_BODY)
        del missing_keys["principles"]
        self.write_import("bad-shape.json", body=missing_keys)
        bad = self.preview(process, info, "bad-shape.json")
        self.assertFalse(bad["ok"])
        self.assertEqual("IMPORT_INVALID", bad["error"]["code"])
        self.write_import("extra.json", body={**IMPORT_BODY, "approved_by": "model"})
        extra = self.preview(process, info, "extra.json")
        self.assertFalse(extra["ok"])
        self.assertEqual("IMPORT_INVALID", extra["error"]["code"])
        (self.import_dir / "big.json").write_bytes(
            json.dumps({**IMPORT_BODY, "identity_summary": "x" * 300_000}).encode()
        )
        big = self.preview(process, info, "big.json")
        self.assertFalse(big["ok"])
        self.assertEqual("IMPORT_INVALID", big["error"]["code"])

    # ---------------------------------------------------------------- publish

    def test_publish_reviewed_writes_a_real_bound_event_and_keeps_the_pointer(self) -> None:
        self.write_import()
        process, info = self.start_admin()
        before = self.scalar(
            "SELECT active_profile_record_id, active_profile_version "
            "FROM namespace_state WHERE namespace = ?",
            (NAMESPACE,),
        )
        preview = self.preview(process, info)
        missing_confirm = process.request(
            "profile.publish_reviewed",
            {
                "session_id": info["session_id"],
                "import_id": preview["result"]["import_id"],
                "confirm": False,
            },
        )
        self.assertFalse(missing_confirm["ok"])
        self.assertEqual("CONFIRMATION_REQUIRED", missing_confirm["error"]["code"])
        published = self.publish_reviewed(process, info, preview["result"]["import_id"])
        self.assertTrue(published["ok"], published)
        result = published["result"]
        self.assertFalse(result["active_changed"])
        self.assertFalse(result["reused"])
        self.assertEqual(1, result["record_version"])
        self.assertEqual(
            before,
            self.scalar(
                "SELECT active_profile_record_id, active_profile_version "
                "FROM namespace_state WHERE namespace = ?",
                (NAMESPACE,),
            ),
        )
        events = self.query(
            "SELECT event_id, payload_ref FROM event_journal "
            "WHERE event_type = 'persona.review.approve' ORDER BY event_seq DESC"
        )
        self.assertTrue(events)
        self.assertEqual(result["review_event_id"], events[0]["event_id"])
        payload = json.loads(
            self.scalar(
                "SELECT body_json FROM private_payloads WHERE payload_ref = ?",
                (events[0]["payload_ref"],),
            )
        )
        if isinstance(payload, dict) and "payload" in payload:
            payload = payload["payload"]
        self.assertEqual(result["review_body_sha256"], payload["body_sha256"])
        self.assertEqual("desktop.settings.persona", payload["surface"])
        listed = process.request("profile.list", {"session_id": info["session_id"]})
        imported = next(
            item
            for item in listed["result"]["profiles"]
            if item["profile_id"] == IMPORT_PROFILE_ID
        )
        self.assertFalse(imported["is_active"])
        self.assertEqual(1, imported["versions_total"])

    def test_republishing_the_same_body_reuses_the_version(self) -> None:
        self.write_import()
        process, info = self.start_admin()
        first = self.preview(process, info)
        published = self.publish_reviewed(process, info, first["result"]["import_id"])
        self.assertTrue(published["ok"], published)
        events_before = self.scalar(
            "SELECT COUNT(*) FROM event_journal WHERE event_type = 'persona.review.approve'"
        )
        again = self.preview(process, info)
        second = self.publish_reviewed(process, info, again["result"]["import_id"])
        self.assertTrue(second["ok"], second)
        self.assertTrue(second["result"]["reused"])
        self.assertEqual(
            published["result"]["record_version"], second["result"]["record_version"]
        )
        # The reused version's approval was written by this reviewed path, so it
        # carries the matching digest and may be named.
        self.assertTrue(second["result"]["review_event_bound"])
        self.assertEqual(
            published["result"]["review_event_id"],
            second["result"]["review_event_id"],
        )
        self.assertEqual(
            events_before,
            self.scalar(
                "SELECT COUNT(*) FROM event_journal "
                "WHERE event_type = 'persona.review.approve'"
            ),
        )

    def test_reusing_a_legacy_body_never_returns_an_unbound_event(self) -> None:
        # A legacy approval has no body digest, so it does not bind this body.
        self.write_import()
        process, info = self.start_admin()
        self.assertTrue(self.preview(process, info)["ok"])
        process.close()
        from quintessence_pcs.store import CognitionStore

        store = CognitionStore.open(self.db_path)
        try:
            receipt = store.ingest_event(
                namespace=NAMESPACE,
                source_kind="user_report",
                source_instance="ui-f02-legacy",
                source_event_id=f"ui-f02-legacy-{uuid.uuid4()}",
                event_type="persona.review.approve",
                observed_at="2026-10-29T00:00:00Z",
                payload={"decision": "approve", "synthetic": True},
            )
            legacy_event_id = receipt.event_id
        finally:
            store.close()
        process = self.start(document=self.document())
        info = process.request("app.info")["result"]
        body = {
            **IMPORT_BODY,
            "review_ref": {
                "ref_type": "event",
                "namespace": NAMESPACE,
                "event_id": legacy_event_id,
            },
        }
        legacy = process.request(
            "profile.publish",
            {
                "session_id": info["session_id"],
                "profile_id": IMPORT_PROFILE_ID,
                "body": body,
                "review_event_id": legacy_event_id,
            },
        )
        self.assertTrue(legacy["ok"], legacy)
        again = self.preview(process, info)
        reused = self.publish_reviewed(process, info, again["result"]["import_id"])
        self.assertTrue(reused["ok"], reused)
        self.assertTrue(reused["result"]["reused"])
        self.assertFalse(reused["result"]["review_event_bound"])
        self.assertIsNone(reused["result"]["review_event_id"])
        payload = json.loads(
            self.scalar(
                "SELECT body_json FROM private_payloads WHERE payload_ref = "
                "(SELECT payload_ref FROM event_journal WHERE event_id = ?)",
                (legacy_event_id,),
            )
        )
        if isinstance(payload, dict) and "payload" in payload:
            payload = payload["payload"]
        self.assertNotIn("body_sha256", payload)

    def test_published_version_and_its_approval_survive_a_restart(self) -> None:
        self.write_import()
        process, info = self.start_admin()
        preview = self.preview(process, info)
        published = self.publish_reviewed(process, info, preview["result"]["import_id"])
        self.assertTrue(published["ok"], published)
        process.close()
        process = self.start(document=self.document())
        info = process.request("app.info")["result"]
        listed = process.request("profile.list", {"session_id": info["session_id"]})
        imported = [
            item
            for item in listed["result"]["profiles"]
            if item["profile_id"] == IMPORT_PROFILE_ID
        ]
        self.assertEqual(1, len(imported))
        self.assertEqual(
            published["result"]["record_version"],
            imported[0]["current"]["record_version"],
        )
        self.assertEqual(
            published["result"]["review_event_id"],
            imported[0]["versions"][0]["review_event_id"],
        )
        self.assertEqual(
            1,
            self.scalar(
                "SELECT COUNT(*) FROM event_journal "
                "WHERE event_type = 'persona.review.approve' AND event_id = ?",
                (published["result"]["review_event_id"],),
            ),
        )
        body = process.request(
            "profile.read_body",
            {
                "session_id": info["session_id"],
                "record_id": imported[0]["record_id"],
                "record_version": published["result"]["record_version"],
            },
        )
        self.assertTrue(body["ok"], body)
        self.assertEqual(IMPORT_PROFILE_ID, body["result"]["body"]["profile_id"])

    def test_publish_reviewed_rejects_a_mismatched_profile_id(self) -> None:
        self.write_import()
        process, info = self.start_admin()
        preview = self.preview(process, info)
        reply = self.publish_reviewed(
            process,
            info,
            preview["result"]["import_id"],
            profile_id="example.test.other",
        )
        self.assertFalse(reply["ok"])
        self.assertEqual("INVALID_PARAMS", reply["error"]["code"])
        self.assertIsNone(
            self.scalar(
                "SELECT record_id FROM records WHERE kind = 'persona_profile' "
                "AND record_id != (SELECT active_profile_record_id FROM "
                "namespace_state WHERE namespace = ?)",
                (NAMESPACE,),
            )
        )

    def test_a_digest_mismatched_event_is_refused_by_the_legacy_publish(self) -> None:
        process, info = self.start_admin()
        process.close()
        from quintessence_pcs.store import CognitionStore

        store = CognitionStore.open(self.db_path)
        try:
            receipt = store.ingest_event(
                namespace=NAMESPACE,
                source_kind="user_report",
                source_instance="ui-f02-negative",
                source_event_id=f"ui-f02-negative-{uuid.uuid4()}",
                event_type="persona.review.approve",
                observed_at="2026-10-29T00:00:00Z",
                payload={
                    "decision": "approve",
                    "profile_id": IMPORT_PROFILE_ID,
                    "body_sha256": "f" * 64,
                },
            )
            event_id = receipt.event_id
        finally:
            store.close()
        process = self.start(document=self.document())
        info = process.request("app.info")["result"]
        reply = process.request(
            "profile.publish",
            {
                "session_id": info["session_id"],
                "profile_id": IMPORT_PROFILE_ID,
                "body": {**IMPORT_BODY, "review_ref": {
                    "ref_type": "event",
                    "namespace": NAMESPACE,
                    "event_id": event_id,
                }},
                "review_event_id": event_id,
            },
        )
        self.assertFalse(reply["ok"])
        self.assertEqual("SOURCE_UNTRUSTED", reply["error"]["code"])

    def test_management_reads_are_busy_while_a_turn_runs(self) -> None:
        process, info = self.start_admin(
            delay_ms=1500, script=[{"action": "final", "reply": "嗯。"}]
        )
        process.request(
            "app.confirm_destination",
            {
                "session_id": info["session_id"],
                "destination_id": info["destination"]["destination_id"],
            },
        )
        created = process.request(
            "conversation.create", {"session_id": info["session_id"], "mode": "chat"}
        )
        conversation_id = created["result"]["conversation"]["conversation_id"]
        sent = process.request(
            "conversation.send",
            {
                "session_id": info["session_id"],
                "conversation_id": conversation_id,
                "client_request_id": f"ui-f02-busy-{time.time_ns()}",
                "text": "在跑的一轮",
                "confirmed_destination_id": info["destination"]["destination_id"],
            },
        )
        self.assertTrue(sent["ok"], sent)
        for method, params in (
            ("profile.list", {"session_id": info["session_id"]}),
            (
                "preference.list",
                {"session_id": info["session_id"], "conversation_id": conversation_id},
            ),
        ):
            reply = process.request(method, params)
            self.assertFalse(reply["ok"], method)
            self.assertEqual("BUSY", reply["error"]["code"], method)

    def test_management_methods_reject_authority_fields(self) -> None:
        process, info = self.start_admin()
        listed = process.request("profile.list", {"session_id": info["session_id"]})
        profile = listed["result"]["profiles"][0]
        reply = process.request(
            "profile.read_body",
            {
                "session_id": info["session_id"],
                "record_id": profile["record_id"],
                "record_version": 1,
                "authorization_ref": ACCESS_REF,
            },
        )
        self.assertFalse(reply["ok"])
        self.assertEqual("AUTHORITY_FIELD_REJECTED", reply["error"]["code"])


if __name__ == "__main__":
    unittest.main()
