from __future__ import annotations

import datetime as dt
import html
import re
import urllib.parse
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence

from .models import UrlItem


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def safe_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        if isinstance(value, bool):
            return int(value)
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def clean_text(value: Any, limit: int = 240) -> str:
    text = str(value) if value is not None else ""
    text = text.replace("\n", " ").replace("\r", " ").replace("\t", " ").strip()
    return text[:limit]


def clean_multiline_text(value: Any, limit: int = 1600) -> str:
    text = str(value) if value is not None else ""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    return "\n".join(lines)[:limit]


def html_fragment_to_text(fragment: str, *, limit: int = 1600) -> str:
    if not fragment:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", fragment)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return clean_multiline_text(html.unescape(text), limit=limit)


def _normalize_lookup_text(value: str) -> str:
    return re.sub(r"\s+", "", value).strip().lower()


def _extract_labeled_text_from_plain_text(raw_html: str, label: str, *, limit: int) -> str:
    plain_text = html_fragment_to_text(raw_html, limit=max(limit * 3, 3200))
    lines = [line.strip() for line in plain_text.splitlines() if line.strip()]
    if not lines:
        return ""

    normalized_label = _normalize_lookup_text(label)
    for index, line in enumerate(lines):
        normalized_line = _normalize_lookup_text(line)
        if normalized_label not in normalized_line:
            continue

        for splitter in ("：", ":"):
            if splitter in line:
                right = line.split(splitter, 1)[1].strip()
                if right:
                    return clean_multiline_text(right, limit=limit)

        if index + 1 < len(lines):
            candidate = lines[index + 1].strip()
            if candidate and normalized_label not in _normalize_lookup_text(candidate):
                return clean_multiline_text(candidate, limit=limit)
    return ""


def extract_html_labeled_text(raw_html: str, label: str, *, limit: int = 1600) -> str:
    escaped = re.escape(label)
    patterns = [
        rf"<(?:td|div)[^>]*>\s*{escaped}\s*</(?:td|div)>\s*<(?:td|div)[^>]*>(.*?)</(?:td|div)>",
        rf"{escaped}\s*</(?:td|div)>\s*<(?:td|div)[^>]*>(.*?)</(?:td|div)>",
    ]
    for pattern in patterns:
        match = re.search(pattern, raw_html, re.S)
        if match:
            return html_fragment_to_text(match.group(1), limit=limit)

    label_norm = _normalize_lookup_text(label)
    for row_match in re.finditer(r"(?is)<tr[^>]*>(.*?)</tr>", raw_html):
        row_html = row_match.group(1)
        cells = re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", row_html)
        if len(cells) < 2:
            continue

        normalized_cells = [html_fragment_to_text(cell, limit=max(limit, 200)) for cell in cells]
        for index, cell_text in enumerate(normalized_cells[:-1]):
            if label_norm in _normalize_lookup_text(cell_text):
                return clean_multiline_text(normalized_cells[index + 1], limit=limit)

    fallback = _extract_labeled_text_from_plain_text(raw_html, label, limit=limit)
    if fallback:
        return fallback
    return ""


def extract_status_code_from_html(raw_html: str) -> Optional[int]:
    labels = (
        "返回状态码",
        "HTTP状态码",
        "状态码",
        "Status Code",
        "HTTP Status",
    )
    for label in labels:
        text = extract_html_labeled_text(raw_html, label, limit=240)
        if not text:
            continue
        match = re.search(r"\b([1-5]\d{2})\b", text)
        if match:
            return safe_int(match.group(1))

    attr_match = re.search(
        r'(?is)data-(?:status(?:-?code)?|http(?:-?status)?(?:-?code)?)\s*=\s*["\']?([1-5]\d{2})',
        raw_html,
    )
    if attr_match:
        return safe_int(attr_match.group(1))

    plain_text = html_fragment_to_text(raw_html, limit=6000)
    for marker in ("返回状态码", "状态码", "HTTP状态码", "Status Code", "HTTP Status"):
        match = re.search(rf"(?is){re.escape(marker)}[^\d]{{0,24}}([1-5]\d{{2}})", plain_text)
        if match:
            return safe_int(match.group(1))

    fallback = re.search(r"(?is)(?:返回状态码|status\s*code|http\s*status)[^\d]{0,24}([1-5]\d{2})", raw_html)
    if fallback:
        return safe_int(fallback.group(1))
    return None


def find_html_failure_message(raw_html: str, markers: Sequence[str]) -> str:
    plain_text = html_fragment_to_text(raw_html, limit=6000)
    plain_text_lower = plain_text.lower()
    for marker in markers:
        if not marker:
            continue
        marker_lower = marker.lower()
        if marker in raw_html or marker in plain_text or marker_lower in plain_text_lower:
            return marker
    return ""


def flatten_header_items(items: Any, *, limit: int = 600) -> str:
    if not isinstance(items, list):
        return ""
    lines: List[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            lines.append(f"{key}: {value}")
    return clean_multiline_text("\n".join(lines), limit=limit)


def normalize_url(raw_url: str) -> str:
    parsed = urllib.parse.urlparse(raw_url)
    if parsed.scheme:
        return raw_url
    return f"http://{raw_url}"


def parse_input_urls(input_path: Path) -> List[UrlItem]:
    items: List[UrlItem] = []
    with input_path.open("r", encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            items.append(
                UrlItem(
                    index=len(items),
                    raw_url=raw,
                    normalized_url=normalize_url(raw),
                )
            )
    return items


def unique_urls(items: Sequence[UrlItem]) -> List[str]:
    seen = set()
    output: List[str] = []
    for item in items:
        if item.normalized_url in seen:
            continue
        seen.add(item.normalized_url)
        output.append(item.normalized_url)
    return output


def percentile(values: Iterable[float], p: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if p <= 0:
        return ordered[0]
    if p >= 100:
        return ordered[-1]
    rank = (len(ordered) - 1) * (p / 100.0)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    fraction = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * fraction
