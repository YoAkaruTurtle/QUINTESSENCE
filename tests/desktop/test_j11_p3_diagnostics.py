"""P3: the model-call diagnostics must separate the three budgets.

The refusal behaviour is unchanged; these tests only pin that a caller can tell
"out of calls" from "the Context is too big" from "the request is too big" —
because the recovery differs for each.
"""

from __future__ import annotations

import unittest

from quintessence_pcs.runtime.model_port import (
    ModelBudgetExceededError,
    ModelNotAuthorizedError,
)
from quintessence_pcs.store.model import (
    ModelAuthorizationExpired,
    ModelAuthorizationMismatch,
    ModelBudgetExceeded,
    ModelNotAuthorized,
)

try:  # the bridge needs the sidecar directory on sys.path
    from pcs_bridge import _map_model_error  # type: ignore
except Exception:  # pragma: no cover - collection guard for path setups
    _map_model_error = None  # type: ignore


@unittest.skipIf(_map_model_error is None, "sidecar path not on sys.path")
class BudgetDiagnosticsTests(unittest.TestCase):
    def code(self, exc: BaseException) -> str:
        return _map_model_error(exc, default_unknown=True)[0]

    def message(self, exc: BaseException) -> str:
        return _map_model_error(exc, default_unknown=True)[1]

    def test_call_budget_asks_for_a_new_authorization(self) -> None:
        exc = ModelBudgetExceededError("used 8 of 8", reason="calls_exhausted")
        self.assertEqual(self.code(exc), "MODEL_CALLS_EXHAUSTED")
        self.assertIn("新的授权", self.message(exc))

    def test_injection_budget_points_at_the_context(self) -> None:
        exc = ModelBudgetExceededError("injected 9000 U", reason="injection_budget")
        self.assertEqual(self.code(exc), "MODEL_CONTEXT_BUDGET_EXCEEDED")
        self.assertIn("上下文", self.message(exc))

    def test_request_budget_points_at_the_whole_request(self) -> None:
        exc = ModelBudgetExceededError("needs 9000 U", reason="request_budget")
        self.assertEqual(self.code(exc), "MODEL_REQUEST_BUDGET_EXCEEDED")
        self.assertIn("请求", self.message(exc))

    def test_a_reasonless_budget_error_still_reports_budget(self) -> None:
        self.assertEqual(
            self.code(ModelBudgetExceededError("budget")), "MODEL_BUDGET_EXCEEDED"
        )

    def test_an_expired_authorization_is_not_a_generic_refusal(self) -> None:
        exc = ModelNotAuthorizedError("expired", reason="authorization_expired")
        self.assertEqual(self.code(exc), "MODEL_AUTHORIZATION_EXPIRED")
        self.assertIn("过期", self.message(exc))

    def test_a_mismatched_authorization_names_the_binding(self) -> None:
        exc = ModelNotAuthorizedError("mismatch", reason="authorization_mismatch")
        self.assertEqual(self.code(exc), "MODEL_AUTHORIZATION_MISMATCH")

    def test_a_plain_refusal_stays_generic(self) -> None:
        self.assertEqual(
            self.code(ModelNotAuthorizedError("no grant")), "MODEL_NOT_AUTHORIZED"
        )

    def test_messages_never_carry_credentials(self) -> None:
        exc = ModelNotAuthorizedError(
            "no grant for sk-abcdef", reason="authorization_expired"
        )
        self.assertNotIn("sk-abcdef", self.message(exc))


class StoreTranslationTests(unittest.TestCase):
    """The ledger's distinct causes must survive translation to the port."""

    def translate(self, exc: BaseException):
        from quintessence_pcs.runtime.http_model import _translate_store_error

        return _translate_store_error(exc)  # type: ignore[arg-type]

    def test_expired_keeps_its_reason(self) -> None:
        port = self.translate(ModelAuthorizationExpired("expired"))
        self.assertEqual(getattr(port, "reason", None), "authorization_expired")

    def test_mismatch_keeps_its_reason(self) -> None:
        port = self.translate(ModelAuthorizationMismatch("mismatch"))
        self.assertEqual(getattr(port, "reason", None), "authorization_mismatch")

    def test_calls_and_walltime_are_one_bucket(self) -> None:
        port = self.translate(ModelBudgetExceeded("used 8 of 8"))
        self.assertEqual(getattr(port, "reason", None), "calls_or_walltime")

    def test_plain_not_authorized_stays_generic(self) -> None:
        port = self.translate(ModelNotAuthorized("none"))
        self.assertEqual(getattr(port, "reason", None), "not_authorized")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
