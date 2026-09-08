"""Normalized adapter for OpenRouter and Ollama's OpenAI-compatible API."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from ..config import ProviderConfig
from ..types import Message, ModelPolicy, ModelTurn, RetryClass, TokenUsage, ToolCall, ToolDefinition


class ModelProviderError(RuntimeError):
    def __init__(self, message: str, retry_class: RetryClass):
        super().__init__(message)
        self.retry_class = retry_class


Transport = Callable[[urllib.request.Request, float], tuple[int, dict[str, str], bytes]]


def _default_transport(request: urllib.request.Request, timeout: float) -> tuple[int, dict[str, str], bytes]:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, dict(response.headers), response.read(1_048_577)


def _classification(code: int, body: bytes) -> RetryClass:
    if code in {401, 403}: return RetryClass.AUTHENTICATION
    if code == 429: return RetryClass.RATE_LIMIT
    if code in {408, 504}: return RetryClass.TIMEOUT
    if 500 <= code <= 599: return RetryClass.RETRYABLE_UPSTREAM
    text = body.decode("utf-8", errors="ignore").lower()
    if code == 400 and ("context" in text or "token limit" in text): return RetryClass.CONTEXT_LIMIT
    if 400 <= code <= 499: return RetryClass.INVALID_REQUEST
    return RetryClass.PROGRAMMING


class OpenAICompatibleAdapter:
    def __init__(self, config: ProviderConfig, transport: Transport = _default_transport):
        self.config, self.transport, self.name = config, transport, config.id

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key_file:
            try:
                raw_token = Path(self.config.api_key_file).read_bytes()
            except OSError as exc:
                raise ModelProviderError("provider credential file is unavailable", RetryClass.AUTHENTICATION) from exc
            if len(raw_token) > 4096:
                raise ModelProviderError("provider credential file exceeds 4 KiB", RetryClass.AUTHENTICATION)
            try:
                token = raw_token.decode("utf-8", errors="strict").strip()
            except UnicodeDecodeError as exc:
                raise ModelProviderError("provider credential file is not UTF-8", RetryClass.AUTHENTICATION) from exc
            if not token:
                raise ModelProviderError("provider credential file is empty", RetryClass.AUTHENTICATION)
            headers["Authorization"] = f"Bearer {token}"
        elif self.config.api_key_env:
            token = os.getenv(self.config.api_key_env)
            if not token:
                raise ModelProviderError(f"{self.config.api_key_env} is not configured", RetryClass.AUTHENTICATION)
            headers["Authorization"] = f"Bearer {token}"
        if self.config.adapter == "openrouter":
            headers["HTTP-Referer"] = "https://aegis.local"
            headers["X-Title"] = "Aegis"
        return headers

    def _request(self, path: str, payload: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.config.base_url + path, data=data, headers=self._headers(), method="POST" if data else "GET")
        try:
            status, _headers, raw = self.transport(request, timeout)
        except urllib.error.HTTPError as exc:
            body = exc.read(65_536)
            raise ModelProviderError(f"provider returned HTTP {exc.code}", _classification(exc.code, body)) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            reason = getattr(exc, "reason", exc)
            classification = RetryClass.TIMEOUT if isinstance(reason, (TimeoutError, socket.timeout)) else RetryClass.PROVIDER_OUTAGE
            raise ModelProviderError("provider transport failed", classification) from exc
        if status >= 400:
            raise ModelProviderError(f"provider returned HTTP {status}", _classification(status, raw))
        if len(raw) > 1_048_576:
            raise ModelProviderError("provider response exceeds 1 MiB", RetryClass.PROGRAMMING)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ModelProviderError("provider returned invalid JSON", RetryClass.PROGRAMMING) from exc
        if not isinstance(value, dict):
            raise ModelProviderError("provider response must be an object", RetryClass.PROGRAMMING)
        return value

    async def validate_model(self, timeout: float = 10.0) -> None:
        payload = await asyncio.to_thread(self._request, "/models", None, timeout)
        ids = {item.get("id") for item in payload.get("data", []) if isinstance(item, dict)}
        if self.config.model not in ids:
            raise ModelProviderError(f"configured model {self.config.model} is unavailable", RetryClass.INVALID_REQUEST)

    async def run_turn(self, messages: list[Message], tools: list[ToolDefinition], policy: ModelPolicy) -> ModelTurn:
        normalized_messages = []
        for item in messages:
            value: dict[str, Any] = {"role": item.role, "content": item.content}
            if item.tool_calls:
                value["tool_calls"] = [{"id": call.id, "type": "function", "function": {"name": call.name, "arguments": json.dumps(call.arguments, sort_keys=True)}} for call in item.tool_calls]
            if item.tool_call_id:
                value["tool_call_id"] = item.tool_call_id
            normalized_messages.append(value)
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": normalized_messages,
            "max_tokens": policy.max_output_tokens,
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": {"name": tool.name, "description": tool.description, "parameters": tool.input_schema}} for tool in tools]
        value = await asyncio.to_thread(self._request, "/chat/completions", payload, policy.request_timeout_seconds)
        try:
            choice = value["choices"][0]
            message = choice["message"]
            calls = []
            for raw_call in message.get("tool_calls") or []:
                arguments = json.loads(raw_call["function"]["arguments"])
                if not isinstance(arguments, dict): raise TypeError("tool arguments are not an object")
                calls.append(ToolCall(raw_call["id"], raw_call["function"]["name"], arguments))
            usage_value = value.get("usage") or {}
            usage = TokenUsage(int(usage_value.get("prompt_tokens", 0)), int(usage_value.get("completion_tokens", 0)))
            cost = (usage.input_tokens * self.config.input_cost_per_million + usage.output_tokens * self.config.output_cost_per_million) / 1_000_000
            return ModelTurn(str(message.get("content") or ""), tuple(calls), str(choice.get("finish_reason") or "stop"), value.get("id"), usage, cost)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ModelProviderError("provider response violated normalized contract", RetryClass.PROGRAMMING) from exc
