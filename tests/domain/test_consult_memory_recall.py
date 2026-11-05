"""RG08 R-B: memory recall material in a consult Context."""

from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.domains.memory.scope_recall_recall import (  # noqa: E402
    MemoryRecallItem,
    MemoryRecallMaterial,
)
from quintessence_pcs.foundation.errors import ContextStale  # noqa: E402
from quintessence_pcs.runtime import ContextService  # noqa: E402
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

HOST = "host:rg08-rb-context"
ACCESS_REF = "authorization:rg08-rb-context"


class ConsultMemoryRecallTests(CognitionTestCase):
    def access(self) -> ExperienceAccess:
        return ExperienceAccess(
            namespace=NS,
            host_instance=HOST,
            access_ref=ACCESS_REF,
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"consult"}),
        )

    def _seed_source(self):
        row = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body="我喜欢红色。",
            privacy_class="private",
            retention="standard",
        )
        self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance=HOST,
            access_ref=ACCESS_REF,
            policy_ref="policy:collab.v1",
            input_refs=[
                {
                    "ref_type": "collab_input",
                    "namespace": NS,
                    "input_id": row.input_id,
                    "content_version": row.content_version,
                }
            ],
        )
        event = self.ingest(event_type="ExperienceObserved", payload={"text": "我喜欢红色。"})
        external_ref = "event-" + uuid.uuid4().hex
        delivery_id = str(uuid.uuid4())
        source_key = f"source-{uuid.uuid4()}"
        now = "2026-10-20T10:00:00Z"
        connection = self.store._require_connection()
        connection.execute(
            "INSERT INTO memory_delivery ("
            "delivery_id, namespace, event_id, ref_type, ref_id, ref_version, "
            "source_event_key, source_revision, request_hash, request_json, "
            "state, attempt, external_ref, external_revision, created_at, updated_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                delivery_id,
                NS,
                event.event_id,
                "collab_input",
                row.input_id,
                row.content_version,
                source_key,
                1,
                "0" * 64,
                "{}",
                "applied",
                1,
                external_ref,
                1,
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO experience_source_mapping ("
            "namespace, ref_type, ref_id, ref_version, event_id, source_event_key, "
            "source_revision, privacy_classes_json, retention, origin, role, "
            "principal_kind, root_source_key, root_members_json, root_verified, "
            "source_dependencies_json, delivery_id, format_version, created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                NS,
                "collab_input",
                row.input_id,
                row.content_version,
                event.event_id,
                source_key,
                1,
                '["private"]',
                "standard",
                "user_statement",
                "user",
                "human",
                "0" * 64,
                "[]",
                1,
                '[{"ref_type":"collab_input","ref_id":"'
                + row.input_id
                + '","ref_version":'
                + str(row.content_version)
                + "}]",
                delivery_id,
                "j04.projection.v1",
                now,
            ),
        )
        connection.commit()
        return row, event, external_ref

    def material(self, *, status: str = "ok", content: str = "我喜欢红色。", items=1):
        row, event, external_ref = self._seed_source()
        built = []
        for index in range(items):
            source_event = event if index == 0 else self.ingest(
                event_type="ExperienceObserved", payload={"text": content}
            )
            ref_id = row.input_id if index == 0 else str(uuid.uuid4())
            if index:
                # A second item needs a real collab source; use the artifact
                # path only when it exists, otherwise keep one item.
                break
            built.append(
                MemoryRecallItem(
                    upstream_ref=external_ref,
                    upstream_revision=1,
                    upstream_kind="event",
                    content=content,
                    content_truncated=False,
                    subject="user",
                    subject_ref=f"collab_input:{ref_id}@{row.content_version}",
                    temporal_status="current",
                    occurred_at="2026-10-20T10:00:00+08:00",
                    recorded_at=None,
                    scope_id="scope:rg08-rb",
                    source_event_id=source_event.event_id,
                    source_ref={
                        "ref_type": "collab_input",
                        "namespace": NS,
                        "input_id": row.input_id,
                        "content_version": row.content_version,
                    },
                    delivery_state="applied",
                )
            )
        return MemoryRecallMaterial(
            status=status,
            items=tuple(built),
            coverage="unknown",
            upstream_answerability="supported",
            memory_epoch=1,
            elapsed_ms=12,
            units=sum(item.units for item in built),
            bytes=sum(len(item.content.encode("utf-8")) for item in built),
        )

    def test_material_renders_as_history_not_beliefs(self):
        material = self.material()
        context = self.contexts.build_consult_context(
            namespace=NS,
            access=self.access(),
            input_refs=[],
            memory_recall=material,
        )
        self.assertIn("memory_recall", context.rendered_input)
        self.assertNotIn("event-", context.rendered_input)
        self.assertNotIn("collab_input:", context.rendered_input)
        snapshot = self.contexts._store.load_context_snapshot(context.context_id)
        self.assertEqual((), snapshot.memory_hits)
        self.assertIn(
            ("event", material.items[0].source_event_id, 0),
            {(source.ref_kind, source.ref_id, source.ref_version) for source in snapshot.sources},
        )
        collab = self.store.context_collab_sources(
            namespace=NS, context_id=context.context_id
        )
        self.assertIn(
            material.items[0].source_ref,
            [dict(ref) for ref in collab],
        )
        budget = snapshot.budget_view["memory_recall"]
        self.assertEqual("ok", budget["status"])
        self.assertEqual(1, budget["items"])
        self.assertNotIn("items_detail", budget)

    def test_material_policy_names_the_speaker_not_the_subject(self):
        material = self.material()
        context = self.contexts.build_consult_context(
            namespace=NS,
            access=self.access(),
            input_refs=[],
            memory_recall=material,
        )
        document = json.loads(context.rendered_input)
        policy = document["memory_recall"]["policy"]
        self.assertIn("subject 表示说话者", policy)
        self.assertIn("不是句中被谈论的人", policy)
        self.assertIn("第三人称", policy)
        self.assertIn("用户本人的偏好或事实", policy)

    def test_memory_is_dropped_before_the_total_context_byte_cap(self):
        material = self.material(content="喜欢" * 1600)
        small = ContextService(
            self.store,
            self_view_service=self.self_view,
            profile_service=self.profiles,
            pcs_context_max_bytes=4096,
        )
        context = small.build_consult_context(
            namespace=NS,
            access=self.access(),
            input_refs=[],
            memory_recall=material,
        )
        self.assertNotIn("memory_recall", context.rendered_input)
        snapshot = small._store.load_context_snapshot(context.context_id)
        self.assertTrue(
            any(item.get("reason") == "memory_budget" for item in snapshot.omissions),
            snapshot.omissions,
        )
        self.assertLessEqual(
            len(context.rendered_input.encode("utf-8")),
            snapshot.budget_view["limit_bytes"],
        )

    def test_deleted_source_after_build_bars_context(self):
        material = self.material()
        context = self.contexts.build_consult_context(
            namespace=NS,
            access=self.access(),
            input_refs=[],
            memory_recall=material,
        )
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=material.items[0].source_ref["input_id"],
            ref_version=material.items[0].source_ref["content_version"],
            authorization_ref="authorization:rg08-rb-delete",
        )
        with self.assertRaises(ContextStale):
            self.contexts.require_current_consult_context(
                context_id=context.context_id, namespace=NS
            )

    def test_deleted_source_before_save_is_stale(self):
        material = self.material()
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=material.items[0].source_ref["input_id"],
            ref_version=material.items[0].source_ref["content_version"],
            authorization_ref="authorization:rg08-rb-delete",
        )
        with self.assertRaises(ContextStale):
            self.contexts.build_consult_context(
                namespace=NS,
                access=self.access(),
                input_refs=[],
                memory_recall=material,
            )

    def test_item_must_be_material_type(self):
        with self.assertRaises(Exception):
            self.contexts.build_consult_context(
                namespace=NS,
                access=self.access(),
                input_refs=[],
                memory_recall={"status": "ok"},
            )


if __name__ == "__main__":
    unittest.main()
