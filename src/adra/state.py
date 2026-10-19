from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["SEARCH", "READ", "STOP"]
    argument: str = ""
    task_id: str | None = None

    @model_validator(mode="after")
    def validate_argument(self):
        self.argument = self.argument.strip()
        if self.kind != "STOP" and not self.argument:
            raise ValueError("SEARCH/READ require a nonempty argument")
        return self


class Evidence(BaseModel):
    id: str
    title: str
    url: str
    content: str
    read: bool = False
    task_ids: list[str] = Field(default_factory=list)
    original_length: int = 0
    selected_length: int = 0
    truncated: bool = False
    raw_sha256: str = ""
    raw_kind: Literal["search", "read"] = "search"
    selection_method: str = ""
    selection_focus: list[dict] = Field(default_factory=list)
    passages: list[dict] = Field(default_factory=list)


class Claim(BaseModel):
    text: str
    evidence_ids: list[str] = Field(default_factory=list)


class Report(BaseModel):
    claims: list[Claim]
    limitations: list[str] = Field(default_factory=list)


class Budget(BaseModel):
    tool_calls: int = Field(default=6, ge=0, le=100)
    steps: int = Field(default=10, ge=1, le=200)


class AgentState(TypedDict):
    task_id: str
    query: str
    evidence: list[dict]
    action: dict
    tool_calls: int
    steps: int
    transitions: list[dict]
    stop_reason: str
    report: dict
    planning: dict
    allowed_domains: list[str]
    raw_documents: dict[str, dict[str, str]]
    coverage: dict
