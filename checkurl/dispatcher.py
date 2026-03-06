from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import random
import time
import zlib
from typing import Optional, Sequence

import aiohttp

from .errors import ErrorKind, ProviderError
from .logging_utils import log_event
from .models import CheckResult
from .providers.base import Provider
from .stats import StatsCollector
from .utils import clean_text, now_iso


@dataclass
class ProviderRuntimeState:
    run_requests: int = 0
    minute_window_started: float = 0.0
    minute_requests: int = 0
    rate_limit_streak: int = 0
    cooldown_level: int = 0
    cooldown_until: float = 0.0
    attempts: int = 0
    success: int = 0
    failure: int = 0
    consecutive_failures: int = 0
    latency_ema_ms: float = 0.0
    last_latency_ms: float = 0.0


class DispatchController:
    def __init__(
        self,
        providers: Sequence[Provider],
        *,
        order_strategy: str,
        cooldown_seconds: float,
        cooldown_backoff_factor: float,
        max_cooldown_seconds: float,
        rate_limit_threshold: int,
        max_requests_per_run: int,
        max_requests_per_minute: int,
    ) -> None:
        self.providers = list(providers)
        self.order_strategy = order_strategy
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.cooldown_backoff_factor = max(1.0, float(cooldown_backoff_factor))
        self.max_cooldown_seconds = max(0.0, float(max_cooldown_seconds))
        if self.max_cooldown_seconds > 0 and self.cooldown_seconds > 0:
            self.max_cooldown_seconds = max(self.cooldown_seconds, self.max_cooldown_seconds)
        self.rate_limit_threshold = max(0, int(rate_limit_threshold))
        self.max_requests_per_run = max(0, int(max_requests_per_run))
        self.max_requests_per_minute = max(0, int(max_requests_per_minute))

        self._rotation_pool = [provider for provider in self.providers if provider.name != "smallseotools"]
        self._tail_pool = [provider for provider in self.providers if provider.name == "smallseotools"]
        if not self._rotation_pool:
            self._rotation_pool = list(self.providers)
            self._tail_pool = []

        self._rotation_index = {
            provider.name: index for index, provider in enumerate(self._rotation_pool)
        }

        self._next_start = 0
        self._order_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._states: dict[str, ProviderRuntimeState] = {}

    async def get_provider_order(self, normalized_url: str) -> list[Provider]:
        if len(self._rotation_pool) <= 1:
            return list(self._rotation_pool) + list(self._tail_pool)

        if self.order_strategy == "adaptive":
            adaptive_order = await self._adaptive_rotation_order()
            return adaptive_order + list(self._tail_pool)

        start = 0
        if self.order_strategy == "round_robin":
            async with self._order_lock:
                start = self._next_start
                self._next_start = (self._next_start + 1) % len(self._rotation_pool)
        elif self.order_strategy == "url_hash":
            start = zlib.crc32(normalized_url.encode("utf-8")) % len(self._rotation_pool)

        rotated = self._rotation_pool[start:] + self._rotation_pool[:start]
        return rotated + list(self._tail_pool)

    async def _adaptive_rotation_order(self) -> list[Provider]:
        async with self._state_lock:
            snapshots = dict(self._states)

        now = time.monotonic()
        scored: list[tuple[float, int, Provider]] = []
        for provider in self._rotation_pool:
            state = snapshots.get(provider.name)
            index = self._rotation_index.get(provider.name, 0)
            score = self._provider_score(state, now=now, index=index)
            scored.append((score, -index, provider))

        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [item[2] for item in scored]

    @staticmethod
    def _provider_score(
        state: Optional[ProviderRuntimeState],
        *,
        now: float,
        index: int,
    ) -> float:
        if state is None or state.attempts <= 0:
            return 0.5 - index * 0.001

        success_rate = state.success / max(1, state.attempts)
        latency_ms = state.latency_ema_ms if state.latency_ema_ms > 0 else 1000.0
        latency_score = 1.0 / (1.0 + latency_ms / 800.0)
        confidence = min(1.0, state.attempts / 20.0)
        blended = (success_rate * 0.75 + latency_score * 0.25) * confidence + 0.5 * (1.0 - confidence)

        failure_penalty = min(0.35, state.consecutive_failures * 0.06)
        cooldown_penalty = 0.2 if state.cooldown_until > now else 0.0
        return blended - failure_penalty - cooldown_penalty

    async def reserve_attempt(self, provider_name: str) -> tuple[bool, str]:
        async with self._state_lock:
            state = self._get_state_unlocked(provider_name)
            now = time.monotonic()

            if state.cooldown_until > now:
                return False, f"cooldown({state.cooldown_until - now:.1f}s)"

            if self.max_requests_per_run > 0 and state.run_requests >= self.max_requests_per_run:
                return False, "run_budget_exceeded"

            if self.max_requests_per_minute > 0:
                if now - state.minute_window_started >= 60:
                    state.minute_window_started = now
                    state.minute_requests = 0
                if state.minute_requests >= self.max_requests_per_minute:
                    return False, "minute_budget_exceeded"

            state.run_requests += 1
            state.minute_requests += 1
            return True, ""

    async def record_success(self, provider_name: str, latency_ms: float) -> None:
        async with self._state_lock:
            state = self._get_state_unlocked(provider_name)
            state.attempts += 1
            state.success += 1
            state.consecutive_failures = 0
            self._record_latency_unlocked(state, latency_ms)
            state.rate_limit_streak = 0
            state.cooldown_level = 0

    async def record_error(self, provider_name: str, error_kind: ErrorKind, latency_ms: float) -> float:
        async with self._state_lock:
            state = self._get_state_unlocked(provider_name)
            now = time.monotonic()
            state.attempts += 1
            state.failure += 1
            state.consecutive_failures += 1
            self._record_latency_unlocked(state, latency_ms)

            if error_kind == ErrorKind.RATE_LIMITED:
                state.rate_limit_streak += 1
                if (
                    self.rate_limit_threshold > 0
                    and self.cooldown_seconds > 0
                    and state.rate_limit_streak >= self.rate_limit_threshold
                ):
                    state.cooldown_level += 1
                    cooldown_seconds = self.cooldown_seconds * (
                        self.cooldown_backoff_factor ** max(0, state.cooldown_level - 1)
                    )
                    if self.max_cooldown_seconds > 0:
                        cooldown_seconds = min(cooldown_seconds, self.max_cooldown_seconds)
                    state.cooldown_until = max(state.cooldown_until, now + cooldown_seconds)
                    state.rate_limit_streak = 0
                    return state.cooldown_until - now
                return 0.0

            state.rate_limit_streak = 0
            state.cooldown_level = 0
            return 0.0

    @staticmethod
    def _record_latency_unlocked(state: ProviderRuntimeState, latency_ms: float) -> None:
        latency = max(0.0, float(latency_ms))
        state.last_latency_ms = latency
        if state.latency_ema_ms <= 0:
            state.latency_ema_ms = latency
            return
        alpha = 0.35
        state.latency_ema_ms = state.latency_ema_ms * (1.0 - alpha) + latency * alpha

    def _get_state_unlocked(self, provider_name: str) -> ProviderRuntimeState:
        state = self._states.get(provider_name)
        if state is not None:
            return state

        now = time.monotonic()
        state = ProviderRuntimeState(minute_window_started=now)
        self._states[provider_name] = state
        return state


def _retry_jitter_factor(retry_jitter: float) -> float:
    if retry_jitter <= 0:
        return 1.0
    low = max(0.0, 1.0 - retry_jitter)
    high = 1.0 + retry_jitter
    return random.uniform(low, high)


async def check_with_fallback(
    normalized_url: str,
    session: aiohttp.ClientSession,
    *,
    dispatch_controller: DispatchController,
    max_retries: int,
    retry_backoff: float,
    retry_jitter: float,
    stats: StatsCollector,
    logger: logging.Logger,
) -> CheckResult:
    errors: list[str] = []
    providers = await dispatch_controller.get_provider_order(normalized_url)

    for provider in providers:
        if provider.is_disabled():
            errors.append(f"{provider.name}:disabled")
            continue

        last_error: Optional[ProviderError] = None
        for attempt in range(max_retries + 1):
            allowed, blocked_reason = await dispatch_controller.reserve_attempt(provider.name)
            if not allowed:
                errors.append(f"{provider.name}:{blocked_reason}")
                log_event(
                    logger,
                    "provider_skipped",
                    level=logging.DEBUG,
                    provider=provider.name,
                    url=normalized_url,
                    reason=blocked_reason,
                )
                break

            started = time.perf_counter()
            try:
                status_code, detail = await provider.check_once(normalized_url, session)
                latency_ms = (time.perf_counter() - started) * 1000
                stats.record_attempt(provider.name, latency_ms, success=True)
                await dispatch_controller.record_success(provider.name, latency_ms)
                log_event(
                    logger,
                    "provider_success",
                    level=logging.DEBUG,
                    provider=provider.name,
                    url=normalized_url,
                    status_code=status_code,
                    latency_ms=round(latency_ms, 2),
                    attempt=attempt,
                )
                return CheckResult(
                    normalized_url=normalized_url,
                    status_code=status_code,
                    provider=provider.name,
                    detail=detail,
                    checked_at=now_iso(),
                    latency_ms=round(latency_ms, 2),
                )
            except ProviderError as exc:
                latency_ms = (time.perf_counter() - started) * 1000
                stats.record_attempt(
                    provider.name,
                    latency_ms,
                    success=False,
                    error_kind=exc.kind.value,
                )
                cooldown_seconds = await dispatch_controller.record_error(provider.name, exc.kind, latency_ms)
                last_error = exc
                log_event(
                    logger,
                    "provider_error",
                    level=logging.DEBUG,
                    provider=provider.name,
                    url=normalized_url,
                    error_kind=exc.kind.value,
                    error=clean_text(exc),
                    latency_ms=round(latency_ms, 2),
                    attempt=attempt,
                )

                if cooldown_seconds > 0:
                    log_event(
                        logger,
                        "provider_cooldown",
                        provider=provider.name,
                        cooldown_seconds=round(cooldown_seconds, 2),
                        trigger=exc.kind.value,
                    )

                if exc.fatal:
                    provider.disable(str(exc))
                    log_event(
                        logger,
                        "provider_disabled",
                        provider=provider.name,
                        reason=clean_text(exc),
                    )
                    break

                if exc.retryable and attempt < max_retries:
                    wait_seconds = retry_backoff * (attempt + 1) * _retry_jitter_factor(retry_jitter)
                    log_event(
                        logger,
                        "provider_retry",
                        provider=provider.name,
                        url=normalized_url,
                        wait_seconds=round(wait_seconds, 2),
                        error_kind=exc.kind.value,
                    )
                    await asyncio.sleep(wait_seconds)
                    continue
                break
            except Exception as exc:
                latency_ms = (time.perf_counter() - started) * 1000
                stats.record_attempt(
                    provider.name,
                    latency_ms,
                    success=False,
                    error_kind="unexpected",
                )
                last_error = ProviderError(f"unexpected: {clean_text(exc)}")
                await dispatch_controller.record_error(provider.name, ErrorKind.PROVIDER_DOWN, latency_ms)
                break

        if last_error:
            errors.append(f"{provider.name}:{clean_text(last_error)}")

    return CheckResult(
        normalized_url=normalized_url,
        status_code=-1,
        provider="none",
        detail=clean_text("; ".join(errors) or "all providers failed", limit=500),
        checked_at=now_iso(),
        latency_ms=0.0,
    )
