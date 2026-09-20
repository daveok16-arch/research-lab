"""Connector registry.

Adding a city or a trade source means writing a connector class and registering it here.
Nothing else in the platform needs to change.
"""

from __future__ import annotations

from typing import Any

from ..config import load_sources
from .base import BaseConnector, ConnectorError
from .collin_cad_permits import CollinCadPermitsConnector
from .dallas_permits import DallasGisPermitsConnector, DallasPermitsSocrataConnector
from .fort_worth_permits import FortWorthPermitsConnector

CONNECTOR_CLASSES: list[type[BaseConnector]] = [
    FortWorthPermitsConnector,
    CollinCadPermitsConnector,
    DallasGisPermitsConnector,
    DallasPermitsSocrataConnector,
]

_BY_ID = {cls.source_id: cls for cls in CONNECTOR_CLASSES}


def connector_ids() -> list[str]:
    return sorted(_BY_ID)


def build_connector(source_id: str, defaults: dict[str, Any] | None = None) -> BaseConnector:
    """Instantiate a connector for the given source id."""
    if source_id not in _BY_ID:
        raise ConnectorError(
            f"Unknown source {source_id!r}. Known sources: {', '.join(connector_ids())}"
        )
    sources = load_sources()
    if source_id not in sources:
        raise ConnectorError(
            f"Source {source_id!r} has no entry in config/sources.yaml"
        )
    return _BY_ID[source_id](sources[source_id], defaults)


__all__ = [
    "BaseConnector",
    "CollinCadPermitsConnector",
    "DallasGisPermitsConnector",
    "DallasPermitsSocrataConnector",
    "FortWorthPermitsConnector",
    "build_connector",
    "connector_ids",
]