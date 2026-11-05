"""Claim-level semantic judgments with mechanically validated source quotations.

These are model judgments, not ground truth. Failed checks remain unevaluated;
never turn missing budget/invalid output into a supported or insufficient claim.
"""
import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from adra.coverage import Citation
from adra.evaluation_retry import retryable
from adra.evidence_store import retrieve

# Only constant local validation messages may enter artifacts; never log provider text.
QUOTE_ERROR_REASONS = {
    'Source digest mismatch': 'source_digest_mismatch',
    'Passage does not match raw text': 'passage_raw_mismatch',
    'Quote absent from supplied passages': 'quote_not_in_selected_passages',
    'Supported/contradicted verdict needs a source quote': 'missing_required_quote',
}


class ClaimVerdict(BaseModel):
    model_config = ConfigDict(extra='forbid')
    verdict: Literal['supported', 'contradicted', 'insufficient']
    explanation: str = Field(min_length=1)
    citations: list[Citation] = Field(default_factory=list)


def validate_quotes(citations, evidence, documents):
    sources = {e['id']: e for e in evidence if e['read']}
    output = []
    for citation in citations:
        source = sources[citation.evidence_id]
        raw = documents[source['id']]['read']
        if hashlib.sha256(raw.encode()).hexdigest() != source['raw_sha256']:
            raise ValueError('Source digest mismatch')
        for passage in source['passages']:
            if raw[passage['start']:passage['end']] != passage['text']:
                raise ValueError('Passage does not match raw text')
            offset = passage['text'].find(citation.quote)
            if offset >= 0:
                start = passage['start'] + offset
                output.append({**citation.model_dump(), 'start': start,
                               'end': start + len(citation.quote), 'raw_sha256': source['raw_sha256']})
                break
        else:
            raise ValueError('Quote absent from supplied passages')
    return output


def verify_report(state, policy, max_attempts=2):
    records = []
    for index, claim in enumerate(state['report']['claims']):
        record = {'claim_index': index, 'claim': claim, 'verdict': None, 'citations': []}
        attempts = []
        feedback = None
        for attempt in range(1, max_attempts + 1):
            stage = 'evidence_selection'
            try:
                allowed = [e for e in state['evidence'] if e['id'] in claim['evidence_ids']]
                # Only the sources this claim actually cites may support it.
                evidence = retrieve(allowed, state['raw_documents'], [claim['text']], char_budget=6000)
                if not evidence:
                    # Cross-language lexical misses must still expose the writer's actual evidence.
                    remaining = 6000
                    evidence = []
                    for source in state.get('research_notes', {}).get('evidence', []):
                        if source['id'] not in claim['evidence_ids']:
                            continue
                        passages = []
                        for passage in source['passages']:
                            text = passage['text'][:remaining]
                            if text:
                                passages.append({**passage, 'text': text, 'end': passage['start'] + len(text)})
                                remaining -= len(text)
                        if passages:
                            evidence.append({**source, 'passages': passages, 'truncated': True})
                if not evidence:
                    verdict = ClaimVerdict(verdict='insufficient', explanation='No retrieved read evidence in cited sources.')
                else:
                    stage = 'model_request'
                    if feedback and hasattr(policy, 'repair_claim_verification'):
                        response = policy.repair_claim_verification(claim, evidence, feedback)
                    else:
                        response = policy.verify_claim(claim, evidence)
                    verdict = ClaimVerdict.model_validate(response)
                stage = 'quote_validation'
                if verdict.verdict in ('supported', 'contradicted') and not verdict.citations:
                    raise ValueError('Supported/contradicted verdict needs a source quote')
                citations = validate_quotes(verdict.citations, evidence, state['raw_documents'])
                record.pop('error_type', None)
                record.pop('error_reason', None)
                record.update(verdict=verdict.verdict, explanation=verdict.explanation,
                              citations=citations, validation='quotes_validated_model_judgment')
                attempts.append({'attempt': attempt, 'status': 'ok'})
                break
            except Exception as exc:
                feedback = None
                record.update(validation='assessment_failed', error_type=type(exc).__name__)
                attempts.append({'attempt': attempt, 'status': 'failed', 'stage': stage, 'error_type': type(exc).__name__})
                if stage == 'quote_validation':
                    reason = QUOTE_ERROR_REASONS.get(str(exc), 'unclassified_quote_validation_error')
                    record['error_reason'] = reason
                    attempts[-1]['error_reason'] = reason
                    if reason in ('quote_not_in_selected_passages', 'missing_required_quote'):
                        feedback = reason
                if not retryable(exc):
                    break
        record['attempts'] = attempts
        records.append(record)
    complete = all(r['validation'] != 'assessment_failed' for r in records)
    rates = {f'{verdict}_claim_rate': (sum(r['verdict'] == verdict for r in records) / len(records)
                                     if complete and records else None)
             for verdict in ('supported', 'contradicted', 'insufficient')}
    return {'status': 'completed' if complete else 'incomplete', 'claims': records,
            'evaluated_claims': sum(r['verdict'] is not None for r in records),
            'total_claims': len(records), **rates}
