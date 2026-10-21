import math
from copy import deepcopy

import pytest

from adra.coverage import Citation
from adra.evidence_store import add_source, bm25_scores, retrieve, tokenize
from adra.experiment_summary import aggregate
from adra.graph import run
from adra.planning import Plan
from adra.research_control import (ResearchOptions, ReflectionResult, bounded_history,
                                   initial_goals, reflection_trigger, update_goals)
from adra.research_models import ResearchDecision
from adra.state import Budget, Claim, Report
from adra.tools import SnapshotTools
from adra.verification import ClaimVerdict, verify_report

URL = 'https://example.com/docs'
TEXT = 'alpha is enabled by default.'


def plan():
    return Plan(query='Explain alpha', tasks=[{'id': 't1', 'description': 'alpha', 'queries': ['alpha'],
                'criteria': [{'id': 'default', 'description': 'alpha default', 'retrieval_terms': ['alpha']}]}])


def corpus(text=TEXT):
    evidence, documents = [], {}
    add_source(evidence, documents, {'url': URL, 'raw_content': text}, read=True)
    return evidence, documents


def test_bm25_term_frequency_and_length_follow_formula():
    docs = [['alpha', 'alpha', 'beta'], ['alpha', 'beta', 'beta']]
    scores = bm25_scores(docs, ['alpha'])
    idf = math.log(1 + 0.5 / 2.5)
    assert scores == pytest.approx([idf * 2 * 2.2 / (2 + 1.2), idf * 2.2 / (1 + 1.2)])
    assert scores[0] > scores[1]
    assert tokenize('alpha alpha 状态状态').count('alpha') == 2
    assert tokenize('状态状态').count('状态') == 2
    assert bm25_scores([], ['alpha']) == []
    assert bm25_scores([[], ['alpha']], []) == [0, 0]


def test_overlapping_relevant_chunks_merge_without_losing_second_topic():
    raw = 'alpha ' + 'x ' * 700 + 'beta ' + 'y ' * 100
    evidence, documents = corpus(raw)
    notes = retrieve(evidence, documents, ['alpha', 'beta'], char_budget=len(raw), top_k=1)
    assert len(notes[0]['passages']) == 1
    assert notes[0]['content'] == raw
    assert notes[0]['selected_length'] <= len(raw)
    small = retrieve(evidence, documents, ['alpha', 'beta'], char_budget=1200, top_k=1)
    assert sum(e['selected_length'] for e in small) <= 1200


def reflection(status='supported', quote=TEXT):
    return ReflectionResult(goals=[{'goal_id': 'g1', 'status': status,
        'missing_facts': [] if status == 'supported' else ['alpha default'],
        'citations': [Citation(evidence_id='E1', quote=quote)] if status != 'unexplored' else []}],
        should_replan=True, suggested_queries=['alpha default official docs'])


def test_goal_tracker_requires_quotes_and_cannot_change_rubric():
    brief = plan()
    before = brief.model_dump()
    goals = initial_goals(brief)
    evidence, documents = corpus()
    notes = retrieve(evidence, documents, ['alpha'])
    updated = update_goals(goals, reflection(), notes, documents, 2)
    assert updated[0]['status'] == 'supported'
    assert updated[0]['citations'][0]['quote'] == TEXT
    assert brief.model_dump() == before
    assert goals[0]['status'] == 'unexplored'
    with pytest.raises(ValueError):
        update_goals(goals, reflection(quote='made up'), notes, documents, 2)
    invalid = reflection().model_copy(deep=True)
    invalid.goals[0].goal_id = 'replaced'
    with pytest.raises(ValueError):
        update_goals(goals, invalid, notes, documents, 2)
    with pytest.raises(ValueError):
        update_goals(goals, reflection(status='conflicted'), notes, documents, 2)


def test_stale_source_invalidates_previously_supported_goals():
    from adra.research_control import invalidate_stale_goals
    evidence, documents = corpus()
    updated = update_goals(initial_goals(plan()), reflection(), retrieve(evidence, documents, ['alpha']), documents, 1)
    evidence[0]['raw_sha256'] = 'changed'
    invalidated = invalidate_stale_goals(updated, evidence)
    assert invalidated[0]['status'] == 'unexplored'
    assert invalidated[0]['validation'] == 'stale_source'


def test_event_trigger_cooldown_budget_and_finish_gate():
    state = {'steps': 2, 'tool_calls': 2, 'goals': initial_goals(plan()), 'reflections': [],
             'last_reflection_step': -1, 'transitions': [{'new_evidence': False}] * 2}
    options = ResearchOptions(reflection_mode='event')
    assert reflection_trigger(state, Budget(tool_calls=8), options) == 'two_steps_without_new_evidence'
    assert reflection_trigger(state, Budget(tool_calls=8), options, 8, 10) is None
    assert reflection_trigger(state, Budget(tool_calls=2), options) is None
    state['last_reflection_step'] = 2
    assert reflection_trigger(state, Budget(tool_calls=8), options, finishing=True) is None
    state['steps'] = 3
    assert reflection_trigger(state, Budget(tool_calls=8), options, finishing=True) == 'finish_with_gaps'


class Policy:
    def __init__(self, actions, fail_reflection=False):
        self.actions = iter(actions)
        self.fail_reflection = fail_reflection
        self.contexts = []
        self.reflect_count = 0

    def research(self, context):
        self.contexts.append(deepcopy(context))
        return ResearchDecision(**next(self.actions), reason='test')

    def reflect(self, context):
        self.reflect_count += 1
        if self.fail_reflection:
            return reflection(quote='invented')
        return reflection() if context['research_notes'] else reflection('unexplored')

    def write(self, state):
        return Report(claims=[Claim(text=TEXT, evidence_ids=['E1'])])


def test_event_reflection_updates_goals_without_online_report_evaluation():
    backend = SnapshotTools({'searches': {'empty1': [], 'empty2': [],
        'new-query': [{'url': URL, 'raw_content': TEXT}]}, 'pages': {}})
    policy = Policy([{'kind': 'SEARCH', 'argument': 'empty1'}, {'kind': 'SEARCH', 'argument': 'empty2'},
                     {'kind': 'SEARCH', 'argument': 'new-query'}, {'kind': 'FINISH'}, {'kind': 'FINISH'}])
    result = run(plan().query, policy, backend, Budget(tool_calls=6), plan=plan(), evaluate=False,
                 options=ResearchOptions(reflection_mode='event'))
    assert policy.reflect_count == 2
    assert [r['trigger'] for r in result['reflections']] == ['two_steps_without_new_evidence', 'finish_with_gaps']
    assert result['goals'][0]['status'] == 'supported'
    assert result['coverage'] == {}
    assert result['report']['claims']
    assert result['decisions'][-1]['action']['kind'] == 'FINISH'
    assert result['transitions'][0]['decision_index'] == 0


def test_failed_reflection_does_not_loop_or_rewrite_goal():
    backend = SnapshotTools({'searches': {'alpha': [{'url': URL, 'raw_content': TEXT}]}, 'pages': {}})
    policy = Policy([{'kind': 'SEARCH', 'argument': 'alpha'}, {'kind': 'FINISH'}, {'kind': 'FINISH'}], True)
    result = run(plan().query, policy, backend, Budget(tool_calls=6), plan=plan(), evaluate=False,
                 options=ResearchOptions(reflection_mode='event'))
    assert policy.reflect_count == 1
    assert result['goals'][0]['status'] == 'unexplored'
    assert result['metrics']['reflection_failures'] == 1
    assert result['report']['claims']


def test_history_is_bounded_with_full_trajectory_retained():
    transitions = [{'action': {'kind': 'SEARCH', 'argument': str(i)}, 'observation': {},
                    'tool_cost': 1} for i in range(20)]
    ctx = bounded_history(transitions, 3)
    assert len(ctx['history']) == 3
    assert ctx['history_summary']['older_actions'] == 17
    assert ctx['history_summary']['external_requests'] == 17
    assert len(transitions) == 20


def test_external_failures_and_local_rejections_have_distinct_counters():
    backend = SnapshotTools({'searches': {}, 'pages': {}})
    policy = Policy([{'kind': 'SEARCH', 'argument': 'not-in-snapshot'},
                     {'kind': 'READ', 'argument': 'file:///secret'}, {'kind': 'FINISH'}])
    result = run(plan().query, policy, backend, Budget(tool_calls=3), plan=plan(), evaluate=False)
    metrics = result['metrics']
    assert metrics['tool_attempts'] == 2
    assert metrics['tool_requests_sent'] == 1
    assert metrics['tool_successes'] == 0
    assert metrics['tool_rejections'] == 1
    assert result['tool_calls'] == 1


def claim_state():
    evidence, documents = corpus()
    return {'evidence': evidence, 'raw_documents': documents,
            'report': {'claims': [{'text': TEXT, 'evidence_ids': ['E1']}]}}


@pytest.mark.parametrize('verdict', ['supported', 'contradicted', 'insufficient'])
def test_claim_verdicts_validate_quotes_and_publish_separate_rates(verdict):
    class Judge:
        def verify_claim(self, claim, evidence):
            return ClaimVerdict(verdict=verdict, explanation='test',
                                citations=[Citation(evidence_id='E1', quote=TEXT)])
    result = verify_report(claim_state(), Judge())
    assert result['status'] == 'completed'
    assert result[f'{verdict}_claim_rate'] == 1


def test_claim_checker_rejects_unrelated_sources_and_fabricated_quotes():
    class Judge:
        def verify_claim(self, claim, evidence):
            return ClaimVerdict(verdict='supported', explanation='test',
                                citations=[Citation(evidence_id='E2', quote=TEXT)])
    result = verify_report(claim_state(), Judge())
    assert result['status'] == 'incomplete'
    assert result['supported_claim_rate'] is None
    assert result['evaluated_claims'] == 0


def test_claim_budget_failure_is_not_counted_as_insufficient():
    class Judge:
        def verify_claim(self, claim, evidence):
            raise RuntimeError('private-secret')
    result = verify_report(claim_state(), Judge())
    assert result['status'] == 'incomplete'
    assert result['insufficient_claim_rate'] is None
    assert 'private-secret' not in str(result)


def test_ablation_commands_use_same_budget_and_correct_variant():
    from pathlib import Path
    from adra.live_evaluation import build_command
    case = {'plan_data': plan().model_dump(), 'allowed_domains': []}
    for variant, expected in [('baseline', 'off'), ('event', 'event'), ('always', 'always')]:
        cmd = build_command(case, 'semantic', Path('plan'), Path('out'), Budget(tool_calls=8), 32, variant)
        assert cmd[cmd.index('--reflection-mode') + 1] == expected
        assert cmd[cmd.index('--max-tools') + 1] == '8'
        assert cmd[cmd.index('--max-model-calls') + 1] == '32'


def test_statistics_preserve_missing_and_failed_runs():
    records = [{'case': 'q', 'variant': 'event', 'status': 'completed', 'metrics': {'total_tokens': n}}
               for n in [10, 20]] + [{'case': 'q', 'variant': 'event', 'status': 'failed'}]
    result = aggregate(records)['groups'][0]
    assert result['failed_runs'] == 1
    assert result['metrics']['total_tokens']['mean'] == 15
    assert result['metrics']['total_tokens']['missing'] == 1
    assert result['metrics']['semantic_coverage']['mean'] is None
    assert result['metrics']['semantic_coverage']['missing'] == 3
    assert aggregate(records) == aggregate(records)


def test_model_reported_conflict_is_a_bounded_event():
    state = {'steps': 1, 'tool_calls': 1, 'goals': initial_goals(plan()), 'reflections': [],
             'last_reflection_step': -1, 'transitions': [{'new_evidence': True}]}
    options = ResearchOptions(reflection_mode='event')
    assert reflection_trigger(state, Budget(tool_calls=8), options, conflict=True) == 'reported_conflict'
    state['last_reflection_step'] = 1
    assert reflection_trigger(state, Budget(tool_calls=8), options, conflict=True) is None


def test_half_budget_trigger_only_once():
    state = {'steps': 5, 'tool_calls': 1, 'goals': initial_goals(plan()), 'reflections': [],
             'last_reflection_step': -1, 'transitions': [{'new_evidence': True}]}
    options = ResearchOptions(reflection_mode='event')
    assert reflection_trigger(state, Budget(tool_calls=8), options) == 'half_budget_with_gaps'
    state['reflections'] = [{'trigger': 'half_budget_with_gaps'}]
    state['steps'] = 6
    assert reflection_trigger(state, Budget(tool_calls=8), options) is None


def test_reflection_model_cap_preserves_decision_and_writer_calls():
    from types import SimpleNamespace
    from adra.policy import ModelPolicy
    class Model:
        def with_structured_output(self, schema, **kwargs):
            self.schema = schema
            return self
        def invoke(self, messages):
            if self.schema is ResearchDecision:
                value = ResearchDecision(kind='SEARCH', argument='alpha', reason='test')
            else:
                assert self.schema is Report
                value = Report(claims=[Claim(text=TEXT, evidence_ids=['E1'])])
            return {'parsed': value, 'raw': SimpleNamespace(usage_metadata={'total_tokens': 10})}
    policy = ModelPolicy(Model(), max_model_calls=2)
    backend = SnapshotTools({'searches': {'alpha': [{'url': URL, 'raw_content': TEXT}]}, 'pages': {}})
    result = run(plan().query, policy, backend, Budget(tool_calls=8), plan=plan(), evaluate=False,
                 options=ResearchOptions(reflection_mode='always', verify_claims=True))
    assert policy.model_calls == 2
    assert result['reflections'] == []
    assert result['report']['claims']
    assert result['claim_verification']['status'] == 'incomplete'
    assert result['metrics']['supported_claim_rate'] is None


def test_batch_variants_dry_run_is_paid_call_free(monkeypatch, tmp_path, capsys):
    from adra.live_evaluation import main
    monkeypatch.setattr('subprocess.run', lambda *args, **kwargs: pytest.fail('unexpected API run'))
    monkeypatch.setattr('sys.argv', ['eval', '--variants', 'baseline', 'event', 'always',
                                    '--repeat', '3', '--output', str(tmp_path / 'out')])
    main()
    assert '27 runs' in capsys.readouterr().out
    assert not (tmp_path / 'out').exists()
