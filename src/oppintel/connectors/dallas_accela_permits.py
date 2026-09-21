"""City of Dallas current permits via Accela Citizen Access (DallasNow).

This is the *current* Dallas source. The Socrata open-data extract and the FY2023-24 GIS
layer both stop in the past (2019-12-31 and 2023-12-29), which is why they are retained only
for historical analysis.

Mechanism, verified 2026-09-21 and documented in docs/dallas_source.md:

* The portal is ASP.NET WebForms. Search is a ``__doPostBack`` on ``btnNewSearch``.
* The POST is accepted only when all three hold: a ``__VIEWSTATE`` taken from a GET in the
  *same* session, the session cookies, and ``Origin``/``Referer`` headers. Without the
  Origin and Referer headers the server returns an error page for an otherwise valid
  payload.
* Every hidden field on the form must be echoed back unchanged, so the connector parses the
  form rather than hard-coding a subset of fields.
* Results paginate ten per page via ``$ctl13$ctlNN`` postbacks.
* The result counter saturates at the literal string "100+", so pagination terminates on
  "no new rows" rather than on a count.

``Commercial Mechanical Permit`` is a first-class category here, which yields Tier-1 HVAC
evidence directly from the City.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Any, Iterator

from ..models import (
    Permit,
    RawPermit,
    clean_text,
    parse_date,
    parse_money,
    parse_sqft,
)
from .base import BaseConnector, ConnectorError

log = logging.getLogger(__name__)

#: Permit-type option values for the commercially meaningful categories.
#: The value encodes Module/Category/SubCategory/Variant as the server expects.
COMMERCIAL_PERMIT_TYPES: dict[str, str] = {
    "commercial_new_construction": "Building/Commercial/New/NA",
    "commercial_alteration_addition": "Building/Commercial/Alteration Addition/NA",
    "commercial_mechanical": "Building/Commercial/Mechanical/NA",
    "commercial_electrical": "Building/Commercial/Electrical/NA",
    "commercial_plumbing": "Building/Commercial/Plumbing/NA",
    "commercial_roofing": "Building/Commercial/Roofing/NA",
    "commercial_accessory": "Building/Commercial/Accessory/NA",
    "commercial_demolition": "Building/Commercial/Demolition/NA",
    "commercial_solar_pv": "Building/Commercial/SolarPV/NA",
    "commercial_pool_spa": "Building/Commercial/PoolSpa/NA",
    "certificate_of_occupancy": "Building/Certificate of Occupancy/NA/NA",
    "site_plan_review": "Building/Site Plan/Review/NA",
    "fire_prevention": "Building/Fire/NA/NA",
    "grading": "Building/Grading/NA/NA",
}

#: Permit-type keyword used for Tier-1 mechanical detection, per source label.
MECHANICAL_TYPE_LABEL = "Commercial Mechanical Permit"

#: The result counter saturates at this literal value rather than reporting a true total.
#: The bound grows as the search is paged (page 1 reports "100+", page 11 reports "200+"),
#: so it is a lower bound on the result set, never an exact total.
SATURATED_COUNT_MARKER = "100+"

#: Matches the pager's "Next >" anchor and captures its postback target.
#: The label is the reliable signal, because pager control indices shift as the window
#: moves. The anchor markup was captured verbatim from the live portal on 2026-09-21:
#:   <a class="..." href="javascript:__doPostBack('...$ctl13$ctl14','')">Next &gt;</a>
NEXT_ANCHOR_RE = re.compile(r"__doPostBack\(&#39;([^&]+)&#39;,&#39;&#39;\)\">Next &gt;</a>")

PAGE_SIZE = 10


class _FormParser(HTMLParser):
    """Collect the aspnetForm's inputs and selects so the submission can be replayed."""

    def __init__(self) -> None:
        super().__init__()
        self.in_form = False
        self.fields: list[tuple[str, dict[str, Any]]] = []
        self._select: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "form" and (a.get("name") == "aspnetForm" or a.get("id") == "aspnetForm"):
            self.in_form = True
        if not self.in_form:
            return
        if tag == "input":
            self.fields.append(("input", a))
        elif tag == "select":
            self._select = {"name": a.get("name"), "selected": None}
        elif tag == "option" and self._select is not None and "selected" in a:
            self._select["selected"] = a.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "select" and self._select is not None:
            self.fields.append(("select", self._select))
            self._select = None
        elif tag == "form":
            self.in_form = False


def build_form(html: str) -> dict[str, str]:
    """Extract every form field with its current value.

    Replaying the complete form is what makes the POST acceptable to the server; sending a
    hand-picked subset produces an error page.
    """
    parser = _FormParser()
    parser.feed(html)
    data: dict[str, str] = {}
    for kind, attrs in parser.fields:
        if kind == "input":
            name = attrs.get("name")
            input_type = (attrs.get("type") or "text").lower()
            if not name or input_type in ("submit", "button", "image", "reset"):
                continue
            if input_type in ("checkbox", "radio"):
                # Only checked controls are submitted by a browser.
                if "checked" in attrs:
                    data[name] = attrs.get("value", "on")
            else:
                data[name] = attrs.get("value", "")
        elif kind == "select":
            name = attrs.get("name")
            if name:
                data[name] = attrs.get("selected") or ""
    return data


def parse_results(html: str) -> list[dict[str, str]]:
    """Parse the permit grid into row dicts.

    Rows are matched on a leading `MM/DD/YYYY` date cell, which is what distinguishes a data
    row from the header, filter, and pager rows that share the same CSS class.
    """
    rows: list[dict[str, str]] = []
    for block in re.split(r"<tr[^>]*class=\"ACA_TabRow", html)[1:]:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", block, re.S)
        values = [
            re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", cell)).strip() for cell in cells
        ]
        if len(values) < 5 or not re.match(r"^\d{2}/\d{2}/\d{4}$", values[1] or ""):
            continue
        rows.append(
            {
                "record_date": values[1],
                "record_number": values[2],
                "record_type": values[3],
                "address": values[4],
                "description": values[5] if len(values) > 5 else "",
                "project_name": values[6] if len(values) > 6 else "",
                "expiration_date": values[7] if len(values) > 7 else "",
                "status": values[8] if len(values) > 8 else "",
                "short_notes": values[10] if len(values) > 10 else "",
            }
        )
    return rows


def parse_total(html: str) -> tuple[int | None, bool]:
    """Return (total, saturated) for a result page.

    The interface reports a literal "100+" rather than a true count once a search exceeds
    one hundred matches, so callers must treat this as a lower bound. The saturating form is
    checked first, because a naive search for "N Record results matching" also matches inside
    "100+ Record results matching" and would silently report an exact total of 100.
    """
    match = re.search(r"(\d+)\+\s*Record results matching", html)
    if match:
        return int(match.group(1)), True
    match = re.search(r"(\d+)\s+Record results matching", html)
    if match:
        return int(match.group(1)), False
    return None, False


def _split_address(text: str | None) -> dict[str, str | None]:
    """Split the portal's combined address into street, city, and ZIP.

    Format is consistently "<street>, <city> TX <zip>", but the ZIP and even the city are
    sometimes absent, so each part is optional. When no address is published at all, the
    city is still known because this source only covers Dallas, so it is reported; the
    street is left absent rather than invented.
    """
    if not text:
        return {"street": None, "city": "Dallas", "zip": None}
    street = text
    city = None
    zip_code = None

    match = re.match(r"^(?P<street>.+?),\s*(?P<city>[^,]+?)\s+TX\s*(?P<zip>\d{5})?\s*$", text)
    if match:
        street = match.group("street")
        city = match.group("city")
        zip_code = match.group("zip")
    else:
        match = re.match(r"^(?P<street>.+?),\s*(?P<city>.+)$", text)
        if match:
            street = match.group("street")
            city = match.group("city")
    return {
        "street": clean_text(street),
        "city": clean_text(city) or "Dallas",
        "zip": clean_text(zip_code),
    }


def is_commercial_type(record_type: str | None) -> bool | None:
    """Classify a record type as commercial, residential, or unknown from its own label."""
    if not record_type:
        return None
    lowered = record_type.lower()
    if "residential" in lowered:
        return False
    if "commercial" in lowered:
        return True
    # Non-trade categories such as Certificate of Occupancy and Site Plan Review carry no
    # residential/commercial marker of their own, so the answer is genuinely unknown.
    return None


class DallasAccelaConnector(BaseConnector):
    """Current Dallas permits from Accela Citizen Access."""

    source_id = "dallas_accela_permits"

    #: The interface's default earliest date.
    DEFAULT_START = "10/09/2015"

    def __init__(self, config: Any, defaults: dict[str, Any] | None = None):
        super().__init__(config, defaults)
        # The Origin/Referer headers are mandatory; without them this endpoint rejects
        # otherwise-valid POSTs with an error page.
        #: Pages fetched per permit type on the last run. Surfaced as source metadata so a
        #: truncated crawl is visible rather than silently producing fewer records.
        self.pages_by_type: dict[str, int] = {}
        self.session.headers.update(
            {
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Origin": "https://aca-prod.accela.com",
                "Referer": self._home_url(),
                "Upgrade-Insecure-Requests": "1",
            }
        )

    def _home_url(self) -> str:
        return self.config.base_url

    # --- session ---------------------------------------------------------------

    def _fresh_form(self) -> dict[str, str]:
        """GET the search page and parse its form, in the current session.

        The viewstate is session-scoped, so this must not be cached or reused.
        """
        self.limiter.wait()
        response = self.session.get(self._home_url(), timeout=self.timeout)
        response.raise_for_status()
        form = build_form(response.text)
        if "__VIEWSTATE" not in form:
            raise ConnectorError(
                "Dallas Accela search page returned no __VIEWSTATE; the interface may have "
                "changed or the request was blocked."
            )
        return form

    def _post(self, form: dict[str, str], event_target: str, *, attempts: int | None = None) -> str:
        """POST the form with a postback target, retrying transient failures.

        Retrying matters more here than it does for a single GET: a crawl issues one POST
        per page, so a single transient 502 from the portal aborts the whole pagination
        sequence and silently truncates the result set. Each attempt re-parses nothing and
        reuses the same session, so the viewstate stays valid.
        """
        attempts = attempts or self.max_retries
        last_error: Exception | None = None
        for attempt in range(attempts):
            payload = dict(form)
            payload["__EVENTTARGET"] = event_target
            payload["__EVENTARGUMENT"] = ""
            self.limiter.wait()
            try:
                response = self.session.post(
                    self._home_url(), data=payload, timeout=self.timeout,
                    allow_redirects=True,
                )
                if response.status_code in (429, 500, 502, 503, 504):
                    raise ConnectorError(
                        f"Dallas Accela returned transient HTTP {response.status_code}"
                    )
                response.raise_for_status()
                if "Error.aspx" in response.url:
                    raise ConnectorError(
                        f"Dallas Accela rejected the request ({event_target}) with an error page."
                    )
                return response.text
            except Exception as exc:  # noqa: BLE001 - retried, then re-raised
                last_error = exc
                backoff = 2 ** attempt
                log.warning(
                    "Dallas Accela: attempt %d/%d for %s failed (%s); retrying in %ds",
                    attempt + 1, attempts, event_target, exc, backoff,
                )
                time.sleep(backoff)
        raise ConnectorError(
            f"Dallas Accela request {event_target} failed after {attempts} attempts: "
            f"{last_error}"
        )

    # --- fetch -----------------------------------------------------------------

    def fetch_raw(
        self,
        since: date | None = None,
        until: date | None = None,
        permit_type_keys: list[str] | None = None,
        max_pages_per_type: int | None = None,
    ) -> Iterator[RawPermit]:
        """Yield Dallas permit records.

        One search is issued per commercial permit type, because the interface filters by a
        single record type at a time. Pagination continues until a page yields no new
        records, which is necessary because the result counter saturates at "100+".
        """
        start = since.strftime("%m/%d/%Y") if since else self.DEFAULT_START
        end = (until or date.today()).strftime("%m/%d/%Y")

        keys = permit_type_keys or list(COMMERCIAL_PERMIT_TYPES)
        page_cap = max_pages_per_type or self.max_pages

        for key in keys:
            permit_type = COMMERCIAL_PERMIT_TYPES.get(key)
            if not permit_type:
                log.warning("Unknown Dallas permit type key %r; skipping", key)
                continue
            log.info("Dallas: searching %s (%s..%s)", key, start, end)
            yield from self._fetch_type(permit_type, key, start, end, page_cap)

    def _fetch_type(
        self, permit_type: str, key: str, start: str, end: str, page_cap: int
    ) -> Iterator[RawPermit]:
        """Yield every record of one permit type, following the pager to exhaustion.

        Termination conditions, in order:
          * the response has no ``Next >`` anchor, which is the natural end of the result set;
          * a page yields no records that have not already been seen, which guards against a
            pager that repeats rather than advances;
          * ``page_cap`` pages have been fetched, an explicit operator safety bound.
        """
        form = self._fresh_form()
        form["ctl00$PlaceHolderMain$generalSearchForm$txtGSStartDate"] = start
        form["ctl00$PlaceHolderMain$generalSearchForm$txtGSEndDate"] = end
        form["ctl00$PlaceHolderMain$generalSearchForm$ddlGSPermitType"] = permit_type

        html: str | None = self._post(form, "ctl00$PlaceHolderMain$btnNewSearch")
        seen: set[str] = set()
        pages_fetched = 0
        self.pages_by_type[key] = 0

        while html is not None and pages_fetched < page_cap:
            rows = parse_results(html)
            pages_fetched += 1
            self.pages_by_type[key] = pages_fetched

            new_rows = [
                r for r in rows if r["record_number"] and r["record_number"] not in seen
            ]
            for row in new_rows:
                seen.add(row["record_number"])
                payload = dict(row)
                payload["_permit_type_key"] = key
                payload["_permit_type_value"] = permit_type
                yield RawPermit(
                    source_id=self.source_id,
                    natural_key=self.natural_key(row),
                    payload=payload,
                    source_url=self._record_url(row),
                )

            if not new_rows:
                log.info(
                    "Dallas: %s stopped at page %d with no new records", key, pages_fetched
                )
                break

            html = self._paged(html)

        if pages_fetched >= page_cap:
            log.warning("Dallas: %s hit the page cap (%d pages)", key, page_cap)

    def _paged(self, html: str) -> str | None:
        """Advance to the next page, or return None when there is no next page.

        The pager is a *windowed* control, verified against the live portal on 2026-09-21:

            ctl13$ctl03 .. ctl13$ctl11  -> pages 2 .. 10
            ctl13$ctl12                 -> the '...' ellipsis, which jumps ten pages on
            ctl13$ctl14                 -> 'Next >'

        Two consequences shaped this implementation:

        1. Pager indices are *not* a stable arithmetic function of the page number. Past
           page 10 the window shifts, so computing ``ctl{page+1}`` silently stops landing on
           the intended page. The connector therefore follows the ``Next >`` control, whose
           presence is also the natural termination signal.
        2. The document contains more than one pagination region (a hidden dialog reuses the
           same CSS classes), so the *last* matching anchor is taken, which is the live
           result pager.
        """
        targets = NEXT_ANCHOR_RE.findall(html)
        if not targets:
            return None
        return self._post(build_form(html), targets[-1])

    def _record_url(self, row: dict[str, Any]) -> str | None:
        """Build a stable public URL for a record.

        The portal addresses records by a three-part ``capID`` rather than by record number,
        and that id is only exposed on the detail links. Where the template is unavailable
        the record is still traceable by number through the search interface.
        """
        if self.config.source_url_template:
            return self.config.source_url_template.format(
                record_number=row.get("record_number", "")
            )
        return None

    def natural_key(self, row: dict[str, Any]) -> str:
        return str(row.get("record_number"))

    # --- normalize -------------------------------------------------------------

    def normalize(self, raw: RawPermit) -> Permit | None:
        row = raw.payload
        record_type = clean_text(row.get("record_type"))
        if not record_type:
            return None

        parts = _split_address(clean_text(row.get("address")))
        record_date = parse_date(row.get("record_date"))

        # The portal exposes no declared job value or floor area, so these stay None rather
        # than being inferred from the description text.
        return Permit(
            source_id=self.source_id,
            permit_number=str(row.get("record_number")),
            natural_key=raw.natural_key,
            permit_type=record_type,
            permit_subtype=clean_text(row.get("_permit_type_key")),
            permit_date=record_date,
            status=clean_text(row.get("status")),
            address=parts["street"],
            city=parts["city"],
            state=self.config.jurisdiction_state,
            zip_code=parts["zip"],
            work_description=clean_text(row.get("description")) or clean_text(row.get("short_notes")),
            land_use=None,
            specific_use=None,
            job_value=parse_money(None),
            square_footage=parse_sqft(None),
            owner=None,
            contractor=None,
            is_commercial=is_commercial_type(record_type),
            source_url=raw.source_url,
            source_date=record_date,
            raw=row,
        )