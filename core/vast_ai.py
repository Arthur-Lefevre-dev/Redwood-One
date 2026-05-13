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


def parse_iso_country_codes(raw: Optional[str]) -> List[str]:
    """Parse comma-separated ISO 3166-1 alpha-2 codes (e.g. 'CN,RU' -> ['CN','RU'])."""
    if not raw or not str(raw).strip():
        return []
    out: List[str] = []
    for part in str(raw).split(","):
        c = part.strip().upper()
        if len(c) == 2 and c.isalpha():
            out.append(c)
    return out


def parse_skip_int_ids(raw: Optional[str]) -> set[int]:
    """Parse comma-separated integer ids (e.g. VAST_SKIP_MACHINE_IDS). Invalid or empty tokens are ignored."""
    if not raw or not str(raw).strip():
        return set()
    out: set[int] = set()
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.add(int(part))
        except ValueError:
            continue
    return out


def country_code_from_vast_geolocation(geo: Any) -> Optional[str]:
    """Vast returns geolocation like 'Shanghai, CN' — return trailing alpha-2 if present."""
    if not isinstance(geo, str):
        return None
    parts = [p.strip() for p in geo.split(",") if p.strip()]
    if not parts:
        return None
    last = parts[-1].strip().upper()
    if len(last) == 2 and last.isalpha():
        return last
    return None


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
    min_inet_down_mbps: Optional[float] = None,
    min_inet_up_mbps: Optional[float] = None,
    exclude_geolocation_codes: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    POST /bundles/ — returns a list of offer dicts (subset of fields kept as-is).

    When max_bandwidth_usd_per_tb is set, applies inet_down_cost / inet_up_cost
    lte in $/GB (API fields), i.e. max_usd_per_tb / 1024 per GB.

    When num_gpus_eq is set, the bundles filter uses num_gpus eq that value (overrides num_gpus_min).

    When min_inet_down_mbps / min_inet_up_mbps are > 0 (defaults from settings), adds inet_down / inet_up
    gte filters (speeds in Mb/s per Vast API).

    When exclude_geolocation_codes is None, uses VAST_EXCLUDE_GEOLOCATION_CODES from settings (default CN).
    Pass [] to disable geolocation exclusion for this call.
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

    down_floor = (
        float(min_inet_down_mbps)
        if min_inet_down_mbps is not None
        else float(getattr(s, "VAST_MIN_INET_DOWN_MBPS", 0.0) or 0.0)
    )
    up_floor = (
        float(min_inet_up_mbps)
        if min_inet_up_mbps is not None
        else float(getattr(s, "VAST_MIN_INET_UP_MBPS", 0.0) or 0.0)
    )

    if exclude_geolocation_codes is None:
        exclude_geo = parse_iso_country_codes(getattr(s, "VAST_EXCLUDE_GEOLOCATION_CODES", None) or "")
    else:
        exclude_geo = [str(x).strip().upper() for x in exclude_geolocation_codes if str(x).strip()]
        exclude_geo = [c for c in exclude_geo if len(c) == 2 and c.isalpha()]

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
    if down_floor > 0:
        payload["inet_down"] = {"gte": down_floor}
    if up_floor > 0:
        payload["inet_up"] = {"gte": up_floor}
    if exclude_geo:
        payload["geolocation"] = {"notin": exclude_geo}
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
        try:
            idown = float(o.get("inet_down")) if o.get("inet_down") is not None else None
        except (TypeError, ValueError):
            idown = None
        try:
            iup = float(o.get("inet_up")) if o.get("inet_up") is not None else None
        except (TypeError, ValueError):
            iup = None
        if down_floor > 0 and idown is not None and idown < down_floor:
            continue
        if up_floor > 0 and iup is not None and iup < up_floor:
            continue
        if down_floor > 0 and idown is None:
            continue
        if up_floor > 0 and iup is None:
            continue
        if exclude_geo:
            gcc = country_code_from_vast_geolocation(o.get("geolocation"))
            if gcc and gcc in exclude_geo:
                continue
        out.append(
            {
                "id": oid,
                "gpu_name": o.get("gpu_name"),
                "num_gpus": o.get("num_gpus"),
                "dph_total": o.get("dph_total"),
                "reliability": o.get("reliability"),
                "verified": o.get("verified"),
                "geolocation": o.get("geolocation"),
                "inet_down": o.get("inet_down"),
                "inet_up": o.get("inet_up"),
                "inet_down_cost": o.get("inet_down_cost"),
                "inet_up_cost": o.get("inet_up_cost"),
                "internet_down_cost_per_tb": o.get("internet_down_cost_per_tb"),
                "internet_up_cost_per_tb": o.get("internet_up_cost_per_tb"),
                "cuda_max_good": o.get("cuda_max_good"),
                "driver_version": o.get("driver_version"),
                "machine_id": o.get("machine_id"),
                "host_id": o.get("host_id"),
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


def is_no_such_ask_error(exc: BaseException) -> bool:
    """True if create_instance failed because the bundle offer id is gone (stale admin pick)."""
    s = str(exc).lower()
    return "no_such_ask" in s or "is not available" in s


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
            # Idempotent cleanup: instance may already be gone (SIGTERM, failed create, provider reclaim).
            if r.status_code == 404 and isinstance(data, dict) and data.get("error") == "no_such_instance":
                logger.info("vast destroy_instance: instance %s already absent (404)", instance_id)
                return {"success": True, "already_deleted": True, **data}
            raise RuntimeError(f"vast destroy_instance HTTP {r.status_code}: {data}")
    return data if isinstance(data, dict) else {"result": data}


def default_gpu_name_list() -> List[str]:
    raw = (get_settings().VAST_DEFAULT_GPU_NAMES or "").strip()
    if not raw:
        return ["RTX 3060", "RTX 4060"]
    return [x.strip() for x in raw.split(",") if x.strip()]


def usable_gpu_name_list() -> List[str]:
    """Lower-priority GPUs (e.g. entry Turing) — still fine for NVENC; not used by pick_first by default."""
    raw = (getattr(get_settings(), "VAST_USABLE_GPU_NAMES", None) or "").strip()
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


def vast_gpu_names_for_tier(tier: str) -> List[str]:
    """
    Resolve GPU name list when the admin does not pass an explicit `gpu` query.
    tier: default | usable | all (all = default first, then usable, deduped case-insensitively).
    """
    t = (tier or "default").strip().lower()
    if t == "usable":
        return usable_gpu_name_list()
    if t in ("all", "combined", "default+usable"):
        out: List[str] = []
        seen: set[str] = set()
        for n in default_gpu_name_list() + usable_gpu_name_list():
            k = n.casefold()
            if k in seen:
                continue
            seen.add(k)
            out.append(n)
        return out
    return default_gpu_name_list()


def pick_first_verified_bundle_offer(
    gpu_names: List[str],
    *,
    search_limit: int = 64,
    skip_offer_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """
    Auto-pick the first GPU offer that is verified (API filter + client-side skip if verified=false).
    skip_offer_ids: offer ids to skip (e.g. hosts that never exposed /dev/nvidia0).
    Raises RuntimeError if none match.
    """
    s = get_settings()
    single_gpu = bool(getattr(s, "VAST_TRANSCODE_SINGLE_GPU_ONLY", True))
    search_kw: Dict[str, Any] = {"verified": True, "limit": search_limit}
    if single_gpu:
        search_kw["num_gpus_eq"] = 1
    skip: set[int] = set()
    if skip_offer_ids:
        for x in skip_offer_ids:
            try:
                skip.add(int(x))
            except (TypeError, ValueError):
                continue
    skip_machines = parse_skip_int_ids(getattr(s, "VAST_SKIP_MACHINE_IDS", None) or "")
    skip_hosts = parse_skip_int_ids(getattr(s, "VAST_SKIP_HOST_IDS", None) or "")
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
        if oid is None:
            continue
        try:
            ioid = int(oid)
        except (TypeError, ValueError):
            continue
        if ioid in skip:
            continue
        mid = o.get("machine_id")
        if skip_machines and mid is not None:
            try:
                if int(mid) in skip_machines:
                    continue
            except (TypeError, ValueError):
                pass
        hid = o.get("host_id")
        if skip_hosts and hid is not None:
            try:
                if int(hid) in skip_hosts:
                    continue
            except (TypeError, ValueError):
                pass
        return o
    raise RuntimeError(
        "Aucune offre GPU vérifiée (verified) ne correspond aux filtres actuels "
        "(VAST_MAX_DPH_PER_HOUR, VAST_MAX_BANDWIDTH_USD_PER_TB, VAST_MIN_INET_DOWN_MBPS, "
        "VAST_MIN_INET_UP_MBPS, VAST_SKIP_MACHINE_IDS, VAST_SKIP_HOST_IDS, noms GPU — auto-pic : "
        "VAST_DEFAULT_GPU_NAMES ; GPU secondaires : VAST_USABLE_GPU_NAMES, voir "
        "GET /api/admin/vast/offers?gpu_tier=usable ou all"
        + (", une seule GPU requise (VAST_TRANSCODE_SINGLE_GPU_ONLY)" if single_gpu else "")
        + "). Élargissez les noms de GPU, relevez les plafonds $/h ou réseau, ou baissez les débits min. (Mb/s)"
        + (" ou désactivez VAST_TRANSCODE_SINGLE_GPU_ONLY si l'API n'a aucune offre 1×GPU" if single_gpu else "")
        + "."
    )
