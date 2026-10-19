import json
from types import SimpleNamespace

import pytest

from adra.cli import main
from adra.graph import run
from adra.planning import PlanningPolicy
from adra.policy import ModelPolicy
from adra.state import Budget
from adra.tools import SnapshotTools


def plan(query='question'):
    return {'query': query, 'tasks': [
        {'id': 't1', 'description': 'research question', 'queries': ['focused']}]}


class Model:
    def __init__(self, outputs, usage=10):
        self.outputs = iter(outputs)
        self.usage = usage
        self.messages = []

    def with_structured_output(self, schema, include_raw):
        return self

    def invoke(self, messages):
        self.messages.append(messages)
        output = next(self.outputs)
        if isinstance(output, Exception):
            raise output
        return {'raw': SimpleNamespace(usage_metadata=(
            {'total_tokens': self.usage} if self.usage is not None else None)),
                'parsed': output, 'parsing_error': None}


def test_generated_plan_end_to_end():
    policy = ModelPolicy(Model([plan(), {'claims': [
        {'text': 'supported example', 'evidence_ids': ['E1']}]}]))
    budget = Budget(tool_calls=2)
    generated = policy.generate_plan('question', budget)
    snapshot = {'searches': {'focused': [
        {'url': 'https://example.org/', 'title': 'example', 'content': 'snippet'}]},
        'pages': {'https://example.org/': 'supported example'}}
    result = run('question', PlanningPolicy(generated, policy, 'adaptive'),
                 SnapshotTools(snapshot), budget)
    assert result['metrics']['total_tokens'] == 20
    assert result['metrics']['model_calls'] == 2
    assert [c['phase'] for c in result['model_call_records']] == ['planning', 'writing']
    assert result['plan_generation']['status'] == 'accepted'
    assert result['metrics']['planning_latency_seconds'] >= 0
    assert result['metrics']['latency_seconds'] >= result['metrics']['planning_latency_seconds']
    assert result['metrics']['covered_tasks'] == 1


@pytest.mark.parametrize('invalid', ['query', 'cycle', 'budget', 'duplicate'])
def test_invalid_plan_corrected_once(invalid):
    bad = plan()
    if invalid == 'query':
        bad['query'] = 'another question'
    elif invalid == 'cycle':
        bad['tasks'][0]['dependencies'] = ['t1']
    elif invalid == 'budget':
        bad['tasks'][0]['target_sources'] = 10
    else:
        bad['tasks'].append({**bad['tasks'][0], 'id': 't2'})
    model = Model([bad, plan()])
    policy = ModelPolicy(model)
    generated = policy.generate_plan('question', Budget(tool_calls=2))
    assert generated.query == 'question'
    assert policy.tokens == 20
    assert len(policy.plan_generation['attempts']) == 2
    assert 'feedback' in json.loads(model.messages[1][1][1])


def test_invalid_plan_stops_after_two_attempts():
    policy = ModelPolicy(Model([plan('wrong'), plan('wrong')]))
    with pytest.raises(ValueError, match='two attempts'):
        policy.generate_plan('question', Budget())
    assert policy.model_calls == 2
    assert policy.plan_generation['status'] == 'failed'
    assert policy.tokens == 20


def test_provider_failure_is_not_retried_or_exposed():
    policy = ModelPolicy(Model([RuntimeError('secret request body')]))
    with pytest.raises(RuntimeError):
        policy.generate_plan('question', Budget())
    assert policy.model_calls == 1
    assert not policy.usage_complete
    assert 'secret' not in json.dumps(policy.call_records)
    assert policy.plan_generation['error_type'] == 'RuntimeError'


def test_missing_usage_is_explicit():
    policy = ModelPolicy(Model([plan()], usage=None))
    policy.generate_plan('question', Budget())
    assert not policy.usage_complete
    assert policy.call_records[0]['tokens'] is None


def test_budget_rejected_before_call():
    policy = ModelPolicy(Model([]))
    with pytest.raises(ValueError):
        policy.generate_plan('question', Budget(tool_calls=1))
    assert policy.model_calls == 0


@pytest.mark.parametrize('arguments', [
    ['--auto-plan'],
    ['--auto-plan', '--strategy', 'adaptive', '--snapshot', 'data/demo.json'],
    ['--auto-plan', '--strategy', 'static', '--max-tools', '1'],
    ['--auto-plan', '--strategy', 'static', '--plan', 'data/planning/plan.json'],
])
def test_invalid_cli_auto_plan(monkeypatch, arguments):
    monkeypatch.setattr('sys.argv', ['adra', *arguments])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 2


def setup_cli(monkeypatch, tmp_path, outputs):
    chat_models = pytest.importorskip('langchain.chat_models')
    monkeypatch.setenv('ADRA_MODEL', 'test:model')
    monkeypatch.setenv('TAVILY_API_KEY', 'test-key')
    monkeypatch.setattr(chat_models, 'init_chat_model', lambda *a, **kw: Model(outputs))
    monkeypatch.setattr('sys.argv', ['adra', 'question', '--auto-plan', '--strategy', 'adaptive',
                                   '--output', str(tmp_path)])


def test_cli_saves_generated_plan(monkeypatch, tmp_path):
    setup_cli(monkeypatch, tmp_path, [plan(), {'claims': [], 'limitations': ['no evidence']}])
    monkeypatch.setattr('adra.tools.TavilyTools.search', lambda *a: [])
    main()
    result = json.loads(next(tmp_path.glob('*/run.json')).read_text())
    saved = json.loads(next(tmp_path.glob('*/plan.json')).read_text())
    assert saved == result['plan']
    assert result['plan_source'] == 'generated'
    assert result['metrics']['total_tokens'] == 20


def test_cli_saves_planning_failure(monkeypatch, tmp_path):
    setup_cli(monkeypatch, tmp_path, [plan('wrong'), plan('wrong')])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 1
    failure = json.loads(next(tmp_path.glob('*/failure.json')).read_text())
    assert failure['plan_generation']['status'] == 'failed'
    assert failure['total_tokens'] == 20
    assert not list(tmp_path.glob('*/report.md'))
    assert 'test-key' not in json.dumps(failure)


def test_cli_preserves_plan_when_writer_fails(monkeypatch, tmp_path):
    setup_cli(monkeypatch, tmp_path, [plan(), RuntimeError('private provider response')])
    monkeypatch.setattr('adra.tools.TavilyTools.search', lambda *a: [])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 1
    text = next(tmp_path.glob('*/failure.json')).read_text()
    failure = json.loads(text)
    assert failure['plan']['query'] == 'question'
    assert failure['plan_generation']['status'] == 'accepted'
    assert failure['model_call_records'][-1]['phase'] == 'writing'
    assert not failure['token_usage_complete']
    assert 'private provider' not in text


@pytest.mark.parametrize('kind', ['query_mismatch', 'plan_exceeds_budget'])
def test_plan_diagnostics_and_targeted_feedback(kind):
    bad = plan('wrong' if kind == 'query_mismatch' else 'question')
    if kind == 'plan_exceeds_budget':
        bad['tasks'][0]['target_sources'] = 2
    model = Model([bad, plan()])
    policy = ModelPolicy(model)
    policy.generate_plan('question', Budget(tool_calls=2))
    attempt = policy.plan_generation['attempts'][0]
    assert attempt['reason'] == kind
    assert attempt['capacity'] == 2
    assert attempt['task_count'] == 1
    feedback = json.loads(model.messages[1][1][1])['feedback']
    assert feedback['reason'] == kind
    if kind == 'plan_exceeds_budget':
        assert feedback['estimated_tool_calls'] == 3
    else:
        assert not feedback['query_matches']


def test_schema_error_diagnostic_does_not_echo_model_output():
    bad = plan()
    bad['private_key'] = 'sensitive-model-text'
    policy = ModelPolicy(Model([bad, plan()]))
    policy.generate_plan('question', Budget())
    assert policy.plan_generation['attempts'][0]['reason'] == 'structured_output_or_schema_error'
    assert 'sensitive-model-text' not in json.dumps(policy.plan_generation)
