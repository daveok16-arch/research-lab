"""Programmatic-SEO quality gate.

The single most damaging thing a programmatic SEO layer can do is publish a page for every
market/trade/location combination the configuration can express. Hundreds of near-empty pages
dilute the good ones, waste crawl budget, and — worst for this product — make public claims
that nothing backs.

So every programmatic page must clear an explicit, data-driven gate before it is treated as
indexable. The gate is deliberately mechanical:

* The page must have enough real records to be useful, not merely one or two.
* A trade page must have enough records carrying that trade's *evidence*, because a trade
  directory with no evidence is exactly the unbacked claim this product refuses to make.
* The gate result is returned as data, so the route can render a real page while the metadata
  layer marks it `noindex` — the page is still reachable and honest, it is simply not offered
  to crawlers as a destination until the data justifies it.

Nothing here invents a count. Every figure comes from the same `OpportunityService` the public
site renders, so a page cannot clear the gate on one number and display a different one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Default thresholds, overridable per page in `config/keywords.yaml`. Chosen so a page needs a
#: handful of genuine records before it is worth a crawler's attention.
DEFAULT_MIN_PROJECTS = 5
DEFAULT_MIN_MECHANICAL = 3


@dataclass
class GateResult:
    """Whether a page cleared its quality gate, and why."""

    indexable: bool
    failures: list[str] = field(default_factory=list)
    thresholds: dict[str, int] = field(default_factory=dict)
    observed: dict[str, int] = field(default_factory=dict)

    @property
    def reason(self) -> str:
        """A short, honest explanation for the operations report."""
        if self.indexable:
            return "meets the minimum data requirement for indexing"
        return "; ".join(self.failures)


def evaluate_gate(
    *,
    stats: dict[str, Any],
    quality_gate: dict[str, int] | None,
    page_label: str,
) -> GateResult:
    """Compare a page's real counts against its configured thresholds.

    `stats` is the mapping `OpportunityService.statistics_for` returns, so the numbers checked
    here are the same ones the page renders.
    """
    thresholds = dict(quality_gate or {})
    min_projects = int(thresholds.get("min_projects", DEFAULT_MIN_PROJECTS))
    min_mechanical = int(thresholds.get("min_mechanical", 0))

    projects = int(stats.get("projects_public", 0) or 0)
    mechanical = int(stats.get("with_mechanical", 0) or 0)

    observed = {"projects": projects, "mechanical": mechanical}
    failures: list[str] = []

    if projects < min_projects:
        failures.append(
            f"{page_label} holds {projects} discoverable project(s), below the "
            f"{min_projects} required to index"
        )
    if min_mechanical and mechanical < min_mechanical:
        failures.append(
            f"{page_label} holds {mechanical} project(s) with the trade's evidence, below the "
            f"{min_mechanical} required to index"
        )

    return GateResult(
        indexable=not failures,
        failures=failures,
        thresholds={"min_projects": min_projects, "min_mechanical": min_mechanical},
        observed=observed,
    )
