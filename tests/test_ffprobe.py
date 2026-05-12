"""Tests for ffprobe helpers."""

from core.ffprobe import (
    probe_has_audio_stream,
    summarize,
    text_subtitle_stream_indices_from_probe,
)


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


def test_text_subtitle_indices_filters_bitmap():
    data = {
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "index": 0},
            {"codec_type": "audio", "codec_name": "aac", "index": 1},
            {"codec_type": "subtitle", "codec_name": "subrip", "index": 2},
            {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle", "index": 3},
        ],
    }
    assert text_subtitle_stream_indices_from_probe(data) == [2]


def test_probe_has_audio_stream():
    with_audio = {"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}
    without = {"streams": [{"codec_type": "video"}]}
    assert probe_has_audio_stream(with_audio) is True
    assert probe_has_audio_stream(without) is False
