"""Rules for automatic BitTorrent download retries (Celery beat + worker)."""

from __future__ import annotations


def torrent_error_eligible_for_auto_retry(message: str) -> bool:
    """
    Return False when re-downloading would not help (bad payload, admin cancel, missing deps).
    English-only matching for stable logs.
    """
    m = (message or "").strip()
    if not m:
        return True
    low = m.lower()
    if "annulé par l'administrateur" in low or "annule par l'administrateur" in low:
        return False
    if "aria2c not installed" in m:
        return False
    if "invalid base64" in low:
        return False
    if "too small" in low and "torrent" in low:
        return False
    if "vast_api_key" in low and "manquante" in low:
        return False
    if "extension non prise en charge pour vast" in low:
        return False
    if "missing torrent source" in low:
        return False
    # Persisted row has no usable magnet / .torrent file (retry would not help).
    if "enregistrée pour ce film" in m or "enregistree pour ce film" in low:
        return False
    if "aucune source" in low and ("magnet" in low or ".torrent" in low):
        return False
    return True
