"""Host metrics for GET /api/admin/system/stats."""

import logging
import shutil
import subprocess
from typing import Any, Dict

logger = logging.getLogger(__name__)

try:
    import psutil  # type: ignore
except ImportError:
    psutil = None


def _nvidia_query() -> Dict[str, Any]:
    if not shutil.which("nvidia-smi"):
        return {}
    cmd = [
        "nvidia-smi",
        "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        out = subprocess.check_output(cmd, text=True, timeout=5).strip()
        line = out.splitlines()[0]
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 5:
            return {
                "gpu_name": parts[0],
                "gpu_util": float(parts[1]) if parts[1] else 0.0,
                "vram_used_mb": float(parts[2]) if parts[2] else 0.0,
                "vram_total_mb": float(parts[3]) if parts[3] else 0.0,
                "gpu_temp_c": float(parts[4]) if parts[4] else 0.0,
            }
    except Exception as e:
        logger.debug("nvidia-smi parse failed: %s", e)
    return {}


def collect_system_stats(encoder_info: Dict[str, Any]) -> Dict[str, Any]:
    gpu = _nvidia_query()
    cpu_pct = float(psutil.cpu_percent(interval=0.1)) if psutil else 0.0
    ram = psutil.virtual_memory() if psutil else None
    ram_pct = float(ram.percent) if ram else 0.0
    ram_used_gb = float(ram.used) / (1024**3) if ram else 0.0
    ram_total_gb = float(ram.total) / (1024**3) if ram else 0.0

    vram_pct = 0.0
    if gpu.get("vram_total_mb"):
        vram_pct = 100.0 * gpu["vram_used_mb"] / max(gpu["vram_total_mb"], 1.0)

    label = encoder_info.get("label") or encoder_info.get("vendor")
    if gpu.get("gpu_name"):
        label = gpu["gpu_name"]

    return {
        "gpu_label": label,
        "gpu_vendor": encoder_info.get("vendor"),
        "encoder": encoder_info,
        "gpu_util_percent": gpu.get("gpu_util", 0.0),
        "vram_used_mb": gpu.get("vram_used_mb", 0.0),
        "vram_total_mb": gpu.get("vram_total_mb", 0.0),
        "vram_percent": round(vram_pct, 1),
        "gpu_temp_c": gpu.get("gpu_temp_c", 0.0),
        "cpu_percent": round(cpu_pct, 1),
        "ram_percent": round(ram_pct, 1),
        "ram_used_gb": round(ram_used_gb, 2),
        "ram_total_gb": round(ram_total_gb, 2),
    }
