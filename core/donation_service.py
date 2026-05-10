"""Fetch on-chain balances and EUR spot prices for donation tracking (CoinGecko)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

MEMPOOL_ADDRESS_API = "https://mempool.space/api/address"
POLYGON_RPC = "https://polygon-bor.publicnode.com"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
XRPL_JSON_RPC = "https://xrplcluster.com"
TRONGRID_ACCOUNT_API = "https://api.trongrid.io/v1/accounts"
COINGECKO_SIMPLE = "https://api.coingecko.com/api/v3/simple/price"

USER_AGENT = "RedwoodPlus-Donations/1.0 (self-hosted; contact: admin)"

COIN_IDS = "bitcoin,matic-network,solana,ripple,tron"


def _fetch_eur_prices(client: httpx.Client) -> Dict[str, float]:
    r = client.get(
        COINGECKO_SIMPLE,
        params={"ids": COIN_IDS, "vs_currencies": "eur"},
        headers={"User-Agent": USER_AGENT},
        timeout=25.0,
    )
    r.raise_for_status()
    data = r.json()
    return {
        "btc": float((data.get("bitcoin") or {}).get("eur") or 0),
        "matic": float((data.get("matic-network") or {}).get("eur") or 0),
        "sol": float((data.get("solana") or {}).get("eur") or 0),
        "xrp": float((data.get("ripple") or {}).get("eur") or 0),
        "trx": float((data.get("tron") or {}).get("eur") or 0),
    }


def _fetch_btc_balance(client: httpx.Client, address: str) -> Tuple[Optional[float], Optional[str]]:
    addr = (address or "").strip()
    if not addr:
        return None, None
    try:
        r = client.get(
            f"{MEMPOOL_ADDRESS_API}/{addr}",
            headers={"User-Agent": USER_AGENT},
            timeout=30.0,
        )
        if r.status_code == 404:
            return 0.0, None
        r.raise_for_status()
        j = r.json()
        cs = j.get("chain_stats") or {}
        funded = int(cs.get("funded_txo_sum") or 0)
        spent = int(cs.get("spent_txo_sum") or 0)
        sats = max(0, funded - spent)
        return sats / 1e8, None
    except Exception as exc:
        logger.warning("BTC balance fetch failed: %s", exc)
        return None, str(exc)[:220]


def _fetch_polygon_native_balance(
    client: httpx.Client, address: str
) -> Tuple[Optional[float], Optional[str]]:
    addr = (address or "").strip()
    if not addr:
        return None, None
    if not re.match(r"^0x[a-fA-F0-9]{40}$", addr):
        return None, "Adresse Polygon invalide (0x + 40 caractères hex)."
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getBalance",
        "params": [addr, "latest"],
    }
    try:
        r = client.post(
            POLYGON_RPC,
            json=payload,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
            timeout=30.0,
        )
        r.raise_for_status()
        j = r.json()
        err = j.get("error")
        if err:
            return None, str(err)[:220]
        res = j.get("result")
        if res in (None, "0x"):
            return 0.0, None
        wei = int(res, 16)
        return wei / 1e18, None
    except Exception as exc:
        logger.warning("Polygon balance fetch failed: %s", exc)
        return None, str(exc)[:220]


def _fetch_solana_balance(client: httpx.Client, address: str) -> Tuple[Optional[float], Optional[str]]:
    addr = (address or "").strip()
    if not addr:
        return None, None
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [addr]}
    try:
        r = client.post(
            SOLANA_RPC,
            json=payload,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
            timeout=30.0,
        )
        r.raise_for_status()
        j = r.json()
        err = j.get("error")
        if err:
            return None, str(err)[:220]
        result = j.get("result")
        if isinstance(result, dict):
            lamports = int(result.get("value", 0))
        else:
            lamports = int(result or 0)
        return lamports / 1e9, None
    except Exception as exc:
        logger.warning("Solana balance fetch failed: %s", exc)
        return None, str(exc)[:220]


def _fetch_xrp_balance(client: httpx.Client, address: str) -> Tuple[Optional[float], Optional[str]]:
    """Classic XRP Ledger address (starts with r…); balance via rippled account_info."""
    addr = (address or "").strip()
    if not addr:
        return None, None
    if not addr.startswith("r") or not (25 <= len(addr) <= 36):
        return None, "Adresse XRP invalide (format classique r… attendu)."
    payload = {
        "method": "account_info",
        "params": [
            {
                "account": addr,
                "ledger_index": "validated",
                "strict": True,
            }
        ],
    }
    try:
        r = client.post(
            XRPL_JSON_RPC,
            json=payload,
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
            timeout=30.0,
        )
        r.raise_for_status()
        j = r.json()
        result = j.get("result") or {}
        if result.get("error") == "actNotFound":
            return 0.0, None
        if result.get("error"):
            msg = result.get("error_message") or result.get("error_string") or result.get("error")
            return None, str(msg)[:220]
        ad = result.get("account_data") or {}
        raw = ad.get("Balance")
        if raw is None:
            return 0.0, None
        drops = int(str(raw))
        return drops / 1e6, None
    except Exception as exc:
        logger.warning("XRP balance fetch failed: %s", exc)
        return None, str(exc)[:220]


def _fetch_tron_balance(client: httpx.Client, address: str) -> Tuple[Optional[float], Optional[str]]:
    """Native TRX balance via TronGrid (balance in SUN, 1 TRX = 1e6 SUN)."""
    addr = (address or "").strip()
    if not addr:
        return None, None
    if not (addr.startswith("T") and len(addr) == 34):
        return None, "Adresse Tron invalide (T + 33 caractères base58 attendus)."
    try:
        r = client.get(
            f"{TRONGRID_ACCOUNT_API}/{addr}",
            headers={"User-Agent": USER_AGENT},
            timeout=30.0,
        )
        if r.status_code == 404:
            return 0.0, None
        r.raise_for_status()
        j = r.json()
        data = j.get("data")
        if not data:
            return 0.0, None
        bal = int((data[0] or {}).get("balance") or 0)
        return bal / 1e6, None
    except Exception as exc:
        logger.warning("Tron balance fetch failed: %s", exc)
        return None, str(exc)[:220]


def compute_donation_snapshot(addresses: Dict[str, Optional[str]]) -> Dict[str, Any]:
    """
    Read optional per-chain deposit addresses and return balances + EUR conversion.
    Keys: btc, polygon, solana, xrp, tron.
    """
    errors: Dict[str, str] = {}
    balances: Dict[str, Optional[float]] = {}

    with httpx.Client(follow_redirects=True) as client:
        try:
            prices = _fetch_eur_prices(client)
        except Exception as exc:
            logger.exception("CoinGecko EUR prices failed")
            raise ValueError(f"Prix EUR indisponibles (CoinGecko) : {exc}") from exc

        b, err = _fetch_btc_balance(client, addresses.get("btc") or "")
        balances["btc"] = b
        if err:
            errors["btc"] = err

        b, err = _fetch_polygon_native_balance(client, addresses.get("polygon") or "")
        balances["polygon"] = b
        if err:
            errors["polygon"] = err

        b, err = _fetch_solana_balance(client, addresses.get("solana") or "")
        balances["solana"] = b
        if err:
            errors["solana"] = err

        b, err = _fetch_xrp_balance(client, addresses.get("xrp") or "")
        balances["xrp"] = b
        if err:
            errors["xrp"] = err

        b, err = _fetch_tron_balance(client, addresses.get("tron") or "")
        balances["tron"] = b
        if err:
            errors["tron"] = err

    eur_by_asset: Dict[str, float] = {}
    raised = 0.0
    if balances.get("btc") is not None:
        v = balances["btc"] * prices["btc"]
        eur_by_asset["btc"] = round(v, 2)
        raised += v
    if balances.get("polygon") is not None:
        v = balances["polygon"] * prices["matic"]
        eur_by_asset["polygon"] = round(v, 2)
        raised += v
    if balances.get("solana") is not None:
        v = balances["solana"] * prices["sol"]
        eur_by_asset["solana"] = round(v, 2)
        raised += v
    if balances.get("xrp") is not None:
        v = balances["xrp"] * prices["xrp"]
        eur_by_asset["xrp"] = round(v, 2)
        raised += v
    if balances.get("tron") is not None:
        v = balances["tron"] * prices["trx"]
        eur_by_asset["tron"] = round(v, 2)
        raised += v

    return {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "prices_eur": {k: round(v, 8) for k, v in prices.items()},
        "balances": {k: (round(v, 8) if v is not None else None) for k, v in balances.items()},
        "eur_by_asset": eur_by_asset,
        "raised_eur": round(raised, 2),
        "errors": errors,
        "price_source": "coingecko",
    }
