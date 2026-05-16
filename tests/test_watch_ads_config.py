from api.routes.watch_ads_config import _safe_aads_unit_id


def test_safe_aads_unit_id_accepts_numeric():
    assert _safe_aads_unit_id("2437671") == "2437671"


def test_safe_aads_unit_id_rejects_non_numeric():
    assert _safe_aads_unit_id("24x37") is None
    assert _safe_aads_unit_id("") is None
    assert _safe_aads_unit_id("a" * 25) is None
