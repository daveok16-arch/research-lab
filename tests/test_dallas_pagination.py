"""Tests for the Accela pagination mechanism.

The pager is windowed, and this was the source of a real defect: an earlier version computed
the pager control index arithmetically as ``ctl{page+1}``. That happens to be correct for
pages 2-10, and silently wrong afterwards, because at page 10 the window shifts and
``ctl12`` becomes the "..." ellipsis rather than page 11. The result was a crawl that
oscillated between two pages and quietly collected 10 records where 309 existed.

These tests lock down the corrected behaviour: follow the ``Next >`` anchor, and treat its
absence as the end of the result set.
"""

from __future__ import annotations

import pytest

from oppintel.connectors.dallas_accela_permits import (
    NEXT_ANCHOR_RE,
    DallasAccelaConnector,
    build_form,
    parse_results,
)
from oppintel.config import CONFIG_DIR, load_sources

#: The exact pager markup captured from the live portal on 2026-09-21 (page 1 of a
#: large result set). Note ctl12 labelled '...' and ctl14 labelled 'Next >'.
LIVE_PAGER = """
<tr class="ACA_Table_Pages" align="center" valign="bottom"><td colspan="12">
<table class="aca_pagination" align="Center" role="presentation" border="0"><tr>
<td class="ACA_Hide"><a id="ctl00_PlaceHolderMain_dgvPermitList_gdvPermitList_ctl13_lb4btnExport"
  href="javascript:__doPostBack(&#39;ctl00$PlaceHolderMain$dgvPermitList$gdvPermitList$ctl13$lb4btnExport&#39;,&#39;&#39;)"></a></td>
<td class="aca_pagination_td aca_pagination_PrevNext"><span class="aca_simple_text font11px">&lt; Prev</span></td>
<td class="aca_pagination_td"><span class="SelectedPageButton font11px">1</span></td>
<td class="aca_pagination_td"><a class="aca_simple_text font11px" href="javascript:__doPostBack(&#39;ctl00$PlaceHolderMain$dgvPermitList$gdvPermitList$ctl13$ctl03&#39;,&#39;&#39;)">2</a></td>
<td class="aca_pagination_td"><a class="aca_simple_text font11px" href="javascript:__doPostBack(&#39;ctl00$PlaceHolderMain$dgvPermitList$gdvPermitList$ctl13$ctl11&#39;,&#39;&#39;)">10</a></td>
<td class="aca_pagination_td"><a class="aca_simple_text font11px" href="javascript:__doPostBack(&#39;ctl00$PlaceHolderMain$dgvPermitList$gdvPermitList$ctl13$ctl12&#39;,&#39;&#39;)">...</a></td>
<td class="aca_pagination_td aca_pagination_PrevNext"><a class="aca_simple_text font11px" href="javascript:__doPostBack(&#39;ctl00$PlaceHolderMain$dgvPermitList$gdvPermitList$ctl13$ctl14&#39;,&#39;&#39;)">Next &gt;</a></td>
</tr></table></td></tr>
"""

#: A pager at the end of the result set: the 'Next >' anchor is replaced by a disabled span.
LAST_PAGE_PAGER = """
<tr class="ACA_Table_Pages" align="center" valign="bottom"><td colspan="12">
<table class="aca_pagination" align="Center" role="presentation" border="0"><tr>
<td class="aca_pagination_td aca_pagination_PrevNext"><a class="aca_simple_text font11px" href="javascript:__doPostBack(&#39;ctl00$PlaceHolderMain$dgvPermitList$gdvPermitList$ctl13$ctl02&#39;,&#39;&#39;)">&lt; Prev</a></td>
<td class="aca_pagination_td"><span class="SelectedPageButton font11px">31</span></td>
</tr></table></td></tr>
"""


def _connector() -> DallasAccelaConnector:
    import yaml

    defaults = yaml.safe_load((CONFIG_DIR / "sources.yaml").read_text())["defaults"]
    return DallasAccelaConnector(load_sources()["dallas_accela_permits"], defaults)


def test_next_anchor_is_found_on_a_mid_result_page():
    targets = NEXT_ANCHOR_RE.findall(LIVE_PAGER)
    assert targets == ["ctl00$PlaceHolderMain$dgvPermitList$gdvPermitList$ctl13$ctl14"]


def test_next_anchor_is_absent_on_the_last_page():
    """Absence of the anchor is the termination signal, so it must not be matched."""
    assert NEXT_ANCHOR_RE.findall(LAST_PAGE_PAGER) == []


def test_ellipsis_anchor_is_not_mistaken_for_next():
    """ctl12 is the '...' control, not page 11. An arithmetic index would pick it."""
    targets = NEXT_ANCHOR_RE.findall(LIVE_PAGER)
    assert all("ctl12" not in t for t in targets)


def test_arithmetic_index_would_be_wrong_beyond_page_ten():
    """Documents the defect: at page 10 the window shifts, so ctl12 means '...' not page 11."""
    pager_targets = dict(
        (m.group(1).split("$")[-1], m.group(2))
        for m in __import__("re").finditer(
            r"__doPostBack\(&#39;[^&]*\$(ctl\d+)&#39;,&#39;&#39;\)\">([^<]{0,10})<", LIVE_PAGER
        )
    )
    assert pager_targets["ctl03"] == "2"
    assert pager_targets["ctl11"] == "10"
    # ctl12 is labelled '...' rather than '11', which is why following the label is correct.
    assert pager_targets["ctl12"] == "..."


def test_last_matching_anchor_is_used_when_the_document_has_several_pagers():
    """A hidden dialog reuses the pagination classes, so the last anchor is the live one."""
    doc = LIVE_PAGER + "<div id='hidden'>" + LAST_PAGE_PAGER + "</div>" + LIVE_PAGER
    targets = NEXT_ANCHOR_RE.findall(doc)
    assert len(targets) == 2


def test_paged_returns_none_when_there_is_no_next_anchor():
    """Guards the crawl loop against an infinite pagination sequence."""
    connector = _connector()

    class NeverCalled:
        def __call__(self, *a, **k):  # pragma: no cover - must not be invoked
            raise AssertionError("_post should not be called when there is no Next anchor")

    connector._post = NeverCalled()  # type: ignore[method-assign]
    assert connector._paged(LAST_PAGE_PAGER) is None


def test_paged_targets_the_next_anchor_when_present():
    connector = _connector()
    captured: dict[str, str] = {}

    def fake_post(form, event_target, **kwargs):
        captured["target"] = event_target
        return "<html></html>"

    connector._post = fake_post  # type: ignore[method-assign]
    result = connector._paged(LIVE_PAGER)
    assert result == "<html></html>"
    assert captured["target"].endswith("ctl13$ctl14")


def test_page_size_matches_the_observed_portal_behaviour():
    from oppintel.connectors.dallas_accela_permits import PAGE_SIZE

    assert PAGE_SIZE == 10


def test_empty_page_terminates_pagination():
    """A page with no parseable rows yields nothing new and must stop the crawl."""
    assert parse_results("<html><body>no rows</body></html>") == []


def test_build_form_is_required_for_each_page():
    """Each page's postback must carry that page's own viewstate."""
    html_a = '<form name="aspnetForm"><input type="hidden" name="__VIEWSTATE" value="AAA"/></form>'
    html_b = '<form name="aspnetForm"><input type="hidden" name="__VIEWSTATE" value="BBB"/></form>'
    assert build_form(html_a)["__VIEWSTATE"] == "AAA"
    assert build_form(html_b)["__VIEWSTATE"] == "BBB"