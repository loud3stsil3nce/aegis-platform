"""Load and validate provider/model policy without provider-specific imports."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .types import ModelPolicy


class ModelConfigurationError(ValueError): pass


@dataclass(frozen=True)
class ProviderConfig:
    id: str
    adapter: str
    base_url: str
    model: str
    api_key_env: str | None
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0
    api_key_file: str | None = None


def load_model_config(path: str | Path) -> tuple[ModelPolicy, list[ProviderConfig]]:
    data: dict[str, Any] = json.loads(Path(path).read_text())
    providers = data.get("providers")
    if not isinstance(providers, list) or not providers:
        raise ModelConfigurationError("at least one model provider is required")
    configs: list[ProviderConfig] = []
    ids: set[str] = set()
    for item in providers:
        if item.get("adapter") not in {"openrouter", "ollama-openai"}:
            raise ModelConfigurationError("unsupported provider adapter")
        if not item.get("id") or item["id"] in ids or not item.get("model") or not item.get("baseUrl"):
            raise ModelConfigurationError("provider id, baseUrl, and model must be unique and non-empty")
        ids.add(item["id"])
        input_cost = float(item.get("inputCostPerMillion", 0.0))
        output_cost = float(item.get("outputCostPerMillion", 0.0))
        if input_cost < 0 or output_cost < 0:
            raise ModelConfigurationError("provider token costs cannot be negative")
        if item["adapter"] == "openrouter" and (input_cost <= 0 or output_cost <= 0):
            raise ModelConfigurationError("cloud provider token costs must be positive")
        api_key_env = item.get("apiKeyEnv")
        api_key_file = item.get("apiKeyFile")
        if api_key_env and api_key_file:
            raise ModelConfigurationError("provider cannot configure both apiKeyEnv and apiKeyFile")
        configs.append(ProviderConfig(item["id"], item["adapter"], item["baseUrl"].rstrip("/"), item["model"], api_key_env, input_cost, output_cost, api_key_file))
    budget = data.get("policy", {})
    try:
        policy = ModelPolicy(**budget)
    except TypeError as exc:
        raise ModelConfigurationError(f"invalid or incomplete model policy: {exc}") from exc
    numeric = policy.__dict__
    if any(isinstance(value, bool) or value <= 0 for value in numeric.values()):
        raise ModelConfigurationError("all model policy limits must be positive")
    return policy, configs
