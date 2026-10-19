from types import SimpleNamespace

import httpx
import pytest

from adra.policy import ModelPolicy
from adra.state import Action
from adra.tools import TavilyTools


def test_model_usage_and_structure():
    class Model:
        def with_structured_output(self, schema, include_raw):
            assert schema is Action
            assert include_raw
            return self

        def invoke(self, messages):
            assert messages[0][0] == 'system'
            return {'raw': SimpleNamespace(usage_metadata={'total_tokens': 17}),
                    'parsed': Action(kind='STOP'), 'parsing_error': None}

    policy = ModelPolicy(Model())
    assert policy.decide({'query': 'test'}).kind == 'STOP'
    assert policy.tokens == 17
    assert policy.usage_complete
    assert policy.model_calls == 1


def test_tavily_request_contract(monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        data = ({'results': [{'url': 'https://example.org', 'content': 'snippet'}]}
                if url.endswith('/search') else {'results': [{'raw_content': 'page'}]})
        return httpx.Response(200, json=data, request=httpx.Request('POST', url))

    monkeypatch.setattr(httpx, 'post', post)
    tools = TavilyTools('test-key')
    assert tools.search('query')[0]['content'] == 'snippet'
    assert tools.read('https://example.org') == 'page'
    assert calls[0][1]['json']['query'] == 'query'
    assert calls[0][1]['headers']['Authorization'] == 'Bearer test-key'
    assert calls[1][1]['json']['urls'] == ['https://example.org']


def test_empty_extraction(monkeypatch):
    monkeypatch.setattr(TavilyTools, '_post', lambda *args: {'results': []})
    with pytest.raises(ValueError, match='no content'):
        TavilyTools('test').read('https://example.org')
