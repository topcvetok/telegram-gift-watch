"""Shared cross-market floors and collection activity metrics."""

import json
import time
from decimal import Decimal, InvalidOperation

from curl_cffi import requests


CATALOG_URL = "https://gift-loot.com/api/catalog"
MARKETS = ("Tonnel", "Portals", "MRKT")


def _decimal(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() and result > 0 else None


def _cached_catalog(db, config):
    db.execute(
        "CREATE TABLE IF NOT EXISTS market_data_cache "
        "(key TEXT PRIMARY KEY, payload TEXT, checked REAL)"
    )
    row = db.execute(
        "SELECT payload, checked FROM market_data_cache WHERE key='gift_loot_catalog'"
    ).fetchone()
    ttl = max(30, int(config.get("cross_market_floor_cache_seconds", 120)))
    if row and time.time() - float(row[1]) < ttl:
        try:
            return json.loads(row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    try:
        response = requests.get(CATALOG_URL, impersonate="chrome", timeout=8)
        if response.status_code != 200:
            raise RuntimeError(f"Gift Loot HTTP {response.status_code}")
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("gifts"), list):
            raise RuntimeError("Gift Loot изменил схему каталога")
        db.execute(
            "INSERT OR REPLACE INTO market_data_cache VALUES (?, ?, ?)",
            ("gift_loot_catalog", json.dumps(payload), time.time()),
        )
        db.commit()
        return payload
    except (OSError, ValueError, RuntimeError, requests.RequestsError):
        if row:
            try:
                return json.loads(row[0])
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        payload = {"gifts": []}
        db.execute(
            "INSERT OR REPLACE INTO market_data_cache VALUES (?, ?, ?)",
            ("gift_loot_catalog", json.dumps(payload), time.time()),
        )
        db.commit()
        return payload


def catalog_collection_floors(item, db, config):
    """Return current collection floors from a public multi-market catalog."""
    name = str(item.get("name") or "").casefold()
    if not name:
        return {}
    payload = _cached_catalog(db, config)
    match = next(
        (
            gift for gift in payload.get("gifts", [])
            if isinstance(gift, dict) and str(gift.get("name") or "").casefold() == name
        ),
        None,
    )
    if not match:
        return {}
    return {
        "Tonnel": _decimal(match.get("tonnel_floor_ton")),
        "Portals": _decimal(match.get("portals_floor_ton")),
        "MRKT": _decimal(match.get("mrkt_floor_ton")),
    }


def complete_market_floors(item, result, db, config):
    """Keep direct stable floors and fill collection gaps from the public catalog."""
    if result is None:
        return None
    enriched = dict(result)
    direct = {}
    for entry in enriched.get("market_floors") or []:
        market = str(entry.get("market") or "")
        if market in MARKETS and entry.get("floor") is not None:
            direct[market] = dict(entry, verified=entry.get("verified", True))

    external = {}
    if enriched.get("basis_scope", "collection") == "collection":
        external = catalog_collection_floors(item, db, config)

    floors = []
    for market in MARKETS:
        if market in direct:
            floors.append(direct[market])
        elif external.get(market) is not None:
            floors.append({"market": market, "floor": external[market], "verified": False})
        else:
            floors.append({"market": market, "floor": None, "verified": False})
    enriched["market_floors"] = floors
    return enriched


def record_market_activity(db, event_id, market, gift_name, kind, occurred=None):
    """Store one deduplicated collection activity event."""
    if not event_id or not gift_name:
        return False
    normalized = {
        "sale": "sale",
        "listing": "listing",
        "price": "price",
        "delisting": "other",
    }.get(str(kind).casefold(), "other")
    db.execute(
        "CREATE TABLE IF NOT EXISTS market_activity ("
        "event_id TEXT PRIMARY KEY, market TEXT, gift_name TEXT, kind TEXT, occurred REAL)"
    )
    cursor = db.execute(
        "INSERT OR IGNORE INTO market_activity VALUES (?, ?, ?, ?, ?)",
        (str(event_id), str(market).casefold(), str(gift_name), normalized,
         float(occurred if occurred is not None else time.time())),
    )
    db.execute("DELETE FROM market_activity WHERE occurred < ?", (time.time() - 2 * 86400,))
    return cursor.rowcount > 0


def apply_activity_evidence(item, result, db, config, *, portals_sales_24h=0, now=None):
    """Calculate liquidity from all observed activity for the whole collection."""
    if result is None:
        return None
    db.execute(
        "CREATE TABLE IF NOT EXISTS market_activity ("
        "event_id TEXT PRIMARY KEY, market TEXT, gift_name TEXT, kind TEXT, occurred REAL)"
    )
    moment = float(now if now is not None else time.time())
    rows = db.execute(
        "SELECT kind, COUNT(*) FROM market_activity "
        "WHERE gift_name=? AND occurred>=? GROUP BY kind",
        (item.get("name"), moment - 86400),
    ).fetchall()
    counts = {str(kind): int(count) for kind, count in rows}
    portals_sales = max(0, int(portals_sales_24h or 0))
    sales = counts.get("sale", 0) + portals_sales
    listings = counts.get("listing", 0)
    price_changes = counts.get("price", 0)
    other = counts.get("other", 0)
    total = sales + listings + price_changes + other
    score = sales * 3 + listings + price_changes * 2 + other

    medium = int(config.get("liquidity_medium_activity_24h", 10))
    high = int(config.get("liquidity_high_activity_24h", 30))
    liquidity = "высокая" if score >= high else "средняя" if score >= medium else "низкая"

    enriched = dict(result)
    enriched.update({
        "liquidity": liquidity,
        "activity_24h": total,
        "activity_score_24h": score,
        "activity_sales_24h": sales,
        "activity_listings_24h": listings,
        "activity_price_changes_24h": price_changes,
        "activity_other_24h": other,
        "portals_sales_24h": portals_sales,
    })
    return enriched
