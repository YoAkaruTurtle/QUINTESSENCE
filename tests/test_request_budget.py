"""J03 request-budget unit and gate tests."""

from __future__ import annotations

import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

_SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from quintessence_pcs.runtime.budget import (
    INJECTION_BUDGET_UNITS,
    SAFETY_MARGIN_UNITS,
    available_injection_units,
    estimate_units,
    fits_request,
    quarter_units,
    request_units,
)
from quintessence_pcs.runtime.http_model import DirectHttpModel
from quintessence_pcs.runtime.manifest import ModelManifest

from tests.domain.cognition_support import NS as CONTEXT_NS, CognitionTestCase
from quintessence_pcs.store.errors import ContextTooLarge

NAMESPACE = "example.test.budget"
MODEL_KEY_ENV = "QUINTESSENCE_J03_TEST_MODEL_KEY"

MANIFEST = {
    "provider": "synthetic",
    "base_url": "http://127.0.0.1:9/v1",
    "model": "budget-test-model",
    "api_key_env": MODEL_KEY_ENV,
    "max_calls": 4,
    "max_output_tokens_per_call": 512,
    "max_wall_seconds": 60,
    "thinking": "disabled",
    "temperature": 0,
}


class _Authorization:
    authorization_id = "authorization:j03-test"
    max_output_tokens_per_call = 512


class _Store:
    def __init__(self) -> None:
        self.reserved = 0

    def active_model_authorization(self, **kwargs):  # noqa: ANN003
        del kwargs
        return _Authorization()

    def reserve_model_attempt(self, **kwargs):  # noqa: ANN003
        del kwargs
        self.reserved += 1
        raise AssertionError("the budget gate must run before the reservation")


class _Context:
    namespace = NAMESPACE
    adapter_kind = "direct-http"
    run_id = "run-j03"
    context_id = "context-j03"
    rendered_input = "abcd"


class UnitTests(unittest.TestCase):
    def test_ascii_letters_digits_and_fixed_whitespace(self) -> None:
        self.assertEqual(1, estimate_units("abcd"))
        self.assertEqual(1, estimate_units("ab c"))
        self.assertEqual(4, quarter_units("abcd"))
        for whitespace in (" ", "\t", "\n", "\v", "\f", "\r"):
            self.assertEqual(1, quarter_units(whitespace), whitespace)

    def test_ascii_punctuation_and_controls(self) -> None:
        self.assertEqual(3, estimate_units("!!!"))
        self.assertEqual(1, estimate_units("\x00"))
        self.assertEqual(1, estimate_units("\x1c"))
        self.assertEqual(1, estimate_units("\x1f"))

    def test_cjk_and_non_ascii_letters(self) -> None:
        self.assertEqual(1, estimate_units("中"))
        self.assertEqual(1, estimate_units("é"))
        self.assertEqual(1, estimate_units("\u0301"))

    def test_symbols_and_controls_above_ascii(self) -> None:
        self.assertEqual(2, estimate_units("\u00b4"))
        self.assertEqual(4, estimate_units("\U0001f600"))
        self.assertEqual(2, estimate_units("\u0085"))

    def test_mixed_and_empty(self) -> None:
        self.assertEqual(3, estimate_units("ab中!"))
        self.assertEqual(1, estimate_units(""))

    def test_budget_arithmetic(self) -> None:
        self.assertEqual(10 + 5 + SAFETY_MARGIN_UNITS, request_units(10, 5))
        self.assertTrue(fits_request(10, 10 + 5 + SAFETY_MARGIN_UNITS, output_reserve_units=5))
        self.assertFalse(
            fits_request(11, 10 + 5 + SAFETY_MARGIN_UNITS, output_reserve_units=5)
        )
        self.assertEqual(
            INJECTION_BUDGET_UNITS,
            available_injection_units(INJECTION_BUDGET_UNITS + 10_000),
        )
        self.assertEqual(
            1000 - SAFETY_MARGIN_UNITS,
            available_injection_units(1000),
        )


class GateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ, {MODEL_KEY_ENV: "test-key"})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.manifest = ModelManifest.from_mapping(MANIFEST)

    def _model(self, *, request_budget_units, output_reserve_units=0):
        store = _Store()
        model = DirectHttpModel(
            store,  # type: ignore[arg-type]
            self.manifest,
            request_budget_units=request_budget_units,
            output_reserve_units=output_reserve_units,
        )
        return model, store

    def test_over_budget_refuses_before_reservation_and_send(self) -> None:
        model, store = self._model(request_budget_units=300, output_reserve_units=0)
        context = _Context()
        with model.bind_run(namespace=NAMESPACE, context=context):
            with self.assertRaises(Exception) as caught:
                model.propose(rendered_input=context.rendered_input, references=())
        self.assertEqual("BUDGET_EXCEEDED", getattr(caught.exception, "error_label", None))
        self.assertEqual(0, store.reserved)

    def test_gate_accepts_a_body_inside_the_budget(self) -> None:
        model, _store = self._model(request_budget_units=200_000, output_reserve_units=4096)
        model._require_request_budget("abcd", "abcd")

    def test_missing_budget_is_not_silently_configured(self) -> None:
        model, _store = self._model(request_budget_units=None)
        model._require_request_budget("abcd", "abcd")

    def test_gate_checks_injection_units_from_budget_view(self) -> None:
        model, store = self._model(request_budget_units=200_000, output_reserve_units=0)
        context = types.SimpleNamespace(budget_view={"injection_units": 5000})
        with self.assertRaises(Exception) as caught:
            model._require_request_budget("abcd", "abcd", context=context)
        self.assertEqual("BUDGET_EXCEEDED", getattr(caught.exception, "error_label", None))
        self.assertEqual(0, store.reserved)

    def test_legacy_context_without_injection_units_is_marked_unknown(self) -> None:
        model, _store = self._model(request_budget_units=200_000, output_reserve_units=0)
        context = types.SimpleNamespace(budget_view={})
        model._require_request_budget("abcd", "abcd", context=context)
        self.assertIs(False, context.budget_view["injection_units_known"])


class _SettleStore(_Store):
    def __init__(self) -> None:
        super().__init__()
        self.settled: dict | None = None

    def finish_model_attempt(self, **kwargs):  # noqa: ANN003
        self.settled = kwargs
        return types.SimpleNamespace(attempt_id=kwargs.get("attempt_id"))


class EstimateRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = ModelManifest.from_mapping(MANIFEST)

    def test_gate_records_the_estimate_and_finish_attaches_it(self) -> None:
        store = _SettleStore()
        model = DirectHttpModel(
            store,  # type: ignore[arg-type]
            self.manifest,
            request_budget_units=200_000,
            output_reserve_units=4096,
        )
        model._require_request_budget("abcd", "abcd")
        estimate = model._pcs_estimate
        self.assertIsNotNone(estimate)
        self.assertEqual(
            {
                "body_units",
                "injected_units",
                "request_budget_units",
                "output_reserve_units",
                "request_units",
                "safety_margin_units",
            },
            set(estimate),
        )
        self.assertEqual(SAFETY_MARGIN_UNITS, estimate["safety_margin_units"])
        self.assertEqual(200_000, estimate["request_budget_units"])
        model._finish(types.SimpleNamespace(attempt_id="attempt-1"), outcome="failed")
        self.assertEqual(estimate, store.settled["pcs_estimated"])


class ContextBudgetTests(CognitionTestCase):
    """AC-12: the existing Context trim loop also honours the U budget."""

    def test_required_material_over_budget_is_refused(self) -> None:
        trigger = self.ingest()
        with self.assertRaises(ContextTooLarge):
            self.contexts.build_context(
                namespace=CONTEXT_NS,
                trigger_ref=self.event_ref(trigger.event_id),
                request_budget_units=1,
                output_reserve_units=0,
            )

    def test_context_without_a_request_budget_is_unchanged(self) -> None:
        trigger = self.ingest()
        envelope = self.contexts.build_context(
            namespace=CONTEXT_NS,
            trigger_ref=self.event_ref(trigger.event_id),
        )
        self.assertTrue(envelope.rendered_input)

    def test_optional_material_is_measured_below_the_injection_cap(self) -> None:
        """Five Episodes render as references and stay under the cap.

        This is a measurement baseline, not a claim that A is unreachable: a
        facts-only Context can exceed A (see the punctuation-facts test below).
        """

        from quintessence_pcs.domains import EpisodeService

        episodes = EpisodeService(self.store)
        for index in range(5):
            source = self.ingest(
                event_type="CapabilityObserved:file-read",
                payload={"text": f"{index}-" + "z" * 4000},
            )
            self.complete_work_as_wait(source.event_id)
            episodes.build_episode_from_event(
                namespace=CONTEXT_NS,
                event_id=source.event_id,
                scope={},
                authorization_ref=f"auth:j03-episode-{index}",
            )
        question = self.ingest(
            source_kind="user_report",
            event_type="exp01.question",
            payload={"question": "合成问题", "scenario": "A", "synthetic_fixture": True},
        )
        reference = {
            "namespace": CONTEXT_NS,
            "trigger_ref": self.event_ref(question.event_id),
            "adapter_kind": "direct-http",
        }
        full = self.contexts.build_context(**reference)
        self.assertLess(full.budget_view["injection_units"], 4096)
        # A huge R does not trim anything, because the injection cap is not
        # the binding constraint for this Context.
        same = self.contexts.build_context(
            **reference, request_budget_units=200_000, output_reserve_units=0
        )
        self.assertEqual(
            len(json.loads(same.rendered_input).get("episodes", [])),
            len(json.loads(full.rendered_input).get("episodes", [])),
        )

    def test_injection_cap_trims_punctuation_facts_when_request_fits(self) -> None:
        question = self.ingest(
            source_kind="user_report",
            event_type="exp01.question",
            payload={"question": "合成问题", "scenario": "A", "synthetic_fixture": True},
        )
        for _index in range(4):
            self.ingest(event_type="TestEvidence", payload={"text": "!" * 1500})
        reference = {
            "namespace": CONTEXT_NS,
            "trigger_ref": self.event_ref(question.event_id),
            "adapter_kind": "direct-http",
        }
        # R is far larger than the request, so only A can force the trim.
        trimmed = self.contexts.build_context(
            **reference, request_budget_units=200_000, output_reserve_units=0
        )
        document = json.loads(trimmed.rendered_input)
        self.assertLess(len(document["facts"]), 4)
        self.assertLessEqual(trimmed.budget_view["injection_units"], 4096)
        self.assertTrue(any(o.get("reason") == "budget" for o in trimmed.omissions))
        self.assertIn("profile", document)
        self.assertIn("beliefs", document)
        self.assertEqual(
            [item["ref"] for item in document["user_input"]],
            [self.event_ref(question.event_id)],
        )
        self.assertLessEqual(
            request_units(estimate_units(trimmed.rendered_input), 0), 200_000
        )
        # A is active even without a host request budget.
        baseline = self.contexts.build_context(**reference)
        self.assertLess(len(json.loads(baseline.rendered_input)["facts"]), 4)

    def test_optional_facts_are_trimmed_with_a_budget_omission(self) -> None:
        question = self.ingest(
            source_kind="user_report",
            event_type="exp01.question",
            payload={"question": "合成问题", "scenario": "A", "synthetic_fixture": True},
        )
        self.ingest(
            event_type="TestEvidence", payload={"text": "x" * 1500 + "-1"}
        )
        self.ingest(
            event_type="TestEvidence", payload={"text": "y" * 1500 + "-2"}
        )
        reference = {
            "namespace": CONTEXT_NS,
            "trigger_ref": self.event_ref(question.event_id),
            "adapter_kind": "direct-http",
        }
        full = self.contexts.build_context(**reference)
        full_document = json.loads(full.rendered_input)
        self.assertGreaterEqual(len(full_document["facts"]), 1)
        full_units = estimate_units(full.rendered_input)

        trimmed = None
        chosen_budget = None
        for delta in range(100, 4000, 100):
            try:
                candidate = self.contexts.build_context(
                    **reference,
                    request_budget_units=full_units - delta,
                    output_reserve_units=0,
                )
            except ContextTooLarge:
                continue
            if len(json.loads(candidate.rendered_input)["facts"]) < len(
                full_document["facts"]
            ):
                trimmed = candidate
                chosen_budget = full_units - delta
                break
        self.assertIsNotNone(trimmed, "no budget produced an optional-material trim")
        trimmed_document = json.loads(trimmed.rendered_input)
        self.assertLess(
            len(trimmed_document["facts"]), len(full_document["facts"])
        )
        self.assertTrue(
            any(o.get("reason") == "budget" for o in trimmed.omissions),
            trimmed.omissions,
        )
        # Required material stays: profile, current beliefs, user input.
        self.assertIn("profile", trimmed_document)
        self.assertIn("beliefs", trimmed_document)
        self.assertEqual(
            [item["ref"] for item in trimmed_document["user_input"]],
            [self.event_ref(question.event_id)],
        )
        self.assertLessEqual(
            request_units(estimate_units(trimmed.rendered_input), 0),
            chosen_budget,
        )


class EstimatePersistenceTests(CognitionTestCase):
    """AC-11: the estimate is stored beside provider usage, never replacing it."""

    def _reserve(self, envelope, manifest, authorization):
        return self.store.reserve_model_attempt(
            run_id=envelope.run_id,
            namespace=CONTEXT_NS,
            context_id=envelope.context_id,
            authorization_id=authorization.authorization_id,
            provider=manifest.provider,
            model_requested=manifest.model,
            manifest_hash=manifest.manifest_hash,
            adapter_kind="direct-http",
            policy_version="j03-test",
            request_body="{}",
            manifest_max_calls=4,
            manifest_max_output_tokens_per_call=512,
            manifest_max_wall_seconds=60,
        )

    def test_usage_json_keeps_provider_tokens_and_pcs_estimate(self) -> None:
        trigger = self.ingest()
        envelope = self.contexts.build_context(
            namespace=CONTEXT_NS,
            trigger_ref=self.event_ref(trigger.event_id),
            adapter_kind="direct-http",
        )
        manifest = ModelManifest.from_mapping(MANIFEST)
        authorization = self.store.grant_model_authorization(
            namespace=CONTEXT_NS,
            authorization_ref="authorization:j03-estimate-test",
            provider=manifest.provider,
            model=manifest.model,
            manifest_hash=manifest.manifest_hash,
            max_calls=4,
            max_output_tokens_per_call=512,
            max_wall_seconds=60,
        )
        first = self._reserve(envelope, manifest, authorization)
        first_estimate = {
            "body_units": 10,
            "injected_units": 8,
            "request_units": 266,
            "request_budget_units": 200_000,
            "output_reserve_units": 0,
            "safety_margin_units": SAFETY_MARGIN_UNITS,
        }
        self.store.finish_model_attempt(
            attempt_id=first.attempt.attempt_id,
            outcome="failed",
            usage={"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160},
            usage_known=True,
            pcs_estimated=first_estimate,
        )
        second = self._reserve(envelope, manifest, authorization)
        second_estimate = dict(first_estimate, body_units=20, injected_units=18)
        self.store.finish_model_attempt(
            attempt_id=second.attempt.attempt_id,
            outcome="failed",
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
            usage_known=True,
            pcs_estimated=second_estimate,
        )

        attempts = {
            attempt.attempt_id: attempt
            for attempt in self.store.model_attempts_for_authorization(
                authorization.authorization_id
            )
        }
        first_usage = attempts[first.attempt.attempt_id].usage
        second_usage = attempts[second.attempt.attempt_id].usage
        self.assertEqual(120, first_usage["prompt_tokens"])
        self.assertEqual(40, first_usage["completion_tokens"])
        self.assertEqual(160, first_usage["total_tokens"])
        self.assertEqual(first_estimate, first_usage["pcs_estimated"])
        self.assertEqual(7, second_usage["prompt_tokens"])
        self.assertEqual(3, second_usage["completion_tokens"])
        self.assertEqual(10, second_usage["total_tokens"])
        self.assertEqual(second_estimate, second_usage["pcs_estimated"])
        # The estimate keys are exactly the six the card names, in usage_json.
        self.assertEqual(
            {
                "body_units",
                "injected_units",
                "request_units",
                "request_budget_units",
                "output_reserve_units",
                "safety_margin_units",
            },
            set(first_usage["pcs_estimated"]),
        )
        columns = [
            row[1]
            for row in self.store.connection.execute(
                "PRAGMA table_info(model_attempts)"
            )
        ]
        self.assertNotIn("pcs_estimated", columns)


class NonInterferenceTests(unittest.TestCase):
    """AC-14: J03 code does not reach into the upstream budget names."""

    def test_budget_module_has_no_upstream_units(self) -> None:
        source = (_SRC_DIR / "quintessence_pcs" / "runtime" / "budget.py").read_text(
            encoding="utf-8"
        )
        for forbidden in ("scope_recall", "budget_tokens", "max_items"):
            self.assertNotIn(forbidden, source)

    def test_gate_module_does_not_import_the_upstream_package(self) -> None:
        source = (_SRC_DIR / "quintessence_pcs" / "runtime" / "http_model.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("scope_recall", source)


if __name__ == "__main__":
    unittest.main()
