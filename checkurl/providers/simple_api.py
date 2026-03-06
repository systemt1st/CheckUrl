from __future__ import annotations

from typing import List, Tuple

import aiohttp

from ..errors import ErrorKind, ProviderError
from ..utils import (
    clean_multiline_text,
    clean_text,
    flatten_header_items,
    safe_int,
)
from .base import DEFAULT_USER_AGENT, Provider


class XiarouProvider(Provider):
    ENDPOINT = "https://v.api.aa1.cn/api/api-web-code/index.php"

    def __init__(self, *, timeout: float, rps: float) -> None:
        super().__init__("xiarou", timeout=timeout, rps=rps)

    async def check_once(self, normalized_url: str, session: aiohttp.ClientSession) -> Tuple[int, str]:
        _, data = await self._request_json(
            session,
            "GET",
            self.ENDPOINT,
            params={"url": normalized_url},
        )
        status_code = safe_int(data.get("code"))
        if status_code is None:
            raise ProviderError("code missing", ErrorKind.PARSE_ERROR)
        return status_code, clean_text(data.get("msg") or "")


class HaikouLvxiaProvider(Provider):
    ENDPOINT = "https://api.lxurl.net/api/sitecode.php"

    def __init__(self, *, timeout: float, rps: float) -> None:
        super().__init__("haikou_luxia", timeout=timeout, rps=rps)

    async def check_once(self, normalized_url: str, session: aiohttp.ClientSession) -> Tuple[int, str]:
        _, data = await self._request_json(
            session,
            "GET",
            self.ENDPOINT,
            params={"url": normalized_url, "json": 1},
        )
        api_code = safe_int(data.get("code"))
        if api_code is not None and api_code != 200:
            raise ProviderError(clean_text(data.get("msg") or f"api code {api_code}"), ErrorKind.PROVIDER_DOWN)

        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        status_code = safe_int(payload.get("code"))
        if status_code is None:
            raise ProviderError("data.code missing", ErrorKind.PARSE_ERROR)
        detail = clean_text(payload.get("msg") or data.get("msg") or "")
        return status_code, detail


class ApihzProvider(Provider):
    ENDPOINT = "https://cn.apihz.cn/api/wangzhan/getcode.php"

    def __init__(self, api_id: str, api_key: str, region_type: str, *, timeout: float, rps: float) -> None:
        super().__init__("apihz", timeout=timeout, rps=rps)
        self.api_id = api_id
        self.api_key = api_key
        self.region_type = region_type

    async def check_once(self, normalized_url: str, session: aiohttp.ClientSession) -> Tuple[int, str]:
        _, data = await self._request_json(
            session,
            "GET",
            self.ENDPOINT,
            params={
                "id": self.api_id,
                "key": self.api_key,
                "type": self.region_type,
                "url": normalized_url,
            },
        )
        api_code = safe_int(data.get("code"))
        msg = clean_text(data.get("msg") or "")
        if api_code is None:
            raise ProviderError("code missing", ErrorKind.PARSE_ERROR)
        if api_code != 200:
            lower_msg = msg.lower()
            if any(token in lower_msg for token in ("秘钥", "密钥", "key", "用户id", "id错误", "id不存在", "权限")):
                raise ProviderError(msg or f"api code {api_code}", ErrorKind.FATAL)
            if "频" in msg and "限制" in msg:
                raise ProviderError(msg or f"api code {api_code}", ErrorKind.RATE_LIMITED)
            raise ProviderError(msg or f"api code {api_code}", ErrorKind.PROVIDER_DOWN)

        status_code = safe_int(msg)
        if status_code is None:
            raise ProviderError(f"invalid status code in msg: {msg or 'empty'}", ErrorKind.PARSE_ERROR)
        return status_code, f"region_type={self.region_type}"


class XianglianProvider(Provider):
    ENDPOINT = "https://openapi.chinaz.net/v1/1029/ping"

    def __init__(self, api_key: str, *, timeout: float, rps: float) -> None:
        super().__init__("xianglian", timeout=timeout, rps=rps)
        self.api_key = api_key

    async def check_once(self, normalized_url: str, session: aiohttp.ClientSession) -> Tuple[int, str]:
        _, data = await self._request_json(
            session,
            "GET",
            self.ENDPOINT,
            params={"url": normalized_url, "APIKey": self.api_key, "ChinazVer": "1.0"},
        )
        code = safe_int(data.get("code"))
        msg = clean_text(data.get("msg") or "")
        if code is None:
            raise ProviderError("code missing", ErrorKind.PARSE_ERROR)

        if code == 1001:
            return 200, msg or "域名正常"
        if code == 1002:
            return 0, msg or "域名异常"
        if code in (-202, -210):
            raise ProviderError(msg or f"api code {code}", ErrorKind.PROVIDER_DOWN)
        if code in (431, 432, 433, 434, 436, 437):
            raise ProviderError(msg or f"api code {code}", ErrorKind.FATAL)
        if code in (531, 532):
            raise ProviderError(msg or f"api code {code}", ErrorKind.RATE_LIMITED)
        return code, msg


class Ip33Provider(Provider):
    ENDPOINT = "https://api.ip33.com/httpstatus/search"

    def __init__(self, *, timeout: float, rps: float) -> None:
        super().__init__("ip33", timeout=timeout, rps=rps)

    async def check_once(self, normalized_url: str, session: aiohttp.ClientSession) -> Tuple[int, str]:
        _, data = await self._request_json(
            session,
            "POST",
            self.ENDPOINT,
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
            data={"url": normalized_url},
        )
        code = safe_int(data.get("code"))
        text_msg = clean_text(data.get("text") or data.get("description") or data.get("msg") or "")
        if code == 208 or "frequency out of limit" in text_msg.lower():
            raise ProviderError(text_msg or "rate limited", ErrorKind.RATE_LIMITED)

        state = data.get("state")
        if state is False:
            return (code if code is not None else 0), (text_msg or "state=false")
        if code is None:
            raise ProviderError("code missing", ErrorKind.PARSE_ERROR)

        ip_text = clean_text(data.get("ip") or "", limit=160)
        head_text = flatten_header_items(data.get("head"), limit=400)
        detail_parts: List[str] = []
        if text_msg:
            detail_parts.append(text_msg)
        if ip_text:
            detail_parts.append(f"ip={ip_text}")
        if head_text:
            detail_parts.append(head_text.splitlines()[0])
        return code, clean_text("; ".join(detail_parts) or "ok")


class La46Provider(Provider):
    ENDPOINT = "https://www.46.la/api/http.php"

    def __init__(self, *, timeout: float, rps: float) -> None:
        super().__init__("la46", timeout=timeout, rps=rps)

    async def check_once(self, normalized_url: str, session: aiohttp.ClientSession) -> Tuple[int, str]:
        _, data = await self._request_json(
            session,
            "GET",
            self.ENDPOINT,
            headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"},
            params={"domain": normalized_url},
        )
        if data.get("error"):
            raise ProviderError(clean_text(data.get("error")), ErrorKind.PROVIDER_DOWN)

        status_code = safe_int(data.get("zhuangtaima"))
        if status_code is None:
            status_code = safe_int(data.get("ret"))
        if status_code is None:
            raise ProviderError("status missing", ErrorKind.PARSE_ERROR)

        ip_text = clean_text(data.get("fuwuqiip") or "", limit=160)
        head_text = clean_multiline_text(data.get("HEAD") or "", limit=600)
        detail_parts: List[str] = []
        if ip_text:
            detail_parts.append(f"ip={ip_text}")
        if head_text:
            detail_parts.append(head_text.splitlines()[0])
        return status_code, clean_text("; ".join(detail_parts) or "ok")


class NullgoProvider(Provider):
    ENDPOINT = "https://www.nullgo.com/api/mix/http-status"

    def __init__(self, *, timeout: float, rps: float) -> None:
        super().__init__("nullgo", timeout=timeout, rps=rps)

    async def check_once(self, normalized_url: str, session: aiohttp.ClientSession) -> Tuple[int, str]:
        form = aiohttp.FormData()
        form.add_field("url", normalized_url)

        _, data = await self._request_json(
            session,
            "POST",
            self.ENDPOINT,
            headers={
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "*/*",
                "Origin": "https://www.nullgo.com",
                "Referer": "https://www.nullgo.com/web/http-status",
                "X-Requested-With": "XMLHttpRequest",
            },
            data=form,
        )

        code = safe_int(data.get("code"))
        msg = clean_text(data.get("msg") or "")
        if code is None:
            raise ProviderError("code missing", ErrorKind.PARSE_ERROR)
        if code != 200:
            retryable = code in (429, 503) or ("频" in msg and "限" in msg)
            raise ProviderError(msg or f"api code {code}", ErrorKind.RATE_LIMITED if retryable else ErrorKind.PROVIDER_DOWN)

        payload = data.get("data")
        if not isinstance(payload, dict):
            raise ProviderError("response data missing", ErrorKind.PARSE_ERROR)
        details = payload.get("details")
        if not isinstance(details, list) or not details:
            raise ProviderError("response data.details missing", ErrorKind.PARSE_ERROR)

        detail_item = details[0]
        if not isinstance(detail_item, dict):
            raise ProviderError("response data.details[0] invalid", ErrorKind.PARSE_ERROR)
        parsed = detail_item.get("parsed")
        if not isinstance(parsed, dict):
            raise ProviderError("response data.details[0].parsed missing", ErrorKind.PARSE_ERROR)

        status_node = parsed.get("status_code")
        status_value = status_node.get("value") if isinstance(status_node, dict) else status_node
        status_code = safe_int(status_value)
        if status_code is None:
            raise ProviderError("response parsed.status_code.value missing", ErrorKind.PARSE_ERROR)

        title = clean_text(payload.get("title") or "")
        header_text = clean_multiline_text(detail_item.get("header") or "", limit=600)
        server_ip = ""
        if isinstance(parsed.get("server_ip"), dict):
            server_ip = clean_text(parsed["server_ip"].get("value") or "", limit=160)
        total_time = ""
        if isinstance(parsed.get("total_time"), dict):
            total_time = clean_text(parsed["total_time"].get("value") or "", limit=80)

        detail_parts: List[str] = []
        if title:
            detail_parts.append(title)
        if msg and msg != title:
            detail_parts.append(msg)
        if server_ip:
            detail_parts.append(f"ip={server_ip}")
        if total_time:
            detail_parts.append(f"time={total_time}")
        if header_text:
            detail_parts.append(header_text.splitlines()[0])
        return status_code, clean_text("; ".join(detail_parts) or "ok")
