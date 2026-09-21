"""City of Dallas building permits.

Two Dallas sources exist:

* `dallas_gis_permits` — an ArcGIS layer covering FY2023-24. Verified to end 2023-12-29.
* `dallas_permits_socrata` — the Socrata dataset, verified to end 2019-12-31.

Both are implemented here. Both are historical, and the connector reports that honestly
via its coverage metadata. Dallas is the largest city in the market, so leaving it out
entirely would misrepresent coverage; presenting 2019 rows as new opportunities would be
worse. The pipeline carries the source's own maximum date through to the dashboard.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator

from ..models import (
    Permit,
    RawPermit,
    clean_text,
    normalize_address,
    parse_date,
    parse_money,
    parse_sqft,
)
from .base import BaseConnector

log = logging.getLogger(__name__)


class DallasGisPermitsConnector(BaseConnector):
    """Dallas FY2023-24 permits from the DallasGIS ArcGIS service."""

    source_id = "dallas_gis_permits"

    OUT_FIELDS = (
        "PERMIT_No,PERMIT_TYPE,ACTIVITY,LAND_USE,COMMERCIAL,STATUS_NAME,STATUS_DESCRIPTION,"
        "ADDRESS,ZIP_CODE,ISSUE_DATE,CREATED_DATE,CONTRACTOR_NAME,VALUE,AREA,UNITS"
    )

    def fetch_raw(self, since: Any = None, batch_size: int | None = None) -> Iterator[RawPermit]:
        batch_size = batch_size or self.page_size
        offset = 0
        page = 0
        while page < self.max_pages:
            params = {
                "where": "1=1",
                "outFields": self.OUT_FIELDS,
                "returnGeometry": "false",
                "orderByFields": "ISSUE_DATE DESC",
                "resultOffset": offset,
                "resultRecordCount": batch_size,
                "f": "json",
            }
            data = self.get_json(f"{self.config.base_url}/query", params=params)
            if "error" in data:
                raise RuntimeError(f"ArcGIS error: {data['error']}")
            features = data.get("features", [])
            if not features:
                break
            for feature in features:
                row = feature.get("attributes", {})
                if not row.get("PERMIT_No"):
                    continue
                yield RawPermit(
                    source_id=self.source_id,
                    natural_key=self.natural_key(row),
                    payload=row,
                    source_url=self.config.portal_url,
                )
            offset += len(features)
            page += 1
            if len(features) < batch_size:
                break

    def natural_key(self, row: dict[str, Any]) -> str:
        return f"{row.get('PERMIT_No')}::{row.get('PERMIT_TYPE')}::{row.get('ACTIVITY')}"

    def normalize(self, raw: RawPermit) -> Permit | None:
        row = raw.payload
        permit_type = clean_text(row.get("PERMIT_TYPE"))
        activity = clean_text(row.get("ACTIVITY"))

        # The dataset exposes an explicit commercial flag; trust it when present.
        commercial_flag = clean_text(row.get("COMMERCIAL"))
        is_commercial: bool | None = None
        if commercial_flag:
            is_commercial = commercial_flag.upper() == "Y"

        combined_type = " ".join(p for p in (permit_type, activity) if p) or None

        return Permit(
            source_id=self.source_id,
            permit_number=str(row.get("PERMIT_No")),
            natural_key=raw.natural_key,
            permit_type=combined_type,
            permit_subtype=None,
            permit_date=parse_date(row.get("ISSUE_DATE")),
            status=clean_text(row.get("STATUS_NAME")),
            address=clean_text(row.get("ADDRESS")),
            city=self.config.jurisdiction_city,
            state=self.config.jurisdiction_state,
            zip_code=clean_text(row.get("ZIP_CODE")),
            work_description=clean_text(row.get("STATUS_DESCRIPTION")),
            land_use=clean_text(row.get("LAND_USE")),
            specific_use=None,
            job_value=parse_money(row.get("VALUE")),
            square_footage=parse_sqft(row.get("AREA")),
            owner=None,
            contractor=clean_text(row.get("CONTRACTOR_NAME")),
            is_commercial=is_commercial,
            source_url=raw.source_url,
            source_date=parse_date(row.get("ISSUE_DATE")),
            raw=row,
        )


class DallasPermitsSocrataConnector(BaseConnector):
    """Dallas building permits from Socrata. Historical (ends 2019-12-31)."""

    source_id = "dallas_permits_socrata"
    PAGE_LIMIT = 50000

    def _endpoint(self) -> str:
        return f"https://{self.config.domain}/resource/{self.config.dataset_id}.json"

    def fetch_raw(self, since: Any = None, batch_size: int | None = None) -> Iterator[RawPermit]:
        batch_size = batch_size or self.page_size
        offset = 0
        page = 0
        while page < self.max_pages:
            params: dict[str, Any] = {
                "$limit": batch_size,
                "$offset": offset,
                "$order": "issued_date DESC",
            }
            if since is not None:
                params["$where"] = f"issued_date > '{since}'"
            rows = self.get_json(self._endpoint(), params=params)
            if not rows:
                break
            for row in rows:
                if not row.get("permit_number"):
                    continue
                yield RawPermit(
                    source_id=self.source_id,
                    natural_key=self.natural_key(row),
                    payload=row,
                    source_url=self.convert_url(row),
                )
            offset += len(rows)
            page += 1
            if len(rows) < batch_size:
                break

    def convert_url(self, row: dict[str, Any]) -> str | None:
        permit = row.get("permit_number")
        if not permit or not self.config.source_url_template:
            return None
        return self.config.source_url_template.format(permit_number=permit)

    def natural_key(self, row: dict[str, Any]) -> str:
        return f"{row.get('permit_number')}::{row.get('permit_type')}"

    def normalize(self, raw: RawPermit) -> Permit | None:
        row = raw.payload
        permit_type = clean_text(row.get("permit_type"))
        land_use = clean_text(row.get("land_use"))

        is_commercial: bool | None = None
        if permit_type:
            lowered = permit_type.lower()
            if "commercial" in lowered:
                is_commercial = True
            elif any(k in lowered for k in ("single family", "multi family")):
                is_commercial = False

        return Permit(
            source_id=self.source_id,
            permit_number=str(row.get("permit_number")),
            natural_key=raw.natural_key,
            permit_type=permit_type,
            permit_subtype=None,
            permit_date=parse_date(row.get("issued_date")),
            status=None,
            address=clean_text(row.get("street_address")),
            city=self.config.jurisdiction_city,
            state=self.config.jurisdiction_state,
            zip_code=clean_text(row.get("zip_code")),
            work_description=clean_text(row.get("work_description")),
            land_use=land_use,
            specific_use=None,
            job_value=parse_money(row.get("value")),
            square_footage=parse_sqft(row.get("area")),
            owner=None,
            contractor=clean_text(row.get("contractor")),
            is_commercial=is_commercial,
            source_url=raw.source_url,
            source_date=parse_date(row.get("issued_date")),
            raw=row,
        )