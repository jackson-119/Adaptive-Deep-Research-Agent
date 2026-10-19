import json
from pathlib import Path

import pytest

from adra.evaluation import evaluate
from adra.graph import run
from adra.planning import Plan, PlanningPolicy
from adra.policy import DemoPolicy
from adra.state import Budget
from adra.tools import SnapshotTools


@pytest.fixture
def cases():
    return json.loads(Path('data/planning/suite.json').read_text())


def execute(case, strategy='static', **limits):
    policy = PlanningPolicy(Plan.model_validate(case['plan']), DemoPolicy(), strategy)
    return run(case['plan']['query'], policy, SnapshotTools(case['snapshot']),
               Budget(**(limits or case['budget'])))


@pytest.mark.parametrize('strategy', ['static', 'adaptive'])
def test_dependencies_and_completion(cases, strategy):
    result = execute(cases[1], strategy)
    searches = [t['action']['task_id'] for t in result['transitions']
                if t['action']['kind'] == 'SEARCH']
    assert searches == ['foundation', 'dependent']
    assert result['stop_reason'] == 'plan_covered'
    assert result['metrics']['source_coverage_proxy'] == 1
    assert result['metrics']['covered_tasks'] == 2


def test_budget_changes_order(cases):
    static = execute(cases[0], 'static')
    adaptive = execute(cases[0], 'adaptive')
    assert static['transitions'][0]['action']['task_id'] == 'broad'
    assert adaptive['transitions'][0]['action']['task_id'] == 'focused'
    assert static['metrics']['source_coverage_proxy'] == pytest.approx(1 / 3)
    assert adaptive['metrics']['source_coverage_proxy'] == pytest.approx(2 / 3)
    assert static['tool_calls'] <= 4 and adaptive['tool_calls'] <= 4
    assert '未达到来源阅读目标' in static['report']['limitations'][1]
    assert adaptive['transitions'][0]['state']['planning']['selected']['score'] == 0.5


def test_empty_search_uses_next_query(cases):
    result = execute(cases[2], 'adaptive')
    actions = [t['action']['argument'] for t in result['transitions']]
    assert actions[:2] == ['empty', 'fallback']
    assert result['stop_reason'] == 'plan_covered'
    assert result['tool_calls'] == 3


def test_failed_read_does_not_loop(cases):
    result = execute(cases[3], 'adaptive')
    assert result['tool_calls'] == 4
    assert result['metrics']['tool_errors'] == 1
    assert result['stop_reason'] == 'no_feasible_plan_action'
    assert result['planning']['tasks'][0]['status'] == 'exhausted'
    assert result['planning']['tasks'][1]['status'] == 'covered'


def test_shared_evidence_preserves_prior_state(cases):
    result = execute(cases[4])
    assert len(result['evidence']) == 1
    assert result['evidence'][0]['task_ids'] == ['one', 'two']
    assert result['metrics']['covered_tasks'] == 2
    # Association discovered later must not mutate previous trajectory snapshots.
    earlier = result['transitions'][1]['state']['evidence'][0]
    assert earlier['task_ids'] == ['one']
    assert not earlier['read']
    assert result['tool_calls'] == 3


@pytest.mark.parametrize('tools,steps', [(1, 10), (6, 1)])
def test_does_not_start_unaffordable_search(cases, tools, steps):
    result = execute(cases[0], tool_calls=tools, steps=steps)
    assert result['tool_calls'] == 0
    assert result['stop_reason'] == 'no_feasible_plan_action'


def test_exhausted_dependency_blocks_child(cases):
    case = cases[1]
    case['snapshot']['searches']['foundation'] = []
    result = execute(case)
    assert result['tool_calls'] == 1
    assert result['planning']['tasks'][0]['status'] == 'blocked'
    assert result['planning']['tasks'][1]['status'] == 'exhausted'


@pytest.mark.parametrize('mutation', ['cycle', 'unknown', 'duplicate_id', 'empty_query', 'shared_query'])
def test_invalid_plans(cases, mutation):
    data = cases[1]['plan']
    first, second = data['tasks']
    if mutation == 'cycle':
        second['dependencies'] = [first['id']]
    elif mutation == 'unknown':
        first['dependencies'] = ['unknown']
    elif mutation == 'duplicate_id':
        second['id'] = first['id']
    elif mutation == 'empty_query':
        first['queries'] = [' ']
    else:
        second['queries'] = first['queries']
    with pytest.raises(ValueError):
        Plan.model_validate(data)


def test_replay_is_reproducible(cases):
    a = execute(cases[0], 'adaptive')
    b = execute(cases[0], 'adaptive')
    assert [t['action'] for t in a['transitions']] == [t['action'] for t in b['transitions']]
    assert a['report'] == b['report']
    assert a['metrics']['source_coverage_proxy'] == b['metrics']['source_coverage_proxy']


def test_comparison_artifacts(tmp_path):
    target = tmp_path / 'comparison'
    summary = evaluate(Path('data/planning/suite.json'), target)
    assert len(summary['runs']) == 10
    assert len(summary['suite_sha256']) == 64
    assert (target / 'shared_source/adaptive/trajectory.jsonl').is_file()
    assert (target / 'summary.md').is_file()
    assert all(r['task_quality'] is None for r in summary['runs'])
    with pytest.raises(FileExistsError):
        evaluate(Path('data/planning/suite.json'), target)


@pytest.mark.parametrize('arguments', [
    ['--strategy', 'adaptive'],
    ['--plan', 'data/planning/plan.json'],
    ['different question', '--snapshot', 'data/planning/snapshot.json',
     '--plan', 'data/planning/plan.json', '--strategy', 'adaptive'],
])
def test_cli_rejects_inconsistent_plan(arguments, tmp_path):
    import subprocess
    import sys
    result = subprocess.run(
        [sys.executable, '-m', 'adra.cli', *arguments, '--output', str(tmp_path / 'runs')],
        capture_output=True, text=True,
    )
    assert result.returncode == 2
    assert not (tmp_path / 'runs').exists()
