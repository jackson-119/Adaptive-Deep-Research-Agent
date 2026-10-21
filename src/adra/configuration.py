"""Non-secret, validated model routing and request limits."""

import os

from pydantic import BaseModel, Field


class RuntimeConfig(BaseModel):
    model: str = Field(min_length=1)
    planning_model: str | None = None
    coverage_model: str | None = None
    writing_model: str | None = None
    reflection_model: str | None = None
    verification_model: str | None = None
    max_model_calls: int = Field(default=32, ge=1, le=1000)
    max_output_tokens: int = Field(default=2048, ge=128, le=32768)
    timeout_seconds: float = Field(default=60, gt=0, le=600)

    @classmethod
    def from_env(cls, max_model_calls=32):
        return cls(model=os.environ.get("ADRA_MODEL", ""),
                   planning_model=os.environ.get("ADRA_PLANNING_MODEL") or None,
                   coverage_model=os.environ.get("ADRA_COVERAGE_MODEL") or None,
                   writing_model=os.environ.get("ADRA_WRITING_MODEL") or None,
                   reflection_model=os.environ.get("ADRA_REFLECTION_MODEL") or None,
                   verification_model=os.environ.get("ADRA_VERIFICATION_MODEL") or None,
                   max_model_calls=max_model_calls)

    def create_policy(self):
        from langchain.chat_models import init_chat_model
        from adra.policy import ModelPolicy
        names = {"decision": self.model, "planning": self.planning_model or self.model,
                 "coverage": self.coverage_model or self.model,
                 "writing": self.writing_model or self.model,
                 "reflection": self.reflection_model or self.model,
                 "verification": self.verification_model or self.coverage_model or self.model}
        models = {name: init_chat_model(name, temperature=0, max_tokens=self.max_output_tokens,
                                       timeout=self.timeout_seconds, max_retries=0)
                  for name in dict.fromkeys(names.values())}
        return ModelPolicy(models[self.model], phase_models={
            phase: models[name] for phase, name in names.items()},
            max_model_calls=self.max_model_calls, model_names=names)
