"""Shared fixtures for the PCS2-007 domain tests.

Every test gets a fresh database in a temporary directory and only synthetic
data crosses the API.  No test reads private persona assets, writes
private instance directories, calls a model or touches the network.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
STORE_TESTS = HERE.parent / "store"
SRC_DIR = HERE.parents[1] / "src"
for _path in (HERE, STORE_TESTS, SRC_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from support import all_text_cells  # noqa: E402,F401  (tests/store/support.py)

from quintessence_pcs.domains import (  # noqa: E402
    EpisodeService,
    PersonaProfileService,
)
from quintessence_pcs.domains.persona.profiles import (  # noqa: E402
    REVIEW_APPROVE_EVENT_TYPE,
)
from quintessence_pcs.store.domain import DomainStore  # noqa: E402
from quintessence_pcs.store.events import IngestReceipt  # noqa: E402
from quintessence_pcs.store.records import RecordVersion  # noqa: E402

NS = "example.test.domain"
NS_B = "example.test.domain.other"
RECORDED_AT = "2026-10-17T00:00:00Z"
OBSERVED_AT = "2026-10-17T01:02:03Z"
PROFILE_ID = "example.synthetic.p0"
PROFILE_ID_B = "example.synthetic.p1"


class DomainTestCase(unittest.TestCase):
    """Fresh ``DomainStore`` plus the two services for one test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pcs2-007-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.db_path = self.tmp_path / "pcs.sqlite"
        self.store = DomainStore.open(self.db_path)
        self.addCleanup(self.store.close)
        self.persona = PersonaProfileService(self.store)
        self.episodes = EpisodeService(self.store)

    # ------------------------------------------------------------- lifecycle

    def open_second_store(self) -> DomainStore:
        store = DomainStore.open(self.db_path)
        self.addCleanup(store.close)
        return store

    # ----------------------------------------------------------------- events

    def ingest(
        self,
        *,
        namespace: str = NS,
        source_kind: str = "synthetic_fixture",
        source_instance: str = "domain-tests",
        source_event_id: str | None = None,
        event_type: str = "ObservationRecorded",
        payload: Any = None,
        observed_at: str | None = OBSERVED_AT,
        recorded_at: str = RECORDED_AT,
    ) -> IngestReceipt:
        return self.store.ingest_event(
            namespace=namespace,
            source_kind=source_kind,
            source_instance=source_instance,
            source_event_id=source_event_id or f"source-{uuid.uuid4()}",
            event_type=event_type,
            observed_at=observed_at,
            payload={"text": "synthetic event body"} if payload is None else payload,
            recorded_at=recorded_at,
        )

    def approve_review(
        self,
        *,
        namespace: str = NS,
        source_kind: str = "user_report",
        event_type: str = REVIEW_APPROVE_EVENT_TYPE,
        source_event_id: str | None = None,
        payload: Any = None,
    ) -> dict[str, Any]:
        receipt = self.ingest(
            namespace=namespace,
            source_kind=source_kind,
            source_instance="admin-fixture",
            source_event_id=source_event_id or f"review-{uuid.uuid4()}",
            event_type=event_type,
            payload={"decision": "approve", "synthetic": True} if payload is None else payload,
        )
        return {
            "ref_type": "event",
            "namespace": namespace,
            "event_id": receipt.event_id,
        }

    # -------------------------------------------------------------- profiles

    def profile_body(
        self,
        *,
        review_ref: dict[str, Any],
        profile_id: str = PROFILE_ID,
        profile_version: str = "0.0.1",
        identity_summary: str = "Synthetic profile used only to test the mechanism.",
    ) -> dict[str, Any]:
        return {
            "profile_id": profile_id,
            "profile_version": profile_version,
            "identity_summary": identity_summary,
            "principles": [
                "State what is unknown instead of inventing an answer.",
                "Do not claim a tool ran without evidence.",
            ],
            "style": {
                "language": "zh-CN",
                "tone": "plain",
                "verbosity_by_context": {"casual": "short", "technical": "detailed"},
            },
            "attention_preferences": [
                {"topic": "mechanism testing", "reason": "synthetic fixture"}
            ],
            "examples": [
                {
                    "example_id": "synthetic-example-1",
                    "scene": "synthetic mechanism test",
                    "user": "Synthetic user line.",
                    "assistant": "Synthetic assistant line.",
                    "synthetic": True,
                }
            ],
            "asset_hash": "0" * 64,
            "review_ref": review_ref,
            "renderer_compatibility": "pcs.renderer.synthetic-v1",
        }

    def publish(
        self,
        *,
        namespace: str = NS,
        profile_id: str = PROFILE_ID,
        body: dict[str, Any] | None = None,
        review_ref: dict[str, Any] | None = None,
        authorization_ref: str = "auth:profile-publish",
        profile_version: str = "0.0.1",
        identity_summary: str = "Synthetic profile used only to test the mechanism.",
    ) -> RecordVersion:
        if review_ref is None:
            review_ref = self.approve_review(namespace=namespace)
        if body is None:
            body = self.profile_body(
                review_ref=review_ref,
                profile_id=profile_id,
                profile_version=profile_version,
                identity_summary=identity_summary,
            )
        return self.persona.publish_profile(
            namespace=namespace,
            profile_id=profile_id,
            body=body,
            review_ref=review_ref,
            authorization_ref=authorization_ref,
        )

    def activate(
        self,
        version: RecordVersion,
        *,
        namespace: str = NS,
        authorization_ref: str = "auth:profile-activate",
    ) -> None:
        self.persona.activate_profile(
            namespace=namespace,
            record_id=version.record_id,
            record_version=version.record_version,
            authorization_ref=authorization_ref,
        )

    # -------------------------------------------------------------- episodes

    def build_episode(
        self,
        event_id: str,
        *,
        namespace: str = NS,
        scope: dict[str, Any] | None = None,
        authorization_ref: str = "auth:episode-build",
    ) -> RecordVersion:
        return self.episodes.build_episode_from_event(
            namespace=namespace,
            event_id=event_id,
            scope={} if scope is None else scope,
            authorization_ref=authorization_ref,
        )

    def search(
        self,
        *,
        query: str,
        namespace: str = NS,
        scope: dict[str, Any] | None = None,
        **kwargs: Any,
    ):
        return self.episodes.search_episodes(
            namespace=namespace,
            query=query,
            scope={} if scope is None else scope,
            **kwargs,
        )

    # ------------------------------------------------------------- diagnostics

    def revision(self, namespace: str = NS) -> int:
        return self.store.current_state_revision(namespace)

    def watermarks(self, namespace: str = NS) -> tuple[int, int, int]:
        row = self.store.connection.execute(
            "SELECT state_revision, last_event_seq, privacy_epoch "
            "FROM namespace_state WHERE namespace = ?",
            (namespace,),
        ).fetchone()
        if row is None:
            return (0, 0, 0)
        return (
            int(row["state_revision"]),
            int(row["last_event_seq"]),
            int(row["privacy_epoch"]),
        )

    def audit_rows(self, namespace: str = NS) -> list[Any]:
        return self.store.connection.execute(
            "SELECT operation, target_kind, target_id, target_version, "
            "authorization_ref FROM authorization_audit "
            "WHERE namespace = ? ORDER BY recorded_at, operation",
            (namespace,),
        ).fetchall()

    def count_rows(self, table: str) -> int:
        return int(
            self.store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )

    def delete_record(self, record_id: str, *, namespace: str = NS) -> None:
        self.store.request_deletion(
            target_kind="record",
            namespace=namespace,
            target_id=record_id,
            authorization_ref="auth:test-delete",
        )

    def delete_event(self, event_id: str, *, namespace: str = NS) -> None:
        self.store.request_deletion(
            target_kind="event",
            namespace=namespace,
            target_id=event_id,
            authorization_ref="auth:test-delete",
        )


__all__ = [
    "DomainTestCase",
    "NS",
    "NS_B",
    "OBSERVED_AT",
    "PROFILE_ID",
    "PROFILE_ID_B",
    "RECORDED_AT",
    "all_text_cells",
]
