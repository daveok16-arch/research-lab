"""Plans, subscriptions and entitlements.

Three separate concepts, deliberately not collapsed:

* A **plan** is a catalogue entry: a name, a rank and the set of features it grants. It is
  configuration, seeded from `plan.py`'s defaults, and holds no customer data.
* A **subscription** is an account's recorded relationship to a plan, with a status. It is
  written only by an operator command or an external billing system, never by a web request.
* An **entitlement** is the resulting answer to "may this account use this feature", computed
  from the plan plus its status and the account's access level.

Payment is not implemented, and nothing here pretends otherwise. What this module guarantees
is the property that matters for a commercial product: an account cannot grant itself access.
`entitlements_for` returns only what the stored subscription and access level justify, so a
FREE account that never paid cannot reach a paid feature by asking nicely or by editing a form.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..db import Database
from .accounts import User

#: Subscription statuses. Only ACTIVE and TRIALING grant a plan's entitlements; everything
#: else falls back to the free tier, so a lapsed or cancelled subscription loses access
#: without anyone having to remember to revoke it.
STATUS_ACTIVE = "ACTIVE"
STATUS_TRIAL = "TRIALING"
STATUS_PAST_DUE = "PAST_DUE"
STATUS_CANCELED = "CANCELED"
GRANTING_STATUSES = (STATUS_ACTIVE, STATUS_TRIAL)

#: Feature keys the application checks. A feature that is not listed is not a paid feature,
#: so adding a gated feature is an explicit change rather than an accident.
FEATURE_EXPORT = "export"
FEATURE_ALERTS = "alerts"
FEATURE_TEAM = "team"
FEATURE_API = "api"

#: Plan catalogue. `rank` orders the plans for display; `entitlements` is what the plan
#: grants. FREE is rank 0 and grants nothing beyond the public product.
DEFAULT_PLANS: tuple[dict[str, Any], ...] = (
    {
        "id": "FREE",
        "name": "Free",
        "rank": 0,
        "description": "Browse the directory, save opportunities and set preferences.",
        "entitlements": [],
    },
    {
        "id": "PRO",
        "name": "Professional",
        "rank": 1,
        "description": "Monitoring, alerts and export for an individual estimator.",
        "entitlements": [FEATURE_ALERTS, FEATURE_EXPORT],
    },
    {
        "id": "TEAM",
        "name": "Team",
        "rank": 2,
        "description": "Shared pipeline and assignment across an organization.",
        "entitlements": [FEATURE_ALERTS, FEATURE_EXPORT, FEATURE_TEAM, FEATURE_API],
    },
)

#: The plan every account falls back to. Not configurable, because a missing fallback would
#: mean an unentitled account, which is indistinguishable from a broken account.
FREE_PLAN = "FREE"


#: The plan id used for an operator's resolved entitlement. It is not a catalogue entry, because
#: an operator is not a customer and there is no price to attach.
OPERATOR_PLAN = "ADMIN"


@dataclass
class Entitlement:
    """The resolved access an account holds, and why."""

    plan_id: str
    plan_name: str
    status: str
    features: frozenset[str] = field(default_factory=frozenset)
    source: str = "default"

    def has(self, feature: str) -> bool:
        return feature in self.features

    @property
    def is_paid(self) -> bool:
        """Whether the account holds a paid commercial tier.

        An operator deliberately does not count: they hold every feature because they run the
        service, not because they bought anything. Reporting an internal account as paid would
        make a support screen or a metric wrong.
        """
        return (
            self.plan_id not in (FREE_PLAN, OPERATOR_PLAN)
            and self.status in GRANTING_STATUSES
        )


class SubscriptionService:
    """Plan catalogue, subscriptions and entitlement resolution."""

    def __init__(self, db: Database):
        self.db = db

    # --- catalogue ------------------------------------------------------------

    def ensure_plans(self) -> None:
        """Seed the plan catalogue. Idempotent, and additive when a plan is added in code."""
        now = datetime.now(timezone.utc).isoformat()
        for plan in DEFAULT_PLANS:
            self.db.conn.execute(
                """
                INSERT INTO plan (id, name, rank, description, entitlements, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    rank = excluded.rank,
                    description = excluded.description,
                    entitlements = excluded.entitlements,
                    updated_at = excluded.updated_at
                """,
                (
                    plan["id"], plan["name"], plan["rank"], plan["description"],
                    json.dumps(sorted(plan["entitlements"])), now,
                ),
            )
        self.db.conn.commit()

    def plans(self) -> list[dict[str, Any]]:
        rows = self.db.conn.execute("SELECT * FROM plan ORDER BY rank, id").fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["entitlements"] = json.loads(item.get("entitlements") or "[]")
            except (TypeError, ValueError):
                item["entitlements"] = []
            out.append(item)
        return out

    def plan(self, plan_id: str) -> dict[str, Any] | None:
        return next((p for p in self.plans() if p["id"] == plan_id), None)

    # --- subscription ---------------------------------------------------------

    def subscription_for(self, user_id: int) -> dict[str, Any] | None:
        row = self.db.conn.execute(
            "SELECT * FROM subscription WHERE user_id = ?", (user_id,)
        ).fetchone()
        return dict(row) if row else None

    def set_subscription(
        self, user_id: int, plan_id: str, status: str,
        *, external_ref: str | None = None,
    ) -> None:
        """Record an account's plan relationship.

        Called only from the operator CLI or an external billing integration. There is no web
        route that reaches this method, which is what keeps an account from upgrading itself.
        """
        if self.plan(plan_id) is None:
            raise ValueError(f"Unknown plan {plan_id!r}")
        now = datetime.now(timezone.utc).isoformat()
        existing = self.subscription_for(user_id)
        self.db.conn.execute(
            """
            INSERT INTO subscription (user_id, plan_id, status, external_ref, started_at,
                renewed_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                plan_id = excluded.plan_id,
                status = excluded.status,
                external_ref = excluded.external_ref,
                renewed_at = CASE
                    WHEN subscription.plan_id <> excluded.plan_id THEN NULL
                    ELSE subscription.renewed_at END,
                updated_at = excluded.updated_at
            """,
            (user_id, plan_id, status, external_ref, now, now if existing else None, now),
        )
        self.db.conn.commit()

    def clear_subscription(self, user_id: int) -> None:
        self.db.conn.execute("DELETE FROM subscription WHERE user_id = ?", (user_id,))
        self.db.conn.commit()

    # --- entitlement resolution -----------------------------------------------

    def entitlements_for(self, user: User | None) -> Entitlement:
        """Resolve what an account may actually use.

        Resolution order is deliberate and narrow: an operator holds every feature because
        they run the service; otherwise a subscription in a granting status supplies its
        plan's features; otherwise the account is on the free plan with no gated features.
        A subscription in any other status grants nothing beyond free, so an expired plan
        loses access automatically.
        """
        free = self.plan(FREE_PLAN) or {"id": FREE_PLAN, "name": "Free", "entitlements": []}

        if user is None:
            return Entitlement(
                plan_id=FREE_PLAN, plan_name=free["name"], status="ANONYMOUS",
                features=frozenset(free["entitlements"]), source="anonymous",
            )

        if user.is_admin:
            every = {feature for plan in self.plans() for feature in plan["entitlements"]}
            return Entitlement(
                plan_id=OPERATOR_PLAN, plan_name="Operator", status=STATUS_ACTIVE,
                features=frozenset(every), source="operator",
            )

        subscription = self.subscription_for(user.id)
        if subscription and subscription["status"] in GRANTING_STATUSES:
            plan = self.plan(subscription["plan_id"]) or free
            return Entitlement(
                plan_id=plan["id"], plan_name=plan["name"], status=subscription["status"],
                features=frozenset(plan.get("entitlements") or []), source="subscription",
            )

        if subscription:
            # A recorded subscription that is not in a granting status. Access is free, but the
            # status is surfaced so the account is told why rather than silently downgraded.
            return Entitlement(
                plan_id=FREE_PLAN, plan_name=free["name"], status=subscription["status"],
                features=frozenset(free["entitlements"]), source="lapsed",
            )

        return Entitlement(
            plan_id=FREE_PLAN, plan_name=free["name"], status="FREE",
            features=frozenset(free["entitlements"]), source="default",
        )
