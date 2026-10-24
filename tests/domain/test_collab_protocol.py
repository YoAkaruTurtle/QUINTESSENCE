"""C09: the collab.v1 parser, repair helper, renderer and request builder.

Every test uses the real PCS2-008 consult Context fixture but never opens a
socket.  The parser is deliberately strict: these tests assert the exact
refusal path (``InvalidInput`` / ``ModelConfigurationError``) and never rely on
a permissive fallback.
"""

from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, NS_B, CognitionTestCase  # noqa: E402
from model_support import make_manifest  # noqa: E402

from quintessence_pcs.foundation.errors import InvalidInput  # noqa: E402
from quintessence_pcs.foundation.jsonutil import canonical_json_text  # noqa: E402
from quintessence_pcs.runtime import (  # noqa: E402
    COLLAB_ACTIONS,
    COLLAB_MODEL_POLICY_VERSION,
    COLLAB_PROTOCOL_VERSION,
    COLLAB_RENDERER_VERSION,
    MAX_BRIEF_REASON_CHARS,
    MAX_REFERENCES,
    MAX_REPLY_CHARS,
    ConsultSendPolicy,
    build_collab_request_body,
    build_request_body,
    parse_collab_output,
    render_collab_messages,
    repair_feedback,
)
from quintessence_pcs.runtime.model_port import ModelConfigurationError  # noqa: E402
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

HOST = "host:synthetic-c09-protocol"
ACCESS_REF = "authorization:access:c09-protocol"
CONSULT_BODY = "Synthetic consult question for the protocol tests."


class CollabProtocolTests(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        row = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body=CONSULT_BODY,
            privacy_class="private",
            retention="standard",
            content_version=1,
        )
        self.ref = {
            "ref_type": "collab_input",
            "namespace": NS,
            "input_id": row.input_id,
            "content_version": row.content_version,
        }
        self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance=HOST,
            access_ref=ACCESS_REF,
            policy_ref="policy:collab.v1",
            input_refs=[self.ref],
        )
        self.access = ExperienceAccess(
            namespace=NS,
            host_instance=HOST,
            access_ref=ACCESS_REF,
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"consult"}),
        )
        self.context = self.contexts.build_consult_context(
            namespace=NS,
            access=self.access,
            input_refs=[self.ref],
            adapter_kind="direct-http",
        )
        self.manifest = make_manifest("https://api.deepseek.com")

    def parse(self, content, *, allowed=None, mode="consult"):
        return parse_collab_output(
            content,
            mode=mode,
            allowed_references=[self.ref] if allowed is None else allowed,
        )

    # ---------------------------------------------------------------- parser

    def test_parser_accepts_wait_query_and_none(self) -> None:
        none = self.parse('{"action":"none","reply":"synthetic reply"}')
        self.assertEqual("none", none.action)
        self.assertEqual("synthetic reply", none.reply)
        self.assertEqual((), none.references)
        self.assertIsNone(none.brief_reason)

        wait = self.parse('{"action":"wait","brief_reason":"not yet"}')
        self.assertEqual("wait", wait.action)
        self.assertEqual("not yet", wait.brief_reason)

        query = self.parse('{"action":"query"}')
        self.assertEqual("query", query.action)
        self.assertIsNone(query.reply)

        with_reference = self.parse(
            canonical_json_text(
                {"action": "query", "references": [self.ref]}
            )
        )
        self.assertEqual((self.ref,), with_reference.references)

        none_with_reason = self.parse(
            '{"action":"none","brief_reason":"answered without a reply"}'
        )
        self.assertEqual("answered without a reply", none_with_reason.brief_reason)

    def test_parser_rejects_consult_propose(self) -> None:
        with self.assertRaises(InvalidInput) as caught:
            self.parse('{"action":"propose","reply":"a complete shape"}')
        self.assertIn("consult cannot propose", str(caught.exception))
        self.assertEqual("INVALID_SCHEMA", caught.exception.code)

    def test_parser_rejects_malformed_missing_and_hidden_fields(self) -> None:
        cases = [
            "not json at all",
            "[]",
            '"a json string"',
            "{}",
            '{"reply":"missing action"}',
            '{"action":"execute"}',
            '{"action":"none"}',
            '{"action":"none","reply":""}',
            '{"action":"none","reply":"ok","extra":1}',
            '{"action":"none","reply":"ok","request_id":"'
            + str(uuid.uuid4())
            + '"}',
            '{"action":"none","reply":"ok","read_set":[]}',
            '{"action":"none","reply":"ok","state_revision":1}',
            '{"action":"none","reply":"ok","authorization":"auth:model"}',
            '{"action":"none","reply":"ok","context_id":"'
            + str(uuid.uuid4())
            + '"}',
        ]
        for content in cases:
            with self.subTest(content=content):
                with self.assertRaises(InvalidInput) as caught:
                    self.parse(content)
                self.assertEqual("INVALID_SCHEMA", caught.exception.code)

        with self.assertRaises(InvalidInput):
            self.parse('{"action":"none","reply":"ok"}', mode="review")

    def test_parser_enforces_text_bounds(self) -> None:
        self.assertEqual(
            MAX_REPLY_CHARS,
            len(self.parse('{"action":"none","reply":"' + "x" * MAX_REPLY_CHARS + '"}').reply or ""),
        )
        with self.assertRaises(InvalidInput):
            self.parse('{"action":"none","reply":"' + "x" * (MAX_REPLY_CHARS + 1) + '"}')
        self.assertEqual(
            MAX_BRIEF_REASON_CHARS,
            len(
                self.parse(
                    '{"action":"wait","brief_reason":"'
                    + "x" * MAX_BRIEF_REASON_CHARS
                    + '"}'
                ).brief_reason
                or ""
            ),
        )
        with self.assertRaises(InvalidInput):
            self.parse(
                '{"action":"wait","brief_reason":"'
                + "x" * (MAX_BRIEF_REASON_CHARS + 1)
                + '"}'
            )
        with self.assertRaises(InvalidInput):
            self.parse('{"action":"none","reply":5}')

    def test_parser_requires_an_exact_allowed_reference_match(self) -> None:
        expected = self.parse(
            canonical_json_text({"action": "query", "references": [self.ref]})
        )
        self.assertEqual(self.ref, expected.references[0])

        wrong_namespace = dict(self.ref, namespace=NS_B)
        wrong_version = dict(self.ref, content_version=2)
        wrong_id = dict(self.ref, input_id=str(uuid.uuid4()))
        for reference in (wrong_namespace, wrong_version, wrong_id):
            with self.subTest(reference=reference):
                with self.assertRaises(InvalidInput):
                    self.parse(
                        canonical_json_text(
                            {"action": "query", "references": [reference]}
                        )
                    )

        with self.assertRaises(InvalidInput):
            self.parse(
                '{"action":"query","references":[' + json.dumps(self.ref) + ","
                + json.dumps(self.ref) + "]}"
            )

        extra_ref = {
            "ref_type": "collab_input",
            "namespace": NS,
            "input_id": str(uuid.uuid4()),
            "content_version": 1,
        }
        allowed = [self.ref, extra_ref]
        with self.assertRaises(InvalidInput):
            self.parse(
                '{"action":"query","references":[' + json.dumps(extra_ref) + "]}"
            )

        many = [
            {
                "ref_type": "collab_input",
                "namespace": NS,
                "input_id": str(uuid.uuid4()),
                "content_version": 1,
            }
            for _ in range(MAX_REFERENCES + 1)
        ]
        with self.assertRaises(InvalidInput):
            self.parse(
                canonical_json_text({"action": "query", "references": many}),
                allowed=many,
            )

    # ------------------------------------------------------------- repair

    def test_repair_feedback_is_fixed_bounded_and_body_free(self) -> None:
        feedback = repair_feedback(InvalidInput("SECRET " + CONSULT_BODY))
        self.assertIn("InvalidInput", feedback)
        self.assertIn(COLLAB_PROTOCOL_VERSION, feedback)
        for action in COLLAB_ACTIONS:
            self.assertIn(action, feedback)
        self.assertNotIn(CONSULT_BODY, feedback)
        self.assertNotIn("SECRET", feedback)
        self.assertLess(len(feedback), 512)
        self.assertEqual(
            feedback, repair_feedback(InvalidInput("a different body"))
        )

    # ------------------------------------------------------------- renderer

    def test_renderer_declares_versions_and_keeps_user_input_exact(self) -> None:
        messages = render_collab_messages(self.context)
        self.assertEqual(2, len(messages))
        self.assertEqual(["system", "user"], [item["role"] for item in messages])
        for item in messages:
            self.assertEqual({"role", "content"}, set(item))

        self.assertEqual(self.context.rendered_input, messages[1]["content"])
        declaration = json.loads(messages[0]["content"])
        self.assertEqual(COLLAB_PROTOCOL_VERSION, declaration["protocol_version"])
        self.assertEqual(COLLAB_RENDERER_VERSION, declaration["renderer_version"])
        self.assertEqual("consult", declaration["mode"])
        self.assertEqual(
            list(COLLAB_ACTIONS), declaration["allowed_actions"]
        )
        self.assertEqual("propose", declaration["consult_forbidden_action"])
        self.assertEqual(
            {
                "request_id",
                "read_set",
                "state_revision",
                "authorization",
                "context_id",
            },
            set(declaration["forbidden_output_fields"]),
        )
        self.assertIn("exactly", declaration["references_policy"])
        self.assertEqual(
            "model_output.json", declaration["output_schema"]["$id"]
        )
        self.assertEqual(
            ["action"], declaration["output_schema"]["required"]
        )
        self.assertEqual(
            {"reply", "action", "references", "brief_reason"},
            set(declaration["output_schema"]["properties"]),
        )
        policy = declaration["query_policy"]
        self.assertEqual(
            {
                "use_query_when",
                "do_not_query_when",
                "host_owned_plan",
                "after_query",
            },
            set(policy),
        )
        self.assertIn("preferences", policy["use_query_when"])
        self.assertIn("ordinary general knowledge", policy["do_not_query_when"])
        self.assertIn("host", policy["host_owned_plan"])
        self.assertIn("empty", policy["after_query"])
        self.assertIn("never", policy["after_query"])
        self.assertNotIn("propose", policy["after_query"])

    def test_renderer_appends_assistant_and_feedback_for_repair(self) -> None:
        messages = render_collab_messages(
            self.context,
            previous_output='{"bad":"previous output"}',
            feedback="Return exactly one JSON object.",
        )
        self.assertEqual(
            ["system", "user", "assistant", "user"],
            [item["role"] for item in messages],
        )
        self.assertEqual(self.context.rendered_input, messages[1]["content"])
        self.assertEqual('{"bad":"previous output"}', messages[2]["content"])
        self.assertEqual(
            "Return exactly one JSON object.", messages[3]["content"]
        )

        with self.assertRaises(ModelConfigurationError):
            render_collab_messages(self.context, previous_output="only previous")
        with self.assertRaises(ModelConfigurationError):
            render_collab_messages(self.context, feedback="only feedback")
        with self.assertRaises(ModelConfigurationError):
            render_collab_messages(
                self.context, previous_output="previous", feedback="   "
            )

    # -------------------------------------------------------------- request

    def test_collab_body_is_canonical_and_legacy_body_is_byte_identical(self) -> None:
        messages = render_collab_messages(self.context)
        body_text = build_collab_request_body(
            self.manifest, messages=messages, max_tokens=512
        )
        body = json.loads(body_text)
        self.assertEqual(
            {
                "model",
                "messages",
                "thinking",
                "temperature",
                "max_tokens",
                "stream",
            },
            set(body),
        )
        self.assertEqual(self.manifest.model, body["model"])
        self.assertEqual(
            [dict(message) for message in messages], body["messages"]
        )
        self.assertEqual({"type": "disabled"}, body["thinking"])
        self.assertEqual(0, body["temperature"])
        self.assertEqual(512, body["max_tokens"])
        self.assertFalse(body["stream"])
        self.assertEqual(canonical_json_text(body), body_text)

        with self.assertRaises(ModelConfigurationError):
            build_collab_request_body(
                self.manifest,
                messages=(
                    {"role": "user", "content": "x", "tool_calls": []},
                ),
                max_tokens=512,
            )
        with self.assertRaises(ModelConfigurationError):
            build_collab_request_body(
                self.manifest,
                messages=({"role": "tool", "content": "x"},),
                max_tokens=512,
            )

        legacy = build_request_body(
            self.manifest,
            rendered_input="synthetic legacy input",
            max_tokens=512,
        )
        self.assertEqual(
            canonical_json_text(
                {
                    "model": self.manifest.model,
                    "messages": [
                        {
                            "role": "user",
                            "content": "synthetic legacy input",
                        }
                    ],
                    "thinking": {"type": "disabled"},
                    "temperature": 0,
                    "max_tokens": 512,
                    "stream": False,
                }
            ),
            legacy,
        )

    # --------------------------------------------------------------- policy

    def test_send_policy_validates_its_fields(self) -> None:
        policy = ConsultSendPolicy(
            namespace=NS,
            host_instance=HOST,
            access_ref=ACCESS_REF,
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"consult"}),
            disclose=True,
        )
        self.assertIsInstance(policy.to_experience_access(), ExperienceAccess)
        self.assertEqual(
            COLLAB_MODEL_POLICY_VERSION,
            "collab.v1.direct-http.v2",
        )

        invalid_values = [
            {"namespace": "Not A Namespace"},
            {"host_instance": ""},
            {"access_ref": ""},
            {"privacy_classes": {"private"}},
            {"privacy_classes": frozenset({"unknown"})},
            {"purposes": {"consult"}},
            {"purposes": frozenset({""})},
            {"disclose": 1},
        ]
        defaults = {
            "namespace": NS,
            "host_instance": HOST,
            "access_ref": ACCESS_REF,
            "privacy_classes": frozenset({"private"}),
            "purposes": frozenset({"consult"}),
            "disclose": True,
        }
        for override in invalid_values:
            with self.subTest(override=override):
                values = dict(defaults)
                values.update(override)
                with self.assertRaises(InvalidInput):
                    ConsultSendPolicy(**values)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
