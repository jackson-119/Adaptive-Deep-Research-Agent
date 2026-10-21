"""Claim-level semantic judgments with mechanically validated source quotations.

These are model judgments, not ground truth. Failed checks remain unevaluated;
never turn missing budget/invalid output into a supported or insufficient claim.
"""
import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from adra.coverage import Citation
from adra.evidence_store import retrieve


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


def verify_report(state, policy):
    records = []
    for index, claim in enumerate(state['report']['claims']):
        record = {'claim_index': index, 'claim': claim, 'verdict': None, 'citations': []}
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
                verdict = ClaimVerdict.model_validate(policy.verify_claim(claim, evidence))
            if verdict.verdict in ('supported', 'contradicted') and not verdict.citations:
                raise ValueError('Supported/contradicted verdict needs a source quote')
            citations = validate_quotes(verdict.citations, evidence, state['raw_documents'])
            record.update(verdict=verdict.verdict, explanation=verdict.explanation,
                          citations=citations, validation='quotes_validated_model_judgment')
        except Exception as exc:
            record.update(validation='assessment_failed', error_type=type(exc).__name__)
        records.append(record)
    complete = all(r['validation'] != 'assessment_failed' for r in records)
    rates = {f'{verdict}_claim_rate': (sum(r['verdict'] == verdict for r in records) / len(records)
                                     if complete and records else None)
             for verdict in ('supported', 'contradicted', 'insufficient')}
    return {'status': 'completed' if complete else 'incomplete', 'claims': records,
            'evaluated_claims': sum(r['verdict'] is not None for r in records),
            'total_claims': len(records), **rates}
