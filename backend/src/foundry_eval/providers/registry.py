"""Adapter registry keyed by API style."""

from __future__ import annotations

import httpx2

from ..types import ApiStyle
from .anthropic_messages import AnthropicMessagesAdapter
from .base import ProviderAdapter
from .local_cli import ClaudeCliAdapter, CodexCliAdapter
from .openai_compat import ChatCompletionsAdapter, ResponsesAdapter


def default_adapters(transport: httpx2.AsyncBaseTransport | None = None) -> dict[ApiStyle, ProviderAdapter]:
    return {
        ApiStyle.OPENAI_RESPONSES: ResponsesAdapter(transport),
        ApiStyle.OPENAI_CHAT: ChatCompletionsAdapter(transport),
        ApiStyle.ANTHROPIC_MESSAGES: AnthropicMessagesAdapter(transport),
        ApiStyle.CLAUDE_CLI: ClaudeCliAdapter(),
        ApiStyle.CODEX_CLI: CodexCliAdapter(),
    }
