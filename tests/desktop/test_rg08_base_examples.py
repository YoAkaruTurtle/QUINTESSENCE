"""RG08 v12: the conversation mode reaches the persona selector.

This is an in-process desktop bridge (the real runtime, the real store and the
real T0 turn registration) with the scripted transport.  No model is called:
only ``conversation_create``/``conversation_send`` run, and the assertion reads
the persona plan the host snapshotted into ``strategy_json["persona"]``.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
for _path in (REPO_ROOT / "src", REPO_ROOT / "apps" / "desktop" / "sidecar"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from tests.desktop import support  # noqa: E402

from pcs_bridge import Bridge  # noqa: E402
from quintessence_pcs.domains.persona.profiles import (  # noqa: E402
    REVIEW_APPROVE_EVENT_TYPE,
    PersonaProfileService,
    profile_body_digest,
)
from quintessence_pcs.foundation.timeutil import utc_now_rfc3339  # noqa: E402
from quintessence_pcs.store import CognitionStore  # noqa: E402

PROFILE_ID = "example.synthetic.base-examples"
PLAIN_TEXT = "没什么特别的事，就是过来找你说两句"

BASE_EXAMPLES = [
    {
        "example_id": "ex13",
        "scene": "owner-base",
        "user": "没什么特别的事，就是过来找你说两句",
        "assistant": "那挺好，我也想和你随便聊聊",
        "synthetic": True,
    },
    {
        "example_id": "ex14",
        "scene": "owner-base",
        "user": "你刚才那句话倒是提醒我了",
        "assistant": "看来我这句没白说嘛",
        "synthetic": True,
    },
]


class BaseExampleModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="rg08-base-examples-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.db_path = self.root / "host.sqlite"
        self._seed_profile()
        self.bridge = None

    def _seed_profile(self) -> None:
        """Publish one synthetic owner profile with two base demos."""

        store = CognitionStore.open(self.db_path)
        try:
            namespace = support.NAMESPACE
            body = {
                "profile_id": PROFILE_ID,
                "profile_version": "12",
                "identity_summary": "Synthetic base-example fixture.",
                "principles": ["Synthetic principle."],
                "style": {
                    "language": "zh-CN",
                    "tone": "plain",
                    "verbosity_by_context": {
                        "casual": "short",
                        "technical": "detailed",
                    },
                },
                "attention_preferences": [],
                "examples": [dict(item) for item in BASE_EXAMPLES],
                "asset_hash": "a" * 64,
                "renderer_compatibility": "pcs.renderer.synthetic-v1",
            }
            digest = profile_body_digest(body)
            review = store.ingest_event(
                namespace=namespace,
                source_kind="user_report",
                source_instance="rg08-base-examples-fixture",
                source_event_id=f"rg08-base-examples-{uuid.uuid4()}",
                event_type=REVIEW_APPROVE_EVENT_TYPE,
                observed_at=utc_now_rfc3339(),
                payload={"decision": "approve", "body_sha256": digest},
            )
            review_ref = {
                "ref_type": "event",
                "namespace": namespace,
                "event_id": review.event_id,
            }
            body["review_ref"] = review_ref
            profiles = PersonaProfileService(store)
            published = profiles.publish_profile(
                namespace=namespace,
                profile_id=PROFILE_ID,
                body=body,
                review_ref=review_ref,
                authorization_ref="authorization:rg08-base-examples",
            )
            profiles.activate_profile(
                namespace=namespace,
                record_id=published.record_id,
                record_version=published.record_version,
                authorization_ref="authorization:rg08-base-examples",
            )
        finally:
            store.close()

    def _start(self, *, speaker: dict) -> Bridge:
        document = support.config_document(
            self.db_path,
            script=[{"action": "none", "reply": "synthetic answer"}],
        )
        document["app"]["speaker"] = dict(speaker)
        config_path = support.write_config(self.root, document, name="host.json")
        bridge = Bridge(str(config_path), None, None)
        self.assertIsNotNone(bridge.runtime, bridge.config_error)
        # LIFO cleanup: stop the worker first, then release the data lock.
        self.addCleanup(bridge._release_lock)
        if bridge.worker is not None:
            self.addCleanup(bridge.worker.stop)
        return bridge

    def _send(self, bridge: Bridge, *, mode: str, client_request_id: str):
        """Run one T0 send on the worker thread (the store's owning thread)."""

        runtime = bridge.runtime
        assert runtime is not None
        box: dict[str, object] = {}
        done = threading.Event()

        def job() -> None:
            try:
                conversation = runtime.conversation_create(mode=mode)
                _conversation, creation = runtime.conversation_send(
                    conversation_id=conversation.conversation_id,
                    client_request_id=client_request_id,
                    text=PLAIN_TEXT,
                )
                box["value"] = creation
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                box["error"] = exc
            finally:
                done.set()

        assert bridge.worker is not None
        bridge.worker.submit(job)
        self.assertTrue(done.wait(10.0), "worker job did not finish")
        if "error" in box:
            raise box["error"]  # type: ignore[misc]
        return box["value"]

    def test_chat_turn_snapshots_base_examples_and_work_turn_does_not(self) -> None:
        bridge = self._start(
            speaker={"identity": "owner", "display_name": "Synthetic Owner"}
        )
        chat = self._send(bridge, mode="chat", client_request_id="base-chat-1")
        work = self._send(bridge, mode="work", client_request_id="base-work-1")
        self.assertEqual(["ex13", "ex14"], chat.turn.strategy["persona"]["example_ids"])
        self.assertEqual([], work.turn.strategy["persona"]["example_ids"])
        # The plan still carries the speaker, so base never bypasses identity.
        self.assertEqual("owner", chat.turn.strategy["persona"]["speaker"]["identity"])
        # T0 only: no model attempt was made by either send.
        connection = sqlite3.connect(str(self.db_path))
        try:
            attempts = connection.execute(
                "SELECT COUNT(*) FROM model_attempts"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(0, int(attempts))

    def test_known_other_gets_no_owner_base_in_a_chat_turn(self) -> None:
        bridge = self._start(
            speaker={"identity": "known_other", "display_name": "Synthetic Visitor"}
        )
        chat = self._send(
            bridge, mode="chat", client_request_id="base-chat-other"
        )
        self.assertEqual([], chat.turn.strategy["persona"]["example_ids"])
        omitted = chat.turn.strategy["persona"]["examples_omitted"]
        reasons = {entry["example_id"]: entry["reason"] for entry in omitted}
        self.assertEqual("speaker_mismatch", reasons["ex13"])
        self.assertEqual("speaker_mismatch", reasons["ex14"])


if __name__ == "__main__":
    unittest.main()
