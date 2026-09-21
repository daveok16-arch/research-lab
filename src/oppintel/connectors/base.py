"""Connector base: HTTP with polite rate limiting, retries, and pagination.

Every connector inherits from BaseConnector. Adding a new city or source means subclassing
this and implementing `fetch_raw`, then registering the class in connectors/__init__.py.
The rest of the platform is unaware of source-specific detail.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import requests

from ..config import DATA_DIR, SourceConfig
from ..models import RawPermit, utcnow

log = logging.getLogger(__name__)


class ConnectorError(RuntimeError):
    pass


@dataclass
class RateLimiter:
    """Token-bucket-style limiter enforcing a minimum gap between requests."""

    min_interval: float = 1.0
    _last: float = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()


class BaseConnector:
    """Shared behaviour for all source connectors."""

    #: Set by subclasses to the id used in config/sources.yaml.
    source_id: str = ""

    def __init__(self, config: SourceConfig, defaults: dict[str, Any] | None = None):
        self.config = config
        self.defaults = defaults or {}
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.defaults.get(
                    "user_agent", "DFW-OppIntelligence-MVP/0.1 (public-records research)"
                ),
                "Accept": "application/json",
            }
        )
        self.limiter = RateLimiter(
            min_interval=float(self.defaults.get("min_seconds_between_requests", 1.0))
        )
        self.timeout = float(self.defaults.get("timeout_seconds", 60))
        self.max_retries = int(self.defaults.get("max_retries", 3))
        self.page_size = int(self.defaults.get("page_size", 1000))
        self.max_pages = int(self.defaults.get("max_pages", 200))

    # --- HTTP -----------------------------------------------------------------

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        """GET a URL and decode JSON, retrying transient failures with backoff."""
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self.limiter.wait()
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                if response.status_code in (429, 500, 502, 503, 504):
                    raise ConnectorError(
                        f"Transient HTTP {response.status_code} from {url}"
                    )
                response.raise_for_status()
                return response.json()
            except Exception as exc:  # noqa: BLE001 - retried and re-raised below
                last_error = exc
                backoff = 2 ** attempt
                log.warning(
                    "%s: attempt %d/%d failed (%s); retrying in %ds",
                    self.source_id, attempt + 1, self.max_retries, exc, backoff,
                )
                time.sleep(backoff)
        raise ConnectorError(f"Failed to fetch {url} after {self.max_retries} attempts: {last_error}")

    # --- landing --------------------------------------------------------------

    def landing_path(self) -> Path:
        directory = DATA_DIR / "raw"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = utcnow().strftime("%Y%m%dT%H%M%S")
        return directory / f"{self.source_id}_{stamp}.jsonl"

    def land_to_disk(self, records: Iterator[RawPermit], path: Path) -> int:
        """Append verbatim payloads to a JSONL file for replay and audit."""
        count = 0
        with path.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(
                    json.dumps(
                        {
                            "source_id": record.source_id,
                            "natural_key": record.natural_key,
                            "source_url": record.source_url,
                            "fetched_at": record.fetched_at.isoformat(),
                            "payload": record.payload,
                        },
                        default=str,
                    )
                    + "\n"
                )
                count += 1
        return count

    # --- interface ------------------------------------------------------------

    def fetch_raw(self, since: Any = None) -> Iterator[RawPermit]:
        """Yield RawPermit records from the source. Implemented by subclasses."""
        raise NotImplementedError

    def normalize(self, raw: RawPermit) -> Any:
        """Convert a RawPermit into a domain Permit. Implemented by subclasses."""
        raise NotImplementedError

    def natural_key(self, row: dict[str, Any]) -> str:
        raise NotImplementedError