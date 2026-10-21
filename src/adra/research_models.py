"""Contracts for the new research controller; no per-task action scheduler."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ResearchDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['SEARCH', 'READ', 'RETRIEVE', 'FINISH']
    argument: str = Field(default='', max_length=2000)
    focus: str = Field(default='', max_length=2000)
    reason: str = Field(min_length=1, max_length=2000)
    suspected_conflict: bool = False

    @model_validator(mode='after')
    def check_argument(self):
        self.argument = self.argument.strip()
        if self.kind != 'FINISH' and not self.argument:
            raise ValueError('Research actions require an argument')
        return self
