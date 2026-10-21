"""Research runs with frozen briefs and post-report evaluation; dry-run unless --execute."""

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from adra.domains import DomainPolicy
from adra.planning import Plan
from adra.state import Budget


def prepare_cases(manifest: Path, selected):
    cases = json.loads(manifest.read_text(encoding="utf-8"))
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)) or not set(selected) <= set(ids):
        raise ValueError("Duplicate or unknown case IDs")
    output = []
    for case in cases:
        if case["id"] not in selected:
            continue
        if not case["id"].replace("-", "").replace("_", "").isalnum():
            raise ValueError("Case IDs must be simple names")
        raw = Path(case["plan"]).read_text(encoding="utf-8")
        plan = Plan.model_validate_json(raw)
        if not all(t.criteria for t in plan.tasks):
            raise ValueError("Final evaluation requires criteria")
        domains = DomainPolicy(case["allowed_domains"], plan.allowed_url_prefixes)
        output.append({**case, "allowed_domains": list(domains.domains),
                       "plan_data": plan.model_dump(),
                       "input_sha256": hashlib.sha256(raw.encode()).hexdigest()})
    return output


def build_command(case, mode, plan_path, output, budget, max_model_calls):
    command = [sys.executable, "-m", "adra.cli", case["plan_data"]["query"],
               "--plan", str(plan_path), "--strategy", "research",
               "--coverage-mode", mode, "--max-tools", str(budget.tool_calls),
               "--max-steps", str(budget.steps), "--max-model-calls", str(max_model_calls),
               "--output", str(output)]
    for domain in case["allowed_domains"]:
        command.extend(["--allowed-domain", domain])
    return command


def collect_result(output: Path, returncode: int):
    runs = list(output.glob("*/run.json"))
    failures = list(output.glob("*/failure.json"))
    if returncode == 0 and len(runs) == 1:
        run = json.loads(runs[0].read_text(encoding="utf-8"))
        return {"status": "completed", "metrics": run["metrics"],
                "stop_reason": run["stop_reason"], "run": str(runs[0])}
    result = {"status": "failed", "returncode": returncode}
    if len(failures) == 1:
        failure = json.loads(failures[0].read_text(encoding="utf-8"))
        result.update(error_type=failure["error_type"],
                      total_tokens=failure.get("total_tokens"), failure=str(failures[0]))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("data/live-eval/manifest.json"))
    parser.add_argument("--cases", nargs="+", default=[
        "langgraph-state-routing", "python-asyncio", "pydantic-validation"])
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--max-tools", type=int, default=8)
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--max-model-calls", type=int, default=32)
    parser.add_argument("--output", type=Path, default=Path("outputs"))
    parser.add_argument("--execute", action="store_true", help="Actually call configured providers")
    args = parser.parse_args()
    try:
        if not 1 <= args.repeat <= 20 or not 1 <= args.max_model_calls <= 1000:
            raise ValueError("Invalid repeat count or model-call budget")
        budget = Budget(tool_calls=args.max_tools, steps=args.max_steps)
        cases = prepare_cases(args.manifest, args.cases)
    except (ValueError, OSError, KeyError) as exc:
        parser.error(f"Invalid batch inputs ({type(exc).__name__})")
    count = len(cases) * args.repeat
    print(f"{count} runs; each <= {budget.tool_calls} tools, {args.max_model_calls} model requests.")
    if not args.execute:
        print("Dry run: no API calls. Add --execute to start.")
        print(json.dumps([{"id": c["id"], "query": c["plan_data"]["query"],
                           "allowed_domains": c["allowed_domains"],
                           "allowed_url_prefixes": c["plan_data"]["allowed_url_prefixes"]}
                          for c in cases], ensure_ascii=False, indent=2))
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    batch = args.output / f"live-eval-{stamp}-{uuid4().hex[:8]}"
    (batch / "plans").mkdir(parents=True)
    for case in cases:
        (batch / "plans" / f"{case['id']}.json").write_text(
            json.dumps(case["plan_data"], ensure_ascii=False, indent=2), encoding="utf-8")
    metadata = {"cases": cases, "budget": budget.model_dump(),
                "max_model_calls": args.max_model_calls, "repeat": args.repeat}
    (batch / "manifest.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    records = []
    for repeat in range(args.repeat):
        # Each report is evaluated after generation; no second research run for scoring.
        modes = ("semantic",)
        for case in cases:
            for mode in modes:
                output = batch / case["id"] / mode / str(repeat + 1)
                command = build_command(case, mode, batch / "plans" / f"{case['id']}.json",
                                        output, budget, args.max_model_calls)
                try:
                    process = subprocess.run(command, check=False)
                    record = collect_result(output, process.returncode)
                except OSError as exc:
                    record = {"status": "failed", "error_type": type(exc).__name__}
                record.update(case=case["id"], mode=mode, repeat=repeat + 1)
                records.append(record)
                (batch / "summary.json").write_text(
                    json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    review = ["# 人工复核表", "", "请依据同一标准独立核验报告，不直接复制模型覆盖评分。",
              "", "| Case | Mode | Repeat | Criterion | 人工结论 | 引文支持与备注 |",
              "| --- | --- | --- | --- | --- | --- |"]
    for record in records:
        case = next(c for c in cases if c["id"] == record["case"])
        for task in case["plan_data"]["tasks"]:
            for criterion in task["criteria"]:
                review.append(f"| {case['id']} | {record['mode']} | {record['repeat']} | "
                              f"{task['id']}/{criterion['id']} | 待核验 | |")
    (batch / "review.md").write_text("\n".join(review) + "\n", encoding="utf-8")
    print(f"Batch artifacts: {batch}")
    if any(r["status"] != "completed" for r in records):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
