from __future__ import annotations

import unittest

import aiohttp

from checkurl.dispatcher import DispatchController
from checkurl.errors import ErrorKind
from checkurl.providers.base import Provider


class DummyProvider(Provider):
    def __init__(self, name: str) -> None:
        super().__init__(name, timeout=1.0, rps=100.0)

    async def check_once(self, normalized_url: str, session: aiohttp.ClientSession) -> tuple[int, str]:
        raise NotImplementedError


class AdaptiveDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_adaptive_prefers_high_success_and_low_latency(self) -> None:
        fast = DummyProvider("fast")
        slow = DummyProvider("slow")
        unstable = DummyProvider("unstable")

        controller = DispatchController(
            [fast, slow, unstable],
            order_strategy="adaptive",
            cooldown_seconds=30.0,
            cooldown_backoff_factor=2.0,
            max_cooldown_seconds=300.0,
            rate_limit_threshold=2,
            max_requests_per_run=0,
            max_requests_per_minute=0,
        )

        for _ in range(6):
            await controller.record_success("fast", latency_ms=80.0)

        for _ in range(4):
            await controller.record_success("slow", latency_ms=1100.0)

        await controller.record_error("unstable", error_kind=ErrorKind.PROVIDER_DOWN, latency_ms=300.0)
        await controller.record_error("unstable", error_kind=ErrorKind.PROVIDER_DOWN, latency_ms=320.0)

        ordered = await controller.get_provider_order("https://example.com")
        self.assertEqual(ordered[0].name, "fast")
        self.assertEqual(ordered[-1].name, "unstable")


if __name__ == "__main__":
    unittest.main()
