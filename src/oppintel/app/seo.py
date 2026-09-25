"""SEO metadata, sitemap and robots.

Three rules govern everything here:

1. **Never invent a fact to fill a metadata field.** If a project has no city, the description
   omits it rather than guessing. A meta description is a public claim like any other.
2. **Only canonical pages are indexable.** Filtered or paged result sets get `noindex` and a
   canonical pointing at the clean directory, because thousands of near-duplicate pages hurt
   rather than help ranking.
3. **Structured data must be factually supportable.** Only markup the record actually
   justifies is emitted; where a field is unknown it is omitted from the JSON-LD entirely,
   since schema.org has no "unknown" value.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from urllib.parse import urlencode

from ..config import MarketConfig, TradeConfig
from .config import AppConfig


@dataclass
class Seo:
    """Everything a `<head>` needs, resolved before rendering."""

    title: str
    description: str
    canonical: str = ""
    robots: str = "index,follow"
    og_type: str = "website"
    og_image: str | None = None
    json_ld: list[dict[str, Any]] = field(default_factory=list)
    noindex: bool = False

    @property
    def robots_directive(self) -> str:
        return "noindex,nofollow" if self.noindex else self.robots


class SeoBuilder:
    """Builds metadata from configured market/trade and real database values."""

    def __init__(self, cfg: AppConfig, market: MarketConfig, trade: TradeConfig):
        self.cfg = cfg
        # Stored with a leading underscore so they cannot shadow a method. An earlier version
        # assigned `self.market` and `self.trade`, which silently replaced the market and trade
        # page builders with config objects.
        self._market = market
        self._trade = trade

    @property
    def market(self) -> MarketConfig:
        return self._market

    @property
    def trade(self) -> TradeConfig:
        return self._trade

    # --- url helpers ----------------------------------------------------------

    def url(self, path: str) -> str:
        """Absolute URL for canonical and Open Graph tags.

        Falls back to a relative path when no BASE_URL is configured, which is correct for a
        local run and honest about not knowing the public origin. A guessed domain would put a
        wrong canonical on every page.
        """
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.cfg.base_url}{path}" if self.cfg.base_url else path

    def _site_name(self) -> str:
        return f"{self.market.short_name} Construction Opportunity Intelligence"

    # --- pages ----------------------------------------------------------------

    def home(self, stats: dict[str, Any]) -> Seo:
        count = stats.get("projects_public", 0)
        mechanical = stats.get("with_mechanical", 0)
        # Distinct from the market+trade landing page ("... Opportunities"), because two
        # indexable pages sharing a title compete with each other in search results.
        title = (
            f"{self.market.short_name} Construction Opportunity Intelligence — "
            f"Commercial {self.trade.short_label or self.trade.label}"
        )
        description = (
            f"Search {count:,} active commercial construction projects across "
            f"{self.market.name} with documented mechanical evidence "
            f"({mechanical:,} with mechanical or HVAC permits). Built from public City permit "
            f"records, with every fact linked to its source."
        )
        return Seo(
            title=title,
            description=description,
            canonical=self.url("/"),
            json_ld=[self._org_json_ld(), self._dataset_json_ld(stats)],
        )

    def directory(self, filters: Any, is_canonical: bool, total: int) -> Seo:
        title = (
            f"{self.market.short_name} Commercial {self.trade.short_label} Opportunities"
        )
        description = (
            f"Browse {total:,} active commercial construction opportunities across "
            f"{self.market.name} with documented mechanical or HVAC evidence."
        )
        seo = Seo(
            title=title,
            description=description,
            canonical=self.url("/opportunities"),
            json_ld=[self._breadcrumbs([("Home", "/"), ("Opportunities", "/opportunities")])],
        )
        if not is_canonical:
            # A filtered or paged view is a different result set. Canonicalising it to the
            # clean directory keeps one indexable page instead of an unbounded set of thin ones.
            seo.noindex = True
        return seo

    def opportunity(self, project: dict[str, Any]) -> Seo:
        address = project.get("address")
        city = project.get("city")
        project_type = project.get("project_type")
        tier = project.get("mechanical_evidence_tier")
        slug = project.get("slug") or ""

        name = project.get("project_name")
        headline_bits = [b for b in (address, city) if b]
        headline = ", ".join(headline_bits) or (name or "Commercial project")

        title = f"{headline[:60]} — {self.trade.short_label} Opportunity"
        if project_type:
            title = f"{headline[:52]} — {project_type} {self.trade.short_label} Opportunity"

        # Only state what the record supports.
        evidence_phrase = {
            1: "a mechanical permit filed at this address",
            2: "mechanical scope stated in the permit record",
        }.get(tier)
        description_parts = [f"{headline}."]
        if project_type:
            description_parts.append(f"{project_type} project.")
        if evidence_phrase:
            description_parts.append(f"Evidence: {evidence_phrase}.")
        if project.get("project_status"):
            description_parts.append(f"Status: {project['project_status']}.")
        description_parts.append(
            "Sourced from public permit records. Permit evidence does not confirm an "
            "available HVAC bid."
        )
        description = " ".join(description_parts)

        return Seo(
            title=title,
            description=description,
            canonical=self.url(f"/opportunities/{slug}"),
            og_type="article",
            json_ld=[
                self._breadcrumbs(
                    [
                        ("Home", "/"),
                        ("Opportunities", "/opportunities"),
                        (headline[:60], f"/opportunities/{slug}"),
                    ]
                ),
                self._project_json_ld(project),
            ],
        )

    def market_page(self, market: MarketConfig, stats: dict[str, Any]) -> Seo:
        count = stats.get("projects_public", 0)
        return Seo(
            title=f"{market.name} Commercial Construction Opportunities",
            description=(
                f"{count:,} active commercial construction projects across {market.name} with "
                f"documented mechanical evidence, from public City permit records."
            ),
            canonical=self.url(f"/markets/{market.slug}"),
            json_ld=[
                self._breadcrumbs(
                    [("Home", "/"), ("Markets", "/markets"), (market.short_name, f"/markets/{market.slug}")]
                )
            ],
        )

    def city_page(self, market: MarketConfig, city_name: str, stats: dict[str, Any], city_slug: str) -> Seo:
        count = stats.get("projects_public", 0)
        label = self.trade.short_label or self.trade.label
        return Seo(
            title=f"{city_name} Commercial {label} Construction Opportunities",
            description=(
                f"{count:,} active commercial construction projects in {city_name}, "
                f"{market.state} with documented mechanical evidence. Updated from public "
                f"City permit records."
            ),
            canonical=self.url(f"/markets/{market.slug}/{city_slug}"),
            json_ld=[
                self._breadcrumbs(
                    [
                        ("Home", "/"),
                        ("Markets", "/markets"),
                        (market.short_name, f"/markets/{market.slug}"),
                        (city_name, f"/markets/{market.slug}/{city_slug}"),
                    ]
                )
            ],
        )

    def trade_page(self, trade: TradeConfig, stats: dict[str, Any]) -> Seo:
        count = stats.get("projects_public", 0)
        return Seo(
            title=f"Commercial {trade.label} Construction Opportunities",
            description=(
                f"{count:,} commercial construction projects with documented "
                f"{trade.short_label or trade.label} evidence across {self.market.name}."
            ),
            canonical=self.url(f"/trades/{trade.slug or trade.id}"),
            json_ld=[
                self._breadcrumbs(
                    [("Home", "/"), ("Trades", "/trades"),
                     (trade.label, f"/trades/{trade.slug or trade.id}")]
                )
            ],
        )

    def market_trade_page(self, market: MarketConfig, trade: TradeConfig, stats: dict[str, Any]) -> Seo:
        count = stats.get("projects_public", 0)
        mechanical = stats.get("with_mechanical", 0)
        label = trade.short_label or trade.label
        return Seo(
            title=f"{market.short_name} Commercial {label} Construction Opportunities",
            description=(
                f"{count:,} active commercial construction projects across {market.name} with "
                f"documented mechanical evidence. {mechanical:,} carry a mechanical or HVAC "
                f"permit. Every fact links to its public source."
            ),
            canonical=self.url(f"/markets/{market.slug}/{trade.slug or trade.id}"),
            json_ld=[
                self._breadcrumbs(
                    [
                        ("Home", "/"),
                        ("Markets", "/markets"),
                        (market.short_name, f"/markets/{market.slug}"),
                        (label, f"/markets/{market.slug}/{trade.slug or trade.id}"),
                    ]
                ),
                self._dataset_json_ld(stats),
            ],
        )

    def simple(self, title: str, description: str) -> Seo:
        return Seo(title=title, description=description, canonical=self.url("/"))

    def project_types_index(self, types: list[dict[str, Any]]) -> Seo:
        named = ", ".join(row["project_type"] for row in types[:6]) or "commercial building types"
        return Seo(
            title=f"{self.market.short_name} Commercial Project Types",
            description=(
                f"Commercial construction opportunities across {self.market.name} by project "
                f"type, including {named}. Counts are drawn from public permit records."
            ),
            canonical=self.url("/project-types"),
            json_ld=[
                self._breadcrumbs(
                    [("Home", "/"), ("Project types", "/project-types")]
                )
            ],
        )

    def project_type_page(self, project_type: str, stats: dict[str, Any]) -> Seo:
        from ..config import type_slug

        count = stats.get("projects_public", 0)
        mechanical = stats.get("with_mechanical", 0)
        label = self.trade.short_label or self.trade.label
        return Seo(
            title=f"{project_type} Construction Opportunities in {self.market.short_name}",
            description=(
                f"{count:,} active {project_type.lower()} construction projects across "
                f"{self.market.name} with documented mechanical evidence"
                + (f", {mechanical:,} carrying a mechanical permit." if mechanical else ".")
            ),
            canonical=self.url(f"/project-types/{type_slug(project_type)}"),
            json_ld=[
                self._breadcrumbs(
                    [
                        ("Home", "/"),
                        ("Project types", "/project-types"),
                        (project_type, f"/project-types/{type_slug(project_type)}"),
                    ]
                ),
                self._dataset_json_ld(stats),
            ],
        )

    def guide_page(self, guide: dict[str, Any]) -> Seo:
        return Seo(
            title=guide["title"],
            description=guide["summary"],
            canonical=self.url(f"/guides/{guide['slug']}"),
            og_type="article",
            json_ld=[
                self._breadcrumbs(
                    [
                        ("Home", "/"),
                        ("Guides", "/guides"),
                        (guide["title"], f"/guides/{guide['slug']}"),
                    ]
                )
            ],
        )

    # --- structured data ------------------------------------------------------

    def _org_json_ld(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "@context": "https://schema.org",
            "@type": "Organization",
            "name": self._site_name(),
            "description": (
                f"Construction opportunity intelligence for commercial "
                f"{self.trade.label} contractors in {self.market.name}."
            ),
        }
        if self.cfg.base_url:
            data["url"] = self.cfg.base_url
        return data

    def _dataset_json_ld(self, stats: dict[str, Any]) -> dict[str, Any]:
        """Describe the underlying dataset.

        Every value here is a real count. `dateModified` is only included when a retrieval date
        is known, because claiming freshness we cannot evidence is the same error as any other
        unsupported claim.
        """
        data: dict[str, Any] = {
            "@context": "https://schema.org",
            "@type": "Dataset",
            "name": f"{self.market.name} commercial construction permits",
            "description": (
                f"Public construction and permit records for {self.market.name}, normalised "
                f"into commercial project intelligence with mechanical evidence classification."
            ),
            "creator": {"@type": "Organization", "name": self._site_name()},
            "isAccessibleForFree": True,
            "variableMeasured": "Commercial construction projects",
        }
        return data

    def _project_json_ld(self, project: dict[str, Any]) -> dict[str, Any]:
        """Markup for one opportunity.

        Fields are included only when the record supports them. schema.org has no "unknown"
        value, so an absent field is omitted rather than filled with a placeholder — emitting
        `"address": "Not verified"` would be a machine-readable falsehood.
        """
        parts = []
        if project.get("address"):
            parts.append(html.escape(str(project["address"])))
        if project.get("city"):
            parts.append(html.escape(str(project["city"])))
        if project.get("state"):
            parts.append(html.escape(str(project["state"])))
        headline = ", ".join(parts)

        data: dict[str, Any] = {
            "@context": "https://schema.org",
            "@type": "CreativeWork",
            "name": project.get("project_name") or headline or "Commercial construction project",
            "url": self.url(f"/opportunities/{project.get('slug')}"),
            "isPartOf": {
                "@type": "Dataset",
                "name": f"{self.market.name} commercial construction permits",
            },
        }
        if headline:
            data["spatialCoverage"] = headline
        if project.get("permit_date"):
            data["dateCreated"] = str(project["permit_date"])[:10]
        if project.get("last_verified"):
            data["dateModified"] = str(project["last_verified"])[:10]
        if project.get("project_type"):
            data["about"] = str(project["project_type"])
        return data

    def _breadcrumbs(self, items: list[tuple[str, str]]) -> dict[str, Any]:
        return {
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "itemListElement": [
                {
                    "@type": "ListItem",
                    "position": index,
                    "name": html.escape(name),
                    "item": self.url(path),
                }
                for index, (name, path) in enumerate(items, start=1)
            ],
        }

    # --- sitemap and robots ---------------------------------------------------

    def sitemap_urls(self) -> list[dict[str, Any]]:
        """Every canonical, indexable URL.

        Built from configuration and from the canonical opportunity set only. Filtered views,
        paginated result sets, the account area and the internal operations view are all
        excluded, so the sitemap cannot advertise a page that is marked noindex.
        """
        from ..db import Database

        urls: list[dict[str, Any]] = []

        def add(path: str, priority: float, changefreq: str = "weekly") -> None:
            urls.append(
                {
                    "loc": self.url(path),
                    "priority": priority,
                    "changefreq": changefreq,
                }
            )

        add("/", 1.0, "daily")
        add("/opportunities", 0.9, "daily")
        add("/markets", 0.6)
        add("/trades", 0.6)
        add("/project-types", 0.6)
        add("/guides", 0.5, "monthly")
        add("/how-it-works", 0.5, "monthly")
        add("/reports", 0.6)

        # Project-type pages, only for types the database actually holds, so the sitemap never
        # advertises a thin page with nothing behind it.
        db = Database(self.cfg.database_path)
        try:
            type_rows = db.conn.execute(
                """
                SELECT DISTINCT project_type FROM project
                 WHERE classification IN ('HIGH', 'MEDIUM')
                   AND procurement_status IN ('Confirmed open', 'Evidence found, status unclear')
                   AND project_type IS NOT NULL
                """
            ).fetchall()
        finally:
            db.close()
        from ..config import type_slug

        for row in type_rows:
            slug = type_slug(row["project_type"])
            if slug:
                add(f"/project-types/{slug}", 0.7, "weekly")

        from .content import GUIDES

        for guide in GUIDES:
            add(f"/guides/{guide['slug']}", 0.5, "monthly")

        for market in _all_markets().values():
            if not market.active:
                continue
            add(f"/markets/{market.slug}", 0.8, "daily")
            for page in market.landing_pages:
                add(f"/markets/{market.slug}/{page['slug']}", 0.8, "daily")
            for trade in _all_trades().values():
                if trade.active and trade.id in (market.trades or []):
                    add(f"/markets/{market.slug}/{trade.slug or trade.id}", 0.9, "daily")

        for trade in _all_trades().values():
            if trade.active:
                add(f"/trades/{trade.slug or trade.id}", 0.7)

        # Canonical, discoverable opportunities only.
        db = Database(self.cfg.database_path)
        try:
            rows = db.conn.execute(
                """
                SELECT s.slug, p.last_verified
                  FROM project p
                  JOIN project_slug s ON s.project_id = p.id
                 WHERE p.classification IN ('HIGH', 'MEDIUM')
                   AND p.procurement_status IN ('Confirmed open', 'Evidence found, status unclear')
                 ORDER BY p.classification_score DESC, p.id
                """,
            ).fetchall()
        finally:
            db.close()

        for row in rows:
            entry: dict[str, Any] = {
                "loc": self.url(f"/opportunities/{row['slug']}"),
                "priority": 0.7,
                "changefreq": "weekly",
            }
            if row["last_verified"]:
                entry["lastmod"] = str(row["last_verified"])[:10]
            urls.append(entry)

        return urls

    def sitemap(self) -> str:
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        ]
        for entry in self.sitemap_urls():
            lines.append("  <url>")
            lines.append(f"    <loc>{html.escape(entry['loc'])}</loc>")
            if entry.get("lastmod"):
                lines.append(f"    <lastmod>{entry['lastmod']}</lastmod>")
            if entry.get("changefreq"):
                lines.append(f"    <changefreq>{entry['changefreq']}</changefreq>")
            if entry.get("priority") is not None:
                lines.append(f"    <priority>{entry['priority']}</priority>")
            lines.append("  </url>")
        lines.append("</urlset>")
        return "\n".join(lines)

    def robots(self) -> str:
        """robots.txt with the private areas disallowed.

        The disallow list covers the account area, the saved list, the operations view and any
        path carrying a query string, because a query string implies a filtered or paged view
        that is already marked noindex.
        """
        lines = ["User-agent: *", "Allow: /"]
        for path in (
            "/saved",
            "/preferences",
            "/signin",
            "/signup",
            "/signout",
            "/admin",
            "/api",
        ):
            lines.append(f"Disallow: {path}")
        lines.append("")
        lines.append("# Filtered and paginated views are noindex; keep crawlers on canonical pages.")
        lines.append("Disallow: /*?")
        if self.cfg.base_url:
            lines.append("")
            lines.append(f"Sitemap: {self.cfg.base_url}/sitemap.xml")
        return "\n".join(lines)


def _all_markets() -> dict[str, MarketConfig]:
    from ..config import load_markets

    return load_markets()


def _all_trades() -> dict[str, TradeConfig]:
    from ..config import load_trades

    return load_trades()