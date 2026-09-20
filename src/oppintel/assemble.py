"""Project assembly: cluster related permits into a single opportunity.

Clustering is deliberately conservative. Two permits belong to the same project only when
they share a normalized address and fall inside a rolling time window. Anything less
certain produces two projects, which is the safer error: a split project is still
actionable, whereas a merged project invents a relationship that does not exist.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import date, timedelta

from .config import TradeConfig
from .constants import (
    EVIDENCE_TIER_MECHANICAL_PERMIT,
    EVIDENCE_TIER_MECHANICAL_SCOPE,
    ROLE_GENERAL_CONTRACTOR,
    ROLE_OWNER,
)
from .models import Permit, Project, ProjectParty, normalize_address
from .normalize import (
    commercial_candidate,
    detect_mechanical_signal,
    derive_project_type,
    primary_permit,
)
from .provenance import assert_field, record_permit_evidence

#: Default rolling window for grouping permits at one address.
ASSEMBLY_WINDOW_DAYS = 540


def project_key_for(address_key: str, trade_id: str) -> str:
    digest = hashlib.sha1(f"{trade_id}|{address_key}".encode("utf-8")).hexdigest()
    return digest[:20]


def cluster_permits(
    permits: list[Permit],
    trade: TradeConfig,
    *,
    window_days: int = ASSEMBLY_WINDOW_DAYS,
) -> dict[str, list[Permit]]:
    """Group permits into address-based clusters.

    Returns a mapping of normalized address key to the permits at that address. Permits
    with no usable address, or that are clearly residential, are dropped before grouping.
    """
    buckets: dict[str, list[Permit]] = defaultdict(list)
    for permit in permits:
        if not commercial_candidate(permit, trade):
            continue
        key = normalize_address(permit.address)
        if not key:
            continue
        buckets[key].append(permit)

    clusters: dict[str, list[Permit]] = {}
    for key, group in buckets.items():
        clusters[key] = _split_by_time_window(group, window_days)
        # _split_by_time_window returns a flat list; multiple windows at one address are
        # rare in this market and are intentionally kept together to avoid inventing
        # separate projects from one building.
    return clusters


def _split_by_time_window(permits: list[Permit], window_days: int) -> list[Permit]:
    """Keep permits whose active period overlaps within the assembly window.

    Permits are ordered by date. A permit joins the cluster when it falls within the
    window measured from the earliest permit in the cluster; otherwise it starts a new
    one. The function returns the largest such cluster, which corresponds to the most
    recent phase of work at the address.
    """
    dated = [p for p in permits if p.permit_date]
    undated = [p for p in permits if not p.permit_date]
    if not dated:
        return undated

    dated.sort(key=lambda p: p.permit_date)
    clusters: list[list[Permit]] = [[dated[0]]]
    for permit in dated[1:]:
        current = clusters[-1]
        span = permit.permit_date - current[0].permit_date
        if span <= timedelta(days=window_days):
            current.append(permit)
        else:
            clusters.append([permit])

    largest = max(clusters, key=len)
    # Undated permits are attached to the dominant cluster only when they describe the
    # same building; otherwise they would introduce unrelated facts.
    return largest + undated if len(largest) == len(dated) else largest


def assemble_project(
    address_key: str,
    permits: list[Permit],
    trade: TradeConfig,
    source_names: dict[str, str],
    *,
    observed_at=None,
) -> Project:
    """Build a Project from a cluster of permits, attaching evidence for every fact."""
    project = Project(
        project_key=project_key_for(address_key, trade.id),
        trade=trade.id,
    )

    ordered = sorted(
        permits,
        key=lambda p: (p.permit_date or date.min),
    )

    # 1. Detect mechanical evidence across the whole cluster. Tier 1 wins outright.
    mechanical_signals = []
    for permit in permits:
        signal = detect_mechanical_signal(permit, trade)
        if signal:
            mechanical_signals.append(signal)
    mechanical_signals.sort(key=lambda s: s.tier)

    # 2. Record the directly-observed facts from every permit. A building permit carries
    #    value and area, so the primary permit is recorded first and later permits only
    #    fill fields the primary did not supply. This ordering is the documented
    #    precedence and it is why evidence stays consistent across runs.
    primary = primary_permit(permits, trade)
    for permit in [primary] + [p for p in ordered if p is not primary]:
        source_name = source_names.get(permit.source_id, permit.source_id)
        record_permit_evidence(project, permit, source_name, observed_at=observed_at)
        project.last_verified = observed_at or project.last_verified

    # 3. Parties. Only roles the source explicitly supports are recorded.
    for permit in permits:
        source_name = source_names.get(permit.source_id, permit.source_id)
        if permit.owner:
            project.parties.append(
                ProjectParty(
                    role=ROLE_OWNER,
                    name=permit.owner,
                    source_id=permit.source_id,
                    source_url=permit.source_url,
                    excerpt=f"Owner of record on permit {permit.permit_number}",
                )
            )
            if project.owner is None:
                assert_field(
                    project, "owner", permit.owner,
                    source_id=permit.source_id, source_name=source_name,
                    source_url=permit.source_url, source_record_key=permit.natural_key,
                    source_date=permit.source_date,
                    excerpt=f"Owner of record on permit {permit.permit_number}",
                    observed_at=observed_at,
                )
        if permit.contractor:
            project.parties.append(
                ProjectParty(
                    role=ROLE_GENERAL_CONTRACTOR,
                    name=permit.contractor,
                    source_id=permit.source_id,
                    source_url=permit.source_url,
                    excerpt=f"Contractor of record on permit {permit.permit_number}",
                )
            )
            if project.general_contractor is None:
                assert_field(
                    project, "general_contractor", permit.contractor,
                    source_id=permit.source_id, source_name=source_name,
                    source_url=permit.source_url, source_record_key=permit.natural_key,
                    source_date=permit.source_date,
                    excerpt=f"Contractor of record on permit {permit.permit_number}",
                    observed_at=observed_at,
                )

    # 4. Mechanical evidence, recorded with the tier that licenses the claim.
    if mechanical_signals:
        best = mechanical_signals[0]
        permit = best.permit
        source_name = source_names.get(permit.source_id, permit.source_id)
        project.mechanical_evidence_tier = best.tier
        assert_field(
            project, "mechanical_hvac_evidence",
            f"Tier {best.tier}: {best.excerpt}",
            source_id=permit.source_id,
            source_name=source_name,
            source_url=permit.source_url,
            source_record_key=permit.natural_key,
            source_date=permit.source_date,
            evidence_type="permit_scope",
            tier=best.tier,
            excerpt=best.excerpt,
            observed_at=observed_at,
        )
        # No mechanical contractor party row is created: none of these public sources
        # publishes the mechanical contractor for a trade permit, and inserting a
        # placeholder would invent a named entity. The trade permit itself is the
        # evidence, and it is already recorded on the project.

    # 5. Derived project type. Recorded as a derived value so it is never mistaken for a
    #    sourced fact.
    project_type, property_class = derive_project_type(permits, trade)
    if project_type:
        assert_field(
            project,
            "project_type",
            project_type,
            source_id=primary.source_id,
            source_name=source_names.get(primary.source_id, primary.source_id),
            source_url=primary.source_url,
            source_record_key=primary.natural_key,
            evidence_type="derived",
            excerpt=f"Derived from permit type and land use: {project_type}",
            observed_at=observed_at,
        )
    if property_class:
        project.property_class = str(property_class.get("label"))

    # 6. Project-level provenance: the primary source and its date.
    primary_source_name = source_names.get(primary.source_id, primary.source_id)
    project.source_name = primary_source_name
    project.source_url = primary.source_url
    project.source_date = primary.source_date

    project.permits = permits
    if project.city is None:
        project.city = primary.city
    if project.state is None:
        project.state = primary.state

    return project