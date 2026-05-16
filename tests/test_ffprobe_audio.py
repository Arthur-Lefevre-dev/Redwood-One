"""French audio stream selection for transcode."""

from core.ffprobe import is_french_audio_stream, preferred_audio_stream_index_from_probe


def _probe_json(*streams):
    return {"streams": list(streams)}


def test_preferred_french_over_english():
    data = _probe_json(
        {"index": 0, "codec_type": "video"},
        {
            "index": 1,
            "codec_type": "audio",
            "tags": {"language": "eng", "title": "English"},
        },
        {
            "index": 2,
            "codec_type": "audio",
            "tags": {"language": "fre", "title": "French"},
        },
    )
    assert preferred_audio_stream_index_from_probe(data) == 2


def test_french_title_hint_vf():
    data = _probe_json(
        {"index": 0, "codec_type": "video"},
        {"index": 1, "codec_type": "audio", "tags": {"language": "eng"}},
        {
            "index": 3,
            "codec_type": "audio",
            "tags": {"language": "und", "title": "VF"},
        },
    )
    assert preferred_audio_stream_index_from_probe(data) == 3


def test_fallback_first_audio_when_no_french():
    data = _probe_json(
        {"index": 0, "codec_type": "video"},
        {"index": 5, "codec_type": "audio", "tags": {"language": "eng"}},
        {"index": 6, "codec_type": "audio", "tags": {"language": "spa"}},
    )
    assert preferred_audio_stream_index_from_probe(data) == 5


def test_is_french_audio_stream_lang_fr():
    assert is_french_audio_stream({"tags": {"language": "fr-FR"}})
