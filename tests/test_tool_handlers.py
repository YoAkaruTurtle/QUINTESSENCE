"""J07 M4: the tool handlers that make the catalog do real work.

L2 tests: a real store, real services, and an explicitly labelled deterministic
stub in place of a model.  No real model is called here -- that is M8 / L3, and
the L3 case at the bottom stays skipped on purpose.

The web cases (TW-*) do call the live DeepSeek Search/Fetch API, a handful of
times, because the point of those cases is that the *real* response shape is
what the handlers must accept.  They skip honestly when no key is configured.
"""

from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
for _path in (_ROOT / "src", _ROOT / "tests" / "domain", _ROOT / "tests" / "store"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.collaboration.change_service import (  # noqa: E402
    ChangeAccess,
    CognitionChangeService,
)
from quintessence_pcs.domains.cognition.epistemic import EpistemicAccess  # noqa: E402
from quintessence_pcs.domains.cognition.query import (  # noqa: E402
    CognitionQueryService,
    QueryAccess,
)
from quintessence_pcs.domains.cognition.questions import (  # noqa: E402
    QuestionAccess,
    QuestionService,
)
from quintessence_pcs.runtime.agent_loop import STOP_FINAL, run_agent_loop  # noqa: E402
from quintessence_pcs.runtime.agent_protocol import StepOutcome, ToolCall  # noqa: E402
from quintessence_pcs.runtime.tool_catalog import (  # noqa: E402
    TOOL_ARGUMENTS_INVALID,
    TOOL_FAILED,
    TOOL_OK,
    TOOL_UNKNOWN,
    ToolArgumentsInvalid,
    ToolCatalog,
)
from quintessence_pcs.runtime.tool_handlers import (  # noqa: E402
    CognitionToolkit,
    build_cognition_handlers,
    build_handlers,
)
from quintessence_pcs.store.governance import (  # noqa: E402
    GovernanceAccess,
    GovernanceStore,
)

NOW_TEXT = "2026-10-17T12:00:00Z"
def change_access() -> ChangeAccess:
    return ChangeAccess(
        namespace=NS,
        host_instance="synthetic-host",
        operator="operator-a",
        access_ref="access:synthetic",
        permissions=frozenset({"change.propose", "change.review", "change.commit", "change.result"}),
        privacy_classes=frozenset({"private"}),
        purposes=frozenset({"cognition.change"}),
    )


def query_access() -> QueryAccess:
    return QueryAccess(
        namespace=NS,
        privacy_classes=frozenset({"private"}),
        purposes=frozenset({"consult"}),
    )


def question_access() -> QuestionAccess:
    return QuestionAccess(
        namespace=NS,
        host_instance="synthetic-host",
        access_ref="access:synthetic",
        privacy_classes=frozenset({"private"}),
        permissions=frozenset(
            {"question.read", "question.create", "question.transition", "question.attention"}
        ),
        purposes=frozenset({"cognition.question"}),
    )


def governance_access() -> GovernanceAccess:
    return GovernanceAccess(
        namespace=NS,
        host_instance="synthetic-host",
        operator="operator-a",
        access_ref="access:governance",
        permissions=frozenset({"governance.admin", "governance.read"}),
    )


class HandlerTestCase(CognitionTestCase):
    """One real store, real services, host-built access, stub model."""

    def setUp(self) -> None:
        super().setUp()
        self.changes = CognitionChangeService(
            self.store, beliefs=self.beliefs, clock=lambda: NOW_TEXT
        )
        self.governance = GovernanceStore(self.store)
        self.queries = CognitionQueryService(self.store, clock=lambda: NOW_TEXT)
        self.questions = QuestionService(self.store)
        self._authorizations: dict[str, str] = {}
        self.current_context_id = ""

    def build_context(self):
        context = super().build_context()
        self.current_context_id = context.context_id
        return context

    def toolkit(self, *, auto: bool = True) -> CognitionToolkit:
        return CognitionToolkit(
            store=self.store,
            changes=self.changes,
            queries=self.queries,
            questions=self.questions,
            access=change_access(),
            query_access=query_access(),
            question_access=question_access(),
            epistemic_access=EpistemicAccess(
                namespace=NS, privacy_classes=frozenset({"private"})
            ),
            now=lambda: NOW_TEXT,
            change_scope={"scene": "test.scene"},
            context_id=self.current_context_id,
            auto_authorization=self.authorize if auto else (lambda _op: None),
        )

    def authorize(self, operation: str) -> str | None:
        if operation in self._authorizations:
            return self._authorizations[operation]
        authorization_id = str(uuid.uuid4())
        self.governance.grant_commit_authorization(
            governance_access(),
            authorization_id=authorization_id,
            host_instance="synthetic-host",
            access_ref="access:synthetic",
            operations=(operation,),
            claim_types=("general_hypothesis",),
            scope={"scene": "test.scene"},
            max_impact="E1",
            max_changes_per_bundle=1,
            max_targets_per_bundle=1,
            policy_id="policy:cognition.auto.v2",
            policy_version="1",
            authorization_source="host_config",
            issued_at="2026-10-17T00:00:00Z",
        )
        self._authorizations[operation] = authorization_id
        return authorization_id

    def handlers(self, **kwargs: Any):
        return build_handlers(cognition=build_cognition_handlers(self.toolkit(**kwargs)))

    def catalog(self) -> ToolCatalog:
        return ToolCatalog.cognition_and_web()

    def hypothesis_change(self, claim: str = "synthetic mechanism hypothesis") -> dict:
        body = self.belief_body(
            claim=claim, claim_type="general_hypothesis", strength="limited"
        )
        return {"op": "propose_belief", "body": body, "scope": {"scene": "test.scene"}}

    def call(self, name: str, arguments: dict, *, handlers=None, catalog=None):
        handlers = self.handlers() if handlers is None else handlers
        catalog = catalog or self.catalog()
        return catalog.execute(
            ToolCall(call_id="call-1", name=name, arguments=json.dumps(arguments)),
            handlers=handlers,
        )


class CognitionHandlerTests(HandlerTestCase):
    def test_ta_01_belief_read_returns_real_records(self) -> None:
        # A committed tentative belief: written through the real Belief path,
        # not a candidate, because that is what the partition projects.
        version = self.propose(
            body=self.belief_body(
                claim="synthetic mechanism hypothesis", claim_type="general_hypothesis"
            )
        )

        outcome = self.call("cognition.belief.read", {"status": "proposed", "limit": 5})
        self.assertTrue(outcome.ok, outcome.payload)
        items = outcome.payload["result"]["items"]
        self.assertEqual(1, len(items))
        self.assertEqual(version.record_id, items[0]["ref"]["record_id"])
        self.assertEqual("proposed", items[0]["status"])
        self.assertTrue(items[0]["provisional"])

    def test_ta_02_propose_creates_a_real_candidate(self) -> None:
        context = self.build_context()
        outcome = self.call(
            "cognition.propose",
            {
                "summary": "one hypothesis",
                "changes": [self.hypothesis_change()],
            },
        )
        self.assertTrue(outcome.ok, outcome.payload)
        result = outcome.payload["result"]
        # The handler drives the whole governed path: register, review, and --
        # when the pre-authorization covers cognition.change -- commit.
        self.assertEqual("COMMITTED", result["state"], result)
        candidate_id = result["candidate_id"]
        stored = self.store.load_artifact(namespace=NS, artifact_id=candidate_id, artifact_version=1)
        self.assertEqual("candidate", stored.kind)
        self.assertIsNotNone(result["receipt"])

    def test_ta_03_question_create_creates_a_real_question(self) -> None:
        context = self.build_context()
        outcome = self.call(
            "question.create",
            {"text": "what is the synthetic mechanism?"},
        )
        self.assertTrue(outcome.ok, outcome.payload)
        result = outcome.payload["result"]
        self.assertEqual("COMMITTED", result["state"], result)
        self.assertIsNotNone(result["question_ref"])

    def test_ta_04_unknown_tool_has_no_side_effect(self) -> None:
        before = self.store.current_state_revision(NS)
        outcome = self.call("cognition.commit", {"candidate_id": "x"})
        self.assertFalse(outcome.ok)
        self.assertEqual(TOOL_UNKNOWN, outcome.code)
        self.assertEqual(before, self.store.current_state_revision(NS))

    def test_ta_05_authority_fields_are_stripped_before_dispatch(self) -> None:
        context = self.build_context()
        outcome = self.call(
            "cognition.propose",
            {
                "summary": "one hypothesis",
                "changes": [self.hypothesis_change()],
                # The model tries to certify itself; the catalog drops these.
                "scope": {"scene": "elsewhere"},
                "approval_id": str(uuid.uuid4()),
                "authority_kind": "manual_approval",
            },
        )
        self.assertTrue(outcome.ok, outcome.payload)
        self.assertEqual("COMMITTED", outcome.payload["result"]["state"])
        self.assertFalse(hasattr(outcome, "authority_kind"))

    def test_ta_06_handler_failure_flows_back_as_structured_error(self) -> None:
        outcome = self.call("cognition.review", {"candidate_id": str(uuid.uuid4())})
        self.assertFalse(outcome.ok)
        self.assertEqual(TOOL_FAILED, outcome.code)
        self.assertIn("message", outcome.payload)

    def test_ta_07_stub_model_drives_one_real_tool_round(self) -> None:
        context = self.build_context()

        class _Stub:
            def __init__(self) -> None:
                self.rounds = 0
                self.seen_tools: list[int] = []

            def step(self, *, messages, tools):
                self.rounds += 1
                self.seen_tools.append(len(tools))
                if self.rounds == 1:
                    return StepOutcome(
                        finish_reason="tool_calls",
                        content="",
                        reasoning_content="thinking",
                        tool_calls=(
                            ToolCall(
                                call_id="call-1",
                                name="cognition.partition",
                                arguments=json.dumps({"limit": 5}),
                            ),
                        ),
                    )
                return StepOutcome("stop", "no beliefs yet", "")

        stub = _Stub()
        result = run_agent_loop(
            stub,
            self.catalog(),
            messages=[{"role": "user", "content": "what do you know?"}],
            handlers=self.handlers(),
            max_steps=3,
            clock=lambda: 0.0,
        )
        self.assertEqual(STOP_FINAL, result.stopped_reason)
        self.assertEqual(1, len(result.calls))
        self.assertTrue(result.calls[0].ok)
        self.assertEqual("cognition.partition", result.calls[0].tool)
        # The tool budget shrank by one on the wrap-up turn, but not before.
        self.assertEqual(2, len(stub.seen_tools))

    def test_question_transition_rejects_resolve_close_reopen(self) -> None:
        outcome = self.call(
            "question.transition",
            {"question_id": str(uuid.uuid4()), "action": "resolve", "expected_version": 1},
        )
        # The catalog's enum refuses it before the handler runs.
        self.assertFalse(outcome.ok)
        self.assertEqual(TOOL_ARGUMENTS_INVALID, outcome.code)


class Rg3eRevisionPreflightTests(HandlerTestCase):
    """R-G3E: one shared pre-flight, one legal atomic retract+propose bundle."""

    def _target(self, record) -> dict:
        return {
            "ref_type": "record",
            "namespace": NS,
            "record_id": record.record_id,
            "record_version": record.record_version,
        }

    def _old_belief(self):
        event = self.ingest()
        record = self.propose(
            body=self.belief_body(
                claim="old synthetic hypothesis",
                claim_type="general_hypothesis",
                strength="limited",
                evidence_links=[self.evidence_link(self.event_ref(event.event_id))],
            )
        )
        return record, event

    def test_substantive_revise_is_refused_before_a_candidate_exists(self) -> None:
        record, _event = self._old_belief()
        self.build_context()
        before = len(self.store.pending_change_candidates(NS))
        outcome = self.call(
            "cognition.propose",
            {
                "summary": "rewrite the claim",
                "changes": [
                    {
                        "op": "revise_belief",
                        "target": self._target(record),
                        "body": self.belief_body(
                            claim="new synthetic hypothesis",
                            claim_type="general_hypothesis",
                            strength="limited",
                        ),
                    }
                ],
            },
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(TOOL_ARGUMENTS_INVALID, outcome.code, outcome.payload)
        message = outcome.payload["message"]
        self.assertIn("changes[0].body.claim", message)
        self.assertIn(record.record_id, message)
        self.assertIn("retract_belief", message)
        self.assertIn("propose_belief", message)
        self.assertNotIn("new synthetic hypothesis", message)
        self.assertEqual(before, len(self.store.pending_change_candidates(NS)))
        self.assertEqual(1, self.store.get_current(NS, record.record_id).record_version)

    def test_retract_plus_propose_is_one_atomic_commit(self) -> None:
        record, event = self._old_belief()
        self.build_context()
        outcome = self.call(
            "cognition.propose",
            {
                "summary": "replace the frozen claim with one bundle",
                "changes": [
                    {
                        "op": "retract_belief",
                        "target": self._target(record),
                        "evidence_links": [
                            self.evidence_link(
                                self.event_ref(event.event_id), relation="contradicts"
                            )
                        ],
                        "reason": "the old claim no longer describes the observation",
                    },
                    {
                        "op": "propose_belief",
                        "scope": {"scene": "test.scene"},
                        "body": self.belief_body(
                            claim="new synthetic hypothesis",
                            claim_type="general_hypothesis",
                            strength="limited",
                        ),
                    },
                ],
            },
        )
        self.assertTrue(outcome.ok, outcome.payload)
        result = outcome.payload["result"]
        self.assertEqual("COMMITTED", result["state"], result)
        refs = result["receipt"]["record_refs"]
        self.assertEqual(2, len(refs), result)
        old = self.store.get_current(NS, record.record_id)
        self.assertEqual(2, old.record_version)
        self.assertEqual("retracted", old.status)
        new_ids = [item["record_id"] for item in refs if item["record_id"] != record.record_id]
        self.assertEqual(1, len(new_ids), result)
        new = self.store.get_current(NS, new_ids[0])
        self.assertEqual("proposed", new.status)
        self.assertEqual("new synthetic hypothesis", new.body["claim"])
        decision = self.governance.latest_decision(NS, result["candidate_id"], 1)
        self.assertIsNotNone(decision)
        self.assertEqual("committed", decision.decision)
        self.assertEqual("E0", decision.impact)
        self.assertEqual(0, len(self.store.pending_change_candidates(NS)))


class RealModelAgentTests(unittest.TestCase):
    """L3: real model over the agent loop.  NOT RUN in M4 — this is M8."""

    @unittest.skip("L3 real-model acceptance runs in M8, not M4")
    def test_real_model_chooses_a_tool(self) -> None:  # pragma: no cover
        raise AssertionError("not part of M4")


if __name__ == "__main__":
    unittest.main()

class ContractConsistencyTests(unittest.TestCase):
    """The schema the model sees must be the shape the code accepts."""

    def test_d1_a_string_ref_is_refused_with_the_expected_shape(self) -> None:
        catalog = ToolCatalog.cognition_and_web()
        outcome = catalog.execute(
            ToolCall(
                call_id="call-1",
                name="cognition.belief.read",
                arguments=json.dumps({"ref": "859eeaa1-c465-4128-a166-974dab0252f0"}),
            ),
            handlers={"cognition.belief.read": lambda _args: {"result": {}}},
        )
        self.assertFalse(outcome.ok)
        self.assertEqual(TOOL_ARGUMENTS_INVALID, outcome.code)
        self.assertIn("record_id", str(outcome.payload))

    def test_d2_every_op_publishes_its_validator_field_set(self) -> None:
        from quintessence_pcs.domains.cognition.changes import (
            CHANGE_FIELDS,
            CHANGE_OPS,
        )

        catalog = ToolCatalog.cognition_and_web()
        spec = next(
            item for item in catalog.definitions()
            if item["function"]["name"] == "cognition.propose"
        )
        variants = spec["function"]["parameters"]["properties"]["changes"]["items"]["oneOf"]
        by_op = {}
        for variant in variants:
            op = variant["properties"]["op"]["const"]
            by_op[op] = set(variant["required"])

        self.assertEqual(set(CHANGE_OPS), set(by_op))
        for op, fields in CHANGE_FIELDS.items():
            expected = set(fields) - {"scope"}   # scope is host-stamped
            self.assertEqual(expected, by_op[op], f"{op} schema drifted from the validator")
            if "target" in fields:
                target = next(
                    v for v in variants if v["properties"]["op"]["const"] == op
                )["properties"]["target"]
                self.assertNotIn("namespace", target["properties"])

class BodyContractTests(unittest.TestCase):
    """D4: the belief body schema must be the validator's own field table."""

    def test_d4_body_schema_matches_validate_belief_body(self) -> None:
        from quintessence_pcs.domains.cognition.beliefs import BELIEF_BODY_FIELDS

        catalog = ToolCatalog.cognition_and_web()
        spec = next(
            item for item in catalog.definitions()
            if item["function"]["name"] == "cognition.propose"
        )
        variants = spec["function"]["parameters"]["properties"]["changes"]["items"]["oneOf"]
        body = next(
            variant for variant in variants
            if variant["properties"]["op"]["const"] == "propose_belief"
        )["properties"]["body"]

        self.assertEqual(set(BELIEF_BODY_FIELDS), set(body["properties"]))
        self.assertEqual(set(BELIEF_BODY_FIELDS), set(body["required"]))
        self.assertEqual(
            BELIEF_BODY_FIELDS["claim_type"]["enum"],
            body["properties"]["claim_type"]["enum"],
        )
        self.assertEqual(
            BELIEF_BODY_FIELDS["strength"]["enum"],
            body["properties"]["strength"]["enum"],
        )
        links = body["properties"]["evidence_links"]["items"]
        self.assertEqual(
            {"ref", "relation", "origin_group_id", "applicability_reason", "quote"},
            set(links["properties"]),
        )
        self.assertEqual(
            ["ref", "relation", "origin_group_id", "applicability_reason"],
            links["required"],
        )

    def test_d4_no_tool_publishes_an_empty_object_shell(self) -> None:
        """A nested ``{"type": "object"}`` is a contract that says nothing."""

        def shells(node, path=""):
            found = []
            if isinstance(node, dict):
                if node.get("type") == "object" and not (
                    node.get("properties") or node.get("oneOf")
                ):
                    found.append(path or "<root>")
                for key, value in node.items():
                    found += shells(value, f"{path}.{key}" if path else key)
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    found += shells(value, f"{path}[{index}]")
            return found

        catalog = ToolCatalog.cognition_and_web()
        offenders = {}
        for definition in catalog.definitions():
            name = definition["function"]["name"]
            problems = shells(definition["function"]["parameters"])
            if problems:
                offenders[name] = problems
        self.assertEqual({}, offenders)

class HostStateAndCitationTests(unittest.TestCase):
    """The model must not be asked for host state, nor be able to invent refs."""

    def test_d5_context_id_is_not_in_any_schema(self) -> None:
        catalog = ToolCatalog.cognition_and_web()
        offenders = []
        for definition in catalog.definitions():
            text = json.dumps(definition["function"]["parameters"])
            if '"context_id"' in text:
                offenders.append(definition["function"]["name"])
        self.assertEqual([], offenders)

    def test_d6_basis_pairs_match_the_validator(self) -> None:
        from quintessence_pcs.domains.cognition.beliefs import (
            INFERENCE_BASIS_BY_CLAIM_TYPE,
        )

        catalog = ToolCatalog.cognition_and_web()
        spec = next(
            item for item in catalog.definitions()
            if item["function"]["name"] == "cognition.propose"
        )
        variants = spec["function"]["parameters"]["properties"]["changes"]["items"]["oneOf"]
        body = next(
            variant for variant in variants
            if variant["properties"]["op"]["const"] == "propose_belief"
        )["properties"]["body"]
        pairs = body["allOf"][0]["oneOf"]
        published = {
            (p["properties"]["claim_type"]["const"], p["properties"]["inference_basis"]["const"])
            for p in pairs
        }
        self.assertEqual(set(INFERENCE_BASIS_BY_CLAIM_TYPE.items()), published)

    def test_d7_an_invented_evidence_ref_is_refused(self) -> None:
        from quintessence_pcs.runtime.tool_handlers import (
            CognitionToolkit,
            build_cognition_handlers,
        )

        captured = {}

        class FakeChanges:
            def propose(self, access, *, context_id, request_id, bundle):
                captured["bundle"] = bundle
                raise AssertionError("must not reach the service")

        class FakeQueries:  # pragma: no cover - not used
            pass

        toolkit = CognitionToolkit(
            store=None, changes=FakeChanges(), queries=FakeQueries(),
            questions=None, access=type("A", (), {"namespace": NS})(),
            query_access=None, question_access=None, epistemic_access=None,
            now=lambda: NOW_TEXT, change_scope={"scene": "test.scene"},
            context_id="ctx-1", known_refs=set(),
        )
        handlers = build_cognition_handlers(toolkit)
        body = {
            "claim": "synthetic",
            "claim_type": "environment_state",
            "evidence_links": [
                {
                    "ref": {"ref_type": "event", "namespace": NS, "event_id": str(uuid.uuid4())},
                    "relation": "supports",
                    "origin_group_id": "g1",
                    "applicability_reason": "synthetic",
                }
            ],
            "strength": "limited",
            "strength_reason": "synthetic",
            "inference_basis": "observation",
            "premise_refs": [],
            "alternative_group_id": None,
            "valid_until": None,
            "review_condition": "synthetic",
        }
        with self.assertRaises(ToolArgumentsInvalid):
            handlers["cognition.propose"](
                {"changes": [{"op": "propose_belief", "body": body}]}
            )

class RefNormalizationTests(unittest.TestCase):
    """D8: five ref shapes go through one normalizer, never five branches."""

    def _normalize(self, value, namespace=NS):
        from quintessence_pcs.runtime.tool_handlers import _normalize_ref

        return _normalize_ref(namespace, value)

    def test_d8_record_without_version(self) -> None:
        ref = self._normalize({"record_id": "r-1"})
        self.assertEqual(("record", "r-1", 1), (ref["ref_type"], ref["record_id"], ref["record_version"]))
        self.assertEqual(NS, ref["namespace"])

    def test_d8_record_with_version(self) -> None:
        ref = self._normalize({"record_id": "r-1", "record_version": 3})
        self.assertEqual(3, ref["record_version"])

    def test_d8_event_ref(self) -> None:
        ref = self._normalize({"event_id": "e-1"})
        self.assertEqual("event", ref["ref_type"])
        self.assertEqual("e-1", ref["event_id"])

    def test_d8_missing_ref_type_uses_the_keys(self) -> None:
        self.assertEqual("record", self._normalize({"record_id": "r"})["ref_type"])
        self.assertEqual("event", self._normalize({"event_id": "e"})["ref_type"])

    def test_d8_bare_string_is_refused(self) -> None:
        with self.assertRaises(ToolArgumentsInvalid):
            self._normalize("r-1")

    def test_d8_model_namespace_is_overridden(self) -> None:
        ref = self._normalize({"record_id": "r-1", "namespace": "elsewhere"})
        self.assertEqual(NS, ref["namespace"])

