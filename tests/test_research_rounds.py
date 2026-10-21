from copy import deepcopy

import pytest

from adra.coverage import research_notes
from adra.planning import Plan, PlanningPolicy
from adra.policy import DemoPolicy, ModelPolicy
from adra.state import Report, Claim


def policy():
    class Assessor:
        def assess_coverage(self, *args):
            raise AssertionError("No model needed to schedule")
    return PlanningPolicy(Plan.model_validate({"query": "q", "tasks": [
        {"id": t, "description": t, "queries": [t],
         "criteria": [{"id": "c", "description": "persistence"}]}
        for t in ("a", "b")]}), DemoPolicy(), "adaptive", "semantic", Assessor())


def last_search_state():
    return {
        "evidence": [
            {"id": t, "title": t, "url": f"https://example.org/{t}",
             "content": "persistence", "read": False, "task_ids": [t]} for t in ("a", "b")],
        "remaining_tool_budget": 1, "remaining_steps": 1,
        "attempted_actions": [{"kind": "SEARCH", "task_id": "a", "argument": "a"}],
        "transitions": [{"action": {"kind": "SEARCH", "task_id": "a", "argument": "a"},
                         "observation": {"source_ids": ["a"], "added_ids": ["a"]}}],
    }


def test_search_read_round_completes_before_task_rotation():
    state = last_search_state()
    selected = policy().describe(state)["selected"]
    assert selected["action"]["task_id"] == "a"  # b has fewer attempts
    assert selected["selection_reason"] == "research_round_read"
    assert selected["action"]["kind"] == "READ"


@pytest.mark.parametrize("observation", [
    {"source_ids": []}, {"error": "TimeoutError", "source_ids": ["a"]},
])
def test_empty_or_failed_search_releases_control(observation):
    state = last_search_state()
    state["transitions"][0]["observation"] = observation
    assert policy().decide(state).task_id == "b"


def test_read_failure_does_not_pin_task_or_retry_same_url():
    state = last_search_state()
    action = {"kind": "READ", "task_id": "a", "argument": "https://example.org/a"}
    state["attempted_actions"].append(action)
    state["transitions"].append({"action": action, "observation": {"error": "TimeoutError"}})
    assert policy().decide(state).task_id == "b"


def note_state():
    return {"query": "q", "report_evidence_mode": "validated_quotes", "evidence": [
        {"id": "E1", "url": "https://example.org/a", "title": "read",
         "read": True, "raw_sha256": "hash", "content": "safe claim. unrelated signature.",
         "passages": [{"start": 0, "end": 32, "text": "safe claim. unrelated signature."}]},
        {"id": "E2", "url": "https://example.org/b", "title": "search",
         "read": False, "content": "unverified snippet"}],
        "coverage": {"a": {"validation": "quotes_validated", "judgments": [{
            "criterion_id": "c", "description": "test", "status": "partial",
            "reason": "Only part established.", "citations": [{
                "evidence_id": "E1", "quote": "safe claim.", "start": 0, "end": 11,
                "raw_sha256": "hash"}]}]}}}


def test_notes_only_contain_checked_quotes_and_preserve_gaps():
    state = note_state()
    before = deepcopy(state)
    notes = research_notes(state)
    assert len(notes["evidence"]) == 1
    assert notes["evidence"][0]["passages"][0]["text"] == "safe claim."
    assert notes["acceptance_checks"][0]["status"] == "partial"
    assert state == before
    state["coverage"]["a"]["judgments"][0]["citations"][0]["raw_sha256"] = "stale"
    assert research_notes(state)["evidence"] == []


def test_writer_cannot_cite_snippet_excluded_from_notes():
    class Writer(ModelPolicy):
        def _invoke(self, schema, instruction, state, phase="writing"):
            assert len(state["evidence"]) == 1
            assert "unrelated signature" not in str(state["evidence"])
            return Report(claims=[Claim(text="unsupported", evidence_ids=["E2"])])
    with pytest.raises(ValueError, match="outside validated"):
        Writer(None).write(note_state())
