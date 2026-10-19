import json
from types import SimpleNamespace

import pytest

from adra.coverage import Assessment, ReplayAssessor, assessment_key
from adra.graph import run
from adra.planning import Plan, PlanningPolicy
from adra.policy import DemoPolicy, ModelPolicy
from adra.state import Budget
from adra.tools import SnapshotTools


@pytest.fixture
def case():
    plan = Plan.model_validate({'query': 'State and persistence', 'tasks': [
        {'id': 'state', 'description': 'State and persistence', 'queries': ['state', 'persistence'],
         'criteria': [
             {'id': 'schema', 'description': 'How is the state schema defined?'},
             {'id': 'persistence', 'description': 'What do checkpointer and thread_id do?'}]}]})
    snapshot = {'query': plan.query, 'searches': {
        q: [{'url': 'https://example.org/' + q, 'title': q, 'content': 'synthetic summary'}]
        for q in ['state', 'persistence']}, 'pages': {
        'https://example.org/state': 'State schema defines fields for node updates.',
        'https://example.org/persistence': 'A checkpointer saves state; thread_id selects the conversation.'}}
    return plan, snapshot


class AnnotatedAssessor:
    """Hand-authored synthetic annotations, not an automatic semantic judge."""
    def __init__(self):
        self.calls = 0
        self.replay = {}

    def assess_coverage(self, task, evidence):
        self.calls += 1
        judgments = []
        for criterion, suffix in [('schema', 'state'), ('persistence', 'persistence')]:
            source = next((e for e in evidence if e['url'] == 'https://example.org/' + suffix), None)
            judgments.append({'criterion_id': criterion, 'status': 'supported' if source else 'missing',
                              'citations': [{'evidence_id': source['id'], 'quote': source['content']}]
                              if source else [], 'reason': 'Synthetic manually annotated example.'})
        output = Assessment.model_validate({'judgments': judgments})
        self.replay[assessment_key(task, evidence)] = output.model_dump()
        return output


def execute(case, assessor, tools=4):
    plan, snapshot = case
    policy = PlanningPolicy(plan, DemoPolicy(), 'adaptive', 'semantic', assessor)
    return run(plan.query, policy, SnapshotTools(snapshot), Budget(tool_calls=tools))


def test_missing_checkpointer_blocks_completion(case):
    result = execute(case, AnnotatedAssessor(), tools=2)
    assert result['metrics']['source_coverage_proxy'] == 1
    assert result['metrics']['semantic_coverage'] == 0.5
    assert result['metrics']['covered_tasks'] == 0
    assert result['coverage']['state']['judgments'][1]['status'] == 'missing'
    assert any('persistence: missing' in s for s in result['report']['limitations'])


def test_gap_triggers_existing_alternative_query(case):
    assessor = AnnotatedAssessor()
    result = execute(case, assessor)
    assert result['metrics']['covered_tasks'] == 1
    assert result['metrics']['semantic_coverage'] == 1
    assert result['tool_calls'] == 4
    # No assessment of snippets and no redundant reassessment after a search with no new read.
    assert assessor.calls == 2
    for judgment in result['coverage']['state']['judgments']:
        citation = judgment['citations'][0]
        raw = result['raw_documents'][citation['evidence_id']]['read']
        assert raw[citation['start']:citation['end']] == citation['quote']
    assert result['transitions'][2]['state']['coverage']['state']['judgments'][1]['status'] == 'missing'


@pytest.mark.parametrize('fault', ['quote', 'unknown_source', 'cross_task', 'missing_criterion',
                                   'duplicate_criterion', 'no_citation', 'partial', 'exception'])
def test_invalid_or_partial_judgments_never_complete(case, fault):
    class BadAssessor(AnnotatedAssessor):
        def assess_coverage(self, task, evidence):
            if fault == 'exception':
                raise RuntimeError('secret provider response')
            output = super().assess_coverage(task, evidence)
            j = output.judgments[0]
            if fault == 'quote':
                j.citations[0].quote = 'invented quotation'
            elif fault == 'unknown_source':
                j.citations[0].evidence_id = 'E999'
            elif fault == 'cross_task':
                j.citations[0].evidence_id = 'other-task-source'
            elif fault == 'missing_criterion':
                output.judgments.pop()
            elif fault == 'duplicate_criterion':
                output.judgments[1].criterion_id = 'schema'
            elif fault == 'no_citation':
                j.citations = []
            else:
                j.status = 'partial'
            return output
    result = execute(case, BadAssessor(), tools=2)
    assert result['metrics']['semantic_coverage'] == 0
    assert result['metrics']['covered_tasks'] == 0
    assert 'secret provider' not in json.dumps(result)


def test_replay_requires_exact_evidence(case):
    assessor = AnnotatedAssessor()
    original = execute(case, assessor)
    replayed = execute(case, ReplayAssessor(assessor.replay))
    assert original['coverage'] == replayed['coverage']
    case[1]['pages']['https://example.org/state'] += ' New revision.'
    changed = execute(case, ReplayAssessor(assessor.replay))
    assert changed['metrics']['covered_tasks'] == 0
    assert changed['coverage']['state']['validation'] == 'assessment_failed'


def test_no_criteria_or_assessor_rejected(case):
    plan, _ = case
    with pytest.raises(ValueError):
        PlanningPolicy(plan, DemoPolicy(), 'adaptive', 'semantic')
    plan.tasks[0].criteria = []
    with pytest.raises(ValueError):
        PlanningPolicy(plan, DemoPolicy(), 'adaptive', 'semantic', AnnotatedAssessor())


def test_model_assessment_cost_recorded(case):
    class Model:
        def with_structured_output(self, schema, include_raw):
            self.schema = schema
            return self
        def invoke(self, messages):
            if self.schema is Assessment:
                parsed = {'judgments': [dict(criterion_id=c, status='missing', citations=[],
                                            reason='Not established.') for c in ['schema', 'persistence']]}
            else:
                parsed = {'claims': [], 'limitations': ['Missing evidence.']}
            return {'raw': SimpleNamespace(usage_metadata={'total_tokens': 7}), 'parsed': parsed}
    writer = ModelPolicy(Model())
    plan, snapshot = case
    result = run(plan.query, PlanningPolicy(plan, writer, 'adaptive', 'semantic'),
                 SnapshotTools(snapshot), Budget(tool_calls=2))
    assert [r['phase'] for r in result['model_call_records']] == ['coverage', 'writing']
    assert result['metrics']['total_tokens'] == 14


def test_duplicate_criteria_rejected(case):
    data = case[0].model_dump()
    data['tasks'][0]['criteria'].append(data['tasks'][0]['criteria'][0])
    with pytest.raises(ValueError):
        Plan.model_validate(data)


def test_serialized_plan_replay_key_is_stable(case):
    original = AnnotatedAssessor()
    execute(case, original)
    loaded = Plan.model_validate_json(case[0].model_dump_json())
    result = execute((loaded, case[1]), ReplayAssessor(original.replay))
    assert result['metrics']['semantic_coverage'] == 1


def test_auto_plan_requires_criteria(case):
    valid = case[0].model_dump()
    invalid = json.loads(json.dumps(valid))
    invalid['tasks'][0]['criteria'] = []
    class Model:
        def __init__(self):
            self.outputs = iter([invalid, valid])
        def with_structured_output(self, schema, include_raw):
            return self
        def invoke(self, messages):
            return {'raw': SimpleNamespace(usage_metadata={'total_tokens': 5}),
                    'parsed': next(self.outputs)}
    policy = ModelPolicy(Model())
    generated = policy.generate_plan(case[0].query, Budget(), semantic_coverage=True)
    assert generated.tasks[0].criteria
    assert policy.model_calls == 2
    assert policy.plan_generation['attempts'][0]['reason'] == 'missing_acceptance_criteria'


def test_dependency_waits_for_semantic_support(case):
    data = case[0].model_dump()
    data['tasks'].append({'id': 'dependent', 'description': 'downstream analysis',
                          'queries': ['downstream'], 'dependencies': ['state'],
                          'criteria': [{'id': 'd', 'description': 'analyze persistence'}]})
    result = execute((Plan.model_validate(data), case[1]), AnnotatedAssessor(), tools=2)
    assert result['planning']['tasks'][1]['status'] == 'blocked'


def test_cli_semantic_replay(monkeypatch, tmp_path):
    from adra.cli import main
    monkeypatch.setattr('sys.argv', ['adra', '--snapshot', 'data/coverage/snapshot.json',
        '--plan', 'data/coverage/plan.json', '--strategy', 'adaptive',
        '--coverage-mode', 'semantic', '--coverage-replay', 'data/coverage/assessments.json',
        '--max-tools', '2', '--output', str(tmp_path)])
    main()
    result = json.loads(next(tmp_path.glob('*/run.json')).read_text())
    assert result['metrics']['semantic_coverage'] == 0.5
    assert result['metrics']['covered_tasks'] == 0
    assert next(tmp_path.glob('*/coverage.json')).is_file()


@pytest.mark.parametrize('args', [
    ['--coverage-mode', 'semantic'],
    ['--strategy', 'adaptive', '--plan', 'data/coverage/plan.json',
     '--snapshot', 'data/coverage/snapshot.json', '--coverage-mode', 'semantic'],
    ['--coverage-replay', 'data/coverage/assessments.json'],
])
def test_invalid_semantic_cli(monkeypatch, args):
    from adra.cli import main
    monkeypatch.setattr('sys.argv', ['adra', *args])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 2
