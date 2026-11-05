"""RG08 card B2: the human review path for workless chat candidates.

A chat turn proposes a cognition candidate that owns no ``collab_work_control``
row, so ``review.*`` cannot resolve it.  ``cognition.candidate.decide`` is that
path.  These tests run the real sidecar over the real framing pipe with the
scripted v2 transport: the candidate is produced by an actual turn, and only the
human decision is scripted.

They pin four things:

* a source-provable workless candidate can be approved or rejected;
* the commit basis may include the turn's own post-Context experience events
  and this candidate's earlier review statements, but never a later turn;
* repeats are idempotent and a cross-decision is refused as decided;
* anything that is not this host's own consult change candidate is refused as
  an unsupported path, not disguised as a stale candidate.
"""

from __future__ import annotations

import json
import sqlite3
import unittest

from tests.desktop import support
from tests.desktop.support import DesktopCase, config_document

NAMESPACE = support.NAMESPACE


def v2_call(name: str, arguments: dict) -> dict:
    return {
        "action": "call_tool",
        "tool_call": {"name": name, "arguments": arguments},
    }


def v2_final(reply: str = "好的。") -> dict:
    return {"action": "final", "reply": reply}


def proposal(
    claim: str = "用户喜欢深红色",
    *,
    claim_type: str = "user_preference",
) -> dict:
    return {
        "summary": "synthetic colour preference",
        "changes": [
            {
                "op": "propose_belief",
                "body": {
                    "claim": claim,
                    "claim_type": claim_type,
                    "evidence_links": [],
                    "strength": "limited",
                    "strength_reason": "synthetic desktop decide fixture",
                    "inference_basis": "direct_report",
                    "premise_refs": [],
                    "alternative_group_id": None,
                    "valid_until": None,
                    "review_condition": "synthetic",
                },
            }
        ],
    }


class CognitionDecideCase(DesktopCase):
    def _document(self, *, bridge: bool = False, script=None):
        document = config_document(
            self.db_path,
            script=script,
            purposes=["consult", "memory.bridge"] if bridge else None,
        )
        document["destination"]["model_protocol"] = "collab.v2"
        document["budget"]["pcs_context_max_bytes"] = 65536
        document["cognition"] = {"propose": True}
        if bridge:
            document["memory_bridge"] = {
                "enabled": True,
                "python_executable": support.sys.executable,
                "entry": str(self._fake_cli()),
                "package_path": str(self.tmp_path),
                "data_directory": "memory-external",
                "agent_id": "rg08-decide",
                "installation_id": "rg08-decide-install",
                "scope_id": "scope-decide",
                "session_id": "session-decide",
                "permissions": ["memory_bridge"],
            }
        return document

    def _fake_cli(self):
        path = self.tmp_path / "fake_scope_recall.py"
        path.write_text(
            "import json, sys\n"
            "request = json.loads(sys.stdin.buffer.read())\n"
            "if request.get('action') == 'search':\n"
            "    json.dump({'kind':'ok','result':{'items':[],'examined':0,"
            "'scan_complete':True,'stop_reason':'complete','next_cursor':None,"
            "'memory_epoch':1}}, sys.stdout)\n"
            "else:\n"
            "    json.dump({'kind':'ok','receipt':{'disposition':'inserted',"
            "'durability':'persisted','event_refs':[]}}, sys.stdout)\n",
            encoding="utf-8",
        )
        return path

    def _start(self, document=None):
        document = document or self._document(script=[v2_final()])
        process = self.start(document=document)
        info = process.request("app.info")["result"]
        if info["destination"]["confirmation_required"]:
            process.request(
                "app.confirm_destination",
                {
                    "session_id": info["session_id"],
                    "destination_id": info["destination"]["destination_id"],
                },
            )
        return process, info

    def _send(self, process, info, text, *, conversation=None, script=None):
        if conversation is None:
            conversation = process.request(
                "conversation.create",
                {"session_id": info["session_id"], "mode": "chat"},
            )["result"]["conversation"]["conversation_id"]
        sent = process.request(
            "conversation.send",
            {
                "session_id": info["session_id"],
                "conversation_id": conversation,
                "client_request_id": f"decide-{text[:4]}-{len(text)}",
                "text": text,
                "confirmed_destination_id": (
                    info["destination"]["destination_id"]
                    if info["destination"]["confirmation_required"]
                    else None
                ),
            },
        )
        self.assertTrue(sent["ok"], sent)
        turn_id = sent["result"]["turn_id"]
        deadline = 30.0
        import time

        start = time.monotonic()
        while time.monotonic() - start < deadline:
            ping = process.request("bridge.ping")
            if ping["ok"] and not ping["result"]["busy"]:
                result = process.request(
                    "turn.result", {"session_id": info["session_id"], "turn_id": turn_id}
                )
                if result["ok"]:
                    return conversation, turn_id, result["result"]
            time.sleep(0.05)
        raise TimeoutError("turn did not settle")

    def _propose_candidate(self, process, info, text="我喜欢深红色"):
        document = None
        return self._send(process, info, text)

    def _candidates(self, process, info):
        reply = process.request(
            "cognition.candidates.list", {"session_id": info["session_id"]}
        )
        self.assertTrue(reply["ok"], reply)
        return reply["result"]["candidates"]

    def _user_preference_beliefs(self) -> int:
        """Belief versions that came from this candidate (not the Lab seed)."""

        rows = self.query(
            "SELECT v.body_payload_ref FROM record_versions v "
            "WHERE v.kind = 'belief'"
        )
        count = 0
        connection = sqlite3.connect(str(self.db_path))
        try:
            for row in rows:
                payload = connection.execute(
                    "SELECT body_json FROM private_payloads WHERE payload_ref = ?",
                    (row["body_payload_ref"],),
                ).fetchone()
                if not payload or not payload[0]:
                    continue
                try:
                    body = json.loads(payload[0])
                except ValueError:
                    continue
                if body.get("claim_type") == "user_preference":
                    count += 1
        finally:
            connection.close()
        return count

    def _decide(self, process, info, candidate, decision):
        return process.request(
            "cognition.candidate.decide",
            {
                "session_id": info["session_id"],
                "candidate_id": candidate["candidate_id"],
                "artifact_version": candidate["artifact_version"],
                "decision": decision,
            },
        )

    # ------------------------------------------------------------- fixtures

    def _candidate_with_script(self, *, bridge: bool = False):
        script = [v2_call("cognition.propose", proposal()), v2_final()]
        document = self._document(bridge=bridge, script=script)
        process, info = self._start(document)
        _conversation, _turn, _result = self._send(
            process, info, "我喜欢深红色"
        )
        candidates = self._candidates(process, info)
        self.assertEqual(1, len(candidates), candidates)
        return process, info, candidates[0]


class ApproveTests(CognitionDecideCase):
    def test_fresh_candidate_is_approved_and_committed(self):
        process, info, candidate = self._candidate_with_script()
        reply = self._decide(process, info, candidate, "approve")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual("COMMITTED", reply["result"]["state"])
        self.assertTrue(reply["result"]["effective"])
        self.assertFalse(reply["result"]["replayed"])
        self.assertEqual(1, self._user_preference_beliefs())
        # Approving again is a replay, not a second decision.
        again = self._decide(process, info, candidate, "approve")
        self.assertTrue(again["ok"], again)
        self.assertEqual("COMMITTED", again["result"]["state"])
        self.assertTrue(again["result"]["replayed"])
        self.assertEqual(1, self._user_preference_beliefs())

    def test_post_context_experience_events_do_not_stale_the_basis(self):
        # The turn's memory projection lands after the candidate's Context.  It
        # belongs to the candidate, so the commit must still be accepted.
        script = [v2_call("cognition.propose", proposal()), v2_final()]
        document = self._document(bridge=True, script=script)
        process, info = self._start(document)
        self._send(process, info, "我喜欢深红色")
        swept = process.request("memory.bridge.sync", {"limit": 20})
        self.assertTrue(swept["ok"], swept)
        delivered = process.request("memory.bridge.deliver", {"limit": 20})
        self.assertTrue(delivered["ok"], delivered)
        observed = self.query(
            "SELECT COUNT(*) FROM event_journal WHERE event_type = 'experience.observed'"
        )[0][0]
        self.assertGreaterEqual(observed, 1)
        candidates = self._candidates(process, info)
        self.assertEqual(1, len(candidates))
        reply = self._decide(process, info, candidates[0], "approve")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual("COMMITTED", reply["result"]["state"])

    def test_a_later_turn_does_not_extend_the_basis(self):
        # A second turn moves the event watermark for reasons that are not this
        # candidate's, so the basis really is stale and the click is refused.
        script = [
            v2_call("cognition.propose", proposal()),
            v2_final(),
            v2_final("再说一句。"),
        ]
        document = self._document(bridge=True, script=script)
        process, info = self._start(document)
        conversation, _turn, _result = self._send(process, info, "我喜欢深红色")
        swept = process.request("memory.bridge.sync", {"limit": 20})
        self.assertTrue(swept["ok"], swept)
        process.request("memory.bridge.deliver", {"limit": 20})
        candidates = self._candidates(process, info)
        self.assertEqual(1, len(candidates))
        self._send(process, info, "再聊一句无关的话", conversation=conversation)
        process.request("memory.bridge.sync", {"limit": 20})
        process.request("memory.bridge.deliver", {"limit": 20})
        reply = self._decide(process, info, candidates[0], "approve")
        self.assertFalse(reply["ok"], reply)
        self.assertEqual("CANDIDATE_STALE", reply["error"]["code"], reply)


class RejectTests(CognitionDecideCase):
    def test_reject_records_the_decision_without_a_belief(self):
        process, info, candidate = self._candidate_with_script()
        reply = self._decide(process, info, candidate, "reject")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual("REJECTED", reply["result"]["state"])
        self.assertFalse(reply["result"]["effective"])
        self.assertEqual(0, self._user_preference_beliefs())
        decisions = self.query(
            "SELECT decision FROM cognition_review_decisions ORDER BY decision_seq"
        )
        self.assertIn("rejected", [row["decision"] for row in decisions])
        again = self._decide(process, info, candidate, "reject")
        self.assertTrue(again["ok"], again)
        self.assertTrue(again["result"]["replayed"])
        # Crossing from rejected to approved is refused, not silently accepted.
        crossed = self._decide(process, info, candidate, "approve")
        self.assertFalse(crossed["ok"], crossed)
        self.assertEqual("CANDIDATE_ALREADY_DECIDED", crossed["error"]["code"])


class RestartTests(CognitionDecideCase):
    def test_retry_after_restart_approves(self):
        process, info, candidate = self._candidate_with_script()
        process.close()
        reopened, new_info = self._start(self._document(script=[v2_final()]))
        # Same database; the candidate survived the restart.
        candidates = self._candidates(reopened, new_info)
        self.assertEqual(1, len(candidates))
        self.assertEqual(candidate["candidate_id"], candidates[0]["candidate_id"])
        reply = self._decide(reopened, new_info, candidates[0], "approve")
        self.assertTrue(reply["ok"], reply)
        self.assertEqual("COMMITTED", reply["result"]["state"])


class SourceGateTests(CognitionDecideCase):
    def _patch(self, table: str, sql: str, params: tuple = ()) -> None:
        """Corrupt one binding column for a source-gate test.

        The live schema protects these columns with an immutability trigger, so
        the fixture drops that one guard first: the gate under test must be able
        to refuse a row that *is* inconsistent, not merely one the schema would
        have prevented.
        """

        connection = sqlite3.connect(str(self.db_path))
        try:
            connection.execute(f"DROP TRIGGER IF EXISTS {table}_update_guard")
            connection.execute(sql, params)
            connection.commit()
        finally:
            connection.close()

    def test_managed_work_candidate_is_an_unsupported_path(self):
        process, info, candidate = self._candidate_with_script()
        self._patch(
            "collab_artifacts",
            "UPDATE collab_artifacts SET work_id = ?, work_generation = 0 "
            "WHERE artifact_id = ?",
            ("11111111-1111-4111-8111-111111111111", candidate["candidate_id"]),
        )
        reply = self._decide(process, info, candidate, "approve")
        self.assertFalse(reply["ok"], reply)
        self.assertEqual("REVIEW_PATH_UNSUPPORTED", reply["error"]["code"], reply)

    def test_non_consult_candidate_is_an_unsupported_path(self):
        process, info, candidate = self._candidate_with_script()
        trigger = json.dumps(
            {
                "ref_type": "event",
                "namespace": NAMESPACE,
                "event_id": "11111111-1111-4111-8111-111111111111",
            }
        )
        self._patch(
            "collab_interactions",
            "UPDATE collab_interactions SET mode = 'review', work_less = 1, "
            "trigger_ref_json = ? "
            "WHERE interaction_id IN (SELECT interaction_id FROM collab_artifacts "
            "WHERE artifact_id = ?)",
            (trigger, candidate["candidate_id"]),
        )
        reply = self._decide(process, info, candidate, "approve")
        self.assertFalse(reply["ok"], reply)
        self.assertEqual("REVIEW_PATH_UNSUPPORTED", reply["error"]["code"], reply)

    def test_candidate_from_another_policy_is_an_unsupported_path(self):
        process, info, candidate = self._candidate_with_script()
        self._patch(
            "collab_interactions",
            "UPDATE collab_interactions SET policy_ref = 'policy:collab.v1' "
            "WHERE interaction_id IN (SELECT interaction_id FROM collab_artifacts "
            "WHERE artifact_id = ?)",
            (candidate["candidate_id"],),
        )
        reply = self._decide(process, info, candidate, "approve")
        self.assertFalse(reply["ok"], reply)
        self.assertEqual("REVIEW_PATH_UNSUPPORTED", reply["error"]["code"], reply)

    def test_context_that_does_not_belong_to_the_interaction_is_refused(self):
        process, info, candidate = self._candidate_with_script()
        self._patch(
            "collab_interactions",
            "UPDATE collab_interactions SET context_id = ? "
            "WHERE interaction_id IN (SELECT interaction_id FROM collab_artifacts "
            "WHERE artifact_id = ?)",
            ("22222222-2222-4222-8222-222222222222", candidate["candidate_id"]),
        )
        reply = self._decide(process, info, candidate, "approve")
        self.assertFalse(reply["ok"], reply)
        self.assertEqual("CANDIDATE_SOURCE_MISMATCH", reply["error"]["code"], reply)

    def test_wrong_artifact_version_is_a_version_mismatch(self):
        process, info, candidate = self._candidate_with_script()
        reply = process.request(
            "cognition.candidate.decide",
            {
                "session_id": info["session_id"],
                "candidate_id": candidate["candidate_id"],
                "artifact_version": 2,
                "decision": "approve",
            },
        )
        self.assertFalse(reply["ok"], reply)
        self.assertEqual("CANDIDATE_VERSION_MISMATCH", reply["error"]["code"], reply)


if __name__ == "__main__":
    unittest.main()
