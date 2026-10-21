"""Production graph acceptance tests; historical scheduler tests live separately."""
from types import SimpleNamespace

import pytest

from adra.coverage import Assessment
from adra.evidence_store import add_source, retrieve
from adra.graph import run
from adra.planning import Plan
from adra.policy import ModelPolicy
from adra.research_models import ResearchDecision
from adra.state import Budget, Claim, Report
from adra.tools import SnapshotTools, TavilyTools

URL = 'https://docs.example.com/python/state'
TEXT = 'State schema defines shared state. Nodes return partial updates. Reducers merge updates.'


def brief():
    return Plan(query='Explain state and reducers', tasks=[
        {'id': 'schema', 'description': 'state schema', 'queries': ['initial state query'],
         'criteria': [{'id': 'schema', 'description': 'State schema and partial updates'}]},
        {'id': 'reducer', 'description': 'reducers', 'queries': ['initial reducer query'],
         'criteria': [{'id': 'reducer', 'description': 'Reducers merge updates'}]},
    ], allowed_url_prefixes=['https://docs.example.com/python'])


class Researcher:
    max_model_calls = None
    model_calls = 0
    def __init__(self, actions):
        self.actions = iter(actions)
        self.events = []
        self.contexts = []

    def research(self, state):
        self.events.append('research')
        self.contexts.append(state)
        return ResearchDecision(**next(self.actions), reason='test research decision')

    def write(self, state):
        self.events.append('write')
        assert state['coverage'] == {}
        self.writer_evidence = state['evidence']
        return Report(claims=[Claim(text=TEXT, evidence_ids=[state['evidence'][0]['id']])])

    def assess_report(self, task, evidence, report):
        self.events.append('evaluate')
        assert report['claims']
        assert evidence and evidence[0]['id'] == 'E1'
        return Assessment(judgments=[{'criterion_id': c.id, 'status': 'supported',
            'citations': [{'evidence_id': 'E1', 'quote': TEXT}], 'reason': 'Supported in report and text'}
            for c in task.criteria])


def tools(raw=True):
    return SnapshotTools({'searches': {'new query outside fixed plan': [
        {'url': URL, 'title': 'State', 'content': 'summary', **({'raw_content': TEXT} if raw else {})}
    ]}, 'pages': {URL: TEXT}})


def test_global_retrieval_ignores_discovery_membership_and_finds_late_text():
    evidence, documents = [], {}
    raw = ('Navigation overview ' * 3000) + '\n\ncheckpointer thread_id persistent state'
    add_source(evidence, documents, {'url': URL, 'raw_content': raw}, task_id='other', read=True)
    notes = retrieve(evidence, documents, ['checkpointer thread_id'], char_budget=2400)
    assert 'thread_id' in notes[0]['content']
    assert notes[0]['task_ids'] == ['other']
    for p in notes[0]['passages']:
        assert raw[p['start']:p['end']] == p['text']
    assert sum(len(e['content']) for e in notes) <= 2400


def test_dynamic_queries_and_shared_evidence_final_evaluation_only():
    policy = Researcher([{'kind': 'SEARCH', 'argument': 'new query outside fixed plan'},
                         {'kind': 'FINISH'}])
    result = run(brief().query, policy, tools(), Budget(tool_calls=3), plan=brief())
    assert policy.events == ['research', 'research', 'write', 'evaluate', 'evaluate']
    assert result['metrics']['semantic_coverage'] == 1
    assert result['tool_calls'] == 1
    assert result['evidence'][0]['task_ids'] == []
    assert result['evidence'][0]['read']
    assert result['architecture'] == 'global_research_v2'


def test_local_retrieve_does_not_consume_external_tool_budget():
    policy = Researcher([{'kind': 'SEARCH', 'argument': 'new query outside fixed plan'},
                         {'kind': 'RETRIEVE', 'argument': 'Reducers'}, {'kind': 'FINISH'}])
    result = run(brief().query, policy, tools(), Budget(tool_calls=3), plan=brief(), evaluate=False)
    assert result['tool_calls'] == 1
    assert result['steps'] == 2
    assert result['evaluation_status'] == 'not_requested'
    assert 'evaluate' not in policy.events


def test_read_is_available_when_search_returns_only_snippet():
    policy = Researcher([{'kind': 'SEARCH', 'argument': 'new query outside fixed plan'},
                         {'kind': 'READ', 'argument': URL}, {'kind': 'FINISH'}])
    result = run(brief().query, policy, tools(False), Budget(tool_calls=2), plan=brief())
    assert result['tool_calls'] == 2
    assert result['raw_documents']['E1']['read'] == TEXT


def test_duplicate_and_already_read_actions_are_bounded():
    policy = Researcher([{'kind': 'SEARCH', 'argument': 'new query outside fixed plan'},
                         {'kind': 'READ', 'argument': URL},
                         {'kind': 'SEARCH', 'argument': 'new query outside fixed plan'}])
    result = run(brief().query, policy, tools(), Budget(tool_calls=5, steps=3), plan=brief())
    assert result['tool_calls'] == 1
    assert result['metrics']['tool_errors'] == 2
    assert result['stop_reason'] == 'step_budget'


def test_final_judge_failure_keeps_report_and_does_not_claim_zero_quality():
    policy = Researcher([{'kind': 'SEARCH', 'argument': 'new query outside fixed plan'}])
    def broken(*args):
        raise ValueError('private error content')
    policy.assess_report = broken
    result = run(brief().query, policy, tools(), Budget(tool_calls=1), plan=brief())
    assert result['report']['claims']
    assert result['evaluation_status'] == 'incomplete'
    assert result['metrics']['semantic_coverage'] is None
    assert 'private error content' not in str(result)


def test_invalid_quote_fails_closed_after_writing():
    policy = Researcher([{'kind': 'SEARCH', 'argument': 'new query outside fixed plan'}])
    policy.assess_report = lambda task, evidence, report: Assessment(judgments=[{
        'criterion_id': task.criteria[0].id, 'status': 'supported',
        'citations': [{'evidence_id': 'E1', 'quote': 'invented text'}], 'reason': 'wrong'}])
    result = run(brief().query, policy, tools(), Budget(tool_calls=1), plan=brief())
    assert result['evaluation_status'] == 'incomplete'
    assert result['report']['claims']


def test_disallowed_sources_never_enter_corpus():
    policy = Researcher([{'kind': 'SEARCH', 'argument': 'new query outside fixed plan'}])
    plan = brief().model_copy(update={'allowed_url_prefixes': []})
    result = run(plan.query, policy, tools(), Budget(tool_calls=1), plan=plan,
                 allowed_domains=['other.example.com'])
    assert result['evidence'] == []
    assert result['metrics']['rejected_sources'] == 1
    assert result['report']['claims'] == []


def test_snippet_does_not_overwrite_read():
    evidence, documents = [], {}
    add_source(evidence, documents, {'url': URL, 'raw_content': TEXT}, read=True)
    add_source(evidence, documents, {'url': URL, 'content': 'short snippet'})
    assert evidence[0]['read']
    assert documents['E1']['read'] == TEXT


def test_search_requests_body_without_implicit_extra_calls(monkeypatch):
    backend = TavilyTools('not-a-real-key', include_raw_content=True)
    requests = []
    def post(endpoint, payload):
        requests.append((endpoint, payload))
        return {'results': []}
    monkeypatch.setattr(backend, '_post', post)
    backend.search('state')
    assert len(requests) == 1
    assert requests[0][1]['include_raw_content'] is True


def test_model_budget_reserves_writer_and_judge_cannot_overrun():
    class FakeModel:
        def with_structured_output(self, schema, **kwargs):
            self.schema = schema
            return self
        def invoke(self, messages):
            if self.schema is ResearchDecision:
                value = ResearchDecision(kind='SEARCH', argument='new query outside fixed plan', reason='research')
            else:
                assert self.schema is Report
                value = Report(claims=[Claim(text=TEXT, evidence_ids=['E1'])])
            return {'parsed': value, 'raw': SimpleNamespace(usage_metadata={'total_tokens': 10})}
    policy = ModelPolicy(FakeModel(), max_model_calls=2)
    result = run(brief().query, policy, tools(), Budget(tool_calls=8), plan=brief())
    assert policy.model_calls == 2
    assert [r['phase'] for r in policy.call_records] == ['decision', 'writing']
    assert result['report']['claims']
    assert result['evaluation_status'] == 'incomplete'
    assert result['metrics']['semantic_coverage'] is None


def test_unknown_report_citation_is_rejected():
    policy = Researcher([{'kind': 'SEARCH', 'argument': 'new query outside fixed plan'}])
    policy.write = lambda state: Report(claims=[Claim(text='unsupported', evidence_ids=['E99'])])
    with pytest.raises(ValueError, match='retrieved read evidence'):
        run(brief().query, policy, tools(), Budget(tool_calls=1), plan=brief())


def test_modified_raw_document_is_not_silently_reused():
    evidence, documents = [], {}
    add_source(evidence, documents, {'url': URL, 'raw_content': TEXT}, read=True)
    documents['E1']['read'] += ' tampered'
    with pytest.raises(ValueError, match='digest mismatch'):
        retrieve(evidence, documents, ['state'])


def test_invalid_read_target_is_an_observation_not_lost_run():
    policy = Researcher([{'kind': 'READ', 'argument': 'file:///secret'}])
    result = run(brief().query, policy, tools(), Budget(steps=1), plan=brief())
    assert result['metrics']['tool_errors'] == 1
    assert result['tool_calls'] == 0
    assert result['report']['claims'] == []


def test_production_cli_generates_brief_and_saves_new_artifacts(monkeypatch, tmp_path):
    import json
    from adra.cli import main
    from adra.configuration import RuntimeConfig
    policy = Researcher([{'kind': 'SEARCH', 'argument': 'new query outside fixed plan'}])
    policy.generate_brief = lambda query, budget: brief()
    monkeypatch.setenv('ADRA_MODEL', 'test:model')
    monkeypatch.setenv('TAVILY_API_KEY', 'fake-key')
    monkeypatch.setattr(RuntimeConfig, 'create_policy', lambda self: policy)
    monkeypatch.setattr(TavilyTools, 'search', lambda self, query: tools().search(query))
    monkeypatch.setattr('sys.argv', ['adra', brief().query, '--max-tools', '1', '--output', str(tmp_path)])
    main()
    saved = json.loads(next(tmp_path.glob('*/run.json')).read_text())
    assert saved['architecture'] == 'global_research_v2'
    assert saved['plan_source'] == 'generated'
    assert next(tmp_path.glob('*/evaluation.json')).exists()
    assert next(tmp_path.glob('*/research_notes.json')).exists()
    assert 'fake-key' not in str(saved)


def test_independent_evaluation_reuses_report_not_previous_scores(monkeypatch, tmp_path):
    import json
    from adra.report_evaluation import main
    from adra.configuration import RuntimeConfig
    policy = Researcher([{'kind': 'SEARCH', 'argument': 'new query outside fixed plan'}])
    saved = run(brief().query, policy, tools(), Budget(tool_calls=1), plan=brief())
    path = tmp_path / 'run.json'
    path.write_text(json.dumps(saved))
    original = path.read_bytes()
    policy.events = []
    policy.call_records, policy.tokens, policy.usage_complete = [], 0, True
    monkeypatch.setenv('ADRA_MODEL', 'test:judge')
    monkeypatch.setattr(RuntimeConfig, 'create_policy', lambda self: policy)
    monkeypatch.setattr('sys.argv', ['evaluate', '--run', str(path), '--output', str(tmp_path / 'evaluation.json')])
    main()
    assert policy.events == ['evaluate', 'evaluate']
    assert path.read_bytes() == original
    assert json.loads((tmp_path / 'evaluation.json').read_text())['status'] == 'completed'
