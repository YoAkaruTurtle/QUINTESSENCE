"""Read the Lab store's public evidence rows without printing body content."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


def query(path: Path, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(sql, params)]
    finally:
        connection.close()


def count(path: Path, table: str) -> int:
    rows = query(path, f"SELECT COUNT(*) AS n FROM {table}")
    return int(rows[0]["n"]) if rows else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--expect-state", default=None)
    args = parser.parse_args()
    path = args.database
    if not path.exists():
        raise SystemExit("database not found")

    interactions = query(
        path,
        "SELECT interaction_id, mode, result, access_ref, policy_ref, "
        "context_id, created_at, completed_at FROM collab_interactions "
        "ORDER BY created_at",
    )
    inputs = query(
        path,
        "SELECT i.input_id, i.content_version, i.origin_kind, i.privacy_class, "
        "i.retention_policy, length(p.body_json) AS body_bytes, "
        "i.content_digest FROM collab_inputs i JOIN private_payloads p "
        "ON p.payload_ref = i.body_payload_ref ORDER BY i.recorded_at",
    )
    contexts = query(
        path,
        "SELECT context_id, run_id FROM context_snapshots ORDER BY rowid",
    )
    attempts = query(
        path,
        "SELECT attempt_id, attempt_no, context_id, outcome, provider, "
        "model_requested, request_hash FROM model_attempts ORDER BY attempt_no",
    )
    artifacts = query(
        path,
        "SELECT a.artifact_id, a.kind, a.origin_kind, a.context_id, "
        "a.attempt_ref_json, length(p.body_json) AS body_bytes "
        "FROM collab_artifacts a JOIN private_payloads p "
        "ON p.payload_ref = a.body_payload_ref ORDER BY a.created_at",
    )
    links = query(
        path,
        "SELECT interaction_id, input_id, content_version FROM collab_interaction_inputs "
        "ORDER BY position",
    )
    counts = {
        table: count(path, table)
        for table in (
            "collab_interactions",
            "collab_inputs",
            "collab_artifacts",
            "model_attempts",
            "context_snapshots",
            "collab_interaction_inputs",
            "records",
            "record_versions",
            "pending_work",
            "work_progress",
            "event_journal",
        )
    }
    report = {
        "database": str(path),
        "counts": counts,
        "interactions": interactions,
        "inputs": inputs,
        "contexts": contexts,
        "attempts": attempts,
        "artifacts": artifacts,
        "interaction_inputs": links,
    }
    passed = True
    if args.expect_state is not None:
        passed = bool(interactions) and interactions[-1]["result"] == args.expect_state
        report["expected_state"] = args.expect_state
        report["state_matches"] = passed
    report["passed"] = passed
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
