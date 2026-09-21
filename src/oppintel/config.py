"""Configuration loading for sources and trade profiles."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
DATA_DIR = REPO_ROOT / "data"


@dataclass
class SourceConfig:
    id: str
    name: str
    publisher: str
    kind: str
    enabled: bool = True
    market_coverage: str = "unknown"
    coverage_note: str | None = None
    base_url: str | None = None
    domain: str | None = None
    dataset_id: str | None = None
    portal_url: str | None = None
    source_url_template: str | None = None
    jurisdiction_city: str | None = None
    jurisdiction_state: str = "TX"
    reliability: float = 0.8
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceConfig:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def is_current(self) -> bool:
        return self.market_coverage == "current"


@dataclass
class TradeConfig:
    id: str
    label: str
    active: bool
    market: dict[str, Any]
    mechanical_permit_type_keywords: list[str] = field(default_factory=list)
    mechanical_scope_keywords: list[str] = field(default_factory=list)
    construction_activity_keywords: list[str] = field(default_factory=list)
    property_classes: list[dict[str, Any]] = field(default_factory=list)
    scoring: dict[str, Any] = field(default_factory=dict)
    thresholds: dict[str, Any] = field(default_factory=dict)
    active_status_keywords: list[str] = field(default_factory=list)
    inactive_status_keywords: list[str] = field(default_factory=list)
    residential_exclusion_keywords: list[str] = field(default_factory=list)

    def property_class_for(self, text: str) -> dict[str, Any] | None:
        """Return the highest-scoring property class matching the given text.

        Longer keyword matches win so that "assisted living" is preferred over the bare
        "living" style of accidental substring hit, and the most specific class is used.
        """
        if not text:
            return None
        haystack = text.lower()
        best: dict[str, Any] | None = None
        best_len = 0
        for klass in self.property_classes:
            for keyword in klass.get("keywords", []):
                if keyword in haystack and len(keyword) > best_len:
                    best = klass
                    best_len = len(keyword)
        return best


@lru_cache(maxsize=1)
def load_sources(path: Path | None = None) -> dict[str, SourceConfig]:
    path = path or (CONFIG_DIR / "sources.yaml")
    raw = yaml.safe_load(path.read_text())
    return {s["id"]: SourceConfig.from_dict(s) for s in raw["sources"]}


@lru_cache(maxsize=1)
def load_trades(path: Path | None = None) -> dict[str, TradeConfig]:
    path = path or (CONFIG_DIR / "trades.yaml")
    raw = yaml.safe_load(path.read_text())
    trades: dict[str, TradeConfig] = {}
    for trade_id, data in raw["trades"].items():
        known = {f for f in TradeConfig.__dataclass_fields__}
        trades[trade_id] = TradeConfig(
            id=trade_id, **{k: v for k, v in data.items() if k in known}
        )
    return trades


def active_trade() -> TradeConfig:
    for trade in load_trades().values():
        if trade.active:
            return trade
    raise RuntimeError("No active trade profile found in config/trades.yaml")