import time
from copy import deepcopy

from langgraph.graph import END, START, StateGraph

from adra.policy import Policy
from adra.state import Action, AgentState, Budget, Evidence
from adra.tools import ResearchTools, canonical_url


def view(state: dict, budget: Budget) -> dict:
    return {
        "query": state["query"], "evidence": state["evidence"],
        "recent_actions": [t["action"] for t in state["transitions"][-8:]],
        "recent_observations": [t["observation"] for t in state["transitions"][-3:]],
        "remaining_tool_budget": budget.tool_calls - state["tool_calls"],
        "remaining_steps": budget.steps - state["steps"],
        "attempted_actions": [t["action"] for t in state["transitions"]],
        "planning": state.get("planning", {}),
        "transitions": state["transitions"][-1:],
    }


def build_graph(policy: Policy, tools: ResearchTools, budget: Budget):
    def decide(state):
        reason = ""
        if state["steps"] >= budget.steps:
            reason = "step_budget"
        elif state["tool_calls"] >= budget.tool_calls:
            reason = "tool_budget"
        context = view(state, budget)
        planning = policy.describe(context) if hasattr(policy, "describe") else {}
        action = Action(kind="STOP") if reason else policy.decide(context)
        return {"action": action.model_dump(),
                "planning": planning,
                "stop_reason": reason or ((planning.get("stop_reason") or "policy_stop")
                                          if action.kind == "STOP" else "")}

    def execute(state):
        action = Action.model_validate(state["action"])
        before = view(state, budget)
        # Remove nested transitions to keep each trajectory entry bounded.
        before.pop("transitions")
        evidence = deepcopy(state["evidence"])
        observation = {}
        calls = 0
        start = time.monotonic()
        duplicate = any(t["action"]["kind"] == action.kind
                        and t["action"]["argument"] == action.argument
                        for t in state["transitions"])
        try:
            if duplicate:
                observation = {"error": "duplicate_action", "new_evidence": 0}
            elif action.kind == "SEARCH":
                calls = 1
                results = tools.search(action.argument)
                known = {e["url"] for e in evidence}
                added = []
                for result in results[:5]:
                    url = canonical_url(result["url"])
                    content = result.get("content", "").strip()
                    if url in known:
                        item = next(e for e in evidence if e["url"] == url)
                        if action.task_id and action.task_id not in item["task_ids"]:
                            item["task_ids"].append(action.task_id)
                        continue
                    if not content:
                        continue
                    item = Evidence(id=f"E{len(evidence) + 1}", url=url,
                                    title=result.get("title", url), content=content[:6000],
                                    task_ids=[action.task_id] if action.task_id else [])
                    evidence.append(item.model_dump())
                    added.append(item.id)
                    known.add(url)
                observation = {"added_ids": added, "new_evidence": len(added)}
            elif action.kind == "READ":
                url = canonical_url(action.argument)
                item = next((e for e in evidence if e["url"] == url), None)
                if item is None:
                    raise ValueError("READ target must come from collected search results")
                calls = 1
                content = tools.read(url).strip()
                if not content:
                    raise ValueError("Empty page content")
                item.update(content=content[:6000], read=True)
                observation = {"read_id": item["id"], "new_evidence": 0}
            else:
                observation = {"stop_reason": state["stop_reason"], "new_evidence": 0}
        except Exception as exc:
            # Provider errors can include credentials or request URLs. Log only their type.
            observation = {"error": type(exc).__name__, "new_evidence": 0}
        transition = {
            "task_id": state["task_id"], "state": before, "action": state["action"],
            "observation": observation, "reward": None, "token_cost": None,
            "tool_cost": calls, "latency_seconds": time.monotonic() - start,
            "done": action.kind == "STOP",
        }
        return {"evidence": evidence, "tool_calls": state["tool_calls"] + calls,
                "steps": state["steps"] + (action.kind != "STOP"),
                "transitions": state["transitions"] + [transition]}

    def write(state):
        report = policy.write(view(state, budget))
        known = {e["id"] for e in state["evidence"]}
        for claim in report.claims:
            if not claim.evidence_ids or not set(claim.evidence_ids) <= known:
                raise ValueError("Report contains missing or unknown evidence references")
        return {"report": report.model_dump()}

    graph = StateGraph(AgentState)
    graph.add_node("decide", decide)
    graph.add_node("execute", execute)
    graph.add_node("write", write)
    graph.add_edge(START, "decide")
    graph.add_edge("decide", "execute")
    graph.add_conditional_edges("execute",
        lambda s: "write" if s["action"]["kind"] == "STOP" else "decide")
    graph.add_edge("write", END)
    return graph.compile()


def run(query: str, policy: Policy, tools: ResearchTools,
        budget: Budget | None = None, task_id: str = "research") -> dict:
    if not query.strip():
        raise ValueError("Research query must not be empty")
    budget = budget or Budget()
    initial = AgentState(task_id=task_id, query=query.strip(), evidence=[], action={},
                         tool_calls=0, steps=0, transitions=[], stop_reason="", report={}, planning={})
    started = time.monotonic()
    result = build_graph(policy, tools, budget).invoke(
        initial, {"recursion_limit": budget.steps * 2 + 10})
    result["metrics"] = {
        "tool_calls": result["tool_calls"], "steps": result["steps"],
        "source_count": len(result["evidence"]),
        "read_source_count": sum(e["read"] for e in result["evidence"]),
        "tool_errors": sum("error" in t["observation"] for t in result["transitions"]),
        "latency_seconds": time.monotonic() - started,
        "total_tokens": getattr(policy, "tokens", None),
        "token_usage_complete": getattr(policy, "usage_complete", False),
        "model_calls": getattr(policy, "model_calls", 0),
        "citation_reference_validity": 1.0 if result["report"]["claims"] else None,
        "citation_support": None, "task_quality": None,
    }
    result["config"] = budget.model_dump()
    result["model_call_records"] = list(getattr(policy, "call_records", []))
    result["plan_generation"] = getattr(policy, "plan_generation", None)
    if result["plan_generation"]:
        result["metrics"]["planning_latency_seconds"] = result["plan_generation"]["latency_seconds"]
        result["metrics"]["latency_seconds"] += result["metrics"]["planning_latency_seconds"]
    if hasattr(policy, "plan"):
        result["plan"] = policy.plan.model_dump()
        tasks = result["planning"]["tasks"]
        result["metrics"]["source_coverage_proxy"] = sum(
            t["source_coverage_proxy"] for t in tasks) / len(tasks)
        result["metrics"]["covered_tasks"] = sum(t["status"] == "covered" for t in tasks)
    return result
