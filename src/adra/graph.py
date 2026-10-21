import time
from copy import deepcopy

from langgraph.graph import END, START, StateGraph

from adra.policy import Policy
from adra.state import Action, AgentState, Budget, Evidence
from adra.tools import ResearchTools, canonical_url
from adra.domains import DomainPolicy, SourceDomainError
from adra.provenance import runtime_provenance
from adra.passages import CONTENT_LIMIT, select_passages
from adra.coverage import refresh_coverage, research_notes


def view(state: dict, budget: Budget) -> dict:
    return {
        "query": state["query"], "evidence": state["evidence"],
        "recent_actions": [t["action"] for t in state["transitions"][-8:]],
        "recent_observations": [t["observation"] for t in state["transitions"][-3:]],
        "remaining_tool_budget": budget.tool_calls - state["tool_calls"],
        "remaining_steps": budget.steps - state["steps"],
        "attempted_actions": [t["action"] for t in state["transitions"]],
        "planning": state.get("planning", {}),
        "coverage": state.get("coverage", {}),
        "allowed_domains": state.get("allowed_domains", []),
        "transitions": state["transitions"][-1:],
    }


def build_graph(policy: Policy, tools: ResearchTools, budget: Budget, allowed_domains=None):
    domains = DomainPolicy(allowed_domains if allowed_domains is not None
                           else getattr(tools, "allowed_domains", ()),
                           getattr(getattr(policy, "plan", None), "allowed_url_prefixes", ()))
    # Apply the same scope to extraction response URLs (including disclosed redirects).
    if hasattr(tools, "domain_policy"):
        tools.domain_policy = domains

    def select(item, raw, query):
        plan = getattr(policy, "plan", None)
        focuses = [{"task_id": task.id, "query": " ".join([task.description, *task.queries,
                                                           *(c.description for c in task.criteria)])}
                   for task in plan.tasks if task.id in item["task_ids"]] if plan else []
        if plan and getattr(policy, "coverage_mode", "sources") == "semantic":
            criterion_focuses = [
                {"task_id": task.id, "criterion_id": c.id,
                 "query": " ".join([c.description, *c.retrieval_terms])}
                for task in plan.tasks if task.id in item["task_ids"] for c in task.criteria
            ]
            focuses = criterion_focuses + focuses
        if not focuses:
            focuses = [{"task_id": None, "query": query}]
        item.update(select_passages(raw, focuses))

    def decide(state):
        coverage = state.get("coverage", {})
        if (getattr(policy, "coverage_mode", "sources") == "semantic"
                and not getattr(policy, "budget_exhausted", False)):
            coverage = refresh_coverage(policy.plan, policy.assessor, state)
        state = {**state, "coverage": coverage}
        reason = ""
        if state["steps"] >= budget.steps:
            reason = "step_budget"
        elif state["tool_calls"] >= budget.tool_calls:
            reason = "tool_budget"
        elif getattr(policy, "budget_exhausted", False):
            reason = "model_budget"
        context = view(state, budget)
        planning = policy.describe(context) if hasattr(policy, "describe") else {}
        action = Action(kind="STOP") if reason else policy.decide(context)
        return {"action": action.model_dump(),
                "coverage": coverage,
                "planning": planning,
                "stop_reason": reason or ((planning.get("stop_reason") or "policy_stop")
                                          if action.kind == "STOP" else "")}

    def execute(state):
        action = Action.model_validate(state["action"])
        before = view(state, budget)
        # Remove nested transitions to keep each trajectory entry bounded.
        before.pop("transitions")
        evidence = deepcopy(state["evidence"])
        raw_documents = deepcopy(state.get("raw_documents", {}))
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
                source_ids = []
                rejected = 0
                for result in results[:5]:
                    if not domains.allows(result.get("url", "")):
                        rejected += 1
                        continue
                    url = canonical_url(result["url"])
                    content = result.get("content", "")
                    if url in known:
                        item = next(e for e in evidence if e["url"] == url)
                        source_ids.append(item["id"])
                        if action.task_id and action.task_id not in item["task_ids"]:
                            item["task_ids"].append(action.task_id)
                            select(item, raw_documents[item["id"]][item["raw_kind"]], state["query"])
                        continue
                    if not content.strip():
                        continue
                    item = Evidence(id=f"E{len(evidence) + 1}", url=url,
                                    title=result.get("title", url), content="",
                                    task_ids=[action.task_id] if action.task_id else [])
                    data = item.model_dump()
                    select(data, content, state["query"])
                    raw_documents[item.id] = {"search": content}
                    evidence.append(data)
                    added.append(item.id)
                    source_ids.append(item.id)
                    known.add(url)
                observation = {"added_ids": added, "new_evidence": len(added),
                               "source_ids": list(dict.fromkeys(source_ids)),
                               "rejected_sources": rejected}
            elif action.kind == "READ":
                url = canonical_url(action.argument)
                domains.require(url)
                item = next((e for e in evidence if e["url"] == url), None)
                if item is None:
                    raise ValueError("READ target must come from collected search results")
                calls = 1
                content = tools.read(url)
                if not content.strip():
                    raise ValueError("Empty page content")
                select(item, content, state["query"])
                raw_documents.setdefault(item["id"], {})["read"] = content
                item.update(read=True, raw_kind="read")
                observation = {"read_id": item["id"], "new_evidence": 0,
                               "original_length": item["original_length"],
                               "selected_length": item["selected_length"],
                               "truncated": item["truncated"]}
            else:
                observation = {"stop_reason": state["stop_reason"], "new_evidence": 0}
        except Exception as exc:
            # Provider errors can include credentials or request URLs. Log only their type.
            observation = {"error": type(exc).__name__, "new_evidence": 0}
            if isinstance(exc, SourceDomainError):
                observation["rejected_sources"] = 1
        transition = {
            "task_id": state["task_id"], "state": before, "action": state["action"],
            "observation": observation, "reward": None, "token_cost": None,
            "tool_cost": calls, "latency_seconds": time.monotonic() - start,
            "done": action.kind == "STOP",
        }
        return {"evidence": evidence, "tool_calls": state["tool_calls"] + calls,
                "raw_documents": raw_documents,
                "steps": state["steps"] + (action.kind != "STOP"),
                "transitions": state["transitions"] + [transition]}

    def write(state):
        for item in state["evidence"]:
            domains.require(item["url"])
        report = policy.write(view(state, budget))
        known = {e["id"]: e for e in state["evidence"]}
        for claim in report.claims:
            if not claim.evidence_ids or not set(claim.evidence_ids) <= known.keys():
                raise ValueError("Report contains missing or unknown evidence references")
            for evidence_id in claim.evidence_ids:
                domains.require(known[evidence_id]["url"])
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
        budget: Budget | None = None, task_id: str = "research", allowed_domains=None) -> dict:
    if not query.strip():
        raise ValueError("Research query must not be empty")
    budget = budget or Budget()
    domains = DomainPolicy(allowed_domains if allowed_domains is not None
                           else getattr(tools, "allowed_domains", ()))
    initial = AgentState(task_id=task_id, query=query.strip(), evidence=[], action={},
                         tool_calls=0, steps=0, transitions=[], stop_reason="", report={}, planning={},
                         allowed_domains=list(domains.domains), raw_documents={}, coverage={})
    started = time.monotonic()
    result = build_graph(policy, tools, budget, domains.domains).invoke(
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
        "truncated_sources": sum(e.get("truncated", False) for e in result["evidence"]),
        "rejected_sources": sum(t["observation"].get("rejected_sources", 0)
                                for t in result["transitions"]),
    }
    result["config"] = budget.model_dump()
    result["config"]["allowed_domains"] = list(domains.domains)
    result["config"]["evidence_char_limit"] = CONTENT_LIMIT
    result["config"]["coverage_mode"] = getattr(policy, "coverage_mode", "sources")
    result["provenance"] = runtime_provenance()
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
        if getattr(policy, "coverage_mode", "sources") == "semantic":
            result["research_notes"] = research_notes(result)
            judgments = [j for record in result["coverage"].values() for j in record["judgments"]]
            result["metrics"]["semantic_coverage"] = sum(j["status"] == "supported"
                                                        for j in judgments) / len(judgments)
            result["metrics"]["coverage_assessment_failures"] = sum(
                r["validation"] == "assessment_failed" for r in result["coverage"].values())
    return result
