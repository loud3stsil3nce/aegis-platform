"""Budgeted provider-neutral agent loop with transient-only fallback."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from .adapters import ModelProviderError
from .types import AgentModel, Message, ModelPolicy, RetryClass, TokenUsage, ToolDefinition


class RuntimePolicyError(RuntimeError): pass


@dataclass(frozen=True)
class AgentRun:
    text: str
    provider: str
    turns: int
    tool_calls: int
    usage: TokenUsage
    estimated_cost_usd: float
    messages: tuple[Message, ...]


ToolExecutor = Callable[[str, dict], Awaitable[str]]


class AgentRuntime:
    def __init__(self, providers: list[AgentModel], tool_executor: ToolExecutor):
        if not providers: raise ValueError("at least one provider is required")
        self.providers, self.tool_executor = providers, tool_executor
        self._failures = {provider.name: 0 for provider in providers}
        self._open = {provider.name: False for provider in providers}

    async def _turn(self, provider: AgentModel, messages, tools, policy, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise RuntimePolicyError("wall-clock budget exhausted")
        timeout = min(policy.request_timeout_seconds, remaining)
        try:
            return await asyncio.wait_for(provider.run_turn(messages, tools, policy), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise ModelProviderError("provider request timed out", RetryClass.TIMEOUT) from exc

    async def run(self, messages: list[Message], tools: list[ToolDefinition], policy: ModelPolicy) -> AgentRun:
        history, allowed = list(messages), {tool.name for tool in tools}
        deadline = time.monotonic() + policy.max_wall_seconds
        turns = tool_count = input_tokens = output_tokens = 0
        cost = 0.0
        last_transient: ModelProviderError | None = None

        for provider in self.providers:
            if self._open[provider.name]: continue
            attempts = 0
            while attempts <= policy.provider_retries:
                if turns >= policy.max_turns: raise RuntimePolicyError("model turn budget exhausted")
                try:
                    turn = await self._turn(provider, history, tools, policy, deadline)
                except ModelProviderError as exc:
                    if not exc.retry_class.allows_fallback: raise
                    last_transient = exc
                    attempts += 1
                    self._failures[provider.name] += 1
                    if self._failures[provider.name] >= policy.circuit_failure_threshold:
                        self._open[provider.name] = True
                        break
                    if attempts <= policy.provider_retries: continue
                    break

                self._failures[provider.name] = 0
                turns += 1
                input_tokens += turn.usage.input_tokens
                output_tokens += turn.usage.output_tokens
                cost += turn.estimated_cost_usd
                if input_tokens > policy.max_input_tokens: raise RuntimePolicyError("input token budget exhausted")
                if output_tokens > policy.max_output_tokens: raise RuntimePolicyError("output token budget exhausted")
                if cost > policy.max_cost_usd: raise RuntimePolicyError("cost budget exhausted")
                history.append(Message("assistant", turn.text, turn.tool_calls))
                if not turn.tool_calls:
                    return AgentRun(turn.text, provider.name, turns, tool_count, TokenUsage(input_tokens, output_tokens), cost, tuple(history))
                for call in turn.tool_calls:
                    if call.name not in allowed: raise RuntimePolicyError(f"model requested undeclared tool {call.name}")
                    if tool_count >= policy.max_tool_calls: raise RuntimePolicyError("tool-call budget exhausted")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0: raise RuntimePolicyError("wall-clock budget exhausted")
                    result = await asyncio.wait_for(self.tool_executor(call.name, call.arguments), timeout=remaining)
                    tool_count += 1
                    history.append(Message("tool", str(result), tool_call_id=call.id))
                attempts = 0
            # only exhausted transient attempts or an open circuit reaches next provider
        if last_transient: raise last_transient
        raise RuntimePolicyError("all provider circuits are open")
