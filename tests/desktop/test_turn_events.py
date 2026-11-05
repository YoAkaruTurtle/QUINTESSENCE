"""RG03 desktop bridge: turn events, steps visibility, cancel and restart.

These tests run the real sidecar over the real framing pipe (``BridgeProcess``)
with the scripted v2 transport.  No GUI/WebView2 is involved: the DOM-free UI
tests live in ``apps/desktop/tests/frontend``.
"""

from __future__ import annotations

import json
import time
import unittest

from tests.desktop.support import DesktopCase, config_document


def v2_call(name: str, arguments: dict | None = None, commentary=None) -> dict:
    payload = {
        "action": "call_tool",
        "tool_call": {"name": name, "arguments": arguments or {}},
    }
    if commentary is not None:
        payload["commentary"] = commentary
    return payload


def v2_final(reply: str) -> dict:
    return {"action": "final", "reply": reply}


class TurnBridgeCase(DesktopCase):
    # ------------------------------------------------------------- helpers

    def _start(
        self,
        *,
        script,
        delay_ms: int = 0,
        config: dict | None = None,
        extra_env: dict | None = None,
    ):
        document = config or config_document(
            self.db_path, script=script, delay_ms=delay_ms
        )
        document["destination"]["model_protocol"] = "collab.v2"
        process = self.start(document=document, extra_env=extra_env)
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

    def _conversation(self, process, info, *, mode: str = "chat") -> str:
        frame = process.request(
            "conversation.create",
            {"session_id": info["session_id"], "mode": mode},
        )
        self.assertTrue(frame["ok"], frame)
        return frame["result"]["conversation"]["conversation_id"]

    def _send(self, process, info, conversation_id, text, request_id):
        frame = process.request(
            "conversation.send",
            {
                "session_id": info["session_id"],
                "conversation_id": conversation_id,
                "client_request_id": request_id,
                "text": text,
                "confirmed_destination_id": (
                    info["destination"]["destination_id"]
                    if info["destination"]["confirmation_required"]
                    else None
                ),
            },
        )
        self.assertTrue(frame["ok"], frame)
        return frame["result"]["turn_id"]

    def _events(self, process, info, turn_id, *, after_seq=None, limit=None):
        params = {"session_id": info["session_id"], "turn_id": turn_id}
        if after_seq is not None:
            params["after_seq"] = after_seq
        if limit is not None:
            params["limit"] = limit
        return process.request("turn.events", params)

    def _steps(self, process, info, turn_id, **extra):
        params = {"session_id": info["session_id"], "turn_id": turn_id, **extra}
        return process.request("turn.steps", params)

    def _cancel(self, process, info, turn_id):
        return process.request(
            "turn.cancel",
            {"session_id": info["session_id"], "turn_id": turn_id},
        )

    def _wait_result(self, process, info, turn_id, *, timeout: float = 40.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ping = process.request("bridge.ping")
            self.assertTrue(ping["ok"], ping)
            if not ping["result"]["busy"]:
                frame = process.request(
                    "turn.result",
                    {"session_id": info["session_id"], "turn_id": turn_id},
                )
                if frame["ok"]:
                    return frame["result"]
            time.sleep(0.05)
        raise TimeoutError("turn did not settle")

    def _wait_events(self, process, info, turn_id, predicate, *, timeout=30.0):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            frame = self._events(process, info, turn_id)
            if frame["ok"]:
                last = frame["result"]
                if predicate(last):
                    return last
            time.sleep(0.05)
        raise TimeoutError(f"events did not satisfy the predicate: {last}")

class TurnEventsDesktopTests(TurnBridgeCase):
    # ---------------------------------------------------------------- U01

    def test_commentary_is_visible_while_the_turn_is_still_running(self) -> None:
        script = [
            v2_call(
                "cognition.partition",
                {"limit": 5},
                commentary="我翻一下之前的记录。",
            ),
            v2_call("cognition.partition", {"limit": 5}),
            v2_final("合成回答：查完了。"),
        ]
        process, info = self._start(script=script, delay_ms=700)
        conversation = self._conversation(process, info)
        turn_id = self._send(process, info, conversation, "查一下。", "u01-req")

        seen = self._wait_events(
            process,
            info,
            turn_id,
            lambda page: any(
                event["type"] == "message_published"
                and event.get("kind") == "commentary"
                and event.get("text")
                for event in page["events"]
            ),
        )
        # The turn is still running: the ring served an in-progress event.
        ping = process.request("bridge.ping")
        self.assertTrue(ping["result"]["busy"])
        commentary = [
            event
            for event in seen["events"]
            if event["type"] == "message_published"
            and event.get("kind") == "commentary"
        ][0]
        self.assertEqual("我翻一下之前的记录。", commentary["text"])
        self.assertEqual("model", commentary.get("commentary_source"))
        self.assertFalse(seen["terminal"])

        result = self._wait_result(process, info, turn_id)
        self.assertEqual("completed", result["state"])
        events = self._events(process, info, turn_id)["result"]
        self.assertTrue(events["terminal"])
        kinds = [event["type"] for event in events["events"]]
        self.assertLess(
            kinds.index("message_published"), kinds.index("turn_completed")
        )
        commentary_seq = [
            event["seq"]
            for event in events["events"]
            if event["type"] == "message_published"
            and event.get("kind") == "commentary"
        ]
        finished_seq = [
            event["seq"]
            for event in events["events"]
            if event["type"] == "tool_finished"
        ]
        self.assertEqual(2, len(commentary_seq))
        self.assertEqual(2, len(finished_seq))
        self.assertEqual(
            ["model", "host_fallback"],
            [
                event.get("commentary_source")
                for event in events["events"]
                if event["type"] == "message_published"
                and event.get("kind") == "commentary"
            ],
        )
        self.assertLess(commentary_seq[0], finished_seq[0])
        self.assertLess(finished_seq[0], commentary_seq[1])
        self.assertLess(commentary_seq[1], finished_seq[1])
        # The durable records are complete.
        page = process.request(
            "conversation.get",
            {"session_id": info["session_id"], "conversation_id": conversation},
        )["result"]
        self.assertEqual(
            ["user", "commentary", "commentary", "final"],
            [item["kind"] for item in page["messages"]],
        )
        self.assertEqual(1, len(page["turns"]))
        steps = self._steps(process, info, turn_id)["result"]["steps"]
        self.assertEqual(2, len(steps))
        self.assertTrue(all(step["result"]["available"] for step in steps))

    # ------------------------------------------------- final commentary

    def test_final_commentary_is_published_before_the_answer(self) -> None:
        script = [
            {
                "action": "final",
                "commentary": "我核对完了。",
                "reply": "合成回答：带短句的最终回答。",
            }
        ]
        process, info = self._start(script=script)
        conversation = self._conversation(process, info)
        turn_id = self._send(process, info, conversation, "答一下。", "fc-req")
        result = self._wait_result(process, info, turn_id)
        self.assertEqual("completed", result["state"], result.get("failure_reason"))
        self.assertEqual("合成回答：带短句的最终回答。", result["final"]["text"])
        page = process.request(
            "conversation.get",
            {"session_id": info["session_id"], "conversation_id": conversation},
        )["result"]
        self.assertEqual(
            ["user", "commentary", "final"],
            [item["kind"] for item in page["messages"]],
        )
        self.assertEqual("我核对完了。", page["messages"][1]["text"])
        events = self._events(process, info, turn_id)["result"]["events"]
        commentary = [
            event
            for event in events
            if event["type"] == "message_published"
            and event.get("kind") == "commentary"
        ]
        self.assertEqual(1, len(commentary))
        self.assertLess(
            commentary[0]["seq"],
            [
                event["seq"]
                for event in events
                if event["type"] == "message_published"
                and event.get("kind") == "final"
            ][0],
        )
        final_artifact = self.scalar(
            "SELECT artifact_id FROM collab_turn_messages "
            "WHERE namespace = ? AND turn_id = ? AND kind = 'final'",
            (info["host"]["namespace"], turn_id),
        )
        commentary_artifact = self.scalar(
            "SELECT artifact_id FROM collab_turn_messages "
            "WHERE namespace = ? AND turn_id = ? AND kind = 'commentary'",
            (info["host"]["namespace"], turn_id),
        )
        linked = {
            row["source_id"]
            for row in self.query(
                "SELECT source_id FROM collab_source_links "
                "WHERE owner_kind = 'artifact' AND owner_id = ?",
                (final_artifact,),
            )
        }
        self.assertIn(commentary_artifact, linked)

    # ---------------------------------------------------------------- U04

    def test_reconnect_after_seq_does_not_duplicate_or_rerun(self) -> None:
        script = [
            v2_call("cognition.partition", {"limit": 5}),
            v2_final("合成回答。"),
        ]
        process, info = self._start(script=script)
        conversation = self._conversation(process, info)
        turn_id = self._send(process, info, conversation, "查一下。", "u04-req")
        result = self._wait_result(process, info, turn_id)
        self.assertEqual("completed", result["state"])
        first = self._events(process, info, turn_id)["result"]
        self.assertTrue(first["terminal"])
        self.assertGreater(len(first["events"]), 3)
        attempts = int(self.scalar("SELECT COUNT(*) FROM model_attempts"))
        steps = int(self.scalar("SELECT COUNT(*) FROM collab_turn_steps"))

        tail = self._events(
            process, info, turn_id, after_seq=first["next_seq"]
        )["result"]
        self.assertEqual([], tail["events"])
        replay = self._events(process, info, turn_id)["result"]
        self.assertEqual(
            [event["event_id"] for event in first["events"]],
            [event["event_id"] for event in replay["events"]],
        )
        self.assertEqual(
            len({event["seq"] for event in replay["events"]}),
            len(replay["events"]),
        )
        self.assertEqual(attempts, int(self.scalar("SELECT COUNT(*) FROM model_attempts")))
        self.assertEqual(steps, int(self.scalar("SELECT COUNT(*) FROM collab_turn_steps")))

    # ---------------------------------------------------------------- U06

    def test_slow_first_request_shows_only_real_state(self) -> None:
        script = [
            v2_call(
                "cognition.partition",
                {"limit": 5},
                commentary="我翻一下。",
            ),
            v2_final("合成回答。"),
        ]
        process, info = self._start(script=script, delay_ms=900)
        conversation = self._conversation(process, info)
        started = time.monotonic()
        turn_id = self._send(process, info, conversation, "查一下。", "u06-req")
        page = self._wait_events(process, info, turn_id, lambda value: True)
        self.assertLess(time.monotonic() - started, 5.0)
        kinds = {event["type"] for event in page["events"]}
        self.assertTrue(kinds.issubset({"turn_accepted", "stage_started"}))
        self.assertFalse(
            any(
                event["type"] == "message_published" for event in page["events"]
            )
        )
        result = self._wait_result(process, info, turn_id)
        self.assertEqual("completed", result["state"])

    # ---------------------------------------------------------------- R4

    def test_public_surface_is_whitelisted_and_busy_safe(self) -> None:
        script = [
            v2_call(
                "cognition.partition",
                {"limit": 5},
                commentary="我先看一眼。",
            ),
            v2_final("合成回答。"),
        ]
        process, info = self._start(script=script, delay_ms=900)
        conversation = self._conversation(process, info)
        turn_id = self._send(process, info, conversation, "查一下。", "r4-req")
        # Busy: steps/result answer BUSY immediately; events/cancel do not queue.
        busy = process.request(
            "turn.steps", {"session_id": info["session_id"], "turn_id": turn_id}
        )
        self.assertFalse(busy["ok"])
        self.assertEqual("BUSY", busy["error"]["code"])
        busy_result = process.request(
            "turn.result", {"session_id": info["session_id"], "turn_id": turn_id}
        )
        self.assertEqual("BUSY", busy_result["error"]["code"])
        live = self._events(process, info, turn_id)
        self.assertTrue(live["ok"], live)
        cancel = self._cancel(process, info, turn_id)
        self.assertTrue(cancel["ok"], cancel)
        result = self._wait_result(process, info, turn_id)
        self.assertEqual("cancelled", result["state"])

        process.close()
        # Completed arm for the public DTO shape.
        script2 = [
            v2_call("cognition.partition", {"limit": 5}, commentary="我先看一眼。"),
            v2_final("合成回答。"),
        ]
        process2, info2 = self._start(script=script2)
        conversation2 = self._conversation(process2, info2)
        turn2 = self._send(process2, info2, conversation2, "再查一下。", "r4-req-2")
        self._wait_result(process2, info2, turn2)
        page = self._events(process2, info2, turn2)["result"]
        allowed = {
            "seq",
            "event_id",
            "type",
            "at",
            "step_id",
            "step_no",
            "tool",
            "arguments_digest",
            "status",
            "duration_ms",
            "result_artifact_id",
            "result_artifact_version",
            "sizes",
            "stage",
            "message_id",
            "kind",
            "message_seq",
            "reason",
            "commentary_omitted",
            "commentary_source",
            "state",
            "attempt_ref",
            "text",
            "arguments",
            "result_excerpt",
        }
        for event in page["events"]:
            self.assertTrue(set(event).issubset(allowed), set(event))
            encoded = json.dumps(event, ensure_ascii=False)
            for forbidden in (
                "rendered_input",
                "payload_json",
                "brief_reason",
                "reasoning_content",
                "sk-",
                "api_key",
            ):
                self.assertNotIn(forbidden, encoded)
        steps = self._steps(process2, info2, turn2)["result"]["steps"]
        self.assertEqual(1, len(steps))
        self.assertEqual({"limit": 5}, steps[0]["arguments"])
        self.assertTrue(steps[0]["result"]["available"])
        self.assertNotIn("rendered_input", json.dumps(steps, ensure_ascii=False))

        # Tauri sends `null` for an omitted Option; the bridge treats it as the
        # documented default instead of refusing it.
        defaults = process2.request(
            "turn.steps",
            {
                "session_id": info2["session_id"],
                "turn_id": turn2,
                "after_step_no": None,
                "limit": None,
            },
        )
        self.assertTrue(defaults["ok"], defaults)
        self.assertEqual(1, len(defaults["result"]["steps"]))
        default_events = process2.request(
            "turn.events",
            {
                "session_id": info2["session_id"],
                "turn_id": turn2,
                "after_seq": None,
                "limit": None,
            },
        )
        self.assertTrue(default_events["ok"], default_events)
        self.assertTrue(default_events["result"]["terminal"])

        # Invalid pagination is refused, and the method list is advertised.
        bad = self._events(process2, info2, turn2, after_seq=-1)
        self.assertFalse(bad["ok"])
        self.assertEqual("INVALID_PARAMS", bad["error"]["code"])
        self.assertIn("turn.events", info2["methods"])
        self.assertIn("turn.steps", info2["methods"])
        self.assertIn("turn.cancel", info2["methods"])
        self.assertNotIn("turn.events", info2["reserved"])
        self.assertTrue(info2["capabilities"]["process"])
        self.assertTrue(info2["capabilities"]["cancel"])

    # ---------------------------------------------------------------- M05

    def test_restart_replays_only_committed_events(self) -> None:
        script = [
            v2_call("cognition.partition", {"limit": 5}, commentary="我翻一下。"),
            v2_final("不会到达的回答。"),
        ]
        process, info = self._start(script=script, delay_ms=1_500)
        conversation = self._conversation(process, info)
        turn_id = self._send(process, info, conversation, "查一下。", "m05-req")
        self._wait_events(
            process,
            info,
            turn_id,
            lambda page: any(
                event["type"] == "message_published" for event in page["events"]
            ),
        )
        process.kill()
        restarted, info2 = self._start(script=[v2_final("重启后的回答。")])
        listed = restarted.request(
            "conversation.list", {"session_id": info2["session_id"]}
        )
        self.assertTrue(listed["ok"], listed)
        self.assertIn(
            conversation,
            [
                item["conversation_id"]
                for item in listed["result"]["conversations"]
            ],
        )
        result = restarted.request(
            "turn.result", {"session_id": info2["session_id"], "turn_id": turn_id}
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual("interrupted", result["result"]["state"])
        self.assertIsNone(result["result"]["final"])
        events = restarted.request(
            "turn.events", {"session_id": info2["session_id"], "turn_id": turn_id}
        )
        self.assertTrue(events["ok"], events)
        self.assertTrue(events["result"]["terminal"])
        kinds = [event["type"] for event in events["result"]["events"]]
        self.assertIn("turn_interrupted", kinds)
        self.assertNotIn("turn_completed", kinds)

    # ---------------------------------------------------------------- RG08

    def test_web_tool_is_wired_and_reports_a_missing_credential(self) -> None:
        """The sidecar publishes web.search only when `web.enabled` is set.

        With no credential in the environment the real handler answers
        `web_not_configured`; the turn must not invent a search result.
        """

        document = config_document(
            self.db_path,
            script=[
                v2_call("web.search", {"query": "synthetic query"}),
                v2_final("合成回答：这台主机没有联网凭据，没有搜索结果。"),
            ],
        )
        document["web"] = {"enabled": True, "timeout_seconds": 10}
        process, info = self._start(
            script=document["destination"]["transport"]["script"],
            config=document,
            extra_env={"DEEPSEEK_API_KEY": ""},
        )
        conversation = self._conversation(process, info)
        turn_id = self._send(process, info, conversation, "搜一下。", "rg08-req")
        result = self._wait_result(process, info, turn_id)
        self.assertEqual("completed", result["state"], result.get("failure_reason"))
        steps = self._steps(process, info, turn_id)["result"]["steps"]
        self.assertEqual(1, len(steps))
        self.assertEqual("web.search", steps[0]["tool_name"])
        self.assertEqual("failed", steps[0]["status"])
        self.assertEqual("web_not_configured", steps[0]["error_label"])
        events = self._events(process, info, turn_id)["result"]["events"]
        finished = [
            event for event in events if event["type"] == "tool_finished"
        ]
        self.assertEqual(1, len(finished))
        self.assertEqual("web.search", finished[0]["tool"])
        self.assertIn(
            "web_not_configured", finished[0].get("result_excerpt", "")
        )

    def test_rejected_tool_is_public_without_arguments_or_execution(self) -> None:
        """A refused call is a rejection event, not a started/finished step."""

        process, info = self._start(
            script=[
                v2_call("not.a.tool", {"query": "SECRET-ARG"}),
                v2_final("合成回答：没有这个工具。"),
            ]
        )
        conversation = self._conversation(process, info)
        turn_id = self._send(process, info, conversation, "试一下。", "rg08-reject")
        result = self._wait_result(process, info, turn_id)
        self.assertEqual("completed", result["state"], result.get("failure_reason"))

        events = self._events(process, info, turn_id)["result"]["events"]
        self.assertEqual(
            [],
            [event for event in events if event["type"] == "tool_started"],
        )
        self.assertEqual(
            [],
            [event for event in events if event["type"] == "tool_finished"],
        )
        rejected = [
            event for event in events if event["type"] == "tool_rejected"
        ]
        self.assertEqual(1, len(rejected))
        self.assertEqual("unknown_tool", rejected[0]["reason"])
        self.assertEqual("rejected", rejected[0]["status"])
        self.assertEqual("not.a.tool", rejected[0]["tool"])
        self.assertNotIn("arguments", rejected[0])
        self.assertNotIn("SECRET-ARG", json.dumps(rejected[0], ensure_ascii=False))

        steps = self._steps(process, info, turn_id)["result"]["steps"]
        self.assertEqual(1, len(steps))
        self.assertEqual("failed", steps[0]["status"])
        self.assertEqual("refused:tool_unknown", steps[0]["error_label"])
        self.assertIsNone(steps[0]["arguments"])
        self.assertNotIn("SECRET-ARG", json.dumps(steps, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
