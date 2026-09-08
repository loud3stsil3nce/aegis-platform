"""Construct configured providers without leaking adapter details into callers."""

from __future__ import annotations

from collections.abc import Callable

from .adapters.openai_compatible import OpenAICompatibleAdapter, Transport
from .config import ProviderConfig


def build_providers(
    configs: list[ProviderConfig],
    transport_factory: Callable[[ProviderConfig], Transport] | None = None,
) -> list[OpenAICompatibleAdapter]:
    providers = []
    for config in configs:
        providers.append(
            OpenAICompatibleAdapter(config)
            if transport_factory is None
            else OpenAICompatibleAdapter(config, transport_factory(config))
        )
    return providers
