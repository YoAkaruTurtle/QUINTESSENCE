"""J06 S6b: AC-H1 -- the strict Scope containment used by the impact policy.

`scope_contained(candidate, authorized)` must mean exactly "every query the
candidate applies to is also covered by the authorization".  The reference
semantics is `scope_matches`, so the property test compares the two directly over
a synthetic universe instead of trusting a hand-written truth table.
"""

from __future__ import annotations

import itertools
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from quintessence_pcs.collaboration.review_policy import scope_contained  # noqa: E402
from quintessence_pcs.domains.scope import scope_matches  # noqa: E402


class ScopeContainmentTests(unittest.TestCase):
    def test_h1_1_candidate_missing_an_authorized_constraint_is_broader(self) -> None:
        authorized = {"project_id": "A", "scene": "x"}
        candidate = {"project_id": "A"}
        self.assertFalse(scope_contained(candidate, authorized))

    def test_h1_2_candidate_with_an_extra_constraint_is_narrower(self) -> None:
        authorized = {"project_id": "A"}
        candidate = {"project_id": "A", "scene": "x"}
        self.assertTrue(scope_contained(candidate, authorized))

    def test_h1_3_empty_scope_on_either_side_is_refused(self) -> None:
        self.assertFalse(scope_contained({}, {"project_id": "A"}))
        self.assertFalse(scope_contained({"project_id": "A"}, {}))
        self.assertFalse(scope_contained({}, {}))

    def test_h1_4_a_different_project_is_not_contained(self) -> None:
        self.assertFalse(
            scope_contained({"project_id": "B"}, {"project_id": "A"})
        )

    def test_h1_6_entity_ids_use_subset_semantics(self) -> None:
        authorized = {"project_id": "A", "entity_ids": ["e1"]}
        self.assertTrue(
            scope_contained({"project_id": "A", "entity_ids": ["e1", "e2"]}, authorized)
        )
        self.assertFalse(
            scope_contained(
                {"project_id": "A", "entity_ids": ["e1"]},
                {"project_id": "A", "entity_ids": ["e1", "e2"]},
            )
        )

    def test_h1_5_containment_equals_denotation_subset(self) -> None:
        projects: tuple[str | None, ...] = (None, "A", "B")
        scenes: tuple[str | None, ...] = (None, "x", "y")
        entity_sets: tuple[tuple[str, ...] | None, ...] = (
            None,
            ("e1",),
            ("e2",),
            ("e1", "e2"),
        )

        scopes: list[dict[str, object]] = []
        for project, scene, entities in itertools.product(projects, scenes, entity_sets):
            scope: dict[str, object] = {}
            if project is not None:
                scope["project_id"] = project
            if scene is not None:
                scope["scene"] = scene
            if entities is not None:
                scope["entity_ids"] = list(entities)
            scopes.append(scope)

        # A query universe wide enough to separate every scope pair.
        queries = scopes

        # An empty authorization is not a bound at all: the policy refuses it
        # before the denotation rule applies, so it is asserted separately.
        for candidate in scopes:
            self.assertFalse(scope_contained(candidate, {}))

        checked = 0
        for candidate, authorized in itertools.product(scopes, scopes):
            if not authorized:
                continue
            expected = all(
                (not scope_matches(candidate, query))
                or scope_matches(authorized, query)
                for query in queries
            )
            actual = scope_contained(candidate, authorized)
            self.assertEqual(
                expected,
                actual,
                f"scope_contained({candidate}, {authorized}) disagreed with "
                "the scope_matches denotation",
            )
            checked += 1
        self.assertGreater(checked, 500)


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
