"""RG07: the off/on/auto expression-mode policy.

The policy is pure: these tests prove the resolution table, the gate order, the
request reserve and that ``auto`` is a recorded "not implemented" skip rather
than a hidden trigger.  No store, no model, no network.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1] / "src"
for _path in (HERE, SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from quintessence_pcs.conversation.sdrpe import (  # noqa: E402
    AUTO_IMPLEMENTED,
    MIN_EXPRESSION_WALL_SECONDS,
    SKIP_AUTO_NOT_IMPLEMENTED,
    SKIP_BUDGET,
    SKIP_MODE_OFF,
    SKIP_NO_RESERVE,
    SKIP_PROTOCOL_V1,
    SKIP_WAIT_QUESTION,
    SKIP_WALL,
    SKIP_WORK_MODE,
    SdrpePlan,
    content_request_cap,
    decide,
    skip,
)


def plan(mode: str, *, protocol: str = "collab.v2") -> SdrpePlan:
    return SdrpePlan.resolve(mode, protocol_version=protocol)


def run(plan_value: SdrpePlan, **overrides):
    values = {
        "plan": plan_value,
        "turn_mode": "chat",
        "content_action": "final",
        "protocol_version": "collab.v2",
        "max_model_requests": 8,
        "requests_used": 1,
        "wall_left": 120.0,
    }
    values.update(overrides)
    return decide(**values)


class SdrpePlanTests(unittest.TestCase):
    def test_off_and_on_resolve_to_themselves(self) -> None:
        off = plan("off")
        self.assertEqual(("off", "off"), (off.requested_mode, off.effective_mode))
        self.assertIsNone(off.resolved_reason)
        on = plan("on")
        self.assertEqual(("on", "on"), (on.requested_mode, on.effective_mode))
        self.assertIsNone(on.resolved_reason)

    def test_auto_is_reserved_and_resolves_to_off(self) -> None:
        self.assertFalse(AUTO_IMPLEMENTED)
        auto = plan("auto")
        self.assertEqual("auto", auto.requested_mode)
        self.assertEqual("off", auto.effective_mode)
        self.assertEqual(SKIP_AUTO_NOT_IMPLEMENTED, auto.resolved_reason)

    def test_collab_v1_never_enables_the_stage(self) -> None:
        for mode in ("on", "auto"):
            with self.subTest(mode=mode):
                resolved = plan(mode, protocol="collab.v1")
                self.assertEqual("off", resolved.effective_mode)
                self.assertEqual(SKIP_PROTOCOL_V1, resolved.resolved_reason)

    def test_strategy_round_trip_and_legacy_string(self) -> None:
        resolved = plan("on")
        restored = SdrpePlan.from_strategy(
            resolved.to_strategy(), protocol_version="collab.v2"
        )
        self.assertEqual(resolved, restored)
        legacy = SdrpePlan.from_strategy("off", protocol_version="collab.v2")
        self.assertEqual("off", legacy.effective_mode)
        unknown = SdrpePlan.from_strategy(
            {"requested_mode": "sometimes"}, protocol_version="collab.v2"
        )
        self.assertEqual("off", unknown.effective_mode)
        self.assertIsNone(
            SdrpePlan.from_strategy(None, protocol_version="collab.v2").resolved_reason
        )

    def test_impossible_plans_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            SdrpePlan(requested_mode="sometimes", effective_mode="off")
        with self.assertRaises(ValueError):
            SdrpePlan(requested_mode="off", effective_mode="on")
        with self.assertRaises(ValueError):
            SdrpePlan(
                requested_mode="on",
                effective_mode="off",
                resolved_reason="not_a_reason",
            )
        with self.assertRaises(ValueError):
            skip("not_a_reason")

    def test_content_request_cap_reserves_one_only_when_on(self) -> None:
        self.assertEqual(8, content_request_cap(plan("off"), 8))
        self.assertEqual(7, content_request_cap(plan("on"), 8))
        self.assertEqual(1, content_request_cap(plan("on"), 1))
        self.assertEqual(1, content_request_cap(plan("on"), 2))


class ExpressionDecisionTests(unittest.TestCase):
    def test_on_chat_final_runs(self) -> None:
        decision = run(plan("on"))
        self.assertTrue(decision.run)
        self.assertIsNone(decision.reason)
        self.assertEqual("sdrpe.v3", decision.policy_version)

    def test_off_never_runs_and_keeps_its_reason(self) -> None:
        self.assertEqual(SKIP_MODE_OFF, run(plan("off")).reason)
        auto = plan("auto")
        self.assertEqual(SKIP_AUTO_NOT_IMPLEMENTED, run(auto).reason)

    def test_gate_order_is_fixed(self) -> None:
        # protocol before turn mode before action before budget before wall
        self.assertEqual(
            SKIP_PROTOCOL_V1,
            run(plan("on"), protocol_version="collab.v1", turn_mode="work").reason,
        )
        self.assertEqual(
            SKIP_WORK_MODE,
            run(plan("on"), turn_mode="work", content_action="wait").reason,
        )
        self.assertEqual(
            SKIP_WAIT_QUESTION,
            run(plan("on"), content_action="wait", max_model_requests=1).reason,
        )
        self.assertEqual(
            SKIP_NO_RESERVE,
            run(plan("on"), max_model_requests=1).reason,
        )
        self.assertEqual(
            SKIP_BUDGET,
            run(plan("on"), max_model_requests=2, requests_used=2).reason,
        )
        self.assertEqual(
            SKIP_WALL,
            run(plan("on"), wall_left=MIN_EXPRESSION_WALL_SECONDS - 0.1).reason,
        )

    def test_minimum_wall_is_inclusive(self) -> None:
        decision = run(plan("on"), wall_left=MIN_EXPRESSION_WALL_SECONDS)
        self.assertTrue(decision.run)
        self.assertFalse(run(plan("on"), wall_left=0.0).run)


if __name__ == "__main__":
    unittest.main()
