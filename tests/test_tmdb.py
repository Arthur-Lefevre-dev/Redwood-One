"""Tests for TMDB client."""

from unittest.mock import MagicMock, patch

from core.tmdb import enrich_from_filename


@patch("core.tmdb.get_settings")
@patch("httpx.Client")
def test_enrich_skips_without_key(mock_client, mock_settings):
    mock_settings.return_value = MagicMock(TMDB_API_KEY="")
    out = enrich_from_filename("Some.Movie.2020.mkv")
    assert out["tmdb_id"] is None
    assert "Movie" in out["titre"] or "Some" in out["titre"]
