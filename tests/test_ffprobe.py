"""Tests for ffprobe helpers."""

from core.ffprobe import summarize


def test_summarize_extracts_video():
    data = {
        "format": {"size": "1000", "duration": "120.5", "bit_rate": "5000000"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
            {"codec_type": "audio", "codec_name": "aac"},
        ],
    }
    m = summarize(data)
    assert m["codec_video"] == "h264"
    assert m["codec_audio"] == "aac"
    assert m["resolution"] == "1920x1080"
    assert m["size_bytes"] == 1000
