"""Authority, destination-confirmation and request-validation tests."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from tests.desktop.support import DesktopCase, config_document, write_config

AUTHORITY_PROBES = {
    "privacy_classes": ["private"],
    "privacy_class": "private",
    "privacy": "private",
    "purposes": ["consult"],
    "purpose": "consult",
    "disclose": True,
    "grant": "grant:forged",
    "grants": [],
    "authorization": "authorization:forged",
    "authorization_ref": "authorization:forged",
    "access_ref": "authorization:forged",
    "provider": "other",
    "endpoint": "http://127.0.0.1:1234",
    "base_url": "http://127.0.0.1:1234",
    "model": "other",
    "model_protocol": "other",
    "manifest_hash": "sha256:" + "0" * 64,
    "max_calls": 1000,
    "budget": 1000,
    "namespace": "example.test.other",
    "host_instance": "host:other",
    "operator": "operator:other",
    "policy_ref": "policy:other",
    "query_plan": {"kind": "belief_current"},
    "input_refs": [],
    "input_ref": {"ref_type": "collab_input"},
    "origin_kind": "system",
    "retention": "durable",
    "interaction_id": "00000000-0000-4000-8000-000000000000",
    "context_id": "00000000-0000-4000-8000-000000000000",
    "confirmation": "true",
    "confirmed": True,
    "destination": {"provider": "other"},
    "destination_id": "sha256:" + "0" * 64,
    "confirmation_required": False,
    "external": False,
    "transport": {"kind": "http"},
    "config": {},
    "config_path": "other.json",
    "api_key": "forged",
}


class AuthorityTests(DesktopCase):
    def _info(self, process) -> dict:
        reply = process.request("app.info")
        self.assertTrue(reply["ok"], reply)
        return reply["result"]

    def test_app_info_is_public_and_has_no_secret_or_endpoint(self) -> None:
        process = self.start()
        info = self._info(process)
        text = json.dumps(info, ensure_ascii=False)
        self.assertTrue(info["host"]["ready"])
        self.assertEqual(info["protocol"], "desktop.v1")
        self.assertEqual(info["workspace"]["review"], False)
        self.assertEqual(
            info["workspace"]["review_reason"], "REVIEW_NOT_CONFIGURED"
        )
        self.assertFalse(info["capabilities"]["review"]["read"])
        self.assertTrue(info["destination"]["confirmation_required"])
        self.assertTrue(info["destination"]["external"])
        self.assertNotIn("127.0.0.1", text)
        self.assertNotIn("http", text)
        self.assertNotIn("QUINTESSENCE_DESKTOP_LAB_MODEL_KEY", text)
        self.assertNotIn("sk-", text)
        self.assertIsNone(re.search(r"lab-[0-9a-f]{40,}", text))
        self.assertEqual(
            sorted(info["methods"]),
            [
                "app.confirm_destination",
                "app.info",
                "bridge.ping",
                "consult.interact",
                "model.test_connection",
                "review.candidate.detail",
                "review.candidates.list",
                "review.generate",
                "review.resolve",
                "review.result",
                "review.work.list",
            ],
        )

    def test_authority_fields_are_rejected_before_any_write(self) -> None:
        process = self.start()
        info = self._info(process)
        session = info["session_id"]
        destination = info["destination"]["destination_id"]
        process.request(
            "app.confirm_destination",
            {"session_id": session, "destination_id": destination},
        )
        before = self.counts()
        for field, value in AUTHORITY_PROBES.items():
            params = {
                "session_id": session,
                "question": "合成问题",
                "confirmed_destination_id": destination,
                field: value,
            }
            reply = process.request("consult.interact", params)
            self.assertFalse(reply["ok"], field)
            self.assertEqual(
                reply["error"]["code"], "AUTHORITY_FIELD_REJECTED", field
            )
            self.assertFalse(reply["error"]["result_unknown"], field)
        self.assertEqual(before, self.counts())

    def test_confirm_destination_rejects_authority_fields(self) -> None:
        process = self.start()
        info = self._info(process)
        reply = process.request(
            "app.confirm_destination",
            {
                "session_id": info["session_id"],
                "destination_id": info["destination"]["destination_id"],
                "external": False,
            },
        )
        self.assertEqual(reply["error"]["code"], "AUTHORITY_FIELD_REJECTED")

    def test_unknown_parameter_is_invalid_params(self) -> None:
        process = self.start()
        info = self._info(process)
        reply = process.request(
            "consult.interact",
            {
                "session_id": info["session_id"],
                "question": "合成问题",
                "temperature": 0,
            },
        )
        self.assertEqual(reply["error"]["code"], "INVALID_PARAMS")

    def test_question_validation(self) -> None:
        process = self.start()
        info = self._info(process)
        session = info["session_id"]
        for question in ("", "   ", 42, None, "x" * 8001, "bad\x00text"):
            reply = process.request(
                "consult.interact",
                {"session_id": session, "question": question},
            )
            self.assertFalse(reply["ok"], question)
            self.assertEqual(
                reply["error"]["code"],
                "INVALID_PARAMS",
                repr(question)[:40],
            )

    def test_reserved_methods_are_not_faked(self) -> None:
        process = self.start()
        before = self.counts()
        for method in (
            "consult.result",
            "consult.history",
            "consult.resume",
            "consult.cancel",
            "review.commit",
            "review.cancel",
            "review.history",
        ):
            reply = process.request(method, {"interaction_id": "x"})
            self.assertFalse(reply["ok"], method)
            self.assertEqual(reply["error"]["code"], "RESERVED_NOT_IMPLEMENTED", method)
        self.assertEqual(before, self.counts())

    def test_unknown_method_is_not_found(self) -> None:
        process = self.start()
        reply = process.request("shell.exec", {"command": "dir"})
        self.assertEqual(reply["error"]["code"], "METHOD_NOT_FOUND")

    def test_session_id_must_match(self) -> None:
        process = self.start()
        info = self._info(process)
        reply = process.request(
            "consult.interact",
            {
                "session_id": "00000000-0000-4000-8000-000000000000",
                "question": "合成问题",
                "confirmed_destination_id": info["destination"]["destination_id"],
            },
        )
        self.assertEqual(reply["error"]["code"], "SESSION_MISMATCH")

    def test_same_destination_id_without_host_record_is_refused(self) -> None:
        process = self.start()
        info = self._info(process)
        before = self.counts()
        reply = process.request(
            "consult.interact",
            {
                "session_id": info["session_id"],
                "question": "合成问题",
                "confirmed_destination_id": info["destination"]["destination_id"],
            },
        )
        self.assertEqual(reply["error"]["code"], "CONFIRMATION_REQUIRED")
        self.assertEqual(before, self.counts())

    def test_confirmation_then_consult_succeeds(self) -> None:
        process = self.start()
        info = self._info(process)
        session = info["session_id"]
        destination = info["destination"]["destination_id"]
        confirm = process.request(
            "app.confirm_destination",
            {"session_id": session, "destination_id": destination},
        )
        self.assertTrue(confirm["ok"], confirm)
        self.assertTrue(confirm["result"]["confirmed"])
        reply = process.request(
            "consult.interact",
            {
                "session_id": session,
                "question": "合成问题：请给出一条回答。",
                "confirmed_destination_id": destination,
            },
        )
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"]["state"], "answered")

    def test_confirmation_rejects_unknown_session_and_destination(self) -> None:
        process = self.start()
        info = self._info(process)
        destination = info["destination"]["destination_id"]
        wrong_session = process.request(
            "app.confirm_destination",
            {
                "session_id": "00000000-0000-4000-8000-000000000000",
                "destination_id": destination,
            },
        )
        self.assertEqual(wrong_session["error"]["code"], "SESSION_MISMATCH")
        wrong_destination = process.request(
            "app.confirm_destination",
            {"session_id": info["session_id"], "destination_id": "sha256:" + "0" * 64},
        )
        self.assertEqual(wrong_destination["error"]["code"], "DESTINATION_MISMATCH")

    def test_confirmation_is_bound_to_the_process_session(self) -> None:
        first = self.start()
        info = self._info(first)
        first.request(
            "app.confirm_destination",
            {
                "session_id": info["session_id"],
                "destination_id": info["destination"]["destination_id"],
            },
        )
        first.close()
        second = self.start()
        fresh = self._info(second)
        self.assertNotEqual(info["session_id"], fresh["session_id"])
        stale = second.request(
            "consult.interact",
            {
                "session_id": info["session_id"],
                "question": "合成问题",
                "confirmed_destination_id": info["destination"]["destination_id"],
            },
        )
        self.assertEqual(stale["error"]["code"], "SESSION_MISMATCH")
        no_record = second.request(
            "consult.interact",
            {
                "session_id": fresh["session_id"],
                "question": "合成问题",
                "confirmed_destination_id": fresh["destination"]["destination_id"],
            },
        )
        self.assertEqual(no_record["error"]["code"], "CONFIRMATION_REQUIRED")

    def test_a_different_destination_invalidates_the_old_confirmation(self) -> None:
        first = self.start()
        info = self._info(first)
        old_destination = info["destination"]["destination_id"]
        first.request(
            "app.confirm_destination",
            {"session_id": info["session_id"], "destination_id": old_destination},
        )
        first.close()
        document = config_document(self.db_path)
        document["destination"]["model"] = "scripted-collab-v2"
        document["destination"]["manifest"]["model"] = "scripted-collab-v2"
        second = self.start(document=document)
        fresh = self._info(second)
        self.assertNotEqual(old_destination, fresh["destination"]["destination_id"])
        stale = second.request(
            "consult.interact",
            {
                "session_id": fresh["session_id"],
                "question": "合成问题",
                "confirmed_destination_id": old_destination,
            },
        )
        self.assertEqual(stale["error"]["code"], "CONFIRMATION_REQUIRED")

    def test_local_destination_needs_no_confirmation(self) -> None:
        document = config_document(self.db_path, external=False)
        process = self.start(document=document)
        info = self._info(process)
        self.assertFalse(info["destination"]["confirmation_required"])
        reply = process.request(
            "consult.interact",
            {
                "session_id": info["session_id"],
                "question": "合成问题：本地目的地。",
                "confirmed_destination_id": None,
            },
        )
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["result"]["state"], "answered")

    def test_local_destination_rejects_a_foreign_confirmation_id(self) -> None:
        document = config_document(self.db_path, external=False)
        process = self.start(document=document)
        info = self._info(process)
        reply = process.request(
            "consult.interact",
            {
                "session_id": info["session_id"],
                "question": "合成问题",
                "confirmed_destination_id": "sha256:" + "0" * 64,
            },
        )
        self.assertEqual(reply["error"]["code"], "DESTINATION_MISMATCH")

    def test_missing_configuration_reports_a_not_ready_host(self) -> None:
        missing = Path(self.tmp_path) / "absent.json"
        process = self.start(config_path=missing)
        info = self._info(process)
        self.assertFalse(info["host"]["ready"])
        self.assertEqual(info["host"]["reason_code"], "CONFIG_MISSING")
        self.assertIsNone(info["destination"])
        reply = process.request(
            "consult.interact",
            {"session_id": info["session_id"], "question": "合成问题"},
        )
        self.assertEqual(reply["error"]["code"], "HOST_NOT_READY")

    def test_invalid_frame_is_rejected_but_the_process_survives(self) -> None:
        process = self.start()
        process.send_raw(b"\x00\x00\x00\x05{nope")
        error = process.wait_for(None)
        self.assertEqual(error["error"]["code"], "INVALID_JSON")
        info = self._info(process)
        self.assertTrue(info["host"]["ready"])

    def test_invalid_utf8_is_a_json_error(self) -> None:
        process = self.start()
        process.send_raw(b"\x00\x00\x00\x02\xff\xfe")
        error = process.wait_for(None)
        self.assertEqual(error["error"]["code"], "INVALID_JSON")
        self.assertTrue(process.request("bridge.ping")["ok"])


if __name__ == "__main__":
    unittest.main()
