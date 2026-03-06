from __future__ import annotations

from typing import Callable, Dict, List, Sequence, Tuple

from ..models import AppConfig, ProviderConfig
from .base import Provider
from .boce import BoceProvider
from .cjzzc import CjzzcProvider
from .simple_api import (
    ApihzProvider,
    HaikouLvxiaProvider,
    Ip33Provider,
    La46Provider,
    NullgoProvider,
    XianglianProvider,
    XiarouProvider,
)
from .simple_html import ChinazToolProvider, FulimamaProvider
from .smallseotools import SmallSeoToolsProvider


ProviderBuilder = Callable[[AppConfig, ProviderConfig], Tuple[Provider | None, str]]


def _to_str(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _normalize_node_ids(value: object) -> Sequence[str]:
    if isinstance(value, (list, tuple)):
        ids = [str(item).strip() for item in value if str(item).strip()]
        if ids:
            return ids
    return ("81173", "81022", "81100")


def _simple_builder(provider_cls: type[Provider]) -> ProviderBuilder:
    def _build(config: AppConfig, provider_cfg: ProviderConfig) -> Tuple[Provider | None, str]:
        provider = provider_cls(timeout=config.runtime.timeout, rps=provider_cfg.rps)
        return provider, ""

    return _build


def _build_apihz(config: AppConfig, provider_cfg: ProviderConfig) -> Tuple[Provider | None, str]:
    api_id = _to_str(provider_cfg.options.get("id"))
    api_key = _to_str(provider_cfg.options.get("key"))
    region_type = _to_str(provider_cfg.options.get("type") or "1") or "1"
    if not api_id or not api_key:
        return None, "apihz(缺少 id/key)"
    return (
        ApihzProvider(api_id, api_key, region_type, timeout=config.runtime.timeout, rps=provider_cfg.rps),
        "",
    )


def _build_xianglian(config: AppConfig, provider_cfg: ProviderConfig) -> Tuple[Provider | None, str]:
    api_key = _to_str(provider_cfg.options.get("api_key"))
    if not api_key:
        return None, "xianglian(缺少 api_key)"
    return XianglianProvider(api_key, timeout=config.runtime.timeout, rps=provider_cfg.rps), ""


def _build_boce(config: AppConfig, provider_cfg: ProviderConfig) -> Tuple[Provider | None, str]:
    node_ids = _normalize_node_ids(provider_cfg.options.get("node_ids"))
    return BoceProvider(node_ids, timeout=config.runtime.timeout, rps=provider_cfg.rps), ""


PROVIDER_BUILDERS: Dict[str, ProviderBuilder] = {
    "apihz": _build_apihz,
    "xiarou": _simple_builder(XiarouProvider),
    "haikou_luxia": _simple_builder(HaikouLvxiaProvider),
    "xianglian": _build_xianglian,
    "ip33": _simple_builder(Ip33Provider),
    "la46": _simple_builder(La46Provider),
    "fulimama": _simple_builder(FulimamaProvider),
    "chinaz_tool": _simple_builder(ChinazToolProvider),
    "nullgo": _simple_builder(NullgoProvider),
    "cjzzc": _simple_builder(CjzzcProvider),
    "boce": _build_boce,
    "smallseotools": _simple_builder(SmallSeoToolsProvider),
}


def build_providers(config: AppConfig) -> Tuple[List[Provider], List[str]]:
    providers: List[Provider] = []
    skipped: List[str] = []

    for provider_cfg in config.providers:
        builder = PROVIDER_BUILDERS.get(provider_cfg.name)
        if builder is None:
            skipped.append(f"{provider_cfg.name}(无构建器)")
            continue

        provider, reason = builder(config, provider_cfg)
        if provider is None:
            skipped.append(reason or f"{provider_cfg.name}(构建失败)")
            continue
        providers.append(provider)

    return providers, skipped
