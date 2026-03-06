from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, List, Tuple

import aiohttp

from ..errors import ErrorKind, ProviderError
from ..utils import clean_text, safe_int
from .base import DEFAULT_USER_AGENT, Provider


class CjzzcProvider(Provider):
    NODES = (
        "http://tt1.cjzzc.com",
        "http://tt2.cjzzc.com",
        "http://tt1.yywy.cn",
        "http://tt2.yywy.cn",
        "http://tt3.yywy.cn",
        "http://tt4.yywy.cn",
    )
    ENDPOINT_PATH = "/tools/http_code"
    REQUEST_TYPE = "1"
    RSA_MODULUS_HEX = (
        "93651E9911C85DDF63DCB10C9390D013A4751BD288576339FF6D8387C329ABC3"
        "2222E69149AB29D7C8146A6B8B96B93A26620D8D1588EBF5ABD1423074830972"
        "26C41ECB3CBD325A47030AD7DC210D43205AAA5396DD3E91C648BDBEED6EFA14"
        "2C695B8686D8216494FA5AA01B9DDC7D26D7180186556BA79BAD6D64CFCAF5AD"
    )
    RSA_EXPONENT_HEX = "10001"

    def __init__(self, *, timeout: float, rps: float) -> None:
        super().__init__("cjzzc", timeout=timeout, rps=rps)
        self._next_node = 0
        self._node_lock = asyncio.Lock()

    @classmethod
    def _rsa_encrypt_hex(cls, raw_text: str) -> str:
        modulus = int(cls.RSA_MODULUS_HEX, 16)
        exponent = int(cls.RSA_EXPONENT_HEX, 16)
        key_bytes = (modulus.bit_length() + 7) // 8

        message = raw_text.encode("utf-8")
        if len(message) > key_bytes - 11:
            raise ProviderError("url too long for rsa encryption", ErrorKind.PARSE_ERROR)

        padding_length = key_bytes - len(message) - 3
        padding = bytearray()
        while len(padding) < padding_length:
            chunk = os.urandom(padding_length - len(padding))
            for byte in chunk:
                if byte == 0:
                    continue
                padding.append(byte)
                if len(padding) >= padding_length:
                    break

        block = b"\x00\x02" + bytes(padding) + b"\x00" + message
        encrypted_int = pow(int.from_bytes(block, "big"), exponent, modulus)
        return f"{encrypted_int:0{key_bytes * 2}x}"

    @classmethod
    def _build_url_param(cls, normalized_url: str) -> str:
        if len(normalized_url) < 88:
            return cls._rsa_encrypt_hex(normalized_url)
        return normalized_url

    @staticmethod
    def _parse_jsonp_payload(raw_text: str) -> Dict[str, Any]:
        payload_text = raw_text.strip()
        if not payload_text:
            raise ProviderError("empty response", ErrorKind.PARSE_ERROR)

        left = payload_text.find("(")
        right = payload_text.rfind(")")
        if left < 0 or right <= left:
            raise ProviderError(f"invalid jsonp response: {clean_text(payload_text)}", ErrorKind.PARSE_ERROR)

        raw_json = payload_text[left + 1 : right].strip()
        try:
            payload = json.loads(raw_json)
        except json.JSONDecodeError:
            raise ProviderError(f"invalid json payload: {clean_text(raw_json)}", ErrorKind.PARSE_ERROR)
        if not isinstance(payload, dict):
            raise ProviderError(f"unexpected response type: {type(payload).__name__}", ErrorKind.PARSE_ERROR)
        return payload

    async def _next_start_index(self) -> int:
        async with self._node_lock:
            index = self._next_node
            self._next_node = (self._next_node + 1) % len(self.NODES)
        return index

    async def check_once(self, normalized_url: str, session: aiohttp.ClientSession) -> Tuple[int, str]:
        encoded_url = self._build_url_param(normalized_url)
        callback = f"cjzzc_cb_{int(time.time() * 1000)}"
        start_index = await self._next_start_index()
        errors: List[str] = []

        headers = {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "*/*",
            "Referer": "http://www.cjzzc.com/",
        }

        for offset in range(len(self.NODES)):
            node = self.NODES[(start_index + offset) % len(self.NODES)]
            endpoint = f"{node}{self.ENDPOINT_PATH}"
            params = {
                "callback": callback,
                "type": self.REQUEST_TYPE,
                "url": encoded_url,
                "rd": f"{time.time():.6f}",
                "_": str(int(time.time() * 1000)),
            }

            try:
                _, raw_text = await self._request_text(session, "GET", endpoint, headers=headers, params=params)
                data = self._parse_jsonp_payload(raw_text)
            except ProviderError as exc:
                errors.append(f"{node}:{clean_text(exc)}")
                continue

            status_code = safe_int(data.get("code"))
            if status_code is None:
                raise ProviderError("response code missing", ErrorKind.PARSE_ERROR)

            detail_parts: List[str] = []
            title_text = clean_text(data.get("title") or data.get("msg") or "")
            if title_text:
                detail_parts.append(title_text)
            redirected_url = clean_text(data.get("_url") or data.get("url") or "", limit=200)
            if redirected_url:
                detail_parts.append(f"url={redirected_url}")
            return status_code, clean_text("; ".join(detail_parts) or "ok")

        raise ProviderError(clean_text("; ".join(errors) or "all cjzzc nodes failed", limit=500), ErrorKind.RETRYABLE)
