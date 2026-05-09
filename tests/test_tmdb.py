"""Tests for TMDB client."""

from unittest.mock import MagicMock, patch

from core.tmdb import enrich_from_filename, parse_tv_season_episode
from db.models import ContentKind


@patch("core.tmdb.get_settings")
@patch("httpx.Client")
def test_enrich_skips_without_key(mock_client, mock_settings):
    mock_settings.return_value = MagicMock(TMDB_API_KEY="")
    out = enrich_from_filename("Some.Movie.2020.mkv")
    assert out["tmdb_id"] is None
    assert "Movie" in out["titre"] or "Some" in out["titre"]


@patch("core.tmdb.get_settings")
def test_enrich_series_skips_without_key(mock_settings):
    mock_settings.return_value = MagicMock(TMDB_API_KEY="")
    out = enrich_from_filename("Doctor.Who.S01E02.mkv", ContentKind.series_episode)
    assert out["tmdb_id"] is None
    assert out["season_number"] == 1
    assert out["episode_number"] == 2


def test_parse_tv_season_episode_patterns():
    assert parse_tv_season_episode("Doctor.Who.S01E02.mkv") == (1, 2)
    assert parse_tv_season_episode("Show.1x03.mp4") == (1, 3)
    assert parse_tv_season_episode("no.pattern.mp4") is None
