import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from adra.graph import run
from adra.configuration import RuntimeConfig
from adra.planning import Plan
from adra.state import Budget
from adra.tools import TavilyTools
from adra.domains import DomainPolicy
from adra.provenance import runtime_provenance


def save(result: dict, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    report = [f"# {result['query']}", ""]
    for claim in result["report"]["claims"]:
        refs = " ".join(f"[{i}]" for i in claim["evidence_ids"])
        report.extend([f"{claim['text']} {refs}", ""])
    report.extend(["## 局限性", *result["report"]["limitations"], "", "## 来源"])
    for e in result["evidence"]:
        report.append(f"- [{e['id']}] {e['title']}: {e['url']}")
    (directory / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    (directory / "run.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / "trajectory.jsonl").write_text(
        "".join(json.dumps(t, ensure_ascii=False) + "\n" for t in result["transitions"]),
        encoding="utf-8")
    if "plan" in result:
        (directory / "plan.json").write_text(
            json.dumps(result["plan"], ensure_ascii=False, indent=2), encoding="utf-8")
    if "evaluation_status" in result:
        (directory / "evaluation.json").write_text(json.dumps({
            "status": result["evaluation_status"], "target": "final_report",
            "metric": "fraction_of_report_criteria_supported_by_retrieved_sources",
            "coverage": result.get("coverage", {}),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    if result.get("coverage"):
        (directory / "coverage.json").write_text(
            json.dumps(result["coverage"], ensure_ascii=False, indent=2), encoding="utf-8")
    if "research_notes" in result:
        (directory / "research_notes.json").write_text(
            json.dumps(result["research_notes"], ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Global evidence research agent")
    parser.add_argument("query", nargs="?")
    parser.add_argument("--snapshot", type=Path, help="Removed from production; use adra.legacy.cli for historical replay")
    parser.add_argument("--max-tools", type=int, default=6)
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument("--max-model-calls", type=int, default=32)
    parser.add_argument("--output", type=Path, default=Path("outputs"))
    parser.add_argument("--allowed-domain", action="append", default=[],
                        help="Allowed exact source hostname; repeat for multiple hosts")
    parser.add_argument("--strategy", choices=["research"], default="research")
    parser.add_argument("--coverage-mode", choices=["none", "semantic"], default="semantic",
                        help="Final report evaluation, never an online stopping rule")
    parser.add_argument("--coverage-replay", type=Path,
                        help="Historical replay only, rejected by this entry point")
    plans = parser.add_mutually_exclusive_group()
    plans.add_argument("--plan", type=Path, help="Research brief JSON with scope, topics and acceptance criteria")
    plans.add_argument("--auto-plan", action="store_true", help="Generate a research brief online (default without --plan)")
    args = parser.parse_args()
    try:
        allowed_domains = DomainPolicy(args.allowed_domain).domains
    except ValueError as exc:
        parser.error(str(exc))
    policy = None
    plan = None
    runtime = None
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    directory = args.output / run_id
    try:
        budget = Budget(tool_calls=args.max_tools, steps=args.max_steps)
        if args.snapshot or args.coverage_replay:
            parser.error("Historical snapshots use python -m adra.evaluation; the research CLI is live")
        plan = Plan.model_validate_json(args.plan.read_text(encoding="utf-8")) if args.plan else None
        query = args.query
        if not query:
            parser.error("Provide a research query")
        model_name = os.environ.get("ADRA_MODEL")
        key = os.environ.get("TAVILY_API_KEY")
        if not model_name or not key:
            parser.error("Live mode requires ADRA_MODEL and TAVILY_API_KEY; see README")
        runtime = RuntimeConfig.from_env(args.max_model_calls)
        policy, tools = runtime.create_policy(), TavilyTools(key, allowed_domains, include_raw_content=True)
        mode = "live"
        if not query.strip():
            parser.error("Research query must not be empty")
        if args.auto_plan or plan is None:
            plan = policy.generate_brief(query, budget)
        if query.strip() != plan.query.strip():
            parser.error("Plan query must match the research query")
        result = run(query, policy, tools, budget, task_id=run_id, allowed_domains=allowed_domains,
                     plan=plan, evaluate=args.coverage_mode == "semantic")
        result["mode"] = mode
        result["strategy"] = args.strategy
        result["model"] = os.environ.get("ADRA_MODEL") if mode == "live" else None
        result["plan_source"] = "file" if args.plan else "generated"
        if runtime:
            result["runtime_config"] = runtime.model_dump()
        save(result, directory)
    except Exception as exc:
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
            failure = {"status": "failed", "error_type": type(exc).__name__,
                       "allowed_domains": list(allowed_domains),
                       "provenance": runtime_provenance(),
                       "plan_generation": getattr(policy, "plan_generation", None),
                       "model_call_records": getattr(policy, "call_records", []),
                       "total_tokens": getattr(policy, "tokens", None),
                       "token_usage_complete": getattr(policy, "usage_complete", False)}
            if runtime:
                failure["runtime_config"] = runtime.model_dump()
            if plan is not None:
                failure["plan"] = plan.model_dump()
            (directory / "failure.json").write_text(
                json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Failure record: {directory / 'failure.json'}")
            generation = failure.get("plan_generation") or {}
            if generation.get("status") == "failed":
                for attempt in generation.get("attempts", []):
                    reason = attempt.get("reason", "unknown")
                    print(f"Planning attempt {attempt['attempt']}: {reason}")
        parser.exit(1, f"Research failed ({type(exc).__name__}). Check configuration and inputs.\n")
    print(f"Report: {directory / 'report.md'}")
    print(json.dumps(result["metrics"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
