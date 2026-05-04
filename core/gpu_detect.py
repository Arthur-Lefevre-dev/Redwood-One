"""Detect local GPU and return optimal ffmpeg encoder settings (cached)."""

import glob
import logging
import platform
import re
import shutil
import subprocess
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_encoder_cache: Optional[Dict[str, Any]] = None


def _run(cmd: list[str], timeout: int = 5) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        logger.debug("subprocess failed: %s", e)
        return -1, "", str(e)


def _detect_nvidia() -> Optional[Dict[str, Any]]:
    if not shutil.which("nvidia-smi"):
        return None
    code, out, err = _run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"])
    if code != 0:
        return None
    name = (out.strip().splitlines()[0] if out.strip() else "NVIDIA GPU").strip()
    logger.info("gpu_detect: NVIDIA found — %s", name)
    return {
        "vendor": "nvidia",
        "h264": "h264_nvenc",
        "h265": "hevc_nvenc",
        "hwaccel": "-hwaccel",
        "hwaccel_device": "cuda",
        "label": name,
    }


def _has_intel_qsv() -> bool:
    renders = glob.glob("/dev/dri/renderD*")
    if not renders:
        return False
    if shutil.which("vainfo"):
        code, out, _ = _run(["vainfo"], timeout=8)
        if code == 0 and out and re.search(r"H264", out, re.I):
            return True
    return bool(renders)


def _detect_intel() -> Optional[Dict[str, Any]]:
    if not _has_intel_qsv():
        return None
    logger.info("gpu_detect: Intel Quick Sync assumed from /dev/dri + vainfo")
    return {
        "vendor": "intel",
        "h264": "h264_qsv",
        "h265": "hevc_qsv",
        "hwaccel": "-hwaccel",
        "hwaccel_device": "qsv",
        "label": "Intel Quick Sync",
    }


def _ffmpeg_encoders_blob() -> str:
    code, out, err = _run(["ffmpeg", "-hide_banner", "-encoders"], timeout=25)
    return ((out or "") + (err or ""))


def _vainfo_blob() -> str:
    if not shutil.which("vainfo"):
        return ""
    code, out, err = _run(["vainfo"], timeout=12)
    return ((out or "") + (err or ""))


def _sysfs_amd_drm_vendor() -> bool:
    """True if a DRM card under sysfs reports AMD PCI vendor 0x1002 (works when /dev/dri is passed through)."""
    for vendor_path in glob.glob("/sys/class/drm/card[0-9]/device/vendor"):
        try:
            with open(vendor_path, encoding="ascii", errors="ignore") as fh:
                txt = fh.read().strip().lower()
            if txt == "0x1002":
                return True
        except OSError:
            continue
    return False


def _amd_vaapi_dict(render: str) -> Dict[str, Any]:
    return {
        "vendor": "amd",
        "h264": "h264_vaapi",
        "h265": "hevc_vaapi",
        "hwaccel": "vaapi",
        "hwaccel_device": render,
        "label": "AMD VAAPI",
    }


def _try_amd_vaapi_from_dri() -> Optional[Dict[str, Any]]:
    renders = sorted(glob.glob("/dev/dri/renderD*"))
    if not renders:
        return None
    enc_blob = _ffmpeg_encoders_blob()
    if "h264_vaapi" not in enc_blob or "hevc_vaapi" not in enc_blob:
        return None
    return _amd_vaapi_dict(renders[0])


def _detect_amd() -> Optional[Dict[str, Any]]:
    """
    AMD on Linux: need /dev/dri (expose GPU into Docker) plus either
    AMF encoders in ffmpeg, or VAAPI + AMDGPU/Mesa in vainfo / lspci.
    """
    if platform.system() != "Linux":
        return None
    renders = sorted(glob.glob("/dev/dri/renderD*"))
    if not renders:
        logger.info(
            "gpu_detect: no /dev/dri/renderD* — AMD GPU not visible in this environment "
            "(Docker: mount host /dev/dri on worker; WSL: GPU in Linux containers is limited)."
        )
        return None

    render = renders[0]
    enc_blob = _ffmpeg_encoders_blob()
    va_blob = _vainfo_blob()

    pci_amd = False
    if shutil.which("lspci"):
        code, out, _ = _run(["lspci"], timeout=5)
        pci_amd = code == 0 and bool(
            re.search(r"\bAMD\b|\bATI\b|Radeon|RX\s*\d", out or "", re.I)
        )

    vainfo_amd = bool(
        re.search(
            r"amdgpu|radeonsi|AMD Radeon|Radeon RX|Mesa.*RADV|\bAMD\b.*Graphics",
            va_blob,
            re.I,
        )
    )

    sysfs_amd = _sysfs_amd_drm_vendor()

    if not pci_amd and not vainfo_amd and not sysfs_amd:
        logger.info(
            "gpu_detect: /dev/dri present but no AMD signal (lspci/vainfo/sysfs) — "
            "mount host /dev/dri on the worker, install mesa-va-drivers in the image, "
            "or set REDWOOD_GPU_VENDOR=amd if the GPU is passed through."
        )
        return None

    has_amf = "h264_amf" in enc_blob and "hevc_amf" in enc_blob
    has_vaapi = "h264_vaapi" in enc_blob and "hevc_vaapi" in enc_blob

    if has_amf:
        logger.info("gpu_detect: AMD AMF (ffmpeg encoders + DRI)")
        return {
            "vendor": "amd",
            "h264": "h264_amf",
            "h265": "hevc_amf",
            "hwaccel": None,
            "hwaccel_device": None,
            "label": "AMD AMF",
        }
    if has_vaapi:
        logger.info("gpu_detect: AMD VAAPI via %s (ffmpeg vaapi encoders)", render)
        return {
            "vendor": "amd",
            "h264": "h264_vaapi",
            "h265": "hevc_vaapi",
            "hwaccel": "vaapi",
            "hwaccel_device": render,
            "label": "AMD VAAPI",
        }

    logger.warning(
        "gpu_detect: AMD GPU likely present (lspci/vainfo) but this ffmpeg build has "
        "no h264_amf/hevc_amf nor h264_vaapi/hevc_vaapi — install a full ffmpeg or use a worker image with VAAPI/AMF."
    )
    return None


def _cpu_fallback() -> Dict[str, Any]:
    logger.warning(
        "gpu_detect: no GPU encoder detected — using CPU libx264/libx265 (slow)"
    )
    return {
        "vendor": "cpu",
        "h264": "libx264",
        "h265": "libx265",
        "hwaccel": None,
        "hwaccel_device": None,
        "label": "CPU",
    }


def _forced_vendor_encoder() -> Optional[Dict[str, Any]]:
    """Optional override from REDWOOD_GPU_VENDOR (see docker/.env)."""
    try:
        from config import get_settings

        v = (get_settings().REDWOOD_GPU_VENDOR or "").strip().lower()
    except Exception:
        v = ""
    if not v or v == "auto":
        return None
    if v == "cpu":
        return _cpu_fallback()
    if v == "nvidia":
        return _detect_nvidia() or _cpu_fallback()
    if v == "intel":
        return _detect_intel() or _cpu_fallback()
    if v == "amd":
        forced = _try_amd_vaapi_from_dri()
        if forced:
            logger.info("gpu_detect: REDWOOD_GPU_VENDOR=amd — using VAAPI on %s", forced["hwaccel_device"])
            return forced
        amf = _detect_amd()
        if amf:
            return amf
        logger.warning(
            "REDWOOD_GPU_VENDOR=amd but VAAPI/AMF not usable — need /dev/dri/renderD* and ffmpeg vaapi/amf encoders"
        )
        return _cpu_fallback()
    return None


def detect_encoder() -> Dict[str, Any]:
    """Return encoder dict; priority NVIDIA → AMD → Intel → CPU."""
    forced = _forced_vendor_encoder()
    if forced is not None:
        return forced

    nvidia = _detect_nvidia()
    if nvidia:
        return nvidia
    amd = _detect_amd()
    if amd:
        return amd
    intel = _detect_intel()
    if intel:
        return intel
    return _cpu_fallback()


def get_encoder() -> Dict[str, Any]:
    """Singleton cached encoder info (API startup + worker processes)."""
    global _encoder_cache
    if _encoder_cache is None:
        _encoder_cache = detect_encoder()
    return _encoder_cache


def refresh_encoder_cache() -> Dict[str, Any]:
    """Force re-detection (e.g. lifespan)."""
    global _encoder_cache
    _encoder_cache = detect_encoder()
    return _encoder_cache


def encoder_dict_for_api() -> Dict[str, Any]:
    """Subset safe to expose in GET /api/admin/system/stats."""
    enc = get_encoder()
    hw = enc.get("hwaccel")
    dev = enc.get("hwaccel_device")
    if hw == "vaapi" and dev:
        hwaccel_str = f"-hwaccel vaapi -hwaccel_device {dev}"
    elif hw == "-hwaccel" and dev:
        hwaccel_str = f"{hw} {dev}".strip()
    elif hw:
        hwaccel_str = str(hw)
    else:
        hwaccel_str = None
    return {
        "vendor": enc["vendor"],
        "h264": enc["h264"],
        "h265": enc["h265"],
        "hwaccel": hwaccel_str,
        "label": enc.get("label"),
    }
