"""Production research graph: brief -> research -> synthesis -> final evaluation.

The researcher owns query selection. Global retrieval owns source selection.
Evaluation never influences research scheduling or supplies the writer's facts.
"""
import time
from copy import deepcopy

from langgraph.graph import END, START, StateGraph

from adra.coverage import refresh_coverage
from adra.domains import DomainPolicy
from adra.evidence_store import add_source, retrieve
from adra.policy import compact_evidence
from adra.provenance import runtime_provenance
from adra.research_models import ResearchDecision
from adra.state import Budget, Report
from adra.tools import canonical_url


def research_queries(query, plan):
    if plan is None:
        return [query]
    return [f"{c.description} {' '.join(c.retrieval_terms)}" for t in plan.tasks
            for c in t.criteria] or [t.description for t in plan.tasks]


def evaluate_report(state, plan, policy):
    # Retrieve independently from the raw corpus for each rubric, only after writing.
    evidence_by_task = {
        t.id: retrieve(state['evidence'], state['raw_documents'],
                       [f"{c.description} {' '.join(c.retrieval_terms)}" for c in t.criteria],
                       char_budget=8000) for t in plan.tasks}
    class FinalJudge:
        def assess_coverage(self, task, evidence):
            return policy.assess_report(task, evidence, state['report'])
    coverage = refresh_coverage(plan, FinalJudge(), {**state, "coverage": {}},
                                evidence_by_task=evidence_by_task)
    complete = all(r['validation'] in ('quotes_validated', 'no_read_evidence')
                   for r in coverage.values())
    return {**state, 'coverage': coverage,
            'evaluation_status': 'completed' if complete else 'incomplete'}


def build_graph(policy, tools, budget, plan=None, allowed_domains=None, evaluate=True):
    domains = DomainPolicy(allowed_domains if allowed_domains is not None
                           else getattr(tools, 'allowed_domains', ()),
                           plan.allowed_url_prefixes if plan else ())
    if hasattr(tools, 'domain_policy'):
        tools.domain_policy = domains

    def brief(state):
        return {**state, 'brief': plan.model_dump() if plan else {'query': state['query']},
                'retrieval_queries': research_queries(state['query'], plan)}

    def context(state):
        return {'query': state['query'], 'brief': state['brief'],
                'allowed_domains': list(domains.domains),
                'allowed_url_prefixes': list(domains.url_prefixes),
                'remaining_tool_budget': budget.tool_calls - state['tool_calls'],
                'remaining_steps': budget.steps - state['steps'],
                'source_catalog': [{k: e[k] for k in ('id', 'url', 'title', 'read', 'content')}
                                   for e in state['evidence']],
                'research_notes': compact_evidence(state.get('notes', [])),
                'history': state['transitions']}

    def controller(state):
        reason = ''
        maximum = getattr(policy, 'max_model_calls', None)
        if state['steps'] >= budget.steps:
            reason = 'step_budget'
        elif state['tool_calls'] >= budget.tool_calls:
            reason = 'tool_budget'
        elif maximum is not None and policy.model_calls >= maximum - 1:
            reason = 'model_budget_reserved_for_writing'
        if reason:
            action = ResearchDecision(kind='FINISH', reason=reason)
        else:
            action = ResearchDecision.model_validate(policy.research(context(state)))
        return {**state, 'action': action.model_dump(),
                'stop_reason': reason or ('researcher_complete' if action.kind == 'FINISH' else '')}

    def execute(state):
        state = deepcopy(state)
        action = ResearchDecision.model_validate(state['action'])
        observation, calls = {}, 0
        started = time.monotonic()
        argument = action.argument
        try:
            if action.kind == 'READ':
                argument = canonical_url(argument)
            duplicate = any(t['action']['kind'] == action.kind and
                            t['action']['argument'] == argument for t in state['transitions'])
            if duplicate:
                observation = {'error': 'duplicate_action'}
            elif action.kind == 'SEARCH':
                calls = 1
                found, rejected = [], 0
                for result in tools.search(argument)[:5]:
                    if not domains.allows(result.get('url', '')):
                        rejected += 1
                        continue
                    is_read = isinstance(result.get('raw_content'), str) and bool(result['raw_content'].strip())
                    source_id = add_source(state['evidence'], state['raw_documents'], result,
                                           read=is_read)
                    if source_id:
                        found.append(source_id)
                observation = {'source_ids': list(dict.fromkeys(found)), 'rejected_sources': rejected}
            elif action.kind == 'READ':
                domains.require(argument)
                item = next((e for e in state['evidence'] if e['url'] == argument), None)
                if item is None:
                    raise ValueError('READ requires a collected URL')
                if item['read']:
                    observation = {'error': 'already_read_use_retrieve'}
                else:
                    calls = 1
                    raw = tools.read(argument)
                    source_id = add_source(state['evidence'], state['raw_documents'],
                                           {**item, 'raw_content': raw}, read=True)
                    if source_id is None:
                        raise ValueError('Empty page content')
                    observation = {'read_id': source_id}
            elif action.kind == 'RETRIEVE':
                observation = {'retrieval_query': argument}
            else:
                raise ValueError('Unsupported research action')
        except Exception as exc:
            observation = {'error': type(exc).__name__}
        state['tool_calls'] += calls
        state['steps'] += 1
        state['transitions'].append({'action': {**action.model_dump(), 'argument': argument},
                                     'observation': observation, 'tool_cost': calls,
                                     'latency_seconds': time.monotonic() - started})
        state['focus'] = action.focus or (argument if action.kind != 'READ' else state['query'])
        return state

    def retrieval(state):
        queries = [state.get('focus', state['query']), *state['retrieval_queries']]
        notes = retrieve(state['evidence'], state['raw_documents'], queries)
        return {**state, 'notes': notes}

    def synthesize(state):
        # The final handoff covers every brief criterion, not only the last focus.
        notes = retrieve(state['evidence'], state['raw_documents'], state['retrieval_queries'])
        if not notes:
            report = Report(claims=[], limitations=['没有取得可引用的已读取原文，未生成事实性结论。'])
        else:
            report = policy.write({'query': state['query'], 'evidence': notes,
                                   'research_scope': {**state['brief'], 'research_stop_reason': state['stop_reason']},
                                   'coverage': {}})
        known = {e['id'] for e in notes}
        if any(not c.evidence_ids or not set(c.evidence_ids) <= known for c in report.claims):
            raise ValueError('Report must cite retrieved read evidence')
        return {**state, 'report': report.model_dump(),
                'research_notes': {'method': 'global_bm25_v1', 'evidence': notes,
                                   'limitations': 'Extractive retrieval; not a semantic verification.'}}

    def final_evaluation(state):
        if not evaluate or plan is None:
            return {**state, 'evaluation_status': 'not_requested', 'coverage': {}}
        return evaluate_report(state, plan, policy)

    graph = StateGraph(dict)
    for name, node in [('brief', brief), ('researcher', controller), ('tools', execute),
                       ('retrieve', retrieval), ('synthesize', synthesize), ('evaluate', final_evaluation)]:
        graph.add_node(name, node)
    graph.add_edge(START, 'brief')
    graph.add_edge('brief', 'researcher')
    graph.add_conditional_edges('researcher', lambda s: 'synthesize'
                               if s['action']['kind'] == 'FINISH' else 'tools')
    graph.add_edge('tools', 'retrieve')
    graph.add_edge('retrieve', 'researcher')
    graph.add_edge('synthesize', 'evaluate')
    graph.add_edge('evaluate', END)
    return graph.compile()


def run(query, policy, tools, budget=None, task_id='research', allowed_domains=None,
        plan=None, evaluate=True):
    if not query.strip():
        raise ValueError('Research query must not be empty')
    budget = budget or Budget()
    started = time.monotonic()
    result = build_graph(policy, tools, budget, plan, allowed_domains, evaluate).invoke(
        {'task_id': task_id, 'query': query.strip(), 'evidence': [], 'raw_documents': {},
         'transitions': [], 'steps': 0, 'tool_calls': 0, 'coverage': {}},
        {'recursion_limit': budget.steps * 3 + 12})
    judgments = [j for r in result['coverage'].values() for j in r['judgments']]
    result['architecture'] = 'global_research_v2'
    result['config'] = {**budget.model_dump(), 'evaluation': evaluate,
                        'allowed_domains': list(allowed_domains or getattr(tools, 'allowed_domains', ()))}
    result['provenance'] = runtime_provenance()
    result['model_call_records'] = list(getattr(policy, 'call_records', []))
    result['metrics'] = {
        'tool_calls': result['tool_calls'], 'steps': result['steps'],
        'source_count': len(result['evidence']),
        'read_source_count': sum(e['read'] for e in result['evidence']),
        'tool_errors': sum('error' in t['observation'] for t in result['transitions']),
        'rejected_sources': sum(t['observation'].get('rejected_sources', 0) for t in result['transitions']),
        'latency_seconds': time.monotonic() - started,
        'total_tokens': getattr(policy, 'tokens', None),
        'token_usage_complete': getattr(policy, 'usage_complete', False),
        'model_calls': getattr(policy, 'model_calls', 0),
        'citation_reference_validity': 1.0 if result['report']['claims'] else None,
        'citation_support': None, 'task_quality': None,
        'semantic_coverage': (sum(j['status'] == 'supported' for j in judgments) / len(judgments)
                              if judgments and result['evaluation_status'] == 'completed' else None),
        'evaluation_status': result['evaluation_status'],
        'coverage_assessment_failures': sum(r['validation'] == 'assessment_failed'
                                            for r in result['coverage'].values()),
        'phase_tokens': {phase: (sum(r['tokens'] for r in result['model_call_records']
                                    if r['phase'] == phase)
                                  if all(r['tokens'] is not None for r in result['model_call_records']
                                         if r['phase'] == phase) else None)
                         for phase in {r['phase'] for r in result['model_call_records']}},
    }
    if plan:
        result['plan'] = plan.model_dump()
    return result
