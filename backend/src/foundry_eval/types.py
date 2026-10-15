"""Shared enumerations."""

from __future__ import annotations

from enum import StrEnum


class ApiStyle(StrEnum):
    OPENAI_RESPONSES = "openai_responses"
    OPENAI_CHAT = "openai_chat"
    ANTHROPIC_MESSAGES = "anthropic_messages"

    @property
    def is_openai(self) -> bool:
        return self in (ApiStyle.OPENAI_RESPONSES, ApiStyle.OPENAI_CHAT)


class FinishReason(StrEnum):
    COMPLETED = "completed"
    MAX_TOKENS = "max_tokens"
    CONTENT_FILTER = "content_filter"
    REFUSAL = "refusal"
    CONTEXT_EXCEEDED = "context_exceeded"
    ERROR = "error"
    OTHER = "other"


class AttemptStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    SKIPPED_BUDGET = "skipped_budget"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
