"""Offline journal writing with synthetic PCS sources and scripted model rounds."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from tests.desktop import support  # establishes sidecar import path
from tests.domain.model_support import FakeProvider, ScriptedResponse
from dreaming_calendar import DesktopCalendar
from host_config import load_config
from instance_manager import CONFIG, default_document, prepare, prepare_dreaming_root
from runtime import DesktopRuntime
from quintessence_pcs.runtime.agent_protocol import StepOutcome, ToolCall
from quintessence_pcs.runtime.collab_protocol import ConsultSendPolicy
from quintessence_pcs.runtime.dreaming_journal import CalendarJournalStore
from quintessence_pcs.runtime.dreaming_journal_model import JournalContextOverflow, JournalModel, JournalRound

NOW = datetime(2026, 6, 3, 3, tzinfo=UTC)
TZ = "Asia/Shanghai"


class CalendarWriterTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="calendar-writer-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "instance"
        root.mkdir()
        prepare(root, default_document())
        identity = json.loads((root / "instance.json").read_text(encoding="utf-8"))["instance_id"]
        prepare_dreaming_root(root, identity)
        config = load_config(root / CONFIG)
        self.runtime = DesktopRuntime(config, instance_id=identity, dreaming_root=root / "dreaming")
        self.addCleanup(self.runtime.close)
        self.journals = CalendarJournalStore(root / "dreaming", instance_id=identity)
        authority = config.authority
        self.policy = ConsultSendPolicy(
            authority.namespace, authority.host_instance, authority.access_ref, authority.privacy_classes,
            authority.purposes | {"dreaming"}, authority.disclose,
        )
        self.calendar = DesktopCalendar(
            self.runtime, store=self.journals, history=None, semantic=None, wake=None,
            read_policy=lambda: self.policy,
        )

    def source(self, text, occurred):
        store = self.runtime.store
        timestamp = occurred.isoformat().replace("+00:00", "Z")
        body = store.register_input(
            namespace=self.policy.namespace, origin_kind="user_statement", body=text,
            privacy_class="private", retention="standard", observed_at=timestamp,
            recorded_at=timestamp, input_id=str(uuid4()), content_version=1,
        )
        ref = {
            "ref_type": "collab_input", "namespace": self.policy.namespace,
            "input_id": body.input_id, "content_version": body.content_version,
        }
        store.register_interaction(
            namespace=self.policy.namespace, mode="consult", activation_kind="user_input",
            host_instance=self.policy.host_instance, access_ref=self.policy.access_ref,
            policy_ref="collab.v1", input_refs=[ref], result="answered",
        )
        return ref

    def due(self):
        self.journals.record_running(NOW, TZ)
        period = self.journals.next_due(NOW + timedelta(days=1), TZ)
        self.assertIsNotNone(period)
        return period

    def run_period(self, period, step, cancel=None):
        model = type("Model", (), {"step": staticmethod(step), "in_flight": False})()
        with patch("dreaming_calendar.JournalModel", return_value=model):
            return self.calendar.run(
                period, policy=self.policy, cancel=cancel or threading.Event(), allow_call=lambda: True,
            )

    @staticmethod
    def final(text):
        return JournalRound(StepOutcome("stop", json.dumps({"text": text, "tags": ["synthetic"]}), ""))

    def test_provider_overflow_splits_all_records_and_preserves_full_source_text(self):
        sources = [
            self.source(f"Original entry {index}: " + "x" * 660 + f" end-{index}", NOW + timedelta(minutes=index))
            for index in range(9)
        ]
        period = self.due()
        fragments = []

        def step(messages, tools, **kwargs):
            packet = json.loads(messages[0]["content"])
            evidence = packet["evidence"]
            self.assertTrue(packet["persona"]["profile"]["identity_summary"])
            self.assertIn("日记", packet["task"])
            if evidence and "source" in evidence[0]:
                if len(evidence) > 5:
                    raise JournalContextOverflow()
                fragments.extend(evidence)
            return self.final("own private journal")

        self.assertEqual(self.run_period(period, step), "published")
        self.assertEqual([row["source"] for row in fragments], sources)
        for index, row in enumerate(fragments):
            self.assertTrue(row["text"].endswith(f"end-{index}"))
        self.assertTrue(all(ref in self.journals.get(period).refs for ref in sources))

    def test_no_chat_day_can_research_an_earlier_original_and_use_it_next_round(self):
        previous = self.source("An earlier test decision", NOW - timedelta(days=3))
        period = self.due()
        first = True

        def step(messages, tools, **kwargs):
            nonlocal first
            self.assertTrue(json.loads(messages[0]["content"])["no_current_events"])
            self.assertIn("experience.timeline", [tool["function"]["name"] for tool in tools])
            if first:
                first = False
                return JournalRound(StepOutcome(
                    "tool_calls", "", "",
                    (ToolCall("one", "experience.timeline", '{"date":"2026-05-31"}'),),
                ))
            self.assertIn("An earlier test decision", messages[-1]["content"])
            return self.final("I reconsidered an earlier decision during the quiet day.")

        self.assertEqual(self.run_period(period, step), "published")
        self.assertIn(previous, self.journals.get(period).refs)
        self.assertTrue(self.calendar.live(self.journals.get(period), self.policy))

    def test_length_reply_is_continued_without_replacing_earlier_text(self):
        period = self.due()

        def step(messages, tools, **kwargs):
            if len(messages) == 1:
                return JournalRound(StepOutcome("length", '{"text":"First', ""), continuation=True)
            self.assertEqual(messages[-2]["content"], '{"text":"First')
            return JournalRound(StepOutcome("stop", ' and second.","tags":[]}', ""))

        self.assertEqual(self.run_period(period, step), "published")
        self.assertEqual(self.journals.get(period).text, "First and second.")

    def test_overflow_after_research_preserves_the_result_in_both_fragments(self):
        earlier = self.source("first-boundary " + "x" * 900 + " second-boundary", NOW - timedelta(days=3))
        period = self.due()
        fragments = []

        def step(messages, tools, **kwargs):
            if len(messages) > 1:
                raise JournalContextOverflow()
            evidence = json.loads(messages[0]["content"])["evidence"]
            if not evidence:
                return JournalRound(StepOutcome(
                    "tool_calls", "", "",
                    (ToolCall("one", "experience.timeline", '{"date":"2026-05-31"}'),),
                ))
            if evidence[0]["kind"] == "research_result":
                fragments.extend(item["text"] for item in evidence)
            return self.final("A reflection on what I found.")

        self.assertEqual(self.run_period(period, step), "published")
        self.assertIn("first-boundary", fragments[0])
        self.assertIn("second-boundary", fragments[-1])
        self.assertIn(earlier, self.journals.get(period).refs)

    def test_revoked_historical_source_invalidates_the_annual_lineage(self):
        source = self.source("a revocable source", NOW)
        period = self.due()
        self.assertEqual(self.run_period(period, lambda *args, **kwargs: self.final("original diary")), "published")
        destination = datetime(2027, 1, 2, 3, tzinfo=UTC)
        entries = []
        while (period := self.journals.next_due(destination, TZ)) is not None:
            entries.append(self.journals.save(
                period,
                text="derived publication",
                tags=(),
                refs=(),
                children=[child.revision for child in self.journals.inputs(period)],
            ))
        annual = entries[-1]
        self.assertEqual(annual.period.kind, "year")
        self.assertTrue(self.calendar.live(annual, self.policy))
        self.runtime.store.delete_experience(
            namespace=self.policy.namespace, ref_type=source["ref_type"], ref_id=source["input_id"],
            ref_version=source["content_version"], authorization_ref="authorization:synthetic-delete",
        )
        self.assertFalse(self.calendar.live(annual, self.policy))

    def test_http_classifies_provider_overflow_and_uses_provider_output_default(self):
        provider = FakeProvider()
        self.addCleanup(provider.close)
        provider.enqueue(
            ScriptedResponse(status=400, body={"error": {"code": "context_length_exceeded"}}),
            ScriptedResponse(body={
                "choices": [{"message": {"content": '{"text":"a partial'}, "finish_reason": "length"}],
            }),
        )
        manifest = replace(self.runtime.config.model.manifest, base_url=provider.url)
        model = JournalModel(self.runtime.store, manifest)
        messages = [{"role": "user", "content": "a synthetic writing job"}]
        _, _, tools = self.calendar._tools()
        cancel = threading.Event()
        with patch.dict(os.environ, {manifest.api_key_env: "synthetic-calendar-key"}):
            with self.assertRaises(JournalContextOverflow):
                model.step(messages, tools, cancel=cancel, live=lambda: True)
            result = model.step(messages, tools, cancel=cancel, live=lambda: True)
        self.assertTrue(result.continuation)
        self.assertEqual(result.outcome.content, '{"text":"a partial')
        for sent in provider.requests:
            packet = json.loads(sent["body"])
            self.assertNotIn("max_tokens", packet)
            self.assertIn("experience__timeline", [tool["function"]["name"] for tool in packet["tools"]])

    def test_restart_resumes_after_the_completed_provider_fragment(self):
        for index in (1, 2):
            self.source(f"Original fragment {index}", NOW + timedelta(minutes=index))
        period = self.due()
        interrupted = threading.Event()

        def first(messages, tools, **kwargs):
            items = json.loads(messages[0]["content"])["evidence"]
            if len(items) > 1:
                raise JournalContextOverflow()
            if items[0]["text"].endswith("2"):
                interrupted.set()
                return self.final("cancelled reply")
            return self.final("Reviewed first fragment")

        self.assertEqual(self.run_period(period, first, interrupted), "paused")
        self.assertIsNone(self.journals.get(period))
        self.calendar = DesktopCalendar(
            self.runtime, store=self.journals, history=None, semantic=None, wake=None,
            read_policy=lambda: self.policy,
        )
        seen = []

        def second(messages, tools, **kwargs):
            items = json.loads(messages[0]["content"])["evidence"]
            seen.append([item["text"] for item in items])
            return self.final("Reviewed second fragment" if len(items) == 1 else "Published both fragments")

        self.assertEqual(self.run_period(period, second), "published")
        self.assertEqual(seen, [["Original fragment 2"], ["Reviewed first fragment", "Reviewed second fragment"]])
