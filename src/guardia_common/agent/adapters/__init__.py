"""Provider adapters. `build_client` chooses one by the environment's `agent_provider`."""

from __future__ import annotations

from typing import Any

from guardia_common.agent.models import AgentConfigError, ModelClient

AZURE_OPENAI = "azure_openai"
BEDROCK = "bedrock"
PROVIDERS = (AZURE_OPENAI, BEDROCK)


def build_client(
    provider: str | None,
    *,
    secrets: Any = None,
    secret_id: str | None = None,
    bedrock_runtime: Any = None,
    model_id: str | None = None,
) -> ModelClient:
    if provider == AZURE_OPENAI:
        from .azure_openai import AzureOpenAIClient, load_settings

        if secrets is None or not secret_id:
            raise AgentConfigError("Azure OpenAI needs the secret client and the secret id")
        return AzureOpenAIClient(load_settings(secrets, secret_id))
    if provider == BEDROCK:
        from .bedrock import BedrockClient

        if bedrock_runtime is None or not model_id:
            raise AgentConfigError("Bedrock needs the runtime client and the model id")
        return BedrockClient(bedrock_runtime, model_id)
    raise AgentConfigError("no agent provider is configured")
