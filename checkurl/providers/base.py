from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional, Tuple

import aiohttp

from ..errors import ErrorKind, ProviderError
from ..rate_limiter import AsyncRateLimiter
from ..utils import clean_text


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)


class Provider:
    def __init__(self, name: str, *, timeout: float, rps: float) -> None:
        self.name = name
        self.timeout = timeout
        self.rate_limiter = AsyncRateLimiter(rps)
        self._global_rate_limiter: Optional[AsyncRateLimiter] = None
        self._disabled_reason = ""

    def set_global_rate_limiter(self, limiter: Optional[AsyncRateLimiter]) -> None:
        self._global_rate_limiter = limiter

    def is_disabled(self) -> bool:
        return bool(self._disabled_reason)

    def disable(self, reason: str) -> None:
        if not self._disabled_reason:
            self._disabled_reason = clean_text(reason, limit=400)

    async def _wait_for_request(self) -> None:
        await self.rate_limiter.wait()
        if self._global_rate_limiter is not None:
            await self._global_rate_limiter.wait()

    def _raise_for_status(self, status: int, body_text: str) -> None:
        if status < 400:
            return
        message = clean_text(body_text)
        if status in (401, 403):
            raise ProviderError(f"HTTP {status}: {message}", ErrorKind.FATAL)
        if status in (419, 429, 503):
            raise ProviderError(f"HTTP {status}: {message}", ErrorKind.RATE_LIMITED)
        raise ProviderError(f"HTTP {status}: {message}", ErrorKind.PROVIDER_DOWN)

    async def _request_text(
        self,
        session: aiohttp.ClientSession,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Any] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, str]:
        if self.is_disabled():
            raise ProviderError("provider disabled", ErrorKind.FATAL)
        await self._wait_for_request()
        try:
            async with session.request(
                method.upper(),
                url,
                headers=headers,
                params=params,
                data=data,
                json=json_body,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as resp:
                text = await resp.text(errors="replace")
                status = resp.status
        except asyncio.TimeoutError:
            raise ProviderError("timeout", ErrorKind.RETRYABLE)
        except aiohttp.ClientError as exc:
            raise ProviderError(f"network error: {clean_text(exc)}", ErrorKind.RETRYABLE)

        self._raise_for_status(status, text)
        return status, text

    async def _request_json(
        self,
        session: aiohttp.ClientSession,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Any] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        status, raw_text = await self._request_text(
            session,
            method,
            url,
            headers=headers,
            params=params,
            data=data,
            json_body=json_body,
        )
        if not raw_text.strip():
            return status, {}
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            raise ProviderError(f"non-json response: {clean_text(raw_text)}", ErrorKind.PARSE_ERROR)
        if not isinstance(payload, dict):
            raise ProviderError(f"unexpected response type: {type(payload).__name__}", ErrorKind.PARSE_ERROR)
        return status, payload

    async def check_once(self, normalized_url: str, session: aiohttp.ClientSession) -> tuple[int, str]:
        raise NotImplementedError
