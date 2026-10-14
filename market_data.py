"""Shared cross-market model floors and collection activity metrics."""

import time
MARKETS = ("Tonnel", "Portals", "MRKT")


def complete_market_floors(item, result, db, config):
    """Keep only direct floors for the exact model, ordered by marketplace."""
    if result is None:
        return None
    enriched = dict(result)
    direct = {}
    for entry in enriched.get("market_floors") or []:
        market = str(entry.get("market") or "")
        if market not in MARKETS:
            continue
        row = direct.setdefault(market, {"market": market})
        if entry.get("model_floor") is not None:
            row["model_floor"] = entry["model_floor"]
            row["model_verified"] = entry.get(
                "model_verified", entry.get("verified", True)
            )
        # Accept the previous one-floor shape while rolling cached state forward.
        if entry.get("floor") is not None:
            scope = entry.get("scope") or enriched.get("basis_scope")
            if scope == "model":
                row["model_floor"] = entry["floor"]
                row["model_verified"] = entry.get("verified", True)

    floors = []
    for market in MARKETS:
        row = direct.get(market, {"market": market})
        row.setdefault("model_floor", None)
        row.setdefault("model_verified", False)
        floors.append(row)
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
