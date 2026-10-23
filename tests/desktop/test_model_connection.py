"""C13b-1: managed host bootstrap and DeepSeek connection probe.

The public client has no pre-provisioned private host config.  These tests
exercise the generated managed config, neutral profile/model authorization
provisioning, and the side-effect-free ``model.test_connection`` path against a
local mock provider.  No real API key or external endpoint is used.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.desktop.support import (
    REVIEW_PERMISSIONS,
    DesktopCase,
    config_document,
    review_proposal,
    seed_work,
)


class _MockProvider:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.mode = "ok"
        self.content = "ok"
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: ANN001 - quiet test
                return

            def do_POST(self):  # noqa: N802 - stdlib hook
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                outer.requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "body": json.loads(body),
                    }
                )
                if outer.mode == "unauthorized":
                    payload = b'{"error":{"message":"invalid key"}}'
                    self.send_response(401)
                elif outer.mode == "rate":
                    payload = b'{"error":{"message":"rate limited"}}'
                    self.send_response(429)
                elif outer.mode == "server":
                    payload = b'{"error":{"message":"server failure"}}'
                    self.send_response(500)
                elif outer.mode == "invalid":
                    payload = b"not-json"
                    self.send_response(200)
                else:
                    payload = json.dumps(
                        {
                            "id": "mock",
                            "object": "chat.completion",
                            "created": 0,
                            "model": "mock-model",
                            "choices": [
                                {
                                    "index": 0,
                                    "finish_reason": "stop",
                                    "message": {
                                        "role": "assistant",
                                        "content": outer.content,
                                    },
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 1,
                                "completion_tokens": 1,
                                "total_tokens": 2,
                            },
                        }
                    ).encode("utf-8")
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return Handler


class ManagedModelSettingsTests(DesktopCase):
    def _document(self, port: int):
        document = config_document(
            self.db_path,
            profile="production",
            external=True,
            transport_kind="http",
            review_permissions=REVIEW_PERMISSIONS,
            desktop={"managed": True, "provision": True},
        )
        document["destination"]["manifest"]["base_url"] = (
            f"http://127.0.0.1:{port}/v1"
        )
        document["destination"]["manifest"]["api_key_env"] = "C13B_TEST_KEY"
        return document

    def test_managed_install_provisions_generic_host_and_key_missing_state(self):
        document = self._document(9)
        process = self.start(document=document, extra_env={})
        info = process.request("app.info")["result"]
        self.assertFalse(info["host"]["ready"])
        self.assertEqual(info["host"]["reason_code"], "MODEL_KEY_MISSING")
        self.assertEqual(info["destination"]["provider"], "synthetic")
        self.assertIn("model.test_connection", info["methods"])
        # The managed bootstrap created no private/chat artefacts.
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM collab_interactions"), 0)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM model_attempts"), 0)

        reply = process.request(
            "model.test_connection",
            {"session_id": info["session_id"]},
        )
        self.assertFalse(reply["ok"], reply)
        self.assertEqual(reply["error"]["code"], "MODEL_KEY_MISSING")

    def test_real_probe_is_side_effect_free_and_uses_the_saved_key(self):
        provider = _MockProvider()
        try:
            document = self._document(provider.port)
            process = self.start(
                document=document,
                extra_env={"C13B_TEST_KEY": "sk-test-0123456789"},
            )
            info = process.request("app.info")["result"]
            self.assertTrue(info["host"]["ready"], info["host"])
            before = self.counts()
            reply = process.request(
                "model.test_connection",
                {"session_id": info["session_id"]},
            )
            self.assertTrue(reply["ok"], reply)
            self.assertTrue(reply["result"]["connected"])
            self.assertEqual(reply["result"]["provider"], "synthetic")
            self.assertEqual(reply["result"]["model"], "scripted-collab-v1")
            self.assertEqual(len(provider.requests), 1)
            request = provider.requests[0]
            self.assertEqual(request["path"], "/v1/chat/completions")
            self.assertEqual(
                request["authorization"], "Bearer sk-test-0123456789"
            )
            self.assertEqual(request["body"]["model"], "scripted-collab-v1")
            self.assertEqual(request["body"]["messages"][0]["role"], "user")
            self.assertEqual(before["collab_interactions"], self.counts()["collab_interactions"])
            self.assertEqual(before["model_attempts"], self.counts()["model_attempts"])
            self.assertEqual(before["collab_artifacts"], self.counts()["collab_artifacts"])
            config_text = next(self.tmp_path.glob("host-*.json")).read_text(
                encoding="utf-8"
            )
            self.assertNotIn("sk-test-0123456789", config_text)
        finally:
            provider.close()

    def test_provider_auth_failure_is_a_definite_model_error(self):
        provider = _MockProvider()
        provider.mode = "unauthorized"
        try:
            document = self._document(provider.port)
            process = self.start(
                document=document,
                extra_env={"C13B_TEST_KEY": "sk-test-0123456789"},
            )
            info = process.request("app.info")["result"]
            reply = process.request(
                "model.test_connection",
                {"session_id": info["session_id"]},
            )
            self.assertFalse(reply["ok"], reply)
            self.assertEqual(reply["error"]["code"], "MODEL_AUTH_FAILED")
            self.assertFalse(reply["error"]["result_unknown"])
            self.assertEqual(self.scalar("SELECT COUNT(*) FROM model_attempts"), 0)
        finally:
            provider.close()

    def test_invalid_saved_key_is_rejected_before_network(self):
        provider = _MockProvider()
        try:
            document = self._document(provider.port)
            process = self.start(
                document=document,
                extra_env={"C13B_TEST_KEY": "bad key"},
            )
            info = process.request("app.info")["result"]
            reply = process.request(
                "model.test_connection",
                {"session_id": info["session_id"]},
            )
            self.assertFalse(reply["ok"], reply)
            self.assertEqual(reply["error"]["code"], "MODEL_KEY_INVALID")
            self.assertEqual(provider.requests, [])
        finally:
            provider.close()

    def test_real_consult_uses_the_collab_http_path_and_c09(self):
        provider = _MockProvider()
        try:
            document = self._document(provider.port)
            document["destination"]["provider"] = "deepseek"
            document["destination"]["model"] = "deepseek-chat"
            document["destination"]["manifest"]["provider"] = "deepseek"
            document["destination"]["manifest"]["model"] = "deepseek-chat"
            provider.content = json.dumps(
                {"action": "none", "reply": "合成 HTTP Consult 回答"}
            )
            process = self.start(
                document=document,
                extra_env={"C13B_TEST_KEY": "sk-test-0123456789"},
            )
            info = process.request("app.info")["result"]
            self.assertTrue(info["host"]["ready"], info["host"])
            confirm = process.request(
                "app.confirm_destination",
                {
                    "session_id": info["session_id"],
                    "destination_id": info["destination"]["destination_id"],
                },
            )
            self.assertTrue(confirm["ok"], confirm)
            reply = process.request(
                "consult.interact",
                {
                    "session_id": info["session_id"],
                    "question": "请通过真实 HTTP 适配器回答。",
                    "confirmed_destination_id": info["destination"]["destination_id"],
                },
            )
            self.assertTrue(reply["ok"], reply)
            self.assertEqual(reply["result"]["state"], "answered")
            self.assertEqual(reply["result"]["reply"], "合成 HTTP Consult 回答")
            self.assertEqual(len(provider.requests), 1)
            self.assertEqual(self.scalar("SELECT COUNT(*) FROM model_attempts"), 1)
            self.assertEqual(self.scalar("SELECT COUNT(*) FROM collab_interactions"), 1)
        finally:
            provider.close()

    def test_real_review_uses_the_formal_review_path(self):
        provider = _MockProvider()
        try:
            work = seed_work(self.db_path, managed=True)
            document = self._document(provider.port)
            provider.content = json.dumps(
                review_proposal(
                    work["event_id"], valid_until="2027-01-01T00:00:00Z"
                )
            )
            process = self.start(
                document=document,
                extra_env={"C13B_TEST_KEY": "sk-test-0123456789"},
            )
            info = process.request("app.info")["result"]
            self.assertTrue(info["host"]["ready"], info["host"])
            confirm = process.request(
                "app.confirm_destination",
                {
                    "session_id": info["session_id"],
                    "destination_id": info["destination"]["destination_id"],
                },
            )
            self.assertTrue(confirm["ok"], confirm)
            generated = process.request(
                "review.generate",
                {
                    "session_id": info["session_id"],
                    "work_id": work["work_id"],
                    "event_id": work["event_id"],
                    "confirmed_destination_id": info["destination"]["destination_id"],
                },
            )
            self.assertTrue(generated["ok"], generated)
            self.assertEqual(generated["result"]["state"], "pending")
            candidate = generated["result"]["candidate_id"]
            approved = process.request(
                "review.resolve",
                {
                    "session_id": info["session_id"],
                    "candidate_id": candidate,
                    "artifact_version": 1,
                    "decision": "approve",
                },
            )
            self.assertTrue(approved["ok"], approved)
            self.assertEqual(approved["result"]["state"], "committed")
            self.assertIsNotNone(approved["result"]["receipt"])
            self.assertEqual(self.scalar("SELECT COUNT(*) FROM model_attempts"), 1)
            self.assertEqual(
                self.scalar(
                    "SELECT COUNT(*) FROM collab_interactions WHERE mode = 'review'"
                ),
                1,
            )
            self.assertEqual(len(provider.requests), 1)
        finally:
            provider.close()

    def test_rate_limit_server_and_invalid_response_are_distinct(self):
        provider = _MockProvider()
        try:
            for mode, expected in (
                ("rate", "MODEL_RATE_LIMITED"),
                ("server", "MODEL_PROVIDER_ERROR"),
                ("invalid", "MODEL_INVALID_RESPONSE"),
            ):
                provider.mode = mode
                document = self._document(provider.port)
                process = self.start(
                    document=document,
                    extra_env={"C13B_TEST_KEY": "sk-test-0123456789"},
                )
                info = process.request("app.info")["result"]
                reply = process.request(
                    "model.test_connection",
                    {"session_id": info["session_id"]},
                )
                self.assertFalse(reply["ok"], (mode, reply))
                self.assertEqual(reply["error"]["code"], expected, mode)
                self.assertFalse(reply["error"]["result_unknown"])
                process.close()
        finally:
            provider.close()

    def test_network_failure_is_a_definite_non_retry_probe_error(self):
        document = self._document(1)
        process = self.start(
            document=document,
            extra_env={"C13B_TEST_KEY": "sk-test-0123456789"},
        )
        info = process.request("app.info")["result"]
        reply = process.request(
            "model.test_connection",
            {"session_id": info["session_id"]},
        )
        self.assertFalse(reply["ok"], reply)
        self.assertEqual(reply["error"]["code"], "MODEL_NETWORK")
        self.assertFalse(reply["error"]["result_unknown"])

    def test_managed_profile_is_generic_and_not_private(self):
        provider = _MockProvider()
        try:
            document = self._document(provider.port)
            process = self.start(
                document=document,
                extra_env={"C13B_TEST_KEY": "sk-test-0123456789"},
            )
            info = process.request("app.info")["result"]
            self.assertTrue(info["host"]["ready"], info["host"])
            profiles = self.query(
                "SELECT body_payload_ref FROM record_versions "
                "WHERE kind = 'persona_profile'"
            )
            self.assertEqual(len(profiles), 1)
            body = self.scalar(
                "SELECT body_json FROM private_payloads WHERE payload_ref = ?",
                (profiles[0]["body_payload_ref"],),
            )
            self.assertIn("QUINTESSENCE 通用桌面助手", body)
            self.assertNotIn("\u5343\u5c81", body)
        finally:
            provider.close()
