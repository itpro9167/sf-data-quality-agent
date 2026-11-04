"""LLM client abstraction.

``LLMClient`` is the one seam between the agent loop and a model. The real implementation calls
Claude through the official Anthropic SDK; the mock implementation (``mock_llm.py``) speaks the
same tool-use protocol with canned responses, so the full agent loop is exercised with no API key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic


class LLMError(RuntimeError):
    """A single LLM call failed; the proposer falls back to a safe proposal for that finding."""


class LLMFatalError(LLMError):
    """The LLM is unusable (bad key, unknown model, repeated failures): abort the whole run."""


@dataclass(frozen=True)
class ToolCall:
    """One ``tool_use`` request from the model."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass
class LLMTurn:
    """One model response.

    ``content`` is opaque on purpose: it is whatever the provider returned (SDK content blocks,
    including any thinking blocks) and must be appended to the history verbatim on the next call.
    """

    content: Any
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = "end_turn"
    input_tokens: int = 0
    output_tokens: int = 0


class LLMClient(Protocol):
    """What the agent needs from a model."""

    name: str
    model: str

    def complete(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMTurn:
        """Run one model turn and return its text and tool calls."""
        ...


class AnthropicLLMClient:
    """Claude via the official Anthropic Python SDK (Messages API with client-side tools)."""

    name = "claude"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        max_tokens: int = 16_000,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self.model = model
        self._max_tokens = max_tokens
        self._client = client or anthropic.Anthropic(api_key=api_key)

    def complete(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMTurn:
        """Call ``messages.create`` and translate the response and errors."""
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self._max_tokens,
                # The system prompt + tool list are identical for every finding, so mark them as
                # a cache breakpoint; later calls read this prefix from cache once it is large
                # enough for the model's minimum cacheable size.
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=tools,  # type: ignore[arg-type]
                messages=messages,  # type: ignore[arg-type]
            )
        except (
            anthropic.AuthenticationError,
            anthropic.PermissionDeniedError,
            anthropic.NotFoundError,
        ) as exc:
            raise LLMFatalError(
                f"Anthropic API rejected the request ({exc.status_code}): {exc.message}"
            ) from exc
        except anthropic.RateLimitError as exc:
            raise LLMError(f"rate limited by the Anthropic API: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"could not reach the Anthropic API: {exc}") from exc

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, input=dict(block.input)))
        return LLMTurn(
            content=response.content,
            text="".join(text_parts),
            tool_calls=calls,
            stop_reason=str(response.stop_reason),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
