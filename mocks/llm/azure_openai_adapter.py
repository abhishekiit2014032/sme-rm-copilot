"""Diagram box: Primary / fallback LLM (lane B), the Azure OpenAI adapter.
UNTESTED STUB. Shows where a regional Azure OpenAI deployment plugs in behind the same LlmClient port.
Selected only when COPILOT_LLM=azure; needs the `openai` package and AZURE_OPENAI_* settings, none of which ship here.
"""
from __future__ import annotations

import os
from typing import Any

from copilot.contracts.ports import LlmCompletion, RenderedPrompt


class AzureOpenAIAdapter:  # pragma: no cover - not exercised by tests or demos
    """Maps model gateway tiers to Azure OpenAI deployment names. Never used in this prototype."""

    def __init__(self) -> None:
        from openai import AzureOpenAI  # imported lazily: not a dependency of the prototype

        self._client = AzureOpenAI(
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],   # regional endpoint, never a global one
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            azure_ad_token_provider=None,  # production: managed identity token provider, no API keys
        )

    def complete(self, model: str, prompt: RenderedPrompt, params: dict[str, Any]) -> LlmCompletion:
        response = self._client.chat.completions.create(
            model=model,  # deployment name for the tier
            messages=[{"role": "system", "content": prompt.system}, {"role": "user", "content": prompt.user}],
            temperature=params.get("temperature", 0.1),
            top_p=params.get("top_p", 0.9),
            max_tokens=params.get("max_tokens", 800),
            response_format={"type": "json_object"},
        )
        usage = response.usage
        return LlmCompletion(text=response.choices[0].message.content or "",
                             tokens_in=usage.prompt_tokens, tokens_out=usage.completion_tokens)

    def embed(self, model: str, text: str) -> list[float]:
        return self._client.embeddings.create(model=model, input=text).data[0].embedding
