"""Live OpenRouter cloud-only or mixed-mode verification."""

import asyncio
import os

from core.agent_runtime import AgentRuntime, Message, ModelPolicy
from core.agent_runtime.adapters import OpenAICompatibleAdapter
from core.agent_runtime.config import ProviderConfig

MODE = os.getenv("AEGIS_RUNTIME_MODE", "")
MODEL = os.getenv("AEGIS_OPENROUTER_MODEL", "")
INPUT_COST = float(os.getenv("AEGIS_OPENROUTER_INPUT_COST_PER_MILLION", "0"))
OUTPUT_COST = float(os.getenv("AEGIS_OPENROUTER_OUTPUT_COST_PER_MILLION", "0"))
if MODE not in {"cloud", "mixed"}: raise RuntimeError("AEGIS_RUNTIME_MODE must be cloud or mixed")
if not MODEL: raise RuntimeError("AEGIS_OPENROUTER_MODEL is required")
if INPUT_COST <= 0 or OUTPUT_COST <= 0: raise RuntimeError("positive OpenRouter input and output costs are required")

POLICY = ModelPolicy(120, 3, 2, 4096, 128, 0.10, 1, 2, 90)

async def no_tools(*_): raise RuntimeError("tool execution was not expected")

async def main() -> None:
    cloud = OpenAICompatibleAdapter(ProviderConfig("openrouter", "openrouter", "https://openrouter.ai/api/v1", MODEL, None, INPUT_COST, OUTPUT_COST, "/run/secrets/openrouter"))
    await cloud.validate_model()
    providers = [cloud]
    if MODE == "mixed":
        local_model = os.getenv("AEGIS_OLLAMA_MODEL", "")
        if not local_model: raise RuntimeError("AEGIS_OLLAMA_MODEL is required in mixed mode")
        providers.append(OpenAICompatibleAdapter(ProviderConfig("ollama-local", "ollama-openai", "http://ollama:11434/v1", local_model, None)))
    run = await AgentRuntime(providers, no_tools).run([Message("user", "Reply with the single word READY.")], [], POLICY)
    if run.provider != "openrouter" or not run.text.strip(): raise RuntimeError("OpenRouter completion did not satisfy the live contract")
    print(f"OpenRouter model validation: pass ({MODEL})")
    print(f"{MODE} completion: pass")

if __name__ == "__main__": asyncio.run(main())
