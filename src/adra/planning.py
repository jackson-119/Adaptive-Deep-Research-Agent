"""Deterministic task scheduling; source coverage is a proxy, not factual support."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from adra.state import Action
from adra.coverage import Criterion
from adra.domains import DomainPolicy
from adra.passages import terms


class SubTask(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    queries: list[str] = Field(min_length=1, max_length=10)
    dependencies: list[str] = Field(default_factory=list)
    target_sources: int = Field(default=1, ge=1, le=10)
    priority: float = Field(default=1, gt=0, le=10)
    criteria: list[Criterion] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def clean_queries(self):
        if len({c.id for c in self.criteria}) != len(self.criteria):
            raise ValueError("Criterion IDs must be unique within each task")
        self.queries = [q.strip() for q in self.queries]
        if any(not q for q in self.queries) or len(set(self.queries)) != len(self.queries):
            raise ValueError("Task queries must be nonempty and unique")
        return self


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1)
    tasks: list[SubTask] = Field(min_length=1, max_length=20)
    allowed_url_prefixes: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def validate_graph(self):
        DomainPolicy(url_prefixes=self.allowed_url_prefixes)
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

    def __init__(self, plan: Plan, writer, strategy: Literal["static", "adaptive"],
                 coverage_mode="sources", assessor=None):
        if strategy not in ("static", "adaptive"):
            raise ValueError("Unknown planning strategy")
        self.plan = plan.model_copy(deep=True)
        self.writer = writer
        self.strategy = strategy
        if coverage_mode not in ("sources", "semantic"):
            raise ValueError("Unknown coverage mode")
        self.coverage_mode = coverage_mode
        self.assessor = assessor or writer
        if coverage_mode == "semantic" and (not all(t.criteria for t in plan.tasks)
                                             or not hasattr(self.assessor, "assess_coverage")):
            raise ValueError("Semantic coverage needs criteria for every task and an assessor")

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
    def budget_exhausted(self):
        return getattr(self.writer, "budget_exhausted", False)

    @property
    def call_records(self):
        return getattr(self.writer, "call_records", [])

    @property
    def plan_generation(self):
        return getattr(self.writer, "plan_generation", None)

    def describe(self, state: dict) -> dict:
        attempted = {(a["kind"], a["argument"]) for a in state.get("attempted_actions", [])}
        # Adapt the bounded research-unit pattern: consume one successful search
        # result before returning control to task scheduling. No hidden tool calls.
        last = next(iter(state.get("transitions", [])[-1:]), {})
        last_action = last.get("action", {})
        observation = last.get("observation", {})
        followup_task = (last_action.get("task_id")
                         if self.strategy == "adaptive" and self.coverage_mode == "semantic"
                         and last_action.get("kind") == "SEARCH" and "error" not in observation
                         else None)
        followup_ids = set(observation.get("source_ids", observation.get("added_ids", [])))
        summaries = []
        candidates = []
        completed = set()
        for task in self.plan.tasks:
            sources = [e for e in state["evidence"] if task.id in e.get("task_ids", [])]
            read = sum(e["read"] for e in sources)
            coverage = min(1.0, read / task.target_sources)
            judgments = state.get("coverage", {}).get(task.id, {}).get("judgments", [])
            semantic = (sum(j["status"] == "supported" for j in judgments) / len(task.criteria)
                        if task.criteria else 0)
            effective = semantic if self.coverage_mode == "semantic" else coverage
            if effective == 1:
                completed.add(task.id)
            summaries.append({"task_id": task.id, "description": task.description,
                              "source_coverage_proxy": coverage, "read_sources": read,
                              "semantic_coverage": semantic if self.coverage_mode == "semantic" else None,
                              "judgments": judgments,
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
            selection_reason = "source_order"
            if self.coverage_mode == "semantic":
                statuses = {j["criterion_id"]: j["status"] for j in summary["judgments"]}
                gaps = [c for c in task.criteria if statuses.get(c.id) != "supported"]
                vocab = [terms(" ".join([c.description, *c.retrieval_terms])) for c in gaps]

                def relevance(text):
                    words = terms(text)
                    return sum(len(words & v) / max(1, len(v)) for v in vocab)

                remaining = [e for e in sources if not e["read"]
                             and ("READ", e["url"]) not in attempted]
                unread = max(remaining, key=lambda e: relevance(
                    e["title"] + " " + e["content"]), default=None)
                queries = [q for q in task.queries if ("SEARCH", q) not in attempted]
                # Preserve the initial query; rank alternatives once evidence exists.
                if summary["read_sources"] and queries:
                    query = max(queries, key=relevance)
                    if (min(state["remaining_tool_budget"], state["remaining_steps"]) >= 2
                            and (unread is None or relevance(query) > relevance(
                                unread["title"] + " " + unread["content"]))):
                        unread = None
                        selection_reason = "gap_query"
                if unread:
                    selection_reason = "gap_source"
                followups = [e for e in remaining if e["id"] in followup_ids]
                if task.id == followup_task and followups:
                    unread = max(followups, key=lambda e: relevance(e["title"] + " " + e["content"]))
                    selection_reason = "research_round_read"
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
            if self.coverage_mode == "semantic":
                gain = min(1 / len(task.criteria), 1 - summary["semantic_coverage"])
            else:
                gain = min(1 / task.target_sources, 1 - summary["source_coverage_proxy"])
            score = task.priority * gain / estimated_cost
            candidates.append({"action": action.model_dump(), "score": score,
                               "selection_reason": selection_reason,
                               "estimated_cost": estimated_cost, "expected_proxy_gain": gain})

        affordable = [c for c in candidates
                      if c["estimated_cost"] <= state["remaining_tool_budget"]
                      and c["estimated_cost"] <= state["remaining_steps"]]
        # Semantic gaps can persist after many reads. Balance attempts across
        # eligible tasks before applying the cost score, rather than starving
        # an unstarted task behind an endless queue of cheaper reads.
        continuation = [c for c in affordable if c["selection_reason"] == "research_round_read"]
        if continuation:
            affordable = continuation
        elif self.strategy == "adaptive" and self.coverage_mode == "semantic" and affordable:
            attempts = state.get("attempted_actions", [])
            counts = {t.id: sum(a.get("task_id") == t.id and a["kind"] != "STOP"
                                for a in attempts) for t in self.plan.tasks}
            least = min(counts[c["action"]["task_id"]] for c in affordable)
            affordable = [c for c in affordable if counts[c["action"]["task_id"]] == least]
        # Stable ties preserve plan order; static uses the same eligibility rules.
        chosen = (max(affordable, key=lambda c: c["score"])
                  if self.strategy == "adaptive" and affordable
                  else affordable[0] if affordable else None)
        return {"strategy": self.strategy, "coverage_mode": self.coverage_mode,
                "tasks": summaries, "candidates": candidates,
                "selected": chosen,
                "scheduling_rule": ("research_round_then_balanced_attempts"
                                    if self.strategy == "adaptive" and self.coverage_mode == "semantic"
                                    else "score" if self.strategy == "adaptive" else "plan_order"),
                "stop_reason": ("" if chosen else "plan_covered" if len(completed)
                                == len(self.plan.tasks) else "no_feasible_plan_action")}

    def decide(self, state: dict) -> Action:
        selected = self.describe(state)["selected"]
        return Action.model_validate(selected["action"]) if selected else Action(kind="STOP")

    def write(self, state: dict):
        report = self.writer.write({**state, "research_scope": {
            "tasks": [t.description for t in self.plan.tasks],
            "allowed_url_prefixes": self.plan.allowed_url_prefixes},
            "report_evidence_mode": "validated_quotes" if self.coverage_mode == "semantic"
            else "selected_passages"})
        pending = [t["task_id"] for t in self.describe(state)["tasks"] if t["status"] != "covered"]
        if pending:
            label = "未满足验收要点的子任务：" if self.coverage_mode == "semantic" else "未达到来源阅读目标的子任务："
            report.limitations.append(label + ", ".join(pending))
        if self.coverage_mode == "semantic":
            for task in self.describe(state)["tasks"]:
                for item in task["judgments"]:
                    if item["status"] != "supported":
                        report.limitations.append(f"{task['task_id']}/{item['criterion_id']}: "
                                                  f"{item['status']} — {item['reason']}")
            report.limitations.append("覆盖判断来自模型或回放标注；原文存在性校验不代表语义判断正确。")
        else:
            report.limitations.append("来源覆盖仅按已阅读 URL 数量计算，尚未验证相关性或事实支持。")
        return report
