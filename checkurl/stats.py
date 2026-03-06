from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, Optional

from .models import CheckResult
from .utils import percentile


@dataclass
class ProviderStats:
    attempts: int = 0
    success: int = 0
    failure: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    errors: Counter[str] = field(default_factory=Counter)


class StatsCollector:
    def __init__(self) -> None:
        self.providers: Dict[str, ProviderStats] = {}
        self.final_total = 0
        self.final_success = 0
        self.final_failed = 0

    def record_attempt(
        self,
        provider: str,
        latency_ms: float,
        *,
        success: bool,
        error_kind: Optional[str] = None,
    ) -> None:
        stat = self.providers.setdefault(provider, ProviderStats())
        stat.attempts += 1
        stat.latencies_ms.append(latency_ms)
        if success:
            stat.success += 1
            return
        stat.failure += 1
        if error_kind:
            stat.errors[error_kind] += 1

    def record_result(self, result: CheckResult) -> None:
        self.final_total += 1
        if result.status_code == -1:
            self.final_failed += 1
        else:
            self.final_success += 1

    def summary(self) -> Dict[str, object]:
        provider_summary: Dict[str, Dict[str, object]] = {}
        for name, stat in self.providers.items():
            provider_summary[name] = {
                "attempts": stat.attempts,
                "success": stat.success,
                "failure": stat.failure,
                "avg_ms": round(sum(stat.latencies_ms) / len(stat.latencies_ms), 2)
                if stat.latencies_ms
                else 0.0,
                "p95_ms": round(percentile(stat.latencies_ms, 95), 2),
                "errors": dict(stat.errors),
            }

        success_rate = (self.final_success / self.final_total * 100.0) if self.final_total else 0.0
        return {
            "total": self.final_total,
            "success": self.final_success,
            "failed": self.final_failed,
            "success_rate": round(success_rate, 2),
            "providers": provider_summary,
        }
