"""RG08 T3: the ``content_task`` section and the Persona policy record source.

Two contracts are proved here, both deterministic and offline:

* ``build_consult_context(..., content_task=...)`` renders one extra tail
  section and nothing else changes when it is omitted;
* ``build_consult_context(..., policy_record_ref=...)`` registers the exact
  Persona policy version as a ``pinned_asset`` read-set entry and a record
  source, which is what makes the C09 pre-send gate prove it live (and its
  deletion barrier) before any HTTP try.

No model call, no network, no private persona asset: every record below is
synthetic and lives in a temporary database.
"""

from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1] / "src"
for _path in (HERE, SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.domains.persona.policy import (  # noqa: E402
    CANDIDATE_KIND,
    PUBLISHED_KIND,
)
from quintessence_pcs.domains.refs import RecordRef  # noqa: E402
from quintessence_pcs.foundation.errors import (  # noqa: E402
    InvalidInput,
    ScopeViolation,
)
from quintessence_pcs.runtime import ConsultSendPolicy, DirectHttpModel  # noqa: E402
from quintessence_pcs.runtime.collab_protocol import (  # noqa: E402
    COLLAB_PROTOCOL_VERSION,
)
from quintessence_pcs.runtime.context import MAX_EXPRESSION_SECTION_BYTES  # noqa: E402
from quintessence_pcs.runtime.manifest import ModelManifest  # noqa: E402
from quintessence_pcs.runtime.model_port import ModelAdapterError  # noqa: E402
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

HOST = "host:rg08-t3"
ACCESS_REF = "authorization:rg08-t3"
PRIVACY = "private"
ADAPTER = "direct-http"
PID = "example.synthetic.t3"
PV = "1"


class PolicyContextSourceTests(CognitionTestCase):
    # ------------------------------------------------------------- fixtures

    def access(self) -> ExperienceAccess:
        return ExperienceAccess(
            namespace=NS,
            host_instance=HOST,
            access_ref=ACCESS_REF,
            privacy_classes=frozenset({PRIVACY}),
            purposes=frozenset({"consult"}),
        )

    def register(self, text: str = "synthetic consult input") -> dict:
        row = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body=text,
            privacy_class=PRIVACY,
            retention="standard",
            content_version=1,
        )
        return {
            "ref_type": "collab_input",
            "namespace": NS,
            "input_id": row.input_id,
            "content_version": 1,
        }

    def build(self, ref: dict, **extra):
        context = self.contexts.build_consult_context(
            namespace=NS,
            access=self.access(),
            input_refs=[ref],
            adapter_kind=ADAPTER,
            **extra,
        )
        snapshot = self.store.load_context_snapshot(context.context_id)
        return context, snapshot, json.loads(snapshot.rendered_input)

    def build_bare(self, **extra):
        """A consult Context with no collaboration input.

        The send gate walks every collaboration source before the read-set
        proof; a bare Context keeps this file's gate tests focused on the
        policy record instead of on the input-readability fixture.
        """

        context = self.contexts.build_consult_context(
            namespace=NS,
            access=self.access(),
            input_refs=[],
            adapter_kind=ADAPTER,
            **extra,
        )
        snapshot = self.store.load_context_snapshot(context.context_id)
        return context, snapshot, json.loads(snapshot.rendered_input)

    def policy_record(self, kind: str = PUBLISHED_KIND) -> RecordRef:
        body = {
            "schema_version": "1",
            "profile_id": PID,
            "profile_version": PV,
            "text": "synthetic policy text",
            "text_sha256": "0" * 64,
        }
        version = self.store.append_record_version(
            namespace=NS,
            record_id=str(uuid.uuid4()),
            record_version=1,
            kind=kind,
            body=body,
            status="active",
            schema_version="1",
            privacy_class=PRIVACY,
        )
        return RecordRef(
            namespace=NS,
            record_id=version.record_id,
            record_version=version.record_version,
        )

    def send_model(self) -> DirectHttpModel:
        manifest = ModelManifest.from_mapping(
            {
                "provider": "deepseek",
                "base_url": "http://127.0.0.1:9/v1",
                "model": "deepseek-flash",
                "api_key_env": "PCS_TEST_MODEL_API_KEY",
                "max_calls": 8,
                "max_output_tokens_per_call": 512,
                "max_wall_seconds": 30,
                "thinking": "disabled",
                "temperature": 0,
            }
        )
        return DirectHttpModel(
            self.store,
            manifest,
            protocol=COLLAB_PROTOCOL_VERSION,
            consult_policy=ConsultSendPolicy(
                namespace=NS,
                host_instance=HOST,
                access_ref=ACCESS_REF,
                privacy_classes=frozenset({PRIVACY}),
                purposes=frozenset({"consult"}),
                disclose=True,
            ),
        )

    # ------------------------------------------------- omitted = unchanged

    def test_omitted_sections_keep_the_document_byte_identical(self) -> None:
        ref = self.register()
        _, _, legacy = self.build(ref)
        _, _, explicit_none = self.build(
            ref, content_task=None, policy_record_ref=None
        )
        legacy.pop("run_id")
        explicit_none.pop("run_id")
        self.assertEqual(legacy, explicit_none)
        self.assertNotIn("content_task", explicit_none)

    # ------------------------------------------------------- content_task

    def test_content_task_renders_as_its_own_section(self) -> None:
        ref = self.register()
        task = {
            "version": "sdrpe.content_task.v1",
            "sdrpe": "on",
            "policy": "synthetic content-stage policy",
        }
        expression_task = {
            "version": "sdrpe.task.v1",
            "policy": "synthetic expression-stage policy",
        }
        _, _, document = self.build(
            ref, content_task=task, expression_task=expression_task
        )
        self.assertEqual(task, document["content_task"])
        self.assertEqual(expression_task, document["expression_task"])
        # The rendered text is canonical JSON (keys sorted), so the section is
        # identifiable by key, not by insertion position.
        self.assertIn('"content_task"', json.dumps(document))

    def test_content_task_must_be_an_object(self) -> None:
        ref = self.register()
        for value in ("text", 1, []):
            with self.subTest(value=value):
                with self.assertRaises(InvalidInput):
                    self.build(ref, content_task=value)

    def test_oversized_content_task_is_refused_not_trimmed(self) -> None:
        ref = self.register()
        too_big = {"policy": "x" * (MAX_EXPRESSION_SECTION_BYTES + 64)}
        with self.assertRaises(InvalidInput):
            self.build(ref, content_task=too_big)

    # ------------------------------------------------ policy record source

    def test_policy_record_joins_read_set_as_pinned_asset(self) -> None:
        ref = self.register()
        policy = self.policy_record()
        _, snapshot, _ = self.build(ref, policy_record_ref=policy)

        entries = [
            entry
            for entry in snapshot.read_set
            if entry["ref"]["record_id"] == policy.record_id
        ]
        self.assertEqual(1, len(entries), snapshot.read_set)
        self.assertEqual("pinned_asset", entries[0]["usage"])
        self.assertEqual(policy.record_version, entries[0]["ref"]["record_version"])

        sources = [
            (source.ref_kind, source.ref_id, source.ref_version)
            for source in snapshot.sources
        ]
        self.assertIn(("record", policy.record_id, policy.record_version), sources)

    def test_policy_record_absent_from_read_set_when_not_supplied(self) -> None:
        ref = self.register()
        policy = self.policy_record()
        _, snapshot, _ = self.build(ref)
        record_ids = {entry["ref"]["record_id"] for entry in snapshot.read_set}
        self.assertNotIn(policy.record_id, record_ids)

    def test_policy_record_ref_must_be_a_record_ref(self) -> None:
        ref = self.register()
        with self.assertRaises(InvalidInput):
            self.build(ref, policy_record_ref={"record_id": "x"})

    def test_policy_record_outside_the_namespace_is_refused(self) -> None:
        ref = self.register()
        foreign = RecordRef(
            namespace="example.synthetic.other",
            record_id=str(uuid.uuid4()),
            record_version=1,
        )
        with self.assertRaises(ScopeViolation):
            self.build(ref, policy_record_ref=foreign)

    # --------------------------------------------------------- send gate

    def test_send_gate_accepts_a_published_policy_source(self) -> None:
        policy = self.policy_record(PUBLISHED_KIND)
        context, _, _ = self.build_bare(policy_record_ref=policy)
        self.send_model()._require_collab_send_basis(NS, context)

    def test_send_gate_accepts_a_candidate_policy_source(self) -> None:
        policy = self.policy_record(CANDIDATE_KIND)
        context, _, _ = self.build_bare(policy_record_ref=policy)
        self.send_model()._require_collab_send_basis(NS, context)

    def test_send_gate_refuses_a_deleted_policy_with_zero_attempts(self) -> None:
        policy = self.policy_record()
        context, _, _ = self.build_bare(policy_record_ref=policy)
        attempts_before = self.count_rows("model_attempts")

        self.delete_record(policy.record_id)

        with self.assertRaises(ModelAdapterError):
            self.send_model()._require_collab_send_basis(NS, context)
        self.assertEqual(attempts_before, self.count_rows("model_attempts"))


if __name__ == "__main__":
    unittest.main()
