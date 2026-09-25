"""The web application.

A thin presentation layer over `OpportunityService`. Route handlers do three things only:
resolve configuration, call the service, and render a template. No route builds SQL, scores a
project, or decides what counts as an opportunity, because those rules live in the intelligence
layer and applying them twice would let the two disagree.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import click
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
from .alerts import AlertService
from .config import AppConfig, load_config
from .entitlements import SubscriptionService
from .seo import SeoBuilder
from .security import install_security
from .workflow import (
    DEFAULT_STAGE,
    PIPELINE_STAGES,
    STAGE_LABELS,
    WorkflowError,
    WorkflowService,
)

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
        g.service = _service(g.db, current_market(), current_trade(), g.user)
        g.market = g.service.market
        g.trade = g.service.trade
        g.workflow = WorkflowService(g.db)
        g.alerts = AlertService(g.db)
        g.subscriptions = SubscriptionService(g.db)
        g.entitlement = g.subscriptions.entitlements_for(g.user)
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
            "unread_alerts": (
                g.alerts.unread_count(g.user.id)
                if getattr(g, "user", None) and getattr(g, "alerts", None)
                else 0
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

    @app.errorhandler(403)
    def forbidden(error: Any) -> tuple[str, int]:
        """Signed in, but not permitted.

        Distinct from 404 so the response is honest about what happened: the page exists, the
        account simply does not have access. The body discloses nothing about what the page
        contains.
        """
        seo = g.seo_builder.simple(
            "Not permitted", "This account does not have access to that page."
        )
        seo.noindex = True
        return (
            render_template(
                "errors/403.html", page_title="Not permitted", seo=seo
            ),
            403,
        )

    @app.errorhandler(500)
    def server_error(error: Any) -> tuple[str, int]:
        # The exception is logged with its traceback server-side; the user sees nothing that
        # could disclose a path, a query or a credential.
        log.exception("Unhandled application error")
        # Recorded without a traceback, a user id or a request body, so the operations view can
        # show that something failed without the table becoming a store of sensitive detail.
        db = getattr(g, "db", None)
        if db is not None:
            try:
                db.record_app_error(
                    request.path, request.method, 500,
                    error.__class__.__name__,
                )
            except Exception:  # noqa: BLE001 - never let error reporting mask the error
                log.exception("Failed to record application error")
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

        # Match reasons, timeline and the account's own working record. Each is produced by a
        # shared implementation: the same reasons the feed shows, the same change rows the
        # alerts read, and notes scoped to this account alone.
        from .matching import evaluate_match

        prefs = g.accounts.get_preferences(g.user.id) if g.user else {}
        project["match_reasons"] = evaluate_match(
            project, trade=g.trade, market=g.market, preferences=prefs
        ).reasons
        project["timeline"] = g.service.changes_for(project["id"], limit=25)
        project["notes"] = (
            g.workflow.notes_for(g.user.id, project["id"]) if g.user else []
        )
        project["tags"] = (
            g.workflow.tags_for(g.user.id, project["id"]) if g.user else []
        )

        return render_template(
            "opportunities/detail.html",
            project=project,
            related=g.service.related_for(project),
            is_saved=g.service.is_saved(g.user.id if g.user else None, project["id"]),
            stages=PIPELINE_STAGES,
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
        g.service = _service(g.db, market, current_trade(), g.user)
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
        g.service = _service(g.db, market, current_trade(), g.user)
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
        g.service = _service(g.db, current_market(), trade, g.user)
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
        g.service = _service(g.db, market, trade, g.user)
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

    @app.route("/project-types")
    def project_types_index() -> str:
        """Index of project types that hold real records. Nothing is generated speculatively."""
        from ..config import type_slug

        types = g.service.type_statistics(limit=100)
        return render_template(
            "project_types/index.html",
            types=types,
            type_slug=type_slug,
            page_title=f"{g.market.short_name} Project Types",
            seo=g.seo_builder.project_types_index(types),
        )

    @app.route("/project-types/<type_slug_value>")
    def project_type_page(type_slug_value: str) -> str:
        """A landing page for one project type, built only from real records.

        The slug is resolved back to a stored `project_type` value through the database, so a
        page cannot exist for a type the data does not contain, and a request for an unknown
        type is a 404 rather than an empty page.
        """
        mapping = g.service.project_type_slug_map()
        name = mapping.get(type_slug_value)
        if not name:
            abort(404)
        stats = g.service.statistics_for_type(name)
        result = g.service.list_opportunities(
            OpportunityFilters(project_type=name, page_size=12)
        )
        return render_template(
            "project_types/detail.html",
            project_type=name,
            stats=stats,
            result=result,
            cities=g.service.cities_for_type(name),
            page_title=f"{name} Construction Opportunities in {g.market.short_name}",
            seo=g.seo_builder.project_type_page(name, stats),
        )

    @app.route("/guides")
    def guides_index() -> str:
        """Educational content index. Each guide is static, owned prose — never generated
        filler — and marked indexable only when it carries real substance."""
        from .content import GUIDES

        return render_template(
            "content/index.html",
            guides=GUIDES,
            page_title="Guides",
            seo=g.seo_builder.simple(
                "Guides",
                "How to read permit evidence and evaluate a construction opportunity.",
            ),
        )

    @app.route("/guides/<guide_slug>")
    def guide_detail(guide_slug: str) -> str:
        from .content import GUIDES

        guide = next((item for item in GUIDES if item["slug"] == guide_slug), None)
        if guide is None:
            abort(404)
        return render_template(
            "content/detail.html",
            guide=guide,
            page_title=guide["title"],
            seo=g.seo_builder.guide_page(guide),
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
    # Dashboard and account working views
    # =====================================================================

    @app.route("/dashboard")
    def dashboard() -> Any:
        """The account's working centre.

        Everything here is a real query over this account's own rows. A signed-out visitor is
        sent to sign-in rather than shown a demo dashboard, because an empty dashboard that
        looks populated is the kind of thing this product exists not to do.
        """
        if not g.user:
            return redirect(url_for("signin", next=url_for("dashboard")))

        summary = g.workflow.summary(g.user.id)
        prefs = g.accounts.get_preferences(g.user.id)

        # New matches: recent, discoverable, pre-filtered by the account's hard preferences,
        # then annotated with the reasons each one matches. This is the personalized feed.
        from .matching import evaluate_match, filter_matches

        candidates = g.service.projects_by_ids(
            g.service.recent_discoverable_ids(limit=200)
        )
        matched = filter_matches(candidates, preferences=prefs)
        for project in matched:
            result = evaluate_match(
                project, trade=g.trade, market=g.market, preferences=prefs
            )
            project["match_reasons"] = result.reasons
            project["match_score"] = result.score
        new_matches = matched[:6]

        # Recently updated: projects with a recorded change in the window, not merely a
        # pipeline re-run, so the section means what it says.
        updated_ids = g.service.changed_project_ids(limit=6, days=30)
        recently_updated = g.service.projects_by_ids(updated_ids)

        watching = g.service.projects_by_ids(g.workflow.watched_ids(g.user.id))[:6]
        saved_ids = g.accounts.saved_project_ids(g.user.id)
        saved = g.service.projects_by_ids(saved_ids)[:6]

        pipeline_rows = g.workflow.pipeline_rows(g.user.id)
        stage_by_id = {int(r["project_id"]): r["stage"] for r in pipeline_rows}
        pipeline_projects = g.service.projects_by_ids(list(stage_by_id))[:8]
        for project in pipeline_projects:
            project["stage"] = stage_by_id.get(int(project["id"]))

        # Needs review: pipeline entries still at the entry stage, which is the account's own
        # backlog rather than a claim about any project.
        review_ids = g.workflow.stage_project_ids(g.user.id, "NEW")
        needs_review = g.service.projects_by_ids(review_ids)[:6]

        return render_template(
            "dashboard.html",
            summary=summary,
            new_matches=new_matches,
            recently_updated=recently_updated,
            watching=watching,
            saved=saved,
            pipeline_projects=pipeline_projects,
            needs_review=needs_review,
            prefs=prefs,
            stages=PIPELINE_STAGES,
            unread_alerts=g.alerts.unread_count(g.user.id),
            page_title="Dashboard",
            seo=_private_seo(g, "Dashboard", "Your opportunity workspace."),
        )

    @app.route("/watching")
    def watching() -> Any:
        """Opportunities the account is monitoring for change."""
        if not g.user:
            return redirect(url_for("signin", next=url_for("watching")))
        ids = g.workflow.watched_ids(g.user.id)
        items = g.service.projects_by_ids(ids)
        change_counts = _change_counts_for(g.db, ids)
        return render_template(
            "account/watching.html",
            items=items,
            change_counts=change_counts,
            page_title="Watching",
            seo=_private_seo(g, "Watching", "Opportunities you are monitoring."),
        )

    @app.route("/my-pipeline")
    def my_pipeline() -> Any:
        """The account's own workflow over opportunities, grouped by stage.

        The page states plainly that a stage is the account's working state and not the
        project's procurement status, because the two vocabularies must never be conflated.
        """
        if not g.user:
            return redirect(url_for("signin", next=url_for("my_pipeline")))
        rows = g.workflow.pipeline_rows(g.user.id)
        stage_by_id = {int(r["project_id"]): r for r in rows}
        projects = g.service.projects_by_ids(list(stage_by_id))
        for project in projects:
            row = stage_by_id.get(int(project["id"]), {})
            project["stage"] = row.get("stage")
            project["follow_up_date"] = row.get("follow_up_date")
            project["assigned_to"] = row.get("assigned_to")

        columns: list[dict[str, Any]] = []
        for key, label in PIPELINE_STAGES:
            stage_items = [p for p in projects if p.get("stage") == key]
            # Named `entries`, not `items`: `items` on a dict resolves to the built-in method
            # inside a Jinja attribute lookup, so `column.items | length` would fail.
            columns.append({"key": key, "label": label, "entries": stage_items})

        return render_template(
            "account/pipeline.html",
            columns=columns,
            total=len(projects),
            peers=g.workflow.peers_for(g.user.id),
            stages=PIPELINE_STAGES,
            page_title="My Pipeline",
            seo=_private_seo(g, "My Pipeline", "Opportunities you are working."),
        )

    @app.route("/pipeline/<int:project_id>", methods=["POST"])
    def pipeline_update(project_id: int) -> Any:
        """Move an opportunity within the account's pipeline, or set a follow-up date."""
        if not g.user:
            return _json_or_redirect_unauth()
        action = request.form.get("action", "stage")
        try:
            if action == "remove":
                g.workflow.remove_from_pipeline(g.user.id, project_id)
            elif action == "follow_up":
                g.workflow.set_follow_up(
                    g.user.id, project_id, request.form.get("follow_up_date") or None
                )
            elif action == "assign":
                raw = request.form.get("assigned_to") or ""
                g.workflow.assign(
                    g.user.id, project_id, int(raw) if raw.isdigit() else None
                )
            else:
                g.workflow.set_stage(
                    g.user.id, project_id,
                    request.form.get("stage") or DEFAULT_STAGE,
                    follow_up_date=request.form.get("follow_up_date") or None,
                )
        except (WorkflowError, ValueError) as exc:
            if _wants_json():
                return {"ok": False, "error": str(exc)}, 400
            flash(str(exc), "error")
            return redirect(request.referrer or url_for("my_pipeline"))

        record_analytics(
            g.db, "pipeline_updated", project_id=project_id,
            market_id=g.market.id, trade_id=g.trade.id,
        )
        if _wants_json():
            return {
                "ok": True,
                "stage": g.workflow.stage_for(g.user.id, project_id),
            }
        return redirect(request.form.get("redirect") or request.referrer or url_for("my_pipeline"))

    @app.route("/watching/<int:project_id>", methods=["POST"])
    def watch_opportunity(project_id: int) -> Any:
        """Start or stop monitoring an opportunity. A POST so a crawler cannot change state."""
        if not g.user:
            return _json_or_redirect_unauth()
        if request.form.get("action") == "remove":
            g.workflow.unwatch(g.user.id, project_id)
            watching_now = False
        else:
            watching_now = g.workflow.watch(g.user.id, project_id)
            if watching_now:
                record_analytics(
                    g.db, "opportunity_watched", project_id=project_id,
                    market_id=g.market.id, trade_id=g.trade.id,
                )
        if _wants_json():
            return {"ok": True, "watching": watching_now}
        return redirect(request.form.get("redirect") or request.referrer or url_for("opportunities"))

    @app.route("/notes/<int:project_id>", methods=["POST"])
    def add_note(project_id: int) -> Any:
        """Attach a private note to an opportunity."""
        if not g.user:
            return _json_or_redirect_unauth()
        try:
            g.workflow.add_note(g.user.id, project_id, request.form.get("body", ""))
        except WorkflowError as exc:
            if _wants_json():
                return {"ok": False, "error": str(exc)}, 400
            flash(str(exc), "error")
            return redirect(request.referrer or url_for("opportunities"))
        if _wants_json():
            return {"ok": True}
        return redirect(request.referrer or url_for("dashboard"))

    @app.route("/notes/<int:note_id>/delete", methods=["POST"])
    def delete_note(note_id: int) -> Any:
        """Delete one of the account's own notes."""
        if not g.user:
            return _json_or_redirect_unauth()
        g.workflow.delete_note(g.user.id, note_id)
        if _wants_json():
            return {"ok": True}
        return redirect(request.referrer or url_for("dashboard"))

    @app.route("/tags/<int:project_id>", methods=["POST"])
    def tag_opportunity(project_id: int) -> Any:
        """Add or remove one of the account's own tags on an opportunity."""
        if not g.user:
            return _json_or_redirect_unauth()
        tag = request.form.get("tag", "")
        try:
            if request.form.get("action") == "remove":
                g.workflow.remove_tag(g.user.id, project_id, tag)
            else:
                g.workflow.add_tag(g.user.id, project_id, tag)
        except WorkflowError as exc:
            if _wants_json():
                return {"ok": False, "error": str(exc)}, 400
            flash(str(exc), "error")
        if _wants_json():
            return {"ok": True, "tags": g.workflow.tags_for(g.user.id, project_id)}
        return redirect(request.referrer or url_for("opportunities"))

    @app.route("/alerts")
    def alerts() -> Any:
        """Event-driven alerts for this account.

        Every row shown here traces to a recorded change or a first-match event; the page says
        so, and the operations view asserts it independently.
        """
        if not g.user:
            return redirect(url_for("signin", next=url_for("alerts")))
        unread_only = request.args.get("unread") in ("1", "true", "on")
        items = g.alerts.for_user(g.user.id, unread_only=unread_only)
        return render_template(
            "account/alerts.html",
            items=items,
            unread_count=g.alerts.unread_count(g.user.id),
            unread_only=unread_only,
            page_title="Alerts",
            seo=_private_seo(g, "Alerts", "Changes to the opportunities you watch."),
        )

    @app.route("/alerts/<int:alert_id>/read", methods=["POST"])
    def mark_alert_read(alert_id: int) -> Any:
        if not g.user:
            return _json_or_redirect_unauth()
        g.alerts.mark_read(g.user.id, alert_id)
        if _wants_json():
            return {"ok": True, "unread": g.alerts.unread_count(g.user.id)}
        return redirect(request.referrer or url_for("alerts"))

    @app.route("/alerts/read-all", methods=["POST"])
    def mark_all_alerts_read() -> Any:
        if not g.user:
            return _json_or_redirect_unauth()
        g.alerts.mark_all_read(g.user.id)
        if _wants_json():
            return {"ok": True, "unread": 0}
        return redirect(url_for("alerts"))

    # =====================================================================
    # Internal operations. Not linked publicly and marked noindex.
    # =====================================================================

    @app.route("/admin/data")
    @_require_admin
    def admin_data() -> Any:
        """A read-only operations view, restricted to operators.

        Requires an account with the ADMIN access level. An unauthenticated request is sent to
        sign-in; a signed-in non-operator receives 403. The level is granted only through the
        CLI, so no web request can escalate to it.

        The page shows aggregate counts, source coverage, data-quality findings, monitoring and
        alert integrity, and recent application errors. It deliberately exposes no credentials,
        no outbound source URLs, no ingestion or connector endpoints, no database paths and no
        user data — so even an operator cannot read anything here that would be dangerous if
        the page were reached.
        """
        from ..reporting import data_quality_report

        stats = g.service.market_statistics()
        freshness = g.service.data_freshness()
        health = _system_health(g.db)
        return render_template(
            "admin/data.html",
            stats=stats,
            freshness=freshness,
            quality_report=data_quality_report(g.db),
            health=health,
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

    # CSRF, rate limiting and response headers. Installed after the routes so every route it
    # protects is already registered, and before the first request either way.
    install_security(app, enabled=cfg.csrf_enabled)

    _register_cli(app, cfg)
    return app


# --- module helpers -----------------------------------------------------------


def _service(
    db: Database, market: MarketConfig, trade: TradeConfig, user: Any
) -> OpportunityService:
    """Build the read service for a request, bound to the signed-in account when there is one.

    Centralised so every route constructs the service the same way and a new route cannot
    forget to pass the viewer, which would silently drop the save/watch state from its cards.
    """
    return OpportunityService(
        db, market, trade, viewer_id=getattr(user, "id", None)
    )


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


def _wants_json() -> bool:
    """Whether the caller expects a JSON reply rather than a redirect.

    A fetch/XHR caller sends an `Accept` header naming JSON; a browser form post does not. The
    decision is made from the header alone, never from a query parameter, so a hostile page
    cannot force a JSON response into a browser navigation.
    """
    return request.headers.get("Accept", "").startswith("application/json")


def _json_or_redirect_unauth() -> Any:
    """Reply to an unauthenticated mutation, matching the caller's expectation."""
    if _wants_json():
        return {"ok": False, "reason": "auth_required"}, 401
    return redirect(url_for("signin", next=request.path))


def _change_counts_for(db: Database, project_ids: list[int]) -> dict[int, int]:
    """Recorded change counts for a set of projects, for the watching view.

    A count of real change rows, so "3 changes" on the page means three detected differences
    and nothing more.
    """
    if not project_ids:
        return {}
    placeholders = ",".join("?" for _ in project_ids)
    rows = db.conn.execute(
        f"""
        SELECT project_id, COUNT(*) AS n FROM project_change
         WHERE project_id IN ({placeholders})
         GROUP BY project_id
        """,
        list(project_ids),
    ).fetchall()
    return {int(r["project_id"]): int(r["n"]) for r in rows}


def _system_health(db: Database) -> dict[str, Any]:
    """Aggregate operational health, from stored rows only.

    Every figure is a count or a timestamp the database already holds. Nothing is estimated,
    and the page renders an explicit "not measured" for a metric the schema cannot support, so
    an operator never reads a plausible-looking number that nothing produced.
    """
    from .alerts import AlertService

    def scalar(sql: str, params: tuple = ()) -> int:
        return int(db.conn.execute(sql, params).fetchone()[0])

    # Source health: the last run per source, with its outcome.
    runs = [
        dict(r)
        for r in db.conn.execute(
            """
            SELECT r.source_id, s.name, r.status, r.started_at, r.finished_at,
                   r.rows_fetched, r.rows_landed, r.permits_created, r.error
              FROM ingest_run r
              LEFT JOIN source s ON s.id = r.source_id
             WHERE r.id IN (SELECT MAX(id) FROM ingest_run GROUP BY source_id)
             ORDER BY r.source_id
            """
        ).fetchall()
    ]
    failing = [r for r in runs if r["status"] != "ok"]

    issues = db.quality_issues(limit=500)
    by_severity: dict[str, int] = {}
    for issue in issues:
        by_severity[issue["severity"]] = by_severity.get(issue["severity"], 0) + 1

    alert_summary = AlertService(db).summary()

    return {
        "sources": runs,
        "sources_failing": len(failing),
        "runs_recorded": scalar("SELECT COUNT(*) FROM ingest_run"),
        "changes_total": scalar("SELECT COUNT(*) FROM project_change"),
        "changes_recent": scalar(
            "SELECT COUNT(*) FROM project_change "
            "WHERE detected_at >= datetime('now', '-7 days')"
        ),
        "watched_total": scalar("SELECT COUNT(*) FROM watched_opportunity"),
        "pipeline_total": scalar("SELECT COUNT(*) FROM pipeline_entry"),
        "quality_issues": issues[:50],
        "quality_by_severity": by_severity,
        "quality_total": len(issues),
        "alerts_total": alert_summary["total"],
        "alerts_orphaned": alert_summary["orphaned"],
        "recent_errors": db.recent_app_errors(limit=20),
        "error_count": scalar("SELECT COUNT(*) FROM app_error"),
    }


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


def _require_admin(view: Any) -> Any:
    """Gate a view to authenticated operators.

    Three distinct outcomes, deliberately:

    * No session at all -> redirect to sign-in with `next` pointing back here, which is the
      behaviour a browser user expects from a private page.
    * Signed in but not an operator -> 403. The distinction matters: a signed-in customer is
      not a stranger to be sent round the sign-in loop again, and telling them plainly that
      the page is not for them is both clearer and more honest than a redirect that appears
      to do nothing.
    * Operator -> the view runs.

    This wraps the view rather than sitting in `before_request` so the check lives with the
    route it protects. A future private route cannot be added without visibly opting in.
    """
    from functools import wraps

    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        user = getattr(g, "user", None)
        if user is None:
            return redirect(url_for("signin", next=request.path))
        if not user.is_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def _register_cli(app: Flask, cfg: AppConfig) -> None:
    """Operational commands. The CLI in `oppintel.cli` remains the primary mechanism."""

    @app.cli.command("monitor")
    @click.option("--alerts/--no-alerts", default=True,
                  help="Also generate alerts for watched projects.")
    def monitor_command(alerts: bool) -> None:
        """Run the monitoring pass: detect changes and optionally raise alerts.

        Intended to run after each ingestion. Change detection also runs inline during
        assembly, so this command is idempotent: a second call with no new data creates
        nothing.
        """
        from .alerts import build_alerts

        db = Database(cfg.database_path)
        db.init_schema()
        db.init_app_schema()
        try:
            if alerts:
                result = build_alerts(db)
                print(f"alerts created: {result['created']} (scanned {result['scanned']})")
            else:
                print("monitoring skipped")
        finally:
            db.close()

    @app.cli.command("report-quality")
    def quality_command() -> None:
        """Print the current data-quality findings."""
        from ..quality import detect_quality_issues

        db = Database(cfg.database_path)
        db.init_schema()
        db.init_app_schema()
        try:
            issues = db.quality_issues(limit=200)
            if not issues:
                print("No open data-quality issues.")
                return
            for issue in issues:
                print(f"[{issue['severity']}] {issue['issue_type']}: {issue['detail']}")
        finally:
            db.close()

    @app.cli.command("set-plan")
    @click.argument("email")
    @click.argument("plan_id")
    @click.option("--status", default="ACTIVE", help="Subscription status.")
    @click.option("--external-ref", default=None, help="Reference from an external billing system.")
    def set_plan_command(email: str, plan_id: str, status: str, external_ref: str | None) -> None:
        """Record an account's plan relationship.

        The only mechanism that grants a paid tier. Payment is not implemented, so this command
        is what an operator or an external billing integration calls once a subscription is
        real. There is deliberately no web route that reaches it.
        """
        db = Database(cfg.database_path)
        db.init_schema()
        db.init_app_schema()
        try:
            from .entitlements import SubscriptionService

            service = SubscriptionService(db)
            service.ensure_plans()
            row = db.conn.execute(
                "SELECT id FROM app_user WHERE email = ?", (email.strip().lower(),)
            ).fetchone()
            if row is None:
                print(f"No account found for {email!r}. Create it at /signup first.")
                raise SystemExit(1)
            try:
                service.set_subscription(
                    int(row["id"]), plan_id, status, external_ref=external_ref
                )
            except ValueError as exc:
                print(str(exc))
                raise SystemExit(1)
            print(f"Set {email} to plan {plan_id} ({status})")
        finally:
            db.close()

    @app.cli.command("init-app")
    def init_app_command() -> None:
        """Create the application tables without touching intelligence data."""
        db = Database(cfg.database_path)
        db.init_schema()
        db.init_app_schema()
        from .entitlements import SubscriptionService

        SubscriptionService(db).ensure_plans()
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

    @app.cli.command("grant-admin")
    @click.argument("email")
    def grant_admin_command(email: str) -> None:
        """Grant the ADMIN access level to an existing account.

        The only way to become an operator. There is deliberately no web route that sets this
        level, so a request cannot escalate its own privileges and a compromised session
        cannot promote itself. Run it on the host that owns the database.
        """
        db = Database(cfg.database_path)
        db.init_schema()
        db.init_app_schema()
        cursor = db.conn.execute(
            "UPDATE app_user SET access_level = 'ADMIN' WHERE email = ?",
            (email.strip().lower(),),
        )
        db.conn.commit()
        if cursor.rowcount == 0:
            print(f"No account found for {email!r}. Create it at /signup first.")
            raise SystemExit(1)
        print(f"Granted ADMIN to {email}")

    @app.cli.command("revoke-admin")
    @click.argument("email")
    def revoke_admin_command(email: str) -> None:
        """Return an operator account to the FREE level."""
        db = Database(cfg.database_path)
        db.init_schema()
        db.init_app_schema()
        cursor = db.conn.execute(
            "UPDATE app_user SET access_level = 'FREE' WHERE email = ?",
            (email.strip().lower(),),
        )
        db.conn.commit()
        if cursor.rowcount == 0:
            print(f"No account found for {email!r}.")
            raise SystemExit(1)
        print(f"Revoked ADMIN from {email}")
