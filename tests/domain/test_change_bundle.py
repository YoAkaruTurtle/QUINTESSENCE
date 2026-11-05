"""J06 S3b: the ``cognition.change`` composition bundle.

These tests drive the bundle directly through ``commit_command``: there is no
service layer yet (that arrives in S7), so the transaction is opened here with
exactly the calls a future service will make.
"""

from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.domains.cognition.context_guard import (  # noqa: E402
    require_live_context,
)
from quintessence_pcs.domains.cognition.changes import (  # noqa: E402
    BUNDLE_OPERATION,
    ChangeApplier,
    ChangeBundleInvalid,
    prepare_change_set,
    validate_change_bundle,
)
from quintessence_pcs.domains.cognition.questions import (  # noqa: E402
    QuestionAccess,
    QuestionService,
)
from quintessence_pcs.domains.refs import RecordRef  # noqa: E402
from quintessence_pcs.foundation.errors import (  # noqa: E402
    CommandConflict,
    ContextStale,
    InvalidInput,
    RecordVersionConflict,
    SourceRedacted,
    StateRevisionConflict,
)

ALL_QUESTION_PERMISSIONS = frozenset(
    {
        "question.read",
        "question.create",
        "question.transition",
        "question.evaluate",
        "question.attention",
    }
)


class ChangeBundleTests(CognitionTestCase):
    # ------------------------------------------------------------- fixtures

    def access(self, *, namespace: str = NS) -> QuestionAccess:
        return QuestionAccess(
            namespace,
            "host:j06",
            "authorization:j06",
            frozenset({"private"}),
            ALL_QUESTION_PERMISSIONS,
            frozenset({"cognition.question"}),
        )

    def questions(self) -> QuestionService:
        return QuestionService(self.store)

    def open_question(self, *, text: str = "Why did the query fail?") -> RecordRef:
        event = self.ingest()
        created = self.questions().create(
            self.access(),
            context_id=self.build_context().context_id,
            request_id=str(uuid.uuid4()),
            question=text,
            origin="user_requested",
            scope={"project_id": "p1"},
            origin_refs=[self.event_ref(event.event_id)],
            privacy_class="private",
        )
        return created.view.ref

    def supported_belief(self, *, claim: str = "capability:file-read"):
        event = self.ingest()
        body = self.belief_body(
            claim=claim,
            evidence_links=[self.evidence_link(self.event_ref(event.event_id))],
        )
        return self.propose(body=body, scope={"scene": "test.scene"})

    # ------------------------------------------------------------ execution

    def bundle(self, changes, *, summary: str = "synthetic change bundle") -> dict:
        return {
            "operation": BUNDLE_OPERATION,
            "summary": summary,
            "changes": list(changes),
        }

    def prepare(self, changes, *, question_access=None):
        plan = validate_change_bundle(NS, self.bundle(changes))
        prepared = prepare_change_set(
            plan,
            beliefs=self.beliefs,
            questions=self.questions(),
            question_access=question_access,
        )
        return plan, prepared

    def commit(self, plan, prepared, *, context, request_id=None):
        applier = ChangeApplier(self.store, self.beliefs, self.questions())
        return self.store.commit_command(
            context_id=context.context_id,
            request_id=request_id or str(uuid.uuid4()),
            operation=BUNDLE_OPERATION,
            request_payload=plan.request_payload,
            apply=lambda scope: applier.apply(
                scope, plan, context_id=context.context_id, prepared=prepared
            ),
            namespace=NS,
        )

    def run_bundle(self, changes, *, question_access="auto", request_id=None):
        context = self.build_context()
        # Read the revision *after* the Context exists: building a Context may
        # refresh the SelfView, which is its own domain commit.
        before = self.revision()
        access = self.access() if question_access == "auto" else question_access
        plan, prepared = self.prepare(changes, question_access=access)
        receipt = self.commit(
            plan, prepared, context=context, request_id=request_id
        )
        return receipt, before

    def assert_rejected(self, changes, reason: str, *, question_access="auto"):
        access = self.access() if question_access == "auto" else question_access
        with self.assertRaises(ChangeBundleInvalid) as caught:
            self.prepare(changes, question_access=access)
        self.assertEqual(reason, caught.exception.reason)
        return caught.exception

    # ------------------------------------------------------------ CB-01..03

    def test_cb_01_single_propose_commits_one_version(self) -> None:
        event = self.ingest()
        body = self.belief_body(
            evidence_links=[self.evidence_link(self.event_ref(event.event_id))]
        )
        receipt, before = self.run_bundle(
            [{"op": "propose_belief", "body": body, "scope": {"scene": "test.scene"}}]
        )
        self.assertEqual("committed", receipt.outcome)
        self.assertEqual(1, len(receipt.record_refs))
        self.assertEqual(before + 1, receipt.state_revision)

    def test_cb_02_retract_and_propose_share_one_revision(self) -> None:
        belief = self.supported_belief()
        target = self.record_ref(belief)

        new_event = self.ingest()
        new_body = self.belief_body(
            claim="capability:file-write",
            evidence_links=[self.evidence_link(self.event_ref(new_event.event_id))],
        )
        retraction_event = self.ingest()
        receipt, before = self.run_bundle(
            [
                {
                    "op": "retract_belief",
                    "target": target,
                    "evidence_links": [self.evidence_link(self.event_ref(retraction_event.event_id))],
                    "reason": "synthetic retraction",
                },
                {
                    "op": "propose_belief",
                    "body": new_body,
                    "scope": {"scene": "test.scene"},
                },
            ]
        )

        self.assertEqual(2, len(receipt.record_refs))
        self.assertEqual(before + 1, receipt.state_revision)
        retracted = self.store.get_version(NS, belief.record_id, belief.record_version + 1)
        self.assertEqual("retracted", retracted.status)
        new_ref = receipt.record_refs[1]
        self.assertEqual("belief", self.store.get_version(NS, new_ref.record_id, 1).kind)

    def test_cb_03_retract_propose_close_is_one_transaction(self) -> None:
        belief = self.supported_belief()
        question = self.open_question()

        event = self.ingest()
        new_body = self.belief_body(
            claim="capability:file-write",
            evidence_links=[self.evidence_link(self.event_ref(event.event_id))],
        )
        receipt, before = self.run_bundle(
            [
                {
                    "op": "retract_belief",
                    "target": self.record_ref(belief),
                    "evidence_links": [self.evidence_link(self.event_ref(event.event_id))],
                    "reason": "synthetic retraction",
                },
                {
                    "op": "propose_belief",
                    "body": new_body,
                    "scope": {"scene": "test.scene"},
                },
                {
                    "op": "close_question",
                    "target": question.to_dict(),
                    "closed_reason": "synthetic cancellation",
                },
            ]
        )

        self.assertEqual(3, len(receipt.record_refs))
        self.assertEqual(before + 1, receipt.state_revision)
        closed = self.store.get_version(NS, question.record_id, question.record_version + 1)
        self.assertEqual("closed", closed.status)

    # ------------------------------------------------------------ CB-04/13

    def test_cb_04_in_transaction_failure_rolls_everything_back(self) -> None:
        belief = self.supported_belief()
        stale_target = self.record_ref(belief)
        event = self.ingest()
        body = self.belief_body(
            evidence_links=[self.evidence_link(self.event_ref(event.event_id))]
        )
        changes = [
            {"op": "propose_belief", "body": body, "scope": {"scene": "test.scene"}},
            {
                "op": "revise_belief",
                "target": stale_target,
                "body": self.belief_body(
                    claim="capability:file-read",
                    evidence_links=[self.evidence_link(self.event_ref(event.event_id))],
                    strength="limited",
                    strength_reason="synthetic replacement reason",
                ),
                "scope": {"scene": "test.scene"},
            },
        ]
        plan, prepared = self.prepare(changes)

        # Move the revise target after preparation but before the commit, then
        # take a fresh Context so the watermark check passes and the failure is
        # the seam's own version check.
        self.revise(
            stale_target,
            body=self.belief_body(
                claim="capability:file-read",
                evidence_links=[self.evidence_link(self.event_ref(event.event_id))],
                strength="limited",
                strength_reason="synthetic concurrent change",
            ),
        )
        context = self.build_context()
        before = self.revision()
        beliefs_before = self._count_belief_records()
        request_id = str(uuid.uuid4())

        with self.assertRaises((RecordVersionConflict, StateRevisionConflict)):
            self.commit(plan, prepared, context=context, request_id=request_id)

        self.assertEqual(before, self.revision())
        self.assertEqual(beliefs_before, self._count_belief_records())
        self.assertIsNone(self._stored_receipt(request_id))

    def test_cb_13_deleted_evidence_aborts_the_whole_bundle(self) -> None:
        good_body = self.belief_body()
        doomed_event = self.ingest()
        doomed_body = self.belief_body(
            claim="capability:file-write",
            evidence_links=[self.evidence_link(self.event_ref(doomed_event.event_id))],
        )
        changes = [
            {"op": "propose_belief", "body": good_body, "scope": {"scene": "test.scene"}},
            {"op": "propose_belief", "body": doomed_body, "scope": {"scene": "test.scene"}},
        ]
        plan, prepared = self.prepare(changes)

        self.delete_event(doomed_event.event_id)
        context = self.build_context()
        before = self.revision()
        beliefs_before = self._count_belief_records()
        request_id = str(uuid.uuid4())

        with self.assertRaises(SourceRedacted):
            self.commit(plan, prepared, context=context, request_id=request_id)

        self.assertEqual(before, self.revision())
        self.assertEqual(beliefs_before, self._count_belief_records())
        self.assertIsNone(self._stored_receipt(request_id))

    # ------------------------------------------------------------ CB-05..09

    def test_cb_05_duplicate_target_is_refused(self) -> None:
        belief = self.supported_belief()
        target = self.record_ref(belief)
        event = self.ingest()
        self.assert_rejected(
            [
                {
                    "op": "retract_belief",
                    "target": target,
                    "evidence_links": [self.evidence_link(self.event_ref(event.event_id))],
                    "reason": "first",
                },
                {
                    "op": "revise_belief",
                    "target": target,
                    "body": self.belief_body(),
                    "scope": {"scene": "test.scene"},
                },
            ],
            "duplicate_target",
        )

    def test_cb_06_change_count_is_bounded(self) -> None:
        with self.assertRaises(ChangeBundleInvalid) as empty:
            validate_change_bundle(NS, self.bundle([]))
        self.assertEqual("invalid_shape", empty.exception.reason)

        body = self.belief_body()
        five = [
            {"op": "propose_belief", "body": body, "scope": {"scene": "test.scene"}}
            for _ in range(5)
        ]
        with self.assertRaises(ChangeBundleInvalid) as too_many:
            validate_change_bundle(NS, self.bundle(five))
        self.assertEqual("invalid_shape", too_many.exception.reason)

    def test_cb_07_unknown_op_and_unknown_field_are_refused(self) -> None:
        with self.assertRaises(ChangeBundleInvalid) as bad_op:
            validate_change_bundle(
                NS,
                self.bundle(
                    [{"op": "delete_everything", "target": {"record_id": "x"}}]
                ),
            )
        self.assertEqual("unknown_op", bad_op.exception.reason)

        with self.assertRaises(ChangeBundleInvalid) as bad_field:
            validate_change_bundle(
                NS,
                self.bundle(
                    [
                        {
                            "op": "propose_belief",
                            "body": self.belief_body(),
                            "scope": {"scene": "test.scene"},
                            "strength": "strong",
                        }
                    ]
                ),
            )
        self.assertEqual("unknown_field", bad_field.exception.reason)

    def test_cb_08_close_cannot_carry_resolution_refs(self) -> None:
        question = self.open_question()
        event = self.ingest()
        self.assert_rejected(
            [
                {
                    "op": "close_question",
                    "target": question.to_dict(),
                    "closed_reason": "synthetic cancellation",
                    "resolution_refs": [self.event_ref(event.event_id)],
                }
            ],
            "resolution_not_supported",
        )

    def test_cb_09_resolve_requires_resolution_refs(self) -> None:
        question = self.open_question()
        self.assert_rejected(
            [
                {
                    "op": "resolve_question",
                    "target": question.to_dict(),
                    "resolution_refs": [],
                    "resolution_reason": "synthetic resolution",
                }
            ],
            "invalid_shape",
        )

    # ------------------------------------------------------------ CB-10..12

    def test_cb_10_four_change_bundle_commits_every_version(self) -> None:
        first = self.supported_belief()
        second = self.supported_belief(claim="capability:file-write")
        question_a = self.open_question(text="Question A")
        question_b = self.open_question(text="Question B")

        event = self.ingest()
        receipt, before = self.run_bundle(
            [
                {
                    "op": "retract_belief",
                    "target": self.record_ref(first),
                    "evidence_links": [self.evidence_link(self.event_ref(event.event_id))],
                    "reason": "synthetic retraction",
                },
                {
                    "op": "propose_belief",
                    "body": self.belief_body(
                        evidence_links=[self.evidence_link(self.event_ref(event.event_id))]
                    ),
                    "scope": {"scene": "test.scene"},
                },
                {
                    "op": "close_question",
                    "target": question_a.to_dict(),
                    "closed_reason": "synthetic cancellation",
                },
                {
                    "op": "close_question",
                    "target": question_b.to_dict(),
                    "closed_reason": "synthetic cancellation",
                },
            ]
        )

        self.assertEqual(4, len(receipt.record_refs))
        self.assertEqual(before + 1, receipt.state_revision)
        self.assertEqual(
            "closed",
            self.store.get_version(
                NS, question_a.record_id, question_a.record_version + 1
            ).status,
        )
        self.assertEqual(
            "closed",
            self.store.get_version(
                NS, question_b.record_id, question_b.record_version + 1
            ).status,
        )
        self.assertEqual(
            "retracted",
            self.store.get_version(
                NS, first.record_id, first.record_version + 1
            ).status,
        )
        # ``second`` was not touched by this bundle; its current version is
        # still the one it was created with.
        self.assertEqual(
            "supported",
            self.store.get_version(
                NS, second.record_id, second.record_version
            ).status,
        )

    def test_cb_11_same_request_same_parameters_replays_receipt(self) -> None:
        body = self.belief_body()
        context = self.build_context()
        plan, prepared = self.prepare(
            [{"op": "propose_belief", "body": body, "scope": {"scene": "test.scene"}}]
        )
        request_id = str(uuid.uuid4())
        first = self.commit(plan, prepared, context=context, request_id=request_id)
        second = self.commit(plan, prepared, context=context, request_id=request_id)

        self.assertEqual(first.request_id, second.request_id)
        self.assertEqual(first.record_refs, second.record_refs)
        self.assertEqual(first.state_revision, second.state_revision)
        self.assertEqual(1, self._count_belief_records())

    def test_cb_12_same_request_different_parameters_conflicts(self) -> None:
        context = self.build_context()
        request_id = str(uuid.uuid4())
        first_plan, first_prepared = self.prepare(
            [
                {
                    "op": "propose_belief",
                    "body": self.belief_body(claim="capability:first"),
                    "scope": {"scene": "test.scene"},
                }
            ]
        )
        self.commit(first_plan, first_prepared, context=context, request_id=request_id)

        second_plan, second_prepared = self.prepare(
            [
                {
                    "op": "propose_belief",
                    "body": self.belief_body(claim="capability:second"),
                    "scope": {"scene": "test.scene"},
                }
            ]
        )
        with self.assertRaises(CommandConflict):
            self.commit(second_plan, second_prepared, context=context, request_id=request_id)

    # ------------------------------------------------------------ CB-14..17

    def test_cb_14_prepare_transition_matches_transition_refusals(self) -> None:
        question = self.open_question()
        service = self.questions()
        access = self.access()

        cases = (
            (
                "unknown_action",
                lambda **kwargs: service.transition(
                    access,
                    context_id=self.build_context().context_id,
                    request_id=str(uuid.uuid4()),
                    ref=question,
                    expected_version=question.record_version,
                    action="not_an_action",
                ),
                lambda: service.prepare_transition(
                    access,
                    ref=question,
                    expected_version=question.record_version,
                    action="not_an_action",
                ),
            ),
            (
                "foreign_ref",
                lambda: service.transition(
                    access,
                    context_id=self.build_context().context_id,
                    request_id=str(uuid.uuid4()),
                    ref=RecordRef("other.namespace", question.record_id, 1),
                    expected_version=1,
                    action="close",
                    reason="synthetic",
                ),
                lambda: service.prepare_transition(
                    access,
                    ref=RecordRef("other.namespace", question.record_id, 1),
                    expected_version=1,
                    action="close",
                    reason="synthetic",
                ),
            ),
            (
                "bad_expected_version",
                lambda: service.transition(
                    access,
                    context_id=self.build_context().context_id,
                    request_id=str(uuid.uuid4()),
                    ref=question,
                    expected_version=0,
                    action="close",
                    reason="synthetic",
                ),
                lambda: service.prepare_transition(
                    access,
                    ref=question,
                    expected_version=0,
                    action="close",
                    reason="synthetic",
                ),
            ),
        )

        for label, direct, prepared in cases:
            with self.subTest(case=label):
                with self.assertRaises(Exception) as direct_error:
                    direct()
                with self.assertRaises(Exception) as prepared_error:
                    prepared()
                self.assertIs(type(direct_error.exception), type(prepared_error.exception))
                self.assertEqual(
                    str(direct_error.exception), str(prepared_error.exception)
                )

        no_permission = QuestionAccess(
            NS,
            "host:j06",
            "authorization:j06",
            frozenset({"private"}),
            frozenset({"question.read"}),
            frozenset({"cognition.question"}),
        )
        with self.assertRaises(Exception) as direct_error:
            service.transition(
                no_permission,
                context_id=self.build_context().context_id,
                request_id=str(uuid.uuid4()),
                ref=question,
                expected_version=question.record_version,
                action="close",
                reason="synthetic",
            )
        with self.assertRaises(Exception) as prepare_error:
            service.prepare_transition(
                no_permission,
                ref=question,
                expected_version=question.record_version,
                action="close",
                reason="synthetic",
            )
        self.assertIs(type(direct_error.exception), type(prepare_error.exception))
        self.assertEqual(str(direct_error.exception), str(prepare_error.exception))

    def test_cb_15_question_change_requires_question_access(self) -> None:
        question = self.open_question()
        self.assert_rejected(
            [
                {
                    "op": "close_question",
                    "target": question.to_dict(),
                    "closed_reason": "synthetic cancellation",
                }
            ],
            "invalid_shape",
            question_access=None,
        )

    def test_cb_16_bundle_never_writes_work_progress(self) -> None:
        before = self.count_rows("work_progress")
        self.run_bundle(
            [
                {
                    "op": "propose_belief",
                    "body": self.belief_body(),
                    "scope": {"scene": "test.scene"},
                }
            ]
        )
        self.assertEqual(before, self.count_rows("work_progress"))

    def test_cb_17_payload_is_stable_across_key_order(self) -> None:
        body = self.belief_body()
        first = validate_change_bundle(
            NS,
            self.bundle(
                [{"op": "propose_belief", "body": body, "scope": {"scene": "test.scene"}}]
            ),
        )
        second = validate_change_bundle(
            NS,
            {
                "changes": [
                    {
                        "scope": {"scene": "test.scene"},
                        "body": body,
                        "op": "propose_belief",
                    }
                ],
                "summary": "synthetic change bundle",
                "operation": BUNDLE_OPERATION,
            },
        )
        self.assertEqual(first.request_payload, second.request_payload)

    # ------------------------------------------------- CB-18..20 (tolerance)

    def test_cb_18_foreign_move_is_still_stale(self) -> None:
        """The tolerance is per-record and only for *this* transaction's writes."""

        belief = self.supported_belief(claim="capability:alpha")
        context = self.build_context()

        # A second connection moves the belief the Context rendered.
        other = self.open_second_store()
        other_snapshot = other.capture_snapshot(NS)
        other.commit_command(
            context_id=other_snapshot.context_id,
            request_id=str(uuid.uuid4()),
            operation="belief.revise",
            request_payload={"synthetic": "concurrent move"},
            apply=lambda scope: scope.append_record_version(
                record_id=belief.record_id,
                record_version=belief.record_version + 1,
                kind="belief",
                body={
                    "claim": "capability:alpha",
                    "claim_type": "capability",
                    "evidence_links": [],
                    "strength": "insufficient",
                    "strength_reason": "synthetic concurrent change",
                    "inference_basis": "observation",
                    "premise_refs": [],
                    "alternative_group_id": None,
                    "valid_until": None,
                    "review_condition": "synthetic",
                },
                status="proposed",
                scope={"scene": "test.scene"},
            ),
            namespace=NS,
        )

        # Tolerant only for records this transaction itself appended: an
        # unrelated id (or none at all) must still fail.
        for tolerated in ((), ("some-other-record",)):
            with self.subTest(tolerated=tolerated):
                with self.assertRaises(ContextStale):
                    require_live_context(
                        self.store,
                        context_id=context.context_id,
                        namespace=NS,
                        moved_in_transaction=tolerated,
                    )

        # End to end, a foreign move is refused by the earlier watermark layer.
        plan, prepared = self.prepare(
            [
                {
                    "op": "propose_belief",
                    "body": self.belief_body(claim="capability:beta"),
                    "scope": {"scene": "test.scene"},
                }
            ]
        )
        with self.assertRaises(StateRevisionConflict):
            self.commit(plan, prepared, context=context)

    def test_cb_19_deleted_read_set_record_is_still_stale(self) -> None:
        belief = self.supported_belief(claim="capability:alpha")
        context = self.build_context()
        self.delete_record(belief.record_id)

        with self.assertRaises(ContextStale):
            require_live_context(
                self.store,
                context_id=context.context_id,
                namespace=NS,
                moved_in_transaction=(belief.record_id,),
            )

    def test_cb_20_both_orders_retract_propose_succeed(self) -> None:
        for order in ("retract_first", "propose_first"):
            with self.subTest(order=order):
                belief = self.supported_belief(claim="capability:alpha")
                target = self.record_ref(belief)
                event = self.ingest()
                link = self.evidence_link(self.event_ref(event.event_id))
                retract = {
                    "op": "retract_belief",
                    "target": target,
                    "evidence_links": [link],
                    "reason": "synthetic retraction",
                }
                propose = {
                    "op": "propose_belief",
                    "body": self.belief_body(
                        claim="capability:beta", evidence_links=[link]
                    ),
                    "scope": {"scene": "test.scene"},
                }
                changes = [retract, propose] if order == "retract_first" else [propose, retract]
                receipt, before = self.run_bundle(changes)

                self.assertEqual(2, len(receipt.record_refs))
                self.assertEqual(before + 1, receipt.state_revision)
                # The version the Context saw is still readable after the move.
                old = self.store.get_version(
                    NS, belief.record_id, belief.record_version
                )
                self.assertEqual("supported", old.status)

    def test_cb_21_tolerance_set_is_empty_on_the_single_op_path(self) -> None:
        """The old single-operation path must pass no tolerance at all."""

        from unittest import mock

        from quintessence_pcs.domains.cognition import beliefs as beliefs_module

        real = beliefs_module.require_live_context
        with mock.patch.object(
            beliefs_module, "require_live_context", wraps=real
        ) as spy:
            self.supported_belief(claim="capability:single")

        self.assertTrue(spy.called)
        for call in spy.call_args_list:
            self.assertEqual((), call.kwargs["moved_in_transaction"])

    def test_cb_22_tolerance_set_names_only_this_transactions_writes(self) -> None:
        from unittest import mock

        from quintessence_pcs.domains.cognition import beliefs as beliefs_module

        belief = self.supported_belief(claim="capability:alpha")
        target = self.record_ref(belief)
        event = self.ingest()
        link = self.evidence_link(self.event_ref(event.event_id))

        seen: list[tuple[str, ...]] = []
        real = beliefs_module.require_live_context

        def record(*args, **kwargs):
            seen.append(tuple(kwargs.get("moved_in_transaction", ())))
            return real(*args, **kwargs)

        with mock.patch.object(
            beliefs_module, "require_live_context", side_effect=record
        ):
            receipt, _ = self.run_bundle(
                [
                    {
                        "op": "propose_belief",
                        "body": self.belief_body(
                            claim="capability:beta", evidence_links=[link]
                        ),
                        "scope": {"scene": "test.scene"},
                    },
                    {
                        "op": "retract_belief",
                        "target": target,
                        "evidence_links": [link],
                        "reason": "synthetic retraction",
                    },
                ]
            )

        self.assertEqual(2, len(receipt.record_refs))
        self.assertEqual(2, len(seen))
        # First seam: nothing appended yet, so nothing is tolerated.
        self.assertEqual((), seen[0])
        # Second seam: exactly the record the first seam appended, and only it.
        self.assertEqual((receipt.record_refs[0].record_id,), seen[1])
        self.assertNotIn(belief.record_id, seen[1])

    # ------------------------------------------------------------- helpers

    def _count_belief_records(self) -> int:
        row = self.store.connection.execute(
            "SELECT COUNT(*) FROM records WHERE namespace = ? AND kind = 'belief'",
            (NS,),
        ).fetchone()
        return int(row[0])

    def _stored_receipt(self, request_id: str):
        return self.store.lookup_command_receipt(
            namespace=NS, request_id=request_id
        )


if __name__ == "__main__":
    unittest.main()
