"""Auditable research state, not a replacement for final report evaluation."""
from collections import Counter
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from adra.coverage import Citation
from adra.verification import validate_quotes


class ResearchOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    reflection_mode: Literal['off', 'event', 'always'] = 'off'
    max_reflections: int = Field(default=3, ge=0, le=20)
    history_window: int = Field(default=6, ge=1, le=30)
    verify_claims: bool = False


class GoalUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    goal_id: str
    status: Literal['unexplored', 'partial', 'supported', 'conflicted']
    missing_facts: list[str] = Field(default_factory=list, max_length=10)
    citations: list[Citation] = Field(default_factory=list, max_length=8)


class ReflectionResult(BaseModel):
    model_config = ConfigDict(extra='forbid')
    goals: list[GoalUpdate]
    contradictions: list[str] = Field(default_factory=list, max_length=10)
    redundant_searches: list[str] = Field(default_factory=list, max_length=10)
    suggested_queries: list[str] = Field(default_factory=list, max_length=8)
    should_replan: bool


def initial_goals(plan):
    return [{'id': f'g{i + 1}', 'task_id': t.id, 'criterion_id': c.id,
             'description': c.description, 'priority': t.priority, 'status': 'unexplored',
             'missing_facts': [c.description], 'citations': [], 'checked_at_step': None,
             'validation': 'not_assessed'}
            for i, (t, c) in enumerate((t, c) for t in plan.tasks for c in t.criteria)] if plan else []


def update_goals(goals, reflection, evidence, documents, step):
    reflection = ReflectionResult.model_validate(reflection)
    ids = [g.goal_id for g in reflection.goals]
    if len(set(ids)) != len(ids) or set(ids) != {g['id'] for g in goals}:
        raise ValueError('Reflection must preserve exactly the original goal IDs')
    updates = {g.goal_id: g for g in reflection.goals}
    result = []
    for goal in goals:
        update = updates[goal['id']]
        citations = validate_quotes(update.citations, evidence, documents)
        if update.status != 'unexplored' and not citations:
            raise ValueError('Assessed goal states require source quotations')
        if update.status == 'conflicted' and len({(c['evidence_id'], c['start'], c['end']) for c in citations}) < 2:
            raise ValueError('Conflict requires at least two distinct quotations')
        if update.status == 'supported' and update.missing_facts:
            raise ValueError('Supported goal cannot have unresolved missing facts')
        result.append({**goal, 'status': update.status, 'missing_facts': update.missing_facts,
                       'citations': citations, 'checked_at_step': step,
                       'validation': 'quotes_validated_model_judgment'})
    return result


def invalidate_stale_goals(goals, evidence):
    hashes = {e['id']: e['raw_sha256'] for e in evidence}
    result = deepcopy(goals)
    for goal in result:
        if any(hashes.get(c['evidence_id']) != c['raw_sha256'] for c in goal['citations']):
            goal.update(status='unexplored', citations=[], validation='stale_source',
                        missing_facts=[goal['description']])
    return result


def reflection_trigger(state, budget, options, model_calls=0, max_model_calls=None, finishing=False, conflict=False):
    if options.reflection_mode == 'off' or len(state['reflections']) >= options.max_reflections:
        return None
    if state['last_reflection_step'] == state['steps']:
        return None
    if state['tool_calls'] >= budget.tool_calls or state['steps'] >= budget.steps:
        return None
    # One reflection + a fresh decision + writing must all fit.
    if max_model_calls is not None and max_model_calls - model_calls < 3:
        return None
    unresolved = any(g['status'] != 'supported' for g in state['goals'])
    if conflict:
        return 'reported_conflict'
    if finishing and unresolved:
        return 'finish_with_gaps'
    if not state['steps']:
        return None
    if options.reflection_mode == 'always':
        return 'every_step'
    recent = state['transitions'][-2:]
    if (len(recent) == 2 and state['steps'] - state['last_reflection_step'] >= 2
            and all(not t.get('new_evidence', False) for t in recent)):
        return 'two_steps_without_new_evidence'
    if (state['steps'] >= (budget.steps + 1) // 2 and unresolved
            and not any(r['trigger'] == 'half_budget_with_gaps' for r in state['reflections'])):
        return 'half_budget_with_gaps'
    return None


def bounded_history(transitions, window):
    older = transitions[:-window]
    return {
        'history': [{k: t[k] for k in ('action', 'observation', 'tool_cost', 'new_evidence') if k in t}
                    for t in transitions[-window:]],
        'history_summary': {'older_actions': len(older),
                            'action_counts': dict(Counter(t['action']['kind'] for t in older)),
                            'error_counts': dict(Counter(t['observation']['error'] for t in older
                                                         if 'error' in t['observation'])),
                            'external_requests': sum(t['tool_cost'] for t in older)},
    }
