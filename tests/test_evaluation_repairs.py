from adra.coverage import Assessment
from adra.graph import evaluate_report, run
from adra.verification import verify_report, ClaimVerdict
from adra.evaluation_retry import evaluation_identity
from adra.evidence_store import handoff_evidence
from adra.state import Budget
from test_global_research import Researcher, tools, brief, TEXT
from test_review_changes import claim_state


def test_failed_claim_retries_but_valid_insufficient_does_not():
    class Judge:
        calls = 0
        def verify_claim(self, claim, evidence):
            self.calls += 1
            if self.calls == 1:
                raise ValueError('private response')
            return ClaimVerdict(verdict='insufficient', explanation='gap')
    judge = Judge()
    result = verify_report(claim_state(), judge)
    assert judge.calls == 2
    assert result['status'] == 'completed'
    assert len(result['claims'][0]['attempts']) == 2
    assert 'private response' not in str(result)
    verify_report(claim_state(), judge)
    assert judge.calls == 3


def test_coverage_retries_only_failed_task():
    policy = Researcher([{'kind': 'SEARCH', 'argument': 'new query outside fixed plan'}, {'kind': 'FINISH'}])
    state = run(brief().query, policy, tools(), Budget(tool_calls=3), plan=brief(), evaluate=False)
    class Judge:
        counts = {}
        def assess_report(self, task, evidence, report):
            self.counts[task.id] = self.counts.get(task.id, 0) + 1
            if task.id == 'schema' and self.counts[task.id] == 1:
                raise ValueError('invalid response')
            return Assessment(judgments=[{'criterion_id': c.id, 'status': 'missing',
                'reason': 'gap', 'citations': []} for c in task.criteria])
    judge = Judge()
    result = evaluate_report(state, brief(), judge)
    assert judge.counts == {'schema': 2, 'reducer': 1}
    assert result['evaluation_status'] == 'completed'
    assert evaluation_identity(state)['input_sha256'] != evaluation_identity({**state, 'report': {}})['input_sha256']


def test_retrieve_argument_wins_and_repeated_text_is_not_new():
    policy = Researcher([{'kind': 'SEARCH', 'argument': 'new query outside fixed plan'},
                         {'kind': 'RETRIEVE', 'argument': 'Reducers', 'focus': 'unrelated'},
                         {'kind': 'FINISH'}])
    result = run(brief().query, policy, tools(), Budget(tool_calls=3), plan=brief(), evaluate=False)
    observation = result['transitions'][1]['observation']
    assert observation['retrieval_queries'][0] == 'Reducers'
    assert observation['new_characters'] == 0
    assert not result['transitions'][1]['new_evidence']


def test_handoff_preserves_whole_discovered_passage():
    state = claim_state()
    source = state['evidence'][0]
    raw = state['raw_documents'][source['id']]['read']
    earlier = {**source, 'passages': [{'start': 0, 'end': len(raw), 'text': raw}]}
    later = {**source, 'passages': [{'start': 0, 'end': 5, 'text': raw[:5]}]}
    notes = handoff_evidence(state['evidence'], state['raw_documents'], [[earlier], [later]])
    assert notes[0]['content'] == raw
    stale = {**earlier, 'raw_sha256': 'stale'}
    assert handoff_evidence(state['evidence'], state['raw_documents'], [[stale]]) == []
