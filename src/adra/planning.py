"""Validated research brief schema. Tasks describe scope, not execution queues."""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from adra.coverage import Criterion
from adra.domains import DomainPolicy


class SubTask(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    queries: list[str] = Field(min_length=1, max_length=10)
    dependencies: list[str] = Field(default_factory=list)
    target_sources: int = Field(default=1, ge=1, le=10)
    priority: float = Field(default=1, gt=0, le=10)
    criteria: list[Criterion] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def clean_queries(self):
        if len({c.id for c in self.criteria}) != len(self.criteria):
            raise ValueError("Criterion IDs must be unique within each task")
        self.queries = [q.strip() for q in self.queries]
        if any(not q for q in self.queries) or len(set(self.queries)) != len(self.queries):
            raise ValueError("Task queries must be nonempty and unique")
        return self


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    query: str = Field(min_length=1)
    tasks: list[SubTask] = Field(min_length=1, max_length=20)
    allowed_url_prefixes: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def validate_graph(self):
        DomainPolicy(url_prefixes=self.allowed_url_prefixes)
        ids = {t.id for t in self.tasks}
        if len(ids) != len(self.tasks):
            raise ValueError("Task IDs must be unique")
        queries = [q for task in self.tasks for q in task.queries]
        if len(queries) != len(set(queries)):
            raise ValueError("Queries must be unique across tasks; use task-specific queries")
        for task in self.tasks:
            if not set(task.dependencies) <= ids:
                raise ValueError("Unknown task dependency")
        visited = set()
        pending = list(self.tasks)
        while pending:
            ready = [t for t in pending if set(t.dependencies) <= visited]
            if not ready:
                raise ValueError("Task dependencies contain a cycle")
            visited.update(t.id for t in ready)
            pending = [t for t in pending if t not in ready]
        return self
