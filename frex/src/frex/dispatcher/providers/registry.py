from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

from frex.dispatcher.providers.anthropic import AnthropicProvider
from frex.dispatcher.providers.base import Provider, ProviderError
from frex.dispatcher.providers.gemini import GeminiProvider
from frex.dispatcher.providers.openai_compat import OpenAICompatProvider

if TYPE_CHECKING:
    from frex.dispatcher.config import DispatchConfig, ModelConfig

LOG = logging.getLogger(__name__)

PROVIDER_TYPES: dict[str, type] = {
    "openai_compat": OpenAICompatProvider,
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
}


class NoModelAvailable(Exception):
    """Raised when every model in a routing chain is unhealthy."""


class ProviderRegistry:
    """Owns provider adapters and resolves task types to healthy models.

    Routing is a chain per task type: the first healthy model wins, so a
    dead local vLLM falls through to the next entry instead of stalling
    the fleet. Health is memoized briefly because dispatch happens on
    every loop tick.
    """

    def __init__(self, config: "DispatchConfig"):
        self._models: dict[str, "ModelConfig"] = {
            m.name: m for m in (config.models or [])
        }
        self._routing: dict[str, list[str]] = dict(config.routing or {})
        self._providers: dict[str, Provider] = {}
        for provider_config in config.providers or []:
            cls = PROVIDER_TYPES.get(provider_config.type)
            if cls is None:
                raise ProviderError(f"unknown provider type {provider_config.type}")
            kwargs: dict[str, Any] = {"name": provider_config.name}
            kwargs["base_url"] = provider_config.base_url
            kwargs["api_key"] = provider_config.api_key
            self._providers[provider_config.name] = cls(**kwargs)
        self._health: dict[str, bool] = {}
        self.validate()

    def validate(self) -> None:
        for task_type, chain in self._routing.items():
            for model_name in chain:
                if model_name not in self._models:
                    raise ProviderError(
                        f"routing[{task_type}] references unknown model {model_name}"
                    )
        for model in self._models.values():
            if model.provider not in self._providers:
                raise ProviderError(
                    f"model {model.name} references unknown provider {model.provider}"
                )

    def resolve(self, task_type: str) -> "ModelConfig":
        chain = self._routing.get(task_type)
        if not chain:
            raise NoModelAvailable(f"no routing configured for task type {task_type}")
        for model_name in chain:
            if self.healthy(model_name):
                return self._models[model_name]
        raise NoModelAvailable(
            f"all models unhealthy for task type {task_type}: {chain}"
        )

    def healthy(self, model_name: str) -> bool:
        cached = self._health.get(model_name)
        if cached is not None:
            return cached
        model = self._models[model_name]
        provider = self._providers[model.provider]
        result = provider.healthcheck(model.model)
        if not result:
            LOG.warning("model unhealthy model=%s provider=%s", model_name, model.provider)
        self._health[model_name] = result
        return result

    def invalidate_health(self, model_name: str | None = None) -> None:
        """Forget cached health so the next resolve re-probes.

        Called after a runtime failure: the model that just errored is
        exactly the one whose cached health is stale.
        """
        if model_name is None:
            self._health.clear()
        else:
            self._health.pop(model_name, None)

    def get_provider(self, model_name: str) -> Provider:
        model = self._models[model_name]
        return self._providers[model.provider]

    def routing_chain(self, task_type: str) -> list["ModelConfig"]:
        chain = self._routing.get(task_type)
        if not chain:
            return []
        return [self._models[name] for name in chain if name in self._models]

    def make_failover(
        self, task_type: str, current_model_name: str
    ) -> Callable[[], tuple[Provider, str] | None]:
        """Build a failover callable for a running agent loop.

        The callable advances along the routing chain, skipping unhealthy
        models, and returns (provider, model_id) for the next candidate —
        or None when the chain is exhausted. Health of the model being
        left behind is invalidated so future resolves re-probe it.
        """
        chain = self.routing_chain(task_type)
        try:
            index = next(
                i for i, m in enumerate(chain) if m.name == current_model_name
            )
        except StopIteration:
            index = 0

        def failover() -> tuple[Provider, str] | None:
            nonlocal index
            while index + 1 < len(chain):
                index += 1
                candidate = chain[index]
                if self.healthy(candidate.name):
                    return self.get_provider(candidate.name), candidate.model
            # Everything after the start position is unhealthy: forget
            # cached health for the whole tail so the next resolve
            # re-probes instead of trusting a stale failure.
            self.invalidate_health()
            return None

        return failover
