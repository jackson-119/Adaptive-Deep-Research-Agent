import json
from pathlib import Path

import pytest

from adra.cli import save
from adra.graph import run
from adra.policy import DemoPolicy
from adra.state import Action, Budget, Claim, Report
from adra.tools import SnapshotTools


@pytest.fixture
def snapshot():
    return json.loads(Path('data/demo.json').read_text())


def test_end_to_end(snapshot, tmp_path):
    result = run(snapshot['query'], DemoPolicy(), SnapshotTools(snapshot))
    assert result['tool_calls'] == 3
    assert result['stop_reason'] == 'policy_stop'
    assert all(e['read'] for e in result['evidence'])
    assert result['transitions'][-1]['done']
    assert result['metrics']['citation_support'] is None
    save(result, tmp_path / 'run')
    assert '[E1]' in (tmp_path / 'run/report.md').read_text()
    assert len((tmp_path / 'run/trajectory.jsonl').read_text().splitlines()) == 4


@pytest.mark.parametrize('limit', [0, 1, 2])
def test_budget(snapshot, limit):
    result = run(snapshot['query'], DemoPolicy(), SnapshotTools(snapshot),
                 Budget(tool_calls=limit))
    assert result['tool_calls'] == limit
    assert result['stop_reason'] == 'tool_budget'


class RepeatPolicy(DemoPolicy):
    def decide(self, state):
        return Action(kind='SEARCH', argument=state['query'])


def test_duplicates_terminate(snapshot):
    result = run(snapshot['query'], RepeatPolicy(), SnapshotTools(snapshot),
                 Budget(tool_calls=10, steps=3))
    assert result['tool_calls'] == 1
    assert result['stop_reason'] == 'step_budget'
    assert result['metrics']['tool_errors'] == 2


def test_failure_charged(snapshot):
    result = run('missing', DemoPolicy(), SnapshotTools(snapshot))
    assert result['tool_calls'] == 1
    assert result['transitions'][0]['observation']['error'] == 'ValueError'
    assert result['evidence'] == []


def test_deduplication(snapshot):
    results = snapshot['searches'][snapshot['query']]
    results.append({**results[0], 'url': results[0]['url'] + '#section'})
    result = run(snapshot['query'], DemoPolicy(), SnapshotTools(snapshot))
    assert len(result['evidence']) == 2


def test_unknown_citation(snapshot):
    class BadWriter(DemoPolicy):
        def write(self, state):
            return Report(claims=[Claim(text='unsupported', evidence_ids=['E999'])])
    with pytest.raises(ValueError, match='unknown evidence'):
        run(snapshot['query'], BadWriter(), SnapshotTools(snapshot))


def test_arbitrary_read(snapshot):
    class BadReader(DemoPolicy):
        def decide(self, state):
            return Action(kind='READ', argument='http://127.0.0.1/secrets')
    result = run(snapshot['query'], BadReader(), SnapshotTools(snapshot), Budget(steps=1))
    assert result['tool_calls'] == 0
    assert result['metrics']['tool_errors'] == 1


def test_invalid_inputs(snapshot):
    with pytest.raises(ValueError):
        Action(kind='SEARCH', argument=' ')
    with pytest.raises(ValueError):
        Budget(tool_calls=-1)
    with pytest.raises(ValueError):
        run(' ', DemoPolicy(), SnapshotTools(snapshot))
