"""Global, query-time BM25 retrieval over immutable downloaded source text.

Discovery task IDs are provenance, never an access filter. Every result retains
character offsets and a digest so quotations can be checked against raw text.
"""
import hashlib
import math
import re
from collections import Counter

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


def tokenize(text):
    """Preserve occurrences: ASCII words/API identifiers and overlapping CJK bigrams."""
    tokens = re.findall(r"[a-z0-9_]+", text.lower())
    for span in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.extend(span[i:i + 2] for i in range(max(1, len(span) - 1)))
    return tokens


def bm25_scores(corpus, query, k1=1.2, b=0.75):
    """Okapi TF saturation/length normalization with Lucene's positive IDF.

    Corpus entries and query are token sequences; document length counts tokens,
    not distinct terms. Query terms are deduplicated (no query-TF weighting).
    """
    if not corpus:
        return []
    counts = [Counter(tokens) for tokens in corpus]
    df = Counter(word for count in counts for word in count)
    avgdl = sum(map(len, corpus)) / len(corpus)
    scores = []
    for tokens, count in zip(corpus, counts):
        value = 0.0
        for word in set(query):
            tf = count[word]
            if not tf:
                continue
            idf = math.log(1 + (len(corpus) - df[word] + 0.5) / (df[word] + 0.5))
            value += idf * tf * (k1 + 1) / (
                tf + k1 * (1 - b + b * len(tokens) / max(avgdl, 1)))
        scores.append(value)
    return scores


def merge_spans(spans):
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def retrieve(evidence, documents, queries, char_budget=12000, top_k=4):
    """Round-robin query allocation, merging overlapping relevant spans before charging space."""
    if char_budget < 0 or top_k < 1:
        raise ValueError('Invalid retrieval budget')
    candidates = []
    for source in evidence:
        raw = documents.get(source['id'], {}).get('read')
        if not source['read'] or not raw:
            continue
        if hashlib.sha256(raw.encode()).hexdigest() != source['raw_sha256']:
            raise ValueError('Stored document digest mismatch')
        for start in range(0, len(raw), 900):
            end = min(len(raw), start + 1200)
            candidates.append((source, start, end, tokenize(raw[start:end])))
    if not candidates:
        return []
    rankings = []
    for query in queries:
        scores = bm25_scores([c[-1] for c in candidates], tokenize(query))
        ranked = sorted(range(len(candidates)), key=lambda i: (-scores[i], i))
        rankings.append([i for i in ranked if scores[i] > 0][:top_k])
    selected = {}
    for rank in range(top_k):
        for ranking in rankings:
            if rank >= len(ranking):
                continue
            source, start, end, _ = candidates[ranking[rank]]
            trial = {**selected, source['id']: merge_spans(
                [*selected.get(source['id'], []), (start, end)])}
            size = sum(sum(end - start for start, end in spans) + 2 * (len(spans) - 1)
                       for spans in trial.values())
            if size <= char_budget:
                selected = trial
    output = []
    for source in evidence:
        spans = selected.get(source['id'])
        if not spans:
            continue
        raw = documents[source['id']]['read']
        passages = [{'start': start, 'end': end, 'text': raw[start:end]} for start, end in spans]
        content = '\n\n'.join(p['text'] for p in passages)
        output.append({**source, 'content': content, 'passages': passages,
                       'selection_method': 'bm25_tf_v2', 'selected_length': len(content),
                       'truncated': sum(end - start for start, end in spans) < len(raw)})
    return output


def handoff_evidence(evidence, documents, batches, char_budget=12000, queries=()):
    """Round-robin exact passages across research steps; reject stale snapshots.

    A bounded handoff cannot retain every passage. Keep whole passages rather
    than cutting a previously discovered statement at a new chunk boundary.
    """
    sources = {e['id']: e for e in evidence if e['read']}
    queues = [[(e, p) for e in batch for p in e['passages']] for batch in batches]
    if queries:
        candidates = list({(e['id'], e['raw_sha256'], p['start'], p['end']): (e, p)
                           for queue in queues for e, p in queue}.values())
        corpus = [tokenize(p['text']) for _, p in candidates]
        queues = []
        for query in queries:
            scores = bm25_scores(corpus, tokenize(query))
            queues.append([candidates[i] for i in sorted(range(len(candidates)),
                           key=lambda i: -scores[i]) if scores[i] > 0])
    selected = {}
    for rank in range(max(map(len, queues), default=0)):
        for queue in queues:
            if rank >= len(queue):
                continue
            source, passage = queue[rank]
            sid = source['id']
            current = sources.get(sid)
            if not current or current['raw_sha256'] != source['raw_sha256']:
                continue
            raw = documents[sid]['read']
            a, b = passage['start'], passage['end']
            if raw[a:b] != passage['text']:
                continue
            trial = {**selected, sid: merge_spans([*selected.get(sid, []), (a, b)])}
            if sum(sum(b-a for a,b in spans) + 2*(len(spans)-1) for spans in trial.values()) <= char_budget:
                selected = trial
    output = []
    for sid, spans in selected.items():
        raw = documents[sid]['read']
        passages = [{'start': a, 'end': b, 'text': raw[a:b]} for a,b in spans]
        content = '\n\n'.join(p['text'] for p in passages)
        output.append({**sources[sid], 'passages': passages, 'content': content,
                       'selected_length': len(content), 'truncated': len(content) < len(raw),
                       'selection_method': 'research_handoff_v2'})
    return output
