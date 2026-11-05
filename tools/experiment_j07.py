"""One authorized synthetic J07 real-model turn, with a strict offline preflight.

The caller supplies the secret-file and output paths. Never logs environment
contents, credentials or exception strings from transports. Output is external.
An existing experiment.json or requests.jsonl blocks reruns in that directory.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "tools")]
from benchmark_j07 import external_output, REQUEST_BUDGET, OUTPUT_RESERVE
from tests.test_tool_handlers import HandlerTestCase
from tests.test_j07_retrieval import seed
from tests.domain.cognition_support import NS
from quintessence_pcs.runtime.context import ContextService
from quintessence_pcs.runtime.agent_loop import run_agent_loop
from quintessence_pcs.runtime.manifest import ModelManifest
from quintessence_pcs.runtime.http_model import DirectHttpModel
from quintessence_pcs.runtime.tool_catalog import ToolCatalog, COGNITION_TOOLS
from quintessence_pcs.runtime.tool_handlers import build_cognition_handlers, remember_refs

USER_INPUT = "我们之前记过什么跟部署窗口有关的事？顺便说一句，测试环境这周改到周四了。"


def fixture_context(fixture):
    seed(fixture, 10, claim_chars=1800, status="supported")
    seed(fixture, 1, claim_chars=1800, offset=10, status="contested")
    seed(fixture, 1, claim_chars=1800, offset=11, status="proposed")
    event = fixture.ingest(source_kind="user_report", event_type="j07.question",
                           payload={"text": USER_INPUT, "synthetic_fixture": True})
    # J07's experiment is defined at a 24 KiB brief; pin it so the 24576
    # assertions below keep their meaning after the product default moved.
    fixture.contexts = ContextService(fixture.store, pcs_context_max_bytes=24576,
                                    request_budget_units=REQUEST_BUDGET,
                                    output_reserve_units=OUTPUT_RESERVE)
    context = fixture.contexts.build_context(namespace=NS, trigger_ref=fixture.event_ref(event.event_id),
                                             adapter_kind="direct-http")
    doc = json.loads(context.rendered_input)
    coverage = doc["belief_coverage"]
    assert coverage["observable_total"] == 12 and 0 < coverage["rendered"] < 12
    assert len(context.rendered_input.encode()) <= 24576
    # Disjoint configuration is used only offline to prove full payload size.
    large = ContextService(fixture.store, pcs_context_max_bytes=131072,
        request_budget_units=REQUEST_BUDGET, output_reserve_units=OUTPUT_RESERVE)
    full = large.build_context(namespace=NS, trigger_ref=fixture.event_ref(event.event_id),
                               adapter_kind="direct-http")
    assert len(json.loads(full.rendered_input)["beliefs"]) == 12
    assert len(full.rendered_input.encode()) > 24576
    return context, {"cap_bytes": 24576, "coverage": coverage,
        "document_bytes": len(context.rendered_input.encode()),
        "unclipped_document_bytes": len(full.rendered_input.encode()),
        "fixture": "12 synthetic storage records; 10 supported, 1 contested, 1 proposed; 1800 CJK filler chars each"}


def configuration(path):
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip("\"").strip("'")
    if not all(values.get(key) for key in ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL")):
        raise ValueError("missing required credential configuration")
    parsed = urlsplit(values["DEEPSEEK_BASE_URL"])
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.hostname != "api.deepseek.com":
        raise ValueError("unexpected provider URL")
    return values


def backup(store, path):
    with sqlite3.connect(path) as target:
        store.connection.backup(target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--secret-file", type=Path)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    output = external_output(args.output)
    if (output/"experiment.json").exists() or (output/"requests.jsonl").exists():
        parser.error("this directory already contains an experiment; no rerun permitted")
    os.environ.update(TEMP=str(output), TMP=str(output))
    fixture = HandlerTestCase()
    fixture.setUp()
    key = ""
    def safe_write(path, value):
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        if key:
            text = text.replace(key, "[REDACTED]")
        path.write_text(text, encoding="utf-8")
    try:
        context, preflight = fixture_context(fixture)
        safe_write(output/"preflight.json", preflight)
        (output/"initial-context.json").write_text(context.rendered_input, encoding="utf-8")
        if args.preflight_only:
            print(json.dumps(preflight, ensure_ascii=False))
            return
        if args.secret_file is None:
            parser.error("--secret-file required for the authorized real experiment")
        try:
            config = configuration(args.secret_file)
        except (OSError, ValueError):
            safe_write(output/"experiment.json", {"status": "environment_blocked", "requests": 0,
                "reason": "credential file unavailable or required safe configuration absent"})
            return
        key = config["DEEPSEEK_API_KEY"]
        key_env = "J07_EXPERIMENT_API_KEY"
        os.environ[key_env] = key
        manifest = ModelManifest.from_mapping({
            "provider": "deepseek", "base_url": config["DEEPSEEK_BASE_URL"], "model": config["DEEPSEEK_MODEL"],
            "api_key_env": key_env, "max_calls": 9, "max_output_tokens_per_call": OUTPUT_RESERVE,
            "max_wall_seconds": 600, "thinking": "disabled", "temperature": 0, "tool_mode": "agent"})
        fixture.store.grant_model_authorization(namespace=NS,
            authorization_ref="J07 v0.8 user-authorized single synthetic turn",
            provider=manifest.provider, model=manifest.model, manifest_hash=manifest.manifest_hash,
            max_calls=9, max_output_tokens_per_call=OUTPUT_RESERVE, max_wall_seconds=600)
        fixture.current_context_id = context.context_id
        toolkit = fixture.toolkit()
        remember_refs(toolkit, json.loads(context.rendered_input))
        raw_handlers = build_cognition_handlers(toolkit)
        tool_file = (output/"tools.jsonl").open("x", encoding="utf-8")
        handlers = {}
        for name, handler in raw_handlers.items():
            def wrap(name=name, handler=handler):
                def execute(arguments):
                    started = time.perf_counter()
                    record = {"tool": name, "arguments": arguments}
                    try:
                        result = handler(arguments)
                        record.update(ok=True, result=result)
                        return result
                    except Exception as error:
                        record.update(ok=False, error_type=type(error).__name__)
                        raise
                    finally:
                        record["duration_seconds"] = time.perf_counter()-started
                        text = json.dumps(record, ensure_ascii=False, default=str).replace(key, "[REDACTED]")
                        tool_file.write(text + "\n")
                        tool_file.flush()
                return execute
            handlers[name] = wrap()
        catalog = ToolCatalog(COGNITION_TOOLS)
        model = DirectHttpModel(fixture.store, manifest, request_budget_units=REQUEST_BUDGET,
                                output_reserve_units=OUTPUT_RESERVE)
        # One failed transport attempt remains a result, not a reason to rerun.
        import quintessence_pcs.runtime.http_model as http_module
        http_module.AGENT_MAX_RETRIES = 0
        records = []
        send = model._send
        request_file = (output/"requests.jsonl").open("x", encoding="utf-8")
        def append(record):
            text = json.dumps(record, ensure_ascii=False, default=str).replace(key, "[REDACTED]")
            request_file.write(text + "\n")
            request_file.flush()
        def recording_send(body_text, **kwargs):
            index = len(records)+1
            record = {"request_index": index, "estimate_before_send": dict(model._pcs_estimate or {}),
                "request_body": json.loads(body_text), "cost_known": False, "cost": None}
            records.append(record)
            append({"kind": "before_send", **record})
            started = time.perf_counter()
            try:
                response = send(body_text, **kwargs)
                payload = json.loads(response)
                record.update(response_body=payload, usage=payload.get("usage"), status="response")
                return response
            except Exception as error:
                record.update(status="failed", error_type=type(error).__name__)
                raise
            finally:
                record["latency_seconds"] = time.perf_counter()-started
                append({"kind": "after_send", **record})
        model._send = recording_send
        backup(fixture.store, output/"before.sqlite")
        messages = [{"role": "user", "content": context.rendered_input},
                    {"role": "user", "content": USER_INPUT}]
        result, failure = None, None
        try:
            with model.bind_run(namespace=NS, context=context):
                result = run_agent_loop(model, catalog, messages=messages,
                    handlers=handlers, max_steps=8, deadline_seconds=600)
        except Exception as error:
            failure = type(error).__name__
        finally:
            request_file.close()
            tool_file.close()
        backup(fixture.store, output/"after.sqlite")
        transcript = None if result is None else dataclasses.asdict(result)
        safe_write(output/"transcript.json", transcript)
        usage_totals = {name: (sum(r["usage"][name] for r in records)
                if records and all(isinstance((r.get("usage") or {}).get(name), int) for r in records) else None)
            for name in ("prompt_tokens","completion_tokens","prompt_cache_hit_tokens","prompt_cache_miss_tokens")}
        calls = [] if result is None else [dataclasses.asdict(c) for c in result.calls]
        names = [c["tool"] for c in calls]
        safe_write(output/"experiment.json", {"evidence_kind": "measured", "manifest": manifest.to_dict(),
            "git_head": subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
            "request_budget_units": REQUEST_BUDGET, "output_reserve_units": OUTPUT_RESERVE,
            "preflight": preflight, "user_input": USER_INPUT, "requests": len(records),
            "records": records, "usage_totals": usage_totals, "calls": calls,
            "observed_read": any(n in names for n in ("cognition.belief.read","cognition.partition")),
            "observed_propose": "cognition.propose" in names,
            "final_state": result.stopped_reason if result else "exception", "failure_type": failure,
            "final_content": result.content if result else None,
            "cost_known": False, "cost": None, "cost_reason": "no trusted numeric tariff supplied; usage preserved, not zero cost",
            "transport_retries": 0, "no_rerun": True})
        print(json.dumps({"requests": len(records), "final_state": result.stopped_reason if result else failure,
                          "usage_totals": usage_totals, "tools": names}, ensure_ascii=False))
    finally:
        os.environ.pop("J07_EXPERIMENT_API_KEY", None)
        fixture.doCleanups()


if __name__ == "__main__":
    main()
