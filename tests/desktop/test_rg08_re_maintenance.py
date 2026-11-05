"""RG08 R-E: the bounded maintenance review path, in-process, with real stores.

The chat model is the existing scripted transport; the maintenance/review model
is the same manifest over the legacy protocol and a configured ``wait`` script.
The bridge projection is seeded with a stub adapter exactly as the J09 tests do:
``sync`` persists the real Event/outbox and ``deliver_page`` moves the outbox to
``applied`` without any external capture.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
for _path in (REPO_ROOT / "src", REPO_ROOT / "apps" / "desktop" / "sidecar"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from tests.desktop import support  # noqa: E402

from host_config import load_config  # noqa: E402
from quintessence_pcs.domains.cognition import BeliefService  # noqa: E402
from quintessence_pcs.domains.memory.experience_bridge import (  # noqa: E402
    BridgeConfig,
    ExperienceBridge,
)
from quintessence_pcs.domains.memory.maintenance_material import (  # noqa: E402
    load_event_material,
    load_turn_material,
)
from quintessence_pcs.domains.memory.maintenance_source import SourceMaterial  # noqa: E402
from quintessence_pcs.foundation.errors import InvalidInput  # noqa: E402
from quintessence_pcs.foundation.jsonutil import canonical_json_text, sha256_hex  # noqa: E402
from quintessence_pcs.runtime.maintenance_cognition import (  # noqa: E402
    MaintenanceCognitionWorker,
    _host_bundle,
    _maintenance_task,
    _prefill_origin_groups,
)
from quintessence_pcs.runtime.model_port import ModelCancelled  # noqa: E402
from quintessence_pcs.runtime.proposal_generation import (  # noqa: E402
    parse_proposal,
    validate_proposal,
)
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402
from quintessence_pcs.store.governance import (  # noqa: E402
    GovernanceAccess,
    GovernanceStore,
)

NS = support.NAMESPACE

#: R-G3C: the two public R-G3B maintenance responses, byte-for-byte (only the
#: hidden reasoning was never copied).  The first put the material EventRef in
#: ``premise_refs``; the one repair deleted the field instead.
RG3C_NAMESPACE = "example.rg08.g3b"
RG3C_EVENT_ID = "f601a9d7-d797-4c43-ab5a-6c2435474791"
RG3C_CAPABILITY_RECORD_ID = "ba4fe2ef-ca21-5ce7-acd0-05c47edacfa7"
RG3C_ORIGINAL_PROPOSAL = '{"operation":"propose_belief","body":{"claim":"阳台这盆薄荷把浇水从每天一次改为每两天一次后，叶片发蔫情况可能有所改善，但同期降温也可能影响状态，尚不能确定浇水频率的独立作用。","claim_type":"general_hypothesis","evidence_links":[{"ref":{"event_id":"f601a9d7-d797-4c43-ab5a-6c2435474791","namespace":"example.rg08.g3b","ref_type":"event"},"relation":"supports","origin_group_id":"event:f601a9d7-d797-4c43-ab5a-6c2435474791","applicability_reason":"用户描述调整浇水频率后第三天薄荷看起来更精神，同时提到同期降温，支持一个带混杂因素的暂定观察。"}],"strength":"limited","strength_reason":"仅有一次观察，且浇水频率变化与降温同时发生，无法分离各自影响；只能支持范围有限、需继续观察的假设。","inference_basis":"inductive","premise_refs":[{"event_id":"f601a9d7-d797-4c43-ab5a-6c2435474791","namespace":"example.rg08.g3b","ref_type":"event"}],"alternative_group_id":null,"valid_until":null,"review_condition":"若后续继续记录浇水频率、叶片状态和温度变化，可复查该暂定判断。"},"scope":{}}'
RG3C_ORIGINAL_REPAIR = '{"operation":"propose_belief","body":{"claim":"阳台这盆薄荷把浇水从每天一次改为每两天一次后，叶片发蔫情况可能有所改善，但同期降温也可能影响状态，尚不能确定浇水频率的独立作用。","claim_type":"general_hypothesis","evidence_links":[{"ref":{"event_id":"f601a9d7-d797-4c43-ab5a-6c2435474791","namespace":"example.rg08.g3b","ref_type":"event"},"relation":"supports","origin_group_id":"event:f601a9d7-d797-4c43-ab5a-6c2435474791","applicability_reason":"用户描述调整浇水频率后第三天薄荷看起来更精神，同时提到同期降温，支持一个带混杂因素的暂定观察。"}],"strength":"limited","strength_reason":"仅有一次观察，且浇水频率变化与降温同时发生，无法分离各自影响；只能支持范围有限、需继续观察的假设。","inference_basis":"inductive","alternative_group_id":null,"valid_until":null,"review_condition":"若后续继续记录浇水频率、叶片状态和温度变化，可复查该暂定判断。"},"scope":{}}'

#: RG09 P1: the archived first maintenance raw response, 853 characters, with
#: both evidence links omitting ``origin_group_id`` -- exactly the shape the
#: maintenance prompt permits and the frozen body validator used to refuse.
#: Copied from the private P1 evidence; the one deliberate change is the
#: private namespace replaced by the equal-length public ``example.rg09.p1``
#: (the repository's public-source gate forbids the private instance name).
#: Nothing else differs, and the sanitised sha256 is pinned below.
RG09_P1_NAMESPACE = "example.rg09.p1"
RG09_P1_EVENT_IDS = (
    "3a7fda79-cf3f-4981-9716-dd4178cb2197",
    "4cad78dc-9cbc-4612-8c6b-f58dc345cb51",
)
RG09_P1_RAW_SHA256 = (
    "31995302aae2d4ddbd36772a39f3f865a2e79a0069c565b5fe7ecc38da19f63e"
)
RG09_P1_RAW = """{"operation":"propose_belief","body":{"claim":"在这些连续的低压力闲聊里，用户有时会以“只是想和你说说话”来表达开启交谈的意图；这类交谈本身可能就是他当下想要的东西，而不一定包含待解决的事项。","claim_type":"general_hypothesis","evidence_links":[{"ref":{"event_id":"3a7fda79-cf3f-4981-9716-dd4178cb2197","namespace":"example.rg09.p1","ref_type":"event"},"relation":"supports","applicability_reason":"用户明确说“其实也没有，就是想和你说说话”，直接支持当下交谈本身即是目的、未必另有事项。"},{"ref":{"event_id":"4cad78dc-9cbc-4612-8c6b-f58dc345cb51","namespace":"example.rg09.p1","ref_type":"event"},"relation":"supports","applicability_reason":"用户此前说“今天有点累，但和你说说话挺好的”，支持交谈本身带来正面感受，而非只是在引出待解决的问题。"}],"strength":"limited","strength_reason":"依据同一天内两次用户自述，可支持此场景下“交谈本身即目的”的倾向；但样本少且都来自用户自述，不足以推断稳定偏好或跨情境规律。","inference_basis":"inductive","premise_refs":[],"alternative_group_id":null,"valid_until":null,"review_condition":"若后续出现用户明确要求解决具体问题，或表示只是想独处、不想闲聊的材料时复查。"},"scope":{}}"""


def _rg09_p1_raw() -> str:
    return RG09_P1_RAW


class _StubAdapter:
    """The bridge only needs an adapter for ``deliver``; ``sync`` never calls it."""

    def __init__(self, *, ack: bool = False) -> None:
        self._ack = ack

    def capture(self, event, *, actor_origin, timeout_seconds):
        if not self._ack:
            raise AssertionError("sync must not reach the delivery adapter")
        return {
            "kind": "ok",
            "receipt": {
                "disposition": "inserted",
                "durability": "persisted",
                "event_refs": [
                    {"ref": f"event:rg08-re:{event['source_event_key'][:40]}", "revision": 1}
                ],
            },
        }


def _experience_access() -> ExperienceAccess:
    return ExperienceAccess(
        namespace=NS,
        host_instance=support.HOST_INSTANCE,
        access_ref=support.ACCESS_REF,
        privacy_classes=frozenset({"private"}),
        purposes=frozenset({"memory.bridge"}),
    )


class _FakeProposalModel:
    """A minimal legacy-shaped model for the governance half of the test.

    It does not replace the real ``DirectHttpModel`` send-basis lock (that is
    covered by ``test_rg08_re_model_lock``); it returns one proposal built from
    the event id the test already owns, so the adapter/Review/Tx B path can be
    exercised deterministically.
    """

    adapter_kind = "direct-http"
    model_ref = "synthetic:rg08-r-e"
    policy_version = "synthetic"

    def __init__(
        self,
        event_id: str,
        *,
        mode: str = "propose",
        target: dict[str, Any] | None = None,
        origin_group: str | None = "canonical",
    ) -> None:
        self.event_id = event_id
        self.mode = mode
        self.target = target
        #: ``"canonical"`` emits the store-derived ``event:<id>``; ``None``
        #: omits the field entirely (the shape the real R-F model produced);
        #: any other string is the model's own declaration (host overwrites it).
        self.origin_group = origin_group
        self.calls = 0

    def preflight(self, *, namespace: str) -> None:
        del namespace

    def bind_run(self, *, namespace: str, context):
        del namespace, context
        from contextlib import contextmanager

        @contextmanager
        def scope():
            yield

        return scope()

    def _proposal(self) -> dict[str, Any]:
        link = {
            "ref": {
                "ref_type": "event",
                "namespace": NS,
                "event_id": self.event_id,
            },
            "relation": "supports",
            "origin_group_id": f"event:{self.event_id}",
            "applicability_reason": "synthetic R-E maintenance material",
        }
        if self.origin_group is None:
            link.pop("origin_group_id")
        elif self.origin_group != "canonical":
            link["origin_group_id"] = self.origin_group
        links = [link]
        claim_type = "general_hypothesis"
        strength = "limited"
        inference = "inductive"
        if self.mode == "contested":
            links.append(
                {
                    **link,
                    "relation": "contradicts",
                    "applicability_reason": "synthetic R-E counter-material",
                }
            )
        elif self.mode == "moderate":
            strength = "moderate"
        elif self.mode == "user_preference":
            claim_type = "user_preference"
            inference = "direct_report"
        elif self.mode == "contradicts":
            links = [{**link, "relation": "contradicts"}]
        claim = (
            "prior synthetic hypothesis"
            if self.mode in ("revise", "contradicts")
            else "the synthetic shelf may hold a blue box"
        )
        body = {
            "claim": claim,
            "claim_type": claim_type,
            "evidence_links": links,
            "strength": strength,
            "strength_reason": "one synthetic maintenance material event",
            "inference_basis": inference,
            "premise_refs": [],
            "alternative_group_id": None,
            "valid_until": None,
            "review_condition": "review when the synthetic shelf material changes",
        }
        if self.mode == "retract_propose":
            replacement = {
                **body,
                "claim": "replacement synthetic hypothesis",
                "evidence_links": [link],
            }
            return {
                "operation": "change_bundle",
                "summary": "synthetic replacement bundle",
                "changes": [
                    {
                        "op": "retract_belief",
                        "target": self.target,
                        "evidence_links": [
                            {**link, "relation": "contradicts"}
                        ],
                        "reason": "synthetic replacement of the prior hypothesis",
                    },
                    {
                        "op": "propose_belief",
                        "scope": {"scene": "lab.synthetic"},
                        "body": replacement,
                    },
                ],
            }
        proposal: dict[str, Any] = {
            "operation": "propose_belief",
            "body": body,
            "scope": {"scene": "lab.synthetic"},
        }
        if self.mode in ("revise", "contradicts"):
            proposal["operation"] = "revise_belief"
            proposal["target"] = self.target
        return proposal

    def propose(self, *, rendered_input, references, feedback=None):
        del references, feedback
        assert '"maintenance_task"' in rendered_input
        self.calls += 1
        if self.mode == "invalid_then_valid" and self.calls == 1:
            return "not strict json"
        if self.mode == "wait":
            return json.dumps({"operation": "wait", "reason": "fixture_no_change"})
        return json.dumps(self._proposal(), ensure_ascii=False, separators=(",", ":"))


class _BlockingModel:
    """A maintenance model that waits until the attempt's cancel event fires.

    It is deliberately *not* a transport: it reproduces the worker boundary so
    the test can measure how fast ``run_slice`` releases the worker after a
    reader-thread cancellation.  ``late`` lets the test exercise a result that
    arrives after the cancel decision and must be discarded.
    """

    adapter_kind = "direct-http"
    model_ref = "synthetic:rg08-cancel"
    policy_version = "synthetic"

    def __init__(self, *, late: bool = False) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.late = late
        self.calls = 0
        self._event = None

    def preflight(self, *, namespace: str) -> None:
        del namespace

    @contextmanager
    def bind_cancel(self, event):
        self._event = event
        try:
            yield
        finally:
            self._event = None

    def bind_run(self, *, namespace, context):
        del namespace, context
        return nullcontext()

    def propose(self, *, rendered_input, references, feedback=None):
        del rendered_input, references, feedback
        self.calls += 1
        self.started.set()
        while not self.release.wait(0.02):
            if not self.late and self._event is not None and self._event.is_set():
                raise ModelCancelled("synthetic cancellation")
        return '{"operation":"wait","reason":"synthetic_cancel"}'


class MaintenanceRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="rg08-re-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.db_path = self.tmp_path / "host.sqlite"
        config = support.config_document(
            self.db_path,
            purposes=["consult", "reasoning", "review", "memory.bridge"],
            script=[
                {"action": "none", "reply": "synthetic maintenance lab answer one"},
                {"action": "none", "reply": "synthetic maintenance lab answer two"},
                {"action": "none", "reply": "synthetic maintenance lab answer three"},
                {"action": "none", "reply": "synthetic maintenance lab answer four"},
                {"action": "none", "reply": "synthetic maintenance lab answer five"},
                {"action": "none", "reply": "synthetic maintenance lab answer six"},
            ],
            maintenance={"enabled": True, "permissions": ["maintenance"]},
            review_script=[{"operation": "wait", "reason": "fixture_no_change"}],
            query_plan={
                "kind": "belief_current",
                "claim_types": ["capability", "general_hypothesis"],
                "scope": {"scene": "lab.synthetic"},
                "limit": 5,
            },
            memory_bridge={
                "enabled": True,
                "permissions": ["memory_bridge"],
                "python_executable": sys.executable,
                "entry": str(self.tmp_path / "fake_cli.py"),
                "package_path": str(self.tmp_path),
                "data_directory": str(self.tmp_path / "memory"),
                "agent_id": "agent:rg08-r-e",
                "installation_id": "install:rg08-r-e",
                "scope_id": "scope-rg08-r-e",
                "session_id": "session-rg08-r-e",
            },
        )
        config["cognition"] = {"enabled": True, "propose": True}
        config["budget"]["pcs_context_max_bytes"] = 65536
        self.config_path = support.write_config(self.tmp_path, config)
        self.config = load_config(self.config_path)
        import runtime as runtime_module

        self.runtime_module = runtime_module
        self.runtime = runtime_module.DesktopRuntime(self.config)
        self.addCleanup(self.runtime.close)
        self.runtime.experience_bridge = ExperienceBridge(
            self.runtime.store,
            _StubAdapter(ack=True),
            BridgeConfig(scope_id="scope-rg08-r-e", max_batch=8),
        )
        self.conversation = self.runtime.conversation_create()
        self.conversation, created = self.runtime.conversation_send(
            conversation_id=self.conversation.conversation_id,
            client_request_id="rg08-re-turn-1",
            text="the synthetic shelf holds one blue box",
        )
        self.turn = created.turn
        self.execution = self.runtime.conversation_execute(
            conversation=self.conversation, creation=created
        )
        self.assertEqual(self.execution.turn.state, "completed")
        self.runtime.experience_bridge.sync(
            _experience_access(), namespace=NS, limit=10
        )
        delivery = self.runtime.experience_bridge.deliver(
            _experience_access(), namespace=NS, limit=10, capture_timeout_seconds=2.0
        )
        self.assertGreaterEqual(delivery.applied, 1, delivery)
        self.event_id = self._input_event_id()
        self.baseline_beliefs = self.runtime.store.connection.execute(
            "SELECT COUNT(*) FROM record_versions WHERE namespace = ? "
            "AND kind = 'belief'",
            (NS,),
        ).fetchone()[0]

    def _add_turn(self, text: str, request_id: str):
        conversation, created = self.runtime.conversation_send(
            conversation_id=self.conversation.conversation_id,
            client_request_id=request_id,
            text=text,
        )
        self.conversation = conversation
        execution = self.runtime.conversation_execute(
            conversation=conversation, creation=created
        )
        self.assertEqual(execution.turn.state, "completed")
        self.runtime.experience_bridge.sync(
            _experience_access(), namespace=NS, limit=10
        )
        delivery = self.runtime.experience_bridge.deliver(
            _experience_access(), namespace=NS, limit=10, capture_timeout_seconds=2.0
        )
        self.assertGreaterEqual(delivery.applied, 1, delivery)
        return execution.turn

    def _input_event_id(self) -> str:
        row = self.runtime.store.connection.execute(
            "SELECT sm.event_id FROM experience_source_mapping sm "
            "JOIN collab_inputs i ON i.namespace = sm.namespace "
            "  AND i.input_id = sm.ref_id "
            "WHERE sm.namespace = ? AND sm.ref_type = 'collab_input' "
            "ORDER BY sm.created_at DESC LIMIT 1",
            (NS,),
        ).fetchone()
        self.assertIsNotNone(row)
        return str(row["event_id"])

    def _rewrite_payload(self, payload_ref: str, payload: Any) -> None:
        text = canonical_json_text(payload)
        self.runtime.store.connection.execute(
            "UPDATE private_payloads SET body_json = ?, sha256 = ? WHERE payload_ref = ?",
            (text, sha256_hex(text), payload_ref),
        )
        self.runtime.store.connection.commit()

    def _event_payload(self) -> tuple[str, dict[str, Any]]:
        row = self.runtime.store.connection.execute(
            "SELECT payload_ref FROM event_journal WHERE namespace = ? AND event_id = ?",
            (NS, self.event_id),
        ).fetchone()
        self.assertIsNotNone(row)
        payload = self.runtime.store.get_event(NS, self.event_id).payload
        return str(row["payload_ref"]), dict(payload)

    def _input_body_payload(self) -> str:
        row = self.runtime.store.connection.execute(
            "SELECT body_payload_ref FROM collab_inputs WHERE namespace = ? "
            "AND input_id = ? ORDER BY content_version DESC LIMIT 1",
            (NS, self._input_id()),
        ).fetchone()
        self.assertIsNotNone(row)
        return str(row["body_payload_ref"])

    def _belief_count(self) -> int:
        return int(
            self.runtime.store.connection.execute(
                "SELECT COUNT(*) FROM record_versions WHERE namespace = ? AND kind = 'belief'",
                (NS,),
            ).fetchone()[0]
        )

    def _prior_hypothesis(self, claim: str = "prior synthetic hypothesis") -> Any:
        body = {
            "claim": claim,
            "claim_type": "general_hypothesis",
            "evidence_links": [],
            "strength": "limited",
            "strength_reason": "synthetic prior fixture",
            "inference_basis": "inductive",
            "premise_refs": [],
            "alternative_group_id": None,
            "valid_until": None,
            "review_condition": "review when new material arrives",
        }
        review_context = self.runtime.contexts.build_context(
            namespace=NS,
            trigger_ref={
                "ref_type": "event",
                "namespace": NS,
                "event_id": self.event_id,
            },
            adapter_kind="direct-http",
        )
        return BeliefService(self.runtime.store).propose_belief(
            namespace=NS,
            context_id=review_context.context_id,
            request_id=str(uuid.uuid4()),
            body=body,
            scope={"scene": "lab.synthetic"},
        )

    def _current_belief_by_claim(self, claim: str) -> Any:
        rows = self.runtime.store.connection.execute(
            "SELECT r.record_id FROM records r "
            "JOIN record_versions rv ON rv.namespace = r.namespace "
            "  AND rv.record_id = r.record_id AND rv.record_version = r.current_version "
            "JOIN private_payloads p ON p.payload_ref = rv.body_payload_ref "
            "WHERE r.namespace = ? AND r.kind = 'belief' "
            "AND p.body_json LIKE ?",
            (NS, f"%{claim}%"),
        ).fetchall()
        self.assertTrue(rows)
        return self.runtime.store.get_current(NS, str(rows[-1]["record_id"]))

    def test_retract_plus_propose_is_one_auto_committed_bundle(self) -> None:
        prior = self._prior_hypothesis()
        self.runtime.maintenance_model = _FakeProposalModel(
            self.event_id,
            mode="retract_propose",
            target={
                "ref_type": "record",
                "namespace": NS,
                "record_id": prior.record_id,
                "record_version": prior.record_version,
            },
        )
        result = self.runtime.maintenance_after_turn(self.turn.turn_id)
        self.assertEqual(result["status"], "ran", result)
        self.assertEqual(result["state"], "succeeded", result)
        item = self.runtime.store.connection.execute(
            "SELECT outcome, candidate_id, receipt_ref FROM maintenance_items "
            "ORDER BY item_no DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(item["outcome"], "committed", dict(item))
        self.assertIsNotNone(item["candidate_id"])
        self.assertIsNotNone(item["receipt_ref"])
        old = self.runtime.store.get_current(NS, prior.record_id)
        self.assertEqual(old.status, "retracted")
        self.assertEqual(
            [int(row["record_version"]) for row in self.runtime.store.connection.execute(
                "SELECT record_version FROM record_versions WHERE namespace = ? "
                "AND record_id = ? ORDER BY record_version",
                (NS, prior.record_id),
            )],
            [1, 2],
        )
        replacement = self.runtime.store.connection.execute(
            "SELECT r.record_id FROM records r "
            "JOIN record_versions rv ON rv.namespace = r.namespace "
            "  AND rv.record_id = r.record_id AND rv.record_version = r.current_version "
            "JOIN private_payloads p ON p.payload_ref = rv.body_payload_ref "
            "WHERE r.namespace = ? AND r.kind = 'belief' "
            "AND p.body_json LIKE '%replacement synthetic hypothesis%'",
            (NS,),
        ).fetchall()
        self.assertTrue(replacement)
        new = self.runtime.store.get_current(NS, str(replacement[-1]["record_id"]))
        self.assertEqual(new.body["claim"], "replacement synthetic hypothesis")
        self.assertEqual(new.status, "proposed")
        proposal = self.runtime.store.connection.execute(
            "SELECT p.body_json FROM collab_artifacts a "
            "JOIN private_payloads p ON p.payload_ref = a.proposal_payload_ref "
            "WHERE a.artifact_id = ?",
            (item["candidate_id"],),
        ).fetchone()
        self.assertIsNotNone(proposal)
        wrapper = json.loads(proposal["body_json"])
        inner = json.loads(wrapper["proposal"]) if "proposal" in wrapper else wrapper
        self.assertEqual(len(inner["changes"]), 2)
        working, _refs = self.runtime._build_cognition_material_factory(
            self.runtime.config
        )()
        provisional = json.loads(working.body).get("provisional", [])
        claims = [item.get("claim") for item in provisional]
        self.assertIn("replacement synthetic hypothesis", claims)
        self.assertNotIn("prior synthetic hypothesis", claims)
        count = self._belief_count()
        replay = self.runtime.maintenance_after_turn(self.turn.turn_id)
        self.assertIn(replay.get("status"), ("ran", "no_material", "paused"), replay)
        self.assertEqual(self._belief_count(), count)
        self.assertEqual(self.runtime.store.get_current(NS, prior.record_id).status, "retracted")

    def test_a_bundle_without_origin_group_is_host_bound_and_committed(self) -> None:
        """The shape the real R-F model produced: no ``origin_group_id`` at all.

        The host binds the store-derived ``event:<material event>``, so the
        domain still accepts only the value it would itself derive.
        """

        prior = self._prior_hypothesis()
        before = self._belief_count()
        self.runtime.maintenance_model = _FakeProposalModel(
            self.event_id,
            mode="retract_propose",
            target={
                "ref_type": "record",
                "namespace": NS,
                "record_id": prior.record_id,
                "record_version": prior.record_version,
            },
            origin_group=None,
        )
        result = self.runtime.maintenance_after_turn(self.turn.turn_id)
        self.assertEqual(result["status"], "ran", result)
        self.assertEqual(result["state"], "succeeded", result)
        item = self.runtime.store.connection.execute(
            "SELECT outcome, detail, candidate_id FROM maintenance_items "
            "ORDER BY item_no DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(item["outcome"], "committed", dict(item))
        self.assertEqual(item["detail"], "committed", dict(item))
        old = self.runtime.store.get_current(NS, prior.record_id)
        self.assertEqual(old.status, "retracted")
        new = self._current_belief_by_claim("replacement synthetic hypothesis")
        self.assertEqual(new.status, "proposed")
        self.assertEqual(
            [link["origin_group_id"] for link in new.body["evidence_links"]],
            [f"event:{self.event_id}"],
        )
        self.assertEqual(self._belief_count(), before + 2)
        replay = self.runtime.maintenance_after_turn(self.turn.turn_id)
        self.assertIn(replay.get("status"), ("ran", "no_material", "paused"), replay)
        self.assertEqual(self._belief_count(), before + 2)

    def test_an_invented_origin_group_is_canonically_bound_and_audited(self) -> None:
        """A model-declared independence group never reaches the domain.

        The host overwrites it with the canonical value and records the
        override as the stable ``group_rebound`` audit token in the item detail;
        the stored proposal/link contains only the canonical value.
        """

        prior = self._prior_hypothesis()
        self.runtime.maintenance_model = _FakeProposalModel(
            self.event_id,
            mode="retract_propose",
            target={
                "ref_type": "record",
                "namespace": NS,
                "record_id": prior.record_id,
                "record_version": prior.record_version,
            },
            origin_group="event:invented",
        )
        result = self.runtime.maintenance_after_turn(self.turn.turn_id)
        self.assertEqual(result["status"], "ran", result)
        self.assertEqual(result["state"], "succeeded", result)
        item = self.runtime.store.connection.execute(
            "SELECT outcome, detail, candidate_id FROM maintenance_items "
            "ORDER BY item_no DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(item["outcome"], "committed", dict(item))
        self.assertEqual(item["detail"], "committed:group_rebound", dict(item))
        new = self._current_belief_by_claim("replacement synthetic hypothesis")
        self.assertEqual(
            [link["origin_group_id"] for link in new.body["evidence_links"]],
            [f"event:{self.event_id}"],
        )
        proposal = self.runtime.store.connection.execute(
            "SELECT p.body_json FROM collab_artifacts a "
            "JOIN private_payloads p ON p.payload_ref = a.proposal_payload_ref "
            "WHERE a.artifact_id = ?",
            (item["candidate_id"],),
        ).fetchone()
        self.assertIsNotNone(proposal)
        self.assertIn(f"event:{self.event_id}", proposal["body_json"])
        self.assertNotIn("event:invented", proposal["body_json"])

    def test_a_proposal_without_origin_group_is_host_bound_on_the_auto_path(self) -> None:
        """RG09 P1: the prompt's permitted omission must reach ``_host_bundle``.

        The automatic after-turn path never repairs (``allow_repair=False``), so
        before the fix this exact shape was recorded as ``failed /
        proposal_invalid`` at the frozen body validator.
        """

        self.runtime.maintenance_model = _FakeProposalModel(
            self.event_id, origin_group=None
        )
        result = self.runtime.maintenance_after_turn(self.turn.turn_id)
        self.assertEqual(result["status"], "ran", result)
        self.assertEqual(result["state"], "succeeded", result)
        item = self.runtime.store.connection.execute(
            "SELECT outcome, detail FROM maintenance_items "
            "ORDER BY item_no DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(item["outcome"], "committed", dict(item))
        self.assertEqual(item["detail"], "committed", dict(item))
        current = self._current_belief_by_claim(
            "the synthetic shelf may hold a blue box"
        )
        self.assertEqual(
            [link["origin_group_id"] for link in current.body["evidence_links"]],
            [f"event:{self.event_id}"],
        )

    def test_a_revision_without_origin_group_is_host_bound_on_the_auto_path(self) -> None:
        prior = self._prior_hypothesis()
        self.runtime.maintenance_model = _FakeProposalModel(
            self.event_id,
            mode="revise",
            target={
                "ref_type": "record",
                "namespace": NS,
                "record_id": prior.record_id,
                "record_version": prior.record_version,
            },
            origin_group=None,
        )
        result = self.runtime.maintenance_after_turn(self.turn.turn_id)
        self.assertEqual(result["status"], "ran", result)
        self.assertEqual(result["state"], "succeeded", result)
        latest = self.runtime.store.get_current(NS, prior.record_id)
        self.assertEqual(latest.record_version, 2)
        self.assertEqual(latest.status, "proposed")
        self.assertEqual(latest.body["claim"], "prior synthetic hypothesis")
        self.assertEqual(
            [link["origin_group_id"] for link in latest.body["evidence_links"]],
            [f"event:{self.event_id}"],
        )

    def test_an_explicit_origin_group_is_rebound_on_the_auto_path(self) -> None:
        # ``_prefill_origin_groups`` must leave a declared value alone; the host
        # still rebinds it and records the stable audit token.
        self.runtime.maintenance_model = _FakeProposalModel(
            self.event_id, origin_group="event:invented"
        )
        result = self.runtime.maintenance_after_turn(self.turn.turn_id)
        self.assertEqual(result["status"], "ran", result)
        self.assertEqual(result["state"], "succeeded", result)
        item = self.runtime.store.connection.execute(
            "SELECT outcome, detail FROM maintenance_items "
            "ORDER BY item_no DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(item["outcome"], "committed", dict(item))
        self.assertEqual(item["detail"], "committed:group_rebound", dict(item))
        current = self._current_belief_by_claim(
            "the synthetic shelf may hold a blue box"
        )
        self.assertEqual(
            [link["origin_group_id"] for link in current.body["evidence_links"]],
            [f"event:{self.event_id}"],
        )

    def test_e2_bundle_does_not_retract_before_human(self) -> None:
        prior = self._prior_hypothesis()
        self.runtime.maintenance_model = _FakeProposalModel(
            self.event_id,
            mode="retract_propose",
            target={
                "ref_type": "record",
                "namespace": NS,
                "record_id": prior.record_id,
                "record_version": prior.record_version,
            },
        )
        governance_access = GovernanceAccess(
            namespace=NS,
            host_instance=support.HOST_INSTANCE,
            operator=support.OPERATOR,
            access_ref=support.ACCESS_REF,
            permissions=frozenset({"governance.admin", "governance.read"}),
        )
        GovernanceStore(self.runtime.store).revoke_commit_authorization(
            governance_access,
            authorization_id=self.runtime._cognition_auto_id,
            reason="synthetic E2 bundle revoked grant",
        )
        result = self.runtime.maintenance_after_turn(self.turn.turn_id)
        self.assertEqual(result["status"], "ran", result)
        item = self.runtime.store.connection.execute(
            "SELECT outcome, candidate_id FROM maintenance_items ORDER BY item_no DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(item["outcome"], "awaiting_human", dict(item))
        self.assertIsNotNone(item["candidate_id"])
        current = self.runtime.store.get_current(NS, prior.record_id)
        self.assertEqual(current.record_version, 1)
        self.assertEqual(current.status, "proposed")
        replacement = self.runtime.store.connection.execute(
            "SELECT COUNT(*) FROM records r WHERE r.namespace = ? AND r.kind = 'belief' "
            "AND r.record_id != ?",
            (NS, prior.record_id),
        ).fetchone()[0]
        self.assertEqual(replacement, 1)  # only the Lab seed capability


    def test_contradicts_only_revision_never_supports_the_opposite(self) -> None:
        prior = self._prior_hypothesis()
        self.runtime.maintenance_model = _FakeProposalModel(
            self.event_id,
            mode="contradicts",
            target={
                "ref_type": "record",
                "namespace": NS,
                "record_id": prior.record_id,
                "record_version": prior.record_version,
            },
        )
        result = self.runtime.maintenance_after_turn(self.turn.turn_id)
        self.assertEqual(result["status"], "ran", result)
        item = self.runtime.store.connection.execute(
            "SELECT outcome, detail FROM maintenance_items ORDER BY item_no DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(item["outcome"], "awaiting_human", dict(item))
        current = self.runtime.store.get_current(NS, prior.record_id)
        self.assertEqual(current.record_version, 1)
        self.assertEqual(current.status, "proposed")
        self.assertEqual(self._belief_count(), self.baseline_beliefs + 1)

    def test_a_moved_target_is_refused_before_a_bundle_candidate(self) -> None:
        prior = self._prior_hypothesis()
        body = {
            "claim": "prior synthetic hypothesis",
            "claim_type": "general_hypothesis",
            "evidence_links": [],
            "strength": "limited",
            "strength_reason": "synthetic moved target",
            "inference_basis": "inductive",
            "premise_refs": [],
            "alternative_group_id": None,
            "valid_until": None,
            "review_condition": "review when new material arrives",
        }
        context = self.runtime.contexts.build_context(
            namespace=NS,
            trigger_ref={
                "ref_type": "event",
                "namespace": NS,
                "event_id": self.event_id,
            },
            adapter_kind="direct-http",
        )
        BeliefService(self.runtime.store).revise_belief(
            namespace=NS,
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            target={
                "ref_type": "record",
                "namespace": NS,
                "record_id": prior.record_id,
                "record_version": prior.record_version,
            },
            body=body,
            scope={"scene": "lab.synthetic"},
        )
        self.runtime.maintenance_model = _FakeProposalModel(
            self.event_id,
            mode="revise",
            target={
                "ref_type": "record",
                "namespace": NS,
                "record_id": prior.record_id,
                "record_version": prior.record_version,
            },
        )
        result = self.runtime.maintenance_after_turn(self.turn.turn_id)
        self.assertIn(result.get("status"), ("no_material", "incomplete", "ran"), result)
        item = self.runtime.store.connection.execute(
            "SELECT outcome, detail FROM maintenance_items ORDER BY item_no DESC LIMIT 1"
        ).fetchone()
        if item is not None:
            self.assertIn(
                item["outcome"],
                ("no_change", "failed", "awaiting_human"),
                dict(item),
            )
        current = self.runtime.store.get_current(NS, prior.record_id)
        self.assertEqual(current.record_version, 2)
        self.assertEqual(current.body["claim"], "prior synthetic hypothesis")


    def test_version_mismatch_is_refused_without_a_candidate(self) -> None:
        payload_ref, payload = self._event_payload()
        payload["experience_ref"]["ref_version"] = 999
        self._rewrite_payload(payload_ref, payload)
        result = self.runtime.maintenance_after_turn(self.turn.turn_id)
        self.assertIn(result.get("status"), ("no_material", "incomplete"), result)
        self.assertEqual(result.get("eligible"), 0)
        self.assertIn("ref_mismatch", result.get("reasons", ()), result)
        self.assertEqual(self._belief_count(), self.baseline_beliefs)

    def test_content_digest_mismatch_is_refused_without_a_candidate(self) -> None:
        payload_ref, payload = self._event_payload()
        payload["content_digest"] = "0" * 64
        self._rewrite_payload(payload_ref, payload)
        result = self.runtime.maintenance_after_turn(self.turn.turn_id)
        self.assertIn(result.get("status"), ("no_material", "incomplete"), result)
        self.assertEqual(result.get("eligible"), 0)
        self.assertIn("digest_mismatch", result.get("reasons", ()), result)
        self.assertEqual(self._belief_count(), self.baseline_beliefs)

    def test_unreadable_privacy_is_refused_without_a_candidate(self) -> None:
        original = self.runtime._maintenance_experience_access

        def restricted() -> ExperienceAccess:
            return ExperienceAccess(
                namespace=NS,
                host_instance=support.HOST_INSTANCE,
                access_ref=support.ACCESS_REF,
                privacy_classes=frozenset(),
                purposes=frozenset({"review", "consult"}),
            )

        self.runtime._maintenance_experience_access = restricted
        try:
            result = self.runtime.maintenance_after_turn(self.turn.turn_id)
        finally:
            self.runtime._maintenance_experience_access = original
        self.assertIn(result.get("status"), ("no_material", "incomplete"), result)
        self.assertEqual(result.get("eligible"), 0)
        self.assertEqual(self._belief_count(), self.baseline_beliefs)

    def test_a_revoked_grant_awaits_human_without_a_belief(self) -> None:
        self.runtime.maintenance_model = _FakeProposalModel(self.event_id)
        governance_access = GovernanceAccess(
            namespace=NS,
            host_instance=support.HOST_INSTANCE,
            operator=support.OPERATOR,
            access_ref=support.ACCESS_REF,
            permissions=frozenset({"governance.admin", "governance.read"}),
        )
        authorization = self.runtime._cognition_auto_id
        self.assertIsNotNone(authorization)
        GovernanceStore(self.runtime.store).revoke_commit_authorization(
            governance_access,
            authorization_id=authorization,
            reason="synthetic R-E revoked grant",
        )
        result = self.runtime.maintenance_after_turn(self.turn.turn_id)
        self.assertEqual(result["status"], "ran", result)
        self.assertEqual(result["state"], "paused", result)
        item = self.runtime.store.connection.execute(
            "SELECT outcome, candidate_id FROM maintenance_items "
            "ORDER BY item_no DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(item["outcome"], "awaiting_human", dict(item))
        self.assertIsNotNone(item["candidate_id"])
        self.assertEqual(self._belief_count(), self.baseline_beliefs)

    def test_candidate_may_cite_a_prior_material_event(self) -> None:
        self._add_turn(
            "the synthetic shelf label says the box is blue",
            "rg08-rg3-prior-cite-2",
        )
        prior_event = self._input_event_id()
        third = self._add_turn(
            "the synthetic shelf still looks the same after a week",
            "rg08-rg3-prior-cite-3",
        )
        self.runtime.maintenance_model = _FakeProposalModel(prior_event)
        result = self.runtime.maintenance_after_turn(third.turn_id)
        self.assertEqual(result["status"], "ran", result)
        self.assertEqual(result["state"], "succeeded", result)
        item = self.runtime.store.connection.execute(
            "SELECT outcome, candidate_id FROM maintenance_items ORDER BY item_no DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(item["outcome"], "committed", dict(item))
        belief = self.runtime.store.connection.execute(
            "SELECT rv.record_id FROM record_versions rv "
            "JOIN private_payloads p ON p.payload_ref = rv.body_payload_ref "
            "WHERE rv.namespace = ? AND rv.kind = 'belief' "
            "AND p.body_json LIKE '%the synthetic shelf may hold a blue box%'",
            (NS,),
        ).fetchone()
        self.assertIsNotNone(belief)
        ev = self.runtime.store.connection.execute(
            "SELECT p.body_json FROM collab_artifacts a "
            "JOIN private_payloads p ON p.payload_ref = a.proposal_payload_ref "
            "WHERE a.artifact_id = ?",
            (item["candidate_id"],),
        ).fetchone()
        self.assertIsNotNone(ev)
        self.assertIn(prior_event, ev["body_json"])

    def test_local_timeout_is_not_reported_as_rate_limit(self) -> None:
        from contextlib import contextmanager
        from quintessence_pcs.runtime.model_port import ModelTransportError

        class TimeoutModel:
            adapter_kind = "direct-http"
            model_ref = "synthetic:timeout"
            policy_version = "synthetic"

            def preflight(self, *, namespace: str) -> None:
                del namespace

            def bind_run(self, *, namespace: str, context):
                del namespace, context

                @contextmanager
                def scope():
                    yield

                return scope()

            def propose(self, *, rendered_input, references, feedback=None):
                del rendered_input, references, feedback
                raise ModelTransportError("fixture timeout", label="TIMEOUT")

        self.runtime.maintenance_model = TimeoutModel()
        result = self.runtime.maintenance_after_turn(self.turn.turn_id)
        self.assertEqual(result["status"], "ran", result)
        self.assertEqual(result["state"], "waiting_dependency", result)
        self.assertEqual(result["stop_reason"], "local_timeout", result)
        attempt = self.runtime.store.connection.execute(
            "SELECT stop_reason, detail FROM maintenance_attempts "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(attempt["stop_reason"], "local_timeout", dict(attempt))
        self.assertIn("local_timeout", attempt["detail"])
        self.assertEqual(self._belief_count(), self.baseline_beliefs)

    def test_observation_with_change_verb_remains_material(self) -> None:
        text = (
            "阳台那盆薄荷最近我把浇水从每天一次改成两天一次。"
            "改之前叶子总发蔫，改完第三天看着精神一些；"
            "不过那几天也正好降温，晚上凉快了不少。"
        )
        turn = self._add_turn(text, "rg08-rg3-change-observation")
        self.runtime.maintenance_model = _FakeProposalModel(
            self.event_id, mode="wait"
        )
        result = self.runtime.maintenance_after_turn(turn.turn_id)
        self.assertEqual(result["status"], "ran", result)
        row = self.runtime.store.connection.execute(
            "SELECT p.body_json FROM context_snapshots cs "
            "JOIN private_payloads p ON p.payload_ref = cs.rendered_input_payload_ref "
            "ORDER BY cs.rowid DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        wrapper = json.loads(row["body_json"])
        document = (
            json.loads(wrapper["rendered_input"])
            if isinstance(wrapper.get("rendered_input"), str)
            else wrapper
        )
        contents = [item["content"] for item in document["maintenance_task"]["material"]]
        self.assertTrue(any("阳台那盆薄荷" in item for item in contents), contents)

    def test_instructions_replace_old_wait_rule_but_keep_protocol(self) -> None:
        from quintessence_pcs.runtime.maintenance_cognition import (
            MAINTENANCE_INSTRUCTIONS,
            USER_CONFIRMED_FORMATION_INSTRUCTIONS,
        )

        self.assertIn(
            "不要求材料已经证明结论成立。单次观察、存在混杂因素或尚未排除其他解释",
            MAINTENANCE_INSTRUCTIONS,
        )
        self.assertIn(
            "不确定性本身不是拒绝持久保存的理由",
            MAINTENANCE_INSTRUCTIONS,
        )
        self.assertIn("general_hypothesis", MAINTENANCE_INSTRUCTIONS)
        self.assertIn("change_bundle", MAINTENANCE_INSTRUCTIONS)
        self.assertIn("user_preference", MAINTENANCE_INSTRUCTIONS)
        self.assertNotIn(
            "提问、引用、假设、第三人称、单次表达偏好都不是可以据此提交的事实",
            MAINTENANCE_INSTRUCTIONS,
        )
        self.assertNotIn(
            "如果材料不足、只有提问/偏好/引用/不确定性太高",
            MAINTENANCE_INSTRUCTIONS,
        )
        self.assertTrue(USER_CONFIRMED_FORMATION_INSTRUCTIONS.startswith("你的任务"))

    def test_prior_turns_render_with_current_primary_and_existing_cognition(self) -> None:
        second = self._add_turn(
            "the synthetic shelf label says the box is blue",
            "rg08-rg3-turn-2",
        )
        third = self._add_turn(
            "the synthetic shelf still looks the same after a week",
            "rg08-rg3-turn-3",
        )
        self.runtime.maintenance_model = _FakeProposalModel(
            self.event_id, mode="wait"
        )
        result = self.runtime.maintenance_after_turn(third.turn_id)
        self.assertEqual(result["status"], "ran", result)
        row = self.runtime.store.connection.execute(
            "SELECT p.body_json FROM context_snapshots cs "
            "JOIN private_payloads p ON p.payload_ref = cs.rendered_input_payload_ref "
            "ORDER BY cs.rowid DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        wrapper = json.loads(row["body_json"])
        document = (
            json.loads(wrapper["rendered_input"])
            if isinstance(wrapper.get("rendered_input"), str)
            else wrapper
        )
        task = document["maintenance_task"]
        material = task["material"]
        self.assertEqual(len(material), 3)
        current_event = self._input_event_id()
        self.assertEqual(task["primary_event_id"], current_event)
        self.assertEqual(material[0]["ref"]["event_id"], current_event)
        prior_flags = [item["from_prior_turn"] for item in material]
        self.assertEqual(prior_flags, [False, True, True])
        self.assertEqual(len({item["origin_group_id"] for item in material}), 3)
        self.assertEqual({item["subject"] for item in material}, {"user"})
        texts = {item["content"] for item in material}
        self.assertIn("the synthetic shelf holds one blue box", texts)
        self.assertIn("the synthetic shelf label says the box is blue", texts)
        self.assertNotIn("synthetic maintenance lab answer one", texts)
        self.assertNotIn("synthetic maintenance lab answer two", texts)
        self.assertIn("cognition", document)
        scan = self.runtime.store.connection.execute(
            "SELECT scan_position FROM maintenance_jobs ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(scan)
        page = json.loads(scan["scan_position"])["event_ids"]
        self.assertEqual(page, [current_event])
        self.assertEqual(self._belief_count(), self.baseline_beliefs)

    def test_overstated_strength_is_refused_before_a_candidate(self) -> None:
        self.runtime.maintenance_model = _FakeProposalModel(
            self.event_id, mode="moderate"
        )
        result = self.runtime.maintenance_after_turn(self.turn.turn_id)
        self.assertEqual(result["status"], "ran", result)
        item = self.runtime.store.connection.execute(
            "SELECT outcome, detail FROM maintenance_items "
            "ORDER BY item_no DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(item["outcome"], "no_change", dict(item))
        self.assertEqual(item["detail"], "insufficient_evidence:strength_not_allowed")

    def test_user_preference_is_not_a_maintenance_claim(self) -> None:
        self.runtime.maintenance_model = _FakeProposalModel(
            self.event_id, mode="user_preference"
        )
        result = self.runtime.maintenance_after_turn(self.turn.turn_id)
        self.assertEqual(result["status"], "ran", result)
        item = self.runtime.store.connection.execute(
            "SELECT outcome, detail FROM maintenance_items "
            "ORDER BY item_no DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(item["outcome"], "no_change", dict(item))
        self.assertEqual(item["detail"], "insufficient_evidence:claim_type_not_allowed")

    def test_foreground_yield_cancels_a_waiting_model_before_propose(self) -> None:
        model = _BlockingModel()
        self.runtime.maintenance_model = model
        before_items = self.runtime.store.connection.execute(
            "SELECT COUNT(*) FROM maintenance_items WHERE outcome IN "
            "('committed', 'no_change')"
        ).fetchone()[0]
        before_candidates = self.runtime.store.connection.execute(
            "SELECT COUNT(*) FROM collab_artifacts"
        ).fetchone()[0]
        cancelled_at: dict[str, float] = {}

        def cancel() -> None:
            self.assertTrue(model.started.wait(2.0))
            cancelled_at["t"] = time.monotonic()
            self.assertTrue(
                self.runtime.request_maintenance_yield("foreground_send")
            )

        thread = threading.Thread(target=cancel)
        thread.start()
        result = self.runtime.maintenance_after_turn(self.turn.turn_id)
        thread.join(2.0)
        elapsed = time.monotonic() - cancelled_at["t"]
        self.assertFalse(thread.is_alive())
        self.assertLess(elapsed, 0.5)
        self.assertEqual(result["state"], "paused", result)
        self.assertEqual(result["stop_reason"], "foreground_cancel", result)
        self.assertEqual(model.calls, 1)
        self.assertEqual(
            before_items,
            self.runtime.store.connection.execute(
                "SELECT COUNT(*) FROM maintenance_items WHERE outcome IN "
                "('committed', 'no_change')"
            ).fetchone()[0],
        )
        self.assertEqual(
            before_candidates,
            self.runtime.store.connection.execute(
                "SELECT COUNT(*) FROM collab_artifacts"
            ).fetchone()[0],
        )
        service = self.runtime.maintenance_status_fast()["service"]
        self.assertFalse(service["running"])
        self.assertTrue(service["paused_foreground"])
        self.assertEqual("foreground_cancel", service["last"]["stop_reason"])
        model.release.set()

    def test_a_paused_foreground_cancel_is_resumable_by_run_once(self) -> None:
        model = _BlockingModel()
        self.runtime.maintenance_model = model

        def cancel() -> None:
            self.assertTrue(model.started.wait(2.0))
            self.assertTrue(
                self.runtime.request_maintenance_yield("foreground_send")
            )

        thread = threading.Thread(target=cancel)
        thread.start()
        cancelled = self.runtime.maintenance_after_turn(self.turn.turn_id)
        thread.join(2.0)
        model.release.set()
        task_id = cancelled.get("task_id")
        self.assertIsInstance(task_id, str)
        self.assertEqual(
            self.runtime._maintenance_store().get(NS, task_id).state, "paused"
        )
        self.runtime.maintenance_model = _FakeProposalModel(
            self.event_id, mode="wait"
        )
        # The UI's single Run once / Continue entry sends no task id; the
        # runtime must find the paused cognition review itself.
        resumed = self.runtime.maintenance_run()
        self.assertEqual(resumed["status"], "ran", resumed)
        self.assertNotEqual(
            self.runtime._maintenance_store().get(NS, task_id).state, "leased"
        )

    def test_late_model_result_after_cancel_is_discarded(self) -> None:
        model = _BlockingModel(late=True)
        self.runtime.maintenance_model = model
        before_candidates = self.runtime.store.connection.execute(
            "SELECT COUNT(*) FROM collab_artifacts"
        ).fetchone()[0]

        def cancel() -> None:
            self.assertTrue(model.started.wait(2.0))
            self.assertTrue(
                self.runtime.request_maintenance_yield("foreground_send")
            )
            time.sleep(0.05)
            model.release.set()

        thread = threading.Thread(target=cancel)
        thread.start()
        result = self.runtime.maintenance_after_turn(self.turn.turn_id)
        thread.join(2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["stop_reason"], "foreground_cancel", result)
        self.assertEqual(
            before_candidates,
            self.runtime.store.connection.execute(
                "SELECT COUNT(*) FROM collab_artifacts"
            ).fetchone()[0],
        )


    def test_automatic_review_does_not_repair_a_malformed_proposal(self) -> None:
        self.runtime.maintenance_model = _FakeProposalModel(
            self.event_id, mode="invalid_then_valid"
        )
        result = self.runtime.maintenance_after_turn(self.turn.turn_id)
        self.assertEqual(result["status"], "ran", result)
        self.assertNotEqual(result["state"], "succeeded", result)
        self.assertEqual(result["stop_reason"], "awaits_confirmation", result)
        self.assertEqual(result["units"], 1)
        self.assertEqual(self.runtime.maintenance_model.calls, 1)
        item = self.runtime.store.connection.execute(
            "SELECT outcome, detail FROM maintenance_items "
            "ORDER BY item_no DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(item["outcome"], "failed", dict(item))
        self.assertEqual(item["detail"], "proposal_invalid")

    def test_the_auto_maintenance_switch_is_reloadable_without_restart(self) -> None:
        from dataclasses import replace

        self.assertTrue(self.runtime.maintenance_configured())
        disabled = replace(
            self.runtime.config,
            maintenance=replace(self.runtime.config.maintenance, enabled=False),
        )
        self.runtime.apply_generation_settings(disabled)
        self.assertFalse(self.runtime.maintenance_configured())
        enabled = replace(
            self.runtime.config,
            maintenance=replace(self.runtime.config.maintenance, enabled=True),
        )
        self.runtime.apply_generation_settings(enabled)
        self.assertTrue(self.runtime.maintenance_configured())

    def test_repeating_the_same_turn_does_not_create_a_second_belief(self) -> None:
        self.runtime.maintenance_model = _FakeProposalModel(self.event_id)
        first = self.runtime.maintenance_after_turn(self.turn.turn_id)
        self.assertEqual(first["state"], "succeeded")
        second = self.runtime.maintenance_after_turn(self.turn.turn_id)
        self.assertIn(second.get("status"), ("waiting_budget", "paused", "ran"), second)
        jobs = self.runtime.store.connection.execute(
            "SELECT COUNT(*) FROM maintenance_jobs"
        ).fetchone()[0]
        beliefs = self.runtime.store.connection.execute(
            "SELECT COUNT(*) FROM record_versions WHERE namespace = ? AND kind = 'belief'",
            (NS,),
        ).fetchone()[0]
        self.assertEqual(jobs, 1)
        self.assertEqual(beliefs, self.baseline_beliefs + 1)

    def test_expression_preference_question_and_third_party_are_material(self) -> None:
        """J11: a mixed message keeps its whole body and only carries marks.

        Dropping the event lost the user's own experience: "我妈妈今天和我聊了
        很久，她说……" is a lived event *and* a report of someone else.  The body
        is kept; only a question mark and a quotation mark are reported as
        structural facts.  Who said what is the model's reading, and the source
        of a candidate is checked by Review -- not by a person-reading lexicon.
        """

        cases = (
            ("以后回答简短一点", {"contains_question": False, "contains_quotation": False}),
            ("你记得我喜欢什么颜色吗？", {"contains_question": True, "contains_quotation": False}),
            # J11: who said what is the model's reading of the body; the host
            # keeps a structural mark only, and the mixed message is still
            # material in full.
            ("我妈妈今天和我聊了很久，她说我应该认真考虑未来。", {"contains_question": False, "contains_quotation": False}),
            ("以后回答简单一些，对了，我今天跑了五公里。", {"contains_question": False, "contains_quotation": False}),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                input_row = self.runtime.store.register_input(
                    namespace=NS,
                    origin_kind="user_statement",
                    body=text,
                    privacy_class="private",
                    retention="standard",
                )
                ref = {
                    "ref_type": "collab_input",
                    "namespace": NS,
                    "input_id": input_row.input_id,
                    "content_version": input_row.content_version,
                }
                self.runtime.store.register_interaction(
                    namespace=NS,
                    mode="consult",
                    activation_kind="user_input",
                    host_instance=support.HOST_INSTANCE,
                    access_ref=support.ACCESS_REF,
                    policy_ref="policy:collab.v1",
                    input_refs=[ref],
                    result="answered",
                )
                self.runtime.experience_bridge.sync(
                    _experience_access(), namespace=NS, limit=10
                )
                self.runtime.experience_bridge.deliver(
                    _experience_access(),
                    namespace=NS,
                    limit=10,
                    capture_timeout_seconds=2.0,
                )
                row = self.runtime.store.connection.execute(
                    "SELECT sm.event_id FROM experience_source_mapping sm "
                    "WHERE sm.namespace = ? AND sm.ref_type = 'collab_input' "
                    "AND sm.ref_id = ? ORDER BY sm.created_at DESC LIMIT 1",
                    (NS, input_row.input_id),
                ).fetchone()
                self.assertIsNotNone(row)
                material = load_event_material(
                    self.runtime.store,
                    self.runtime._maintenance_experience_access(),
                    namespace=NS,
                    event_ids=[row["event_id"]],
                    max_items=1,
                )
                self.assertEqual(
                    tuple(item["reason"] for item in material.dropped), ()
                )
                self.assertEqual(1, len(material.usable_sources))
                source = material.usable_sources[0]
                self.assertTrue(source.usable)
                for flag, value in expected.items():
                    self.assertIs(value, getattr(source, flag), flag)
                # The full body survives, not just the clause that matched.
                self.assertIn(text[:6], source.content or "")

    def test_relevant_prior_provisional_can_be_revised(self) -> None:
        prior_body = {
            "claim": "prior synthetic hypothesis",
            "claim_type": "general_hypothesis",
            "evidence_links": [],
            "strength": "limited",
            "strength_reason": "synthetic prior fixture",
            "inference_basis": "inductive",
            "premise_refs": [],
            "alternative_group_id": None,
            "valid_until": None,
            "review_condition": "review when new material arrives",
        }
        review_context = self.runtime.contexts.build_context(
            namespace=NS,
            trigger_ref={
                "ref_type": "event",
                "namespace": NS,
                "event_id": self.event_id,
            },
            adapter_kind="direct-http",
        )
        prior = BeliefService(self.runtime.store).propose_belief(
            namespace=NS,
            context_id=review_context.context_id,
            request_id=str(uuid.uuid4()),
            body=prior_body,
            scope={"scene": "lab.synthetic"},
        )
        self.runtime.maintenance_model = _FakeProposalModel(
            self.event_id,
            mode="revise",
            target={
                "ref_type": "record",
                "namespace": NS,
                "record_id": prior.record_id,
                "record_version": prior.record_version,
            },
        )
        result = self.runtime.maintenance_after_turn(self.turn.turn_id)
        self.assertEqual(result["status"], "ran", result)
        self.assertEqual(result["state"], "succeeded", result)
        versions = self.runtime.store.connection.execute(
            "SELECT record_version, status FROM record_versions "
            "WHERE namespace = ? AND record_id = ? ORDER BY record_version",
            (NS, prior.record_id),
        ).fetchall()
        self.assertEqual([int(row["record_version"]) for row in versions], [1, 2])
        latest = self.runtime.store.get_current(NS, prior.record_id)
        self.assertEqual(latest.record_version, 2)
        self.assertEqual(latest.status, "proposed")
        self.assertEqual(latest.body["claim"], "prior synthetic hypothesis")
        self.assertTrue(latest.body["evidence_links"])
        working, _refs = self.runtime._build_cognition_material_factory(
            self.runtime.config
        )()
        provisional = json.loads(working.body).get("provisional", [])
        self.assertTrue(
            any(
                item.get("claim") == "prior synthetic hypothesis"
                for item in provisional
            ),
            provisional,
        )

    def test_a_low_impact_hypothesis_is_auto_committed_and_becomes_provisional(self) -> None:
        self.runtime.maintenance_model = _FakeProposalModel(self.event_id)
        result = self.runtime.maintenance_after_turn(self.turn.turn_id)
        self.assertEqual(result["status"], "ran", result)
        self.assertEqual(result["state"], "succeeded", result)
        item = self.runtime.store.connection.execute(
            "SELECT outcome, candidate_id, detail FROM maintenance_items "
            "ORDER BY item_no DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(item["outcome"], "committed", dict(item))
        self.assertIsNotNone(item["candidate_id"])
        row = self.runtime.store.connection.execute(
            "SELECT rv.status, p.body_json FROM record_versions rv "
            "JOIN private_payloads p ON p.payload_ref = rv.body_payload_ref "
            "WHERE rv.namespace = ? AND rv.kind = 'belief' "
            "ORDER BY rv.rowid DESC LIMIT 1",
            (NS,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "proposed")
        body = json.loads(row["body_json"])
        self.assertEqual(body["claim_type"], "general_hypothesis")
        self.assertEqual(body["strength"], "limited")
        working, _refs = self.runtime._build_cognition_material_factory(
            self.runtime.config
        )()
        self.assertIsNotNone(working)
        projection = json.loads(working.body)
        self.assertTrue(
            any(
                item.get("claim_type") == "general_hypothesis"
                and item.get("status") == "proposed"
                for item in projection.get("provisional", ())
            ),
            projection,
        )

    def test_a_valid_turn_creates_a_bounded_review_and_no_change(self) -> None:
        result = self.runtime.maintenance_after_turn(self.turn.turn_id)
        self.assertEqual(result["status"], "ran", result)
        self.assertIn(result["state"], ("succeeded", "paused"), result)
        store = self.runtime.store
        jobs = self.runtime.maintenance_status()["health"]
        self.assertEqual(jobs["by_state"], {"succeeded": 1}, jobs)
        rows = store.connection.execute(
            "SELECT target_kind, outcome, detail FROM maintenance_items"
        ).fetchall()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["target_kind"], "event")
        self.assertEqual(rows[0]["outcome"], "no_change")
        event = store.connection.execute(
            "SELECT kind, payload_json FROM collab_turn_events "
            "WHERE turn_id = ? AND kind = 'maintenance' ORDER BY seq DESC LIMIT 1",
            (self.turn.turn_id,),
        ).fetchone()
        self.assertIsNotNone(event)
        payload = json.loads(event["payload_json"])
        self.assertEqual(payload["status"], "ran")

    def test_pending_delivery_is_an_honest_no_material(self) -> None:
        # A second turn whose projection exists but is not delivered yet.
        created = self.runtime.conversation_send(
            conversation_id=self.conversation.conversation_id,
            client_request_id="rg08-re-turn-2",
            text="another synthetic statement about a blue box",
        )
        _, created = created
        self.runtime.conversation_execute(
            conversation=self.conversation, creation=created
        )
        self.runtime.experience_bridge.sync(
            _experience_access(), namespace=NS, limit=10
        )
        result = self.runtime.maintenance_after_turn(created.turn.turn_id)
        self.assertEqual(result["status"], "incomplete", result)
        self.assertEqual(result["eligible"], 0)
        self.assertIn("pending_delivery", result["reasons"])

    def test_deleted_source_does_not_enter_material(self) -> None:
        self.runtime.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=self._input_id(),
            ref_version=1,
            authorization_ref="authorization:rg08-r-e-delete",
        )
        result = self.runtime.maintenance_after_turn(self.turn.turn_id)
        self.assertIn(result["status"], ("no_material", "incomplete"), result)
        self.assertEqual(result["eligible"], 0)

    def _input_id(self) -> str:
        row = self.runtime.store.connection.execute(
            "SELECT ref_id FROM experience_source_mapping "
            "WHERE namespace = ? AND ref_type = 'collab_input' LIMIT 1",
            (NS,),
        ).fetchone()
        self.assertIsNotNone(row)
        return str(row["ref_id"])

    def test_turn_experience_refs_and_material_builder_agree(self) -> None:
        material = load_turn_material(
            self.runtime.store,
            self.runtime._maintenance_experience_access(),
            namespace=NS,
            turn_id=self.turn.turn_id,
            timezone="Asia/Shanghai",
            max_items=2,
        )
        self.assertEqual(len(material.usable_sources), 1)
        source = material.usable_sources[0]
        self.assertEqual(source.event_id, self.event_id)
        self.assertEqual(source.subject, "user")
        self.assertIsNotNone(source.content)
        self.assertIsNotNone(source.source_ref)
        self.assertTrue(material.material.items[0].visible_ref)


class _Rg3cRecordingModel:
    """Return archived proposal text and record the one repair feedback."""

    adapter_kind = "direct-http"

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.feedbacks: list[str | None] = []
        self.calls = 0

    def bind_run(self, *, namespace, context):
        del namespace, context
        return nullcontext()

    def propose(self, *, rendered_input, references, feedback=None):
        del rendered_input, references
        self.calls += 1
        self.feedbacks.append(feedback)
        return self.outputs.pop(0)


class _Rg3cContext:
    rendered_input = "{}"


def _rg3c_worker(model: _Rg3cRecordingModel) -> MaintenanceCognitionWorker:
    worker = object.__new__(MaintenanceCognitionWorker)
    worker._namespace = RG3C_NAMESPACE
    worker._model = model
    worker._clock = lambda: 0.0
    worker._model_call_cap = 30.0
    worker._model_call_reserve = 1.0
    return worker


def _rg3c_source() -> SourceMaterial:
    return SourceMaterial(
        event_id=RG3C_EVENT_ID,
        source_kind="user_report",
        event_type="unknown",
        observed_at=None,
        payload_digest=None,
        usable=True,
        source_ref={
            "ref_type": "event",
            "namespace": RG3C_NAMESPACE,
            "event_id": RG3C_EVENT_ID,
        },
        content=(
            "阳台那盆薄荷最近我把浇水从每天一次改成两天一次。"
            "改之前叶子总发蔫，改完第三天看着精神一些；"
            "不过那几天也正好降温，晚上凉快了不少。"
        ),
    )


class Rg3cRealProposalReplayTests(unittest.TestCase):
    """Replay the two real R-G3B responses offline; zero model requests.

    The archived public content is preserved exactly.  The "minimal manual
    correction" only sets ``body.premise_refs`` to ``[]``: it proves the legal
    path and must never be reported as a real-model formation success.
    """

    def _proposal(self, raw: str) -> dict[str, Any]:
        proposal = parse_proposal(raw)
        self.assertEqual(proposal["operation"], "propose_belief")
        return proposal

    def test_archived_responses_are_the_public_content(self) -> None:
        self.assertEqual(
            hashlib.sha256(RG3C_ORIGINAL_PROPOSAL.encode("utf-8")).hexdigest(),
            "956c1ed0a1a74b250447dfbf5d12388ce9f45b2bea0838004a3be30eb06fda91",
        )
        self.assertEqual(
            hashlib.sha256(RG3C_ORIGINAL_REPAIR.encode("utf-8")).hexdigest(),
            "b9b0b72257923cc40f3b6e607e4604a2845638876d87f41bc4e53c89e07f9ce7",
        )

    def test_original_proposal_fails_at_the_event_premise_ref(self) -> None:
        proposal = self._proposal(RG3C_ORIGINAL_PROPOSAL)
        self.assertEqual(
            proposal["body"]["premise_refs"],
            [proposal["body"]["evidence_links"][0]["ref"]],
        )
        with self.assertRaises(InvalidInput) as caught:
            validate_proposal(RG3C_NAMESPACE, proposal)
        self.assertEqual(
            str(caught.exception),
            "premise_refs[0] must have exactly ref_type, namespace, "
            "record_id and record_version",
        )

    def test_repair_response_fails_at_the_missing_required_field(self) -> None:
        proposal = self._proposal(RG3C_ORIGINAL_REPAIR)
        self.assertNotIn("premise_refs", proposal["body"])
        with self.assertRaises(InvalidInput) as caught:
            validate_proposal(RG3C_NAMESPACE, proposal)
        self.assertEqual(
            str(caught.exception),
            "belief body is missing required field(s): premise_refs",
        )

    def test_minimal_manual_correction_passes_domain_and_host_policy(self) -> None:
        # The one and only change: required premise_refs gets its legal empty
        # value.  The material EventRef stays in evidence_links[].ref.
        proposal = self._proposal(RG3C_ORIGINAL_PROPOSAL)
        proposal["body"]["premise_refs"] = []
        validate_proposal(RG3C_NAMESPACE, proposal)

        bundle, reason, rebound = _host_bundle(
            None,
            namespace=RG3C_NAMESPACE,
            proposal=proposal,
            scope={"scene": "rg08.g3b"},
            materials=[_rg3c_source()],
        )
        self.assertIsNone(reason)
        self.assertFalse(rebound)
        self.assertIsNotNone(bundle)
        change = bundle["changes"][0]
        self.assertEqual(change["op"], "propose_belief")
        self.assertEqual(change["scope"], {"scene": "rg08.g3b"})
        self.assertEqual(change["body"]["premise_refs"], [])
        self.assertEqual(
            change["body"]["evidence_links"][0]["ref"],
            {
                "ref_type": "event",
                "namespace": RG3C_NAMESPACE,
                "event_id": RG3C_EVENT_ID,
            },
        )
        self.assertEqual(change["body"]["claim"], proposal["body"]["claim"])

    def test_valid_record_premise_is_still_refused_by_maintenance_policy(self) -> None:
        # A syntactically legal RecordRef premise passes the frozen body
        # validator, then the maintenance host still refuses premises.  This
        # is why the model-visible rule is "[]", not "any RecordRef".
        proposal = self._proposal(RG3C_ORIGINAL_PROPOSAL)
        proposal["body"]["premise_refs"] = [
            {
                "ref_type": "record",
                "namespace": RG3C_NAMESPACE,
                "record_id": RG3C_CAPABILITY_RECORD_ID,
                "record_version": 1,
            }
        ]
        validate_proposal(RG3C_NAMESPACE, proposal)
        bundle, reason, rebound = _host_bundle(
            None,
            namespace=RG3C_NAMESPACE,
            proposal=proposal,
            scope={"scene": "rg08.g3b"},
            materials=[_rg3c_source()],
        )
        self.assertIsNone(bundle)
        self.assertEqual(reason, "premises_not_allowed")
        self.assertFalse(rebound)

    def test_maintenance_contract_states_the_empty_premise_rule(self) -> None:
        task = _maintenance_task(
            task_id="rg3c-task",
            trigger_key="turn:rg3c",
            primary_event_id=RG3C_EVENT_ID,
            scope={"scene": "rg08.g3b"},
        )
        contract = task["proposal_contract"]
        self.assertIn("premise_refs", contract["belief_body_fields"])
        note = contract["body_field_notes"]["premise_refs"]
        self.assertIn("required", note)
        self.assertIn("[]", note)
        self.assertIn("evidence_links[].ref", note)
        self.assertIn("EventRef", note)

    def test_repair_loop_still_fails_the_archived_repair_with_actionable_feedback(self) -> None:
        model = _Rg3cRecordingModel([RG3C_ORIGINAL_PROPOSAL, RG3C_ORIGINAL_REPAIR])
        worker = _rg3c_worker(model)
        with self.assertRaises(InvalidInput) as caught:
            worker._generate(_Rg3cContext(), deadline=1000.0)
        self.assertEqual(
            str(caught.exception),
            "belief body is missing required field(s): premise_refs",
        )
        self.assertEqual(model.calls, 2)
        self.assertIsNone(model.feedbacks[0])
        feedback = model.feedbacks[1]
        self.assertIn("Erroneous field: premise_refs", feedback)
        self.assertIn("send []", feedback)
        self.assertIn("must be [] in this maintenance review", feedback)
        self.assertIn("evidence_links[].ref", feedback)

    def test_maintenance_revise_preflight_refuses_a_claim_change(self) -> None:
        # The same shared helper the chat handler calls is exercised here: the
        # maintenance host refuses before ``propose_from_material`` can create
        # a candidate.
        from types import SimpleNamespace

        event_id = "11111111-1111-4111-8111-111111111111"
        current_body = {
            "claim": "old claim",
            "claim_type": "general_hypothesis",
            "evidence_links": [
                {
                    "ref": {
                        "ref_type": "event",
                        "namespace": RG3C_NAMESPACE,
                        "event_id": event_id,
                    },
                    "relation": "supports",
                    "origin_group_id": f"event:{event_id}",
                    "applicability_reason": "the original material",
                }
            ],
            "strength": "insufficient",
            "strength_reason": "one observation",
            "inference_basis": "inductive",
            "premise_refs": [],
            "alternative_group_id": None,
            "valid_until": None,
            "review_condition": "review later",
        }
        from quintessence_pcs.domains.cognition.beliefs import (
            BELIEF_SCHEMA_VERSION,
            BELIEF_KIND,
        )

        version = SimpleNamespace(
            record_id="e16e6c04-64c1-40fb-b9ee-a930164b0e3e",
            record_version=1,
            status="proposed",
            kind=BELIEF_KIND,
            schema_version=BELIEF_SCHEMA_VERSION,
            body=current_body,
        )
        class _Store:
            def get_version(self, namespace, record_id, record_version):
                return version

            def get_current(self, namespace, record_id):
                return version

        material = SourceMaterial(
            event_id="11111111-1111-4111-8111-111111111111",
            source_kind="user_report",
            event_type="unknown",
            observed_at=None,
            payload_digest=None,
            usable=True,
            source_ref={
                "ref_type": "event",
                "namespace": RG3C_NAMESPACE,
                "event_id": "11111111-1111-4111-8111-111111111111",
            },
            content="换过土，叶子还是蔫。",
        )
        def proposal(claim: str) -> dict[str, Any]:
            return {
                "operation": "revise_belief",
                "target": {
                    "ref_type": "record",
                    "namespace": RG3C_NAMESPACE,
                    "record_id": version.record_id,
                    "record_version": 1,
                },
                "body": {
                    **current_body,
                    "claim": claim,
                    "evidence_links": [
                        {
                            "ref": {
                                "ref_type": "event",
                                "namespace": RG3C_NAMESPACE,
                                "event_id": "11111111-1111-4111-8111-111111111111",
                            },
                            "relation": "qualifies",
                            "applicability_reason": "new report qualifies the old claim",
                        }
                    ],
                },
            }

        bundle, reason, _rebound = _host_bundle(
            _Store(),
            namespace=RG3C_NAMESPACE,
            proposal=proposal("new claim"),
            scope={"scene": "rg08.g3d"},
            materials=[material],
        )
        self.assertIsNone(bundle)
        self.assertEqual("claim_text_frozen", reason)
        same, reason, _rebound = _host_bundle(
            _Store(),
            namespace=RG3C_NAMESPACE,
            proposal=proposal("old claim"),
            scope={"scene": "rg08.g3d"},
            materials=[material],
        )
        self.assertIsNone(reason)
        self.assertIsNotNone(same)
        self.assertEqual("revise_belief", same["changes"][0]["op"])

    def test_a_manual_premise_fix_passes_the_same_repair_loop(self) -> None:
        # Deterministic stand-in, not a real-model success: the only repair is
        # the required empty array.  This test proves the repair feedback can
        # yield the legal shape; the real model has not been rerun.
        corrected = json.loads(RG3C_ORIGINAL_PROPOSAL)
        corrected["body"]["premise_refs"] = []
        corrected_raw = json.dumps(
            corrected, ensure_ascii=False, separators=(",", ":")
        )
        model = _Rg3cRecordingModel([RG3C_ORIGINAL_PROPOSAL, corrected_raw])
        worker = _rg3c_worker(model)
        proposal, calls = worker._generate(_Rg3cContext(), deadline=1000.0)
        self.assertEqual(calls, 2)
        self.assertEqual(model.calls, 2)
        self.assertEqual(proposal["body"]["premise_refs"], [])
        self.assertIn("must be [] in this maintenance review", model.feedbacks[1])


def _rg09_worker(
    model: _Rg3cRecordingModel, *, allow_repair: bool = False
) -> MaintenanceCognitionWorker:
    worker = object.__new__(MaintenanceCognitionWorker)
    worker._namespace = RG09_P1_NAMESPACE
    worker._model = model
    worker._clock = lambda: 0.0
    worker._model_call_cap = 30.0
    worker._model_call_reserve = 1.0
    worker._allow_repair = allow_repair
    return worker


def _rg09_material() -> list[SourceMaterial]:
    return [
        SourceMaterial(
            event_id=event_id,
            source_kind="user_report",
            event_type="unknown",
            observed_at=None,
            payload_digest=None,
            usable=True,
            source_ref={
                "ref_type": "event",
                "namespace": RG09_P1_NAMESPACE,
                "event_id": event_id,
            },
            content="historical synthetic maintenance material",
        )
        for event_id in RG09_P1_EVENT_IDS
    ]


def _rg09_store(target: dict[str, Any]):
    """A read-only belief store good enough for ``_host_bundle``'s target."""

    from types import SimpleNamespace

    from quintessence_pcs.domains.cognition.beliefs import (
        BELIEF_KIND,
        BELIEF_SCHEMA_VERSION,
    )

    version = SimpleNamespace(
        record_id=target["record_id"],
        record_version=target["record_version"],
        status="proposed",
        kind=BELIEF_KIND,
        schema_version=BELIEF_SCHEMA_VERSION,
        body={
            "claim": "historical synthetic prior",
            "claim_type": "general_hypothesis",
            "evidence_links": [],
            "strength": "limited",
            "strength_reason": "historical synthetic prior fixture",
            "inference_basis": "inductive",
            "premise_refs": [],
            "alternative_group_id": None,
            "valid_until": None,
            "review_condition": "review when historical material changes",
        },
    )

    class _Store:
        def get_version(self, namespace, record_id, record_version):
            del namespace, record_id, record_version
            return version

        def get_current(self, namespace, record_id):
            del namespace, record_id
            return version

    return _Store()


class Rg09P1OriginGroupBindingTests(unittest.TestCase):
    """RG09 P1: host-bind the canonical group before the frozen validator.

    The maintenance prompt permits omitting ``evidence_links[].origin_group_id``
    (the host binds ``event:<event_id>``); the frozen body validator requires
    the key.  ``_prefill_origin_groups`` closes that gap without relaxing any
    field, and these tests pin the archived raw as the deterministic evidence.
    """

    def test_the_archived_raw_fixture_is_exactly_pinned(self) -> None:
        raw = _rg09_p1_raw()
        self.assertEqual(len(raw), 853)
        self.assertEqual(
            hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            RG09_P1_RAW_SHA256,
        )
        proposal = parse_proposal(raw)
        self.assertEqual(proposal["operation"], "propose_belief")
        self.assertEqual(
            [
                link["ref"]["event_id"]
                for link in proposal["body"]["evidence_links"]
            ],
            list(RG09_P1_EVENT_IDS),
        )
        for link in proposal["body"]["evidence_links"]:
            self.assertNotIn("origin_group_id", link)

    def test_frozen_validation_still_rejects_the_raw_omission(self) -> None:
        # No validator change: the untouched historical raw is still refused,
        # with the exact message the P1 forensic report identified.
        with self.assertRaises(InvalidInput) as caught:
            validate_proposal(RG09_P1_NAMESPACE, parse_proposal(_rg09_p1_raw()))
        self.assertEqual(
            str(caught.exception),
            "evidence_links[0] must have exactly ref, relation, "
            "origin_group_id and applicability_reason, plus an optional quote",
        )

    def test_host_prefill_makes_the_archived_raw_validate(self) -> None:
        proposal = parse_proposal(_rg09_p1_raw())
        self.assertEqual(_prefill_origin_groups(RG09_P1_NAMESPACE, proposal), 2)
        validate_proposal(RG09_P1_NAMESPACE, proposal)
        self.assertEqual(
            [
                link["origin_group_id"]
                for link in proposal["body"]["evidence_links"]
            ],
            [f"event:{event_id}" for event_id in RG09_P1_EVENT_IDS],
        )

    def test_generate_accepts_the_archived_raw_without_a_repair(self) -> None:
        # ``allow_repair=False`` is the automatic after-turn path: before the
        # fix this call raised InvalidInput on the first (and only) output.
        model = _Rg3cRecordingModel([_rg09_p1_raw()])
        worker = _rg09_worker(model, allow_repair=False)
        proposal, calls = worker._generate(_Rg3cContext(), deadline=1000.0)
        self.assertEqual((calls, model.calls), (1, 1))
        self.assertIsNone(model.feedbacks[0])
        self.assertEqual(
            [
                link["origin_group_id"]
                for link in proposal["body"]["evidence_links"]
            ],
            [f"event:{event_id}" for event_id in RG09_P1_EVENT_IDS],
        )

    def test_revise_body_links_are_prefilled_before_validation(self) -> None:
        proposal = parse_proposal(_rg09_p1_raw())
        proposal["operation"] = "revise_belief"
        proposal["target"] = {
            "ref_type": "record",
            "namespace": RG09_P1_NAMESPACE,
            "record_id": "e16e6c04-64c1-40fb-b9ee-a930164b0e3e",
            "record_version": 1,
        }
        self.assertEqual(_prefill_origin_groups(RG09_P1_NAMESPACE, proposal), 2)
        validate_proposal(RG09_P1_NAMESPACE, proposal)
        self.assertEqual(
            [
                link["origin_group_id"]
                for link in proposal["body"]["evidence_links"]
            ],
            [f"event:{event_id}" for event_id in RG09_P1_EVENT_IDS],
        )

    def test_retract_top_level_links_are_prefilled_and_host_bound(self) -> None:
        target = {
            "ref_type": "record",
            "namespace": RG09_P1_NAMESPACE,
            "record_id": "e16e6c04-64c1-40fb-b9ee-a930164b0e3e",
            "record_version": 1,
        }
        proposal = {
            "operation": "retract_belief",
            "target": target,
            "evidence_links": [
                {
                    "ref": {
                        "ref_type": "event",
                        "namespace": RG09_P1_NAMESPACE,
                        "event_id": RG09_P1_EVENT_IDS[0],
                    },
                    "relation": "contradicts",
                    "applicability_reason": "historical synthetic counter-material",
                }
            ],
            "reason": "historical synthetic retraction",
        }
        self.assertEqual(_prefill_origin_groups(RG09_P1_NAMESPACE, proposal), 1)
        validate_proposal(RG09_P1_NAMESPACE, proposal)
        bundle, reason, rebound = _host_bundle(
            _rg09_store(target),
            namespace=RG09_P1_NAMESPACE,
            proposal=proposal,
            scope={},
            materials=_rg09_material()[:1],
        )
        self.assertIsNone(reason)
        self.assertFalse(rebound)
        self.assertIsNotNone(bundle)
        change = bundle["changes"][0]
        self.assertEqual(change["op"], "retract_belief")
        self.assertEqual(
            [link["origin_group_id"] for link in change["evidence_links"]],
            [f"event:{RG09_P1_EVENT_IDS[0]}"],
        )

    def test_an_explicit_origin_group_is_not_overwritten_by_prefill(self) -> None:
        proposal = parse_proposal(_rg09_p1_raw())
        proposal["body"]["evidence_links"][0]["origin_group_id"] = "event:invented"
        # Only the second, still-absent link is filled.
        self.assertEqual(_prefill_origin_groups(RG09_P1_NAMESPACE, proposal), 1)
        self.assertEqual(
            proposal["body"]["evidence_links"][0]["origin_group_id"],
            "event:invented",
        )
        validate_proposal(RG09_P1_NAMESPACE, proposal)
        # The host still performs its own canonical rebind and reports it.
        bundle, reason, rebound = _host_bundle(
            None,
            namespace=RG09_P1_NAMESPACE,
            proposal=proposal,
            scope={},
            materials=_rg09_material(),
        )
        self.assertIsNone(reason)
        self.assertTrue(rebound)
        self.assertIsNotNone(bundle)
        self.assertEqual(
            [
                link["origin_group_id"]
                for link in bundle["changes"][0]["body"]["evidence_links"]
            ],
            [f"event:{event_id}" for event_id in RG09_P1_EVENT_IDS],
        )

    def test_repair_feedback_names_the_missing_origin_group(self) -> None:
        # After prefill, the one remaining way to reach the frozen key message
        # is a link without a bindable EventRef (a record ref here); the repair
        # hint must name the missing field instead of staying generic.
        bad = json.loads(_rg09_p1_raw())
        bad["body"]["evidence_links"][0]["ref"] = {
            "ref_type": "record",
            "namespace": RG09_P1_NAMESPACE,
            "record_id": "e16e6c04-64c1-40fb-b9ee-a930164b0e3e",
            "record_version": 1,
        }
        model = _Rg3cRecordingModel(
            [
                json.dumps(bad, ensure_ascii=False, separators=(",", ":")),
                _rg09_p1_raw(),
            ]
        )
        worker = _rg09_worker(model, allow_repair=True)
        proposal, calls = worker._generate(_Rg3cContext(), deadline=1000.0)
        self.assertEqual((calls, model.calls), (2, 2))
        self.assertIn(
            "Evidence link field(s) missing: origin_group_id",
            model.feedbacks[1],
        )
        self.assertIn("host binds event:<event_id>", model.feedbacks[1])
        self.assertEqual(proposal["operation"], "propose_belief")
        self.assertEqual(
            [
                link["origin_group_id"]
                for link in proposal["body"]["evidence_links"]
            ],
            [f"event:{event_id}" for event_id in RG09_P1_EVENT_IDS],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
