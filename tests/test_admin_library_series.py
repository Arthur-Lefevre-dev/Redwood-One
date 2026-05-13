"""Admin library series show label (aligned with frontend adminSeriesShowLabel)."""

from core.admin_library_series import (
    ADMIN_SERIES_UNTITLED_LABEL,
    extract_show_from_colon_titre,
    series_show_label_for_library_episode,
)


def test_extract_show_from_colon_titre():
    assert extract_show_from_colon_titre("Doctor Who: Asylum") == "Doctor Who"
    assert extract_show_from_colon_titre("No colon here") == ""
    assert extract_show_from_colon_titre("") == ""


def test_series_show_label_priority():
    assert (
        series_show_label_for_library_episode("My Show", "key-1", "ignored")
        == "My Show"
    )
    assert series_show_label_for_library_episode("", "key-2", "x") == "key-2"
    assert (
        series_show_label_for_library_episode("", "", "Lost: Pilot")
        == "Lost"
    )
    assert (
        series_show_label_for_library_episode("", "", "nope")
        == ADMIN_SERIES_UNTITLED_LABEL
    )
