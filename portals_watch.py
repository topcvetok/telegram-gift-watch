"""Read-only Portals Market deal monitor with Telegram notifications."""

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from decimal import Decimal

try:
    from curl_cffi import requests
    from curl_cffi.const import CurlHttpVersion, CurlOpt
except ImportError:
    raise SystemExit("Не установлен curl_cffi. Запусти start_watch.command.")

from alert_format import format_market_alert
from bot import ROOT, telegram
from tonnel_watch import (
    allowed_backdrop,
    apply_sales_evidence,
    best_score,
    decimal,
    eligible,
    fetch_collection,
    load_rate,
    slug,
)


API_URL = "https://portal-market.com/api"
APP_URL = "https://t.me/portals/market"


class PortalsRateLimited(RuntimeError):
    def __init__(self, retry_after=30):
        self.retry_after = max(5, int(retry_after or 30))
        super().__init__(f"Portals ограничил запросы; повтор через {self.retry_after} сек.")


def headers(token=""):
    result = {
        "accept": "application/json, text/plain, */*",
        "accept-encoding": "identity",
        "origin": "https://portal-market.com",
        "referer": "https://portal-market.com/",
    }
    if token:
        result["authorization"] = token
    return result


def request(session, path, *, params=None, token="", attempts=1):
    last_status = None
    for attempt in range(attempts):
        try:
            response = session.get(
                API_URL + path,
                params=params,
                headers=headers(token),
                timeout=8,
            )
            last_status = response.status_code
            if response.status_code == 200:
                return response.json()
            if response.status_code == 429:
                try:
                    delay = int(response.headers.get("retry-after", "30"))
                except (TypeError, ValueError):
                    delay = 30
                raise PortalsRateLimited(delay)
            if response.status_code not in (500, 502, 503, 504):
                break
        except PortalsRateLimited:
            raise
        except (OSError, ValueError):
            pass
        if attempt + 1 < attempts:
            time.sleep(attempt + 1)
    raise RuntimeError(f"Portals временно недоступен (HTTP {last_status or 'network'})")


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def portals_item(raw):
    if not isinstance(raw, dict) or raw.get("status") != "listed":
        return None
    try:
        price = decimal(raw.get("price"), "цена Portals")
    except ValueError:
        return None
    gift_id = raw.get("id")
    name = raw.get("name")
    if not gift_id or not isinstance(name, str) or not name or price <= 0:
        return None
    unlock = parse_time(raw.get("unlocks_at"))
    if unlock is not None and unlock > time.time():
        return None
    traits = {}
    for attribute in raw.get("attributes") or []:
        if isinstance(attribute, dict) and attribute.get("type") in ("model", "backdrop", "symbol"):
            traits[attribute["type"]] = attribute.get("value")
    number = raw.get("external_collection_number")
    try:
        number = int(number)
    except (TypeError, ValueError):
        number = None
    return {
        "gift_id": str(gift_id),
        "collection_id": str(raw.get("collection_id") or ""),
        "gift_num": number,
        "name": name,
        "model": traits.get("model"),
        "backdrop": traits.get("backdrop"),
        "symbol": traits.get("symbol"),
        "price": price,
        "status": "forsale",
        "asset": "TON",
        "auction": False,
        "dutchAuctionData": None,
        "premarketData": None,
        "underLoan": False,
        "listed_at": raw.get("listed_at"),
    }


def search_params(*, sort="price asc", limit=20, collection=None, collection_id=None,
                  model=None, backdrop=None):
    params = {"offset": 0, "limit": min(20, max(1, int(limit))), "sort_by": sort, "status": "listed"}
    if collection_id:
        params["collection_ids"] = collection_id
    elif collection:
        params["filter_by_collections"] = collection
    if model:
        params["filter_by_models"] = model
    if backdrop:
        params["filter_by_backdrops"] = backdrop
    return params


def fetch_listings(session, token="", *, sort="price asc", limit=20,
                   collection=None, collection_id=None, model=None, backdrop=None):
    payload = request(
        session,
        "/nfts/search",
        params=search_params(
            sort=sort, limit=limit, collection=collection, collection_id=collection_id,
            model=model, backdrop=backdrop
        ),
        token=token,
    )
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("Portals изменил схему каталога")
    return [item for raw in rows if (item := portals_item(raw)) is not None]


def resolve_collection_id(db, session, token, collection):
    db.execute(
        "CREATE TABLE IF NOT EXISTS portals_collections ("
        "name TEXT PRIMARY KEY, collection_id TEXT, checked REAL)"
    )
    row = db.execute(
        "SELECT collection_id, checked FROM portals_collections WHERE name=?", (collection,)
    ).fetchone()
    if row and time.time() - float(row[1]) < 86400:
        return row[0]
    payload = request(
        session, "/collections", params={"search": collection, "limit": 10}, token=token
    )
    rows = payload.get("collections") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("Portals изменил схему коллекций")
    match = next(
        (entry for entry in rows if str(entry.get("name", "")).casefold() == collection.casefold()),
        None,
    )
    if not match or not match.get("id"):
        raise RuntimeError(f"Portals не нашёл коллекцию {collection}")
    collection_id = str(match["id"])
    db.execute(
        "INSERT OR REPLACE INTO portals_collections VALUES (?, ?, ?)",
        (collection, collection_id, time.time()),
    )
    db.commit()
    return collection_id


def cached_book(db, session, token, *, collection, config, collection_id=None,
                model=None, backdrop=None):
    db.execute(
        "CREATE TABLE IF NOT EXISTS portals_books ("
        "collection TEXT, model TEXT, backdrop TEXT, payload TEXT, checked REAL, "
        "PRIMARY KEY (collection, model, backdrop))"
    )
    if not collection_id:
        collection_id = resolve_collection_id(db, session, token, collection)
    collection_key = collection_id or collection
    model_key = model or ""
    backdrop_key = backdrop or ""
    row = db.execute(
        "SELECT payload, checked FROM portals_books "
        "WHERE collection=? AND model=? AND backdrop=?",
        (collection_key, model_key, backdrop_key),
    ).fetchone()
    ttl = int(config.get("portals_book_cache_seconds", 20))
    if row and time.time() - float(row[1]) < ttl:
        try:
            book = json.loads(row[0], parse_float=Decimal)
            if not isinstance(book, list):
                raise ValueError("некорректный кеш Portals")
            for cached_item in book:
                if not isinstance(cached_item, dict):
                    raise ValueError("некорректный кеш Portals")
                cached_item["price"] = decimal(cached_item.get("price"), "цена Portals")
            return book
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    limit = max(int(config.get("floor_limit", 12)), int(config.get("variant_floor_limit", 10)))
    book = fetch_listings(
        session, token, limit=limit, collection=collection, collection_id=collection_id,
        model=model, backdrop=backdrop
    )
    db.execute(
        "INSERT OR REPLACE INTO portals_books VALUES (?, ?, ?, ?, ?)",
        (collection_key, model_key, backdrop_key, json.dumps(book, default=str), time.time()),
    )
    db.commit()
    return book


def stable_floor(item, book, minimum, max_spread):
    prices = []
    for other in book:
        try:
            if eligible(other) and str(other["gift_id"]) != str(item["gift_id"]):
                prices.append(decimal(other["price"], "цена аналога"))
        except (KeyError, TypeError, ValueError):
            continue
    prices.sort()
    minimum = int(minimum)
    if len(prices) < minimum:
        return None
    cluster = prices[:minimum]
    floor = cluster[0]
    if cluster[-1] > floor * (1 + decimal(max_spread, "разброс floor")):
        return None
    return floor


def basis_book(db, session, token, item, result, config):
    scope = result.get("basis_scope", "collection")
    kwargs = {
        "collection": item["name"],
        "collection_id": item.get("collection_id") or None,
        "config": config,
    }
    minimum = config["min_comparables"]
    spread = config["floor_cluster_max_spread_fraction"]
    if scope == "model":
        kwargs["model"] = result.get("basis_trait")
        minimum = config["variant_min_comparables"]
        spread = config["variant_floor_cluster_max_spread_fraction"]
    elif scope == "backdrop":
        kwargs["backdrop"] = result.get("basis_trait")
        minimum = config["variant_min_comparables"]
        spread = config["variant_floor_cluster_max_spread_fraction"]
    book = cached_book(db, session, token, **kwargs)
    return book, minimum, spread


def add_portals_floor(item, result, config, secrets, db):
    """Add a comparable Portals floor without ever blocking a Tonnel alert."""
    if result is None:
        return None
    enriched = dict(result)
    floors = [{"market": "Tonnel", "floor": enriched.get("next_floor")}]
    if enriched.get("next_floor") is None:
        enriched["market_floors"] = []
        return enriched
    token = str(secrets.get("portals_token", "")).strip()
    try:
        with requests.Session(
            impersonate="chrome",
            curl_options={CurlOpt.HTTP_VERSION: CurlHttpVersion.V1_1},
        ) as session:
            book, minimum, spread = basis_book(db, session, token, item, enriched, config)
        floor = stable_floor(item, book, minimum, spread)
        if floor is not None:
            floors.append({"market": "Portals", "floor": floor})
    except (RuntimeError, PortalsRateLimited):
        pass
    enriched["market_floors"] = [entry for entry in floors if entry["floor"] is not None]
    return enriched


def tonnel_comparable_floor(item, result, config):
    scope = result.get("basis_scope", "collection")
    attributes = None
    minimum = config["min_comparables"]
    spread = config["floor_cluster_max_spread_fraction"]
    if scope == "model":
        attributes = {"model": result.get("basis_trait")}
        minimum = config["variant_min_comparables"]
        spread = config["variant_floor_cluster_max_spread_fraction"]
    elif scope == "backdrop":
        attributes = {"backdrop": result.get("basis_trait")}
        minimum = config["variant_min_comparables"]
        spread = config["variant_floor_cluster_max_spread_fraction"]
    book = fetch_collection(
        item["name"], {"price": 1, "gift_id": -1},
        max(int(minimum) + 2, int(config.get("floor_limit", 12))), attributes,
    )
    return stable_floor(item, book, minimum, spread)


def portals_scoring_config(config):
    result = dict(config)
    result["tonnel_round_trip_cost_fraction"] = config.get(
        "portals_round_trip_cost_fraction", 0.02
    )
    return result


def format_alert(item, result, config):
    gift_slug = slug(item)
    gift_link = f"https://t.me/nft/{gift_slug}" if gift_slug else ""
    listing_link = APP_URL + "?startapp=" + str(item["gift_id"])
    return format_market_alert(
        item, result, market="Portals", listing_link=listing_link, gift_link=gift_link
    )


def send_if_new_price(item, result, config, secrets, db, send=True):
    db.execute(
        "CREATE TABLE IF NOT EXISTS portals_alerts "
        "(id TEXT PRIMARY KEY, price TEXT, sent REAL)"
    )
    key = str(item["gift_id"])
    old = db.execute("SELECT price FROM portals_alerts WHERE id=?", (key,)).fetchone()
    if old and result["price"] >= decimal(old[0]):
        return False
    text = format_alert(item, result, config)
    if not send:
        print(text + "\n")
        return False
    telegram(secrets, text)
    db.execute(
        "INSERT OR REPLACE INTO portals_alerts VALUES (?, ?, ?)",
        (key, str(result["price"]), time.time()),
    )
    db.commit()
    return True


def process_listing(item, config, secrets, db, session, token, send=True):
    rate = load_rate(db)
    if item["price"] * rate > decimal(config["max_lot_rub"], "лимит на лот"):
        return False
    collection_book = cached_book(
        db, session, token, collection=item["name"],
        collection_id=item.get("collection_id") or None, config=config
    )
    variant_books = {}
    if item.get("model"):
        variant_books[("model", item["model"])] = cached_book(
            db, session, token, collection=item["name"],
            collection_id=item.get("collection_id") or None,
            model=item["model"], config=config
        )
    if allowed_backdrop(item.get("backdrop")):
        variant_books[("backdrop", item["backdrop"])] = cached_book(
            db, session, token, collection=item["name"],
            collection_id=item.get("collection_id") or None,
            backdrop=item["backdrop"], config=config
        )
    score_config = portals_scoring_config(config)
    result = best_score(item, collection_book, variant_books, rate, score_config)
    result = apply_sales_evidence(item, result, db, score_config)
    if result is None:
        return False
    floors = [{"market": "Portals", "floor": result["next_floor"]}]
    try:
        floor = tonnel_comparable_floor(item, result, config)
        if floor is not None:
            floors.insert(0, {"market": "Tonnel", "floor": floor})
    except RuntimeError:
        pass
    result["market_floors"] = floors
    return send_if_new_price(item, result, config, secrets, db, send)


def run_cycle(config, secrets, db, session, token="", send=True):
    latest = fetch_listings(
        session, token, sort="listed_at desc", limit=int(config.get("portals_feed_limit", 20))
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS portals_seen "
        "(id TEXT PRIMARY KEY, price TEXT, processed REAL)"
    )
    delivered = 0
    checked = 0
    for item in reversed(latest):
        key = str(item["gift_id"])
        old = db.execute("SELECT price FROM portals_seen WHERE id=?", (key,)).fetchone()
        if old and item["price"] >= decimal(old[0]):
            continue
        checked += 1
        processed = False
        try:
            delivered += int(process_listing(item, config, secrets, db, session, token, send))
            processed = True
        except (RuntimeError, ValueError, KeyError, TypeError) as exc:
            print(f"Portals {key}: {exc}", file=sys.stderr, flush=True)
        if processed:
            db.execute(
                "INSERT OR REPLACE INTO portals_seen VALUES (?, ?, ?)",
                (key, str(item["price"]), time.time()),
            )
    db.execute("DELETE FROM portals_seen WHERE processed < ?", (time.time() - 8 * 86400,))
    db.commit()
    print(f"Portals: новых лотов {checked}; отправлено {delivered} офферов.", flush=True)
    return delivered


def run(config, secrets, db, *, send=True, duration=3300, fail_fast=False):
    token = str(secrets.get("portals_token", "")).strip()
    deadline = time.monotonic() + max(1, int(duration))
    interval = max(3, int(config.get("portals_poll_seconds", 5)))
    delivered = 0
    with requests.Session(
        impersonate="chrome",
        curl_options={CurlOpt.HTTP_VERSION: CurlHttpVersion.V1_1},
    ) as session:
        while time.monotonic() < deadline:
            delay = interval
            try:
                delivered += run_cycle(config, secrets, db, session, token, send)
            except PortalsRateLimited as exc:
                if fail_fast:
                    raise
                delay = min(exc.retry_after, 300)
                print(str(exc), file=sys.stderr, flush=True)
            except RuntimeError as exc:
                if fail_fast:
                    raise
                print(f"Portals: цикл пропущен: {exc}", file=sys.stderr, flush=True)
            time.sleep(min(delay, max(0, deadline - time.monotonic())))
    return delivered


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--duration", type=int, default=3300)
    args = parser.parse_args()
    config = json.loads((ROOT / "config.json").read_text())
    secrets = json.loads((ROOT / "secrets.json").read_text())
    duration = 1 if args.once else args.duration
    with sqlite3.connect(ROOT / "state.sqlite3", timeout=30) as db:
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA journal_mode=WAL")
        run(config, secrets, db, send=args.send, duration=duration, fail_fast=args.once)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"Не удалось запустить Portals: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("Мониторинг Portals остановлен.")
