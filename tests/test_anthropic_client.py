"""AnthropicLLMClient against a fake SDK client (real SDK response/exception types, no network)."""

from __future__ import annotations

from typing import Any

import anthropic
import httpx2
import pytest
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage

from sfdq.agent import AnthropicLLMClient, LLMError, LLMFatalError, build_llm_client
from sfdq.agent.mock_llm import MockLLMClient
from sfdq.settings import load_settings


class FakeMessages:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeSDK:
    def __init__(self, result: Any) -> None:
        self.messages = FakeMessages(result)


def _message(stop: str = "tool_use") -> Message:
    return Message(
        id="msg_1",
        type="message",
        role="assistant",
        model="claude-opus-5-5",
        stop_reason=stop,  # type: ignore[arg-type]
        stop_sequence=None,
        content=[
            TextBlock(type="text", text="Let me look."),
            ToolUseBlock(
                type="tool_use", id="toolu_1", name="get_record", input={"record_id": "001X"}
            ),
        ],
        usage=Usage(input_tokens=1200, output_tokens=80),
    )


def _error(cls: type[anthropic.APIStatusError], status: int) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("nope", response=httpx2.Response(status, request=request), body=None)


def test_translates_text_tool_calls_and_usage() -> None:
    sdk = FakeSDK(_message())
    client = AnthropicLLMClient("sk-test", "claude-opus-5-5", client=sdk)  # type: ignore[arg-type]
    turn = client.complete(
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
        tools=[{"name": "t", "input_schema": {}}],
    )
    assert turn.text == "Let me look." and turn.stop_reason == "tool_use"
    assert [(c.id, c.name, c.input) for c in turn.tool_calls] == [
        ("toolu_1", "get_record", {"record_id": "001X"})
    ]
    assert (turn.input_tokens, turn.output_tokens) == (1200, 80)
    assert (
        turn.content is sdk.messages.result.content
    )  # appended verbatim next turn (thinking blocks etc.)


def test_request_shape_uses_the_configured_model_and_caches_the_stable_prefix() -> None:
    sdk = FakeSDK(_message("end_turn"))
    AnthropicLLMClient("sk-test", "claude-opus-5-5", client=sdk).complete(  # type: ignore[arg-type]
        system="SYS", messages=[{"role": "user", "content": "hi"}], tools=[]
    )
    sent = sdk.messages.kwargs
    assert sent["model"] == "claude-opus-5-5" and sent["max_tokens"] >= 4096
    assert sent["system"] == [
        {"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}
    ]
    assert "tool_choice" not in sent, (
        "forced tool_choice is rejected by current models; we use auto"
    )


@pytest.mark.parametrize(
    ("exc_cls", "status", "expected"),
    [
        (anthropic.AuthenticationError, 401, LLMFatalError),
        (anthropic.PermissionDeniedError, 403, LLMFatalError),
        (anthropic.NotFoundError, 404, LLMFatalError),  # e.g. a mistyped SFDQ_MODEL
        (anthropic.RateLimitError, 429, LLMError),
        (anthropic.InternalServerError, 500, LLMError),
        (anthropic.BadRequestError, 400, LLMError),
    ],
)
def test_error_mapping(
    exc_cls: type[anthropic.APIStatusError], status: int, expected: type[Exception]
) -> None:
    client = AnthropicLLMClient("k", "m", client=FakeSDK(_error(exc_cls, status)))  # type: ignore[arg-type]
    with pytest.raises(expected) as info:
        client.complete(system="s", messages=[], tools=[])
    assert (type(info.value) is LLMFatalError) == (expected is LLMFatalError)


def test_connection_errors_are_recoverable() -> None:
    err = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com"))
    client = AnthropicLLMClient("k", "m", client=FakeSDK(err))  # type: ignore[arg-type]
    with pytest.raises(LLMError, match="could not reach"):
        client.complete(system="s", messages=[], tools=[])


def test_factory_is_mock_by_default_and_real_only_with_a_key(tmp_path: Any) -> None:
    assert isinstance(build_llm_client(load_settings({"SFDQ_HOME": str(tmp_path)})), MockLLMClient)
    real = build_llm_client(
        load_settings({"SFDQ_HOME": str(tmp_path), "ANTHROPIC_API_KEY": "sk-x"})
    )
    assert isinstance(real, AnthropicLLMClient) and real.model == "claude-opus-5-5"
    forced_mock = load_settings({"ANTHROPIC_API_KEY": "sk-x", "SFDQ_MODE": "mock"})
    assert isinstance(build_llm_client(forced_mock), MockLLMClient)
