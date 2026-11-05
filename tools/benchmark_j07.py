"""Reproducible synthetic J07 scan/assembly benchmark; all output is external.

Run with --output pointing to an evidence directory outside the checkout.
No network or credentials are used. Fixtures are storage fixtures, not evidence
of model reasoning or of governance commits.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]
from tests.test_j07_retrieval import seed
from tests.domain.cognition_support import CognitionTestCase, NS
from quintessence_pcs.runtime.context import ContextService
from quintessence_pcs.domains.refs import EventRef
from quintessence_pcs.runtime.agent_protocol import build_agent_request_body
from quintessence_pcs.runtime.budget import estimate_units, request_units
from quintessence_pcs.runtime.manifest import ModelManifest
from quintessence_pcs.runtime.tool_catalog import ToolCatalog

REQUEST_BUDGET = 65536
OUTPUT_RESERVE = 4096


def external_output(path):
    path = path.resolve()
    if path == ROOT or ROOT in path.parents or path in ROOT.parents:
        raise ValueError("output and checkout must be disjoint")
    path.mkdir(parents=True, exist_ok=True)
    return path


def p95(values):
    return sorted(values)[math.ceil(0.95 * len(values)) - 1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=10)
    args = parser.parse_args()
    if args.samples < 5:
        parser.error("use at least five measurements per cell")
    output = external_output(args.output)
    os.environ.update(TEMP=str(output), TMP=str(output))
    raw, summary = [], []
    manifest = ModelManifest.from_mapping({
        "provider": "synthetic", "base_url": "http://127.0.0.1:9/v1", "model": "offline",
        "api_key_env": "J07_OFFLINE_UNUSED", "max_calls": 9, "max_output_tokens_per_call": OUTPUT_RESERVE,
        "max_wall_seconds": 300, "thinking": "disabled", "temperature": 0, "tool_mode": "agent"})
    for count in (100, 1000):
        fixture = CognitionTestCase()
        fixture.setUp()
        try:
            seed(fixture, count, claim_chars=120)
            event = fixture.ingest(source_kind="user_report", event_type="j07.question",
                                   payload={"text": "synthetic benchmark question"})
            fixture.self_view.build_self_view(namespace=NS)
            for cap in (24576, 32768, 49152, 65536):
                service = ContextService(fixture.store, pcs_context_max_bytes=cap,
                    request_budget_units=REQUEST_BUDGET, output_reserve_units=OUTPUT_RESERVE)
                scans, assemblies = [], []
                for sample in range(args.samples):
                    with fixture.store.transaction():
                        started = time.perf_counter_ns()
                        material = service._read_materials_locked(NS, EventRef(NS, event.event_id),
                            fixture.store.event_header(NS, event.event_id))
                        scanned = time.perf_counter_ns()
                        env = service._assemble_locked(ns=NS, run_id=str(uuid.uuid4()), trigger=EventRef(NS, event.event_id),
                            watermarks=fixture.store.context_watermarks(NS), materials=material,
                            adapter_kind="direct-http", request_budget_units=REQUEST_BUDGET,
                            output_reserve_units=OUTPUT_RESERVE)
                        finished = time.perf_counter_ns()
                    scan_ms, assembly_ms = (scanned-started)/1e6, (finished-scanned)/1e6
                    scans.append(scan_ms)
                    assemblies.append(assembly_ms)
                    doc = json.loads(env.rendered_input)
                    c = doc["belief_coverage"]
                    assert c["observable_total"] == count
                    assert c["rendered"] + c["omitted_size"] + c["omitted_budget"] == count
                    assert len(env.rendered_input.encode()) <= cap
                    assert c["rendered"] <= 200
                    assert len(json.dumps({"belief_coverage": c}).encode()) <= 256
                    raw.append(dict(count=count, cap_bytes=cap, sample=sample, scan_ms=scan_ms,
                        assembly_ms=assembly_ms, document_bytes=len(env.rendered_input.encode()), coverage=c))
                body = build_agent_request_body(manifest, messages=[{"role": "user", "content": env.rendered_input}],
                    tools=ToolCatalog.cognition_and_web().definitions(), max_tokens=OUTPUT_RESERVE)
                first_units = request_units(estimate_units(body), OUTPUT_RESERVE)
                summary.append(dict(count=count, cap_bytes=cap, rendered=c["rendered"],
                    document_bytes=len(env.rendered_input.encode()), scan_median_ms=statistics.median(scans),
                    scan_p95_ms=p95(scans), assembly_median_ms=statistics.median(assemblies), assembly_p95_ms=p95(assemblies),
                    first_request_units=first_units, request_budget_units=REQUEST_BUDGET,
                    offline_estimated_additional_rounds={label: max(0, (REQUEST_BUDGET-first_units)//growth)
                        for label, growth in (("low_growth_256_U",256),("typical_growth_1024_U",1024),("higher_growth_4096_U",4096))}))
                print(f"count={count} cap={cap} rendered={c['rendered']} scan={statistics.median(scans):.2f}ms assemble={statistics.median(assemblies):.2f}ms", flush=True)
        finally:
            fixture.doCleanups()
    metadata = {"evidence_kind": "measured", "git_head": subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
        "samples_per_cell": args.samples, "summary": summary, "raw": raw,
        "limits": ["scan and candidate selection grow with record count; two sizes cannot be extrapolated",
            "offline rounds are estimates in PCS units, not provider tokens or observed conversations",
            "10 repeated samples on one machine; p95 is nearest-rank, not a production SLO"],
        "cache_cost_estimate": {"evidence_kind": "estimate", "price_numbers": None,
            "no_cache": "Tin*Pin_miss + Tout*Pout", "cache_fraction_h": "Tin*((1-h)*Pin_miss+h*Pin_hit)+Tout*Pout",
            "fractions": [0,0.5,0.9], "assumptions": "Tin/Tout are provider tokens; no byte-to-token conversion assumed"}}
    (output/"benchmark.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output/"benchmark.csv").open("w", newline="", encoding="utf-8") as handle:
        fields = [key for key in summary[0] if key != "offline_estimated_additional_rounds"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: row[key] for key in fields} for row in summary)


if __name__ == "__main__":
    main()
