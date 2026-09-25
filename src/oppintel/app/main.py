"""The web application.

A thin presentation layer over `OpportunityService`. Route handlers do three things only:
resolve configuration, call the service, and render a template. No route builds SQL, scores a
project, or decides what counts as an opportunity, because those rules live in the intelligence
layer and applying them twice would let the two disagree.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from ..config import (
    MarketConfig,
    TradeConfig,
    load_markets,
    load_trades,
    market_by_slug,
    trade_by_slug,
)
from ..db import Database
from ..slugs import project_id_for_slug
from ..service import (
    DEFAULT_SORT,
    PUBLIC_CLASSIFICATIONS,
    SORT_OPTIONS,
    OpportunityFilters,
    OpportunityService,
)
from .accounts import AuthError, AccountService, record_analytics
from .config import AppConfig, load_config
from .seo import SeoBuilder

log = logging.getLogger(__name__)


def create_app(config: AppConfig | None = None) -> Flask:
    """Application factory. Used by the dev server, the CLI and the tests alike."""
    cfg = config or load_config()
    if not cfg.secret_key_from_env:
        log.warning(
            "SECRET_KEY is not set. A random key was generated for this process, so sessions "
            "will not survive a restart. Set SECRET_KEY in production."
        )

    app = Flask(
        __name__,
        template_folder=str(cfg.templates_dir),
        static_folder=str(cfg.static_dir),
    )
    app.config.update(
        SECRET_KEY=cfg.secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cfg.session_cookie_secure,
        MAX_CONTENT_LENGTH=1 * 1024 * 1024,
        JSON_SORT_KEYS=False,
    )
    app.config["APP_CONFIG"] = cfg

    # --- database and per-request context ------------------------------------

    def open_db() -> Database:
        db = Database(cfg.database_path)
        db.init_schema()
        db.init_app_schema()
        return db

    def current_market() -> MarketConfig:
        """The market for this request.

        Resolved per request so a future multi-market deployment can switch on host or path
        without touching the handlers.
        """
        slug = getattr(g, "market_slug", None)
        if slug:
            market = market_by_slug(slug)
            if market:
                return market
        return _active(markets)

    def current_trade() -> TradeConfig:
        slug = getattr(g, "trade_slug", None)
        if slug:
            trade = trade_by_slug(slug)
            if trade:
                return trade
        return _active_trade()

    @app.before_request
    def load_context() -> None:
        g.db = open_db()
        g.accounts = AccountService(g.db)
        g.user = g.accounts.get_user(session.get("user_id"))
        g.service = OpportunityService(g.db, current_market(), current_trade())
        g.market = g.service.market
        g.trade = g.service.trade
        g.started_at = datetime.now(timezone.utc)

    @app.teardown_request
    def close_db(exception: BaseException | None = None) -> None:
        db = g.pop("db", None)
        if db is not None:
            db.close()

    # --- template helpers -----------------------------------------------------

    @app.context_processor
    def inject_globals() -> dict[str, Any]:
        """Values every template needs, resolved once per request."""
        market = getattr(g, "market", None)
        trade = getattr(g, "trade", None)
        return {
            "market": market,
            "trade": trade,
            "all_markets": sorted(markets.values(), key=lambda m: (not m.active, m.name)),
            "all_trades": sorted(_trade_map().values(), key=lambda t: (not t.active, t.label)),
            "current_user": getattr(g, "user", None),
            "base_url": cfg.base_url,
            "now_year": datetime.now(timezone.utc).year,
            "freshness": _freshness_label(getattr(g, "db", None)),
            "saved_count": (
                g.service.saved_count(g.user.id) if getattr(g, "user", None) else 0
            ),
        }

    @app.template_filter("money")
    def money_filter(value: Any) -> str:
        if value is None:
            return "Not verified"
        try:
            return f"${float(value):,.0f}"
        except (TypeError, ValueError):
            return "Not verified"

    @app.template_filter("sqft")
    def sqft_filter(value: Any) -> str:
        if value is None:
            return "Not verified"
        try:
            return f"{float(value):,.0f} sq ft"
        except (TypeError, ValueError):
            return "Not verified"

    @app.template_filter("or_na")
    def or_na_filter(value: Any) -> str:
        """Render a missing value as 'Not verified'.

        Centralised so no template can invent a placeholder or leave a blank that reads as
        "zero" or "none".
        """
        if value is None:
            return "Not verified"
        text = str(value).strip()
        return text or "Not verified"

    @app.template_filter("nice_date")
    def nice_date_filter(value: Any) -> str:
        return OpportunityService.format_date(value)

    @app.template_filter("month_year")
    def month_year_filter(value: Any) -> str:
        return OpportunityService.format_month(value)

    # --- error handling -------------------------------------------------------

    @app.errorhandler(404)
    def not_found(error: Any) -> tuple[str, int]:
        # Error pages still need metadata, because a 404 is rendered through the same base
        # layout. Building it here keeps the layout from having to handle an undefined value.
        seo = g.seo_builder.simple(
            "Page not found", "The requested page could not be found."
        )
        seo.noindex = True
        return (
            render_template(
                "errors/404.html", page_title="Page not found", seo=seo
            ),
            404,
        )

    @app.errorhandler(500)
    def server_error(error: Any) -> tuple[str, int]:
        # The exception is logged with its traceback server-side; the user sees nothing that
        # could disclose a path, a query or a credential.
        log.exception("Unhandled application error")
        seo = g.seo_builder.simple("Something went wrong", "An unexpected error occurred.")
        seo.noindex = True
        return (
            render_template(
                "errors/500.html",
                page_title="Something went wrong",
                support_note="The issue has been logged.",
                seo=seo,
            ),
            500,
        )

    # =====================================================================
    # Public pages
    # =====================================================================

    @app.route("/")
    def home() -> str:
        stats = g.service.market_statistics()
        latest = g.service.list_opportunities(
            OpportunityFilters(page_size=6, sort=DEFAULT_SORT)
        )
        return render_template(
            "home.html",
            stats=stats,
            latest=latest.items,
            cities=g.service.city_statistics()[:8],
            types=g.service.type_statistics(limit=8),
            page_title=(
                f"Commercial {g.trade.short_label or g.trade.label} Construction "
                f"Opportunities Across {g.market.short_name}"
            ),
            seo=g.seo_for_home(stats),
        )

    @app.route("/opportunities")
    def opportunities() -> str:
        filters = _filters_from_request(request.args)
        result = g.service.list_opportunities(filters)
        record_analytics(
            g.db, "search_performed",
            market_id=g.market.id, trade_id=g.trade.id,
        )
        # Only the first page of the unfiltered directory is canonical; a filtered or paged
        # view is a distinct result set and must not compete with it in search results.
        is_canonical = not any(
            [filters.q, filters.city, filters.project_type, filters.classification,
             filters.procurement_status, filters.date_from, filters.date_to]
        ) and filters.page == 1
        return render_template(
            "opportunities/list.html",
            result=result,
            filters=filters,
            cities=g.service.available_cities(),
            project_types=g.service.available_project_types(),
            procurement_options=g.service.procurement_options(),
            sort_options=SORT_OPTIONS,
            page_title=(
                f"{g.market.short_name} Commercial {g.trade.short_label} Opportunities"
            ),
            seo=g.seo_for_directory(filters, is_canonical, result.total),
        )

    @app.route("/opportunities/<slug>")
    def opportunity_detail(slug: str) -> str:
        project = g.service.get_by_slug(slug)
        if project is None:
            abort(404)
        record_analytics(
            g.db, "opportunity_viewed", project_id=project["id"],
            market_id=g.market.id, trade_id=g.trade.id,
        )
        return render_template(
            "opportunities/detail.html",
            project=project,
            related=g.service.related_for(project),
            is_saved=g.service.is_saved(g.user.id if g.user else None, project["id"]),
            page_title=_detail_title(project, g),
            seo=g.seo_for_opportunity(project),
        )

    @app.route("/markets")
    def markets_index() -> str:
        cards = []
        for market in sorted(markets.values(), key=lambda m: (not m.active, m.name)):
            stats = None
            if market.id == g.market.id:
                stats = g.service.market_statistics()
            cards.append({"market": market, "stats": stats})
        return render_template(
            "markets/index.html",
            cards=cards,
            page_title="Markets",
            seo=g.seo_for_simple("Markets", "Markets covered by the platform."),
        )

    @app.route("/markets/<market_slug>")
    def market_landing(market_slug: str) -> str:
        market = market_by_slug(market_slug)
        if market is None or not market.active:
            abort(404)
        g.market_slug = market_slug
        g.service = OpportunityService(g.db, market, current_trade())
        g.market = market
        stats = g.service.market_statistics()
        result = g.service.list_opportunities(OpportunityFilters(page_size=6))
        return render_template(
            "markets/detail.html",
            mkt=market,
            stats=stats,
            opportunities=result.items,
            cities=g.service.city_statistics(),
            page_title=f"{market.short_name} Commercial Construction Opportunities",
            seo=g.seo_for_market(market, stats),
        )

    @app.route("/markets/<market_slug>/<slug>")
    def market_child(market_slug: str, slug: str) -> str:
        """Resolve a two-segment market URL.

        The market+trade page and the market+city page share a URL shape
        (`/markets/<market>/<slug>`), so one endpoint resolves which the slug refers to. Flask
        can only match one rule per path shape, and registering both would make one of them
        unreachable. A slug that names a known trade is a trade page; otherwise it is treated as
        a city.

        Trade slugs are checked first because they are a closed, configured set, whereas a city
        slug that happens to equal a trade name would be ambiguous.
        """
        if trade_by_slug(slug) is not None:
            return _market_trade_page(market_slug, slug)
        return _market_city_page(market_slug, slug)

    def _market_city_page(market_slug: str, city_slug: str) -> str:
        market = market_by_slug(market_slug)
        if market is None or not market.active:
            abort(404)
        # Only cities declared as indexable landing pages get a page, so the site never
        # generates a near-duplicate for every city in the database.
        if city_slug not in {p["slug"] for p in market.landing_pages}:
            abort(404)
        city_name = market.city_name(city_slug)
        if not city_name:
            abort(404)

        g.market_slug = market_slug
        g.service = OpportunityService(g.db, market, current_trade())
        g.market = market
        filters = OpportunityFilters(city=city_name, page_size=10)
        result = g.service.list_opportunities(filters)
        stats = g.service.statistics_for(where="p.city = ?", params=[city_name])
        return render_template(
            "markets/city.html",
            mkt=market,
            city_name=city_name,
            city_slug=city_slug,
            stats=stats,
            result=result,
            page_title=f"{city_name} Commercial {g.trade.short_label} Construction Opportunities",
            seo=g.seo_for_city(market, city_name, stats, city_slug),
        )

    @app.route("/trades")
    def trades_index() -> str:
        return render_template(
            "trades/index.html",
            trades=sorted(_trade_map().values(), key=lambda t: (not t.active, t.label)),
            page_title="Trades",
            seo=g.seo_for_simple(
                "Trades", "Trades supported by the platform."
            ),
        )

    @app.route("/trades/<trade_slug>")
    def trade_landing(trade_slug: str) -> str:
        trade = trade_by_slug(trade_slug)
        if trade is None or not trade.active:
            abort(404)
        g.trade_slug = trade_slug
        g.service = OpportunityService(g.db, current_market(), trade)
        g.trade = trade
        stats = g.service.market_statistics()
        result = g.service.list_opportunities(OpportunityFilters(page_size=6))
        return render_template(
            "trades/detail.html",
            trd=trade,
            stats=stats,
            opportunities=result.items,
            page_title=f"Commercial {trade.short_label or trade.label} Opportunities",
            seo=g.seo_for_trade(trade, stats),
        )

    def _market_trade_page(market_slug: str, trade_slug: str) -> str:
        """The primary programmatic SEO page: a market crossed with a trade."""
        market = market_by_slug(market_slug)
        trade = trade_by_slug(trade_slug)
        if market is None or trade is None or not market.active or not trade.active:
            abort(404)
        g.market_slug = market_slug
        g.trade_slug = trade_slug
        g.service = OpportunityService(g.db, market, trade)
        g.market, g.trade = market, trade
        stats = g.service.market_statistics()
        result = g.service.list_opportunities(OpportunityFilters(page_size=8))
        return render_template(
            "markets/trade.html",
            mkt=market,
            trd=trade,
            stats=stats,
            result=result,
            cities=g.service.city_statistics()[:10],
            page_title=(
                f"{market.short_name} Commercial {trade.short_label} Construction Opportunities"
            ),
            seo=g.seo_for_market_trade(market, trade, stats),
        )

    @app.route("/how-it-works")
    def how_it_works() -> str:
        return render_template(
            "how_it_works.html",
            page_title="How It Works",
            seo=g.seo_for_simple(
                "How It Works",
                "How public construction records become verified commercial opportunities.",
            ),
        )

    @app.route("/reports")
    def reports_index() -> str:
        """Published reports only. Internal audit reports are never served."""
        from .reports import published_reports

        return render_template(
            "reports/index.html",
            reports=published_reports(g.db, cfg),
            page_title="Reports",
            seo=g.seo_for_simple(
                "Reports",
                "Market summaries and opportunity briefs generated from verified permit data.",
            ),
        )

    @app.route("/reports/<report_slug>")
    def report_detail(report_slug: str) -> str:
        from .reports import published_reports

        report = next(
            (r for r in published_reports(g.db, cfg) if r["slug"] == report_slug), None
        )
        if report is None:
            abort(404)
        return render_template(
            "reports/detail.html",
            report=report,
            body=report["body"],
            page_title=report["title"],
            seo=g.seo_for_simple(report["title"], report["summary"]),
        )

    @app.route("/sitemap.xml")
    def sitemap() -> Any:
        xml = g.seo_builder.sitemap()
        return app.response_class(xml, mimetype="application/xml")

    @app.route("/robots.txt")
    def robots() -> Any:
        return app.response_class(g.seo_builder.robots(), mimetype="text/plain")

    # =====================================================================
    # Accounts
    # =====================================================================

    @app.route("/signin", methods=["GET", "POST"])
    def signin() -> Any:
        if g.user:
            return redirect(url_for("home"))
        error = None
        if request.method == "POST":
            try:
                user = g.accounts.authenticate(
                    request.form.get("email", ""), request.form.get("password", "")
                )
                _start_session(user.id)
                record_analytics(g.db, "signin", market_id=g.market.id, trade_id=g.trade.id)
                target = request.args.get("next") or url_for("home")
                return redirect(_safe_redirect(target))
            except AuthError as exc:
                error = str(exc)
        return render_template(
            "account/signin.html",
            error=error,
            page_title="Sign In",
            seo=_private_seo(g, "Sign In", "Sign in to save opportunities."),
        )

    @app.route("/signup", methods=["GET", "POST"])
    def signup() -> Any:
        if g.user:
            return redirect(url_for("home"))
        error = None
        if request.method == "POST":
            password = request.form.get("password", "")
            if password != request.form.get("password_confirm", ""):
                error = "Passwords do not match."
            else:
                try:
                    user = g.accounts.create_account(
                        request.form.get("email", ""),
                        password,
                        request.form.get("display_name", ""),
                        market=g.market,
                        trade=g.trade,
                    )
                    _start_session(user.id)
                    record_analytics(g.db, "signup", market_id=g.market.id, trade_id=g.trade.id)
                    return redirect(url_for("home"))
                except AuthError as exc:
                    error = str(exc)
        return render_template(
            "account/signup.html",
            error=error,
            page_title="Create Free Account",
            seo=_private_seo(
                g, "Create Free Account", "Save opportunities and set preferences."
            ),
        )

    @app.route("/signout", methods=["POST", "GET"])
    def signout() -> Any:
        session.clear()
        return redirect(url_for("home"))

    @app.route("/saved")
    def saved() -> Any:
        if not g.user:
            return redirect(url_for("signin", next=url_for("saved")))
        ids = g.accounts.saved_project_ids(g.user.id)
        items = []
        for project_id in ids:
            slug = _slug_for(g.db, project_id)
            if not slug:
                continue
            project = g.service.get_by_slug(slug)
            if project:
                items.append(project)
        return render_template(
            "account/saved.html",
            items=items,
            page_title="Saved Opportunities",
            seo=_private_seo(g, "Saved Opportunities", "Your saved opportunities."),
        )

    @app.route("/saved/<int:project_id>", methods=["POST"])
    def save_opportunity(project_id: int) -> Any:
        if not g.user:
            if request.headers.get("Accept", "").startswith("application/json"):
                return {"ok": False, "reason": "auth_required"}, 401
            return redirect(url_for("signin", next=request.referrer or url_for("opportunities")))

        action = request.form.get("action", "save")
        if action == "remove":
            g.accounts.unsave_opportunity(g.user.id, project_id)
            saved_now = False
        else:
            saved_now = g.accounts.save_opportunity(g.user.id, project_id)
            if saved_now:
                record_analytics(
                    g.db, "opportunity_saved", project_id=project_id,
                    market_id=g.market.id, trade_id=g.trade.id,
                )

        if request.headers.get("Accept", "").startswith("application/json"):
            return {"ok": True, "saved": saved_now}
        if request.form.get("redirect") == "saved":
            return redirect(url_for("saved"))
        return redirect(request.referrer or url_for("opportunities"))

    @app.route("/preferences", methods=["GET", "POST"])
    def preferences() -> Any:
        if not g.user:
            return redirect(url_for("signin", next=url_for("preferences")))
        if request.method == "POST":
            g.accounts.update_preferences(
                g.user.id,
                market_id=request.form.get("market_id") or g.market.id,
                trade_id=request.form.get("trade_id") or g.trade.id,
                cities=request.form.getlist("cities"),
                project_types=request.form.getlist("project_types"),
                notify_in_app=bool(request.form.get("notify_in_app")),
                notify_email=bool(request.form.get("notify_email")),
            )
            flash("Preferences saved.", "success")
            return redirect(url_for("preferences"))
        return render_template(
            "account/preferences.html",
            prefs=g.accounts.get_preferences(g.user.id),
            cities=g.service.available_cities(),
            project_types=g.service.available_project_types(),
            page_title="Preferences",
            seo=_private_seo(g, "Preferences", "Your market and trade preferences."),
        )

    # =====================================================================
    # Internal operations. Not linked publicly and marked noindex.
    # =====================================================================

    @app.route("/admin/data")
    def admin_data() -> Any:
        """A read-only operations view.

        Not authenticated in the MVP because it exposes only aggregate counts that are already
        public, and it is not linked from any public page. It deliberately shows no credentials,
        no source URLs, no ingestion endpoints and no user data. Deployment should place it
        behind network-level access control; see the README.
        """
        from ..reporting import data_quality_report

        stats = g.service.market_statistics()
        freshness = g.service.data_freshness()
        return render_template(
            "admin/data.html",
            stats=stats,
            freshness=freshness,
            quality_report=data_quality_report(g.db),
            page_title="Data operations",
            seo=_private_seo(g, "Data operations", "Internal operations view."),
        )

    @app.before_request
    def attach_seo() -> None:
        """Attach per-request configuration and the SEO builder.

        Bound here rather than at app creation, because the market and trade can differ per
        request once multi-market routing exists, and a builder captured at import time would
        describe the wrong market.
        """
        g.seo_builder = SeoBuilder(cfg, current_market(), current_trade())
        g.seo_for_home = g.seo_builder.home
        g.seo_for_directory = g.seo_builder.directory
        g.seo_for_opportunity = g.seo_builder.opportunity
        g.seo_for_market = g.seo_builder.market_page
        g.seo_for_city = g.seo_builder.city_page
        g.seo_for_trade = g.seo_builder.trade_page
        g.seo_for_market_trade = g.seo_builder.market_trade_page
        g.seo_for_simple = g.seo_builder.simple

    # The JSON API is registered after the pages so a route name collision would be caught
    # at import time rather than silently shadowing a page.
    from .api import bp as api_bp

    app.register_blueprint(api_bp)

    _register_cli(app, cfg)
    return app


# --- module helpers -----------------------------------------------------------


def _active(markets: dict[str, MarketConfig]) -> MarketConfig:
    for market in markets.values():
        if market.active:
            return market
    raise RuntimeError("No active market configured")


def _trade_map() -> dict[str, TradeConfig]:
    return load_trades()


def _active_trade() -> TradeConfig:
    for trade in load_trades().values():
        if trade.active:
            return trade
    raise RuntimeError("No active trade configured")


#: Markets are read once at import; `create_app` re-reads so a config change is picked up in
#: tests that patch configuration.
markets = load_markets()


def _filters_from_request(args: Any) -> OpportunityFilters:
    return OpportunityFilters(
        q=args.get("q"),
        city=args.get("city"),
        project_type=args.get("project_type"),
        classification=args.get("classification"),
        procurement_status=args.get("procurement_status"),
        date_from=args.get("date_from"),
        date_to=args.get("date_to"),
        include_unverified=args.get("include_unverified") in ("1", "true", "on"),
        sort=args.get("sort") or DEFAULT_SORT,
        page=_safe_int(args.get("page"), 1),
        page_size=_safe_int(args.get("page_size"), 20),
    ).normalised()


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _start_session(user_id: int) -> None:
    session.clear()
    session["user_id"] = user_id
    session.permanent = False


def _safe_redirect(target: str | None) -> str:
    """Only allow same-site redirect targets.

    An open redirect that forwards to an attacker's domain is a phishing vector and would let
    a link on this site be used as a credible-looking hop.
    """
    from urllib.parse import urlparse

    if not target:
        return "/"
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return "/"
    return target if parsed.path.startswith("/") else "/"


def _slug_for(db: Database, project_id: int) -> str | None:
    from ..slugs import slug_for_project

    return slug_for_project(db, project_id)


def _private_seo(g: Any, title: str, description: str):
    """Metadata for a page that must never be indexed.

    Built here rather than as a kwarg on `render_template`, because a route that already passes
    `seo=` would silently ignore a second `noindex` argument and the page would end up
    indexable despite the intent.
    """
    seo = g.seo_for_simple(title, description)
    seo.noindex = True
    return seo


def _freshness_label(db: Database | None) -> dict[str, Any]:
    if db is None:
        return {"display": "Not verified", "retrieval_date": None}
    row = db.conn.execute("SELECT MAX(retrieval_date) AS d FROM source_coverage").fetchone()
    value = row["d"] if row else None
    return {"display": OpportunityService.format_month(value), "retrieval_date": value}


def _detail_title(project: dict[str, Any], g: Any) -> str:
    name = project.get("project_name") or project.get("address") or "Opportunity"
    city = project.get("city") or ""
    return f"{name[:70]} — {city} {g.trade.short_label} Opportunity".strip()


def _register_cli(app: Flask, cfg: AppConfig) -> None:
    """Operational commands. The CLI in `oppintel.cli` remains the primary mechanism."""

    @app.cli.command("init-app")
    def init_app_command() -> None:
        """Create the application tables without touching intelligence data."""
        db = Database(cfg.database_path)
        db.init_schema()
        db.init_app_schema()
        print(f"Application schema ready at {cfg.database_path}")

    @app.cli.command("build-search-index")
    def build_index_command() -> None:
        """Rebuild the search index and generate any missing slugs."""
        from ..search_index import rebuild_index
        from ..slugs import ensure_slugs

        db = Database(cfg.database_path)
        db.init_schema()
        db.init_app_schema()
        print(f"slugs created: {ensure_slugs(db)}")
        print(f"projects indexed: {rebuild_index(db)}")
