"""Collin CAD building permits (Socrata, data.texas.gov).

Verified live during Phase 1 reconnaissance: commercial permits run through 2026 and
carry owner name, declared value, and building area. Covers Plano, Frisco, McKinney,
Celina, Wylie and the other Collin County jurisdictions, which is a large share of new
commercial construction in the northern DFW corridor.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

from ..models import (
    Permit,
    RawPermit,
    clean_text,
    parse_date,
    parse_money,
    parse_sqft,
)
from .base import BaseConnector

log = logging.getLogger(__name__)

# Issuers seen in this dataset that fall inside the target market.
DFW_JURISDICTIONS = {
    "PLANO CITY": "Plano",
    "FRISCO CITY": "Frisco",
    "MCKINNEY CITY": "McKinney",
    "CELINA CITY": "Celina",
    "WYLIE CITY": "Wylie",
    "ALLEN CITY": "Allen",
    "PROSPER CITY": "Prosper",
    "PRINCETON CITY": "Princeton",
    "MELISSA CITY": "Melissa",
    "ANNA CITY": "Anna",
    "LAVON CITY": "Lavon",
    "COLLIN COUNTY MUD #05": None,
}


class CollinCadPermitsConnector(BaseConnector):
    source_id = "collin_cad_permits"

    def _endpoint(self) -> str:
        return f"https://{self.config.domain}/resource/{self.config.dataset_id}.json"

    def fetch_raw(self, since: Any = None, batch_size: int | None = None) -> Iterator[RawPermit]:
        batch_size = batch_size or self.page_size
        offset = 0
        page = 0

        # Filter to commercial at the source: this dataset is overwhelmingly residential
        # (100k+ homes versus ~12k commercial), so pushing the filter down keeps the crawl
        # small and polite.
        where = "proprescom = 'Commercial'"
        if since is not None:
            where += f" AND permitissueddate > '{since}T00:00:00.000'"

        while page < self.max_pages:
            params: dict[str, Any] = {
                "$limit": batch_size,
                "$offset": offset,
                "$order": "permitissueddate DESC",
                "$where": where,
            }
            rows = self.get_json(self._endpoint(), params=params)
            if not rows:
                break
            for row in rows:
                if not row.get("permitid"):
                    continue
                yield RawPermit(
                    source_id=self.source_id,
                    natural_key=self.natural_key(row),
                    payload=row,
                    source_url=self.record_url(row),
                )
            offset += len(rows)
            page += 1
            if len(rows) < batch_size:
                break

    def natural_key(self, row: dict[str, Any]) -> str:
        return str(row.get("permitid"))

    def record_url(self, row: dict[str, Any]) -> str | None:
        if not self.config.source_url_template:
            return None
        return self.config.source_url_template.format(permit_id=row.get("permitid"))

    def normalize(self, raw: RawPermit) -> Permit | None:
        row = raw.payload
        permit_type = clean_text(row.get("permittypedescr"))
        issuer = clean_text(row.get("permitissuedby"))

        city = DFW_JURISDICTIONS.get((issuer or "").upper())
        if city is None:
            # Fall back to the situs city, which the dataset populates directly.
            situs_city = clean_text(row.get("situscity"))
            if situs_city:
                city = situs_city.title()

        return Permit(
            source_id=self.source_id,
            permit_number=clean_text(row.get("permitnum")) or str(row.get("permitid")),
            natural_key=raw.natural_key,
            permit_type=permit_type,
            permit_subtype=None,
            permit_date=parse_date(row.get("permitissueddate")),
            status=None,
            address=clean_text(row.get("situsconcat")) or clean_text(row.get("situsconcatshort")),
            city=city,
            state=self.config.jurisdiction_state,
            zip_code=clean_text(row.get("situszip")),
            work_description=clean_text(row.get("propabssubname")),
            land_use=None,
            specific_use=None,
            job_value=parse_money(row.get("permitvalue")),
            square_footage=parse_sqft(row.get("permitbldgarea")) or parse_sqft(row.get("propmainarea")),
            owner=clean_text(row.get("propownername")),
            # The dataset records the permit applicant/builder, not a general contractor
            # of record, and does not distinguish the two. It is not asserted as a GC.
            contractor=None,
            is_commercial=True,
            source_url=raw.source_url,
            source_date=parse_date(row.get("datadate")),
            raw=row,
        )