"""C05a: the frozen collab.v1 collaboration contracts.

Two independent layers are exercised here:

* layer S (schema): the public JSON Schema files are the single source of
  truth and are evaluated by the same dependency-free ``ContractChecker`` as
  pcs.v2.  Invalid samples declare the exact ``(path, keyword)`` pair they
  expect, so a right verdict from the wrong constraint is a failure.
* layer D (semantics): :func:`check_semantics` is a pure function over typed
  synthetic objects.  It enforces the rules JSON Schema cannot express: no
  self-reference or cycle beyond depth 8, consult/review Context coupling,
  readability/body coupling, read_set membership, Context/binding agreement,
  source_refs/link agreement, namespace closure, generation discipline and
  the non-derivation of the four permissions.

The two chain files are static, synthetic sequences: every schema object is
validated step by step, every digest is recomputed from the text it names, and
the final accumulated state must be semantically clean.  No model, network or
private data is involved.
"""

from __future__ import annotations

import hashlib
import json
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from contract_checker import (  # noqa: E402
    ContractChecker,
    strict_loads_file,
)

REPO_ROOT = HERE.parents[1]
CONTRACT_DIR = REPO_ROOT / "contracts" / "collaboration" / "v1"
EXAMPLE_DIR = CONTRACT_DIR / "examples"

SCHEMA_FILES = {
    "_common.json",
    "source_ref.json",
    "interaction.json",
    "input.json",
    "artifact.json",
    "candidate.json",
    "work_control.json",
    "context_binding.json",
    "source_link.json",
    "grant.json",
    "experience_view.json",
    "experience_unavailable.json",
}

RULES = {f"R{number}" for number in range(1, 16)}

#: Expected required field sets.  These are copied from the freeze, not
#: derived from the schema files under test, so dropping a field fails.
REQUIRED_FIELDS = {
    "interaction.json": {
        "interaction_id",
        "namespace",
        "schema_version",
        "mode",
        "activation_kind",
        "host_instance",
        "input_refs",
        "trigger_ref",
        "work_id",
        "work_generation",
        "context_id",
        "result",
        "receipt_request_id",
        "access_ref",
        "policy_ref",
        "created_at",
        "completed_at",
    },
    "input.json": {
        "input_id",
        "namespace",
        "schema_version",
        "content_version",
        "origin_kind",
        "body",
        "content_digest",
        "privacy_class",
        "retention",
        "observed_at",
        "recorded_at",
        "supersedes",
    },
    "artifact.json": {
        "artifact_id",
        "namespace",
        "schema_version",
        "artifact_version",
        "kind",
        "interaction_id",
        "context_id",
        "attempt_ref",
        "origin_kind",
        "body",
        "content_digest",
        "retention",
        "source_refs",
        "created_at",
    },
    "candidate.json": {
        "artifact_id",
        "namespace",
        "schema_version",
        "artifact_version",
        "kind",
        "interaction_id",
        "context_id",
        "attempt_ref",
        "origin_kind",
        "body",
        "content_digest",
        "retention",
        "source_refs",
        "created_at",
        "proposal",
        "proposal_digest",
        "proposal_version",
        "work_id",
        "work_generation",
        "request_id",
        "command_digest",
        "structural_validation",
        "rerun_authorization_ref",
        "commit_authorization_ref",
        "status",
        "decided_at",
        "decision_ref",
    },
    "work_control.json": {
        "namespace",
        "work_id",
        "schema_version",
        "generation",
        "blocked_after_rejection",
        "control_version",
        "rerun_authorization_ref",
        "block_reason",
        "updated_at",
        "last_candidate_ref",
    },
    "context_binding.json": {
        "context_id",
        "namespace",
        "schema_version",
        "context_kind",
        "envelope_version",
        "trigger_ref",
        "interaction_id",
        "state_revision",
        "context_event_seq",
        "privacy_epoch",
        "profile_ref",
        "read_set",
        "collab_source_refs",
        "renderer_version",
        "policy_ref",
        "content_hash",
        "model_input_complete",
        "created_at",
    },
    "source_link.json": {
        "namespace",
        "owner_kind",
        "owner_id",
        "owner_version",
        "source",
        "created_at",
    },
    "experience_view.json": {
        "ref",
        "namespace",
        "interaction_id",
        "mode",
        "origin_kind",
        "processing",
        "readability",
        "body",
        "retention",
        "context_ref",
        "attempt_ref",
        "source_refs",
        "created_at",
    },
    "experience_unavailable.json": {
        "ref",
        "readability",
    },
}

NAMESPACE_PATTERN = "^[a-z][a-z0-9._-]{0,127}$"
UUID_PATTERN = "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


# --------------------------------------------------------------------------
# small deterministic helpers (no repository code is imported)
# --------------------------------------------------------------------------


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def bare_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ref_key(ref: dict[str, Any]) -> str:
    return canonical(ref)


def load(path: Path) -> Any:
    return strict_loads_file(path)


def entries(objects: Iterable[dict[str, Any]]) -> dict[str, list[Any]]:
    grouped: dict[str, list[Any]] = {}
    for entry in objects:
        grouped.setdefault(entry["type"], []).append(entry["value"])
    return grouped


@dataclass(frozen=True)
class Violation:
    """One layer-D violation.  ``rule`` is always one of the frozen R ids."""

    rule: str
    path: str
    message: str


@dataclass(frozen=True)
class Decision:
    """Result of one of the two frozen gate reference implementations."""

    allowed: bool
    reason: str
    generation: int


# --------------------------------------------------------------------------
# permission gates: the two pure reference implementations
# --------------------------------------------------------------------------


def grant_active(grant: dict[str, Any], now: str | None) -> bool:
    """Revocation is immediate; expiry is re-evaluated at every use."""

    if grant.get("revoked_at") is not None:
        return False
    expires = grant.get("expires_at")
    if now is not None and expires is not None:
        return expires > now
    return True


def review_start_decision(
    control: dict[str, Any],
    grants: Iterable[dict[str, Any]],
    now: str | None = None,
) -> Decision:
    """Start gate: unblocked work, or a matching rerun grant. Nothing else."""

    rerun = [
        grant
        for grant in grants
        if grant.get("kind") == "rerun"
        and grant_active(grant, now)
        and grant.get("namespace") == control.get("namespace")
        and grant.get("work_id") == control.get("work_id")
        and grant.get("generation") == control.get("generation")
    ]
    if not control.get("blocked_after_rejection"):
        return Decision(True, "not_blocked", control["generation"])
    if rerun:
        return Decision(True, "rerun_grant", control["generation"])
    return Decision(False, "blocked_after_rejection", control["generation"])


def commit_decision(
    candidate: dict[str, Any],
    control: dict[str, Any],
    grants: Iterable[dict[str, Any]],
    approval: dict[str, Any] | None,
    operation: str | None,
    trigger_kind: str | None,
    now: str | None = None,
) -> Decision:
    """Commit gate: current pending candidate plus approval or a commit grant."""

    generation = control["generation"]
    if candidate.get("status") != "pending" or candidate.get("work_generation") != generation:
        return Decision(False, "candidate_not_current", generation)
    approval_ok = (
        approval is not None
        and approval.get("candidate_id") == candidate.get("artifact_id")
        and approval.get("proposal_digest") == candidate.get("proposal_digest")
        and approval.get("context_id") == candidate.get("context_id")
        and approval.get("request_id") == candidate.get("request_id")
    )
    if approval_ok:
        return Decision(True, "manual_approval", generation)
    for grant in grants:
        if grant.get("kind") != "commit" or not grant_active(grant, now):
            continue
        if grant.get("namespace") != candidate.get("namespace"):
            continue
        if grant.get("scope") == "work_generation":
            if (
                grant.get("work_id") == candidate.get("work_id")
                and grant.get("generation") == candidate.get("work_generation")
            ):
                return Decision(True, "commit_grant", generation)
        elif grant.get("scope") == "policy":
            if operation in grant.get("operations", ()) and trigger_kind in grant.get(
                "trigger_kinds", ()
            ):
                return Decision(True, "commit_grant", generation)
    return Decision(False, "no_commit_authority", generation)


# --------------------------------------------------------------------------
# layer D
# --------------------------------------------------------------------------


def check_semantics(objects: Iterable[dict[str, Any]]) -> list[Violation]:
    """Return every layer-D violation for one synthetic state.

    ``objects`` is a list of ``{"type": ..., "value": ...}`` entries.  The
    function is stateless and performs no I/O; a caller can run it on a chain's
    accumulated state or on a single deliberately broken sample.
    """

    by = entries(objects)
    violations: list[Violation] = []

    def bad(rule: str, path: str, message: str) -> None:
        violations.append(Violation(rule, path, message))

    interactions = by.get("interaction", [])
    inputs = by.get("input", [])
    artifacts = by.get("artifact", [])
    candidates = by.get("candidate", [])
    bindings = by.get("context_binding", [])
    controls = by.get("work_control", [])
    grants = by.get("grant", [])
    links = by.get("source_link", [])
    views = by.get("experience_view", [])
    deletions = by.get("deletion", [])
    decisions_start = by.get("review_start_decision", [])
    decisions_commit = by.get("commit_decision", [])
    approvals = by.get("approval", [])
    attempts = by.get("model_attempt", [])

    interactions_by_id = {item["interaction_id"]: item for item in interactions}
    inputs_by_id = {item["input_id"]: item for item in inputs}
    bindings_by_context = {item["context_id"]: item for item in bindings}
    grants_by_id = {item["grant_id"]: item for item in grants}
    approvals_by_ref = {item["approval_ref"]: item for item in approvals}
    attempts_by_id = {item["attempt_id"]: item for item in attempts}
    controls_by_work: dict[tuple[str, str], dict[str, Any]] = {}
    for item in controls:
        controls_by_work[(item["namespace"], item["work_id"])] = item
    candidates_by_id: dict[str, dict[str, Any]] = {}
    for item in candidates:
        candidates_by_id[item["artifact_id"]] = item
    artifacts_by_id: dict[str, dict[str, Any]] = {}
    for item in artifacts + candidates:
        artifacts_by_id[item["artifact_id"]] = item
    inputs_by_version = {
        (item["namespace"], item["input_id"], item["content_version"]): item
        for item in inputs
    }
    artifacts_by_version = {
        (item["namespace"], item["artifact_id"], item["artifact_version"]): item
        for item in artifacts_by_id.values()
    }
    # A chain records successive states of the same row; only the last state
    # of an interaction or a candidate is a live object for semantic checks.
    interactions = list(interactions_by_id.values())
    candidates = list(candidates_by_id.values())

    # ---- R2: synthetic external refs must be declared up front ----------
    externals = by.get("synthetic_externals")
    if externals:
        declared = set()
        for block in externals:
            for key in ("events", "records"):
                for ref in block.get(key, ()):
                    declared.add(ref_key(ref))

        def scoped_refs() -> Iterator[dict[str, Any]]:
            for item in interactions:
                if item.get("trigger_ref"):
                    yield item["trigger_ref"]
            for item in bindings:
                if item.get("trigger_ref"):
                    yield item["trigger_ref"]
                for line in item.get("read_set", ()):
                    yield line["ref"]
            for item in artifacts + candidates:
                yield from item.get("source_refs", ())
            for item in views:
                yield from item.get("source_refs", ())
            for item in links:
                yield item["source"]

        for ref in scoped_refs():
            if ref.get("ref_type") in ("event", "record") and ref_key(ref) not in declared:
                bad("R2", "synthetic_externals", f"undeclared external ref {ref_key(ref)}")

    # ---- R1: collaboration references are not evidence ------------------
    for binding in bindings:
        for index, line in enumerate(binding.get("read_set", ())):
            if line.get("ref", {}).get("ref_type") != "record":
                bad(
                    "R1",
                    f"context_binding/{binding['context_id']}/read_set/{index}",
                    "read_set entries must be RecordRefs",
                )
    for belief in by.get("belief", ()):
        for index, ref in enumerate(belief.get("evidence_links", ())):
            if str(ref.get("ref_type", "")).startswith("collab_"):
                bad(
                    "R1",
                    f"belief/evidence_links/{index}",
                    "a collaboration reference cannot be evidence",
                )

    # ---- R2: namespace closure ------------------------------------------
    for item in interactions:
        namespace = item["namespace"]
        for index, ref in enumerate(item.get("input_refs", ())):
            if ref.get("namespace") != namespace:
                bad("R2", f"interaction/{item['interaction_id']}/input_refs/{index}",
                    "input reference crosses namespaces")
            source = inputs_by_id.get(ref.get("input_id"))
            if source is not None and source.get("namespace") != namespace:
                bad("R2", f"input/{ref.get('input_id')}", "input crosses namespaces")
    for binding in bindings:
        namespace = binding["namespace"]
        for index, ref in enumerate(binding.get("collab_source_refs", ())):
            if ref.get("namespace") != namespace:
                bad("R2", f"context_binding/{binding['context_id']}/collab_source_refs/{index}",
                    "collaboration source crosses namespaces")
        profile = binding.get("profile_ref")
        if profile is not None and profile.get("namespace") != namespace:
            bad("R2", f"context_binding/{binding['context_id']}/profile_ref",
                "profile crosses namespaces")
    for item in artifacts + candidates:
        for index, ref in enumerate(item.get("source_refs", ())):
            if ref.get("namespace") != item["namespace"]:
                bad("R2", f"artifact/{item['artifact_id']}/source_refs/{index}",
                    "source reference crosses namespaces")
    for link in links:
        if link["namespace"] != link["source"].get("namespace"):
            bad("R2", f"source_link/{link['owner_id']}", "link crosses namespaces")
        if link["owner_kind"] == "context" and link.get("owner_version") is not None:
            bad("R2", f"source_link/{link['owner_id']}/owner_version",
                "a context link carries no artifact version")
        if link["owner_kind"] == "artifact" and link.get("owner_version") is None:
            bad("R2", f"source_link/{link['owner_id']}/owner_version",
                "an artifact link must carry its artifact_version")

    # ---- R2: persisted links equal source_refs --------------------------
    by_owner: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for link in links:
        by_owner.setdefault((link["owner_kind"], link["owner_id"]), []).append(link)
    for item in artifacts + candidates:
        owner_links = by_owner.get(("artifact", item["artifact_id"]), [])
        expected = {ref_key(ref) for ref in item.get("source_refs", ())}
        actual = {ref_key(link["source"]) for link in owner_links}
        if expected != actual:
            bad("R2", f"artifact/{item['artifact_id']}/source_refs",
                "source_refs and persisted links differ")
        for link in owner_links:
            if link.get("owner_version") != item.get("artifact_version"):
                bad("R2", f"source_link/{item['artifact_id']}/owner_version",
                    "link version differs from artifact_version")
    for binding in bindings:
        owner_links = by_owner.get(("context", binding["context_id"]), [])
        expected = {ref_key(ref) for ref in binding.get("collab_source_refs", ())}
        actual = {ref_key(link["source"]) for link in owner_links}
        if expected != actual:
            bad("R2", f"context_binding/{binding['context_id']}/collab_source_refs",
                "collab_source_refs and Context links differ")

    # ---- R7: no artifact cycle, depth at most 8 --------------------------
    edges: dict[str, list[str]] = {}
    for identifier, item in artifacts_by_id.items():
        edges[identifier] = [
            ref["artifact_id"]
            for ref in item.get("source_refs", ())
            if ref.get("ref_type") == "collab_artifact"
            and ref.get("namespace") == item.get("namespace")
            and ref.get("artifact_id") in artifacts_by_id
        ]
    state: dict[str, int] = {}
    depth: dict[str, int] = {}

    def visit(node: str) -> int:
        state[node] = 1
        best = 0
        for target in edges.get(node, ()):
            if state.get(target) == 1:
                bad("R7", f"artifact/{node}", f"artifact cycle through {target}")
                continue
            if state.get(target, 0) == 0:
                visit(target)
            best = max(best, 1 + depth.get(target, 0))
        state[node] = 2
        depth[node] = best
        return best

    for identifier in edges:
        if state.get(identifier, 0) == 0:
            if visit(identifier) > 8:
                bad("R7", f"artifact/{identifier}", "artifact chain deeper than 8")

    # ---- R12: consult/review Context coupling ---------------------------
    for item in interactions:
        if item.get("mode") == "review":
            if (
                item.get("trigger_ref") is None
                or item.get("work_id") is None
                or item.get("work_generation") is None
            ):
                bad("R12", f"interaction/{item['interaction_id']}",
                    "review needs trigger_ref, work_id and work_generation")
        elif (
            item.get("trigger_ref") is not None
            or item.get("work_id") is not None
            or item.get("work_generation") is not None
        ):
            bad("R12", f"interaction/{item['interaction_id']}",
                "consult must not carry review trigger/work fields")
    for binding in bindings:
        kind = binding["context_kind"]
        expected_envelope = "pcs.v2" if kind == "review" else "collab.v1"
        if binding.get("envelope_version") != expected_envelope:
            bad("R12", f"context_binding/{binding['context_id']}/envelope_version",
                "envelope_version disagrees with context_kind")
        if kind == "consult" and binding.get("trigger_ref") is not None:
            bad("R12", f"context_binding/{binding['context_id']}/trigger_ref",
                "consult trigger must be null")
        if kind == "review" and binding.get("trigger_ref") is None:
            bad("R12", f"context_binding/{binding['context_id']}/trigger_ref",
                "review trigger must be non-null")
        interaction = interactions_by_id.get(binding["interaction_id"])
        if interaction is not None:
            if interaction.get("mode") != kind:
                bad("R12", f"context_binding/{binding['context_id']}/context_kind",
                    "binding and interaction modes differ")
            if binding.get("trigger_ref") != interaction.get("trigger_ref"):
                bad("R12", f"context_binding/{binding['context_id']}/trigger_ref",
                    "binding and interaction triggers differ")
        if binding.get("profile_ref") is None and binding.get("read_set"):
            bad("R12", f"context_binding/{binding['context_id']}/profile_ref",
                "a null profile cannot hide a non-empty read set")
    snapshots = by.get("context_snapshot", [])
    for snapshot in snapshots:
        binding = bindings_by_context.get(snapshot["context_id"])
        if binding is None:
            continue
        for field in (
            "namespace",
            "trigger_ref",
            "state_revision",
            "context_event_seq",
            "privacy_epoch",
            "profile_ref",
            "read_set",
            "content_hash",
        ):
            if canonical(binding.get(field)) != canonical(snapshot.get(field)):
                bad("R12", f"context_binding/{binding['context_id']}/{field}",
                    "binding disagrees with the stored snapshot")

    # ---- R11: readability/body and deletion coupling --------------------
    latest_views: dict[str, dict[str, Any]] = {}
    for item in views:
        latest_views[ref_key(item["ref"])] = item
    for key, item in latest_views.items():
        if (item.get("readability") == "available") != (item.get("body") is not None):
            bad("R11", "experience_view", "body must be non-null iff readability is available")
        reference = item["ref"]
        target = None
        if reference.get("ref_type") == "collab_input":
            target = inputs_by_version.get(
                (
                    reference.get("namespace"),
                    reference.get("input_id"),
                    reference.get("content_version"),
                )
            )
        elif reference.get("ref_type") == "collab_artifact":
            target = artifacts_by_version.get(
                (
                    reference.get("namespace"),
                    reference.get("artifact_id"),
                    reference.get("artifact_version"),
                )
            )
        if target is not None and target.get("retention") != item.get("retention"):
            bad("R11", "experience_view",
                "view retention must equal the referenced object's retention")
    for deletion in deletions:
        view = latest_views.get(ref_key(deletion["ref"]))
        if view is not None and view.get("readability") == "available":
            bad("R11", "experience_view", "a deleted reference is still available")

    # ---- R4: generation discipline --------------------------------------
    rejected = set()
    for item in candidates:
        control = controls_by_work.get((item["namespace"], item["work_id"]))
        if control is None:
            continue
        if item.get("status") in ("pending", "committed"):
            if item.get("work_generation") != control["generation"]:
                bad("R4", f"candidate/{item['artifact_id']}",
                    "candidate generation is not the current generation")
        elif item.get("status") == "rejected":
            rejected.add((item["namespace"], item["work_id"], item["work_generation"]))
            if item.get("work_generation") >= control["generation"]:
                bad("R4", f"candidate/{item['artifact_id']}",
                    "rejection did not advance the generation")
            if not control.get("blocked_after_rejection"):
                bad("R4", f"work_control/{item['work_id']}",
                    "rejection did not block the work")
    for (namespace, work_id), control in controls_by_work.items():
        if control["generation"] > 0:
            previous = (namespace, work_id, control["generation"] - 1)
            if previous not in rejected:
                bad("R4", f"work_control/{work_id}",
                    "generation advanced without a rejected candidate")

    # ---- R6: a rerun authorization never invents another state ----------
    for control in controls_by_work.values():
        reference = control.get("rerun_authorization_ref")
        if not reference:
            continue
        grant = grants_by_id.get(reference)
        if (
            grant is None
            or grant.get("kind") != "rerun"
            or grant.get("namespace") != control["namespace"]
            or grant.get("work_id") != control["work_id"]
            or grant.get("generation") != control["generation"]
        ):
            bad("R6", f"work_control/{control['work_id']}",
                "rerun_authorization_ref is not a matching rerun grant")

    # ---- R9: attempt reference must match the ledger row ----------------
    for item in artifacts + candidates:
        attempt_ref = item.get("attempt_ref")
        if not attempt_ref:
            continue
        attempt = attempts_by_id.get(attempt_ref.get("attempt_id"))
        if attempt is None:
            continue
        if attempt.get("request_hash") != attempt_ref.get("request_hash"):
            bad("R9", f"artifact/{item['artifact_id']}/attempt_ref",
                "request_hash differs from model_attempts")
        if attempt.get("context_id") != item.get("context_id"):
            bad("R9", f"artifact/{item['artifact_id']}/attempt_ref",
                "attempt context differs from the artifact context")

    # ---- R15: derived retention must not outlive its sources ------------
    retention_rank = {"session": 0, "standard": 1, "durable": 2}
    for item in artifacts + candidates:
        own = retention_rank.get(item.get("retention"))
        if own is None:
            continue
        strictest = None
        for ref in item.get("source_refs", ()):
            source = None
            if ref.get("ref_type") == "collab_input":
                source = inputs_by_version.get(
                    (
                        ref.get("namespace"),
                        ref.get("input_id"),
                        ref.get("content_version"),
                    )
                )
            elif ref.get("ref_type") == "collab_artifact":
                source = artifacts_by_version.get(
                    (
                        ref.get("namespace"),
                        ref.get("artifact_id"),
                        ref.get("artifact_version"),
                    )
                )
            if source is None:
                continue
            source_rank = retention_rank.get(source.get("retention"))
            if source_rank is None:
                continue
            strictest = source_rank if strictest is None else min(strictest, source_rank)
        if strictest is not None and own > strictest:
            bad("R15", f"artifact/{item['artifact_id']}/retention",
                "derived retention is wider than a collaboration source")

    # ---- R13/R14: the two gates, recomputed -----------------------------
    for decision in decisions_start:
        control = controls_by_work.get((decision["namespace"], decision["work_id"]))
        if control is None:
            continue
        computed = review_start_decision(control, grants, decision.get("now"))
        basis = grants_by_id.get(decision.get("basis_grant_id"))
        if decision.get("allowed") and basis is not None and basis.get("kind") != "rerun":
            bad("R13", "review_start_decision",
                f"{basis.get('kind')} grant cannot authorise a rerun")
            continue
        if bool(decision.get("allowed")) != computed.allowed:
            bad("R5", "review_start_decision",
                "start gate result disagrees with the frozen gate")
    for decision in decisions_commit:
        candidate = candidates_by_id.get(decision["candidate_id"])
        if candidate is None:
            continue
        control = controls_by_work.get((candidate["namespace"], candidate["work_id"]))
        if control is None:
            continue
        approval = approvals_by_ref.get(decision.get("approval_ref"))
        computed = commit_decision(
            candidate,
            control,
            grants,
            approval,
            decision.get("operation"),
            decision.get("trigger_kind"),
            decision.get("now"),
        )
        basis = grants_by_id.get(decision.get("basis_grant_id"))
        if decision.get("allowed") and basis is not None and basis.get("kind") != "commit":
            bad("R13", "commit_decision",
                f"{basis.get('kind')} grant cannot authorise a commit")
            continue
        if bool(decision.get("allowed")) != computed.allowed:
            bad("R14", "commit_decision",
                "commit gate result disagrees with the frozen gate")

    return violations


# --------------------------------------------------------------------------
# chain walking
# --------------------------------------------------------------------------


class ChainRun:
    """Run one static chain: validate each schema object, accumulate state."""

    def __init__(self, checker: ContractChecker, chain: dict[str, Any]) -> None:
        self.checker = checker
        self.chain = chain
        self.objects: list[dict[str, Any]] = [
            {"type": "synthetic_externals", "value": chain["synthetic_externals"]}
        ]
        self.results: list[dict[str, Any]] = []

    def grants(self) -> list[dict[str, Any]]:
        return [entry["value"] for entry in self.objects if entry["type"] == "grant"]

    def snapshot_of(self, kind: str, **fields: Any) -> dict[str, Any] | None:
        found = None
        for entry in self.objects:
            value = entry["value"]
            if entry["type"] == kind and all(value.get(k) == v for k, v in fields.items()):
                found = value
        return found

    def apply(self, step: dict[str, Any]) -> dict[str, Any]:
        object_type = step["object_type"]
        obj = step["object"]
        expect = step["expect"]
        detail: dict[str, Any] = {
            "step": step["step"],
            "object_type": object_type,
            "expect": expect,
        }
        if object_type + ".json" in SCHEMA_FILES:
            failures = self.checker.validate(object_type + ".json", obj)
            if failures:
                raise AssertionError(
                    f"step {step['step']} ({object_type}) is schema-invalid: "
                    f"{[failure.as_tuple() for failure in failures]}"
                )
            if expect in {"available", "redacted", "unavailable"}:
                if obj.get("readability") != expect:
                    raise AssertionError(f"step {step['step']}: readability differs")
            elif expect in {"pending", "committed", "rejected", "stale", "failed"}:
                if obj.get("status") != expect:
                    raise AssertionError(f"step {step['step']}: status differs")
            elif expect != "valid":
                raise AssertionError(f"step {step['step']}: unexpected schema expectation")
            detail["result"] = "schema-valid"
            self.objects.append({"type": object_type, "value": obj})
        elif object_type == "model_attempt":
            detail["result"] = "recorded"
            self.objects.append({"type": object_type, "value": obj})
        elif object_type == "approval":
            detail["result"] = "recorded"
            self.objects.append({"type": object_type, "value": obj})
        elif object_type == "deletion":
            if obj.get("phase") != expect:
                raise AssertionError(f"step {step['step']}: deletion phase differs")
            detail["result"] = "semantic"
            self.objects.append({"type": object_type, "value": obj})
        elif object_type == "review_start_decision":
            control = self.snapshot_of("work_control", work_id=obj["work_id"])
            if control is None:
                raise AssertionError(f"step {step['step']}: no work_control")
            computed = review_start_decision(control, self.grants(), obj.get("now"))
            if computed.allowed != obj["allowed"] or (
                "reason" in obj and computed.reason != obj["reason"]
            ):
                raise AssertionError(
                    f"step {step['step']}: gate said {computed}, object said {obj}"
                )
            detail["result"] = "allowed" if computed.allowed else "denied"
            detail["decision"] = computed
        elif object_type == "commit_decision":
            candidate = self.snapshot_of("candidate", artifact_id=obj["candidate_id"])
            if candidate is None:
                raise AssertionError(f"step {step['step']}: no candidate")
            control = self.snapshot_of(
                "work_control", work_id=candidate["work_id"]
            )
            approval = None
            if obj.get("approval_ref"):
                approval = self.snapshot_of("approval", approval_ref=obj["approval_ref"])
            computed = commit_decision(
                candidate,
                control,
                self.grants(),
                approval,
                obj.get("operation"),
                obj.get("trigger_kind"),
                obj.get("now"),
            )
            if computed.allowed != obj["allowed"] or (
                "reason" in obj and computed.reason != obj["reason"]
            ):
                raise AssertionError(
                    f"step {step['step']}: gate said {computed}, object said {obj}"
                )
            detail["result"] = "allowed" if computed.allowed else "denied"
            detail["decision"] = computed
        else:
            raise AssertionError(f"unknown object_type {object_type!r}")
        self.results.append(detail)
        return detail

    def run(self) -> "ChainRun":
        for step in self.chain["steps"]:
            self.apply(step)
        return self

    def semantics(self) -> list[Violation]:
        return check_semantics(self.objects)


def object_of(objects: Iterable[dict[str, Any]], kind: str) -> dict[str, Any]:
    """Return the single typed object of ``kind`` from an alternative block."""

    return next(entry["value"] for entry in objects if entry["type"] == kind)


def closure_of(ref: dict[str, Any], links: Iterable[dict[str, Any]]) -> set[tuple[str, str]]:
    """All owners recursively reachable from one collaboration reference."""

    owners: set[tuple[str, str]] = set()
    pending = [ref_key(ref)]
    guard = 0
    while pending:
        guard += 1
        if guard > 1000:
            raise AssertionError("closure did not terminate")
        key = pending.pop(0)
        for link in links:
            if ref_key(link["source"]) != key:
                continue
            owner = (link["owner_kind"], link["owner_id"])
            if owner in owners:
                continue
            owners.add(owner)
            if link["owner_kind"] == "artifact":
                pending.append(
                    ref_key(
                        {
                            "ref_type": "collab_artifact",
                            "namespace": link["namespace"],
                            "artifact_id": link["owner_id"],
                            "artifact_version": link["owner_version"],
                        }
                    )
                )
    return owners


class CollaborationContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.checker = ContractChecker(CONTRACT_DIR)

    # ---------------------------------------------------------------- S1
    def test_schema_files_are_strict_and_complete(self) -> None:
        on_disk = {path.name for path in CONTRACT_DIR.glob("*.json")}
        self.assertEqual(SCHEMA_FILES, on_disk)
        self.assertTrue((CONTRACT_DIR / "README.md").is_file())

        common = load(CONTRACT_DIR / "_common.json")
        self.assertEqual("https://json-schema.org/draft/2020-12/schema", common["$schema"])
        self.assertEqual(
            {
                "uuid",
                "namespace",
                "timestamp_utc",
                "nullable_timestamp_utc",
                "sha256_digest",
                "hex64",
                "privacy_class",
                "retention",
                "readability",
                "content_version",
                "generation",
                "event_ref",
                "record_ref",
                "read_set_entry",
                "collab_input_ref",
                "collab_artifact_ref",
                "collaboration_ref",
                "source_ref",
                "attempt_ref",
                "proposal_version",
            },
            set(common["$defs"]),
        )

        readme = (CONTRACT_DIR / "README.md").read_text(encoding="utf-8")
        for name in sorted(SCHEMA_FILES):
            with self.subTest(schema=name):
                self.assertIn(name, readme)
                self.assertEqual(
                    "https://json-schema.org/draft/2020-12/schema",
                    self.checker.load(name)["$schema"],
                )
        self.assertEqual(
            "_common.json#/$defs/source_ref",
            load(CONTRACT_DIR / "source_ref.json")["$ref"],
        )

        def refs(node: Any) -> Iterator[str]:
            if isinstance(node, dict):
                if "$ref" in node:
                    yield node["$ref"]
                for key, value in node.items():
                    if key in {"$ref", "const", "enum", "default", "examples"}:
                        continue
                    yield from refs(value)
            elif isinstance(node, list):
                for item in node:
                    yield from refs(item)

        for name in sorted(SCHEMA_FILES):
            for ref in refs(self.checker.load(name)):
                with self.subTest(schema=name, ref=ref):
                    self.checker.resolve(name, ref)

        for path in sorted(CONTRACT_DIR.rglob("*")):
            if not path.is_file():
                continue
            data = path.read_bytes()
            with self.subTest(path=str(path)):
                self.assertNotIn(b"\r", data)
                self.assertTrue(data.endswith(b"\n"))

    # ---------------------------------------------------------------- S2
    def test_common_definition_shapes(self) -> None:
        defs = load(CONTRACT_DIR / "_common.json")["$defs"]
        self.assertEqual(UUID_PATTERN, defs["uuid"]["pattern"])
        self.assertEqual(NAMESPACE_PATTERN, defs["namespace"]["pattern"])
        self.assertEqual("^sha256:[0-9a-f]{64}$", defs["sha256_digest"]["pattern"])
        self.assertEqual("^[0-9a-f]{64}$", defs["hex64"]["pattern"])
        self.assertIn("null", defs["nullable_timestamp_utc"]["type"])
        self.assertEqual(1, defs["content_version"]["minimum"])
        self.assertEqual(0, defs["generation"]["minimum"])
        self.assertEqual(
            ["private", "restricted", "public-example"], defs["privacy_class"]["enum"]
        )
        self.assertEqual(
            ["available", "redacted", "unavailable"], defs["readability"]["enum"]
        )
        self.assertEqual(
            ["session", "standard", "durable"], defs["retention"]["enum"]
        )
        self.assertNotIn("pattern", defs["retention"])
        self.assertTrue(defs["proposal_version"]["pattern"].startswith("^[a-z0-9]"))

        for name in ("event_ref", "record_ref", "attempt_ref", "collab_input_ref",
                     "collab_artifact_ref", "read_set_entry"):
            with self.subTest(defn=name):
                self.assertIs(False, defs[name]["additionalProperties"])
                self.assertEqual(
                    set(defs[name]["required"]),
                    set(defs[name]["properties"]),
                    f"{name} must declare exactly its closed field set",
                )

        self.assertEqual({"event"}, {defs["event_ref"]["properties"]["ref_type"]["const"]})
        self.assertEqual({"record"}, {defs["record_ref"]["properties"]["ref_type"]["const"]})
        self.assertEqual(
            {"current", "historical", "pinned_asset"},
            set(defs["read_set_entry"]["properties"]["usage"]["enum"]),
        )
        self.assertEqual(
            {"collab_input"},
            {defs["collab_input_ref"]["properties"]["ref_type"]["const"]},
        )
        self.assertEqual(
            {"collab_artifact"},
            {defs["collab_artifact_ref"]["properties"]["ref_type"]["const"]},
        )
        self.assertEqual(2, len(defs["collaboration_ref"]["oneOf"]))
        self.assertEqual(4, len(defs["source_ref"]["oneOf"]))
        self.assertEqual(
            {
                "authorization_id",
                "attempt_id",
                "attempt_no",
                "request_hash",
            },
            set(defs["attempt_ref"]["required"]),
        )
        self.assertEqual(
            {"$ref": "#/$defs/sha256_digest"},
            defs["attempt_ref"]["properties"]["request_hash"],
        )

        namespace = "example.private"
        valid_sources = [
            {"ref_type": "event", "namespace": namespace,
             "event_id": "66666666-6666-4666-8666-666666666666"},
            {"ref_type": "record", "namespace": namespace,
             "record_id": "55555555-5555-4555-8555-555555555555", "record_version": 3},
            {"ref_type": "collab_input", "namespace": namespace,
             "input_id": "22222222-2222-4222-8222-222222222222", "content_version": 1},
            {"ref_type": "collab_artifact", "namespace": namespace,
             "artifact_id": "44444444-4444-4444-8444-444444444444", "artifact_version": 1},
        ]
        for source in valid_sources:
            with self.subTest(ref_type=source["ref_type"]):
                failures = self.checker.validate("source_ref.json", source)
                self.assertEqual([], [failure.as_tuple() for failure in failures])

    # ---------------------------------------------------------------- S3
    def test_frozen_object_fields_and_enums(self) -> None:
        for name, required in REQUIRED_FIELDS.items():
            with self.subTest(schema=name):
                doc = load(CONTRACT_DIR / name)
                self.assertIs(False, doc["additionalProperties"])
                self.assertEqual(required, set(doc["required"]))
                self.assertEqual(required, set(doc["properties"]))

        interaction = load(CONTRACT_DIR / "interaction.json")["properties"]
        self.assertEqual({"consult", "review"}, set(interaction["mode"]["enum"]))
        self.assertEqual(
            {"user_input", "manual_review"}, set(interaction["activation_kind"]["enum"])
        )
        self.assertEqual(
            {
                None,
                "answered",
                "waiting",
                "candidate_pending",
                "committed",
                "rejected",
                "stale",
                "denied",
                "failed",
                "incomplete",
            },
            set(interaction["result"]["enum"]),
        )

        input_doc = load(CONTRACT_DIR / "input.json")["properties"]
        self.assertEqual({"user_statement", "host_supplied"}, set(input_doc["origin_kind"]["enum"]))
        self.assertEqual(1, input_doc["body"]["minLength"])
        self.assertEqual(262144, input_doc["body"]["maxLength"])

        artifact = load(CONTRACT_DIR / "artifact.json")["properties"]
        self.assertEqual({"reply", "query_result"}, set(artifact["kind"]["enum"]))
        self.assertEqual(
            {"model_output", "deterministic_query"}, set(artifact["origin_kind"]["enum"])
        )
        self.assertNotIn("readability", artifact)

        candidate = load(CONTRACT_DIR / "candidate.json")["properties"]
        self.assertEqual({"candidate"}, {candidate["kind"]["const"]})
        self.assertEqual({"model_output"}, {candidate["origin_kind"]["const"]})
        self.assertEqual({"passed"}, {candidate["structural_validation"]["const"]})
        self.assertEqual("^[0-9a-f]{64}$", candidate["command_digest"]["pattern"])
        self.assertEqual(
            {"pending", "committed", "rejected", "stale", "failed"},
            set(candidate["status"]["enum"]),
        )
        self.assertIn("rerun_authorization_ref", candidate)
        self.assertIn("commit_authorization_ref", candidate)
        self.assertNotIn("readability", candidate)

        control = load(CONTRACT_DIR / "work_control.json")["properties"]
        self.assertEqual({"rejected", None}, set(control["block_reason"]["enum"]))
        self.assertIn("rerun_authorization_ref", control)
        self.assertNotIn("commit_authorization_ref", control)

        binding = load(CONTRACT_DIR / "context_binding.json")["properties"]
        self.assertEqual({"review", "consult"}, set(binding["context_kind"]["enum"]))
        self.assertEqual({"pcs.v2", "collab.v1"}, set(binding["envelope_version"]["enum"]))
        self.assertEqual(0, binding["read_set"]["minItems"])
        self.assertTrue(binding["read_set"]["uniqueItems"])
        self.assertEqual(
            {"$ref": "_common.json#/$defs/read_set_entry"},
            binding["read_set"]["items"],
        )

        link = load(CONTRACT_DIR / "source_link.json")["properties"]
        self.assertEqual({"context", "artifact"}, set(link["owner_kind"]["enum"]))
        self.assertEqual(2, len(link["owner_version"]["oneOf"]))

        view = load(CONTRACT_DIR / "experience_view.json")["properties"]
        self.assertEqual(
            {"user_statement", "model_output", "deterministic_query"},
            set(view["origin_kind"]["enum"]),
        )
        self.assertEqual(
            {
                "in_progress",
                "answered",
                "waiting",
                "candidate_pending",
                "committed",
                "rejected",
                "stale",
                "denied",
                "failed",
                "incomplete",
            },
            set(view["processing"]["enum"]),
        )
        self.assertEqual({"string", "null"}, set(view["body"]["type"]))

    # ---------------------------------------------------------------- S4
    def test_permission_separation_is_structural_and_operational(self) -> None:
        grant = load(CONTRACT_DIR / "grant.json")
        self.assertIs(False, grant["additionalProperties"])
        self.assertEqual(5, len(grant["oneOf"]))
        self.assertEqual(
            {"access", "egress", "commit", "rerun"},
            set(grant["properties"]["kind"]["enum"]),
        )
        kinds = []
        scopes = []
        for branch in grant["oneOf"]:
            kind = branch["properties"]["kind"]["const"]
            kinds.append(kind)
            scope = branch["properties"].get("scope")
            if isinstance(scope, dict) and "const" in scope:
                scopes.append(scope["const"])
        self.assertEqual(["access", "egress", "commit", "commit", "rerun"], kinds)
        self.assertEqual(["policy", "work_generation"], scopes)

        rerun_branch = grant["oneOf"][-1]
        self.assertIs(False, rerun_branch["properties"]["scope"])
        self.assertIs(False, rerun_branch["properties"]["operations"])
        self.assertIs(False, rerun_branch["properties"]["trigger_kinds"])
        self.assertIs(False, rerun_branch["properties"]["provider"])
        self.assertEqual(
            {"work_id", "generation"},
            set(rerun_branch["required"]),
        )

        work_control = load(CONTRACT_DIR / "work_control.json")["properties"]
        candidate_properties = load(CONTRACT_DIR / "candidate.json")["properties"]
        self.assertFalse(any("commit" in name for name in work_control))
        self.assertIn("rerun_authorization_ref", candidate_properties)
        self.assertIn("commit_authorization_ref", candidate_properties)

        # The same control/candidate data drives both gates independently.
        control = {
            "namespace": "example.private",
            "work_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "generation": 1,
            "blocked_after_rejection": True,
        }
        rerun = {
            "kind": "rerun",
            "namespace": "example.private",
            "work_id": control["work_id"],
            "generation": 1,
            "revoked_at": None,
            "expires_at": None,
        }
        commit_policy = {
            "kind": "commit",
            "namespace": "example.private",
            "scope": "policy",
            "operations": ["propose_belief"],
            "trigger_kinds": ["manual_review"],
            "revoked_at": None,
            "expires_at": None,
        }
        egress = {
            "kind": "egress",
            "namespace": "example.private",
            "privacy_classes": ["private"],
            "revoked_at": None,
            "expires_at": None,
        }
        for grant_value in (commit_policy, egress):
            with self.subTest(kind=grant_value["kind"]):
                self.assertFalse(
                    review_start_decision(control, [grant_value]).allowed,
                    "only a rerun grant may open the start gate",
                )
        self.assertTrue(review_start_decision(control, [rerun]).allowed)
        self.assertEqual(
            "rerun_grant", review_start_decision(control, [rerun]).reason
        )

        candidate = {
            "artifact_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "namespace": "example.private",
            "work_id": control["work_id"],
            "work_generation": 1,
            "context_id": "88888888-8888-4888-8888-888888888888",
            "status": "pending",
            "proposal_digest": sha256_digest("proposal"),
            "request_id": "22223333-4444-4555-8666-777788889999",
            "proposal": {"operation": "propose_belief"},
        }
        self.assertFalse(
            commit_decision(
                candidate, control, [rerun], None, "propose_belief", "manual_review"
            ).allowed,
            "rerun never implies commit",
        )
        self.assertFalse(
            commit_decision(
                candidate, control, [egress], None, "propose_belief", "manual_review"
            ).allowed,
            "egress never implies commit",
        )
        self.assertTrue(
            commit_decision(
                candidate,
                control,
                [commit_policy],
                None,
                "propose_belief",
                "manual_review",
            ).allowed
        )
        self.assertFalse(
            commit_decision(
                candidate, control, [commit_policy], None, "other", "manual_review"
            ).allowed,
            "the policy grant must cover the operation",
        )
        approval = {
            "approval_ref": "approval-1",
            "candidate_id": candidate["artifact_id"],
            "proposal_digest": candidate["proposal_digest"],
            "context_id": candidate["context_id"],
            "request_id": candidate["request_id"],
        }
        self.assertTrue(
            commit_decision(
                candidate, control, [], approval, "propose_belief", "manual_review"
            ).allowed
        )
        wrong = dict(approval, proposal_digest=sha256_digest("other"))
        self.assertFalse(
            commit_decision(
                candidate, control, [], wrong, "propose_belief", "manual_review"
            ).allowed,
            "manual approval must bind the exact proposal digest",
        )
        self.assertIn(
            "model_authorization_ref",
            grant["oneOf"][1]["required"],
            "egress always names a model ledger authorization",
        )

        # One valid instance per branch proves the five-way discriminator.
        common = {
            "grant_id": "16161616-1616-4616-8616-161616161616",
            "namespace": "example.private",
            "schema_version": "collab.v1",
            "grant_version": 1,
            "subject": "host:synthetic-1",
            "authorization_ref": "authorization:1",
            "issued_at": "2026-10-22T09:00:00Z",
            "expires_at": None,
            "revoked_at": None,
        }
        branches = [
            dict(
                common,
                kind="access",
                purposes=["consult"],
                data_classes=["private"],
            ),
            dict(
                common,
                kind="egress",
                provider="synthetic-provider",
                endpoint="https://model.invalid/v1",
                model="synthetic-model",
                manifest_hash=sha256_digest("synthetic manifest"),
                privacy_classes=["private"],
                purposes=["consult"],
                model_authorization_ref="24242424-2424-4424-8424-242424242424",
            ),
            dict(
                common,
                kind="commit",
                scope="policy",
                operations=["propose_belief"],
                trigger_kinds=["manual_review"],
            ),
            dict(
                common,
                kind="commit",
                scope="work_generation",
                work_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                generation=1,
            ),
            dict(
                common,
                kind="rerun",
                work_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                generation=1,
            ),
        ]
        for branch in branches:
            with self.subTest(kind=branch["kind"], scope=branch.get("scope")):
                failures = self.checker.validate("grant.json", branch)
                self.assertEqual([], [failure.as_tuple() for failure in failures])
        with self.subTest(check="rerun never carries commit fields"):
            invalid = dict(branches[-1], operations=["propose_belief"])
            self.assertFalse(self.checker.is_valid("grant.json", invalid))

    # ---------------------------------------------------------------- S5
    def test_schema_invalid_cases(self) -> None:
        document = load(EXAMPLE_DIR / "invalid.json")
        self.assertEqual("collab.v1-invalid-vectors-1", document["format"])
        cases = [case for case in document["cases"] if case["layer"] == "schema"]
        self.assertGreaterEqual(len(cases), 8)
        self.assertEqual(len(cases), len({case["name"] for case in cases}))
        for case in cases:
            with self.subTest(case=case["name"]):
                self.assertIn(case["schema"], SCHEMA_FILES)
                failures = self.checker.validate(case["schema"], case["instance"])
                tuples = {(failure.path, failure.keyword) for failure in failures}
                self.assertIn(
                    (case["expect_path"], case["expect_keyword"]),
                    tuples,
                    f"{case['name']} produced {sorted(tuples)}",
                )

    # ---------------------------------------------------------------- S6
    def test_semantic_invalid_cases(self) -> None:
        document = load(EXAMPLE_DIR / "invalid.json")
        cases = [case for case in document["cases"] if case["layer"] == "semantic"]
        self.assertGreaterEqual(len(cases), 13)
        self.assertEqual(len(cases), len({case["name"] for case in cases}))
        permission_cases = {
            "decision.rerun_as_commit",
            "decision.commit_as_rerun",
            "decision.egress_as_commit",
        }
        self.assertTrue(permission_cases.issubset({case["name"] for case in cases}))
        for case in cases:
            with self.subTest(case=case["name"]):
                self.assertIn(case["rule"], RULES)
                violations = check_semantics(case["objects"])
                self.assertIn(
                    case["rule"],
                    {violation.rule for violation in violations},
                    f"{case['name']} produced {violations}",
                )

    # ---------------------------------------------------------------- S7
    def test_consult_chain(self) -> None:
        chain = load(EXAMPLE_DIR / "consult-chain.json")
        self.assertEqual("consult", chain["chain"])
        run = ChainRun(self.checker, chain).run()
        self.assertEqual("schema-valid", run.results[0]["result"])
        self.assertEqual([], run.semantics())

        all_views = [
            entry["value"]
            for entry in run.objects
            if entry["type"] == "experience_view"
        ]
        self.assertEqual(4, len(all_views), "two readable and two redacted views")
        for item in all_views[:2]:
            self.assertEqual("available", item["readability"])
            self.assertTrue(item["body"])
        latest_views: dict[str, dict[str, Any]] = {}
        for item in all_views:
            latest_views[ref_key(item["ref"])] = item
        self.assertEqual(2, len(latest_views))
        for item in latest_views.values():
            self.assertEqual("redacted", item["readability"])
            self.assertIsNone(item["body"])

        deletion = next(
            entry["value"] for entry in run.objects if entry["type"] == "deletion"
        )
        links = [entry["value"] for entry in run.objects if entry["type"] == "source_link"]
        self.assertEqual(
            {(item["owner_kind"], item["owner_id"]) for item in deletion["closure"]},
            closure_of(deletion["ref"], links),
        )
        self.assertEqual(
            {"privacy_epoch_delta": 1, "state_revision_delta": 1},
            deletion["barrier"],
        )
        bindings = [
            entry["value"]
            for entry in run.objects
            if entry["type"] == "context_binding"
        ]
        self.assertEqual(
            [bindings[-1]["context_id"]], deletion["contexts_invalidated"]
        )
        self.assertEqual(
            {
                "collab_input.body",
                "collab_artifact.body",
                "context.rendered_input",
                "model_attempt.request_payload",
                "model_attempt.response_payload",
            },
            set(deletion["cleared"]),
        )

        alt = chain["alt"]["empty_read_set"]
        failures = self.checker.validate("context_binding.json", alt["object"])
        self.assertEqual([], [failure.as_tuple() for failure in failures])
        self.assertEqual([], alt["object"]["read_set"])
        self.assertIsNone(alt["object"]["profile_ref"])
        self.assertIn("v1 assembly never produces", alt["note"])
        self.assertEqual("consult", alt["object"]["context_kind"])
        self.assertIsNone(alt["object"]["trigger_ref"])

        # Every text digest is the digest of the exact text, recomputed here.
        for entry in run.objects:
            if entry["type"] == "input":
                self.assertEqual(
                    sha256_digest(entry["value"]["body"]),
                    entry["value"]["content_digest"],
                )
            if entry["type"] == "artifact":
                self.assertEqual(
                    sha256_digest(entry["value"]["body"]),
                    entry["value"]["content_digest"],
                )

    # ---------------------------------------------------------------- S8
    def test_review_chain(self) -> None:
        chain = load(EXAMPLE_DIR / "review-chain.json")
        self.assertEqual("review", chain["chain"])
        run = ChainRun(self.checker, chain).run()
        self.assertEqual([], run.semantics())

        controls = [
            entry["value"] for entry in run.objects if entry["type"] == "work_control"
        ]
        self.assertEqual(
            [(1, 0, False), (2, 1, True), (3, 1, True)],
            [
                (item["control_version"], item["generation"], item["blocked_after_rejection"])
                for item in controls
            ],
        )
        self.assertIsNone(controls[0]["rerun_authorization_ref"])
        self.assertIsNone(controls[1]["rerun_authorization_ref"])
        self.assertEqual(
            "12121212-1212-4212-8212-121212121212",
            controls[2]["rerun_authorization_ref"],
        )
        self.assertEqual(1, controls[2]["generation"], "re-authorization must not bump generation")

        candidates = {
            entry["value"]["artifact_id"]: entry["value"]
            for entry in run.objects
            if entry["type"] == "candidate"
        }
        rejected = candidates["99999999-9999-4999-8999-999999999999"]
        self.assertEqual("rejected", rejected["status"])
        self.assertIsNone(rejected["rerun_authorization_ref"])
        self.assertIsNone(rejected["commit_authorization_ref"])

        approved = candidates["aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"]
        self.assertEqual("committed", approved["status"])
        self.assertEqual(1, approved["work_generation"])
        self.assertEqual("12121212-1212-4212-8212-121212121212",
                         approved["rerun_authorization_ref"])
        self.assertIsNone(approved["commit_authorization_ref"])
        self.assertEqual("15151515-1515-4515-8515-151515151515", approved["decision_ref"])

        decisions = {
            result["step"]: result["decision"]
            for result in run.results
            if "decision" in result
        }
        self.assertFalse(decisions[8].allowed)
        self.assertEqual("blocked_after_rejection", decisions[8].reason)
        self.assertTrue(decisions[11].allowed)
        self.assertEqual("rerun_grant", decisions[11].reason)
        self.assertTrue(decisions[15].allowed)
        self.assertEqual("manual_approval", decisions[15].reason)

        final_interaction = run.snapshot_of("interaction")
        self.assertEqual("committed", final_interaction["result"])
        self.assertEqual("22223333-4444-4555-8666-777788889999",
                         final_interaction["receipt_request_id"])

        # Candidate digests are recomputed independently of the fixture.
        for candidate in candidates.values():
            self.assertEqual(
                sha256_digest(candidate["body"]), candidate["content_digest"]
            )
            self.assertEqual(
                sha256_digest(canonical(candidate["proposal"])),
                candidate["proposal_digest"],
            )
            self.assertEqual(
                bare_sha256(
                    canonical(
                        {
                            "context_id": candidate["context_id"],
                            "namespace": candidate["namespace"],
                            "operation": candidate["proposal"]["operation"],
                            "request_payload": candidate["proposal"],
                        }
                    )
                ),
                candidate["command_digest"],
            )

        wait = chain["alt"]["wait"]
        failures = self.checker.validate("interaction.json", wait["object"])
        self.assertEqual([], [failure.as_tuple() for failure in failures])
        self.assertEqual("waiting", wait["object"]["result"])
        self.assertIsNotNone(wait["object"]["receipt_request_id"])
        self.assertEqual("interaction", wait["object_type"])
        self.assertIsNotNone(wait["object"]["completed_at"])

        second = chain["alt"]["second_reject"]
        rules = {violation.rule for violation in check_semantics(second["objects"])}
        self.assertIn(second["expect_rule"], rules)
        control = object_of(second["objects"], "work_control")
        grant = object_of(second["objects"], "grant")
        self.assertEqual(2, control["generation"])
        self.assertEqual(1, grant["generation"])
        self.assertFalse(review_start_decision(control, [grant], "2026-10-22T12:00:00Z").allowed)

        no_approval = chain["alt"]["commit_without_approval"]
        rules = {violation.rule for violation in check_semantics(no_approval["objects"])}
        self.assertIn(no_approval["expect_rule"], rules)
        control = object_of(no_approval["objects"], "work_control")
        candidate = object_of(no_approval["objects"], "candidate")
        grant = object_of(no_approval["objects"], "grant")
        self.assertFalse(
            commit_decision(
                candidate, control, [grant], None, "propose_belief", "manual_review"
            ).allowed
        )


if __name__ == "__main__":
    unittest.main()
