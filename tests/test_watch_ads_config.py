from api.routes.watch_ads_config import _safe_https_script_src, _safe_zone_id


def test_safe_https_script_src_accepts_coinzilla_like_url():
    u = "https://coinzillatag.com/lib/foo.js"
    assert _safe_https_script_src(u) == u


def test_safe_https_script_src_rejects_non_https():
    assert _safe_https_script_src("http://evil.com/x.js") is None
    assert _safe_https_script_src("javascript:alert(1)") is None


def test_safe_zone_id():
    assert _safe_zone_id("abc-123_Z") == "abc-123_Z"
    assert _safe_zone_id("bad id") is None
