from __future__ import annotations

from pathlib import Path
from typing import Dict, Sequence, Tuple

from .models import CheckResult, UrlItem
from .utils import clean_text, now_iso



def write_output(items: Sequence[UrlItem], result_map: Dict[str, CheckResult], output_path: Path) -> None:
    rows: list[Tuple[int, str, CheckResult]] = []
    for item in items:
        result = result_map.get(item.normalized_url)
        if result is None:
            result = CheckResult(
                normalized_url=item.normalized_url,
                status_code=-1,
                provider="none",
                detail="missing result",
                checked_at=now_iso(),
            )
        rows.append((item.index, item.raw_url, result))

    rows.sort(key=lambda row: (0 if row[2].status_code == 200 else 1, row[0]))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        for _, raw_url, result in rows:
            provider = clean_text(result.provider, limit=80)
            detail = clean_text(result.detail, limit=800)
            latency_ms = max(0.0, float(result.latency_ms))
            f.write(f"{result.status_code}\t{raw_url}\t{provider}\t{latency_ms:.2f}\t{detail}\n")
