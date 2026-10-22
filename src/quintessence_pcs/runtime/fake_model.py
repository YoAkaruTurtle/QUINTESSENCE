"""C3: the deterministic, offline FakeModel and its output parser.

The FakeModel is a **process-local stand-in**, not a ModelPort.  It reads only
the two things the runtime hands it — the exact rendered Context text and the
exact reference list — and returns one JSON object.  It never touches the
store, the clock, the network or the filesystem, so two runs over the same
input produce byte-identical output.

Synthetic rule set (``pcs2-008.fake-model.v1``):

* the trigger event must be a ``CapabilityObserved:<name>`` or
  ``CapabilityFailed:<name>`` event that the Context marked as usable evidence;
* ``CapabilityObserved`` proposes (or revises) the claim
  ``capability:<name>`` with a supporting link; ``CapabilityFailed`` adds a
  contradicting link;
* an existing belief with the same claim is revised by copying its evidence
  links and appending the new one.  A ``retracted``/``contradicted`` belief is
  not revived (PCS2-008 has no reopen); the model waits instead;
* everything else waits.

The rule is deliberately about *mechanism*, not semantics: it proves that new
evidence reaches the validator and that status/strength follow the card's
rules.  A real model replaces it in PCS2-009.

Model-claimed ``state_revision``, ``request_id``, ``read_set`` or
``context_id`` are never read: any unknown key in the output is rejected by
:func:`parse_proposal`, and the runtime supplies those values itself.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..foundation.jsonutil import strict_json_loads
from .proposal_generation import parse_proposal

FAKE_MODEL_ADAPTER_KIND = "fake-model"
FAKE_MODEL_POLICY_VERSION = "pcs2-008.fake-model.v1"
FAKE_MODEL_REF = "fake-model:pcs2-008.rule.v1"

CAPABILITY_PREFIXES = ("CapabilityObserved", "CapabilityFailed")
_CAPABILITY_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_DEFAULT_REVIEW_CONDITION = (
    "review when independent capability evidence contradicts this claim"
)
_DEFAULT_STRENGTH_REASON = (
    "synthetic fake-model stand-in: one pending capability source"
)


class FakeModel:
    """Deterministic process-local model substitute (not a ModelPort)."""

    adapter_kind = FAKE_MODEL_ADAPTER_KIND
    policy_version = FAKE_MODEL_POLICY_VERSION
    model_ref = FAKE_MODEL_REF

    def propose(
        self,
        *,
        rendered_input: str,
        references: Sequence[Mapping[str, Any]],
        feedback: str | None = None,
    ) -> str:
        """Return exactly one proposal JSON object or a WAIT object."""

        del feedback  # the deterministic stand-in has nothing to repair
        document = strict_json_loads(rendered_input)
        if not isinstance(document, dict):
            raise ValueError("the rendered context is not a JSON object")
        trigger_ref = document["trigger"]["ref"]
        trigger = next(
            (
                item
                for item in references
                if item.get("kind") == "event" and item.get("ref") == trigger_ref
            ),
            None,
        )
        if trigger is None or not trigger.get("evidence_usable"):
            return _wait("the trigger event is not usable as evidence")
        event_type = document["trigger"]["event_type"]
        parsed = _capability(event_type)
        if parsed is None:
            return _wait("the trigger event is not a synthetic capability event")
        name, relation = parsed
        claim = f"capability:{name}"
        origin_group = trigger["origin_group_id"]
        link = {
            "ref": trigger_ref,
            "relation": relation,
            "origin_group_id": origin_group,
            "applicability_reason": (
                "synthetic capability event chosen by the fake model"
            ),
        }
        existing = next(
            (
                belief
                for belief in document.get("beliefs", [])
                if belief.get("claim") == claim
            ),
            None,
        )
        if existing is not None:
            if existing.get("status") in ("retracted", "contradicted"):
                return _wait(
                    "the existing capability belief is terminal in PCS2-008"
                )
            links = list(existing.get("evidence_links", []))
            if any(item.get("ref") == trigger_ref for item in links):
                return _wait("the trigger event is already evidence for this claim")
            links.append(link)
            body = {
                "claim": claim,
                "claim_type": "capability",
                "evidence_links": links,
                "strength": "strong",
                "strength_reason": _DEFAULT_STRENGTH_REASON,
                "inference_basis": "observation",
                "premise_refs": list(existing.get("premise_refs", [])),
                "alternative_group_id": existing.get("alternative_group_id"),
                "valid_until": existing.get("valid_until"),
                "review_condition": (
                    existing.get("review_condition")
                    or _DEFAULT_REVIEW_CONDITION
                ),
            }
            scope = existing.get("scope") or _scope_for(name)
            return _dumps(
                {
                    "operation": "revise_belief",
                    "target": existing["ref"],
                    "body": body,
                    "scope": scope,
                }
            )
        body = {
            "claim": claim,
            "claim_type": "capability",
            "evidence_links": [link],
            "strength": "strong",
            "strength_reason": _DEFAULT_STRENGTH_REASON,
            "inference_basis": "observation",
            "premise_refs": [],
            "alternative_group_id": None,
            "valid_until": None,
            "review_condition": _DEFAULT_REVIEW_CONDITION,
        }
        return _dumps(
            {
                "operation": "propose_belief",
                "body": body,
                "scope": _scope_for(name),
            }
        )


def _capability(event_type: Any) -> tuple[str, str] | None:
    if not isinstance(event_type, str):
        return None
    prefix, separator, name = event_type.partition(":")
    if not separator or prefix not in CAPABILITY_PREFIXES:
        return None
    if _CAPABILITY_NAME.fullmatch(name) is None:
        return None
    relation = "supports" if prefix == "CapabilityObserved" else "contradicts"
    return name, relation


def _scope_for(name: str) -> dict[str, str]:
    return {"scene": f"pcs2-008.synthetic.{name}"}


def _wait(reason: str) -> str:
    return _dumps({"operation": "wait", "reason": reason})


def _dumps(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


__all__ = [
    "FAKE_MODEL_ADAPTER_KIND",
    "FAKE_MODEL_POLICY_VERSION",
    "FAKE_MODEL_REF",
    "FakeModel",
    "parse_proposal",
]
