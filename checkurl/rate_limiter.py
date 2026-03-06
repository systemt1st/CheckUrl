from __future__ import annotations

import asyncio
import time


class AsyncRateLimiter:
    def __init__(self, rps: float) -> None:
        self.interval = 0.0 if rps <= 0 else 1.0 / rps
        self._next_allowed = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if self.interval <= 0:
            return

        sleep_for = 0.0
        async with self._lock:
            now = time.monotonic()
            if now < self._next_allowed:
                sleep_for = self._next_allowed - now
                anchor = self._next_allowed
            else:
                anchor = now
            self._next_allowed = anchor + self.interval

        if sleep_for > 0:
            await asyncio.sleep(sleep_for)
