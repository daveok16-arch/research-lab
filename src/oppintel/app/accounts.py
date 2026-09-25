"""Accounts, sessions and saved opportunities.

Deliberately minimal. The MVP needs a free account that can save opportunities and hold
preferences; it does not need roles, teams, invitations or password reset flows.

Security decisions worth stating:

* Passwords are hashed with `werkzeug.security` (PBKDF2-SHA256, per-password salt). A plain
  digest or a shared salt would be a real vulnerability in a system that stores email
  addresses.
* A password is never logged, echoed into a template, or written to the database in any form
  other than its hash.
* Session state holds only the user id. Everything else is re-read per request, so a revoked
  or deactivated account cannot keep acting on a stale session.
* Login failure messages are identical for "no such user" and "wrong password", so the form
  cannot be used to enumerate registered addresses.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

from ..config import MarketConfig, TradeConfig
from ..db import Database

#: Minimum password length. Kept low because this is a free research tool, but not zero.
MIN_PASSWORD_LENGTH = 8

#: Deliberately permissive: enough to catch a typo, not enough to reject valid addresses.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(Exception):
    """A user-facing authentication problem with a safe message."""


@dataclass
class User:
    id: int
    email: str
    display_name: str | None
    access_level: str
    created_at: str
    last_login_at: str | None

    @property
    def is_pro(self) -> bool:
        """Whether this user holds a paid tier. Payment is not implemented, so this is
        currently always False; it exists so authorization checks do not need rewriting."""
        return self.access_level in ("PRO", "TEAM")

    @property
    def is_admin(self) -> bool:
        """Whether this user may reach the internal operations view.

        ADMIN is deliberately separate from the paid tiers: an operator is not a customer, and
        a subscription must never imply access to internal data. The level is set only by the
        CLI, so no web request can grant it.
        """
        return self.access_level == "ADMIN"

    @property
    def display(self) -> str:
        return self.display_name or self.email.split("@")[0]


class AccountService:
    """Account creation, authentication and preferences."""

    def __init__(self, db: Database):
        self.db = db

    # --- validation -----------------------------------------------------------

    @staticmethod
    def validate_email(email: str) -> str:
        cleaned = (email or "").strip().lower()
        if not _EMAIL_RE.match(cleaned):
            raise AuthError("Enter a valid email address.")
        if len(cleaned) > 254:
            raise AuthError("Enter a valid email address.")
        return cleaned

    @staticmethod
    def validate_password(password: str) -> str:
        if not password or len(password) < MIN_PASSWORD_LENGTH:
            raise AuthError(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
            )
        if len(password) > 256:
            raise AuthError("Password is too long.")
        return password

    # --- lookup ---------------------------------------------------------------

    def _row_to_user(self, row: Any) -> User:
        return User(
            id=int(row["id"]),
            email=row["email"],
            display_name=row["display_name"],
            access_level=row["access_level"],
            created_at=row["created_at"],
            last_login_at=row["last_login_at"],
        )

    def get_user(self, user_id: int | None) -> User | None:
        if not user_id:
            return None
        row = self.db.conn.execute(
            "SELECT * FROM app_user WHERE id = ? AND is_active = 1", (user_id,)
        ).fetchone()
        return self._row_to_user(row) if row else None

    def email_exists(self, email: str) -> bool:
        return bool(
            self.db.conn.execute(
                "SELECT 1 FROM app_user WHERE email = ?", (email,)
            ).fetchone()
        )

    # --- lifecycle ------------------------------------------------------------

    def create_account(
        self,
        email: str,
        password: str,
        display_name: str | None,
        *,
        market: MarketConfig,
        trade: TradeConfig,
    ) -> User:
        """Create a free account and its default preferences.

        Preferences are seeded from the active market and trade rather than a literal, so a
        new user starts on whatever the product is currently configured to serve.
        """
        email = self.validate_email(email)
        password = self.validate_password(password)
        if self.email_exists(email):
            raise AuthError("An account with that email already exists.")

        now = datetime.now(timezone.utc).isoformat()
        cursor = self.db.conn.execute(
            """
            INSERT INTO app_user (email, password_hash, display_name, access_level,
                                  is_active, created_at)
            VALUES (?, ?, ?, 'FREE', 1, ?)
            """,
            (
                email,
                generate_password_hash(password),
                (display_name or "").strip()[:80] or None,
                now,
            ),
        )
        user_id = int(cursor.lastrowid)
        self.db.conn.execute(
            """
            INSERT INTO user_preference (user_id, market_id, trade_id, cities, project_types,
                                         notify_in_app, notify_email, updated_at)
            VALUES (?, ?, ?, '[]', '[]', 1, 0, ?)
            """,
            (user_id, market.id, trade.id, now),
        )
        self.db.conn.commit()
        return self.get_user(user_id)  # type: ignore[return-value]

    def authenticate(self, email: str, password: str) -> User:
        """Verify credentials, or raise a single generic error.

        The same message is used for an unknown address and a wrong password so the form
        cannot be used to discover which emails are registered.
        """
        generic = AuthError("Email or password is incorrect.")
        try:
            cleaned = self.validate_email(email)
        except AuthError:
            raise generic from None

        row = self.db.conn.execute(
            "SELECT * FROM app_user WHERE email = ?", (cleaned,)
        ).fetchone()
        if row is None or not check_password_hash(row["password_hash"], password or ""):
            raise generic
        if not row["is_active"]:
            raise AuthError("This account is not active.")

        now = datetime.now(timezone.utc).isoformat()
        self.db.conn.execute(
            "UPDATE app_user SET last_login_at = ? WHERE id = ?", (now, row["id"])
        )
        self.db.conn.commit()
        return self._row_to_user(row)

    # --- preferences ----------------------------------------------------------

    def get_preferences(self, user_id: int) -> dict[str, Any]:
        row = self.db.conn.execute(
            "SELECT * FROM user_preference WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return {}
        prefs = dict(row)
        for key in ("cities", "project_types"):
            try:
                prefs[key] = json.loads(prefs.get(key) or "[]")
            except (TypeError, ValueError):
                prefs[key] = []
        return prefs

    def update_preferences(
        self, user_id: int, *, market_id: str, trade_id: str,
        cities: list[str], project_types: list[str],
        notify_in_app: bool, notify_email: bool,
        min_value: float | None = None, max_value: float | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.db.conn.execute(
            """
            INSERT INTO user_preference (user_id, market_id, trade_id, cities, project_types,
                                         notify_in_app, notify_email, min_value, max_value,
                                         updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                market_id = excluded.market_id,
                trade_id = excluded.trade_id,
                cities = excluded.cities,
                project_types = excluded.project_types,
                notify_in_app = excluded.notify_in_app,
                notify_email = excluded.notify_email,
                min_value = excluded.min_value,
                max_value = excluded.max_value,
                updated_at = excluded.updated_at
            """,
            (
                user_id, market_id, trade_id,
                json.dumps(sorted(set(cities))), json.dumps(sorted(set(project_types))),
                1 if notify_in_app else 0, 1 if notify_email else 0,
                min_value, max_value, now,
            ),
        )
        self.db.conn.commit()

    # --- saved opportunities --------------------------------------------------

    def save_opportunity(self, user_id: int, project_id: int) -> bool:
        """Save the relationship only. Returns True when it was newly saved.

        No project fields are copied, so a re-ingest cannot leave a saved record holding a
        stale or contradictory fact.
        """
        exists = self.db.conn.execute(
            "SELECT 1 FROM project WHERE id = ?", (project_id,)
        ).fetchone()
        if not exists:
            return False
        cursor = self.db.conn.execute(
            """
            INSERT OR IGNORE INTO saved_opportunity (user_id, project_id, saved_at)
            VALUES (?, ?, ?)
            """,
            (user_id, project_id, datetime.now(timezone.utc).isoformat()),
        )
        self.db.conn.commit()
        return cursor.rowcount > 0

    def unsave_opportunity(self, user_id: int, project_id: int) -> bool:
        cursor = self.db.conn.execute(
            "DELETE FROM saved_opportunity WHERE user_id = ? AND project_id = ?",
            (user_id, project_id),
        )
        self.db.conn.commit()
        return cursor.rowcount > 0

    def saved_project_ids(self, user_id: int) -> list[int]:
        rows = self.db.conn.execute(
            "SELECT project_id FROM saved_opportunity WHERE user_id = ? ORDER BY saved_at DESC",
            (user_id,),
        ).fetchall()
        return [int(r["project_id"]) for r in rows]

    # --- alerts (architecture only) -------------------------------------------

    def record_alert(self, user_id: int, project_id: int, kind: str = "new_match") -> None:
        """Record a matching event. Delivery is intentionally not implemented."""
        self.db.conn.execute(
            """
            INSERT OR IGNORE INTO alert_event (user_id, project_id, kind, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user_id, project_id, kind, datetime.now(timezone.utc).isoformat()),
        )
        self.db.conn.commit()

    def unread_alert_count(self, user_id: int) -> int:
        return int(
            self.db.conn.execute(
                "SELECT COUNT(*) FROM alert_event WHERE user_id = ? AND read_at IS NULL",
                (user_id,),
            ).fetchone()[0]
        )


def record_analytics(
    db: Database, event_name: str, *, project_id: int | None = None,
    market_id: str | None = None, trade_id: str | None = None,
) -> None:
    """Record a product event.

    Intentionally narrow: an event name, an optional project, and the market/trade. No IP
    address, no user agent, no free-text payload, and no link to a user account, so the
    analytics table cannot become a record of who looked at what.
    """
    db.conn.execute(
        """
        INSERT INTO analytics_event (event_name, project_id, market_id, trade_id, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (event_name, project_id, market_id, trade_id, datetime.now(timezone.utc).isoformat()),
    )
    db.conn.commit()