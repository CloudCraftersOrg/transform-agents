from __future__ import annotations

import re
from collections.abc import Callable
from typing import Protocol


class ModelLike(Protocol):
    def complete(self, prompt: str, *, system: str | None = None) -> str: ...


class FakeModel:
    """Deterministic model for tests. `responses` is a list consumed in order (the last element
    repeats) or a callable over the prompt. Records the prompts it receives."""

    def __init__(self, responses: list[str] | Callable[[str], str]) -> None:
        self._responses = responses
        self._i = 0
        self.prompts: list[str] = []

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        self.prompts.append(prompt)
        if callable(self._responses):
            return self._responses(prompt)
        r = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return r


class StrandsModel:
    """Adapter over strands + Bedrock. Not exercised without credentials. Agents are cached per
    system prompt so repeated calls with the same system reuse one Agent."""

    def __init__(
        self, model_id: str, region: str = "us-east-1", *, guardrail_id: str | None = None
    ) -> None:
        self._model_id = model_id
        self._region = region
        self._guardrail_id = guardrail_id
        self._agents: dict[str, object] = {}

    def _agent(self, system: str):
        if system not in self._agents:
            try:
                from strands import Agent
                from strands.models import BedrockModel
            except ModuleNotFoundError as e:
                raise RuntimeError("install the 'agents' extra to use StrandsModel") from e

            kwargs: dict = {"model_id": self._model_id, "region_name": self._region}
            if self._guardrail_id:
                kwargs["guardrail_id"] = self._guardrail_id
            self._agents[system] = Agent(model=BedrockModel(**kwargs), system_prompt=system)
        return self._agents[system]

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        return str(self._agent(system or "")(prompt))


class BedrockModel:
    """Adapter over the bedrock-runtime Converse API. No strands dependency; pass a client to test."""

    def __init__(
        self,
        model_id: str,
        region: str = "us-east-1",
        *,
        guardrail_id: str | None = None,
        max_tokens: int = 2048,
        client=None,
    ) -> None:
        self._model_id = model_id
        self._region = region
        self._guardrail_id = guardrail_id
        self._max_tokens = max_tokens
        self._client = client

    @property
    def client(self):
        if self._client is None:
            import boto3

            self._client = boto3.client("bedrock-runtime", region_name=self._region)
        return self._client

    def complete(self, prompt: str, *, system: str | None = None) -> str:
        kwargs: dict = {
            "modelId": self._model_id,
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": self._max_tokens, "temperature": 0},
        }
        if system:
            kwargs["system"] = [{"text": system}]
        if self._guardrail_id:
            kwargs["guardrailConfig"] = {
                "guardrailIdentifier": self._guardrail_id,
                "guardrailVersion": "DRAFT",
            }
        resp = self.client.converse(**kwargs)
        return resp["output"]["message"]["content"][0]["text"]


def strip_code_fence(text: str) -> str:
    m = re.match(r"^```[a-zA-Z0-9]*\s*(.*?)\s*```$", text.strip(), re.DOTALL)
    return m.group(1).strip() if m else text.strip()
