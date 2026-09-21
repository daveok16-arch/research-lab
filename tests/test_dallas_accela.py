"""Tests for the Dallas Accela connector.

The HTTP layer is not exercised over the network. Instead these tests use the exact HTML
shapes observed from the live portal on 2026-09-21 — the form layout, the result-grid row
structure, and the pager control names — so the parsing, form-building, and pagination logic
are tested against real markup rather than invented fixtures.
"""

from __future__ import annotations

from datetime import date

import pytest

from oppintel.connectors.dallas_accela_permits import (
    COMMERCIAL_PERMIT_TYPES,
    DallasAccelaConnector,
    MECHANICAL_TYPE_LABEL,
    _split_address,
    build_form,
    is_commercial_type,
    parse_results,
    parse_total,
)
from oppintel.config import load_sources

# --- real markup shapes -------------------------------------------------------

SEARCH_FORM = """
<html><body><form name="aspnetForm" id="aspnetForm" method="post" action="./CapHome.aspx">
<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="TOKEN123" />
<input type="hidden" name="__VIEWSTATEGENERATOR" value="574AD939" />
<input type="hidden" name="__VIEWSTATEENCRYPTED" value="" />
<input type="hidden" name="ctl00$HDExpressionParam" value="" />
<input type="text" name="ctl00$PlaceHolderMain$generalSearchForm$txtGSStartDate" value="10/09/2015" />
<input type="text" name="ctl00$PlaceHolderMain$generalSearchForm$txtGSEndDate" value="09/21/2026" />
<select name="ctl00$PlaceHolderMain$generalSearchForm$ddlGSPermitType">
  <option value="">--Select--</option>
  <option selected="selected" value="Building/Commercial/New/NA">Commercial New Construction Permit</option>
  <option value="Building/Commercial/Mechanical/NA">Commercial Mechanical Permit</option>
</select>
<input type="submit" name="Submit" value="Submit" />
</form></body></html>
"""

RESULTS_GRID = """
<span>100+ Record results matching your search results</span>
<table id="ctl00_PlaceHolderMain_dgvPermitList_gdvPermitList">
<caption>Record list</caption>
<tr><td colspan="19"><span>Showing 1-10 of 100+</span>
<a id="ctl00_PlaceHolderMain_dgvPermitList_gdvPermitList_gdvPermitListtop4btnExport"
   href="javascript:__doPostBack('x','')">Download results</a></td></tr>
<tr class="ACA_TabRow_Header"><th>Date</th><th>Record Number</th><th>Record Type</th>
<th>Address</th><th>Description</th></tr>
<tr class="ACA_TabRow_Odd"><td></td><td>09/21/2026</td><td>COM-NEW-26-000306</td>
<td>Commercial New Construction Permit</td>
<td>9069 VANTAGE POINT DR, Dallas TX 75229</td><td>New office building</td></tr>
<tr class="ACA_TabRow_Even"><td></td><td>09/20/2026</td><td>COM-MEC-26-002405</td>
<td>Commercial Mechanical Permit</td>
<td>12050 E NORTHWEST HWY, Dallas TX 75231</td><td>install commercial RTU</td></tr>
<tr class="ACA_TabRow_Odd"><td></td><td>09/19/2026</td><td>26TMP-153554</td>
<td>Commercial Mechanical Permit</td>
<td></td><td></td></tr>
<tr class="ACA_Table_Pages"><td colspan="12">
<a href="javascript:__doPostBack('ctl00$PlaceHolderMain$dgvPermitList$gdvPermitList$ctl13$ctl03','')">2</a>
</td></tr>
</table>
"""


def _connector() -> DallasAccelaConnector:
    import yaml

    from oppintel.config import CONFIG_DIR

    defaults = yaml.safe_load((CONFIG_DIR / "sources.yaml").read_text())["defaults"]
    return DallasAccelaConnector(load_sources()["dallas_accela_permits"], defaults)


# --- form building ------------------------------------------------------------

def test_form_includes_viewstate_and_all_hidden_fields():
    form = build_form(SEARCH_FORM)
    assert form["__VIEWSTATE"] == "TOKEN123"
    assert form["__VIEWSTATEGENERATOR"] == "574AD939"
    assert "ctl00$HDExpressionParam" in form
    assert form["ctl00$PlaceHolderMain$generalSearchForm$txtGSStartDate"] == "10/09/2015"


def test_form_includes_the_selected_select_value():
    form = build_form(SEARCH_FORM)
    assert form["ctl00$PlaceHolderMain$generalSearchForm$ddlGSPermitType"] == \
        "Building/Commercial/New/NA"


def test_form_excludes_submit_buttons():
    """Browser form submission omits submit controls, and the server expects that."""
    form = build_form(SEARCH_FORM)
    assert "Submit" not in form


def test_form_ignores_unchecked_checkboxes():
    html = SEARCH_FORM.replace(
        "</form>",
        '<input type="checkbox" name="chkUnchecked" value="on" /></form>',
    )
    form = build_form(html)
    assert "chkUnchecked" not in form


# --- result parsing -----------------------------------------------------------

def test_parse_results_extracts_rows():
    rows = parse_results(RESULTS_GRID)
    assert len(rows) == 3
    assert rows[0]["record_number"] == "COM-NEW-26-000306"
    assert rows[0]["record_type"] == "Commercial New Construction Permit"
    assert rows[0]["address"] == "9069 VANTAGE POINT DR, Dallas TX 75229"


def test_parse_results_ignores_header_and_pager_rows():
    """Header and pager rows share the TabRow CSS class and must not become records."""
    rows = parse_results(RESULTS_GRID)
    numbers = {r["record_number"] for r in rows}
    assert "Date" not in numbers
    assert "2" not in numbers


def test_parse_results_handles_missing_address():
    rows = parse_results(RESULTS_GRID)
    blank = [r for r in rows if r["record_number"] == "26TMP-153554"]
    assert blank and blank[0]["address"] == ""


def test_parse_results_on_empty_page_returns_nothing():
    assert parse_results("<html><body>No records found</body></html>") == []


def test_parse_total_detects_saturation():
    """The portal reports a literal '100+' rather than a true total."""
    total, saturated = parse_total(RESULTS_GRID)
    assert total == 100
    assert saturated is True


def test_parse_total_exact_count_is_not_saturated():
    total, saturated = parse_total("<span>25 Record results matching</span>")
    assert total == 25
    assert saturated is False


# --- address handling ---------------------------------------------------------

def test_split_address_extracts_city_and_zip():
    parts = _split_address("11056 SHADY TRL, Dallas TX 75229")
    assert parts["street"] == "11056 SHADY TRL"
    assert parts["city"] == "Dallas"
    assert parts["zip"] == "75229"


def test_split_address_keeps_unit_designator_in_street():
    parts = _split_address("1700 CEDAR SPRINGS RD, 120, Dallas TX 75201")
    assert "1700 CEDAR SPRINGS RD" in parts["street"]
    assert parts["city"] == "Dallas"


def test_split_address_defaults_city_for_blank_input():
    parts = _split_address(None)
    assert parts["street"] is None
    assert parts["city"] == "Dallas"


# --- commercial classification ------------------------------------------------

def test_record_type_classification():
    assert is_commercial_type("Commercial Mechanical Permit") is True
    assert is_commercial_type("Residential Mechanical Permit") is False
    # Certificate of Occupancy carries no residential/commercial marker of its own.
    assert is_commercial_type("Certificate of Occupancy") is None
    assert is_commercial_type(None) is None


# --- connector contract -------------------------------------------------------

def test_permit_type_map_covers_the_mechanical_category():
    assert COMMERCIAL_PERMIT_TYPES["commercial_mechanical"] == \
        "Building/Commercial/Mechanical/NA"
    assert MECHANICAL_TYPE_LABEL == "Commercial Mechanical Permit"


def test_normalize_produces_a_permit_from_a_portal_row():
    from oppintel.models import RawPermit

    connector = _connector()
    raw = RawPermit(
        source_id="dallas_accela_permits",
        natural_key="COM-MEC-26-002405",
        payload={
            "record_date": "09/20/2026",
            "record_number": "COM-MEC-26-002405",
            "record_type": "Commercial Mechanical Permit",
            "address": "12050 E NORTHWEST HWY, Dallas TX 75231",
            "description": "install commercial RTU",
            "status": "Inspection Phase",
            "_permit_type_key": "commercial_mechanical",
        },
    )
    permit = connector.normalize(raw)
    assert permit is not None
    assert permit.permit_number == "COM-MEC-26-002405"
    assert permit.permit_type == "Commercial Mechanical Permit"
    assert permit.address == "12050 E NORTHWEST HWY"
    assert permit.city == "Dallas"
    assert permit.state == "TX"
    assert permit.zip_code == "75231"
    assert permit.permit_date == date(2026, 9, 20)
    assert permit.is_commercial is True


def test_normalize_leaves_value_and_area_unverified():
    """The portal publishes no declared value or floor area; they must stay absent."""
    from oppintel.models import RawPermit

    connector = _connector()
    raw = RawPermit(
        source_id="dallas_accela_permits",
        natural_key="X",
        payload={
            "record_date": "09/20/2026",
            "record_number": "X",
            "record_type": "Commercial Mechanical Permit",
            "address": "1 MAIN ST, Dallas TX 75201",
            "description": "install RTU",
            "status": "Issued",
        },
    )
    permit = connector.normalize(raw)
    assert permit.job_value is None
    assert permit.square_footage is None
    assert permit.owner is None
    assert permit.contractor is None


def test_normalize_returns_none_without_a_record_type():
    from oppintel.models import RawPermit

    connector = _connector()
    raw = RawPermit(
        source_id="dallas_accela_permits",
        natural_key="X",
        payload={"record_date": "09/20/2026", "record_number": "X", "record_type": ""},
    )
    assert connector.normalize(raw) is None


def test_connector_sets_the_required_origin_headers():
    """Without Origin and Referer the portal rejects otherwise-valid POSTs."""
    connector = _connector()
    assert connector.session.headers.get("Origin") == "https://aca-prod.accela.com"
    assert connector.session.headers.get("Referer")
    assert "Referer" in connector.session.headers


def test_natural_key_uses_the_record_number():
    connector = _connector()
    assert connector.natural_key({"record_number": "COM-MEC-26-002405"}) == \
        "COM-MEC-26-002405"


def test_errors_raise_rather_than_silently_returning_nothing(monkeypatch):
    """A rejected request must surface as an error, so a source never looks merely empty."""
    from oppintel.connectors.base import ConnectorError

    connector = _connector()

    class FakeResponse:
        url = "https://aca-prod.accela.com/DALLASTX/Error.aspx?ErrorId=abc"
        status_code = 200
        text = "error page"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(connector.session, "post", lambda *a, **k: FakeResponse())
    with pytest.raises(ConnectorError):
        connector._post({"__VIEWSTATE": "x"}, "ctl00$PlaceHolderMain$btnNewSearch")


def test_missing_viewstate_raises(monkeypatch):
    from oppintel.connectors.base import ConnectorError

    connector = _connector()

    class FakeResponse:
        status_code = 200
        text = "<html><body>no form here</body></html>"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(connector.session, "get", lambda *a, **k: FakeResponse())
    with pytest.raises(ConnectorError):
        connector._fresh_form()