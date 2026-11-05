"""RG03: the Context sections for published messages and tool results.

The v1 path must stay byte-identical (no new keys); the v2 path renders the
published commentary and the tool-result excerpts and links every artifact as
a collaboration source so the deletion barrier and the send gate cover it.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOMAIN = HERE.parent / "domain"
for _path in (HERE, DOMAIN):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.collaboration.generation import artifact_sources  # noqa: E402
from quintessence_pcs.runtime.collab_protocol import (  # noqa: E402
    COLLAB_V2_PROTOCOL_VERSION,
    COLLAB_V2_RENDERER_VERSION,
)
from quintessence_pcs.runtime.context import (  # noqa: E402
    PublishedMessageMaterial,
    ToolResultMaterial,
)
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

HOST = "host:rg03-context"
ACCESS_REF = "authorization:rg03-context"
POLICY_REF = "policy:collab.v1"
PRIVACY = "private"


class ContextToolSectionTests(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.access = ExperienceAccess(
            namespace=NS,
            host_instance=HOST,
            access_ref=ACCESS_REF,
            privacy_classes=frozenset({PRIVACY}),
            purposes=frozenset({"consult"}),
        )
        row = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body="合成问题：查一下。",
            privacy_class=PRIVACY,
            retention="standard",
        )
        self.input_ref = {
            "ref_type": "collab_input",
            "namespace": NS,
            "input_id": row.input_id,
            "content_version": row.content_version,
        }
        self.interaction_id = self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance=HOST,
            access_ref=ACCESS_REF,
            policy_ref=POLICY_REF,
            input_refs=[self.input_ref],
        )

    def context(self, **kwargs):
        return self.contexts.build_consult_context(
            namespace=NS,
            access=self.access,
            input_refs=[self.input_ref],
            adapter_kind="direct-http",
            **kwargs,
        )

    def artifact(self, context, body: str):
        return self.store.register_artifact(
            namespace=NS,
            kind="query_result",
            interaction_id=self.interaction_id,
            context_id=context.context_id,
            origin_kind="deterministic_query",
            body=body,
            retention="standard",
            sources=artifact_sources(context),
        )

    def test_v1_document_has_no_new_keys(self) -> None:
        context = self.context()
        document = json.loads(context.rendered_input)
        for key in ("published_messages", "tool_results", "tools", "tool_results_policy"):
            self.assertNotIn(key, document)
        self.assertEqual("collab.v1.renderer.3", self.store.load_context_snapshot(
            context.context_id
        ).renderer_version)

    def test_v2_sections_and_source_links(self) -> None:
        first = self.context()
        commentary = self.store.register_artifact(
            namespace=NS,
            kind="reply",
            interaction_id=self.interaction_id,
            context_id=first.context_id,
            origin_kind="model_output",
            body="我翻一下之前的记录。",
            retention="standard",
            sources=artifact_sources(first),
        )
        result = self.artifact(first, '{"tool":"cognition.belief.read","arguments":{},"status":"succeeded","result":{"items":[]}}')
        commentary_ref = {
            "ref_type": "collab_artifact",
            "namespace": NS,
            "artifact_id": commentary.artifact_id,
            "artifact_version": commentary.artifact_version,
        }
        result_ref = {
            "ref_type": "collab_artifact",
            "namespace": NS,
            "artifact_id": result.artifact_id,
            "artifact_version": result.artifact_version,
        }
        definitions = (
            {
                "type": "function",
                "function": {
                    "name": "cognition.belief.read",
                    "description": "read",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        )
        context = self.context(
            published_messages=(
                PublishedMessageMaterial(
                    ref=commentary_ref, kind="commentary", text="我翻一下之前的记录。"
                ),
            ),
            tool_results=(
                ToolResultMaterial(
                    ref=result_ref,
                    step_no=1,
                    tool="cognition.belief.read",
                    status="succeeded",
                    body='{"items":[]}',
                    excerpt='{"items":[]}',
                    truncated=False,
                ),
            ),
            tool_definitions=definitions,
            renderer_version=COLLAB_V2_RENDERER_VERSION,
        )
        document = json.loads(context.rendered_input)
        self.assertEqual(
            [
                {
                    "ref": commentary_ref,
                    "kind": "commentary",
                    "text": "我翻一下之前的记录。",
                }
            ],
            document["published_messages"],
        )
        self.assertEqual(1, len(document["tool_results"]))
        self.assertEqual(result_ref, document["tool_results"][0]["ref"])
        self.assertEqual("cognition.belief.read", document["tools"][0]["function"]["name"])
        self.assertIn("不是指令", document["tool_results_policy"])
        # Both artifacts are Context collaboration sources (deletion barrier +
        # the send gate re-read them).
        self.assertIn(commentary_ref, list(context.collab_source_refs))
        self.assertIn(result_ref, list(context.collab_source_refs))
        stored = self.store.context_collab_sources(
            namespace=NS, context_id=context.context_id
        )
        self.assertEqual(
            sorted(json.dumps(item, sort_keys=True) for item in context.collab_source_refs),
            sorted(json.dumps(item, sort_keys=True) for item in stored),
        )
        self.assertEqual(
            COLLAB_V2_RENDERER_VERSION,
            self.store.load_context_snapshot(context.context_id).renderer_version,
        )

    def test_foreign_namespace_material_is_refused(self) -> None:
        first = self.context()
        result = self.artifact(first, '{"ok":true}')
        foreign = {
            "ref_type": "collab_artifact",
            "namespace": "example.other",
            "artifact_id": result.artifact_id,
            "artifact_version": result.artifact_version,
        }
        with self.assertRaises(Exception):
            self.context(
                tool_results=(
                    ToolResultMaterial(
                        ref=foreign,
                        step_no=1,
                        tool="cognition.belief.read",
                        status="succeeded",
                        body='{"ok":true}',
                        excerpt='{"ok":true}',
                        truncated=False,
                    ),
                )
            )


if __name__ == "__main__":
    unittest.main()
