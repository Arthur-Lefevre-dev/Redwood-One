"""Series grouping by canonical id and normalized show title."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.series_grouping import (
    equivalent_series_keys,
    normalize_show_name,
    series_catalog_group_key,
)
from db.models import Base, ContentKind, Film, FilmSource, FilmStatut


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_normalize_show_name_prefers_series_title():
    assert normalize_show_name("Doctor Who (2005)", None) == "doctor who"
    assert normalize_show_name("  My Show  ", "ignored") == "my show"


def test_normalize_show_name_strips_episode_from_titre():
    assert normalize_show_name(None, "Doctor Who S07E05 The Angels") == "doctor who"


def test_series_catalog_group_key_uses_display_title_only_when_set():
    a = series_catalog_group_key("Doctor Who (2005)", "Completely different episode title")
    b = series_catalog_group_key("doctor  who", "Other ep")
    assert a == b and a == "doctor who"


def test_equivalent_series_keys_merges_by_normalized_title():
    db = _session()
    db.add(
        Film(
            id=1,
            titre="S1E1",
            statut=FilmStatut.disponible,
            content_kind=ContentKind.series_episode,
            series_key="tv-111",
            series_title="Doctor Who",
            season_number=1,
            episode_number=1,
            source=FilmSource.upload,
        )
    )
    db.add(
        Film(
            id=2,
            titre="S1E2",
            statut=FilmStatut.disponible,
            content_kind=ContentKind.series_episode,
            series_key="tv-222",
            series_title="Doctor Who",
            season_number=1,
            episode_number=2,
            source=FilmSource.upload,
        )
    )
    db.commit()
    keys = equivalent_series_keys(db, "tv-111")
    assert set(keys) == {"tv-111", "tv-222"}
