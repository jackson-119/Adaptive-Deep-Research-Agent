"""Paired synthetic snapshot experiments. Not a report-quality benchmark."""

import argparse
import hashlib
import json
from importlib.metadata import version
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from adra.cli import save
from adra.graph import run
from adra.planning import Plan, PlanningPolicy
from adra.policy import DemoPolicy
from adra.state import Budget
from adra.tools import SnapshotTools


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    plan: Plan
    snapshot: dict
    budget: Budget

    @model_validator(mode="after")
    def validate_snapshot(self):
        if self.snapshot.get("query") != self.plan.query:
            raise ValueError("Snapshot and plan query mismatch")
        required = {q for t in self.plan.tasks for q in t.queries}
        if not required <= self.snapshot.get("searches", {}).keys():
            raise ValueError("Snapshot must include every planned query (empty results allowed)")
        if "pages" not in self.snapshot:
            raise ValueError("Snapshot must define pages")
        return self


def evaluate(suite: Path, output: Path) -> dict:
    payload = suite.read_bytes()
    cases = [Case.model_validate(c) for c in json.loads(payload)]
    if not cases or len({c.id for c in cases}) != len(cases):
        raise ValueError("Suite must contain unique, nonempty cases")
    output.mkdir(parents=True, exist_ok=False)
    (output / "suite.json").write_bytes(payload)
    rows = []
    for case in cases:
        for strategy in ("static", "adaptive"):
            policy = PlanningPolicy(case.plan, DemoPolicy(), strategy)
            result = run(case.plan.query, policy, SnapshotTools(case.snapshot),
                         case.budget, task_id=case.id)
            result.update(mode="synthetic_replay", strategy=strategy, model=None)
            save(result, output / case.id / strategy)
            rows.append({"case_id": case.id, "strategy": strategy,
                         "stop_reason": result["stop_reason"], **result["metrics"]})
    summary = {"mode": "synthetic_replay", "suite_sha256": hashlib.sha256(payload).hexdigest(),
               "versions": {p: version(p) for p in ("langgraph", "pydantic")},
               "limitations": "Synthetic control-flow checks only; source coverage is not quality.",
               "runs": rows}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                         encoding="utf-8")
    lines = ["# 合成快照规划对照", "", "仅验证策略行为，不代表真实研究质量或算法收益。", "",
             "| Case | Strategy | Tool calls | Read sources | Coverage proxy | Stop |",
             "| --- | --- | ---: | ---: | ---: | --- |"]
    for r in rows:
        lines.append(f"| {r['case_id']} | {r['strategy']} | {r['tool_calls']} | "
                     f"{r['read_source_count']} | {r['source_coverage_proxy']:.3f} | "
                     f"{r['stop_reason']} |")
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New output directory")
    args = parser.parse_args()
    evaluate(args.suite, args.output)
    print(args.output / "summary.md")


if __name__ == "__main__":
    main()
