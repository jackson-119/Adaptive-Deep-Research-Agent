import json

import pytest

from adra.cli import main
from adra.domains import DomainPolicy, SourceDomainError
from adra.graph import build_graph, run
from adra.policy import DemoPolicy
from adra.state import Action, Budget
from adra.tools import SnapshotTools, TavilyTools


@pytest.mark.parametrize("suffix", [
    "/javascript/a", "/pythonish/a", "/python/../javascript/a",
    "/python/%2e%2e/javascript/a", "/python/%252e%252e/javascript/a",
    "/python/%5c../javascript/a",
])
def test_path_scope_rejects_other_language_and_traversal(suffix):
    policy = DomainPolicy(["example.org"], ["https://example.org/python"])
    assert not policy.allows("https://example.org" + suffix)
    assert policy.allows("https://example.org/python/state?x=1#section")


def test_extraction_rejects_disclosed_cross_language_redirect(monkeypatch):
    tools = TavilyTools("fake")
    tools.domain_policy = DomainPolicy(url_prefixes=["https://example.org/python"])
    monkeypatch.setattr(TavilyTools, "_post", lambda *args: {"results": [{
        "url": "https://example.org/javascript/state", "raw_content": "Annotation"}]})
    with pytest.raises(SourceDomainError):
        tools.read("https://example.org/python/state")


@pytest.mark.parametrize('url', [
    'https://docs.langchain.com/a', 'https://DOCS.LANGCHAIN.COM/a',
    'https://docs.langchain.com./a', 'https://docs.langchain.com:443/a',
])
def test_exact_host_allowed(url):
    assert DomainPolicy(['Docs.LangChain.com.']).allows(url)


@pytest.mark.parametrize('url', [
    'https://docs.langchain.com.example.org/a', 'https://example.org/docs.langchain.com',
    'https://example.org/?url=https://docs.langchain.com',
    'https://docs.langchain.com@example.org/a', 'https://evil@docs.langchain.com/a',
    'https://child.docs.langchain.com/a', 'https://langchain.com/a',
    'https://docs-langchain.com/a', 'javascript:docs.langchain.com',
    'https://docs.langchain.com:invalid/', 'https://docs.langchain.com:99999/',
    'https://docs.langchain.com\\@example.org', 'https://docs.langchain.com\n.example.org',
    'https://docs%2elangchain.com/', '//docs.langchain.com/path',
])
def test_deceptive_hosts_rejected(url):
    assert not DomainPolicy(['docs.langchain.com']).allows(url)


@pytest.mark.parametrize('domain', [
    '', 'https://docs.langchain.com', 'docs.langchain.com/path', '*.langchain.com',
    'docs.langchain.com:443', 'a@docs.langchain.com', '127.0.0.1', '-bad.example',
])
def test_invalid_allowlist(domain):
    with pytest.raises(ValueError):
        DomainPolicy([domain])


def test_normalization_and_idna():
    policy = DomainPolicy(['DOCS.LANGCHAIN.COM', 'docs.langchain.com.', '例子.测试'])
    assert len(policy.domains) == 2
    assert policy.allows('https://例子.测试/path')
    assert policy.allows('https://xn--fsqu00a.xn--0zwm56d/path')


def test_search_passes_restriction(monkeypatch):
    calls = []
    def post(self, endpoint, payload):
        calls.append((endpoint, payload))
        return {'results': []}
    monkeypatch.setattr(TavilyTools, '_post', post)
    TavilyTools('fake', ['docs.langchain.com']).search('query')
    assert calls[0][1]['include_domains'] == ['docs.langchain.com']
    assert calls[0][1]['include_domains_mode'] == 'restrict'
    TavilyTools('fake').search('query')
    assert 'include_domains' not in calls[1][1]


@pytest.mark.parametrize('returned_url', ['https://example.org/a', None])
def test_extraction_checks_returned_url(monkeypatch, returned_url):
    result = {'raw_content': 'third-party text'}
    if returned_url:
        result['url'] = returned_url
    monkeypatch.setattr(TavilyTools, '_post', lambda *a: {'results': [result]})
    with pytest.raises(SourceDomainError):
        TavilyTools('fake', ['docs.langchain.com']).read('https://docs.langchain.com/a')


def test_read_denied_before_network(monkeypatch):
    def post(*args):
        pytest.fail('Must not call external service for denied URL')
    monkeypatch.setattr(TavilyTools, '_post', post)
    with pytest.raises(SourceDomainError):
        TavilyTools('fake', ['docs.langchain.com']).read('https://example.org/')


def snapshot():
    return {'query': 'question', 'searches': {'question': [
        {'url': 'https://example.org/', 'title': 'third party', 'content': 'denied'},
        {'url': 'https://docs.langchain.com.example.org/', 'title': 'deceptive', 'content': 'denied'},
        {'url': 'https://docs.langchain.com/', 'title': 'official', 'content': 'accepted'},
    ]}, 'pages': {'https://docs.langchain.com/': 'official page'}}


def test_mixed_sources_filtered_and_traced():
    result = run('question', DemoPolicy(), SnapshotTools(snapshot()),
                 allowed_domains=['docs.langchain.com'])
    assert len(result['evidence']) == 1
    assert result['evidence'][0]['url'] == 'https://docs.langchain.com/'
    assert result['metrics']['rejected_sources'] == 2
    assert result['report']['claims'][0]['evidence_ids'] == ['E1']
    assert result['tool_calls'] == 2
    assert result['config']['allowed_domains'] == ['docs.langchain.com']
    assert result['transitions'][0]['state']['allowed_domains'] == ['docs.langchain.com']


def test_all_denied_no_fallback():
    result = run('question', DemoPolicy(), SnapshotTools(snapshot()),
                 allowed_domains=['reference.langchain.com'])
    assert result['evidence'] == []
    assert result['report']['claims'] == []
    assert result['metrics']['rejected_sources'] == 3
    assert result['tool_calls'] == 1


def test_writer_rechecks_domains_on_seeded_state():
    class Stop(DemoPolicy):
        def decide(self, state):
            return Action(kind='STOP')
        def write(self, state):
            pytest.fail('Rejected evidence must not be sent to the writer')
    state = dict(task_id='test', query='q', action={}, tool_calls=0, steps=0,
                 transitions=[], stop_reason='', report={}, planning={}, allowed_domains=[],
                 evidence=[dict(id='E1', url='https://example.org/', content='bad', read=True)])
    with pytest.raises(SourceDomainError):
        build_graph(Stop(), SnapshotTools(snapshot()), Budget(), ['docs.langchain.com']).invoke(state)


def test_cli_offline_domain_filter(monkeypatch, tmp_path):
    path = tmp_path / 'snapshot.json'
    path.write_text(json.dumps(snapshot()))
    monkeypatch.setattr('sys.argv', ['adra', '--snapshot', str(path),
                                   '--allowed-domain', 'docs.langchain.com',
                                   '--allowed-domain', 'reference.langchain.com',
                                   '--output', str(tmp_path / 'runs')])
    main()
    result = json.loads(next(tmp_path.glob('runs/*/run.json')).read_text())
    assert result['config']['allowed_domains'] == ['docs.langchain.com', 'reference.langchain.com']
    report = next(tmp_path.glob('runs/*/report.md')).read_text()
    assert 'https://example.org' not in report
    assert 'https://docs.langchain.com/' in report


def test_cli_rejects_url_instead_of_host(monkeypatch):
    monkeypatch.setattr('sys.argv', ['adra', '--allowed-domain', 'https://docs.langchain.com'])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 2


def test_allowed_extraction_succeeds(monkeypatch):
    monkeypatch.setattr(TavilyTools, '_post', lambda *a: {'results': [
        {'url': 'https://docs.langchain.com/page', 'raw_content': 'official body'}]})
    assert TavilyTools('fake', ['docs.langchain.com']).read(
        'https://docs.langchain.com/page') == 'official body'


def test_comparison_uses_case_allowlist(tmp_path):
    from adra.evaluation import evaluate
    case = dict(id='domains', plan={'query': 'question', 'tasks': [
        {'id': 'task', 'description': 'official docs', 'queries': ['question']}]},
        snapshot=snapshot(), budget={'tool_calls': 4, 'steps': 6},
        allowed_domains=['docs.langchain.com'])
    suite = tmp_path / 'suite.json'
    suite.write_text(json.dumps([case]))
    result = evaluate(suite, tmp_path / 'evaluation')
    assert all(r['source_count'] == 1 for r in result['runs'])
    assert all(r['rejected_sources'] == 2 for r in result['runs'])
    saved = json.loads((tmp_path / 'evaluation/domains/static/run.json').read_text())
    assert saved['config']['allowed_domains'] == ['docs.langchain.com']
    assert 'git_commit' in saved['provenance']
