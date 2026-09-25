"""Tests for building-level grouping.

The behaviour under test is deliberately conservative. A shared base street address groups
projects as *possible* siblings; it never merges them, and it never changes a classification
or an evidence record. The tests below fix both halves of that: suites of one building are
recognised as related, and genuinely distinct buildings are not.

The live database motivated these tests: 240 base streets hold more than one suite-level
project, and one Dallas address holds 21.
"""

from __future__ import annotations

from oppintel.grouping import (
    ProjectGroup,
    building_key,
    duplicate_statistics,
    group_projects,
    sibling_info_for,
)


# --- key derivation -----------------------------------------------------------

def test_suites_of_one_building_share_a_key():
    """Dallas suite records must collapse to the building they belong to."""
    keys = {
        building_key("8687 N CENTRAL EXPY, 1010", "Dallas"),
        building_key("8687 N CENTRAL EXPY, 1028", "Dallas"),
        building_key("8687 N CENTRAL EXPY, 1112", "Dallas"),
    }
    assert len(keys) == 1


def test_bare_suite_codes_share_a_key():
    keys = {
        building_key("7207 GASTON AVE, BU15", "Dallas"),
        building_key("7207 GASTON AVE, BU16", "Dallas"),
        building_key("7207 GASTON AVE, BU17", "Dallas"),
    }
    assert len(keys) == 1


def test_explicit_suite_designator_shares_a_key():
    keys = {
        building_key("100 MAIN ST STE 5", "Fort Worth"),
        building_key("100 MAIN ST SUITE 400", "Fort Worth"),
        building_key("100 MAIN ST", "Fort Worth"),
    }
    assert len(keys) == 1


def test_distinct_street_numbers_do_not_share_a_key():
    assert building_key("100 MAIN ST", "Fort Worth") != building_key("200 MAIN ST", "Fort Worth")


def test_same_street_in_different_cities_does_not_share_a_key():
    """A street name alone is not a building; the city is part of the identity."""
    assert building_key("100 MAIN ST", "Dallas") != building_key("100 MAIN ST", "Fort Worth")


def test_corridor_without_a_street_number_has_no_key():
    """A block range such as "BLUE RIDGE TRL" cannot identify a building."""
    assert building_key("BLUE RIDGE TRL", "Plano") is None


def test_blank_address_has_no_key():
    assert building_key(None, "Dallas") is None
    assert building_key("", "Dallas") is None
    assert building_key("   ", "Dallas") is None


def test_zip_embedded_in_city_segment_does_not_break_the_key():
    """Collin CAD records arrive as "2200 INDEPENDENCE PKWY , PLANO, TX 75075"."""
    assert building_key("2200 INDEPENDENCE PKWY , PLANO, TX 75075", "Plano")


# --- grouping -----------------------------------------------------------------

def _rows():
    return [
        {"id": 1, "address": "8687 N CENTRAL EXPY, 1010", "city": "Dallas"},
        {"id": 2, "address": "8687 N CENTRAL EXPY, 1028", "city": "Dallas"},
        {"id": 3, "address": "2626 MCKINNEY AVE", "city": "Dallas"},
    ]


def test_groups_collect_siblings_without_merging():
    groups = group_projects(_rows())
    multi = [g for g in groups.values() if g.is_multi]
    assert len(multi) == 1
    assert multi[0].size == 2
    # Every row is still an individual member; nothing was collapsed into one record.
    assert sum(g.size for g in groups.values()) == 3


def test_sibling_info_reports_the_relationship():
    groups = group_projects(_rows())
    info = sibling_info_for(1, groups)
    assert info.is_grouped
    assert info.sibling_count == 1
    assert info.sibling_ids == [2]


def test_sibling_info_is_empty_for_a_solo_project():
    groups = group_projects(_rows())
    info = sibling_info_for(3, groups)
    assert not info.is_grouped
    assert info.sibling_count == 0


def test_sibling_description_marks_the_relationship_as_uncertain():
    """Grouping is a hint, and the wording must not overstate it."""
    groups = group_projects(_rows())
    text = sibling_info_for(1, groups).describe()
    assert "uncertain" in text.lower()
    assert "separate" in text.lower()


def test_grouping_ignores_projects_without_a_key():
    rows = _rows() + [{"id": 9, "address": "BLUE RIDGE TRL", "city": "Plano"}]
    groups = group_projects(rows)
    assert 9 not in [m["id"] for g in groups.values() for m in g.members]


def test_duplicate_statistics_are_computed_from_the_groups():
    groups = group_projects(_rows())
    stats = duplicate_statistics(groups)
    assert stats["building_keys"] == 2
    assert stats["multi_project_keys"] == 1
    assert stats["projects_in_multi_keys"] == 2
    assert stats["largest_group"] == 2
    assert stats["potential_reduction"] == 1


def test_empty_input_yields_empty_statistics():
    stats = duplicate_statistics(group_projects([]))
    assert stats["building_keys"] == 0
    assert stats["largest_group"] == 0


# --- determinism --------------------------------------------------------------

def test_grouping_is_order_independent():
    rows = _rows()
    forward = {k: sorted(m["id"] for m in g.members) for k, g in group_projects(rows).items()}
    backward = {
        k: sorted(m["id"] for m in g.members) for k, g in group_projects(list(reversed(rows))).items()
    }
    assert forward == backward


# --- grouping must not alter evidence -----------------------------------------

def test_group_membership_does_not_imply_a_merge():
    """A ProjectGroup is a container, not a replacement record."""
    group = ProjectGroup(building_key="k", members=_rows()[:2])
    assert group.size == 2
    assert [m["id"] for m in group.members] == [1, 2]