from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class RetryClass(str, Enum):
    NONE = "none"
    RATE_LIMIT = "rate_limit"
    PROVIDER_OUTAGE = "provider_outage"
    TIMEOUT = "timeout"
    RETRYABLE_UPSTREAM = "retryable_upstream"
    AUTHENTICATION = "authentication"
    INVALID_REQUEST = "invalid_request"
    CONTEXT_LIMIT = "context_limit"
    POLICY = "policy"
    PROGRAMMING = "programming"

    @property
    def allows_fallback(self) -> bool:
        return self in {self.RATE_LIMIT, self.PROVIDER_OUTAGE, self.TIMEOUT, self.RETRYABLE_UPSTREAM}


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Message:
    role: str
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class ModelTurn:
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str = "stop"
    provider_request_id: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    estimated_cost_usd: float = 0.0
    retry_class: RetryClass = RetryClass.NONE


@dataclass(frozen=True)
class ModelPolicy:
    max_wall_seconds: float
    max_turns: int
    max_tool_calls: int
    max_input_tokens: int
    max_output_tokens: int
    max_cost_usd: float
    provider_retries: int
    circuit_failure_threshold: int
    request_timeout_seconds: float


class AgentModel(Protocol):
    name: str
    async def run_turn(self, messages: list[Message], tools: list[ToolDefinition], policy: ModelPolicy) -> ModelTurn: ...
