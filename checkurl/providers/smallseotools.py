from __future__ import annotations

import asyncio
import base64
import dataclasses
import re
import time
import urllib.parse
from typing import Dict, List, Optional, Sequence, Tuple

import aiohttp
from yarl import URL

from ..errors import ErrorKind, ProviderError
from ..utils import clean_text, html_fragment_to_text, safe_int
from .base import DEFAULT_USER_AGENT, Provider


@dataclasses.dataclass(frozen=True)
class SmallSeoToolsContext:
    csrf_token: str
    site_key: str
    recaptcha_version: str
    co_param: str
    expires_at: float


class SmallSeoToolsProvider(Provider):
    PAGE_URL = "https://smallseotools.com/zh/check-server-status/"
    RECAPTCHA_API_JS = "https://www.google.com/recaptcha/api.js?hl=zh"
    RECAPTCHA_ANCHOR_ENDPOINT = "https://www.google.com/recaptcha/api2/anchor"
    RECAPTCHA_RELOAD_ENDPOINT = "https://www.google.com/recaptcha/api2/reload"
    CONTEXT_TTL_SECONDS = 90.0
    RECAPTCHA_VERSION_TTL_SECONDS = 900.0

    def __init__(self, *, timeout: float, rps: float) -> None:
        super().__init__("smallseotools", timeout=timeout, rps=rps)
        self._context_lock = asyncio.Lock()
        self._cached_context: Optional[SmallSeoToolsContext] = None
        self._recaptcha_lock = asyncio.Lock()
        self._recaptcha_version = ""
        self._recaptcha_version_expire_at = 0.0

    @staticmethod
    def _build_co_param(page_url: str) -> str:
        parsed = urllib.parse.urlparse(page_url)
        host = parsed.hostname or ""
        if not parsed.scheme or not host:
            raise ProviderError("invalid smallseotools page url", ErrorKind.FATAL)

        default_port = 443 if parsed.scheme == "https" else 80
        origin = f"{parsed.scheme}://{host}:{parsed.port or default_port}"
        encoded = base64.urlsafe_b64encode(origin.encode("utf-8")).decode("ascii").rstrip("=")
        return f"{encoded}."

    @staticmethod
    def _extract_context_tokens(raw_html: str) -> Tuple[str, str]:
        csrf_token = ""
        csrf_patterns = (
            r'name=["\']_token["\'][^>]*value=["\']([^"\']+)["\']',
            r'value=["\']([^"\']+)["\'][^>]*name=["\']_token["\']',
            r'<meta[^>]*name=["\']csrf-token["\'][^>]*content=["\']([^"\']+)["\']',
            r'"_token"\s*:\s*"([^"]+)"',
        )
        for pattern in csrf_patterns:
            csrf_match = re.search(pattern, raw_html, re.I)
            if csrf_match:
                csrf_token = csrf_match.group(1).strip()
                if csrf_token:
                    break

        if not csrf_token:
            raise ProviderError("smallseotools csrf token missing", ErrorKind.RETRYABLE)

        site_key = ""
        site_key_patterns = (
            r'data-sitekey=["\']([^"\']+)["\']',
            r'"sitekey"\s*[:=]\s*["\']([^"\']+)["\']',
            r'render=([0-9A-Za-z_-]{20,})',
            r'\bk\s*[:=]\s*["\']([0-9A-Za-z_-]{20,})["\']',
        )
        for pattern in site_key_patterns:
            site_key_match = re.search(pattern, raw_html, re.I)
            if site_key_match:
                site_key = site_key_match.group(1).strip()
                if site_key:
                    break

        if not site_key:
            raise ProviderError("smallseotools recaptcha sitekey missing", ErrorKind.RETRYABLE)

        return csrf_token, site_key

    @staticmethod
    def _extract_result_rows(raw_html: str) -> List[Tuple[int, str]]:
        rows: List[Tuple[int, str]] = []
        seen = set()

        def append_row(status_raw: str, url_raw: str) -> None:
            status_code = safe_int(status_raw)
            if status_code is None:
                return
            row_url = clean_text(html_fragment_to_text(url_raw, limit=400), limit=400)
            key = (status_code, row_url)
            if key in seen:
                return
            seen.add(key)
            rows.append(key)

        pattern = re.compile(
            r'<div[^>]*class="[^"]*box_w2[^"]*"[^>]*>(?P<url>.*?)</div>\s*'
            r'<div[^>]*class="[^"]*box_w3[^"]*"[^>]*>\s*(?P<code>\d{3})\s*</div>',
            re.S | re.I,
        )
        for match in pattern.finditer(raw_html):
            append_row(match.group("code"), match.group("url"))

        if rows:
            return rows

        for row_match in re.finditer(r"(?is)<tr[^>]*>(.*?)</tr>", raw_html):
            row_html = row_match.group(1)
            row_text = html_fragment_to_text(row_html, limit=800)
            code_match = re.search(r"\b([1-5]\d{2})\b", row_text)
            if not code_match:
                continue

            url_match = re.search(
                r"(https?://[^\s<]+|(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s]*)?)",
                row_text,
                re.I,
            )
            if not url_match:
                continue

            append_row(code_match.group(1), url_match.group(1))

        if rows:
            return rows

        loose_pattern = re.compile(
            r"(?is)(https?://[^\s\"'<>]+|(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s\"'<>]*)?).{0,260}?\b([1-5]\d{2})\b"
        )
        for match in loose_pattern.finditer(raw_html):
            append_row(match.group(2), match.group(1))

        return rows

    @staticmethod
    def _normalize_compare_url(raw_url: str) -> str:
        text = clean_text(raw_url, limit=400)
        if not text:
            return ""
        if "://" not in text:
            text = f"http://{text}"
        parsed = urllib.parse.urlsplit(text)
        host = parsed.netloc.lower()
        path = parsed.path.rstrip("/") or "/"
        query = f"?{parsed.query}" if parsed.query else ""
        return f"{host}{path}{query}"

    async def _request_text_raw(
        self,
        session: aiohttp.ClientSession,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, str]] = None,
        data: Optional[Dict[str, str]] = None,
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
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as resp:
                text = await resp.text(errors="replace")
                status = resp.status
        except asyncio.TimeoutError:
            raise ProviderError("timeout", ErrorKind.RETRYABLE)
        except aiohttp.ClientError as exc:
            raise ProviderError(f"network error: {clean_text(exc)}", ErrorKind.RETRYABLE)

        if status >= 400:
            msg = clean_text(text)
            if status in (401, 403):
                raise ProviderError(f"HTTP {status}: {msg}", ErrorKind.FATAL)
            if status in (419, 429, 503):
                raise ProviderError(f"HTTP {status}: {msg}", ErrorKind.RATE_LIMITED)
            raise ProviderError(f"HTTP {status}: {msg}", ErrorKind.PROVIDER_DOWN)

        return status, text

    async def _resolve_recaptcha_version(self, session: aiohttp.ClientSession, *, force_refresh: bool) -> str:
        now = time.monotonic()
        async with self._recaptcha_lock:
            if not force_refresh and self._recaptcha_version and now < self._recaptcha_version_expire_at:
                return self._recaptcha_version

            _, script_text = await self._request_text_raw(
                session,
                "GET",
                self.RECAPTCHA_API_JS,
                headers={
                    "User-Agent": DEFAULT_USER_AGENT,
                    "Accept": "application/javascript, */*; q=0.8",
                    "Referer": self.PAGE_URL,
                },
            )

            match = re.search(r"releases/([^/]+)/", script_text)
            if not match:
                raise ProviderError("smallseotools recaptcha version missing", ErrorKind.RETRYABLE)

            self._recaptcha_version = match.group(1).strip()
            self._recaptcha_version_expire_at = now + self.RECAPTCHA_VERSION_TTL_SECONDS
            return self._recaptcha_version

    async def _bootstrap_context(
        self,
        session: aiohttp.ClientSession,
        *,
        force_refresh: bool,
    ) -> SmallSeoToolsContext:
        _, page_html = await self._request_text_raw(
            session,
            "GET",
            self.PAGE_URL,
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )

        csrf_token, site_key = self._extract_context_tokens(page_html)
        cookies = session.cookie_jar.filter_cookies(URL(self.PAGE_URL))
        if not cookies:
            raise ProviderError("smallseotools session cookie missing", ErrorKind.RETRYABLE)

        recaptcha_version = await self._resolve_recaptcha_version(session, force_refresh=force_refresh)
        return SmallSeoToolsContext(
            csrf_token=csrf_token,
            site_key=site_key,
            recaptcha_version=recaptcha_version,
            co_param=self._build_co_param(self.PAGE_URL),
            expires_at=time.monotonic() + self.CONTEXT_TTL_SECONDS,
        )

    async def _get_context(
        self,
        session: aiohttp.ClientSession,
        *,
        force_refresh: bool,
    ) -> SmallSeoToolsContext:
        now = time.monotonic()
        if not force_refresh:
            async with self._context_lock:
                if self._cached_context and now < self._cached_context.expires_at:
                    return self._cached_context

        context = await self._bootstrap_context(session, force_refresh=force_refresh)
        async with self._context_lock:
            self._cached_context = context
        return context

    async def _invalidate_context(self) -> None:
        async with self._context_lock:
            self._cached_context = None

    async def _fetch_recaptcha_response(
        self,
        session: aiohttp.ClientSession,
        context: SmallSeoToolsContext,
    ) -> str:
        anchor_url = (
            f"{self.RECAPTCHA_ANCHOR_ENDPOINT}?"
            f"{urllib.parse.urlencode({'ar': '1', 'k': context.site_key, 'co': context.co_param, 'hl': 'zh', 'v': context.recaptcha_version, 'size': 'invisible', 'cb': str(int(time.time() * 1000))})}"
        )
        _, anchor_html = await self._request_text_raw(
            session,
            "GET",
            anchor_url,
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Referer": self.PAGE_URL,
                "Accept": "text/html, */*; q=0.8",
            },
        )

        anchor_token_match = re.search(r'id="recaptcha-token"\s+value="([^"]+)"', anchor_html)
        if not anchor_token_match:
            raise ProviderError("smallseotools recaptcha anchor token missing", ErrorKind.RETRYABLE)

        reload_url = f"{self.RECAPTCHA_RELOAD_ENDPOINT}?{urllib.parse.urlencode({'k': context.site_key})}"
        _, reload_payload = await self._request_text_raw(
            session,
            "POST",
            reload_url,
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Referer": anchor_url,
                "Origin": "https://www.google.com",
                "Accept": "*/*",
            },
            data={
                "v": context.recaptcha_version,
                "reason": "q",
                "c": anchor_token_match.group(1),
                "k": context.site_key,
                "co": context.co_param,
                "hl": "zh",
                "size": "invisible",
            },
        )

        recaptcha_response = ""
        for pattern in (
            r'"rresp"\s*,\s*"([^"]+)"',
            r'"rresp"\s*:\s*"([^"]+)"',
            r'\["rresp"\s*,\s*"([^"]+)"\]',
        ):
            response_match = re.search(pattern, reload_payload)
            if response_match:
                recaptcha_response = response_match.group(1)
                break

        if not recaptcha_response:
            raise ProviderError("smallseotools recaptcha response missing", ErrorKind.RETRYABLE)
        return recaptcha_response

    @classmethod
    def _pick_result_row(cls, rows: Sequence[Tuple[int, str]], normalized_url: str) -> Tuple[int, str]:
        if len(rows) == 1:
            return rows[0]
        expected = cls._normalize_compare_url(normalized_url)
        for row in rows:
            if cls._normalize_compare_url(row[1]) == expected:
                return row
        return rows[0]

    async def check_once(self, normalized_url: str, session: aiohttp.ClientSession) -> Tuple[int, str]:
        if self.is_disabled():
            raise ProviderError("provider disabled", ErrorKind.FATAL)

        last_error: Optional[ProviderError] = None
        for attempt in range(2):
            force_refresh = attempt > 0
            try:
                context = await self._get_context(session, force_refresh=force_refresh)
                recaptcha_response = await self._fetch_recaptcha_response(session, context)

                _, raw_html = await self._request_text_raw(
                    session,
                    "POST",
                    self.PAGE_URL,
                    headers={
                        "User-Agent": DEFAULT_USER_AGENT,
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Content-Type": "application/x-www-form-urlencoded",
                        "Origin": "https://smallseotools.com",
                        "Referer": self.PAGE_URL,
                    },
                    data={
                        "_token": context.csrf_token,
                        "urls": normalized_url,
                        "g-recaptcha-response": recaptcha_response,
                    },
                )

                result_rows = self._extract_result_rows(raw_html)
                if not result_rows:
                    raise ProviderError(
                        "smallseotools result row missing (captcha/session may be invalid)",
                        ErrorKind.RETRYABLE,
                    )

                status_code, returned_url = self._pick_result_row(result_rows, normalized_url)
                detail = clean_text(f"url={returned_url}" if returned_url else "ok")
                return status_code, detail
            except ProviderError as exc:
                last_error = exc
                if attempt == 0 and exc.retryable and not exc.fatal:
                    await self._invalidate_context()
                    continue
                raise

        if last_error:
            raise last_error
        raise ProviderError("smallseotools check failed", ErrorKind.RETRYABLE)
