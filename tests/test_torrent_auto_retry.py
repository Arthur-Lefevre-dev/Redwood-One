"""Tests for torrent auto-retry eligibility rules."""

import pytest

from core.torrent_auto_retry import torrent_error_eligible_for_auto_retry


@pytest.mark.parametrize(
    "message,expected",
    [
        ("", True),
        ("  ", True),
        ("tracker timeout", True),
        ("Annulé par l'administrateur.", False),
        ("annule par l'administrateur", False),
        ("aria2c not installed in worker image", False),
        ("invalid base64 torrent payload from queue", False),
        (".torrent payload too small or empty after decode", False),
        ("VAST_API_KEY manquante : impossible", False),
        ("Extension non prise en charge pour Vast après torrent : .xyz", False),
        ("missing torrent source", False),
        ("Torrent: aucune source (magnet ou fichier .torrent) enregistrée pour ce film.", False),
        ("Something enregistrée pour ce film", False),
    ],
)
def test_torrent_error_eligible_for_auto_retry(message, expected):
    assert torrent_error_eligible_for_auto_retry(message) is expected
