from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Optional

import aiohttp

from .config import DEFAULT_CONFIG_FILE, load_app_config
from .dispatcher import DispatchController, check_with_fallback
from .errors import ProviderError
from .logging_utils import configure_logging, log_event
from .models import AppConfig, CheckResult
from .output import write_output
from .providers import build_providers
from .rate_limiter import AsyncRateLimiter
from .stats import StatsCollector
from .utils import clean_text, normalize_url, now_iso, parse_input_urls, unique_urls


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="异步批量 URL 状态检测（多 provider fallback）")
    parser.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG_FILE,
        help=f"YAML 配置文件路径，默认 {DEFAULT_CONFIG_FILE}",
    )
    parser.add_argument("--dry-run", action="store_true", help="仅验证配置和输入，不发请求")
    parser.add_argument("--test-provider", default="", help="单独测试某个 provider")
    parser.add_argument("--url", default="", help="配合 --test-provider 使用")
    return parser


def _load_config_or_exit(config_path: Path) -> AppConfig:
    try:
        return load_app_config(config_path)
    except Exception as exc:
        print(f"[error] 配置文件读取失败: {clean_text(exc)}", file=sys.stderr)
        if not config_path.exists():
            print(f"[hint] 请先创建配置文件: {config_path}", file=sys.stderr)
        raise SystemExit(1)


def _validate_input_or_exit(config: AppConfig) -> list:
    input_path = Path(config.input)
    if not input_path.exists():
        print(f"[error] 输入文件不存在: {input_path}", file=sys.stderr)
        raise SystemExit(1)

    items = parse_input_urls(input_path)
    if not items:
        print("[error] 输入文件没有可处理 URL", file=sys.stderr)
        raise SystemExit(1)
    return items


def run_dry_run(config: AppConfig) -> int:
    logger = configure_logging(config.runtime.log_level, config.runtime.log_file)
    items = _validate_input_or_exit(config)
    providers, skipped = build_providers(config)
    if skipped:
        log_event(logger, "providers_skipped", skipped=skipped)
    if not providers:
        print("[error] 没有可用 provider", file=sys.stderr)
        return 1

    log_event(
        logger,
        "dry_run_ok",
        input=config.input,
        output=config.output,
        total_urls=len(items),
        providers=[provider.name for provider in providers],
    )
    return 0


def _bind_global_rate_limiter(providers: list, global_rps: float) -> None:
    limiter = AsyncRateLimiter(global_rps)
    for provider in providers:
        provider.set_global_rate_limiter(limiter)


async def run_test_provider(config: AppConfig, provider_name: str, url: str) -> int:
    logger = configure_logging(config.runtime.log_level, config.runtime.log_file)
    providers, skipped = build_providers(config)
    if skipped:
        log_event(logger, "providers_skipped", skipped=skipped)

    provider = next((item for item in providers if item.name == provider_name), None)
    if provider is None:
        print(f"[error] provider 未启用或不可用: {provider_name}", file=sys.stderr)
        return 1

    _bind_global_rate_limiter(providers, config.runtime.global_max_requests_per_second)

    normalized_url = normalize_url(url)
    timeout = aiohttp.ClientTimeout(total=config.runtime.timeout)
    async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.CookieJar()) as session:
        try:
            started = time.perf_counter()
            status_code, detail = await provider.check_once(normalized_url, session)
            latency_ms = (time.perf_counter() - started) * 1000
        except ProviderError as exc:
            print(f"[error] {provider.name}: {clean_text(exc)}", file=sys.stderr)
            return 1

    result = CheckResult(
        normalized_url=normalized_url,
        status_code=status_code,
        provider=provider.name,
        detail=detail,
        checked_at=now_iso(),
        latency_ms=round(latency_ms, 2),
    )
    print(f"{result.status_code}\t{url}\t{result.provider}\t{result.latency_ms:.2f}\t{result.detail}")
    return 0


async def run_batch(config: AppConfig) -> int:
    logger = configure_logging(config.runtime.log_level, config.runtime.log_file)
    items = _validate_input_or_exit(config)
    providers, skipped = build_providers(config)
    if skipped:
        log_event(logger, "providers_skipped", skipped=skipped)
    if not providers:
        print("[error] 没有可用 provider", file=sys.stderr)
        return 1

    _bind_global_rate_limiter(providers, config.runtime.global_max_requests_per_second)

    return await run_batch_with_mode(config, items=items, providers=providers, logger=logger)


async def run_batch_with_mode(
    config: AppConfig,
    *,
    items: list,
    providers: list,
    logger,
) -> int:
    output_path = Path(config.output)
    result_map: dict[str, CheckResult] = {}
    stats = StatsCollector()
    dispatch_controller = DispatchController(
        providers,
        order_strategy=config.runtime.provider_order_strategy,
        cooldown_seconds=config.runtime.provider_cooldown_seconds,
        cooldown_backoff_factor=config.runtime.provider_cooldown_backoff_factor,
        max_cooldown_seconds=config.runtime.provider_cooldown_max_seconds,
        rate_limit_threshold=config.runtime.provider_rate_limit_threshold,
        max_requests_per_run=config.runtime.provider_max_requests_per_run,
        max_requests_per_minute=config.runtime.provider_max_requests_per_minute,
    )

    pending_urls = unique_urls(items)
    total = len(pending_urls)
    done = 0

    log_event(
        logger,
        "run_start",
        total_input=len(items),
        total_unique=total,
        pending=len(pending_urls),
        provider_order_strategy=config.runtime.provider_order_strategy,
        providers=[provider.name for provider in providers],
    )

    if pending_urls:
        timeout = aiohttp.ClientTimeout(total=config.runtime.timeout)
        connector = aiohttp.TCPConnector(limit=max(config.runtime.concurrency * 2, 100))
        async with aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
            cookie_jar=aiohttp.CookieJar(),
        ) as session:
            progress_lock = asyncio.Lock()
            batch_size = config.runtime.batch_size if config.runtime.batch_size > 0 else len(pending_urls)
            total_batches = (len(pending_urls) + batch_size - 1) // batch_size

            for batch_index, start in enumerate(range(0, len(pending_urls), batch_size), start=1):
                batch_urls = pending_urls[start : start + batch_size]
                if total_batches > 1:
                    log_event(
                        logger,
                        "batch_start",
                        batch_index=batch_index,
                        batch_total=total_batches,
                        batch_size=len(batch_urls),
                    )

                queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
                for url in batch_urls:
                    queue.put_nowait(url)

                worker_count = min(config.runtime.concurrency, len(batch_urls))
                for _ in range(worker_count):
                    queue.put_nowait(None)

                async def worker() -> None:
                    nonlocal done
                    while True:
                        url = await queue.get()
                        if url is None:
                            queue.task_done()
                            break

                        try:
                            try:
                                result = await check_with_fallback(
                                    url,
                                    session,
                                    dispatch_controller=dispatch_controller,
                                    max_retries=config.runtime.max_retries,
                                    retry_backoff=config.runtime.retry_backoff,
                                    retry_jitter=config.runtime.retry_jitter,
                                    stats=stats,
                                    logger=logger,
                                )
                            except Exception as exc:
                                result = CheckResult(
                                    normalized_url=url,
                                    status_code=-1,
                                    provider="none",
                                    detail=clean_text(f"worker error: {exc}"),
                                    checked_at=now_iso(),
                                    latency_ms=0.0,
                                )

                            result_map[url] = result
                            stats.record_result(result)

                            async with progress_lock:
                                done += 1
                                if done % config.runtime.progress_every == 0 or done == total:
                                    log_event(logger, "progress", done=done, total=total)
                        finally:
                            queue.task_done()

                workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
                await queue.join()
                await asyncio.gather(*workers)

                if batch_index < total_batches and config.runtime.batch_cooldown > 0:
                    log_event(
                        logger,
                        "batch_cooldown",
                        batch_index=batch_index,
                        sleep_seconds=round(config.runtime.batch_cooldown, 2),
                    )
                    await asyncio.sleep(config.runtime.batch_cooldown)

    write_output(items, result_map, output_path)
    summary = stats.summary()
    log_event(
        logger,
        "run_done",
        output=str(output_path),
        summary=summary,
    )
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    config = _load_config_or_exit(Path(args.config))

    if args.dry_run and args.test_provider:
        print("[error] --dry-run 与 --test-provider 不能同时使用", file=sys.stderr)
        return 1

    if args.dry_run:
        return run_dry_run(config)

    if args.test_provider:
        if not args.url:
            print("[error] 使用 --test-provider 时必须提供 --url", file=sys.stderr)
            return 1
        return asyncio.run(run_test_provider(config, args.test_provider.strip().lower(), args.url.strip()))

    return asyncio.run(run_batch(config))


if __name__ == "__main__":
    raise SystemExit(main())
