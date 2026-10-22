"""C05c: paginated experience listing, exact reads and the read audit.

The fixtures are entirely synthetic.  Two access objects are used throughout:
a wide one (all three privacy classes, three purposes) and a strict one
(``public-example`` only, ``consult`` only).  No test reads real private data,
calls a model, or touches the network.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import sqlite3
import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from support import StoreTestCase, append  # noqa: E402

from quintessence_pcs.foundation.errors import (  # noqa: E402
    InvalidInput,
    ScopeViolation,
)
from quintessence_pcs.store.cognition import CognitionStore  # noqa: E402
from quintessence_pcs.store.experience import (  # noqa: E402
    ExperienceAccess,
    ExperiencePage,
    ExperienceUnavailable,
    ExperienceView,
)

NS = "example.test.experience"
NS_B = "example.test.experience.other"
TS = "2026-10-22T00:00:00Z"

INPUT_A = "11111111-1111-4111-8111-111111111111"
INPUT_B = "12121212-1212-4212-8212-121212121212"
INPUT_C = "13131313-1313-4313-8313-131313131313"
INPUT_D = "14141414-1414-4414-8414-141414141414"
MISSING_INPUT = "19191919-1919-4919-8919-191919191919"
MISSING_ARTIFACT = "1a1a1a1a-1a1a-4a1a-8a1a-1a1a1a1a1a1a"
ARTIFACT_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ARTIFACT_B = "abababab-abab-4bab-8bab-abababababab"
ARTIFACT_C = "acacacac-acac-4cac-8cac-acacacacacac"
RECORD_ID = "22222222-2222-4222-8222-222222222222"
EVENT_ID = "66666666-6666-4666-8666-666666666666"
WORK_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
REQUEST_ID = "22223333-4444-4555-8666-777788889999"
ATTEMPT_ID = "55555555-5555-4555-8555-555555555555"
AUTHORIZATION_ID = "99999999-9999-4999-8999-999999999999"
CANDIDATE_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

PRIVACY_ALL = ("private", "restricted", "public-example")


class ExperienceReadTests(StoreTestCase):
    store_class = CognitionStore

    # ------------------------------------------------------------- fixtures

    def access(
        self,
        *,
        privacy=("private",),
        purposes=("consult",),
        namespace=NS,
        host="host:synthetic-1",
        access_ref="authorization:access:1",
    ) -> ExperienceAccess:
        return ExperienceAccess(
            namespace=namespace,
            host_instance=host,
            access_ref=access_ref,
            privacy_classes=frozenset(privacy),
            purposes=frozenset(purposes),
        )

    @property
    def wide(self) -> ExperienceAccess:
        return self.access(
            privacy=PRIVACY_ALL,
            purposes=("consult", "review", "audit"),
            access_ref="authorization:wide",
            host="host:wide",
        )

    @property
    def strict(self) -> ExperienceAccess:
        return self.access(
            privacy=("public-example",),
            access_ref="authorization:strict",
            host="host:strict",
        )

    def make_context(self, namespace: str = NS) -> str:
        return self.store.capture_snapshot(namespace).context_id

    def make_input(
        self,
        *,
        input_id: str | None = None,
        version: int = 1,
        body: str | None = None,
        privacy: str = "private",
        retention: str = "standard",
        recorded_at: str = TS,
        origin_kind: str = "user_statement",
        namespace: str = NS,
    ):
        return self.store.register_input(
            namespace=namespace,
            origin_kind=origin_kind,
            body=body or f"synthetic input body {version}",
            privacy_class=privacy,
            retention=retention,
            observed_at=None,
            recorded_at=recorded_at,
            input_id=input_id or str(uuid.uuid4()),
            content_version=version,
        )

    def make_interaction(
        self,
        *,
        input_refs=(),
        context_id: str | None = None,
        created_at: str = TS,
        result: str | None = None,
        mode: str = "consult",
        trigger_ref=None,
        work_id=None,
        work_generation=None,
        namespace: str = NS,
    ) -> str:
        return self.store.register_interaction(
            namespace=namespace,
            mode=mode,
            activation_kind="user_input" if mode == "consult" else "manual_review",
            host_instance="host:synthetic-1",
            access_ref="authorization:access:1",
            policy_ref="policy:collab.v1",
            input_refs=input_refs,
            context_id=(
                context_id
                if context_id is not None
                else self.make_context(namespace)
            ),
            trigger_ref=trigger_ref,
            work_id=work_id,
            work_generation=work_generation,
            result=result,
            created_at=created_at,
        )

    def link_input(self, row, *, created_at: str = TS, result: str | None = None) -> str:
        return self.make_interaction(
            input_refs=[(row.input_id, row.content_version)],
            created_at=created_at,
            result=result,
        )

    def make_artifact(
        self,
        *,
        artifact_id: str | None = None,
        version: int = 1,
        kind: str = "reply",
        body: str = "synthetic artifact body",
        origin_kind: str = "model_output",
        retention: str = "standard",
        sources=(),
        created_at: str = TS,
        context_id: str | None = None,
        interaction_id: str | None = None,
        result: str = "answered",
        attempt_ref=None,
        namespace: str = NS,
        **overrides,
    ):
        context = context_id or self.make_context(namespace)
        interaction = interaction_id or self.make_interaction(
            context_id=context,
            created_at=created_at,
            result=result,
            namespace=namespace,
        )
        return self.store.register_artifact(
            namespace=namespace,
            kind=kind,
            interaction_id=interaction,
            context_id=context,
            origin_kind=origin_kind,
            body=body,
            retention=retention,
            sources=list(sources),
            artifact_id=artifact_id or str(uuid.uuid4()),
            artifact_version=version,
            attempt_ref=attempt_ref,
            created_at=created_at,
            **overrides,
        )

    def make_record(self, *, version: int = 1, privacy: str = "restricted"):
        return append(
            self.store,
            version=version,
            body={"text": f"synthetic record version {version}"},
            namespace=NS,
            record_id=RECORD_ID,
            privacy_class=privacy,
        )

    def make_event(self, *, privacy: str = "public-example") -> str:
        receipt = self.store.ingest_event(
            namespace=NS,
            source_kind="synthetic_fixture",
            source_instance="experience-instance",
            source_event_id=str(uuid.uuid4()),
            event_type="ObservationRecorded",
            observed_at=None,
            payload={"text": "synthetic event"},
            privacy_class=privacy,
            recorded_at=TS,
        )
        return receipt.event_id

    # ------------------------------------------------------------- helpers

    def input_ref(self, row, namespace: str = NS) -> dict:
        return {
            "ref_type": "collab_input",
            "namespace": namespace,
            "input_id": row.input_id,
            "content_version": row.content_version,
        }

    def artifact_ref(self, row, namespace: str = NS) -> dict:
        return {
            "ref_type": "collab_artifact",
            "namespace": namespace,
            "artifact_id": row.artifact_id,
            "artifact_version": row.artifact_version,
        }

    def record_ref(self, version: int = 1, privacy: str = "restricted") -> dict:
        self.make_record(version=version, privacy=privacy)
        return {
            "ref_type": "record",
            "namespace": NS,
            "record_id": RECORD_ID,
            "record_version": version,
        }

    def event_ref(self, event_id: str) -> dict:
        return {"ref_type": "event", "namespace": NS, "event_id": event_id}

    def audit_rows(self):
        return self.store.connection.execute(
            "SELECT read_id, namespace, host_instance, access_ref, purpose, "
            "ref_type, ref_id, ref_version, outcome, read_at "
            "FROM collab_experience_reads ORDER BY rowid"
        ).fetchall()

    def audit_count(self) -> int:
        return int(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM collab_experience_reads"
            ).fetchone()[0]
        )

    def table_count(self, table: str) -> int:
        return int(
            self.store.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
        )

    def payload_length(self, table: str, column: str, **where) -> int:
        clause = " AND ".join(f"{name} = ?" for name in where)
        row = self.store.connection.execute(
            "SELECT length(body_json) FROM private_payloads WHERE payload_ref = "
            f"(SELECT {column} FROM {table} WHERE {clause})",
            tuple(where.values()),
        ).fetchone()
        self.assertIsNotNone(row)
        return int(row[0])

    @staticmethod
    def encoded_cursor(**overrides) -> str:
        document = {
            "created_at": TS,
            "ref_type": "collab_input",
            "ref_id": INPUT_A,
            "ref_version": 1,
        }
        document.update(overrides)
        text = json.dumps(document, sort_keys=True, separators=(",", ":"))
        return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")

    # ---------------------------------------------------------------- tests

    def test_list_orders_and_paginates_with_a_stable_cursor(self) -> None:
        first = self.make_artifact(
            artifact_id=ARTIFACT_A, created_at="2026-10-22T00:00:01Z"
        )
        second = self.make_input(
            input_id=INPUT_A, recorded_at="2026-10-22T00:00:02Z"
        )
        self.link_input(second)
        third = self.make_artifact(
            artifact_id=ARTIFACT_B, created_at="2026-10-22T00:00:03Z"
        )
        fourth = self.make_input(
            input_id=INPUT_B, recorded_at="2026-10-22T00:00:04Z"
        )
        self.link_input(fourth)

        page_one = self.store.list_experience_refs(
            self.wide, namespace=NS, limit=2
        )
        self.assertEqual(
            (self.artifact_ref(first), self.input_ref(second)), page_one.refs
        )
        self.assertIsNotNone(page_one.next_cursor)

        page_two = self.store.list_experience_refs(
            self.wide,
            namespace=NS,
            after_cursor=page_one.next_cursor,
            limit=2,
        )
        self.assertEqual(
            (self.artifact_ref(third), self.input_ref(fourth)), page_two.refs
        )
        self.assertIsNone(page_two.next_cursor)

        # The same cursor is stable and page two does not repeat page one.
        self.assertEqual(
            page_one,
            self.store.list_experience_refs(self.wide, namespace=NS, limit=2),
        )
        self.assertEqual(
            page_two,
            self.store.list_experience_refs(
                self.wide,
                namespace=NS,
                after_cursor=page_one.next_cursor,
                limit=2,
            ),
        )

        walked = []
        cursor = None
        while True:
            page = self.store.list_experience_refs(
                self.wide, namespace=NS, after_cursor=cursor, limit=1
            )
            walked.extend(page.refs)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        self.assertEqual(
            [
                self.artifact_ref(first),
                self.input_ref(second),
                self.artifact_ref(third),
                self.input_ref(fourth),
            ],
            walked,
        )

    def test_list_rejects_invalid_limit_and_cursor(self) -> None:
        for limit in (0, -1, 101, "5", True, 1.5, None):
            with self.subTest(limit=limit):
                with self.assertRaises(InvalidInput):
                    self.store.list_experience_refs(
                        self.wide, namespace=NS, limit=limit
                    )
        # Both legal boundaries are accepted.
        self.assertEqual(
            ExperiencePage((), None),
            self.store.list_experience_refs(self.wide, namespace=NS, limit=1),
        )
        self.assertEqual(
            ExperiencePage((), None),
            self.store.list_experience_refs(self.wide, namespace=NS, limit=100),
        )

        bad_cursors = [
            "",
            "!!!not-base64!!!",
            base64.urlsafe_b64encode(b"not json").decode("ascii"),
            self.encoded_cursor(ref_type="record"),
            self.encoded_cursor(ref_id="not-a-uuid"),
            self.encoded_cursor(ref_version=0),
            self.encoded_cursor(created_at="not-a-timestamp"),
            self.encoded_cursor(extra="field"),
            123,
            None,
        ]
        for cursor in bad_cursors:
            if cursor is None:
                continue
            with self.subTest(cursor=cursor):
                with self.assertRaises(InvalidInput):
                    self.store.list_experience_refs(
                        self.wide, namespace=NS, after_cursor=cursor
                    )
        with self.subTest(cursor="none-type"):
            with self.assertRaises(InvalidInput):
                self.store.list_experience_refs(
                    self.wide, namespace=NS, after_cursor=123
                )

        missing_key = json.loads(
            base64.urlsafe_b64decode(self.encoded_cursor().encode("ascii"))
        )
        missing_key.pop("ref_id")
        with self.subTest(cursor="missing-key"):
            with self.assertRaises(InvalidInput):
                self.store.list_experience_refs(
                    self.wide,
                    namespace=NS,
                    after_cursor=base64.urlsafe_b64encode(
                        json.dumps(missing_key).encode("utf-8")
                    ).decode("ascii"),
                )

    def test_list_filters_by_every_source_privacy_class(self) -> None:
        private = self.make_input(input_id=INPUT_A, privacy="private")
        self.link_input(private)
        event_id = self.make_event(privacy="public-example")
        artifact = self.make_artifact(
            artifact_id=ARTIFACT_A,
            created_at="2026-10-22T00:00:01Z",
            sources=[
                self.input_ref(private),
                self.record_ref(version=1, privacy="restricted"),
                self.event_ref(event_id),
            ],
        )

        all_classes = self.access(privacy=PRIVACY_ALL)
        self.assertEqual(
            (self.input_ref(private), self.artifact_ref(artifact)),
            self.store.list_experience_refs(all_classes, namespace=NS).refs,
        )

        public_only = self.access(privacy=("public-example",))
        self.assertEqual(
            ExperiencePage((), None),
            self.store.list_experience_refs(public_only, namespace=NS),
            "an artifact is invisible unless every source class is allowed",
        )

        private_public = self.access(privacy=("private", "public-example"))
        self.assertEqual(
            (self.input_ref(private),),
            self.store.list_experience_refs(private_public, namespace=NS).refs,
            "a missing restricted permission hides the derived artifact",
        )

        restricted_public = self.access(
            privacy=("restricted", "public-example")
        )
        self.assertEqual(
            ExperiencePage((), None),
            self.store.list_experience_refs(restricted_public, namespace=NS),
            "a missing private permission hides the input and the artifact",
        )

    def test_list_requires_the_access_namespace(self) -> None:
        with self.assertRaises(ScopeViolation):
            self.store.list_experience_refs(self.wide, namespace=NS_B)
        with self.assertRaises(InvalidInput):
            self.store.list_experience_refs(self.wide, namespace="Not Valid")
        # A different access namespace is accepted for its own namespace.
        self.assertEqual(
            ExperiencePage((), None),
            self.store.list_experience_refs(
                self.access(namespace=NS_B), namespace=NS_B
            ),
        )

    def test_list_hides_unauthorized_rows_without_shifting_pages(self) -> None:
        first = self.make_input(
            input_id=INPUT_A, privacy="public-example",
            recorded_at="2026-10-22T00:00:01Z",
        )
        self.link_input(first)
        hidden = self.make_input(
            input_id=INPUT_B, privacy="private",
            recorded_at="2026-10-22T00:00:02Z",
        )
        self.link_input(hidden)
        third = self.make_input(
            input_id=INPUT_C, privacy="public-example",
            recorded_at="2026-10-22T00:00:03Z",
        )
        self.link_input(third)
        fourth = self.make_input(
            input_id=INPUT_D, privacy="public-example",
            recorded_at="2026-10-22T00:00:04Z",
        )
        self.link_input(fourth)

        page_one = self.store.list_experience_refs(
            self.strict, namespace=NS, limit=1
        )
        self.assertEqual((self.input_ref(first),), page_one.refs)
        self.assertEqual(1, len(page_one.refs))
        self.assertIsNotNone(page_one.next_cursor)

        page_two = self.store.list_experience_refs(
            self.strict,
            namespace=NS,
            after_cursor=page_one.next_cursor,
            limit=1,
        )
        self.assertEqual((self.input_ref(third),), page_two.refs)
        self.assertEqual(1, len(page_two.refs))
        self.assertIsNotNone(page_two.next_cursor)

        page_three = self.store.list_experience_refs(
            self.strict,
            namespace=NS,
            after_cursor=page_two.next_cursor,
            limit=1,
        )
        self.assertEqual((self.input_ref(fourth),), page_three.refs)
        self.assertIsNone(page_three.next_cursor)

        two_at_a_time = self.store.list_experience_refs(
            self.strict, namespace=NS, limit=2
        )
        self.assertEqual(
            (self.input_ref(first), self.input_ref(third)),
            two_at_a_time.refs,
        )
        self.assertIsNotNone(two_at_a_time.next_cursor)

        rest = self.store.list_experience_refs(
            self.strict,
            namespace=NS,
            after_cursor=two_at_a_time.next_cursor,
            limit=2,
        )
        self.assertEqual((self.input_ref(fourth),), rest.refs)
        self.assertIsNone(rest.next_cursor)
        self.assertNotIn(self.input_ref(hidden), page_one.refs + page_two.refs + page_three.refs)

    def test_read_input_returns_an_available_view(self) -> None:
        row = self.make_input(
            input_id=INPUT_A,
            body="synthetic private statement",
            recorded_at="2026-10-22T00:00:01Z",
        )
        context = self.make_context()
        interaction = self.make_interaction(
            context_id=context,
            created_at="2026-10-22T00:00:00Z",
            result="answered",
            input_refs=[(row.input_id, row.content_version)],
        )

        view = self.store.read_experience(
            self.wide, ref=self.input_ref(row), purpose="consult"
        )
        self.assertIsInstance(view, ExperienceView)
        self.assertEqual(self.input_ref(row), view.ref)
        self.assertEqual(NS, view.namespace)
        self.assertEqual(interaction, view.interaction_id)
        self.assertEqual("consult", view.mode)
        self.assertEqual("user_statement", view.origin_kind)
        self.assertEqual("answered", view.processing)
        self.assertEqual("available", view.readability)
        self.assertEqual("synthetic private statement", view.body)
        self.assertEqual("standard", view.retention)
        self.assertEqual(context, view.context_ref)
        self.assertIsNone(view.attempt_ref)
        self.assertEqual((), view.source_refs)
        self.assertEqual("2026-10-22T00:00:01Z", view.created_at)
        self.assertEqual(13, len(dataclasses.fields(view)))
        self.assertEqual(1, self.audit_count())

    def test_read_reply_returns_context_attempt_and_sources(self) -> None:
        source = self.make_input(input_id=INPUT_A)
        self.link_input(source)
        record = self.record_ref(version=1, privacy="restricted")
        attempt = {
            "authorization_id": AUTHORIZATION_ID,
            "attempt_id": ATTEMPT_ID,
            "attempt_no": 1,
            "request_hash": "sha256:" + "a" * 64,
        }
        context = self.make_context()
        interaction = self.make_interaction(
            context_id=context,
            created_at="2026-10-22T00:00:00Z",
            result="answered",
        )
        artifact = self.make_artifact(
            artifact_id=ARTIFACT_A,
            context_id=context,
            interaction_id=interaction,
            body="synthetic reply body",
            attempt_ref=attempt,
            sources=[self.input_ref(source), record],
        )

        view = self.store.read_experience(
            self.wide, ref=self.artifact_ref(artifact), purpose="consult"
        )
        self.assertIsInstance(view, ExperienceView)
        self.assertEqual(self.artifact_ref(artifact), view.ref)
        self.assertEqual(interaction, view.interaction_id)
        self.assertEqual("consult", view.mode)
        self.assertEqual("model_output", view.origin_kind)
        self.assertEqual("answered", view.processing)
        self.assertEqual("available", view.readability)
        self.assertEqual("synthetic reply body", view.body)
        self.assertEqual(context, view.context_ref)
        self.assertEqual(attempt, view.attempt_ref)
        self.assertEqual(
            (self.input_ref(source), record), view.source_refs
        )
        self.assertEqual(1, self.audit_count())

    def test_read_rejected_candidate_explains_the_rejection(self) -> None:
        context = self.make_context()
        interaction = self.make_interaction(
            context_id=context,
            created_at="2026-10-22T00:00:00Z",
            result="rejected",
            mode="review",
            trigger_ref={
                "ref_type": "event",
                "namespace": NS,
                "event_id": EVENT_ID,
            },
            work_id=WORK_ID,
            work_generation=1,
        )
        candidate = self.store.register_artifact(
            namespace=NS,
            kind="candidate",
            interaction_id=interaction,
            context_id=context,
            origin_kind="model_output",
            body="raw model output for a rejected candidate",
            retention="standard",
            sources=[],
            artifact_id=CANDIDATE_ID,
            artifact_version=1,
            created_at="2026-10-22T00:00:01Z",
            proposal={"operation": "propose_belief", "reason": "synthetic"},
            proposal_version="v1",
            work_id=WORK_ID,
            work_generation=1,
            request_id=REQUEST_ID,
        )
        self.store.connection.execute(
            "UPDATE collab_artifacts SET status = 'rejected', decided_at = ?, "
            "decision_ref = 'decision:synthetic' WHERE namespace = ? "
            "AND artifact_id = ? AND artifact_version = 1",
            (TS, NS, CANDIDATE_ID),
        )

        view = self.store.read_experience(
            self.wide, ref=self.artifact_ref(candidate), purpose="review"
        )
        self.assertIsInstance(view, ExperienceView)
        self.assertEqual("review", view.mode)
        self.assertEqual("rejected", view.processing)
        self.assertEqual("available", view.readability)
        self.assertEqual(
            "raw model output for a rejected candidate", view.body
        )
        self.assertEqual((), view.source_refs)
        self.assertEqual(
            "rejected",
            self.store.connection.execute(
                "SELECT status FROM collab_artifacts WHERE artifact_id = ?",
                (CANDIDATE_ID,),
            ).fetchone()[0],
        )
        self.assertEqual(1, self.audit_count())

    def test_model_output_stays_model_output(self) -> None:
        statement = self.make_input(input_id=INPUT_A)
        self.link_input(statement)
        reply = self.make_artifact(
            artifact_id=ARTIFACT_A,
            body="synthetic reply",
            origin_kind="model_output",
            created_at="2026-10-22T00:00:01Z",
        )
        query = self.make_artifact(
            artifact_id=ARTIFACT_B,
            kind="query_result",
            body="synthetic query result",
            origin_kind="deterministic_query",
            created_at="2026-10-22T00:00:02Z",
        )

        statement_view = self.store.read_experience(
            self.wide, ref=self.input_ref(statement), purpose="consult"
        )
        reply_view = self.store.read_experience(
            self.wide, ref=self.artifact_ref(reply), purpose="consult"
        )
        query_view = self.store.read_experience(
            self.wide, ref=self.artifact_ref(query), purpose="consult"
        )
        self.assertEqual("user_statement", statement_view.origin_kind)
        self.assertEqual("model_output", reply_view.origin_kind)
        self.assertEqual("deterministic_query", query_view.origin_kind)
        # A read never promotes a model output into evidence.
        self.assertEqual(0, self.table_count("event_journal"))
        self.assertEqual(0, self.table_count("record_versions"))
        self.assertEqual(3, self.audit_count())

    def test_read_survives_store_reopen(self) -> None:
        row = self.make_input(input_id=INPUT_A)
        self.link_input(row)
        artifact = self.make_artifact(artifact_id=ARTIFACT_A)

        first_input = self.store.read_experience(
            self.wide, ref=self.input_ref(row), purpose="consult"
        )
        first_artifact = self.store.read_experience(
            self.wide, ref=self.artifact_ref(artifact), purpose="consult"
        )
        self.reopen()
        second_input = self.store.read_experience(
            self.wide, ref=self.input_ref(row), purpose="consult"
        )
        second_artifact = self.store.read_experience(
            self.wide, ref=self.artifact_ref(artifact), purpose="consult"
        )
        self.assertEqual(first_input, second_input)
        self.assertEqual(first_artifact, second_artifact)
        self.assertEqual(4, self.audit_count())

    def test_deleted_input_is_unavailable_and_dependent_artifact_is_redacted(
        self,
    ) -> None:
        source = self.make_input(input_id=INPUT_A)
        self.link_input(source, result="answered")
        artifact = self.make_artifact(
            artifact_id=ARTIFACT_A,
            sources=[self.input_ref(source)],
        )
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=source.input_id,
            ref_version=source.content_version,
            authorization_ref="authorization:delete:1",
        )
        self.store.cleanup_deletions(namespace=NS)

        # Deletion changes readability, not visibility.
        listed = self.store.list_experience_refs(self.wide, namespace=NS)
        self.assertIn(self.input_ref(source), listed.refs)
        self.assertIn(self.artifact_ref(artifact), listed.refs)

        input_view = self.store.read_experience(
            self.wide, ref=self.input_ref(source), purpose="consult"
        )
        artifact_view = self.store.read_experience(
            self.wide, ref=self.artifact_ref(artifact), purpose="consult"
        )
        self.assertIsInstance(input_view, ExperienceView)
        self.assertEqual("unavailable", input_view.readability)
        self.assertIsNone(input_view.body)
        self.assertEqual("answered", input_view.processing)
        self.assertEqual("redacted", artifact_view.readability)
        self.assertIsNone(artifact_view.body)
        self.assertEqual(
            (self.input_ref(source),), artifact_view.source_refs
        )
        self.assertEqual("answered", artifact_view.processing)
        self.assertEqual(
            ["unavailable", "redacted"],
            [row["outcome"] for row in self.audit_rows()],
        )

    def test_barrier_redacts_before_cleanup(self) -> None:
        source = self.make_input(input_id=INPUT_A)
        self.link_input(source)
        artifact = self.make_artifact(
            artifact_id=ARTIFACT_A,
            sources=[self.input_ref(source)],
        )
        input_payload = self.payload_length(
            "collab_inputs",
            "body_payload_ref",
            input_id=source.input_id,
            content_version=1,
        )
        artifact_payload = self.payload_length(
            "collab_artifacts",
            "body_payload_ref",
            artifact_id=artifact.artifact_id,
            artifact_version=1,
        )
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=source.input_id,
            ref_version=source.content_version,
            authorization_ref="authorization:delete:1",
        )
        self.assertGreater(input_payload, 0)
        self.assertGreater(artifact_payload, 0, "barrier is not cleanup")

        before_cleanup = self.store.read_experience(
            self.wide, ref=self.artifact_ref(artifact), purpose="consult"
        )
        self.assertEqual("redacted", before_cleanup.readability)
        self.assertIsNone(before_cleanup.body)
        self.store.cleanup_deletions(namespace=NS)
        self.assertEqual(
            0,
            self.payload_length(
                "collab_inputs",
                "body_payload_ref",
                input_id=source.input_id,
                content_version=1,
            ),
        )
        after_cleanup = self.store.read_experience(
            self.wide, ref=self.artifact_ref(artifact), purpose="consult"
        )
        self.assertEqual("redacted", after_cleanup.readability)
        self.assertIsNone(after_cleanup.body)
        self.assertEqual(2, self.audit_count())

    def test_unauthorized_and_missing_are_the_same_experience_unavailable(
        self,
    ) -> None:
        private = self.make_input(input_id=INPUT_A, privacy="private")
        self.link_input(private)
        artifact = self.make_artifact(
            artifact_id=ARTIFACT_A,
            sources=[self.input_ref(private)],
        )

        unauthorized_input = self.store.read_experience(
            self.strict, ref=self.input_ref(private), purpose="consult"
        )
        missing_input = self.store.read_experience(
            self.wide,
            ref={
                "ref_type": "collab_input",
                "namespace": NS,
                "input_id": MISSING_INPUT,
                "content_version": 1,
            },
            purpose="consult",
        )
        self.assertIsInstance(unauthorized_input, ExperienceUnavailable)
        self.assertIsInstance(missing_input, ExperienceUnavailable)
        self.assertEqual("unavailable", unauthorized_input.readability)
        self.assertEqual("unavailable", missing_input.readability)
        self.assertEqual(
            ["ref", "readability"],
            [field.name for field in dataclasses.fields(unauthorized_input)],
        )
        self.assertEqual(
            {"ref", "readability"},
            {field.name for field in dataclasses.fields(missing_input)},
        )

        unauthorized_artifact = self.store.read_experience(
            self.strict, ref=self.artifact_ref(artifact), purpose="consult"
        )
        missing_artifact = self.store.read_experience(
            self.wide,
            ref={
                "ref_type": "collab_artifact",
                "namespace": NS,
                "artifact_id": MISSING_ARTIFACT,
                "artifact_version": 1,
            },
            purpose="consult",
        )
        self.assertIsInstance(unauthorized_artifact, ExperienceUnavailable)
        self.assertIsInstance(missing_artifact, ExperienceUnavailable)

        # The same caller-supplied ref is fully identical for a namespace
        # denial and a genuine miss: no field distinguishes them.
        foreign_ref = {
            "ref_type": "collab_input",
            "namespace": NS_B,
            "input_id": INPUT_A,
            "content_version": 1,
        }
        namespace_denied = self.store.read_experience(
            self.wide, ref=foreign_ref, purpose="consult"
        )
        same_ref_missing = self.store.read_experience(
            self.access(namespace=NS_B), ref=foreign_ref, purpose="consult"
        )
        self.assertEqual(namespace_denied, same_ref_missing)
        self.assertEqual(0, self.audit_count())

    def test_unresolvable_source_privacy_fails_closed(self) -> None:
        source = self.make_input(input_id=INPUT_A)
        self.link_input(source)
        first = self.make_artifact(
            artifact_id=ARTIFACT_A,
            created_at="2026-10-22T00:00:01Z",
            sources=[self.input_ref(source)],
        )
        nested = self.make_artifact(
            artifact_id=ARTIFACT_B,
            created_at="2026-10-22T00:00:02Z",
            sources=[self.artifact_ref(first)],
        )
        # A dangling source link simulates a corrupted/missing source row.
        self.store.connection.execute(
            "INSERT INTO collab_source_links ("
            "namespace, owner_kind, owner_id, owner_version, source_kind, "
            "source_id, source_version, created_at"
            ") VALUES (?, 'artifact', ?, 1, 'collab_input', ?, 1, ?)",
            (NS, ARTIFACT_A, MISSING_INPUT, TS),
        )

        listed = self.store.list_experience_refs(self.wide, namespace=NS)
        self.assertEqual((self.input_ref(source),), listed.refs)
        self.assertNotIn(self.artifact_ref(first), listed.refs)
        self.assertNotIn(self.artifact_ref(nested), listed.refs)

        for artifact in (first, nested):
            with self.subTest(artifact=artifact.artifact_id):
                unavailable = self.store.read_experience(
                    self.wide,
                    ref=self.artifact_ref(artifact),
                    purpose="consult",
                )
                self.assertIsInstance(unavailable, ExperienceUnavailable)
                self.assertEqual("unavailable", unavailable.readability)
        self.assertEqual(0, self.audit_count())

    def test_malformed_ref_and_purpose_raise_invalid_input(self) -> None:
        row = self.make_input(input_id=INPUT_A)
        good = self.input_ref(row)
        bad_refs = [
            None,
            "a ref",
            [],
            {},
            {
                "ref_type": "record",
                "namespace": NS,
                "record_id": RECORD_ID,
                "record_version": 1,
            },
            {key: value for key, value in good.items() if key != "content_version"},
            dict(good, extra="field"),
            dict(good, input_id="not-a-uuid"),
            dict(good, input_id="AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
            dict(good, content_version=0),
            dict(good, content_version=True),
            dict(good, content_version="1"),
            dict(good, namespace="Not Valid"),
            {
                "ref_type": "collab_artifact",
                "namespace": NS,
                "artifact_id": ARTIFACT_A,
            },
            {
                "ref_type": "collab_artifact",
                "namespace": NS,
                "artifact_id": ARTIFACT_A,
                "artifact_version": 0,
            },
        ]
        for bad_ref in bad_refs:
            with self.subTest(ref=bad_ref):
                with self.assertRaises(InvalidInput):
                    self.store.read_experience(
                        self.wide, ref=bad_ref, purpose="consult"
                    )
        for bad_purpose in ("", None, 5, ["consult"], "x" * 65):
            with self.subTest(purpose=bad_purpose):
                with self.assertRaises(InvalidInput):
                    self.store.read_experience(
                        self.wide, ref=good, purpose=bad_purpose
                    )

    def test_unauthorized_purpose_returns_unavailable(self) -> None:
        row = self.make_input(input_id=INPUT_A)
        self.link_input(row)
        denied = self.store.read_experience(
            self.wide, ref=self.input_ref(row), purpose="delete"
        )
        self.assertIsInstance(denied, ExperienceUnavailable)
        self.assertEqual(self.input_ref(row), denied.ref)
        self.assertEqual("unavailable", denied.readability)
        self.assertEqual(0, self.audit_count())

        allowed = self.store.read_experience(
            self.wide, ref=self.input_ref(row), purpose="consult"
        )
        self.assertIsInstance(allowed, ExperienceView)
        self.assertEqual("available", allowed.readability)
        self.assertEqual(1, self.audit_count())
        self.assertEqual("consult", self.audit_rows()[0]["purpose"])

    def test_read_writes_one_audit_row_with_purpose(self) -> None:
        row = self.make_input(input_id=INPUT_A)
        self.link_input(row)
        revision = self.store.current_state_revision(NS)
        pending_work = self.table_count("pending_work")
        view = self.store.read_experience(
            self.wide, ref=self.input_ref(row), purpose="consult"
        )
        self.assertIsInstance(view, ExperienceView)
        self.assertEqual(revision, self.store.current_state_revision(NS))
        self.assertEqual(pending_work, self.table_count("pending_work"))

        rows = self.audit_rows()
        self.assertEqual(1, len(rows))
        audit = rows[0]
        self.assertRegex(audit["read_id"], r"^[0-9a-f-]{36}$")
        self.assertEqual(NS, audit["namespace"])
        self.assertEqual("host:wide", audit["host_instance"])
        self.assertEqual("authorization:wide", audit["access_ref"])
        self.assertEqual("consult", audit["purpose"])
        self.assertEqual("collab_input", audit["ref_type"])
        self.assertEqual(row.input_id, audit["ref_id"])
        self.assertEqual(1, int(audit["ref_version"]))
        self.assertEqual("available", audit["outcome"])
        self.assertRegex(audit["read_at"], r"^(?:19|20)[0-9]{2}-.*Z$")

        columns = {
            info[1]
            for info in self.store.connection.execute(
                "PRAGMA table_info(collab_experience_reads)"
            )
        }
        self.assertEqual(
            {
                "read_id",
                "namespace",
                "host_instance",
                "access_ref",
                "purpose",
                "ref_type",
                "ref_id",
                "ref_version",
                "outcome",
                "read_at",
            },
            columns,
        )
        self.assertNotIn("body", columns)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE collab_experience_reads SET outcome = 'redacted'"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "DELETE FROM collab_experience_reads"
            )

    def test_missing_read_writes_no_audit_row(self) -> None:
        missing_input = self.store.read_experience(
            self.wide,
            ref={
                "ref_type": "collab_input",
                "namespace": NS,
                "input_id": MISSING_INPUT,
                "content_version": 1,
            },
            purpose="consult",
        )
        missing_artifact = self.store.read_experience(
            self.wide,
            ref={
                "ref_type": "collab_artifact",
                "namespace": NS,
                "artifact_id": MISSING_ARTIFACT,
                "artifact_version": 1,
            },
            purpose="consult",
        )
        self.assertIsInstance(missing_input, ExperienceUnavailable)
        self.assertIsInstance(missing_artifact, ExperienceUnavailable)
        self.assertEqual(0, self.audit_count())

    def test_list_twice_creates_no_events_beliefs_or_episodes(self) -> None:
        source = self.make_input(input_id=INPUT_A)
        self.link_input(source)
        self.make_artifact(artifact_id=ARTIFACT_A)
        tables = (
            "event_journal",
            "record_versions",
            "episode_sources",
            "pending_work",
            "work_progress",
            "context_snapshots",
            "private_payloads",
            "collab_experience_reads",
        )
        before = {name: self.table_count(name) for name in tables}
        revision = self.store.current_state_revision(NS)

        self.store.list_experience_refs(self.wide, namespace=NS)
        self.store.list_experience_refs(self.wide, namespace=NS, limit=1)

        after = {name: self.table_count(name) for name in tables}
        self.assertEqual(before, after)
        self.assertEqual(revision, self.store.current_state_revision(NS))
        self.assertEqual(0, self.table_count("event_journal"))
        self.assertEqual(0, self.table_count("episode_sources"))
        self.assertEqual(
            0,
            int(
                self.store.connection.execute(
                    "SELECT COUNT(*) FROM record_versions "
                    "WHERE kind IN ('belief', 'Episode')"
                ).fetchone()[0]
            ),
        )
        self.assertEqual(0, self.table_count("collab_experience_reads"))

    def test_unlinked_input_is_not_an_experience(self) -> None:
        row = self.make_input(input_id=INPUT_A)
        self.assertEqual(
            ExperiencePage((), None),
            self.store.list_experience_refs(self.wide, namespace=NS),
        )
        unavailable = self.store.read_experience(
            self.wide, ref=self.input_ref(row), purpose="consult"
        )
        self.assertIsInstance(unavailable, ExperienceUnavailable)
        self.assertEqual("unavailable", unavailable.readability)
        self.assertEqual(0, self.audit_count())

        self.link_input(row)
        listed = self.store.list_experience_refs(self.wide, namespace=NS)
        self.assertEqual((self.input_ref(row),), listed.refs)
        view = self.store.read_experience(
            self.wide, ref=self.input_ref(row), purpose="consult"
        )
        self.assertIsInstance(view, ExperienceView)
        self.assertEqual("in_progress", view.processing)
        self.assertEqual(1, self.audit_count())

    def test_input_representative_interaction_is_stable_after_a_later_link(
        self,
    ) -> None:
        row = self.make_input(input_id=INPUT_A)
        first_context = self.make_context()
        first = self.make_interaction(
            context_id=first_context,
            created_at="2026-10-22T00:00:01Z",
            result="answered",
            input_refs=[(row.input_id, row.content_version)],
        )
        later_context = self.make_context()
        later = self.make_interaction(
            context_id=later_context,
            created_at="2026-10-22T00:00:02Z",
            result="rejected",
            input_refs=[(row.input_id, row.content_version)],
        )
        self.assertNotEqual(first, later)

        view = self.store.read_experience(
            self.wide, ref=self.input_ref(row), purpose="consult"
        )
        self.assertIsInstance(view, ExperienceView)
        self.assertEqual(first, view.interaction_id)
        self.assertEqual("answered", view.processing)
        self.assertEqual(first_context, view.context_ref)

        self.reopen()
        again = self.store.read_experience(
            self.wide, ref=self.input_ref(row), purpose="consult"
        )
        self.assertEqual(view, again)
        self.assertEqual(first, again.interaction_id)


if __name__ == "__main__":
    unittest.main()
