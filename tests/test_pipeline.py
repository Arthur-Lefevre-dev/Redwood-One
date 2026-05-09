"""Tests for pipeline decisions."""

from core.pipeline import _ffmpeg_time_to_sec, decide_processing


def test_non_mp4_transcodes():
    t, tx = decide_processing("/tmp/x.mkv", {"size_bytes": 100})
    assert tx is True


def test_small_mp4_direct():
    t, tx = decide_processing("/tmp/x.mp4", {"size_bytes": 1024})
    assert tx is False


def test_large_mp4_direct():
    """MP4 uploads as-is regardless of size (no 3 GiB optimisation threshold)."""
    t, tx = decide_processing("/tmp/x.mp4", {"size_bytes": 20 * 1024 * 1024 * 1024})
    assert tx is False


def test_ffmpeg_stderr_time_parse():
    assert _ffmpeg_time_to_sec("frame=1 fps=0 q=28.0 size=       0kB time=00:01:30.50") == 90.5
    assert _ffmpeg_time_to_sec("no time here") < 0
