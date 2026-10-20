"""Offline regressions for starvation, missed criteria and repeated model input."""

import json

from adra.coverage import Assessment
from adra.graph import run
from adra.planning import Plan, PlanningPolicy
from adra.policy import DemoPolicy, ModelPolicy
from adra.state import Budget
from adra.tools import SnapshotTools


class MissingAssessor:
    def assess_coverage(self, task, evidence):
        return Assessment.model_validate({"judgments": [
            {"criterion_id": c.id, "status": "missing", "citations": [],
             "reason": "Synthetic evidence is insufficient."} for c in task.criteria
        ]})


def test_two_tasks_get_reads_with_four_calls_despite_persistent_gaps():
    plan = Plan.model_validate({"query": "compare", "tasks": [
        {"id": t, "description": t, "queries": [t],
         "criteria": [{"id": "rule", "description": "required behavior"}]}
        for t in ("a", "b")
    ]})
    snapshot = {"searches": {
        t: [{"url": f"https://example.org/{t}/{i}", "content": "summary"}
            for i in range(5)] for t in ("a", "b")
    }, "pages": {f"https://example.org/{t}/{i}": "Incomplete evidence."
                 for t in ("a", "b") for i in range(5)}}
    result = run(plan.query, PlanningPolicy(plan, DemoPolicy(), "adaptive", "semantic",
                                           MissingAssessor()),
                 SnapshotTools(snapshot), Budget(tool_calls=4, steps=6))
    reads = [t["action"]["task_id"] for t in result["transitions"]
             if t["action"]["kind"] == "READ"]
    assert set(reads) == {"a", "b"}
    assert result["tool_calls"] == 4
    assert result["metrics"]["semantic_coverage"] == 0
    assert result["stop_reason"] == "tool_budget"


def test_criterion_terms_recover_rule_from_late_document():
    rule = "The default reducer ignores the left argument and replaces the state value with the right argument."
    text = ("State schema Annotated reducer update checkpointer thread_id.\n\n" * 180
            + rule + "\n\n" + "Unrelated background.\n\n" * 100)
    plan = Plan.model_validate({"query": "状态管理", "tasks": [{
        "id": "state", "description": "状态管理", "queries": ["State schema reducer checkpointer"],
        "criteria": [{"id": "reducer", "description": "说明默认覆盖",
                      "retrieval_terms": ["default", "reducer", "ignores", "left", "replaces", "right"]}]
    }]})
    url = "https://example.org/state"
    result = run(plan.query, PlanningPolicy(plan, DemoPolicy(), "adaptive", "semantic",
                                           MissingAssessor()), SnapshotTools({
        "searches": {plan.tasks[0].queries[0]: [{"url": url, "content": "summary"}]},
        "pages": {url: text}}), Budget(tool_calls=2))
    source = result["evidence"][0]
    assert rule in source["content"]
    assert len(source["content"]) <= 6000
    passage = next(p for p in source["passages"] if rule in p["text"])
    assert passage["start"] > 6000
    assert text[passage["start"]:passage["end"]] == passage["text"]


def test_model_payload_does_not_repeat_passages_or_trajectory():
    class Capture(ModelPolicy):
        def _invoke(self, schema, instruction, state, phase="decision"):
            self.payload = state
    policy = Capture(None)
    text = "UNIQUE_EVIDENCE_TEXT"
    evidence = [{"id": "E1", "url": "https://example.org", "title": "Page", "read": True,
                 "content": text, "passages": [{"start": 0, "end": len(text), "text": text}]}]
    state = {"query": "question", "evidence": evidence,
             "transitions": [{"state": {"evidence": evidence}}],
             "planning": {"evidence": evidence}, "raw_documents": {"E1": text}}
    policy.write(state)
    assert json.dumps(policy.payload).count(text) == 1
    assert not {"transitions", "planning", "raw_documents"} & policy.payload.keys()
    task = Plan.model_validate({"query": "question", "tasks": [{
        "id": "a", "description": "a", "queries": ["a"],
        "criteria": [{"id": "rule", "description": "rule"}]}]}).tasks[0]
    policy.assess_coverage(task, evidence)
    assert json.dumps(policy.payload).count(text) == 1
    assert evidence[0]["content"] == text  # Full persisted evidence is untouched.
