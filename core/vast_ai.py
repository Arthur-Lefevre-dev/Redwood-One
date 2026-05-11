"""Vast.ai REST client (GPU marketplace) — search offers, create / destroy instances. Comments in English."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from config import get_settings

logger = logging.getLogger(__name__)

_DEFAULT_API_ROOT = "https://console.vast.ai/api/v0"


def _api_root() -> str:
    s = get_settings()
    root = (getattr(s, "VAST_API_BASE_URL", None) or _DEFAULT_API_ROOT).strip().rstrip("/")
    return root


def _bearer_headers(api_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def require_vast_api_key() -> str:
    key = (get_settings().VAST_API_KEY or "").strip()
    if not key:
        raise RuntimeError("VAST_API_KEY is not set")
    return key


def search_offers(
    gpu_names: List[str],
    *,
    limit: int = 8,
    instance_type: str = "ondemand",
    min_reliability: float = 0.95,
    verified: bool = True,
    num_gpus_min: int = 1,
    num_gpus_eq: Optional[int] = None,
    max_dph_per_hour: Optional[float] = None,
    max_bandwidth_usd_per_tb: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    POST /bundles/ — returns a list of offer dicts (subset of fields kept as-is).

    When max_bandwidth_usd_per_tb is set, applies inet_down_cost / inet_up_cost
    lte in $/GB (API fields), i.e. max_usd_per_tb / 1024 per GB.

    When num_gpus_eq is set, the bundles filter uses num_gpus eq that value (overrides num_gpus_min).
    """
    api_key = require_vast_api_key()
    s = get_settings()
    names = [n.strip() for n in gpu_names if n and str(n).strip()]
    if not names:
        raise ValueError("gpu_names must be non-empty")

    dph_cap = float(max_dph_per_hour) if max_dph_per_hour is not None else float(s.VAST_MAX_DPH_PER_HOUR)
    bw_tb_cap = (
        float(max_bandwidth_usd_per_tb)
        if max_bandwidth_usd_per_tb is not None
        else float(s.VAST_MAX_BANDWIDTH_USD_PER_TB)
    )

    if num_gpus_eq is not None:
        num_gpus_filter: Dict[str, Any] = {"eq": int(num_gpus_eq)}
    else:
        num_gpus_filter = {"gte": int(num_gpus_min)}

    payload: Dict[str, Any] = {
        "gpu_name": {"in": names},
        "num_gpus": num_gpus_filter,
        "reliability": {"gte": min_reliability},
        "verified": {"eq": verified},
        "rentable": {"eq": True},
        "type": instance_type,
        "limit": limit,
        "dph_total": {"lte": dph_cap},
    }
    if bw_tb_cap > 0:
        per_gb = bw_tb_cap / 1024.0
        payload["inet_down_cost"] = {"lte": per_gb}
        payload["inet_up_cost"] = {"lte": per_gb}
    url = f"{_api_root()}/bundles/"
    with httpx.Client(timeout=60.0) as client:
        r = client.post(url, headers=_bearer_headers(api_key), json=payload)
        r.raise_for_status()
        data = r.json()

    offers = data.get("offers")
    if not isinstance(offers, list):
        logger.warning("vast_ai: unexpected bundles response keys=%s", list(data.keys()) if isinstance(data, dict) else type(data))
        return []

    out: List[Dict[str, Any]] = []
    for o in offers:
        if not isinstance(o, dict):
            continue
        oid = o.get("id")
        if oid is None:
            continue
        out.append(
            {
                "id": oid,
                "gpu_name": o.get("gpu_name"),
                "num_gpus": o.get("num_gpus"),
                "dph_total": o.get("dph_total"),
                "reliability": o.get("reliability"),
                "inet_down": o.get("inet_down"),
                "inet_up": o.get("inet_up"),
                "inet_down_cost": o.get("inet_down_cost"),
                "inet_up_cost": o.get("inet_up_cost"),
                "internet_down_cost_per_tb": o.get("internet_down_cost_per_tb"),
                "internet_up_cost_per_tb": o.get("internet_up_cost_per_tb"),
                "cuda_max_good": o.get("cuda_max_good"),
                "driver_version": o.get("driver_version"),
                "machine_id": o.get("machine_id"),
            }
        )
    return out


def create_instance(
    offer_id: int,
    *,
    image: str,
    disk_gb: int = 32,
    runtype: str = "ssh_direct",
    label: Optional[str] = None,
    onstart: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    price: Optional[float] = None,
) -> Dict[str, Any]:
    """
    PUT /asks/{offer_id}/ — create instance. Response includes new_contract (instance id).
    """
    api_key = require_vast_api_key()
    body: Dict[str, Any] = {
        "image": image,
        "disk": int(disk_gb),
        "runtype": runtype,
    }
    if label:
        body["label"] = label
    if onstart:
        body["onstart"] = onstart
    if env:
        body["env"] = env
    if price is not None:
        body["price"] = float(price)

    url = f"{_api_root()}/asks/{int(offer_id)}/"
    with httpx.Client(timeout=120.0) as client:
        r = client.put(url, headers=_bearer_headers(api_key), json=body)
        try:
            data = r.json()
        except Exception:
            data = {"raw": (r.text or "")[:2000]}
        if r.status_code >= 400:
            raise RuntimeError(f"vast create_instance HTTP {r.status_code}: {data}")
    return data if isinstance(data, dict) else {"result": data}


def destroy_instance(instance_id: int) -> Dict[str, Any]:
    """DELETE /instances/{id}/ — destroy instance (irreversible)."""
    api_key = require_vast_api_key()
    url = f"{_api_root()}/instances/{int(instance_id)}/"
    with httpx.Client(timeout=60.0) as client:
        r = client.delete(url, headers=_bearer_headers(api_key))
        try:
            data = r.json()
        except Exception:
            data = {"raw": (r.text or "")[:2000]}
        if r.status_code >= 400:
            raise RuntimeError(f"vast destroy_instance HTTP {r.status_code}: {data}")
    return data if isinstance(data, dict) else {"result": data}


def default_gpu_name_list() -> List[str]:
    raw = (get_settings().VAST_DEFAULT_GPU_NAMES or "").strip()
    if not raw:
        return ["RTX 3060", "RTX 4060"]
    return [x.strip() for x in raw.split(",") if x.strip()]


def pick_first_verified_bundle_offer(
    gpu_names: List[str],
    *,
    search_limit: int = 48,
) -> Dict[str, Any]:
    """
    Auto-pick the first GPU offer that is verified (API filter + client-side skip if verified=false).
    Raises RuntimeError if none match.
    """
    s = get_settings()
    single_gpu = bool(getattr(s, "VAST_TRANSCODE_SINGLE_GPU_ONLY", True))
    search_kw: Dict[str, Any] = {"verified": True, "limit": search_limit}
    if single_gpu:
        search_kw["num_gpus_eq"] = 1
    raw = search_offers(gpu_names, **search_kw)
    for o in raw:
        if not isinstance(o, dict):
            continue
        if o.get("verified") is False:
            continue
        if single_gpu:
            ng = o.get("num_gpus")
            try:
                if ng is not None and int(ng) != 1:
                    continue
            except (TypeError, ValueError):
                continue
        oid = o.get("id")
        if oid is not None:
            return o
    raise RuntimeError(
        "Aucune offre GPU vérifiée (verified) ne correspond aux filtres actuels "
        "(VAST_MAX_DPH_PER_HOUR, VAST_MAX_BANDWIDTH_USD_PER_TB, VAST_DEFAULT_GPU_NAMES"
        + (", une seule GPU requise (VAST_TRANSCODE_SINGLE_GPU_ONLY)" if single_gpu else "")
        + "). Élargissez les noms de GPU ou augmentez le plafond $/h"
        + (" ou désactivez VAST_TRANSCODE_SINGLE_GPU_ONLY si l'API n'a aucune offre 1×GPU" if single_gpu else "")
        + "."
    )
