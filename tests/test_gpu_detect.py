"""Tests for GPU encoder detection."""

from core import gpu_detect


def test_get_encoder_returns_dict(monkeypatch):
    monkeypatch.setattr(gpu_detect, "_detect_nvidia", lambda: None)
    monkeypatch.setattr(gpu_detect, "_detect_amd", lambda: None)
    monkeypatch.setattr(gpu_detect, "_detect_intel", lambda: None)
    gpu_detect.refresh_encoder_cache()
    enc = gpu_detect.get_encoder()
    assert enc["vendor"] == "cpu"
    assert enc["h264"] == "libx264"
    assert enc["h265"] == "libx265"
