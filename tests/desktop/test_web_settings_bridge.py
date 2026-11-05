"""RG08: the real web surface status and the real settings switch.

These tests run the actual sidecar over the framed pipe.  `app.info` must report
what the host configuration and environment say, and `settings.reload` must make
an enable/disable reach the *next turn's* tool runtime without a restart.
"""

from __future__ import annotations

import json
import time

from tests.desktop.support import DesktopCase, config_document


def v2_call(name: str, arguments: dict | None = None) -> dict:
    return {
        "action": "call_tool",
        "tool_call": {"name": name, "arguments": arguments or {}},
    }


def v2_final(reply: str) -> dict:
    return {"action": "final", "reply": reply}


class WebSettingsBridgeCase(DesktopCase):
    def _web(self, process) -> dict:
        return process.request("app.info")["result"]["capabilities"]["web"]

    def _start_v2(self, *, document):
        document["destination"]["model_protocol"] = "collab.v2"
        process = self.start(document=document)
        info = process.request("app.info")["result"]
        if info["destination"]["confirmation_required"]:
            confirm = process.request(
                "app.confirm_destination",
                {
                    "session_id": info["session_id"],
                    "destination_id": info["destination"]["destination_id"],
                },
            )
            self.assertTrue(confirm["ok"], confirm)
        return process, info

    def _run_web_turn(self, process, info, request_id: str) -> dict:
        conversation = process.request(
            "conversation.create",
            {"session_id": info["session_id"], "mode": "chat"},
        )["result"]["conversation"]["conversation_id"]
        sent = process.request(
            "conversation.send",
            {
                "session_id": info["session_id"],
                "conversation_id": conversation,
                "client_request_id": request_id,
                "text": "联网状态测试。",
                "confirmed_destination_id": (
                    info["destination"]["destination_id"]
                    if info["destination"]["confirmation_required"]
                    else None
                ),
            },
        )
        self.assertTrue(sent["ok"], sent)
        turn_id = sent["result"]["turn_id"]
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            ping = process.request("bridge.ping")
            self.assertTrue(ping["ok"], ping)
            if not ping["result"]["busy"]:
                result = process.request(
                    "turn.result",
                    {"session_id": info["session_id"], "turn_id": turn_id},
                )
                self.assertTrue(result["ok"], result)
                steps = process.request(
                    "turn.steps",
                    {"session_id": info["session_id"], "turn_id": turn_id},
                )
                self.assertTrue(steps["ok"], steps)
                return {
                    "result": result["result"],
                    "steps": steps["result"]["steps"],
                }
            time.sleep(0.05)
        raise TimeoutError("turn did not settle")

    def test_unconfigured_web_reports_what_is_missing(self) -> None:
        process = self.start()
        web = self._web(process)
        self.assertFalse(web["enabled"])
        self.assertFalse(web["configured"])
        self.assertFalse(web["declared"])
        self.assertFalse(web["credential_present"])
        self.assertEqual(0, web["allowed_hosts"])
        self.assertEqual("any_public_https", web["host_policy"])
        self.assertEqual("lab", web["source"])
        self.assertEqual(["web"], web["missing"])
        self.assertTrue(web["search"])
        self.assertTrue(web["fetch"])

    def test_enabled_without_a_credential_is_not_configured(self) -> None:
        document = config_document(self.db_path)
        document["web"] = {
            "enabled": True,
            "timeout_seconds": 10,
            "api_key_env": "RG08_WEB_KEY",
        }
        process = self.start(document=document, extra_env={"RG08_WEB_KEY": ""})
        web = self._web(process)
        self.assertTrue(web["enabled"])
        self.assertFalse(web["configured"])
        self.assertFalse(web["credential_present"])
        self.assertEqual("RG08_WEB_KEY", web["credential_env"])
        self.assertEqual(["web.api_key_env"], web["missing"])
        self.assertEqual("lab", web["source"])

    def test_present_credential_is_reported_without_the_secret(self) -> None:
        document = config_document(self.db_path)
        document["web"] = {
            "enabled": True,
            "timeout_seconds": 10,
            "allowed_hosts": ["example.com"],
            "api_key_env": "RG08_WEB_KEY",
        }
        secret = "synthetic-rg08-web-credential-not-real"
        process = self.start(
            document=document, extra_env={"RG08_WEB_KEY": secret}
        )
        info = process.request("app.info")
        web = info["result"]["capabilities"]["web"]
        self.assertTrue(web["enabled"])
        self.assertTrue(web["configured"])
        self.assertTrue(web["credential_present"])
        self.assertEqual([], web["missing"])
        self.assertEqual(1, web["allowed_hosts"])
        self.assertEqual("allowlist", web["host_policy"])
        self.assertEqual("RG08_WEB_KEY", web["credential_env"])
        # Neither the result frames nor the diagnostics may carry the key.
        self.assertNotIn(secret, json.dumps(info))
        self.assertNotIn(secret, "\n".join(process.stderr_lines))

    def test_managed_source_is_derived_from_the_config_block(self) -> None:
        document = config_document(
            self.db_path,
            profile="production",
            transport_kind="http",
            desktop={"managed": True},
        )
        process = self.start(document=document)
        web = self._web(process)
        self.assertEqual("managed", web["source"])
        self.assertFalse(web["declared"])
        self.assertEqual(["web"], web["missing"])

    def test_reload_applies_an_enable_to_the_next_turn(self) -> None:
        document = config_document(
            self.db_path,
            script=[
                v2_call("web.search", {"query": "synthetic query"}),
                v2_final("合成回答：没有联网凭据。"),
            ],
        )
        process, info = self._start_v2(document=document)
        self.assertFalse(self._web(process)["enabled"])

        # The settings writer flips web.enabled; reload must rebuild the tool
        # runtime, not merely echo the new boolean in app.info.
        document["web"] = {"enabled": True, "timeout_seconds": 10}
        process.config_path.write_text(
            json.dumps(document), encoding="utf-8"
        )
        applied = process.request("settings.reload")
        self.assertTrue(applied["ok"], applied)
        after = process.request("app.info")["result"]
        self.assertEqual(after["session_id"], info["session_id"])
        web = after["capabilities"]["web"]
        self.assertTrue(web["enabled"])
        self.assertFalse(web["configured"])
        self.assertEqual(["web.api_key_env"], web["missing"])

        turn = self._run_web_turn(process, info, "rg08-web-reload")
        self.assertEqual("completed", turn["result"]["state"])
        self.assertEqual(1, len(turn["steps"]))
        self.assertEqual("web.search", turn["steps"][0]["tool_name"])
        self.assertEqual("failed", turn["steps"][0]["status"])
        self.assertEqual("web_not_configured", turn["steps"][0]["error_label"])


if __name__ == "__main__":
    import unittest

    unittest.main()
