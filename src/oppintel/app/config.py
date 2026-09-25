"""Application configuration.

Everything the web layer needs that is not business configuration lives here, read from the
environment so nothing is hardcoded to a local path or a development secret.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from ..config import DATA_DIR


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class AppConfig:
    """Runtime configuration for the web application."""

    #: SQLite database holding the intelligence data and the application tables.
    database_path: Path = field(
        default_factory=lambda: Path(
            os.environ.get("OPPINTEL_DB") or (DATA_DIR / "oppintel.db")
        )
    )

    #: Session signing key. Generated per-process when unset, which is safe for one process but
    #: logs users out on restart, so production must set SECRET_KEY.
    secret_key: str = field(
        default_factory=lambda: os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    )

    #: Whether the key was supplied rather than generated. Surfaced as a startup warning so a
    #: misconfigured production deploy is visible instead of silently regenerating sessions.
    secret_key_from_env: bool = field(
        default_factory=lambda: bool(os.environ.get("SECRET_KEY"))
    )

    debug: bool = field(default_factory=lambda: _env_bool("FLASK_DEBUG", False))

    #: Public base URL, used for canonical links, sitemap entries and Open Graph URLs.
    base_url: str = field(
        default_factory=lambda: (os.environ.get("BASE_URL") or "").rstrip("/")
    )

    #: Whether secure cookies are required. Defaults on outside debug, so a deploy does not
    #: silently ship an insecure session.
    session_cookie_secure: bool = field(
        default_factory=lambda: _env_bool(
            "SESSION_COOKIE_SECURE", not _env_bool("FLASK_DEBUG", False)
        )
    )

    #: Free accounts may view this many distinct opportunities before being asked to sign up.
    #: 0 disables the limit, which is the default for the MVP.
    free_view_limit: int = field(
        default_factory=lambda: _env_int("FREE_VIEW_LIMIT", 0)
    )

    #: Whether CSRF, rate limiting and response headers are enforced. Derived in
    #: `__post_init__` from the `debug` value rather than from the environment, because the
    #: field is often set explicitly (by a test or a factory) and reading the environment
    #: again would disagree with the object the caller actually built.
    csrf_enabled: bool = True

    def __post_init__(self) -> None:
        """Derive the security flag from the debug flag unless overridden explicitly.

        A debug process runs with the protections off so local work is not blocked; every
        other process runs with them on. `CSRF_ENABLED` overrides both, for a test that needs
        the protections against a debug-style configuration.
        """
        raw = os.environ.get("CSRF_ENABLED")
        if raw is None:
            self.csrf_enabled = not self.debug
        else:
            self.csrf_enabled = _env_bool("CSRF_ENABLED", True)

    #: Access levels the authorization layer understands. Payment is not implemented; this
    #: exists so monetization does not require redesigning authorization later.
    #:
    #: ADMIN is an operator level, not a paid tier. It gates the internal operations view and
    #: is granted only through the CLI (`oppintel-admin grant`), never through a web route, so
    #: there is no request a user can make that raises their own privileges.
    access_levels: tuple[str, ...] = ("FREE", "PRO", "TEAM", "ADMIN")

    @property
    def templates_dir(self) -> Path:
        return Path(__file__).resolve().parent / "templates"

    @property
    def static_dir(self) -> Path:
        return Path(__file__).resolve().parent / "static"


def load_config() -> AppConfig:
    return AppConfig()
