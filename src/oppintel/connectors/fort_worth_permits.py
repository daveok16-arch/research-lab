"""Fort Worth Development Permits connector (ArcGIS REST).

Verified live during Phase 1 reconnaissance. The distinguishing value of this source is
that it carries a first-class `Mechanical` permit type, which provides Tier-1 HVAC
evidence as structured data with no inference required.
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

# Permit types that carry commercial scope. Mechanical/Plumbing/Electrical rows are
# included because they are how Tier-1 mechanical evidence is discovered.
HIGH_VALUE_TYPES = {
    "commercial building permit",
    "mechanical",
    "electrical",
    "plumbing",
    "commercial grading permit",
    "commercial accessory structure",
    "mechanical design review",
}

# Use types that indicate non-commercial work, excluded before assembly.
RESIDENTIAL_USE_TYPES = {
    "single family residence",
    "single family residence - model home",
    "duplex",
    "townhouse",
    "pool or spa",
    "pool house",
    "residential accessory struct",
    "storage shed",
    "carport",
    "boat dock",
    "fence",
    "deck - ramp - stair",
    "irrigation",
    "irrigation and backflow",
    "residential alternate energy",
    "accessory dwelling",
    "fire pit or fire place",
    "flag pole",
    "retaining wall",
}

OUT_FIELDS = (
    "Permit_No,Permit_Type,Permit_SubType,Permit_Category,B1_WORK_DESC,Address,"
    "Zip_Code,Owner_Full_Name,File_Date,Current_Status,Status_Date,JobValue,Use_Type,"
    "Specific_Use,SqFt,Latitude,Longitude"
)


class FortWorthPermitsConnector(BaseConnector):
    source_id = "fort_worth_permits"

    def _query_url(self) -> str:
        return f"{self.config.base_url}/query"

    def fetch_raw(self, since: Any = None, batch_size: int | None = None) -> Iterator[RawPermit]:
        batch_size = batch_size or self.page_size
        offset = 0
        page = 0

        # Pending/Denied/Withdrawn rows are all useful for a complete record, so no status
        # filter is applied. The commercial filter is applied during normalization.
        where = "1=1"
        if since is not None:
            where = f"File_Date >= timestamp '{since} 00:00:00'"

        while page < self.max_pages:
            params = {
                "where": where,
                "outFields": OUT_FIELDS,
                "returnGeometry": "false",
                "orderByFields": "File_Date DESC",
                "resultOffset": offset,
                "resultRecordCount": batch_size,
                "f": "json",
            }
            data = self.get_json(self._query_url(), params=params)
            if "error" in data:
                raise RuntimeError(f"ArcGIS error: {data['error']}")
            features = data.get("features", [])
            if not features:
                break
            for feature in features:
                row = feature.get("attributes", {})
                if not row.get("Permit_No"):
                    continue
                key = self.natural_key(row)
                yield RawPermit(
                    source_id=self.source_id,
                    natural_key=key,
                    payload=row,
                    source_url=self._record_url(row),
                )
            offset += len(features)
            page += 1
            if len(features) < batch_size:
                break

    def natural_key(self, row: dict[str, Any]) -> str:
        # Permit numbers repeat across permit types in this dataset, so the type is part of
        # the identity. The source itself exposes Unique_ID; prefer it when present.
        if row.get("Unique_ID"):
            return str(row["Unique_ID"])
        return f"{row.get('Permit_No')}::{row.get('Permit_Type')}"

    def _record_url(self, row: dict[str, Any]) -> str | None:
        permit_no = row.get("Permit_No")
        if not permit_no:
            return None
        if self.config.source_url_template:
            return self.config.source_url_template.format(permit_number=permit_no)
        return None

    def normalize(self, raw: RawPermit) -> Permit | None:
        row = raw.payload
        permit_type = clean_text(row.get("Permit_Type"))
        use_type = clean_text(row.get("Use_Type"))

        is_commercial = self._is_commercial(permit_type, use_type)

        city = self.config.jurisdiction_city
        # The dataset is Fort Worth only, but a small number of rows are filed by other
        # jurisdictions. The issuer is not exposed as a field here, so the jurisdiction is
        # recorded from the source definition rather than guessed per row.

        return Permit(
            source_id=self.source_id,
            permit_number=str(row.get("Permit_No")),
            natural_key=raw.natural_key,
            permit_type=permit_type,
            permit_subtype=clean_text(row.get("Permit_SubType")),
            permit_date=parse_date(row.get("File_Date")),
            status=clean_text(row.get("Current_Status")),
            address=clean_text(row.get("Address")),
            city=city,
            state=self.config.jurisdiction_state,
            zip_code=clean_text(row.get("Zip_Code")),
            work_description=clean_text(row.get("B1_WORK_DESC")),
            land_use=use_type,
            specific_use=clean_text(row.get("Specific_Use")),
            job_value=parse_money(row.get("JobValue")),
            square_footage=parse_sqft(row.get("SqFt")),
            owner=clean_text(row.get("Owner_Full_Name")),
            # Fort Worth does not publish a contractor field on this layer, so it stays
            # None rather than being filled from an unreliable source.
            contractor=None,
            is_commercial=is_commercial,
            source_url=raw.source_url,
            source_date=parse_date(row.get("File_Date")),
            raw=row,
        )

    def _is_commercial(self, permit_type: str | None, use_type: str | None) -> bool | None:
        """Classify a row as commercial, residential, or unknown.

        Returns None when the source does not provide enough information, which keeps the
        row in the candidate pool rather than silently discarding it.
        """
        if use_type:
            normalized = use_type.lower().strip()
            if normalized in RESIDENTIAL_USE_TYPES:
                return False
        if not permit_type:
            return None
        normalized_type = permit_type.lower().strip()
        if normalized_type in HIGH_VALUE_TYPES:
            return True
        if "residential" in normalized_type:
            return False
        return None