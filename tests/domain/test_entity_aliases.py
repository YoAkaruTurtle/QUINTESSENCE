"""J06 A1: entity aliases as append-only records (C13b minimal mechanism).

Every test writes through the real services; nothing pokes the database
directly except the two checks that deliberately look at stored state.
"""

from __future__ import annotations

import inspect
import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, NOW, CognitionTestCase, all_text_cells  # noqa: E402

from quintessence_pcs.domains.aliases import (  # noqa: E402
    ALIAS_KIND,
    AliasConflict,
    AliasNotFound,
    AliasService,
    alias_record_id,
)
from quintessence_pcs.domains.cognition.query import (  # noqa: E402
    ALL_PRIVACY,
    BeliefCurrentQuery,
    CognitionQueryService,
    QueryAccess,
)
from quintessence_pcs.collaboration.review_policy import scope_contained  # noqa: E402
from quintessence_pcs.domains.scope import scope_matches  # noqa: E402
from quintessence_pcs.foundation.errors import StorageError  # noqa: E402

ALIAS = "灰雀-47"
BOUND_BY = "host:user"


class AliasTestCase(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.aliases = AliasService(self.store)
        self.canonical = str(uuid.uuid4())

    def confirmed_alias(self, alias: str = ALIAS, canonical: str | None = None):
        target = canonical or self.canonical
        self.aliases.propose(
            NS,
            canonical_id=target,
            alias=alias,
            basis="user said so",
            decided_by=BOUND_BY,
        )
        return self.aliases.confirm(
            NS,
            alias=alias,
            basis="user confirmed the alias",
            decided_by=BOUND_BY,
        )

    # ------------------------------------------------------------ AL-01..05

    def test_al_01_proposed_then_confirmed_resolves(self) -> None:
        self.confirmed_alias()
        self.assertEqual(self.canonical, self.aliases.resolve(NS, alias=ALIAS))

    def test_al_02_proposed_only_does_not_resolve(self) -> None:
        self.aliases.propose(
            NS,
            canonical_id=self.canonical,
            alias=ALIAS,
            basis="guess",
            decided_by=BOUND_BY,
        )
        self.assertIsNone(self.aliases.resolve(NS, alias=ALIAS))

    def test_al_03_conflicting_target_is_refused(self) -> None:
        self.confirmed_alias()
        other = str(uuid.uuid4())
        with self.assertRaises(AliasConflict):
            self.aliases.propose(
                NS,
                canonical_id=other,
                alias=ALIAS,
                basis="a different page",
                decided_by=BOUND_BY,
            )
        self.assertEqual(self.canonical, self.aliases.resolve(NS, alias=ALIAS))

    def test_al_04_revoke_stops_resolution_but_keeps_history(self) -> None:
        self.confirmed_alias()
        self.aliases.revoke(
            NS, alias=ALIAS, basis="user took it back", decided_by=BOUND_BY
        )
        self.assertIsNone(self.aliases.resolve(NS, alias=ALIAS))
        history = self.aliases.history(NS, canonical_id=self.canonical)
        self.assertEqual(
            ["proposed", "confirmed", "revoked"], [item.status for item in history]
        )

    def test_al_05_versions_append_and_never_overwrite(self) -> None:
        self.confirmed_alias()
        self.aliases.revoke(
            NS, alias=ALIAS, basis="user took it back", decided_by=BOUND_BY
        )
        record_id = alias_record_id(NS, ALIAS)
        pointer = self.store.record_pointer(NS, record_id)
        self.assertIsNotNone(pointer)
        self.assertEqual(ALIAS_KIND, pointer.kind)
        self.assertEqual(3, pointer.current_version)
        versions = [
            self.store.get_version(NS, record_id, number)
            for number in range(1, pointer.current_version + 1)
        ]
        self.assertEqual(
            ["proposed", "confirmed", "revoked"],
            [version.body["status"] for version in versions],
        )
        # The first version is still readable and unchanged: history is append-only.
        self.assertEqual("proposed", versions[0].body["status"])
        self.assertEqual("user said so", versions[0].body["basis"])

    # ------------------------------------------------------------ AL-06..09

    def test_al_06_write_side_stores_the_canonical_id(self) -> None:
        self.confirmed_alias()
        version = self.propose(
            body=self.belief_body(claim_type="general_hypothesis"),
            scope={"scene": "test.scene", "entity_ids": [ALIAS]},
        )
        stored = self.store.get_version(NS, version.record_id, version.record_version)
        self.assertEqual([self.canonical], stored.scope["entity_ids"])

    def test_al_07_query_side_expands_the_alias(self) -> None:
        self.confirmed_alias()
        event = self.ingest()
        version = self.propose(
            body=self.belief_body(
                claim_type="capability",
                evidence_links=[self.evidence_link(self.event_ref(event.event_id))],
            ),
            scope={"scene": "test.scene", "entity_ids": [self.canonical]},
        )
        result = self.query(entity_ids=[ALIAS])
        self.assertEqual("ok", result.status)
        self.assertIn(version.record_id, [item.ref.record_id for item in result.items])

    def test_al_08_alias_does_not_widen_scope_containment(self) -> None:
        self.confirmed_alias()
        # Exact semantics untouched: an authorised scope naming the alias does
        # not authorise a candidate naming the canonical id and vice versa.
        self.assertFalse(
            scope_contained(
                {"entity_ids": [self.canonical]}, {"entity_ids": [ALIAS]}
            )
        )
        self.assertFalse(
            scope_contained(
                {"entity_ids": [ALIAS]}, {"entity_ids": [self.canonical]}
            )
        )
        # And the matcher itself still compares literal values only.
        self.assertFalse(
            scope_matches({"entity_ids": [self.canonical]}, {"entity_ids": [ALIAS]})
        )

    def test_al_09_expansion_does_not_leak_other_entities(self) -> None:
        self.confirmed_alias()
        stranger = str(uuid.uuid4())
        event = self.ingest()
        hidden = self.propose(
            body=self.belief_body(
                claim="capability:other",
                evidence_links=[self.evidence_link(self.event_ref(event.event_id))],
            ),
            scope={"scene": "test.scene", "entity_ids": [stranger]},
        )
        result = self.query(entity_ids=[ALIAS])
        self.assertNotIn(hidden.record_id, [item.ref.record_id for item in result.items])

    # ----------------------------------------------------------- AL-10..11

    def test_al_10_deleting_the_alias_record_erases_its_body(self) -> None:
        self.confirmed_alias()
        record_id = alias_record_id(NS, ALIAS)
        self.delete_record(record_id)
        cells = [value for _, value in all_text_cells(self.store.connection)]
        self.assertNotIn(ALIAS, cells)
        self.assertNotIn("user confirmed the alias", cells)
        with self.assertRaises(StorageError):
            self.aliases.resolve(NS, alias=ALIAS)

    def test_al_11_matching_layer_has_no_alias_notion(self) -> None:
        source = inspect.getsource(scope_matches) + inspect.getsource(scope_contained)
        self.assertNotIn("AliasService", source)
        self.assertNotIn("canonical", source.lower())
        self.assertEqual(
            ["record_scope", "query_scope"],
            list(inspect.signature(scope_matches).parameters),
        )

    # ------------------------------------------------------------- helpers

    def query(self, *, entity_ids: list[str]):
        service = CognitionQueryService(self.store, clock=lambda: NOW)
        access = QueryAccess(
            namespace=NS,
            privacy_classes=ALL_PRIVACY,
            purposes=frozenset({"consult"}),
        )
        return service.query_current_beliefs(
            access,
            query=BeliefCurrentQuery(
                scope={"scene": "test.scene", "entity_ids": entity_ids},
                include_expired=True,
                limit=20,
            ),
        )


class AliasEdgeCaseTests(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.aliases = AliasService(self.store)
        self.canonical = str(uuid.uuid4())

    def confirmed_alias(self, alias: str = ALIAS, canonical: str | None = None):
        target = canonical or self.canonical
        self.aliases.propose(
            NS,
            canonical_id=target,
            alias=alias,
            basis="user said so",
            decided_by=BOUND_BY,
        )
        return self.aliases.confirm(
            NS,
            alias=alias,
            basis="user confirmed the alias",
            decided_by=BOUND_BY,
        )

    def test_confirm_unknown_alias_is_not_found(self) -> None:
        with self.assertRaises(AliasNotFound):
            self.aliases.confirm(
                NS, alias="never-seen", basis="x", decided_by=BOUND_BY
            )

    def test_propose_after_revoke_needs_a_new_basis(self) -> None:
        self.confirmed_alias()
        self.aliases.revoke(NS, alias=ALIAS, basis="withdrawn", decided_by=BOUND_BY)
        with self.assertRaises(AliasConflict):
            self.aliases.propose(
                NS,
                canonical_id=self.canonical,
                alias=ALIAS,
                basis="same again",
                decided_by=BOUND_BY,
            )

    def test_expand_keeps_the_literal_alias_for_old_records(self) -> None:
        self.confirmed_alias()
        expanded = self.aliases.expand(NS, aliases=[ALIAS])
        self.assertEqual({ALIAS, self.canonical}, set(expanded))
        unknown = ALIAS + "-x"
        self.assertEqual({unknown}, set(self.aliases.expand(NS, aliases=[unknown])))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
