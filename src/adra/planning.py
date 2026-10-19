"""Deterministic task scheduling; source coverage is a proxy, not factual support."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from adra.state import Action


class SubTask(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    queries: list[str] = Field(min_length=1, max_length=10)
    dependencies: list[str] = Field(default_factory=list)
    target_sources: int = Field(default=1, ge=1, le=10)
    priority: float = Field(default=1, gt=0, le=10)

    @model_validator(mode="after")
    def clean_queries(self):
        self.queries = [q.strip() for q in self.queries]
        if any(not q for q in self.queries) or len(set(self.queries)) != len(self.queries):
            raise ValueError("Task queries must be nonempty and unique")
        return self


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1)
    tasks: list[SubTask] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_graph(self):
        ids = {t.id for t in self.tasks}
        if len(ids) != len(self.tasks):
            raise ValueError("Task IDs must be unique")
        queries = [q for task in self.tasks for q in task.queries]
        if len(queries) != len(set(queries)):
            raise ValueError("Queries must be unique across tasks; use task-specific queries")
        for task in self.tasks:
            if not set(task.dependencies) <= ids:
                raise ValueError("Unknown task dependency")
        visited = set()
        pending = list(self.tasks)
        while pending:
            ready = [t for t in pending if set(t.dependencies) <= visited]
            if not ready:
                raise ValueError("Task dependencies contain a cycle")
            visited.update(t.id for t in ready)
            pending = [t for t in pending if t not in ready]
        return self


class PlanningPolicy:
    """Recompute task state and candidates from evidence after each action.

    Static chooses the first eligible task. Adaptive ranks eligible tasks by
    weighted coverage gain per estimated tool call. Neither alters the task DAG.
    """

    def __init__(self, plan: Plan, writer, strategy: Literal["static", "adaptive"]):
        if strategy not in ("static", "adaptive"):
            raise ValueError("Unknown planning strategy")
        self.plan = plan.model_copy(deep=True)
        self.writer = writer
        self.strategy = strategy

    @property
    def tokens(self):
        return getattr(self.writer, "tokens", None)

    @property
    def usage_complete(self):
        return getattr(self.writer, "usage_complete", False)

    @property
    def model_calls(self):
        return getattr(self.writer, "model_calls", 0)

    @property
    def call_records(self):
        return getattr(self.writer, "call_records", [])

    @property
    def plan_generation(self):
        return getattr(self.writer, "plan_generation", None)

    def describe(self, state: dict) -> dict:
        attempted = {(a["kind"], a["argument"]) for a in state.get("attempted_actions", [])}
        summaries = []
        candidates = []
        completed = set()
        for task in self.plan.tasks:
            sources = [e for e in state["evidence"] if task.id in e.get("task_ids", [])]
            read = sum(e["read"] for e in sources)
            coverage = min(1.0, read / task.target_sources)
            if coverage == 1:
                completed.add(task.id)
            summaries.append({"task_id": task.id, "description": task.description,
                              "source_coverage_proxy": coverage, "read_sources": read,
                              "target_sources": task.target_sources})

        for task, summary in zip(self.plan.tasks, summaries):
            if task.id in completed:
                summary["status"] = "covered"
                continue
            if not set(task.dependencies) <= completed:
                summary["status"] = "blocked"
                continue
            sources = [e for e in state["evidence"] if task.id in e.get("task_ids", [])]
            unread = next((e for e in sources if not e["read"]
                           and ("READ", e["url"]) not in attempted), None)
            query = next((q for q in task.queries if ("SEARCH", q) not in attempted), None)
            if unread:
                action = Action(kind="READ", argument=unread["url"], task_id=task.id)
                estimated_cost = 1
            elif query:
                action = Action(kind="SEARCH", argument=query, task_id=task.id)
                # Search + at least one page read to increase the coverage proxy.
                estimated_cost = 2
            else:
                summary["status"] = "exhausted"
                continue
            summary["status"] = "ready"
            gain = min(1 / task.target_sources, 1 - summary["source_coverage_proxy"])
            score = task.priority * gain / estimated_cost
            candidates.append({"action": action.model_dump(), "score": score,
                               "estimated_cost": estimated_cost, "expected_proxy_gain": gain})

        affordable = [c for c in candidates
                      if c["estimated_cost"] <= state["remaining_tool_budget"]
                      and c["estimated_cost"] <= state["remaining_steps"]]
        # Stable ties preserve plan order; static uses the same eligibility rules.
        chosen = (max(affordable, key=lambda c: c["score"])
                  if self.strategy == "adaptive" and affordable
                  else affordable[0] if affordable else None)
        return {"strategy": self.strategy, "tasks": summaries, "candidates": candidates,
                "selected": chosen,
                "stop_reason": ("" if chosen else "plan_covered" if len(completed)
                                == len(self.plan.tasks) else "no_feasible_plan_action")}

    def decide(self, state: dict) -> Action:
        selected = self.describe(state)["selected"]
        return Action.model_validate(selected["action"]) if selected else Action(kind="STOP")

    def write(self, state: dict):
        report = self.writer.write(state)
        pending = [t["task_id"] for t in self.describe(state)["tasks"] if t["status"] != "covered"]
        if pending:
            report.limitations.append("未达到来源阅读目标的子任务：" + ", ".join(pending))
        report.limitations.append("来源覆盖仅按已阅读 URL 数量计算，尚未验证相关性或事实支持。")
        return report
