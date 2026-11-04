"""The agent layer: Claude (or a mock) reasons about findings and PROPOSES fixes."""

from __future__ import annotations

from sfdq.agent.llm import AnthropicLLMClient, LLMClient, LLMError, LLMFatalError
from sfdq.agent.mock_llm import MockLLMClient
from sfdq.agent.proposer import FixProposer, ProposeStats, propose_all
from sfdq.settings import Settings

__all__ = [
    "AnthropicLLMClient",
    "FixProposer",
    "LLMClient",
    "LLMError",
    "LLMFatalError",
    "MockLLMClient",
    "ProposeStats",
    "build_llm_client",
    "propose_all",
]


def build_llm_client(settings: Settings) -> LLMClient:
    """Return Claude when ``ANTHROPIC_API_KEY`` is configured, otherwise the canned mock."""
    if settings.llm_mode == "real" and settings.anthropic_api_key:
        return AnthropicLLMClient(settings.anthropic_api_key, settings.model)
    return MockLLMClient()
