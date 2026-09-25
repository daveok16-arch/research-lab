"""Configuration loading for markets, sources and trade profiles.

Nothing in the application hardcodes Dallas–Fort Worth or HVAC. The active market and trade
are resolved from `config/markets.yaml` and `config/trades.yaml`, so a new market or trade is
a configuration change rather than an application rewrite.
"""

from __future__ import annotations

import re
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
class CityConfig:
    """A city inside a market. `slug` addresses it in URLs; `name` matches source values."""

    slug: str
    name: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CityConfig:
        return cls(slug=str(data["slug"]), name=str(data["name"]))


@dataclass
class MarketConfig:
    """A geographic market: a set of cities served by a set of sources and trades."""

    id: str
    slug: str
    name: str
    short_name: str
    state: str = "TX"
    active: bool = False
    trades: list[str] = field(default_factory=list)
    cities: list[CityConfig] = field(default_factory=list)
    landing_pages: list[dict[str, Any]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    seo: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MarketConfig:
        return cls(
            id=str(data["id"]),
            slug=str(data["slug"]),
            name=str(data["name"]),
            short_name=str(data.get("short_name") or data["name"]),
            state=str(data.get("state") or "TX"),
            active=bool(data.get("active", False)),
            trades=list(data.get("trades") or []),
            cities=[CityConfig.from_dict(c) for c in data.get("cities") or []],
            landing_pages=list(data.get("landing_pages") or []),
            sources=list(data.get("sources") or []),
            seo=dict(data.get("seo") or {}),
        )

    @property
    def city_names(self) -> list[str]:
        """City names as the permit sources record them, used for filtering."""
        return [c.name for c in self.cities]

    def city_slug(self, name: str | None) -> str | None:
        """Reverse lookup: city name to URL slug."""
        if not name:
            return None
        wanted = name.strip().lower()
        for city in self.cities:
            if city.name.lower() == wanted:
                return city.slug
        return None

    def city_name(self, slug: str | None) -> str | None:
        if not slug:
            return None
        wanted = slug.strip().lower()
        for city in self.cities:
            if city.slug.lower() == wanted:
                return city.name
        return None


def type_slug(project_type: str | None) -> str:
    """A URL slug for a project-type landing page.

    Kept here rather than in the application layer so a page URL and the value it filters on
    are derived by one rule. Deterministic and lossy on purpose: the slug addresses a page, and
    the page resolves the name back from the database, so a collision would surface as a page
    with two names rather than as a silent mis-filter.
    """
    if not project_type:
        return ""
    lowered = project_type.strip().lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")


@dataclass
class TradeConfig:
    id: str
    label: str
    active: bool
    market: dict[str, Any]
    slug: str = ""
    short_label: str = ""
    #: How the application layer scopes public discovery to this trade. Declares which stored
    #: field evidences the trade, so an HVAC directory cannot list a project with no
    #: mechanical activity. The classification gates are unaffected by this setting.
    discovery: dict[str, Any] = field(default_factory=dict)
    seo: dict[str, Any] = field(default_factory=dict)
    mechanical_permit_type_keywords: list[str] = field(default_factory=list)
    mechanical_scope_keywords: list[str] = field(default_factory=list)
    construction_activity_keywords: list[str] = field(default_factory=list)
    property_classes: list[dict[str, Any]] = field(default_factory=list)
    scoring: dict[str, Any] = field(default_factory=dict)
    thresholds: dict[str, Any] = field(default_factory=dict)
    active_status_keywords: list[str] = field(default_factory=list)
    inactive_status_keywords: list[str] = field(default_factory=list)
    residential_exclusion_keywords: list[str] = field(default_factory=list)

    @property
    def evidence_field(self) -> str | None:
        """The stored column that evidences this trade, or None when undeclared."""
        value = (self.discovery or {}).get("evidence_field")
        if not value:
            return None
        text = str(value)
        # The column name cannot be bound as a query parameter, so it is validated before it is
        # ever interpolated into SQL. This is the only identifier that reaches a query.
        if not re.fullmatch(r"[a-z_]+", text):
            raise ValueError(f"Invalid discovery evidence_field: {text!r}")
        return text

    @property
    def evidence_values(self) -> list[Any]:
        return list((self.discovery or {}).get("evidence_values") or [])

    @property
    def strong_evidence_value(self) -> Any:
        """The value denoting the strongest evidence tier, used for headline statistics."""
        return (self.discovery or {}).get("strong_evidence_value")

    def evidence_clause(self, alias: str = "p") -> tuple[str | None, list[Any]]:
        """SQL predicate selecting rows that carry this trade's evidence.

        Centralised so every layer — service, reports, sitemap — scopes by the same rule. A
        literal column name in any one of those places would be a hardcoded trade.
        """
        field = self.evidence_field
        values = self.evidence_values
        if not field or not values:
            return None, []
        placeholders = ",".join("?" for _ in values)
        return f"{alias}.{field} IN ({placeholders})", list(values)

    def strong_evidence_clause(self, alias: str = "p") -> tuple[str | None, list[Any]]:
        """SQL predicate selecting rows at the strongest evidence tier."""
        field = self.evidence_field
        value = self.strong_evidence_value
        if not field or value is None:
            return None, []
        return f"{alias}.{field} = ?", [value]

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


# --- markets ------------------------------------------------------------------


@lru_cache(maxsize=1)
def load_markets(path: Path | None = None) -> dict[str, MarketConfig]:
    """Load every configured market, keyed by id."""
    path = path or (CONFIG_DIR / "markets.yaml")
    raw = yaml.safe_load(path.read_text())
    return {
        m["id"]: MarketConfig.from_dict(m) for m in raw.get("markets") or []
    }


@lru_cache(maxsize=1)
def _active_market_id(path: Path | None = None) -> str | None:
    path = path or (CONFIG_DIR / "markets.yaml")
    raw = yaml.safe_load(path.read_text())
    return (raw.get("defaults") or {}).get("active_market")


def active_market() -> MarketConfig:
    """The market the application serves by default.

    Resolved from configuration rather than hardcoded, so switching markets is a config change.
    """
    markets = load_markets()
    configured = _active_market_id()
    if configured and configured in markets:
        return markets[configured]
    for market in markets.values():
        if market.active:
            return market
    raise RuntimeError("No active market found in config/markets.yaml")


def market_by_slug(slug: str) -> MarketConfig | None:
    for market in load_markets().values():
        if market.slug == slug:
            return market
    return None


def trade_by_slug(slug: str) -> TradeConfig | None:
    for trade in load_trades().values():
        if (trade.slug or trade.id) == slug:
            return trade
    return None


def active_trades() -> list[TradeConfig]:
    """Trades enabled anywhere. Only one is active in the MVP; the list is future-facing."""
    return [t for t in load_trades().values() if t.active]


# --- keyword map --------------------------------------------------------------


@dataclass
class KeywordEntry:
    """One keyword: the phrase, the intent behind it and the role it plays for its page."""

    phrase: str
    intent: str
    role: str
    reason: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KeywordEntry:
        return cls(
            phrase=str(data["phrase"]),
            intent=str(data.get("intent") or "commercial"),
            role=str(data.get("role") or "secondary"),
            reason=data.get("reason"),
        )


@dataclass
class KeywordPage:
    """A page in the keyword map, with the keywords it is allowed to target."""

    id: str
    path: str
    page_type: str
    intent: str
    title: str
    primary_keyword: str
    keywords: list[KeywordEntry] = field(default_factory=list)
    supporting: list[str] = field(default_factory=list)
    quality_gate: dict[str, int] = field(default_factory=dict)
    explained_on: str | None = None

    @property
    def primary_entries(self) -> list[KeywordEntry]:
        return [k for k in self.keywords if k.role == "primary"]

    @property
    def deferred(self) -> list[KeywordEntry]:
        return [k for k in self.keywords if k.role == "deferred"]

    def all_phrases(self) -> list[str]:
        return [k.phrase for k in self.keywords]


@dataclass
class KeywordMap:
    """The whole keyword-to-page map, loaded from configuration."""

    version: int
    intents: dict[str, str] = field(default_factory=dict)
    pages: list[KeywordPage] = field(default_factory=list)
    guide_topics: list[dict[str, Any]] = field(default_factory=list)

    def page(self, page_id: str) -> KeywordPage | None:
        return next((p for p in self.pages if p.id == page_id), None)

    def primary_claims(self) -> dict[str, str]:
        """Primary keyword phrase to the page id that claims it.

        Used to prove no two pages compete for one primary keyword. Returned as a mapping so a
        collision is visible as a shorter dict than the number of claims.
        """
        claims: dict[str, str] = {}
        for page in self.pages:
            for entry in page.primary_entries:
                claims.setdefault(entry.phrase, page.id)
        return claims

    def duplicate_primary_claims(self) -> list[tuple[str, list[str]]]:
        """Primary phrases claimed by more than one page, with the claiming page ids."""
        by_phrase: dict[str, list[str]] = {}
        for page in self.pages:
            for entry in page.primary_entries:
                by_phrase.setdefault(entry.phrase, []).append(page.id)
        return [(phrase, ids) for phrase, ids in by_phrase.items() if len(ids) > 1]

    def all_keywords(self) -> list[KeywordEntry]:
        return [k for page in self.pages for k in page.keywords]


@lru_cache(maxsize=1)
def load_keyword_map(path: Path | None = None) -> KeywordMap:
    """Load the keyword-to-page map.

    Loaded once and cached, like every other configuration file, so the SEO engine reads one
    consistent map for a process rather than re-parsing YAML per request.
    """
    path = path or (CONFIG_DIR / "keywords.yaml")
    raw = yaml.safe_load(path.read_text()) or {}
    pages: list[KeywordPage] = []
    for item in raw.get("pages") or []:
        pages.append(
            KeywordPage(
                id=str(item["id"]),
                path=str(item["path"]),
                page_type=str(item.get("page_type") or "hub"),
                intent=str(item.get("intent") or "informational"),
                title=str(item.get("title") or ""),
                primary_keyword=str(item.get("primary_keyword") or ""),
                keywords=[
                    KeywordEntry.from_dict(k) for k in item.get("keywords") or []
                ],
                supporting=list(item.get("supporting") or []),
                quality_gate=dict(item.get("quality_gate") or {}),
                explained_on=item.get("explained_on"),
            )
        )
    return KeywordMap(
        version=int(raw.get("version") or 1),
        intents=dict(raw.get("intents") or {}),
        pages=pages,
        guide_topics=list(raw.get("guide_topics") or []),
    )


def reset_config_cache() -> None:
    """Clear cached configuration. Used by tests that write temporary config files."""
    load_markets.cache_clear()
    _active_market_id.cache_clear()
    load_sources.cache_clear()
    load_trades.cache_clear()
    load_keyword_map.cache_clear()