"""R-E preflight: lock the legacy ModelPort against a consult Context.

The R-E plan chooses the legacy ``ModelPort.propose`` path (a
``DirectHttpModel`` with the ``pcs2-008`` protocol) over a host-built
``maintenance consult Context``.  Before any maintenance code is written this
test proves the three things that choice depends on:

* ``bind_run`` accepts a ``ConsultContextEnvelope`` whose ``adapter_kind`` is
  ``direct-http`` and whose ``context_kind`` is ``consult`` (no trigger);
* the legacy pre-send basis (``require_live_context``) accepts that Context;
* the response is the strict ``propose_belief`` / ``wait`` JSON that
  ``parse_proposal`` reads, and one format repair can reuse the same
  run/Context.

The provider is the loopback fake from ``model_support``; there is no real
model call and no network beyond 127.0.0.1.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS  # noqa: E402
from model_support import ModelPortTestCase  # noqa: E402

from quintessence_pcs.foundation.errors import InvalidInput  # noqa: E402
from quintessence_pcs.foundation.jsonutil import canonical_json_text  # noqa: E402
from quintessence_pcs.runtime import DirectHttpModel, parse_proposal  # noqa: E402
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

HOST = "host:rg08-r-e-model-lock"
ACCESS_REF = "authorization:rg08-r-e-model-lock"


class LegacyModelConsultLockTests(ModelPortTestCase):
    """The smallest possible legacy send over a maintenance-shaped Context."""

    def _access(self) -> ExperienceAccess:
        return ExperienceAccess(
            namespace=NS,
            host_instance=HOST,
            access_ref=ACCESS_REF,
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"consult"}),
        )

    def _context(self):
        return self.contexts.build_consult_context(
            namespace=NS,
            access=self._access(),
            input_refs=[],
            adapter_kind="direct-http",
        )

    def _model(self) -> DirectHttpModel:
        return DirectHttpModel(self.store, self.manifest, protocol="pcs2-008")

    def _send(self, model: DirectHttpModel, context, *, feedback=None) -> str:
        with model.bind_run(namespace=NS, context=context):
            return model.propose(
                rendered_input=context.rendered_input,
                references=context.collab_source_refs,
                feedback=feedback,
            )

    def test_a_wait_proposal_is_accepted_over_a_consult_context(self) -> None:
        context = self._context()
        self.assertEqual(context.adapter_kind, "direct-http")
        self.enqueue_content(
            canonical_json_text(self.wait_proposal("fixture_no_change"))
        )
        model = self._model()
        raw = self._send(model, context)
        proposal = parse_proposal(raw)
        self.assertEqual(proposal["operation"], "wait")
        self.assertEqual(proposal["reason"], "fixture_no_change")

    def test_one_format_repair_reuses_the_same_run_and_context(self) -> None:
        context = self._context()
        self.enqueue_content("not strict json")
        self.enqueue_content(canonical_json_text(self.wait_proposal("repaired")))
        model = self._model()
        first = self._send(model, context)
        with self.assertRaises(InvalidInput):
            parse_proposal(first)
        second = self._send(model, context, feedback="return exactly one JSON object")
        self.assertEqual(parse_proposal(second)["operation"], "wait")
        self.assertEqual(parse_proposal(second)["reason"], "repaired")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
