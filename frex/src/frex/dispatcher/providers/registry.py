from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from frex.dispatcher.providers.base import Provider, ProviderError
from frex.dispatcher.providers.openai_compat import OpenAICompatProvider

if TYPE_CHECKING:
    from frex.dispatcher.config import DispatchConfig, ModelConfig

LOG = logging.getLogger(__name__)

PROVIDER_TYPES: dict[str, type] = {
    "openai_compat": OpenAICompatProvider,
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
            if provider_config.type == "openai_compat":
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
