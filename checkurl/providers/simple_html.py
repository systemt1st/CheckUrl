from __future__ import annotations

from typing import List, Tuple

import aiohttp

from ..errors import ErrorKind, ProviderError
from ..utils import (
    clean_text,
    extract_html_labeled_text,
    extract_status_code_from_html,
    find_html_failure_message,
)
from .base import DEFAULT_USER_AGENT, Provider


class FulimamaProvider(Provider):
    ENDPOINT = "https://www.fulimama.com/webstatus/"
    FAILURE_MARKERS = (
        "网页状态检测失败",
        "域名未解析",
        "网站无法访问",
        "输入网址",
    )

    def __init__(self, *, timeout: float, rps: float) -> None:
        super().__init__("fulimama", timeout=timeout, rps=rps)

    async def check_once(self, normalized_url: str, session: aiohttp.ClientSession) -> Tuple[int, str]:
        _, raw_html = await self._request_text(
            session,
            "POST",
            self.ENDPOINT,
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Origin": "https://www.fulimama.com",
                "Referer": self.ENDPOINT,
            },
            data={"url": normalized_url},
        )

        status_code = extract_status_code_from_html(raw_html)
        if status_code is None:
            failure = find_html_failure_message(raw_html, self.FAILURE_MARKERS)
            if failure:
                return 0, failure
            raise ProviderError("status code missing", ErrorKind.PARSE_ERROR)

        ip_text = extract_html_labeled_text(raw_html, "服务器IP", limit=200)
        head_text = extract_html_labeled_text(raw_html, "网页返回HEAD信息", limit=600)
        detail_parts: List[str] = []
        if ip_text:
            detail_parts.append(f"ip={ip_text}")
        if head_text:
            detail_parts.append(head_text.splitlines()[0])
        return status_code, clean_text("; ".join(detail_parts) or "ok")


class ChinazToolProvider(Provider):
    ENDPOINT = "https://tool.chinaz.com/pagestatus"
    FAILURE_MARKERS = (
        "检测异常",
        "无法访问",
        "没有发现你要找的页面",
        "访问过于频繁",
        "验证码",
    )

    def __init__(self, *, timeout: float, rps: float) -> None:
        super().__init__("chinaz_tool", timeout=timeout, rps=rps)

    async def check_once(self, normalized_url: str, session: aiohttp.ClientSession) -> Tuple[int, str]:
        _, raw_html = await self._request_text(
            session,
            "GET",
            self.ENDPOINT,
            headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "text/html"},
            params={"url": normalized_url},
        )

        status_code = extract_status_code_from_html(raw_html)
        if status_code is None:
            failure = find_html_failure_message(raw_html, self.FAILURE_MARKERS)
            if failure:
                return 0, failure
            raise ProviderError("status code missing", ErrorKind.PARSE_ERROR)

        ip_text = extract_html_labeled_text(raw_html, "服务器IP", limit=200)
        head_text = extract_html_labeled_text(raw_html, "网页返回HEAD信息", limit=600)
        detail_parts: List[str] = []
        if ip_text:
            detail_parts.append(f"ip={ip_text}")
        if head_text:
            detail_parts.append(head_text.splitlines()[0])
        return status_code, clean_text("; ".join(detail_parts) or "ok")
