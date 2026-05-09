"""Series next/prev episode resolution."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.series_playback import next_episode_id, prev_episode_id
from db.models import Base, ContentKind, Film, FilmSource, FilmStatut


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_next_and_prev_within_and_across_seasons():
    db = _session()
    rows = [
        (1, 1, 1),
        (2, 1, 2),
        (3, 1, 3),
        (4, 2, 1),
        (5, 2, 2),
    ]
    for fid, season, ep in rows:
        db.add(
            Film(
                id=fid,
                titre=f"S{season}E{ep}",
                statut=FilmStatut.disponible,
                content_kind=ContentKind.series_episode,
                series_key="demo-show",
                season_number=season,
                episode_number=ep,
                source=FilmSource.upload,
            )
        )
    db.commit()
    mid = db.get(Film, 2)
    assert next_episode_id(db, mid) == 3
    assert prev_episode_id(db, mid) == 1
    last_s1 = db.get(Film, 3)
    assert next_episode_id(db, last_s1) == 4
    first_s2 = db.get(Film, 4)
    assert prev_episode_id(db, first_s2) == 3


def test_film_has_no_next():
    db = _session()
    db.add(
        Film(
            id=1,
            titre="Movie",
            statut=FilmStatut.disponible,
            content_kind=ContentKind.film,
            source=FilmSource.upload,
        )
    )
    db.commit()
    f = db.get(Film, 1)
    assert next_episode_id(db, f) is None
    assert prev_episode_id(db, f) is None
