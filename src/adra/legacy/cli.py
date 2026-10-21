import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from adra.legacy.graph import run
from adra.policy import DemoPolicy
from adra.configuration import RuntimeConfig
from adra.legacy.planning import Plan, PlanningPolicy
from adra.state import Budget
from adra.tools import SnapshotTools, TavilyTools
from adra.domains import DomainPolicy
from adra.provenance import runtime_provenance
from adra.coverage import ReplayAssessor


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
    if result.get("coverage"):
        (directory / "coverage.json").write_text(
            json.dumps(result["coverage"], ensure_ascii=False, indent=2), encoding="utf-8")
    if "research_notes" in result:
        (directory / "research_notes.json").write_text(
            json.dumps(result["research_notes"], ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Budgeted research baseline")
    parser.add_argument("query", nargs="?")
    parser.add_argument("--snapshot", type=Path, help="Offline replay JSON (uses demo policy)")
    parser.add_argument("--max-tools", type=int, default=6)
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument("--max-model-calls", type=int, default=32)
    parser.add_argument("--output", type=Path, default=Path("outputs"))
    parser.add_argument("--allowed-domain", action="append", default=[],
                        help="Allowed exact source hostname; repeat for multiple hosts")
    parser.add_argument("--strategy", choices=["baseline", "static", "adaptive"], default="baseline")
    parser.add_argument("--coverage-mode", choices=["sources", "semantic"], default="sources")
    parser.add_argument("--coverage-replay", type=Path,
                        help="Offline assessment map keyed by exact input hash")
    plans = parser.add_mutually_exclusive_group()
    plans.add_argument("--plan", type=Path, help="Validated task DAG in JSON")
    plans.add_argument("--auto-plan", action="store_true", help="Generate a task DAG online")
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
        if args.coverage_mode == "semantic" and args.strategy == "baseline":
            parser.error("Semantic coverage requires static/adaptive planning")
        if args.coverage_replay and (not args.snapshot or args.coverage_mode != "semantic"):
            parser.error("--coverage-replay requires snapshot mode and semantic coverage")
        if args.snapshot and args.coverage_mode == "semantic" and not args.coverage_replay:
            parser.error("Offline semantic coverage requires --coverage-replay")
        if (args.strategy == "baseline") != (not (args.plan or args.auto_plan)):
            parser.error("static/adaptive require --plan or --auto-plan; baseline accepts neither")
        if args.auto_plan and (args.snapshot or min(budget.tool_calls, budget.steps) < 2):
            parser.error("--auto-plan requires online mode and at least two tools/steps")
        plan = Plan.model_validate_json(args.plan.read_text(encoding="utf-8")) if args.plan else None
        if args.snapshot:
            snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
            query = args.query or snapshot["query"]
            policy, tools = DemoPolicy(), SnapshotTools(snapshot)
            mode = "synthetic_replay"
        else:
            query = args.query
            if not query:
                parser.error("Provide a query or --snapshot")
            model_name = os.environ.get("ADRA_MODEL")
            key = os.environ.get("TAVILY_API_KEY")
            if not model_name or not key:
                parser.error("Live mode requires ADRA_MODEL and TAVILY_API_KEY; see README")
            runtime = RuntimeConfig.from_env(args.max_model_calls)
            policy, tools = runtime.create_policy(), TavilyTools(key, allowed_domains)
            mode = "live"
        if not query.strip():
            parser.error("Research query must not be empty")
        if args.auto_plan:
            plan = policy.generate_plan(query, budget, semantic_coverage=args.coverage_mode == "semantic")
        if plan:
            if query.strip() != plan.query.strip():
                parser.error("Plan query must match the research query")
            assessor = (ReplayAssessor(json.loads(args.coverage_replay.read_text(encoding="utf-8")))
                        if args.coverage_replay else None)
            policy = PlanningPolicy(plan, policy, args.strategy, args.coverage_mode, assessor)
        result = run(query, policy, tools, budget, task_id=run_id, allowed_domains=allowed_domains)
        result["mode"] = mode
        result["strategy"] = args.strategy
        result["model"] = os.environ.get("ADRA_MODEL") if mode == "live" else None
        result["plan_source"] = "generated" if args.auto_plan else "file" if args.plan else None
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
