"""Task acceptance checks with exact quotation validation; not an independent quality judge."""

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal


class Criterion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    retrieval_terms: list[str] = Field(default_factory=list, max_length=20)


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str
    quote: str = Field(min_length=1)


class Judgment(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    criterion_id: str
    status: Literal["supported", "partial", "missing"]
    citations: list[Citation] = Field(default_factory=list)
    reason: str = Field(min_length=1)


class Assessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    judgments: list[Judgment]


def assessment_key(task, evidence):
    # Revalidate defaults too, so priority=1 and JSON-loaded priority=1.0 hash identically.
    normalized = type(task).model_validate(task.model_dump()).model_dump()
    payload = {"task": normalized, "evidence": evidence}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class ReplayAssessor:
    def __init__(self, data):
        self.data = data

    def assess_coverage(self, task, evidence):
        # Exact input match; never substitute an unrelated judgment.
        return Assessment.model_validate(self.data[assessment_key(task, evidence)])


def refresh_coverage(plan, assessor, state):
    coverage = {}
    for task in plan.tasks:
        evidence = [e for e in state["evidence"] if e["read"] and task.id in e["task_ids"]]
        key = assessment_key(task, evidence)
        previous = state.get("coverage", {}).get(task.id)
        if previous and previous["input_sha256"] == key:
            coverage[task.id] = previous
            continue
        missing = [{"criterion_id": c.id, "description": c.description, "status": "missing",
                    "citations": [], "reason": "No validated supporting assessment yet."}
                   for c in task.criteria]
        record = {"input_sha256": key, "judgments": missing, "validation": "no_read_evidence"}
        if evidence:
            try:
                assessment = assessor.assess_coverage(task, evidence)
                assessment = Assessment.model_validate(assessment.model_dump())
                ids = [j.criterion_id for j in assessment.judgments]
                if len(ids) != len(set(ids)) or set(ids) != {c.id for c in task.criteria}:
                    raise ValueError("Assessment must cover exactly the task criteria")
                sources = {e["id"]: e for e in evidence}
                judgments = []
                for judgment in assessment.judgments:
                    if ((judgment.status == "missing" and judgment.citations) or
                            (judgment.status != "missing" and not judgment.citations)):
                        raise ValueError("Support judgments require citations; missing has none")
                    citations = []
                    for citation in judgment.citations:
                        source = sources[citation.evidence_id]
                        raw = state["raw_documents"][source["id"]]["read"]
                        location = None
                        for passage in source["passages"]:
                            if raw[passage["start"]:passage["end"]] != passage["text"]:
                                raise ValueError("Passage does not match original text")
                            offset = passage["text"].find(citation.quote)
                            if offset >= 0:
                                location = passage["start"] + offset
                                break
                        if location is None:
                            raise ValueError("Quote must exist within one selected passage")
                        citations.append({**citation.model_dump(), "start": location,
                                          "end": location + len(citation.quote),
                                          "raw_sha256": source["raw_sha256"]})
                    description = next(c.description for c in task.criteria
                                       if c.id == judgment.criterion_id)
                    judgments.append({**judgment.model_dump(), "description": description,
                                      "citations": citations})
                record.update(judgments=judgments, validation="quotes_validated")
            except Exception as exc:
                # Fail closed, preserve only safe error type, and cache until evidence changes.
                record.update(validation="assessment_failed", error_type=type(exc).__name__)
        coverage[task.id] = record
    return coverage


def research_notes(state):
    """Extractive handoff: validated quotations only, without another model call.

    This is evidence compression, not independent semantic verification. Keep
    partial/missing judgments so the writer cannot mistake notes for completion.
    """
    sources = {e["id"]: e for e in state["evidence"]}
    selected = {}
    checks = []
    for task_id, record in state.get("coverage", {}).items():
        for judgment in record["judgments"]:
            checks.append({"task_id": task_id, **{
                k: judgment[k] for k in ("criterion_id", "description", "status", "reason")
                if k in judgment}})
            if record.get("validation") != "quotes_validated":
                continue
            for citation in judgment.get("citations", []):
                source = sources.get(citation["evidence_id"])
                if not source or not source["read"]:
                    continue
                start, end = citation["start"], citation["end"]
                if (citation["raw_sha256"] != source["raw_sha256"] or
                        not any(p["start"] <= start < end <= p["end"] and
                                p["text"][start - p["start"]:end - p["start"]] == citation["quote"]
                                for p in source["passages"])):
                    continue
                entry = selected.setdefault(source["id"], {
                    k: source[k] for k in ("id", "title", "url", "read", "raw_sha256")})
                passages = entry.setdefault("passages", [])
                passage = {"start": start, "end": end, "text": citation["quote"]}
                if passage not in passages:
                    passages.append(passage)
    return {"method": "validated_quotes_v1", "evidence": list(selected.values()),
            "acceptance_checks": checks}
