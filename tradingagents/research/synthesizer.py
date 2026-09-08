"""LLM adapter for research structured output. Tests inject a stub synthesizer."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel

from tradingagents.agents.utils.structured import bind_structured


class ResearchSynthesizer(Protocol):
    def generate(self, prompt: str, schema: type[BaseModel]) -> BaseModel: ...


class CallableSynthesizer:
    def __init__(self, fn: Callable[[str, type[BaseModel]], BaseModel | dict[str, Any]]):
        self._fn = fn

    def generate(self, prompt: str, schema: type[BaseModel]) -> BaseModel:
        result = self._fn(prompt, schema)
        if isinstance(result, schema):
            return result
        if isinstance(result, BaseModel):
            return schema.model_validate(result.model_dump())
        return schema.model_validate(result)


class LangChainSynthesizer:
    def __init__(self, llm: Any, agent_name: str = "research"):
        self.llm = llm
        self.agent_name = agent_name

    def generate(self, prompt: str, schema: type[BaseModel]) -> BaseModel:
        bound = bind_structured(self.llm, schema, self.agent_name)
        if bound is None:
            raise RuntimeError(f"{self.agent_name}: structured output is required")
        result = bound.invoke(prompt)
        if result is None:
            raise RuntimeError(f"{self.agent_name}: structured output returned nothing")
        if isinstance(result, schema):
            return result
        return schema.model_validate(result)
