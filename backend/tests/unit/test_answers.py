"""Which completed responses count as answers: tool calls and empty text are billed but not answers."""

from __future__ import annotations

import pytest

from foundry_eval.providers.anthropic_messages import parse_message
from foundry_eval.providers.base import ProviderResult, unusable_answer
from foundry_eval.providers.openai_compat import parse_chat_object, parse_responses_object
from foundry_eval.types import FinishReason


def result(text: str, finish: FinishReason, ok: bool = True) -> ProviderResult:
    return ProviderResult(ok=ok, request_body={}, streamed=False, latency_ms=1, output_text=text, finish_reason=finish)


def test_chat_tool_calls_finish_is_a_tool_call():
    for raw in ("tool_calls", "function_call"):
        parsed = parse_chat_object({"choices": [{"message": {"content": None}, "finish_reason": raw}]})
        assert parsed["finish"] is FinishReason.TOOL_CALL and parsed["raw_finish"] == raw


def test_responses_function_call_item_is_a_tool_call():
    obj = {"status": "completed", "output": [{"type": "function_call", "name": "lookup", "arguments": "{}"}]}
    assert parse_responses_object(obj)["finish"] is FinishReason.TOOL_CALL


def test_responses_server_side_tool_with_a_message_is_an_answer():
    obj = {
        "status": "completed",
        "output": [
            {"type": "web_search_call", "status": "completed"},
            {"type": "message", "content": [{"type": "output_text", "text": "Found it."}]},
        ],
    }
    parsed = parse_responses_object(obj)
    assert parsed["finish"] is FinishReason.COMPLETED and parsed["text"] == "Found it."


def test_claude_tool_use_stop_is_a_tool_call():
    message = {"content": [{"type": "tool_use", "id": "t", "name": "lookup", "input": {}}], "stop_reason": "tool_use"}
    assert parse_message(message)["finish"] is FinishReason.TOOL_CALL


@pytest.mark.parametrize(
    ("text", "finish", "expected"),
    [
        ("An answer.", FinishReason.COMPLETED, None),
        ("Truncated ans", FinishReason.MAX_TOKENS, None),
        ("I can't help with that.", FinishReason.REFUSAL, None),
        ("Let me look that up.", FinishReason.TOOL_CALL, "call a tool"),
        ("", FinishReason.TOOL_CALL, "call a tool"),
        ("", FinishReason.COMPLETED, "no answer text (finish reason: completed)"),
        ("  \n", FinishReason.MAX_TOKENS, "no answer text (finish reason: max_tokens)"),
        ("", FinishReason.OTHER, "no answer text (finish reason: other)"),
    ],
)
def test_unusable_answer(text, finish, expected):
    problem = unusable_answer(result(text, finish))
    assert problem is None if expected is None else expected in (problem or "")


def test_failed_calls_keep_their_own_error():
    assert unusable_answer(result("", FinishReason.ERROR, ok=False)) is None
