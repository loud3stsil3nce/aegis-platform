"""Live local-only and transient-fallback verification from the runtime container."""

import asyncio
import os

from core.agent_runtime import AgentRuntime, Message, ModelPolicy, ModelTurn, RetryClass
from core.agent_runtime.adapters import ModelProviderError, OpenAICompatibleAdapter
from core.agent_runtime.config import ProviderConfig

MODEL = os.getenv("AEGIS_OLLAMA_MODEL", "")
if not MODEL:
    raise RuntimeError("AEGIS_OLLAMA_MODEL is required")

POLICY = ModelPolicy(
    max_wall_seconds=120, max_turns=3, max_tool_calls=2,
    max_input_tokens=4096, max_output_tokens=128, max_cost_usd=0.01,
    provider_retries=0, circuit_failure_threshold=2, request_timeout_seconds=90,
)


async def no_tools(_name, _arguments):
    raise RuntimeError("tool execution was not expected")


class TransientPrimary:
    name = "transient-primary"
    async def run_turn(self, *_args):
        raise ModelProviderError("intentional availability fixture", RetryClass.PROVIDER_OUTAGE)


async def main() -> None:
    local = OpenAICompatibleAdapter(
        ProviderConfig("ollama-local", "ollama-openai", "http://ollama:11434/v1", MODEL, None)
    )
    await local.validate_model()
    direct = await AgentRuntime([local], no_tools).run(
        [Message("user", "Reply with the single word READY.")], [], POLICY
    )
    if not direct.text.strip():
        raise RuntimeError("Ollama returned an empty direct response")
    fallback = await AgentRuntime([TransientPrimary(), local], no_tools).run(
        [Message("user", "Reply with the single word FALLBACK.")], [], POLICY
    )
    if fallback.provider != "ollama-local" or not fallback.text.strip():
        raise RuntimeError("transient fallback did not reach Ollama")
    print(f"ollama model validation: pass ({MODEL})")
    print("local-only completion: pass")
    print("transient-to-Ollama fallback: pass")


if __name__ == "__main__":
    asyncio.run(main())
