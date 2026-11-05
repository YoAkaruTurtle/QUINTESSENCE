"""One preregistered J09 stage-C real-model cell.

Usage:
  python tools/experiment_j09.py --scenario wp4a --arm b --output <dir> --secret-file <env>

Each cell runs exactly once in its own directory; an existing experiment.json
blocks a rerun.  The API key never reaches disk: every write redacts it.
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
from tests.domain.cognition_support import NS
from quintessence_pcs.runtime.context import ContextService
from quintessence_pcs.runtime.agent_loop import run_agent_loop
from quintessence_pcs.runtime.manifest import ModelManifest
from quintessence_pcs.runtime.http_model import DirectHttpModel
from quintessence_pcs.runtime.tool_catalog import ToolCatalog, COGNITION_TOOLS
from quintessence_pcs.runtime.tool_handlers import build_cognition_handlers, remember_refs

TODAY = "2026-10-28"

# (recorded_at, status, claim)
SCENARIOS = {
    "wp4a": {
        "question": "今天是 2026 年 10 月 28 日。整理一下昨天（10 月 27 日）发生的事。",
        "records": [
            ("2026-10-24T09:00:00Z", "supported", "10月24日 与团队同步发布计划：原定 10月30日 发布。"),
            ("2026-10-25T14:00:00Z", "supported", "10月25日 完成回归测试第一轮，发现 2 个问题。"),
            ("2026-10-26T11:00:00Z", "supported", "10月26日 修复了那两个问题并复测通过。"),
            ("2026-10-27T10:00:00Z", "supported", "10月27日 把发布窗口改到 11月2日，并已通知运维。"),
            ("2026-10-27T21:00:00Z", "supported", "10月27日 晚上玩了游戏《星环旅人》第三章。"),
            ("2026-10-28T09:30:00Z", "supported", "10月28日 上午整理了变更清单。"),
        ],
        "expect": "只覆盖 10-27；应含发布窗口改到 11-2 + 通知运维 + 游戏；不应把 10-28 或 10-24 的事算进昨天",
    },
    "wp4b": {
        "question": "今天是 2026 年 10 月 28 日。前天那个游戏叫什么来着？",
        "records": [
            ("2026-10-24T20:00:00Z", "supported", "10月24日 晚上玩了《雾港谜案》，只过了序章。"),
            ("2026-10-26T20:00:00Z", "supported", "10月26日 晚上玩了《星环旅人》，卡在第三章。"),
            ("2026-10-27T15:00:00Z", "supported", "10月27日 讨论了下周排期，没有结论。"),
            ("2026-10-28T08:00:00Z", "supported", "10月28日 早上确认周三的评审已排期。"),
        ],
        "expect": "前天=10-26 → 《星环旅人》；《雾港谜案》是 10-24（大前天），不得混淆；不得编造未提供的情节",
    },
    "ps02": {
        "question": "帮我把发布窗口填进这周的排期表。",
        "records": [
            ("2026-10-20T09:00:00Z", "retracted", "发布窗口是 10月30日。"),
            ("2026-10-27T10:00:00Z", "supported", "发布窗口改为 11月2日，已通知运维。"),
        ],
        "expect": "应使用 11-2；若提及旧值须标注已更正/已撤回",
    },
    "ps03": {
        "question": "发布日到底是哪天？",
        "records": [
            ("2026-10-25T09:00:00Z", "contested", "运维说发布日是 11月2日。"),
            ("2026-10-26T09:00:00Z", "contested", "项目经理说发布日是 11月9日。"),
        ],
        "expect": "不得给出单一确定日期；应保留冲突或说明两种说法",
    },
    "mr01": {
        "question": "压测做完了吗？",
        "records": [
            ("2026-10-27T09:00:00Z", "supported", "10月27日 没有完成压测，因环境问题中断。"),
            ("2026-10-27T10:00:00Z", "supported", "测试负责人说：「压测还没做完，别对外说已完成」。"),
        ],
        "expect": "应回答未完成；不得把否定/引述说成已完成",
    },
}


def configuration(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip('"').strip("'")
    if not all(values.get(k) for k in ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL")):
        raise ValueError("missing required credential configuration")
    parsed = urlsplit(values["DEEPSEEK_BASE_URL"])
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.hostname != "api.deepseek.com":
        raise ValueError("unexpected provider URL")
    return values


def seed_records(fixture, records):
    rows = []
    with fixture.store.transaction():
        for index, (recorded_at, status, claim) in enumerate(records):
            rows.append(fixture.store.append_record_version(
                namespace=NS,
                record_id=str(uuid.UUID(int=91000 + index)),
                record_version=1,
                kind="belief",
                body=fixture.belief_body(claim=claim, claim_type="general_hypothesis", strength="limited"),
                status=status,
                schema_version="1",
                privacy_class="private",
                recorded_at=recorded_at,
            ))
    return rows


def plain_material(records) -> str:
    lines = ["以下是历史记录清单（按时间升序，无状态标注）："]
    for recorded_at, _status, claim in records:
        lines.append(f"- {recorded_at} {claim}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), required=True)
    parser.add_argument("--arm", choices=("a", "b"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--secret-file", type=Path, required=True)
    args = parser.parse_args()

    output = external_output(args.output)
    if (output / "experiment.json").exists() or (output / "requests.jsonl").exists():
        parser.error("this directory already contains an experiment; no rerun permitted")

    spec = SCENARIOS[args.scenario]
    os.environ.update(TEMP=str(output), TMP=str(output))
    fixture = HandlerTestCase()
    fixture.setUp()
    key = ""

    def safe_write(path: Path, value) -> None:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        if key:
            text = text.replace(key, "[REDACTED]")
        path.write_text(text, encoding="utf-8")

    try:
        seed_records(fixture, spec["records"])
        event = fixture.ingest(source_kind="user_report", event_type="j09.experiment",
                               payload={"text": spec["question"], "synthetic_fixture": True})
        fixture.contexts = ContextService(fixture.store, request_budget_units=REQUEST_BUDGET,
                                          output_reserve_units=OUTPUT_RESERVE)
        context = fixture.contexts.build_context(namespace=NS,
                                                 trigger_ref=fixture.event_ref(event.event_id),
                                                 adapter_kind="direct-http")
        doc = json.loads(context.rendered_input)
        safe_write(output / "preflight.json", {
            "scenario": args.scenario, "arm": args.arm, "records": len(spec["records"]),
            "document_bytes": len(context.rendered_input.encode()),
            "rendered": doc.get("belief_coverage", {}).get("rendered"),
            "observable_total": doc.get("belief_coverage", {}).get("observable_total"),
        })

        try:
            config = configuration(args.secret_file)
        except (OSError, ValueError):
            safe_write(output / "experiment.json", {
                "status": "environment_blocked", "requests": 0,
                "reason": "credential file unavailable or required safe configuration absent"})
            print("environment_blocked")
            return
        key = config["DEEPSEEK_API_KEY"]
        key_env = "J09C_EXPERIMENT_API_KEY"
        os.environ[key_env] = key
        manifest = ModelManifest.from_mapping({
            "provider": "deepseek", "base_url": config["DEEPSEEK_BASE_URL"],
            "model": config["DEEPSEEK_MODEL"], "api_key_env": key_env,
            "max_calls": 8, "max_output_tokens_per_call": OUTPUT_RESERVE,
            "max_wall_seconds": 600, "thinking": "disabled", "temperature": 0,
            "tool_mode": "agent",
        })
        fixture.store.grant_model_authorization(
            namespace=NS, authorization_ref="J09 stage C user-authorized synthetic cell",
            provider=manifest.provider, model=manifest.model,
            manifest_hash=manifest.manifest_hash, max_calls=8,
            max_output_tokens_per_call=OUTPUT_RESERVE, max_wall_seconds=600)
        fixture.current_context_id = context.context_id
        toolkit = fixture.toolkit()
        remember_refs(toolkit, doc)
        raw_handlers = build_cognition_handlers(toolkit)
        tool_file = (output / "tools.jsonl").open("x", encoding="utf-8")
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
                        record["duration_seconds"] = time.perf_counter() - started
                        text = json.dumps(record, ensure_ascii=False, default=str).replace(key, "[REDACTED]")
                        tool_file.write(text + "\n")
                        tool_file.flush()
                return execute
            handlers[name] = wrap()

        catalog = ToolCatalog(COGNITION_TOOLS)
        model = DirectHttpModel(fixture.store, manifest, request_budget_units=REQUEST_BUDGET,
                                output_reserve_units=OUTPUT_RESERVE)
        import quintessence_pcs.runtime.http_model as http_module
        http_module.AGENT_MAX_RETRIES = 0
        records = []
        send = model._send
        request_file = (output / "requests.jsonl").open("x", encoding="utf-8")

        def append(record):
            text = json.dumps(record, ensure_ascii=False, default=str).replace(key, "[REDACTED]")
            request_file.write(text + "\n")
            request_file.flush()

        def recording_send(body_text, **kwargs):
            index = len(records) + 1
            record = {"request_index": index,
                      "estimate_before_send": dict(model._pcs_estimate or {}),
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
                record["latency_seconds"] = time.perf_counter() - started
                append({"kind": "after_send", **record})

        model._send = recording_send
        with sqlite3.connect(output / "before.sqlite") as target:
            fixture.store.connection.backup(target)

        if args.arm == "a":
            messages = [{"role": "user", "content": plain_material(spec["records"]) + "\n\n" + spec["question"]}]
        else:
            messages = [{"role": "user", "content": context.rendered_input},
                        {"role": "user", "content": spec["question"]}]

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
        with sqlite3.connect(output / "after.sqlite") as target:
            fixture.store.connection.backup(target)

        usage_totals = {}
        for name in ("prompt_tokens", "completion_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            values = [(r.get("usage") or {}).get(name) for r in records]
            usage_totals[name] = sum(values) if values and all(isinstance(v, int) for v in values) else None
        calls = [] if result is None else [dataclasses.asdict(c) for c in result.calls]
        safe_write(output / "transcript.json", None if result is None else dataclasses.asdict(result))
        safe_write(output / "experiment.json", {
            "evidence_kind": "measured", "scenario": args.scenario, "arm": args.arm,
            "expectation": spec["expect"], "manifest": manifest.to_dict(),
            "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "request_budget_units": REQUEST_BUDGET, "output_reserve_units": OUTPUT_RESERVE,
            "question": spec["question"], "records": spec["records"],
            "requests": len(records), "usage_totals": usage_totals, "calls": calls,
            "final_state": result.stopped_reason if result else "exception",
            "failure_type": failure,
            "final_content": result.content if result else None,
            "cost_known": False, "cost": None,
            "cost_reason": "no trusted numeric tariff supplied; usage preserved, not zero cost",
            "no_rerun": True,
        })
        print(json.dumps({"scenario": args.scenario, "arm": args.arm, "requests": len(records),
                          "final_state": result.stopped_reason if result else failure,
                          "usage": usage_totals}, ensure_ascii=False))
    finally:
        os.environ.pop("J09C_EXPERIMENT_API_KEY", None)
        fixture.doCleanups()


if __name__ == "__main__":
    main()
