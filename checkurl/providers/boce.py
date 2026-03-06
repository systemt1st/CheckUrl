from __future__ import annotations

import asyncio
import json
import time
from typing import Dict, List, Optional, Sequence, Tuple

import aiohttp

from ..errors import ErrorKind, ProviderError
from ..utils import clean_text, find_html_failure_message, safe_int
from .base import DEFAULT_USER_AGENT, Provider


class BoceProvider(Provider):
    CHECK_ENDPOINT = "https://www.boce.com/http_batch_check"
    WS_ENDPOINT = "wss://www.boce.com/websocket/task_batch"
    FAILURE_MARKERS = (
        "验证码",
        "滑块",
        "波点",
        "访问频繁",
        "登录",
    )

    def __init__(self, node_ids: Sequence[str], *, timeout: float, rps: float) -> None:
        super().__init__("boce", timeout=timeout, rps=rps)
        self.node_ids = tuple(str(node) for node in node_ids)

    @staticmethod
    def _extract_task_tokens(raw_html: str) -> List[str]:
        import ast
        import re

        token_candidates: List[str] = []
        patterns = (
            r"task_token\s*=\s*(\[[^\]]*\])",
            r'"task_token"\s*:\s*(\[[^\]]*\])',
            r"'task_token'\s*:\s*(\[[^\]]*\])",
            r"task_token\s*=\s*['\"]([^'\"]+)['\"]",
            r'"task_token"\s*:\s*"([^"]+)"',
        )

        for pattern in patterns:
            for match in re.finditer(pattern, raw_html, re.S):
                token_candidates.append(match.group(1).strip())

        if not token_candidates:
            return []

        tokens: List[str] = []
        for candidate in token_candidates:
            loaded = None
            for parser in (json.loads, ast.literal_eval):
                try:
                    loaded = parser(candidate)
                    break
                except Exception:
                    continue

            if loaded is None:
                loaded = candidate

            if isinstance(loaded, str):
                token_text = loaded.strip()
                if token_text:
                    tokens.append(token_text)
                continue

            if isinstance(loaded, list):
                for item in loaded:
                    item_text = str(item).strip()
                    if item_text:
                        tokens.append(item_text)

        deduped: List[str] = []
        seen = set()
        for token in tokens:
            if token in seen:
                continue
            seen.add(token)
            deduped.append(token)
        return deduped

    @staticmethod
    def _pick_status_code(status_codes: Sequence[int]) -> Optional[int]:
        if not status_codes:
            return None
        valid_http = [code for code in status_codes if 100 <= code <= 599]
        if valid_http:
            counts: Dict[int, int] = {}
            for code in valid_http:
                counts[code] = counts.get(code, 0) + 1
            max_count = max(counts.values())
            for code in valid_http:
                if counts[code] == max_count:
                    return code
        if 0 in status_codes:
            return 0
        return status_codes[0]

    async def check_once(self, normalized_url: str, session: aiohttp.ClientSession) -> Tuple[int, str]:
        form_data: List[Tuple[str, str]] = [
            ("type", "http"),
            ("random", "batch_checker"),
            ("host[]", normalized_url),
            ("create_task", "1"),
            ("node_type", ""),
            ("http_resolve", ""),
            ("get_token", "1"),
            ("batch", "true"),
        ]
        for node_id in self.node_ids:
            form_data.append(("node_ids[]", node_id))
        form_data.extend([("captcha", ""), ("_token", "")])

        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Origin": "https://www.boce.com",
            "Referer": "https://www.boce.com/http_batch",
        }
        _, raw_html = await self._request_text(
            session,
            "POST",
            self.CHECK_ENDPOINT,
            headers=headers,
            data=form_data,
        )

        task_tokens = self._extract_task_tokens(raw_html)
        if not task_tokens:
            failure = find_html_failure_message(raw_html, self.FAILURE_MARKERS)
            if failure:
                if any(keyword in failure for keyword in ("验证码", "滑块", "波点", "登录")):
                    raise ProviderError(failure, ErrorKind.FATAL)
                if "频繁" in failure:
                    raise ProviderError(failure, ErrorKind.RATE_LIMITED)
                raise ProviderError(failure, ErrorKind.PROVIDER_DOWN)
            raise ProviderError("task token missing", ErrorKind.PARSE_ERROR)

        status_codes: List[int] = []
        ip_candidates: List[str] = []
        last_message = ""

        try:
            await self._wait_for_request()
            async with session.ws_connect(
                self.WS_ENDPOINT,
                timeout=self.timeout,
                headers={
                    "Origin": "https://www.boce.com",
                    "User-Agent": DEFAULT_USER_AGENT,
                },
            ) as ws:
                for token in task_tokens:
                    await ws.send_str(json.dumps({"task_token": token}, ensure_ascii=False))

                deadline = time.monotonic() + self.timeout
                finished = 0
                while time.monotonic() < deadline:
                    remaining = max(0.2, deadline - time.monotonic())
                    try:
                        message = await ws.receive(timeout=remaining)
                    except asyncio.TimeoutError:
                        break

                    if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                        break

                    if message.type == aiohttp.WSMsgType.BINARY:
                        raw_message = message.data.decode("utf-8", errors="replace")
                    elif message.type == aiohttp.WSMsgType.TEXT:
                        raw_message = message.data
                    else:
                        continue

                    last_message = clean_text(raw_message, limit=240)
                    try:
                        payload = json.loads(raw_message)
                    except json.JSONDecodeError:
                        continue

                    code = safe_int(payload.get("code"))
                    msg = clean_text(payload.get("message") or "")

                    if code == 100:
                        continue
                    if code == 102:
                        finished += 1
                        if finished >= len(task_tokens):
                            break
                        continue
                    if code == -3:
                        raise ProviderError(msg or "boce permission error", ErrorKind.FATAL)
                    if code is not None and code not in (0, 100, 102):
                        raise ProviderError(msg or f"boce ws code {code}", ErrorKind.PROVIDER_DOWN)

                    if code == 0 and isinstance(payload.get("data"), list):
                        for item in payload["data"]:
                            if not isinstance(item, dict):
                                continue
                            http_code = safe_int(item.get("httpCode"))
                            if http_code is not None:
                                status_codes.append(http_code)
                            ip_text = clean_text(item.get("ip") or "", limit=120)
                            if ip_text:
                                ip_candidates.append(ip_text)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"boce websocket error: {clean_text(exc)}", ErrorKind.RETRYABLE)

        picked_status = self._pick_status_code(status_codes)
        if picked_status is None:
            if last_message:
                if "频繁" in last_message or "limit" in last_message.lower():
                    raise ProviderError(last_message, ErrorKind.RATE_LIMITED)
                raise ProviderError(f"boce result missing: {last_message}", ErrorKind.PROVIDER_DOWN)
            raise ProviderError("boce result missing", ErrorKind.RETRYABLE)

        detail_parts: List[str] = []
        if ip_candidates:
            detail_parts.append(f"ip={ip_candidates[0]}")
        detail_parts.append(f"nodes={len(status_codes)}")
        return picked_status, clean_text("; ".join(detail_parts) or "ok")
