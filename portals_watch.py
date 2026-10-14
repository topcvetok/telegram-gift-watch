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
from alert_tracking import mark_offer_sold, pending_offer_ids, remember_alert
from market_data import (
    apply_activity_evidence,
    complete_market_floors,
    record_market_activity,
)
from runtime_settings import effective_config
from tonnel_watch import (
    SourceRateLimited,
    allowed_backdrop,
    apply_sales_evidence,
    best_score,
    decimal,
    eligible,
    fetch_collection,
    load_rate,
    load_variant_books,
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


def fetch_nft(session, token, gift_id):
    payload = request(session, f"/nfts/{gift_id}", token=token)
    if not isinstance(payload, dict):
        raise RuntimeError("Portals изменил схему карточки подарка")
    return payload


def refresh_sold_alerts(config, secrets, db, session, token="", send=True):
    ids = pending_offer_ids(
        db, "Portals",
        interval=int(config.get("portals_sold_check_interval_seconds", 30)),
        limit=int(config.get("portals_sold_check_batch", 5)),
    )
    changed = 0
    for gift_id in ids:
        try:
            raw = fetch_nft(session, token, gift_id)
        except (RuntimeError, PortalsRateLimited):
            continue
        if str(raw.get("status") or "").casefold() != "sold":
            continue
        changed += int(mark_offer_sold(
            db, "Portals", gift_id, secrets,
            sale_price=raw.get("price"), send=send,
        ))
    return changed


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


def collection_stats(db, session, token, collection, config):
    """Return official Portals collection activity, with a short local cache."""
    db.execute(
        "CREATE TABLE IF NOT EXISTS portals_collection_stats ("
        "name TEXT PRIMARY KEY, payload TEXT, checked REAL)"
    )
    row = db.execute(
        "SELECT payload, checked FROM portals_collection_stats WHERE name=?", (collection,)
    ).fetchone()
    ttl = max(30, int(config.get("portals_stats_cache_seconds", 60)))
    if row and time.time() - float(row[1]) < ttl:
        try:
            return json.loads(row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    try:
        payload = request(
            session, "/collections", params={"search": collection, "limit": 10}, token=token
        )
        rows = payload.get("collections") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError("Portals изменил схему статистики коллекций")
        match = next(
            (entry for entry in rows
             if str(entry.get("name", "")).casefold() == collection.casefold()),
            None,
        )
        if not isinstance(match, dict):
            raise RuntimeError(f"Portals не нашёл статистику {collection}")
        db.execute(
            "INSERT OR REPLACE INTO portals_collection_stats VALUES (?, ?, ?)",
            (collection, json.dumps(match), time.time()),
        )
        db.commit()
        return match
    except (RuntimeError, PortalsRateLimited):
        if row:
            try:
                return json.loads(row[0])
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
        raise


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


def comparable_model_floor(item, model_book, config):
    """Return the stable floor for the listing's exact model."""
    if not item.get("model"):
        return None
    return stable_floor(
        item, model_book, config["variant_min_comparables"],
        config["variant_floor_cluster_max_spread_fraction"],
    )


def tonnel_model_floor(item, result, config, db):
    tonnel_config = dict(config)
    tonnel_config.setdefault("collection_floor_cache_seconds", 20)
    tonnel_config.setdefault("variant_cache_seconds", 20)
    tonnel_config.setdefault("variant_workers", 1)
    model_item = dict(item, backdrop=None)
    model_books = load_variant_books(
        db, item["name"], [model_item], result["rate"], tonnel_config, [1]
    )
    model_book = model_books.get(("model", item.get("model")), [])
    return comparable_model_floor(item, model_book, config)


def portals_model_floor(item, config, db, session, token):
    model_book = []
    if item.get("model"):
        model_book = cached_book(
            db, session, token, collection=item["name"],
            collection_id=item.get("collection_id") or None,
            model=item["model"], config=config,
        )
    return comparable_model_floor(item, model_book, config)


def mrkt_model_floor(item, config, db, session, token):
    from mrkt_watch import cached_book as mrkt_cached_book

    model_book = []
    if item.get("model"):
        model_book = mrkt_cached_book(
            db, session, token, collection=item["name"],
            model=item["model"], config=config,
        )
    return comparable_model_floor(item, model_book, config)


def add_portals_floor(
        item, result, config, secrets, db, *, base_market="Tonnel",
        portals_session=None, portals_token=None, mrkt_session=None, mrkt_token=None):
    """Attach exact-model floors from every available marketplace."""
    if result is None:
        return None
    enriched = dict(result)
    floors = []
    stats = {}

    try:
        model_floor = tonnel_model_floor(item, enriched, config, db)
        floors.append({"market": "Tonnel", "model_floor": model_floor})
    except (RuntimeError, SourceRateLimited):
        pass

    token = str(
        portals_token if portals_token is not None else secrets.get("portals_token", "")
    ).strip()

    def collect_portals(session):
        nonlocal stats
        try:
            model_floor = portals_model_floor(
                item, config, db, session, token
            )
            floors.append({"market": "Portals", "model_floor": model_floor})
        except (RuntimeError, PortalsRateLimited):
            pass
        try:
            stats = collection_stats(db, session, token, item["name"], config)
        except (RuntimeError, PortalsRateLimited):
            pass

    if portals_session is None:
        with requests.Session(
            impersonate="chrome",
            curl_options={CurlOpt.HTTP_VERSION: CurlHttpVersion.V1_1},
        ) as session:
            collect_portals(session)
    else:
        collect_portals(portals_session)

    token = str(
        mrkt_token if mrkt_token is not None else secrets.get("mrkt_token", "")
    ).strip()
    if token:
        from mrkt_watch import MrktAuthError, MrktRateLimited

        def collect_mrkt(session):
            try:
                model_floor = mrkt_model_floor(
                    item, config, db, session, token
                )
                floors.append({"market": "MRKT", "model_floor": model_floor})
            except (RuntimeError, MrktAuthError, MrktRateLimited):
                pass

        if mrkt_session is None:
            with requests.Session(impersonate="chrome") as session:
                collect_mrkt(session)
        else:
            collect_mrkt(mrkt_session)

    enriched["market_floors"] = floors
    enriched = complete_market_floors(item, enriched, db, config)
    return apply_activity_evidence(
        item, enriched, db, config,
        portals_sales_24h=stats.get("sales_24h_count", 0),
    )


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


def listing_link(item):
    return APP_URL + "?startapp=gift_" + str(item["gift_id"])


def format_alert(item, result, config):
    return format_market_alert(item, result, market="Portals")


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
    gift_slug = slug(item)
    preview_url = f"https://t.me/nft/{gift_slug}" if gift_slug else None
    message_id = telegram(
        secrets, text, preview_url=preview_url, button_url=listing_link(item)
    )
    remember_alert(db, "Portals", item, result, message_id)
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
    result = add_portals_floor(
        item, result, config, secrets, db, base_market="Portals",
        portals_session=session, portals_token=token,
    )
    return send_if_new_price(item, result, config, secrets, db, send)


def run_cycle(config, secrets, db, session, token="", send=True):
    config = effective_config(config, db)
    latest = fetch_listings(
        session, token, sort="listed_at desc", limit=int(config.get("portals_feed_limit", 20))
    )
    refresh_sold_alerts(config, secrets, db, session, token, send)
    db.execute(
        "CREATE TABLE IF NOT EXISTS portals_seen "
        "(id TEXT PRIMARY KEY, price TEXT, processed REAL)"
    )
    delivered = 0
    checked = 0
    for item in reversed(latest):
        key = str(item["gift_id"])
        old = db.execute("SELECT price FROM portals_seen WHERE id=?", (key,)).fetchone()
        old_price = decimal(old[0]) if old else None
        if old_price is not None and item["price"] == old_price:
            continue
        kind = "listing" if old_price is None else "price"
        record_market_activity(
            db, f"portals:{key}:{kind}:{item['price']}", "portals", item.get("name"), kind,
            parse_time(item.get("listed_at")) or time.time(),
        )
        if old_price is not None and item["price"] > old_price:
            db.execute(
                "INSERT OR REPLACE INTO portals_seen VALUES (?, ?, ?)",
                (key, str(item["price"]), time.time()),
            )
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
