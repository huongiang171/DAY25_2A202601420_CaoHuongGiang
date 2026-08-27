from __future__ import annotations

import time
from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError, ProviderResponse


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers."""

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
        budget: float | None = None,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache
        self.budget = budget
        self.cumulative_cost = 0.0

    def complete(self, prompt: str) -> GatewayResponse:
        """Return a reliable response or a static fallback.

        Pipeline:
        1. CACHE CHECK — return immediately on cache hit
        2. COST CHECK — if over budget, skip providers
        3. PROVIDER FALLBACK CHAIN — try each provider via circuit breaker
        4. STATIC FALLBACK — all providers failed
        """
        start = time.perf_counter()

        # 1. CACHE CHECK
        if self.cache is not None:
            cached_text, score = self.cache.get(prompt)
            if cached_text is not None:
                return GatewayResponse(
                    text=cached_text,
                    route=f"cache_hit:{score:.2f}",
                    provider=None,
                    cache_hit=True,
                    latency_ms=0.0,
                    estimated_cost=0.0,
                )

        # 2. COST CHECK & PROVIDER FALLBACK CHAIN
        last_error: str | None = None
        
        skip_all = False
        skip_expensive = False
        
        if self.budget is not None:
            if self.cumulative_cost >= self.budget:
                skip_all = True
            elif self.cumulative_cost >= self.budget * 0.8:
                skip_expensive = True
                
        if not skip_all:
            for i, provider in enumerate(self.providers):
                if skip_expensive and provider.cost_per_request > 0.01:
                    last_error = f"Budget near limit, skipping expensive provider {provider.name}"
                    continue
                    
                breaker = self.breakers[provider.name]
                try:
                    response: ProviderResponse = breaker.call(provider.complete, prompt)
                    # Store in cache on success
                    if self.cache is not None:
                        self.cache.set(prompt, response.text, {"provider": provider.name})
                    route = "primary" if i == 0 else "fallback"
                    self.cumulative_cost += response.estimated_cost
                    return GatewayResponse(
                        text=response.text,
                        route=route,
                        provider=response.provider,
                        cache_hit=False,
                        latency_ms=response.latency_ms,
                        estimated_cost=response.estimated_cost,
                    )
                except (ProviderError, CircuitOpenError) as e:
                    last_error = str(e)
                    continue

        # 3. STATIC FALLBACK — all providers failed
        latency_ms = (time.perf_counter() - start) * 1000
        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=latency_ms,
            estimated_cost=0.0,
            error=last_error,
        )
