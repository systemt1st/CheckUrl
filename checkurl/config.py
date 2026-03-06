from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Mapping

import yaml

from .models import AppConfig, ProviderConfig, RuntimeConfig
from .utils import clean_text, safe_int


DEFAULT_INPUT_FILE = "urls.txt"
DEFAULT_OUTPUT_FILE = "result.txt"
DEFAULT_CONFIG_FILE = "config.yaml"

VALID_PROVIDERS = (
    "apihz",
    "xiarou",
    "haikou_luxia",
    "xianglian",
    "ip33",
    "la46",
    "fulimama",
    "chinaz_tool",
    "nullgo",
    "cjzzc",
    "boce",
    "smallseotools",
)

DEFAULT_RUNTIME: Dict[str, Any] = {
    "concurrency": 10,
    "global_max_requests_per_second": 3.0,
    "timeout": 12.0,
    "max_retries": 0,
    "retry_backoff": 2.0,
    "retry_jitter": 0.25,
    "progress_every": 50,
    "batch_size": 300,
    "batch_cooldown": 60.0,
    "provider_order_strategy": "round_robin",
    "provider_cooldown_seconds": 120.0,
    "provider_cooldown_backoff_factor": 2.0,
    "provider_cooldown_max_seconds": 1800.0,
    "provider_rate_limit_threshold": 2,
    "provider_max_requests_per_run": 0,
    "provider_max_requests_per_minute": 0,
    "log_level": "INFO",
    "log_file": "",
}

DEFAULT_PROVIDERS: Dict[str, Dict[str, Any]] = {
    "apihz": {"enabled": True, "priority": 10, "rps": 1.0, "id": "", "key": "", "type": "1"},
    "xiarou": {"enabled": True, "priority": 20, "rps": 1.0},
    "haikou_luxia": {"enabled": True, "priority": 30, "rps": 1.0},
    "xianglian": {"enabled": True, "priority": 40, "rps": 0.8, "api_key": ""},
    "ip33": {"enabled": True, "priority": 50, "rps": 1.0},
    "la46": {"enabled": True, "priority": 60, "rps": 1.0},
    "fulimama": {"enabled": True, "priority": 70, "rps": 0.8},
    "chinaz_tool": {"enabled": True, "priority": 80, "rps": 0.5},
    "nullgo": {"enabled": True, "priority": 90, "rps": 0.5},
    "cjzzc": {"enabled": True, "priority": 100, "rps": 0.5},
    "boce": {"enabled": True, "priority": 110, "rps": 0.3, "node_ids": ["81173", "81022", "81100"]},
    "smallseotools": {"enabled": True, "priority": 120, "rps": 0.3},
}


def to_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def parse_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"{field_name} 必须是布尔值")


def positive_int(value: Any, field_name: str) -> int:
    parsed = safe_int(value)
    if parsed is None or parsed <= 0:
        raise ValueError(f"{field_name} 必须 > 0")
    return parsed


def non_negative_int(value: Any, field_name: str) -> int:
    parsed = safe_int(value)
    if parsed is None or parsed < 0:
        raise ValueError(f"{field_name} 必须 >= 0")
    return parsed


def positive_float(value: Any, field_name: str) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是数字") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name} 必须 > 0")
    return parsed


def non_negative_float(value: Any, field_name: str) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是数字") from exc
    if parsed < 0:
        raise ValueError(f"{field_name} 必须 >= 0")
    return parsed


def _normalize_order_strategy(value: Any) -> str:
    normalized = to_str(value, "round_robin").lower()
    allowed = {"priority", "round_robin", "url_hash", "adaptive"}
    if normalized not in allowed:
        raise ValueError("runtime.provider_order_strategy 必须是 priority/round_robin/url_hash/adaptive")
    return normalized


def _resolve_path(path_text: str, base_dir: Path) -> str:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return str(path)


def load_yaml_mapping(config_path: Path) -> Dict[str, Any]:
    if not config_path.exists():
        raise FileNotFoundError(f"配置文件不存在: {config_path}")
    try:
        with config_path.open("r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML 解析失败: {clean_text(exc)}") from exc

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError("YAML 顶层必须是键值映射")
    return dict(loaded)


def _validate_top_level(raw: Mapping[str, Any]) -> None:
    allowed = {"input", "output", "runtime", "providers"}
    unknown = sorted(set(raw.keys()) - allowed)
    if unknown:
        raise ValueError(f"存在未知顶层配置项: {', '.join(unknown)}")


def _load_runtime(runtime_raw: Any) -> RuntimeConfig:
    if runtime_raw is None:
        runtime_raw = {}
    if not isinstance(runtime_raw, dict):
        raise ValueError("runtime 必须是键值映射")

    unknown = sorted(set(runtime_raw.keys()) - set(DEFAULT_RUNTIME.keys()))
    if unknown:
        raise ValueError(f"runtime 存在未知配置项: {', '.join(unknown)}")

    merged = dict(DEFAULT_RUNTIME)
    merged.update(runtime_raw)

    retry_jitter = non_negative_float(merged["retry_jitter"], "runtime.retry_jitter")
    if retry_jitter > 1:
        raise ValueError("runtime.retry_jitter 必须在 0~1 之间")

    cooldown_backoff_factor = positive_float(
        merged["provider_cooldown_backoff_factor"],
        "runtime.provider_cooldown_backoff_factor",
    )
    if cooldown_backoff_factor < 1:
        raise ValueError("runtime.provider_cooldown_backoff_factor 必须 >= 1")

    cooldown_max_seconds = non_negative_float(
        merged["provider_cooldown_max_seconds"],
        "runtime.provider_cooldown_max_seconds",
    )

    return RuntimeConfig(
        concurrency=positive_int(merged["concurrency"], "runtime.concurrency"),
        global_max_requests_per_second=non_negative_float(
            merged["global_max_requests_per_second"],
            "runtime.global_max_requests_per_second",
        ),
        timeout=positive_float(merged["timeout"], "runtime.timeout"),
        max_retries=non_negative_int(merged["max_retries"], "runtime.max_retries"),
        retry_backoff=positive_float(merged["retry_backoff"], "runtime.retry_backoff"),
        retry_jitter=retry_jitter,
        progress_every=positive_int(merged["progress_every"], "runtime.progress_every"),
        batch_size=non_negative_int(merged["batch_size"], "runtime.batch_size"),
        batch_cooldown=non_negative_float(merged["batch_cooldown"], "runtime.batch_cooldown"),
        provider_order_strategy=_normalize_order_strategy(merged["provider_order_strategy"]),
        provider_cooldown_seconds=non_negative_float(
            merged["provider_cooldown_seconds"],
            "runtime.provider_cooldown_seconds",
        ),
        provider_cooldown_backoff_factor=cooldown_backoff_factor,
        provider_cooldown_max_seconds=cooldown_max_seconds,
        provider_rate_limit_threshold=non_negative_int(
            merged["provider_rate_limit_threshold"],
            "runtime.provider_rate_limit_threshold",
        ),
        provider_max_requests_per_run=non_negative_int(
            merged["provider_max_requests_per_run"],
            "runtime.provider_max_requests_per_run",
        ),
        provider_max_requests_per_minute=non_negative_int(
            merged["provider_max_requests_per_minute"],
            "runtime.provider_max_requests_per_minute",
        ),
        log_level=to_str(merged["log_level"], "INFO") or "INFO",
        log_file=to_str(merged["log_file"]),
    )


def _load_providers(providers_raw: Any) -> List[ProviderConfig]:
    if providers_raw is None:
        providers_raw = {}
    if not isinstance(providers_raw, dict):
        raise ValueError("providers 必须是键值映射")

    unknown_names = sorted(set(providers_raw.keys()) - set(VALID_PROVIDERS))
    if unknown_names:
        raise ValueError(f"providers 含未知 provider: {', '.join(unknown_names)}")

    configs: List[ProviderConfig] = []
    provider_index = {name: idx for idx, name in enumerate(VALID_PROVIDERS)}

    for name in VALID_PROVIDERS:
        default_cfg = dict(DEFAULT_PROVIDERS[name])
        override = providers_raw.get(name, {})
        if override is None:
            override = {}
        if not isinstance(override, dict):
            raise ValueError(f"providers.{name} 必须是键值映射")

        unknown_keys = sorted(set(override.keys()) - set(default_cfg.keys()))
        if unknown_keys:
            raise ValueError(f"providers.{name} 存在未知配置项: {', '.join(unknown_keys)}")

        merged = dict(default_cfg)
        merged.update(override)

        enabled = parse_bool(merged["enabled"], f"providers.{name}.enabled")
        priority = non_negative_int(merged["priority"], f"providers.{name}.priority")
        rps = positive_float(merged["rps"], f"providers.{name}.rps")

        options = {k: v for k, v in merged.items() if k not in {"enabled", "priority", "rps"}}
        options["_provider_index"] = provider_index[name]

        configs.append(
            ProviderConfig(name=name, enabled=enabled, priority=priority, rps=rps, options=options)
        )

    selected = [cfg for cfg in configs if cfg.enabled]
    if not selected:
        raise ValueError("没有启用任何 provider")

    selected.sort(key=lambda cfg: (cfg.priority, int(cfg.options.get("_provider_index", 0))))

    if any(cfg.name == "smallseotools" for cfg in selected):
        selected = [cfg for cfg in selected if cfg.name != "smallseotools"] + [
            cfg for cfg in selected if cfg.name == "smallseotools"
        ]

    normalized: List[ProviderConfig] = []
    for cfg in selected:
        options = dict(cfg.options)
        options.pop("_provider_index", None)
        normalized.append(
            ProviderConfig(
                name=cfg.name,
                enabled=cfg.enabled,
                priority=cfg.priority,
                rps=cfg.rps,
                options=options,
            )
        )
    return normalized


def load_app_config(config_path: Path) -> AppConfig:
    raw = load_yaml_mapping(config_path)
    _validate_top_level(raw)

    base_dir = config_path.resolve().parent
    runtime = _load_runtime(raw.get("runtime"))
    providers = _load_providers(raw.get("providers"))

    if runtime.log_file:
        runtime = replace(
            runtime,
            log_file=_resolve_path(runtime.log_file, base_dir),
        )

    input_path = _resolve_path(to_str(raw.get("input"), DEFAULT_INPUT_FILE) or DEFAULT_INPUT_FILE, base_dir)
    output_path = _resolve_path(to_str(raw.get("output"), DEFAULT_OUTPUT_FILE) or DEFAULT_OUTPUT_FILE, base_dir)

    return AppConfig(
        input=input_path,
        output=output_path,
        runtime=runtime,
        providers=providers,
    )
