"""Deterministic lexical passage selection with offsets into the returned tool text."""

import hashlib
import re

CONTENT_LIMIT = 6000
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 100


def terms(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9_]+", text.lower()))
    for span in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.update(span[i:i + 2] for i in range(max(1, len(span) - 1)))
    return tokens


def select_passages(text: str, focuses: list[dict], limit: int = CONTENT_LIMIT) -> dict:
    """Rank all paragraphs, then allocate relevant excerpts across task focuses.

    Offsets are Python character offsets, end-exclusive, into the exact input.
    Lexical matching is an auditable retrieval heuristic, not a semantic judge.
    """
    if limit < CHUNK_SIZE:
        raise ValueError("Passage budget must fit at least one chunk")
    focus_terms = [terms(f["query"]) for f in focuses]
    candidates = []
    for paragraph, match in enumerate(re.finditer(r"\S.*?(?=\r?\n[ \t]*\r?\n|\Z)", text, re.S)):
        start, end = match.start(), match.start() + len(match.group().rstrip())
        cursor = start
        while cursor < end:
            stop = min(cursor + CHUNK_SIZE, end)
            snippet = text[cursor:stop]
            words = terms(snippet)
            scores = [len(words & query) / max(1, len(query)) for query in focus_terms]
            candidates.append({"start": cursor, "end": stop, "paragraph_index": paragraph,
                               "text": snippet, "scores": scores})
            if stop == end:
                break
            cursor = stop - CHUNK_OVERLAP

    selected = []
    used = 0

    def add(candidate):
        nonlocal used
        if any(candidate["start"] < s["end"] and s["start"] < candidate["end"]
               for s in selected):
            return False
        size = len(candidate["text"]) + (2 if selected else 0)
        if used + size > limit:
            return False
        selected.append(candidate)
        used += size
        return True

    # Short input is preserved verbatim, including whitespace and paragraph gaps.
    if len(text) <= limit:
        selected = [{"start": 0, "end": len(text), "paragraph_index": None,
                     "text": text, "scores": [0.0] * len(focuses)}] if text else []
    else:
        for i in range(len(focuses)):
            for candidate in sorted(candidates, key=lambda c: (-c["scores"][i], c["start"])):
                if candidate["scores"][i] > 0 and add(candidate):
                    break
        for candidate in sorted(candidates, key=lambda c: (-max(c["scores"], default=0),
                                                            c["start"])):
            add(candidate)
        selected.sort(key=lambda c: c["start"])

    passages = [{"start": c["start"], "end": c["end"],
                 "paragraph_index": c["paragraph_index"], "text": c["text"],
                 "score": max(c["scores"], default=0),
                 "matched_task_ids": [f["task_id"] for f, score in zip(focuses, c["scores"])
                                      if score > 0 and f.get("task_id")]}
                for c in selected]
    content = "\n\n".join(p["text"] for p in passages)
    return {"content": content, "original_length": len(text), "selected_length": len(content),
            "truncated": sum(p["end"] - p["start"] for p in passages) < len(text),
            "raw_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "selection_method": "lexical_passages_v1", "selection_focus": focuses,
            "passages": passages}
