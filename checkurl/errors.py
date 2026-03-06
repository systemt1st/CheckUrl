from __future__ import annotations

from enum import Enum


class ErrorKind(str, Enum):
    RETRYABLE = "retryable"
    RATE_LIMITED = "rate_limited"
    PROVIDER_DOWN = "provider_down"
    FATAL = "fatal"
    PARSE_ERROR = "parse_error"


class ProviderError(Exception):
    def __init__(self, message: str, kind: ErrorKind = ErrorKind.PROVIDER_DOWN) -> None:
        super().__init__(message)
        self.kind = kind

    @property
    def retryable(self) -> bool:
        return self.kind in {ErrorKind.RETRYABLE, ErrorKind.RATE_LIMITED}

    @property
    def fatal(self) -> bool:
        return self.kind == ErrorKind.FATAL
