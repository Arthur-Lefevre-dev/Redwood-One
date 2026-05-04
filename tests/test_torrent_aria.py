"""Tests for aria2 status mapping."""

from core.torrent_aria import status_to_stats


def test_status_to_stats_maps_speeds():
    st = {
        "numSeeders": "12",
        "numLeechers": "3",
        "connections": "8",
        "downloadSpeed": "1048576",
        "uploadSpeed": "65536",
        "completedLength": "100",
        "totalLength": "1000",
        "status": "active",
    }
    out = status_to_stats(st)
    assert out["seeders"] == 12
    assert out["leechers"] == 3
    assert out["connections"] == 8
    assert out["download_bps"] == 1048576
    assert out["upload_bps"] == 65536
    assert out["completed_bytes"] == 100
    assert out["total_bytes"] == 1000
