import hashlib
import json

from adra.cli import save
from adra.legacy.graph import run
from adra.passages import select_passages
from adra.legacy.planning import Plan, PlanningPolicy
from adra.policy import DemoPolicy
from adra.state import Budget
from adra.tools import SnapshotTools


def validate_selection(text, result):
    assert len(result['content']) <= 6000
    assert result['original_length'] == len(text)
    assert result['selected_length'] == len(result['content'])
    assert result['raw_sha256'] == hashlib.sha256(text.encode()).hexdigest()
    for passage in result['passages']:
        assert text[passage['start']:passage['end']] == passage['text']


def long_document():
    return ('General introductory background without relevant details.\n\n' * 180
            + 'Persistence uses a checkpointer and thread_id to restore saved state.\n\n'
            + 'More generic background.\n\n' * 20)


def test_late_paragraph_found():
    text = long_document()
    result = select_passages(text, [{'task_id': 'persistence', 'query': 'checkpointer thread_id'}])
    validate_selection(text, result)
    assert result['truncated']
    assert 'checkpointer' in result['content']
    relevant = next(p for p in result['passages'] if 'checkpointer' in p['text'])
    assert relevant['start'] > 6000
    assert relevant['paragraph_index'] == 180
    assert relevant['matched_task_ids'] == ['persistence']


def test_long_unbroken_paragraph_and_boundary():
    text = 'background ' * 900 + 'checkpointer thread_id' + ' background' * 900
    result = select_passages(text, [{'task_id': 'p', 'query': 'checkpointer thread_id'}])
    validate_selection(text, result)
    assert 'checkpointer thread_id' in result['content']
    assert all(p['paragraph_index'] == 0 for p in result['passages'])


def test_short_document_preserved_exactly():
    text = '  leading spaces\n\nSecond paragraph.\r\n'
    result = select_passages(text, [{'task_id': None, 'query': 'anything'}])
    validate_selection(text, result)
    assert result['content'] == text
    assert not result['truncated']


def test_chinese_relevance():
    text = ('无关的背景介绍内容。\n\n' * 700) + '检查点用于持久化，线程标识区分不同会话。'
    result = select_passages(text, [{'task_id': 'p', 'query': '检查点 持久化 线程标识'}])
    validate_selection(text, result)
    assert '线程标识' in result['content']


def test_deterministic_no_match_fallback():
    text = 'background only.\n\n' * 800
    focus = [{'task_id': None, 'query': 'checkpointer'}]
    a = select_passages(text, focus)
    assert a == select_passages(text, focus)
    assert all(p['score'] == 0 for p in a['passages'])
    validate_selection(text, a)


def test_shared_source_reselected_for_new_task(tmp_path):
    text = ('filler background.\n\n' * 500 + 'alpha_keyword details.\n\n'
            + 'background filler.\n\n' * 500 + 'beta_keyword details.')
    plan = Plan.model_validate({'query': 'comparison', 'tasks': [
        {'id': 'a', 'description': 'alpha_keyword', 'queries': ['alpha_keyword']},
        {'id': 'b', 'description': 'beta_keyword', 'queries': ['beta_keyword']},
    ]})
    url = 'https://example.org/shared'
    snapshot = {'searches': {q: [{'url': url, 'title': 'shared', 'content': 'summary'}]
                             for q in ['alpha_keyword', 'beta_keyword']}, 'pages': {url: text}}
    result = run('comparison', PlanningPolicy(plan, DemoPolicy(), 'static'),
                 SnapshotTools(snapshot), Budget(tool_calls=5))
    evidence = result['evidence'][0]
    assert 'alpha_keyword' in evidence['content'] and 'beta_keyword' in evidence['content']
    assert result['tool_calls'] == 3
    assert result['raw_documents']['E1']['read'] == text
    assert result['raw_documents']['E1']['search'] == 'summary'
    for transition in result['transitions']:
        assert 'raw_documents' not in transition['state']
    before_second_search = result['transitions'][2]['state']['evidence'][0]
    assert [f['task_id'] for f in before_second_search['selection_focus']] == ['a']
    assert [f['task_id'] for f in evidence['selection_focus']] == ['a', 'b']
    save(result, tmp_path / 'run')
    restored = json.loads((tmp_path / 'run/run.json').read_text())
    validate_selection(restored['raw_documents']['E1']['read'], restored['evidence'][0])


def test_baseline_writer_receives_excerpt_not_raw_document():
    text = long_document()
    class Writer(DemoPolicy):
        def write(self, state):
            assert 'raw_documents' not in state
            assert len(state['evidence'][0]['content']) <= 6000
            assert 'checkpointer' in state['evidence'][0]['content']
            return super().write(state)
    query = 'checkpointer thread_id'
    url = 'https://example.org/docs'
    snapshot = {'searches': {query: [{'url': url, 'title': 'docs', 'content': 'summary'}]},
                'pages': {url: text}}
    result = run(query, Writer(), SnapshotTools(snapshot))
    assert result['metrics']['truncated_sources'] == 1
    assert result['transitions'][1]['observation']['truncated']
    assert result['config']['evidence_char_limit'] == 6000


def test_crlf_paragraph_offsets():
    text = 'background\r\n\r\n' * 800 + 'checkpointer thread_id end'
    result = select_passages(text, [{'task_id': 'p', 'query': 'checkpointer thread_id'}])
    validate_selection(text, result)
    relevant = next(p for p in result['passages'] if 'checkpointer' in p['text'])
    assert relevant['paragraph_index'] == 800
