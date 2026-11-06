"""RG02: conversation bridge methods, idempotency, BUSY and restart recovery.

Every test starts the real sidecar over real pipes with the real store and the
scripted in-process transport.  No network or private content is involved.
"""

from __future__ import annotations

import json
import time
import unittest

from tests.desktop.support import DesktopCase, config_document, write_config
from quintessence_pcs.runtime.manifest import ModelManifest
from quintessence_pcs.store import CognitionStore


def reply(text: str) -> dict:
    return {"action": "none", "reply": text}


class ConversationBridgeTests(DesktopCase):
    def _start(self, *, script, delay_ms: int = 0, external: bool = True):
        document = config_document(
            self.db_path,
            script=script,
            delay_ms=delay_ms,
            external=external,
        )
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

    def _conversation(self, process, info, *, mode: str = "chat") -> str:
        reply_frame = process.request(
            "conversation.create",
            {"session_id": info["session_id"], "mode": mode},
        )
        self.assertTrue(reply_frame["ok"], reply_frame)
        return reply_frame["result"]["conversation"]["conversation_id"]

    def _send(self, process, info, conversation_id, text, request_id):
        return process.request(
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

    def _wait_turn(self, process, info, turn_id, *, timeout: float = 30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ping = process.request("bridge.ping")
            self.assertTrue(ping["ok"], ping)
            if not ping["result"]["busy"]:
                result = process.request(
                    "turn.result",
                    {"session_id": info["session_id"], "turn_id": turn_id},
                )
                if result["ok"]:
                    return result["result"]
            time.sleep(0.05)
        raise TimeoutError("turn did not settle")

    def _get(self, process, info, conversation_id, **extra):
        params = {
            "session_id": info["session_id"],
            "conversation_id": conversation_id,
            **extra,
        }
        frame = process.request("conversation.get", params)
        self.assertTrue(frame["ok"], frame)
        return frame["result"]

    def test_send_read_and_restart(self) -> None:
        process, info = self._start(script=[reply("第一答"), reply("第二答")])
        conversation_id = self._conversation(process, info)
        first = self._send(process, info, conversation_id, "第一个问题", "req-1")
        self.assertTrue(first["ok"], first)
        self.assertFalse(first["result"]["reused"])
        self.assertEqual("accepted", first["result"]["state"])
        result = self._wait_turn(process, info, first["result"]["turn_id"])
        self.assertEqual("completed", result["state"])
        self.assertEqual("第一答", result["final"]["text"])
        second = self._send(process, info, conversation_id, "第二个问题", "req-2")
        self.assertTrue(second["ok"], second)
        result = self._wait_turn(process, info, second["result"]["turn_id"])
        self.assertEqual("completed", result["state"])
        page = self._get(process, info, conversation_id)
        self.assertEqual(
            ["user", "final", "user", "final"],
            [item["kind"] for item in page["messages"]],
        )
        self.assertEqual(2, len(page["turns"]))

        # Restart: the same conversation and transcript are readable.
        process.close()
        process, info = self._start(
            script=[reply("不应使用的脚本")], external=True
        )
        listed = process.request(
            "conversation.list", {"session_id": info["session_id"]}
        )
        self.assertTrue(listed["ok"], listed)
        ids = [
            item["conversation_id"]
            for item in listed["result"]["conversations"]
        ]
        self.assertIn(conversation_id, ids)
        page = self._get(process, info, conversation_id)
        self.assertEqual(
            ["user", "final", "user", "final"],
            [item["kind"] for item in page["messages"]],
        )
        replay = self._send(process, info, conversation_id, "第二个问题", "req-2")
        self.assertTrue(replay["ok"], replay)
        self.assertTrue(replay["result"]["reused"])
        self.assertEqual(second["result"]["turn_id"], replay["result"]["turn_id"])
        self.assertEqual(
            2,
            int(self.scalar("SELECT COUNT(*) FROM collab_turns")),
        )

    def test_instance_grant_exhaustion_and_explicit_renewal(self) -> None:
        document = config_document(
            self.db_path, script=[{"action": "final", "reply": "合成答复"}],
        )
        document["destination"]["model_protocol"] = "collab.v2"
        document["destination"]["manifest"]["max_calls"] = 1
        document["conversation"] = {"sdrpe": {"mode": "on"}}
        path = write_config(self.tmp_path, document, name="instance-host.json")
        process = self.start(config_path=path)
        info = process.request("app.info")["result"]
        confirmation = {
            "session_id": info["session_id"],
            "destination_id": info["destination"]["destination_id"],
        }
        self.assertTrue(process.request("app.confirm_destination", confirmation)["ok"])
        conversation = self._conversation(process, info)
        sent = self._send(process, info, conversation, "合成问题", "budget-1")
        self.assertTrue(sent["ok"], sent)
        turn_id = sent["result"]["turn_id"]
        result = self._wait_turn(process, info, turn_id)
        self.assertEqual("completed", result["state"])
        self.assertEqual("合成答复", result["final"]["text"])
        events = process.request(
            "turn.events", {"session_id": info["session_id"], "turn_id": turn_id},
        )["result"]["events"]
        self.assertTrue(any(
            event["type"] == "stage_failed"
            and event["stage"] == "expression"
            and event["reason"] == "model:MODEL_CALLS_EXHAUSTED"
            for event in events
        ))
        exhausted = process.request("app.info")["result"]
        self.assertFalse(exhausted["host"]["ready"])
        self.assertEqual("MODEL_CALLS_EXHAUSTED", exhausted["host"]["reason_code"])
        self.assertEqual(1, exhausted["capabilities"]["model_renewal"]["max_calls"])
        denied = self._send(process, info, conversation, "第二个合成问题", "budget-2")
        self.assertEqual("MODEL_CALLS_EXHAUSTED", denied["error"]["code"])
        self.assertEqual(1, len(self._get(process, info, conversation)["turns"]))

        process.close()
        process = self.start(config_path=path)
        info = process.request("app.info")["result"]
        self.assertEqual("MODEL_CALLS_EXHAUSTED", info["host"]["reason_code"])
        self.assertEqual(1, len(self.query("select 1 from model_authorizations")))
        confirmation = {
            "session_id": info["session_id"],
            "destination_id": info["destination"]["destination_id"],
        }
        params = {**confirmation, "authorization_id": info["capabilities"]["model_renewal"]["authorization_id"]}
        self.assertFalse(process.request("model.renew_authorization", {**params, "max_calls": 99})["ok"])
        self.assertFalse(process.request("model.renew_authorization", {**params, "destination_id": "other"})["ok"])
        self.assertTrue(process.request("model.renew_authorization", params)["ok"])
        self.assertFalse(process.request("model.renew_authorization", params)["ok"])
        self.assertTrue(process.request("app.info")["result"]["host"]["ready"])
        self.assertTrue(process.request("app.confirm_destination", confirmation)["ok"])
        sent = self._send(process, info, conversation, "第二个合成问题", "budget-2")
        self.assertTrue(sent["ok"], sent)
        self.assertEqual("completed", self._wait_turn(process, info, sent["result"]["turn_id"])["state"])
        self.assertEqual(
            [(1, "exhausted"), (1, "exhausted")],
            [tuple(row) for row in self.query("select max_calls, status from model_authorizations order by granted_at")],
        )
        self.assertEqual(2, len(self.query("select 1 from model_attempts")))

    def test_legacy_64_call_renewal_persists_999_and_reuses_interrupted_grant(self) -> None:
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                directory = self.tmp_path / f"upgrade-{interrupted}"
                directory.mkdir()
                db = directory / "pcs.sqlite"
                document = config_document(db, script=[reply("合成答复")])
                path = write_config(directory, document, name="instance-host.json")
                process = self.start(config_path=path, db_path=db)
                process.close()
                with CognitionStore.open(db) as store:
                    old = store.model_authorizations()[0]
                    self.assertEqual(64, old.max_calls)
                    store.mark_model_authorization_exhausted(old.authorization_id)
                    if interrupted:
                        target = ModelManifest.from_mapping({
                            **document["destination"]["manifest"], "max_calls": 999,
                        })
                        store.grant_model_authorization(
                            namespace=old.namespace,
                            authorization_ref=f"desktop-user-confirmed-renewal:{old.authorization_id}",
                            provider=old.provider,
                            model=old.model,
                            manifest_hash=target.manifest_hash,
                            max_calls=999,
                            max_output_tokens_per_call=old.max_output_tokens_per_call,
                            max_wall_seconds=old.max_wall_seconds,
                        )

                process = self.start(config_path=path)
                before = process.request("app.info")["result"]
                offer = before["capabilities"]["model_renewal"]
                self.assertEqual(999, offer["max_calls"])
                params = {
                    "session_id": before["session_id"],
                    "destination_id": before["destination"]["destination_id"],
                    "authorization_id": offer["authorization_id"],
                }
                renewed = process.request("model.renew_authorization", params)
                self.assertTrue(renewed["ok"], renewed)
                after = process.request("app.info")["result"]
                self.assertTrue(after["host"]["ready"])
                self.assertNotEqual(params["destination_id"], after["destination"]["destination_id"])
                document["destination"]["manifest"]["max_calls"] = 999
                self.assertEqual(document, json.loads(path.read_text(encoding="utf-8")))
                self.assertEqual(
                    [(64, "exhausted"), (999, "active")],
                    [tuple(row) for row in self.query(
                        "select max_calls, status from model_authorizations order by granted_at",
                    )],
                )
                process.close()
                process = self.start(config_path=path)
                info = process.request("app.info")["result"]
                self.assertTrue(info["host"]["ready"])
                self.assertEqual(after["destination"], info["destination"])
                self.assertTrue(process.request("app.confirm_destination", {
                    "session_id": info["session_id"],
                    "destination_id": info["destination"]["destination_id"],
                })["ok"])
                sent = self._send(process, info, self._conversation(process, info), "合成问题", "upgrade")
                self.assertTrue(sent["ok"], sent)
                self.assertEqual("completed", self._wait_turn(process, info, sent["result"]["turn_id"])["state"])
                process.close()

    def test_duplicate_request_id_is_reused_and_conflict_is_refused(self) -> None:
        process, info = self._start(script=[reply("唯一答")])
        conversation_id = self._conversation(process, info)
        first = self._send(process, info, conversation_id, "同样的问题", "same-id")
        self.assertTrue(first["ok"], first)
        turn_id = first["result"]["turn_id"]
        self._wait_turn(process, info, turn_id)
        attempts = int(self.scalar("SELECT COUNT(*) FROM model_attempts"))
        again = self._send(process, info, conversation_id, "同样的问题", "same-id")
        self.assertTrue(again["ok"], again)
        self.assertTrue(again["result"]["reused"])
        self.assertEqual(turn_id, again["result"]["turn_id"])
        self.assertEqual(attempts, int(self.scalar("SELECT COUNT(*) FROM model_attempts")))
        conflict = self._send(
            process, info, conversation_id, "不同的问题", "same-id"
        )
        self.assertFalse(conflict["ok"])
        self.assertEqual("CONFLICT", conflict["error"]["code"])
        self.assertEqual(1, int(self.scalar("SELECT COUNT(*) FROM collab_turns")))
        self.assertEqual(1, int(self.scalar("SELECT COUNT(*) FROM collab_inputs")))

    def test_busy_send_and_busy_reads_are_immediate(self) -> None:
        process, info = self._start(script=[reply("慢慢答")], delay_ms=3000)
        conversation_id = self._conversation(process, info)
        first = self._send(process, info, conversation_id, "第一个问题", "req-1")
        self.assertTrue(first["ok"], first)
        pid = process.request("bridge.ping")["result"]["pid"]
        started = time.monotonic()
        second = self._send(process, info, conversation_id, "第二个问题", "req-2")
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertFalse(second["ok"])
        self.assertEqual("BUSY", second["error"]["code"])
        # R2: read methods are refused immediately, never queued behind the turn.
        for method, params in (
            ("turn.result", {"session_id": info["session_id"], "turn_id": first["result"]["turn_id"]}),
            ("conversation.get", {"session_id": info["session_id"], "conversation_id": conversation_id}),
            ("conversation.list", {"session_id": info["session_id"]}),
            ("conversation.create", {"session_id": info["session_id"], "mode": "chat"}),
        ):
            started = time.monotonic()
            frame = process.request(method, params)
            elapsed = time.monotonic() - started
            self.assertFalse(frame["ok"], method)
            self.assertEqual("BUSY", frame["error"]["code"], method)
            self.assertLess(elapsed, 1.0, method)
        self.assertIsNone(process.proc.poll())
        self.assertEqual(pid, process.request("bridge.ping")["result"]["pid"])
        self.assertEqual(1, int(self.scalar("SELECT COUNT(*) FROM collab_turns")))
        result = self._wait_turn(process, info, first["result"]["turn_id"])
        self.assertEqual("completed", result["state"])
        self.assertEqual("慢慢答", result["final"]["text"])
        self.assertIsNone(process.proc.poll())

    def test_query_action_is_an_honest_failure(self) -> None:
        process, info = self._start(script=[{"action": "query", "brief_reason": "x"}])
        conversation_id = self._conversation(process, info)
        sent = self._send(process, info, conversation_id, "需要事实", "req-q")
        self.assertTrue(sent["ok"], sent)
        result = self._wait_turn(process, info, sent["result"]["turn_id"])
        self.assertEqual("failed", result["state"])
        self.assertEqual("query_unavailable", result["failure_reason"])
        self.assertIsNone(result["final"])

    def test_restart_marks_a_running_turn_interrupted(self) -> None:
        process, info = self._start(script=[reply("来不及")], delay_ms=5000)
        conversation_id = self._conversation(process, info)
        sent = self._send(process, info, conversation_id, "会中断的问题", "req-i")
        self.assertTrue(sent["ok"], sent)
        time.sleep(0.3)
        process.kill()
        process.wait_exit(timeout=10)
        process, info = self._start(script=[reply("新进程的回答")])
        result = self._wait_turn(process, info, sent["result"]["turn_id"])
        self.assertEqual("interrupted", result["state"])
        self.assertIsNone(result["final"])
        self.assertEqual(
            1,
            int(
                self.scalar(
                    "SELECT COUNT(*) FROM collab_turn_events WHERE kind = 'turn_interrupted'"
                )
            ),
        )
        page = self._get(process, info, conversation_id)
        self.assertEqual("interrupted", page["turns"][0]["state"])
        self.assertEqual(["user"], [item["kind"] for item in page["messages"]])

    def test_paged_get_returns_turns_for_old_messages(self) -> None:
        """Card C2: the page's turns are the turns of its messages.

        The reader groups messages by turn_no.  Returning the latest 50 turns
        instead leaves empty groups above the latest message page and gives an
        older page messages whose turns are outside the returned window.  This
        path is reached with 55 turns and a small page limit: the initial page
        is 50 messages / 25 turns, and the last page reaches turns 1-5.
        """

        total_turns = 55
        process, info = self._start(
            script=[reply(f"答{index}") for index in range(1, total_turns + 1)]
        )
        conversation_id = self._conversation(process, info)
        for index in range(1, total_turns + 1):
            sent = self._send(
                process,
                info,
                conversation_id,
                f"问{index}",
                f"req-c2-{index}",
            )
            self.assertTrue(sent["ok"], sent)
            result = self._wait_turn(process, info, sent["result"]["turn_id"])
            self.assertEqual("completed", result["state"])

        page = self._get(process, info, conversation_id, limit=10)
        self.assertEqual(10, len(page["messages"]))
        self.assertEqual(5, len(page["turns"]))
        initial_turn_ids = {turn["turn_id"] for turn in page["turns"]}
        all_messages: list[dict] = []
        turn_numbers: dict[str, int] = {}
        old_turn_outside_initial_window = False
        pages = 0
        while True:
            page_turns = {turn["turn_id"]: turn["turn_no"] for turn in page["turns"]}
            turn_numbers.update(page_turns)
            for message in page["messages"]:
                self.assertIn(
                    message["turn_id"],
                    page_turns,
                    "a page message must carry its turn",
                )
                if message["turn_id"] not in initial_turn_ids:
                    old_turn_outside_initial_window = True
            all_messages.extend(page["messages"])
            pages += 1
            self.assertLessEqual(pages, 20)
            next_before = page["next_before"]
            if next_before is None:
                break
            page = self._get(
                process,
                info,
                conversation_id,
                limit=10,
                before_turn_no=next_before["turn_no"],
                before_seq=next_before["seq"],
            )

        self.assertTrue(old_turn_outside_initial_window)
        self.assertEqual(total_turns * 2, len(all_messages))
        self.assertEqual(
            total_turns * 2,
            len({message["message_id"] for message in all_messages}),
        )
        ordered = sorted(
            all_messages,
            key=lambda message: (
                turn_numbers[message["turn_id"]],
                message["seq"],
            ),
        )
        self.assertEqual(
            [(turn_no, seq) for turn_no in range(1, total_turns + 1) for seq in (1, 2)],
            [
                (turn_numbers[message["turn_id"]], message["seq"])
                for message in ordered
            ],
        )
        self.assertEqual(
            [f"问{index}" for index in range(1, total_turns + 1)],
            [message["text"] for message in ordered if message["kind"] == "user"],
        )

    def test_conversation_authority_fields_are_rejected(self) -> None:
        process, info = self._start(script=[reply("答")])
        conversation_id = self._conversation(process, info)
        before = self.counts()
        authority_fields = (
            ("namespace", "example.test.other"),
            ("host_instance", "host:other"),
            ("interaction_id", "00000000-0000-4000-8000-000000000000"),
            ("context_id", "00000000-0000-4000-8000-000000000000"),
            ("retention", "durable"),
        )
        for field, value in authority_fields:
            frame = process.request(
                "conversation.send",
                {
                    "session_id": info["session_id"],
                    "conversation_id": conversation_id,
                    "client_request_id": f"req-{field}",
                    "text": "合成问题",
                    field: value,
                },
            )
            self.assertFalse(frame["ok"], field)
            self.assertEqual(
                "AUTHORITY_FIELD_REJECTED", frame["error"]["code"], field
            )
        mode_frame = process.request(
            "conversation.send",
            {
                "session_id": info["session_id"],
                "conversation_id": conversation_id,
                "client_request_id": "req-mode",
                "text": "合成问题",
                "mode": "work",
            },
        )
        self.assertFalse(mode_frame["ok"])
        self.assertEqual("INVALID_PARAMS", mode_frame["error"]["code"])
        self.assertEqual(before, self.counts())

    def test_turn_and_conversation_are_scoped(self) -> None:
        process, info = self._start(script=[reply("答")])
        conversation_id = self._conversation(process, info)
        missing = process.request(
            "conversation.get",
            {
                "session_id": info["session_id"],
                "conversation_id": "00000000-0000-4000-8000-000000000000",
            },
        )
        self.assertFalse(missing["ok"])
        self.assertEqual("NOT_FOUND", missing["error"]["code"])
        sent = self._send(process, info, conversation_id, "问题", "req-1")
        self._wait_turn(process, info, sent["result"]["turn_id"])
        foreign = process.request(
            "turn.result",
            {
                "session_id": info["session_id"],
                "turn_id": "00000000-0000-4000-8000-000000000000",
            },
        )
        self.assertFalse(foreign["ok"])
        self.assertEqual("NOT_FOUND", foreign["error"]["code"])


if __name__ == "__main__":
    unittest.main()
