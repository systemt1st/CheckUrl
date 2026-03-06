from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List


@dataclass(frozen=True)
class UrlItem:
    index: int
    raw_url: str
    normalized_url: str


@dataclass
class CheckResult:
    normalized_url: str
    status_code: int
    provider: str
    detail: str
    checked_at: str
    latency_ms: float = 0.0

    def to_record(self) -> Dict[str, Any]:
        return {
            "normalized_url": self.normalized_url,
            "status_code": self.status_code,
            "provider": self.provider,
            "detail": self.detail,
            "checked_at": self.checked_at,
            "latency_ms": round(float(self.latency_ms), 2),
        }

    @classmethod
    def from_record(cls, record: Dict[str, Any]) -> "CheckResult":
        status_raw = record.get("status_code")
        try:
            status_code = int(str(status_raw).strip()) if status_raw is not None else -1
        except (TypeError, ValueError):
            status_code = -1

        latency_raw = record.get("latency_ms")
        try:
            latency_ms = float(str(latency_raw).strip()) if latency_raw is not None else 0.0
        except (TypeError, ValueError):
            latency_ms = 0.0

        return cls(
            normalized_url=str(record.get("normalized_url") or ""),
            status_code=status_code,
            provider=str(record.get("provider") or "none"),
            detail=str(record.get("detail") or ""),
            checked_at=str(record.get("checked_at") or ""),
            latency_ms=latency_ms,
        )


@dataclass(frozen=True)
class RuntimeConfig:
    concurrency: int
    global_max_requests_per_second: float
    timeout: float
    max_retries: int
    retry_backoff: float
    retry_jitter: float
    progress_every: int
    batch_size: int
    batch_cooldown: float
    provider_order_strategy: str
    provider_cooldown_seconds: float
    provider_cooldown_backoff_factor: float
    provider_cooldown_max_seconds: float
    provider_rate_limit_threshold: int
    provider_max_requests_per_run: int
    provider_max_requests_per_minute: int
    log_level: str
    log_file: str


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    enabled: bool
    priority: int
    rps: float
    options: Dict[str, Any]


@dataclass(frozen=True)
class AppConfig:
    input: str
    output: str
    runtime: RuntimeConfig
    providers: List[ProviderConfig]

    @property
    def input_path(self) -> Path:
        return Path(self.input)

    @property
    def output_path(self) -> Path:
        return Path(self.output)
