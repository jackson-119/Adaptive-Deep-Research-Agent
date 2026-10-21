"""Global, query-time BM25 retrieval over immutable downloaded source text.

Discovery task IDs are provenance, never an access filter. Every result retains
character offsets and a digest so quotations can be checked against raw text.
"""
import hashlib
import math
from collections import Counter

from adra.passages import terms
from adra.state import Evidence
from adra.tools import canonical_url


def add_source(evidence, documents, result, task_id=None, read=False):
    url = canonical_url(result['url'])
    text = result.get('raw_content') if read else result.get('content')
    if not isinstance(text, str) or not text.strip():
        return None
    source = next((e for e in evidence if e['url'] == url), None)
    if source is None:
        source = Evidence(id=f'E{len(evidence) + 1}', title=result.get('title') or url,
                          url=url, content='').model_dump()
        evidence.append(source)
    if task_id and task_id not in source['task_ids']:
        source['task_ids'].append(task_id)
    kind = 'read' if read else 'search'
    documents.setdefault(source['id'], {})[kind] = text
    # Later search snippets must never overwrite a full read.
    if read or not source['read']:
        source.update(read=read, raw_kind=kind, original_length=len(text),
                      raw_sha256=hashlib.sha256(text.encode()).hexdigest(),
                      content=text[:500], selected_length=min(500, len(text)),
                      truncated=len(text) > 500, passages=[], selection_method='catalog_preview')
    return source['id']


def retrieve(evidence, documents, queries, char_budget=12000, top_k=4):
    """Fairly allocate chunks across queries, not discovery tasks or source counts.

    Overlapping spans retain surrounding context. BM25 here uses
    token sets from the project's bilingual tokenizer (binary term frequency).
    No embedding service is required; relevance is not a support judgment.
    """
    candidates = []
    for source in evidence:
        raw = documents.get(source['id'], {}).get('read')
        if not source['read'] or not raw:
            continue
        if hashlib.sha256(raw.encode()).hexdigest() != source['raw_sha256']:
            raise ValueError('Stored document digest mismatch')
        # Include surrounding paragraphs instead of discarding context at blank lines.
        for start in range(0, len(raw), 900):
            end = min(len(raw), start + 1200)
            chunk = raw[start:end]
            candidates.append((source, start, end, chunk, terms(chunk)))
    if not candidates:
        return []
    df = Counter(word for *_, words in candidates for word in words)
    avg_len = sum(len(c[-1]) for c in candidates) / len(candidates)
    rankings = []
    for query in queries:
        query_terms = terms(query)
        def score(candidate):
            words = candidate[-1]
            length_norm = 1 + 1.2 * (0.25 + 0.75 * len(words) / max(1, avg_len))
            return sum(math.log(1 + (len(candidates) - df[w] + 0.5) / (df[w] + 0.5))
                       * 2.2 / length_norm for w in words & query_terms)
        ranked = sorted(enumerate(candidates), key=lambda pair: (-score(pair[1]), pair[0]))
        rankings.append([i for i, c in ranked if score(c) > 0][:top_k])
    selected, used = [], 0
    for rank in range(top_k):
        for ranking in rankings:
            if rank >= len(ranking):
                continue
            index = ranking[rank]
            source, start, end, text, _ = candidates[index]
            if index in selected or used + len(text) + 2 > char_budget:
                continue
            if any(candidates[i][0]['id'] == source['id'] and
                   start < candidates[i][2] and candidates[i][1] < end for i in selected):
                continue
            selected.append(index)
            used += len(text) + 2
    output = {}
    for i in selected:
        source, start, end, text, _ = candidates[i]
        item = output.setdefault(source['id'], {**source, 'content': '', 'passages': [],
                                               'selection_method': 'global_bm25_v1'})
        item['passages'].append({'start': start, 'end': end, 'text': text})
    for item in output.values():
        item['passages'].sort(key=lambda p: p['start'])
        item['content'] = '\n\n'.join(p['text'] for p in item['passages'])
        item['selected_length'] = len(item['content'])
        item['truncated'] = sum(p['end'] - p['start'] for p in item['passages']) < item['original_length']
    return list(output.values())
