"""Domain models.

These dataclasses are the contract between connectors, the pipeline, and persistence.
Connectors emit RawPermit objects. The pipeline normalizes them into Permit objects,
assembles them into Project objects, and attaches Evidence to every asserted field.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RawPermit:
    """A permit row as emitted by a connector, before normalization.

    `payload` holds the verbatim source record. Keeping it means a normalization bug can
    be fixed and replayed without re-fetching from a government server.
    """

    source_id: str
    natural_key: str
    payload: dict[str, Any]
    source_url: str | None = None
    fetched_at: datetime = field(default_factory=utcnow)

    @property
    def payload_hash(self) -> str:
        canonical = repr(sorted(self.payload.items())) if self.payload else ""
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class Permit:
    """A normalized permit record.

    Any field that the source did not supply stays None. Nothing is inferred.
    """

    source_id: str
    permit_number: str
    natural_key: str

    permit_type: str | None = None
    permit_subtype: str | None = None
    permit_date: date | None = None
    status: str | None = None

    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None

    work_description: str | None = None
    land_use: str | None = None
    specific_use: str | None = None

    job_value: float | None = None
    square_footage: float | None = None

    owner: str | None = None
    contractor: str | None = None

    is_commercial: bool | None = None
    source_url: str | None = None
    source_date: date | None = None

    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def combined_text(self) -> str:
        """All human-readable text on the permit, lowercased, for keyword matching.

        Used for evidence detection and classification only. Never used to populate a
        factual field.
        """
        parts = [
            self.permit_type,
            self.permit_subtype,
            self.work_description,
            self.land_use,
            self.specific_use,
        ]
        return " ".join(p for p in parts if p).lower()


@dataclass
class Evidence:
    """A single substantiated fact about a project.

    One row per (project, field). The presence of an Evidence row is what licenses the
    pipeline to populate the corresponding Project field.
    """

    field_name: str
    value: str | None
    source_id: str
    source_name: str
    source_url: str | None
    source_record_key: str | None = None
    source_date: date | None = None
    observed_at: datetime = field(default_factory=utcnow)
    evidence_type: str = "permit"
    tier: int | None = None
    excerpt: str | None = None


@dataclass
class ProjectParty:
    role: str
    name: str
    source_id: str
    source_url: str | None = None
    excerpt: str | None = None


@dataclass
class Project:
    """An assembled construction opportunity.

    The 20 required fields are populated only where evidence exists. Callers that need to
    display a value must use `display()` so the not-verified rule is applied consistently.
    """

    project_key: str
    trade: str = "commercial_hvac"

    project_name: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    project_type: str | None = None
    estimated_project_value: float | None = None
    square_footage: float | None = None
    permit_number: str | None = None
    permit_date: date | None = None
    project_status: str | None = None
    owner: str | None = None
    developer: str | None = None
    general_contractor: str | None = None
    architect: str | None = None
    mechanical_hvac_evidence: str | None = None

    source_name: str | None = None
    source_url: str | None = None
    source_date: date | None = None
    last_verified: datetime | None = None

    # Derived / analytical fields. Kept separate from the 20 sourced fields so it is
    # always clear which values came from a public record and which we computed.
    mechanical_evidence_tier: int | None = None
    property_class: str | None = None
    location_precision: str | None = None
    classification: str | None = None
    classification_score: int | None = None
    classification_reasons: list[str] = field(default_factory=list)

    #: Fields where two sources state different values. Both values are retained; nothing
    #: is merged or silently resolved. See discrepancy.py.
    discrepancies: list[dict[str, Any]] = field(default_factory=list)
    disputed_fields: list[str] = field(default_factory=list)

    permits: list[Permit] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    parties: list[ProjectParty] = field(default_factory=list)

    def display(self, field_name: str) -> str:
        """Render a field for human consumption, applying the not-verified rule."""
        value = getattr(self, field_name, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            from .constants import NOT_VERIFIED

            return NOT_VERIFIED
        if isinstance(value, float):
            return f"${value:,.0f}"
        if isinstance(value, date):
            return value.isoformat()
        return str(value)


def normalize_address(address: str | None) -> str | None:
    """Normalize an address for clustering into projects.

    Case, punctuation, whitespace, and suite designators are removed so that variants of
    the same building collapse together. Returns None when there is nothing to match on,
    which prevents unrelated blank-address permits from being clustered.
    """
    if not address:
        return None
    text = address.lower().strip()
    # Drop suite/unit designators: "Ste:1710", "suite 400", "#C403", "unit 5".
    text = re.sub(r"\b(ste|suite|unit|apt|bldg|building|floor|fl|no)\b\s*[:#.]?\s*\w+", " ", text)
    text = re.sub(r"#\s*\w+", " ", text)
    # Expand the most common street-type abbreviations so "ave" and "avenue" agree.
    for short, long in (
        ("expy", "expressway"),
        ("blvd", "boulevard"),
        ("ave", "avenue"),
        ("st", "street"),
        ("dr", "drive"),
        ("rd", "road"),
        ("ln", "lane"),
        ("ct", "court"),
        ("pkwy", "parkway"),
        ("hwy", "highway"),
        ("n", "north"),
        ("s", "south"),
        ("e", "east"),
        ("w", "west"),
    ):
        text = re.sub(rf"\b{short}\b\.?", long, text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def parse_date(value: Any) -> date | None:
    """Parse the date formats these public sources actually emit.

    Returns None for anything unrecognized rather than guessing. Silently coercing a
    malformed date would produce a fact the source never stated.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    # ArcGIS epoch milliseconds.
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None

    # ISO 8601, including Socrata's ".000" fractional-second suffix.
    iso = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso).date()
    except ValueError:
        pass

    for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y", "%m-%d-%y", "%d-%b-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def parse_money(value: Any) -> float | None:
    """Parse a money/area value, returning None for blanks and unparseable text.

    These sources use the literal string "NULL" to mean absent, and pipe-delimited
    multiple values (e.g. "100|200"). Both are treated as no value rather than summed or
    guessed at.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("$", "")
    if not text or text.upper() in {"NULL", "N/A", "NA", "NONE", "-"}:
        return None
    if "|" in text:
        # Multiple stacked permits reported in one cell: not a single project value.
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_sqft(value: Any) -> float | None:
    """Parse square footage. Some sources store it as text with a trailing unit."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) or None
    text = str(value).strip().replace(",", "")
    if not text or text.upper() in {"NULL", "N/A", "NA", "-"}:
        return None
    match = re.match(r"^(-?\d+(?:\.\d+)?)", text)
    if not match:
        return None
    try:
        number = float(match.group(1))
    except ValueError:
        return None
    return number or None


def clean_text(value: Any) -> str | None:
    """Trim a source text value, treating the literal 'NULL' as absent.

    HTML entities are decoded because several portals return text with them embedded. The
    Dallas Accela interface returns "Remove &amp; replace (5) RTU package units", and an
    undecoded ampersand would be presented to the customer as literal source text as well as
    breaking keyword matching. The verbatim payload is preserved separately in the raw
    landing zone, so decoding here does not lose the original.
    """
    if value is None:
        return None
    text = html.unescape(str(value)).strip()
    if not text or text.upper() in {"NULL", "N/A", "NA", "NONE"}:
        return None
    return text